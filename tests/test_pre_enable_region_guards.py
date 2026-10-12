"""真实动作门装配、粗框完整性和严格边缘拒绝的离线反例。"""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb
from runtime.phone_region_context import (
    RegionContextError, context_matches_bundle, load_region_context, locate_quad_in_region,
)
from tests.test_crop_registration_geometry import rig
from tests.test_layout_observation import layout_rig, offline_only  # noqa: F401
from tests.test_pre_enable_s2_region_gate_scroll import (
    BOX_LEFT, FH, FW, RegionCamera, _frame, _region, _v3_bundle,
)


def test_region_read_returns_selected_not_latest_version(rig):
    from runtime.layout_region import LayoutRegionService, canonical_digest

    newer = copy.deepcopy(rig.region)
    newer["version"] = 2
    newer["rect"]["x"] += .01
    newer["sha256"] = canonical_digest(
        {key: value for key, value in newer.items() if key != "sha256"})
    rig.regions.insert_version(newer["device_id"], 2, newer)
    view = LayoutRegionService(rig.regions).get(newer["device_id"])
    assert view["selected"]["version"] == 1
    assert view["selected"]["sha256"] == rig.region["sha256"]


def test_installed_region_missing_pointer_is_not_legacy(rig):
    from runtime.layout_region import LayoutRegionService, RegionError

    with rig.regions._conn:
        rig.regions._conn.execute("DELETE FROM layout_region_pointer")
    with pytest.raises(RegionError):
        LayoutRegionService(rig.regions).get(rig.region["device_id"])


def test_region_record_content_must_match_digest(rig):
    record = copy.deepcopy(rig.region)
    record["rect"]["width"] -= .1
    with rig.regions._conn:
        rig.regions._conn.execute(
            "UPDATE layout_region_versions SET json=? WHERE device_id=?",
            (json.dumps(record), record["device_id"]),
        )
    with pytest.raises(RegionContextError):
        load_region_context(rig.regions, record["device_id"])


def test_region_gradient_cannot_override_clipped_outer_edge(rig):
    image = cv2.imdecode(np.frombuffer(rig.image(), np.uint8), cv2.IMREAD_COLOR)
    tight = {"rect": {"x": .1, "y": .1, "width": .8, "height": .825}}
    saved = rig.calibration["screen"]["edgeModel"]["corners"]
    with pytest.raises(RegionContextError):
        locate_quad_in_region(image, tight, saved)


@pytest.mark.parametrize("fault", ["none", "missing_pointer", "digest", "binding", "tight"])
def test_quality_identification_and_shared_observation_use_same_region(
        layout_rig, monkeypatch, fault):
    from drivers import screen_proof
    from drivers.real_camera import RealCameraDriver

    rig = layout_rig
    pixels = rig.frame("chat_ime_empty")
    pixels[50:690, 750:1070] = rig.screens["chat_ime_empty"]
    rig.source.image = pixels
    store = rig.parts["region_store"]
    if fault == "missing_pointer":
        with store._conn:
            store._conn.execute("DELETE FROM layout_region_pointer")
    elif fault == "digest":
        record = copy.deepcopy(rig.region)
        record["rect"]["width"] -= .05
        with store._conn:
            store._conn.execute("UPDATE layout_region_versions SET json=?",
                                (json.dumps(record),))
    elif fault in {"binding", "tight"}:
        record = copy.deepcopy(rig.region)
        record["version"] = 2
        if fault == "binding":
            record["installation_binding"]["camera_device_unique_id"] = "other"
        else:
            record["rect"]["width"] = .2
            record["pixel_rect"]["right"] = 256
        rig.seal(record)
        store.insert_version("arm-01", 2, record)
        store.cas_pointer("arm-01", 1, 2, record["sha256"], "offline")
    original = RealCameraDriver._analyze
    analyzed = []

    def analyze(image, **kwargs):
        assert kwargs["search_region"] is not None, "No full-frame quality fallback"
        analyzed.append(copy.deepcopy(kwargs["search_region"]))
        return original(image, **kwargs)

    monkeypatch.setattr(RealCameraDriver, "_analyze", staticmethod(analyze))
    monkeypatch.setattr(screen_proof, "load_profile",
                        lambda *a, **k: pytest.fail("No legacy profile fallback"))

    async def run():
        quality = await rig.camera.check_quality()
        identified = await rig.camera.identify_ui_state()
        if fault == "none":
            observed = await rig.camera.observe_regions()
            assert quality["quality"] == "clear", quality
            assert identified["ui_state"] == observed["ui"]["ui_state"] == "conversation"
            assert len(analyzed) >= 2
            assert all(region == analyzed[0] for region in analyzed)
        else:
            assert quality["quality"] == "abnormal", quality
            assert identified["ui_state"] == "unknown", identified
            with pytest.raises((ValueError, RuntimeError)):
                await rig.camera.observe_regions()

    asyncio.run(run())


