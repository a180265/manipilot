"""Metadata uses the same selected layout as camera observations, offline only."""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from drivers import calibration_bundle as cb, screen_proof
from runtime.config import PlatformConfig
from tests.test_layout_observation import LayoutRig, offline_only  # noqa: F401


@pytest.fixture
def api_rig(tmp_path, monkeypatch, request):
    rig = LayoutRig(tmp_path, monkeypatch, seed_selected=getattr(request, "param", True))
    rig.runtime.config = PlatformConfig({
        "hardware_mode": "simulation", "pre_enable_maintenance": False,
        "screen_ime_enabled": False, "wx_compat_enabled": False,
        "daemon_supervisor_enabled": False, "hotplug_discovery_enabled": False,
    }, root=tmp_path)
    rig.runtime.devices.get("arm-01")["daemon_session"] = rig.bundle["daemonSession"]
    app = create_app(rig.runtime)
    app.state.runtime = rig.runtime

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata must never capture a frame or start the runtime")

    monkeypatch.setattr(rig.camera, "_capture_for_operation", forbidden)
    monkeypatch.setattr(rig.runtime, "startup", forbidden)
    # No context manager: the ASGI requests intentionally do not enter lifespan.
    client = TestClient(app)
    yield rig, client
    client.close()
    rig.close()


def read_metadata(client):
    response = client.get("/v1/devices/arm-01/screen-proof")
    assert response.status_code == 200, response.text
    return response.json()


def test_selected_layout_metadata_does_not_require_legacy_registration(api_rig):
    rig, client = api_rig
    assert not screen_proof.profile_path("arm-01", rig.data).exists()
    metadata = read_metadata(client)
    assert metadata["usable"] is True, metadata
    assert metadata["registered"] is True
    assert metadata["regions_registered"] is True
    assert metadata["reference_states"] == ["conversation", "wechat_main"]
    assert metadata["revision"] == 1
    assert metadata["template_ref"] == rig.adaptation["template_ref"]
    assert metadata["blocking_reasons"] == []


def test_metadata_tracks_selected_version_and_revision(api_rig):
    rig, client = api_rig
    first = read_metadata(client)
    updated = copy.deepcopy(rig.adaptation)
    updated["version"] = 2
    rig.select(updated)
    second = read_metadata(client)
    assert first["usable"] is second["usable"] is True
    assert second["revision"] > first["revision"]
    assert second["adaptation_ref"]["version"] == 2


def test_shared_metadata_does_not_require_removed_input_acceptance(api_rig):
    rig, client = api_rig
    rig.bundle["status"] = cb.MOTION_READY_STATUS
    rig.bundle["acceptance"]["wechat"] = False
    cb.save_bundle(rig.bundle, rig.data)
    assert read_metadata(client)["usable"] is True


@pytest.mark.parametrize("api_rig", [False], indirect=True)
def test_absent_shared_selection_preserves_valid_legacy_metadata(api_rig):
    from tests.test_screen_proof import registered_scene

    rig, client = api_rig
    registered_scene(rig.data)
    result = read_metadata(client)
    assert result["registered"] is result["usable"] is True
    assert result["regions_registered"] is True
    assert result["reference_states"] == ["conversation"]
    assert "template_ref" not in result
    assert "regions" not in result and "image" not in result


@pytest.mark.parametrize("fault", [
    "missing-version", "calibration", "missing-edge", "ime", "daemon", "capability",
    "missing-colors", "missing-empty-input",
])
def test_selected_invalid_metadata_is_blocked_without_legacy_fallback(api_rig, monkeypatch, fault):
    rig, client = api_rig

    def no_fallback(*args, **kwargs):
        pytest.fail("An invalid selected layout must not read legacy registration")

    monkeypatch.setattr(screen_proof, "load_profile", no_fallback)
    if fault == "missing-version":
        (rig.data / "devices/arm-01/layout_adaptation/versions/1.json").unlink()
    elif fault in {"calibration", "missing-edge"}:
        changed = copy.deepcopy(rig.bundle)
        if fault == "calibration":
            changed["coordinateFrameHash"] = "d" * 64
        else:
            changed["screen"].pop("edgeModel")
        cb.save_bundle(changed, rig.data)
    elif fault in {"ime", "daemon", "capability"}:
        field, value = {
            "ime": ("ime_device_id", "different-phone"),
            "daemon": ("daemon_session", "different-session"),
            "capability": ("capabilities", []),
        }[fault]
        rig.runtime.devices.get("arm-01")[field] = value
    elif fault == "missing-colors":
        template = copy.deepcopy(rig.template)
        template["version"] = 2
        for page in template["pages"]:
            page["regions"] = [r for r in page["regions"] if r["purpose"] != "foreground"]
        rig.template_version(template)
    else:
        adaptation = copy.deepcopy(rig.adaptation)
        adaptation["version"] = 2
        for row in adaptation["color_samples"]:
            row["empty_input_asset_ref"] = None
        rig.select(adaptation)
    metadata = read_metadata(client)
    assert metadata["usable"] is False
    assert metadata["blocking_reasons"]