@pytest.mark.parametrize("key,bundle_key", [
    ("source_geometry_id", "sourceGeometryId"),
    ("camera_id", "cameraId"),
    ("camera_device_unique_id", "cameraDeviceUniqueId"),
    ("camera_binding_id", "cameraBindingId"),
])
@pytest.mark.parametrize("side", ["region", "bundle"])
@pytest.mark.parametrize("value", [None, ""])
def test_selected_region_requires_complete_identity(key, bundle_key, side, value):
    bundle = _v3_bundle()
    region = _region(BOX_LEFT)
    context = {"mode": "region", "region": region}
    context_matches_bundle(context, bundle)
    if side == "bundle":
        bundle[bundle_key] = value
    else:
        target = region if key == "source_geometry_id" else region["installation_binding"]
        target[key] = value
    with pytest.raises(RegionContextError):
        context_matches_bundle(context, bundle)


@pytest.mark.parametrize("fault", ["none", "binding", "calibration", "region"])
@pytest.mark.parametrize("at", ["state", "frame"])
def test_real_device_gate_rechecks_context_after_await(tmp_path, monkeypatch, fault, at):
    from drivers import real_stubs
    from runtime import calibration_gate
    from runtime.devices import DeviceManager

    monkeypatch.setattr(real_stubs, "require_confirmed_hardware", lambda: None)
    monkeypatch.setattr(calibration_gate, "EDGE_CONSENSUS_INTERVAL_S", 0)
    manager = DeviceManager(None, None, None, hardware_mode="real", config_root=tmp_path)
    config = {
        "device_id": "arm-01", "driver_mode": "daemon", "camera_id": "cam-01",
        "camera_device_unique_id": "uvc-01", "binding_revision": 7,
        "camera_binding_id": "cam-bind-9", "source_geometry_id": "geo-s2",
        "daemon_addr": "127.0.0.1:8795", "daemon_session": "session-01",
        "data_root": tmp_path, "expected_frame_width": FW, "expected_frame_height": FH,
    }
    manager._devices["arm-01"] = dict(config)
    bundle = _v3_bundle()
    cb.save_bundle(bundle, tmp_path)
    context = {
        "mode": "region", "device_id": "arm-01",
        "pointer": {"revision": 1, "selected_version": 1, "selected_sha256": "a" * 64},
        "region": _region(BOX_LEFT),
    }
    manager.set_region_context_provider(lambda _: copy.deepcopy(context))
    calls = []

    def change():
        if calls:
            return
        calls.append(True)
        if fault == "binding":
            manager._devices["arm-01"]["binding_revision"] += 1
        elif fault == "region":
            context["pointer"]["revision"] += 1
        elif fault == "calibration":
            changed = copy.deepcopy(bundle)
            changed["camera"]["cameraToScreen"]["homography"][0][2] += 9
            cb.save_bundle(changed, tmp_path)

    class Camera(RegionCamera):
        async def capture(self):
            frame = await super().capture()
            if at == "frame":
                change()
            return frame

    image, _ = _frame()
    camera = Camera([image])
    monkeypatch.setattr(manager, "_make_real_camera", lambda _: camera)
    arm, _, _, _, _ = manager._create_drivers(config)

    async def read_state():
        if at == "state":
            change()
        return {"session": "session-01"}

    arm.read_state = AsyncMock(side_effect=read_state)
    result = asyncio.run(arm.live_calibration_gate())
    checks = result.get("checks", []) if isinstance(result, dict) else result
    assert calls
    if fault == "none":
        assert checks == [], checks
        assert result["pose"] is not None
    else:
        assert checks, "等待期间改变上下文，正式装配的现场门仍允许动作"
        assert not isinstance(result, dict) or result.get("pose") is None
