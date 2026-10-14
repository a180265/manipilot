"""Session-scoped metadata survives page changes without rebinding old geometry."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from adapters.screen_ime.gesture_capture import GestureCaptureCoordinator
from adapters.screen_ime.service import ScreenImeApiError
from drivers import calibration_bundle as cb
from drivers import calibration_wizard as cw
from drivers import gesture_program as gp
from tests.test_gesture_coordinate_frame import (
    LEGACY_SESSION, _FakeIme, _hello, _legacy_metadata, _make_wizard, _page,
)
from tests.test_screen_gesture_driver import gesture_bundle


SECOND_SESSION = "cal-00000000-0000-4000-8000-000000000002"


def test_legacy_metadata_is_retained_per_calibration_session(tmp_path):
    coordinator = GestureCaptureCoordinator(data_root=tmp_path)
    first = _hello(_page("legacy-calibration", legacy=LEGACY_SESSION))
    asyncio.run(coordinator.register_client("ime-01", first))
    second = _hello(_page("legacy-calibration", legacy=SECOND_SESSION))
    second["heartbeatSeq"] = 2
    asyncio.run(coordinator.register_client("ime-01", second))
    found = coordinator.legacy_metadata("ime-01", LEGACY_SESSION)
    assert found is not None
    assert found["page"] == first["page"]


def test_legacy_metadata_survives_coordinator_restart(tmp_path):
    coordinator = GestureCaptureCoordinator(data_root=tmp_path)
    first = _hello(_page("legacy-calibration", legacy=LEGACY_SESSION))
    asyncio.run(coordinator.register_client("ime-01", first))

    restarted = GestureCaptureCoordinator(data_root=tmp_path)
    found = restarted.legacy_metadata("ime-01", LEGACY_SESSION)

    assert found is not None
    assert found["page"] == first["page"]
    assert restarted.legacy_metadata("ime-02", LEGACY_SESSION) is None
    assert restarted.legacy_metadata("ime-01", SECOND_SESSION) is None


def test_out_of_order_heartbeat_cannot_replace_calibration_metadata(tmp_path):
    coordinator = GestureCaptureCoordinator(data_root=tmp_path)
    first = _hello(_page("legacy-calibration", legacy=LEGACY_SESSION))
    first["heartbeatSeq"] = 5
    asyncio.run(coordinator.register_client("ime-01", first))
    stale = deepcopy(first)
    stale["heartbeatSeq"] = 4
    stale["page"]["rootOriginDisplayPx"] = [0, 0]
    asyncio.run(coordinator.register_client("ime-01", stale))
    assert coordinator.legacy_metadata("ime-01", LEGACY_SESSION)["page"] == first["page"]


def test_metadata_lookup_is_a_deep_snapshot(tmp_path):
    coordinator = GestureCaptureCoordinator(data_root=tmp_path)
    first = _hello(_page("legacy-calibration", legacy=LEGACY_SESSION))
    original = deepcopy(first["page"])
    asyncio.run(coordinator.register_client("ime-01", first))
    found = coordinator.legacy_metadata("ime-01", LEGACY_SESSION)
    found["page"]["rootOriginDisplayPx"][1] = 999
    assert coordinator.legacy_metadata("ime-01", LEGACY_SESSION)["page"] == original
    first["page"]["rootWidthPx"] = 999
    assert coordinator.legacy_metadata("ime-01", LEGACY_SESSION)["page"] == original


@pytest.mark.parametrize("wrong", ["device", "page"])
def test_metadata_cannot_be_registered_for_another_device_or_plain_page(tmp_path, wrong):
    coordinator = GestureCaptureCoordinator(data_root=tmp_path)
    hello = _hello(_page("legacy-calibration", legacy=LEGACY_SESSION))
    if wrong == "device":
        hello["deviceId"] = "ime-other"
    else:
        hello["page"]["pageId"] = "gesture-capture"
    with pytest.raises(ScreenImeApiError):
        asyncio.run(coordinator.register_client("ime-01", hello))
    assert coordinator.legacy_metadata("ime-01", LEGACY_SESSION) is None


@pytest.mark.parametrize("wrong", ["device", "session", "page_session", "page", "dimensions", "rotation"])
def test_wizard_rejects_unrelated_metadata_without_overwriting_old_sidecar(tmp_path, wrong):
    bundle = gesture_bundle()
    cb.save_bundle(bundle, tmp_path)
    valid = _legacy_metadata()
    wizard = _make_wizard(tmp_path, _FakeIme(metadata=valid))
    wizard._save_gesture_coordinate_frame(bundle)
    path = gp.coordinate_frame_path("arm-01", tmp_path)
    before = path.read_bytes()
    metadata = deepcopy(valid)
    if wrong == "device":
        metadata["deviceId"] = "ime-other"
    elif wrong == "session":
        metadata["sessionId"] = SECOND_SESSION
    elif wrong == "page_session":
        metadata["page"]["legacyCalibrationSessionId"] = SECOND_SESSION
    elif wrong == "page":
        metadata["page"]["pageId"] = "gesture-capture"
    elif wrong == "dimensions":
        metadata["page"]["rootHeightPx"] = 2400
    else:
        metadata["page"]["rotation"] = 1
    wizard.ime = _FakeIme(metadata=metadata)
    run = {"phase": "PROMOTE", "events": []}
    wizard._auto_runner._auto["run"] = run

    wizard._save_gesture_coordinate_frame(bundle)

    assert path.read_bytes() == before
    assert run["events"][-1]["event"] == "gesture_frame_save_failed"


def test_missing_metadata_records_reason_instead_of_silently_keeping_old_frame(tmp_path):
    bundle = gesture_bundle()
    cb.save_bundle(bundle, tmp_path)
    wizard = _make_wizard(tmp_path, _FakeIme(metadata=None))
    run = {"phase": "PROMOTE", "events": []}
    wizard._auto_runner._auto["run"] = run

    wizard._save_gesture_coordinate_frame(bundle)

    assert gp.load_coordinate_frame("arm-01", tmp_path) is None
    assert run["events"][-1]["event"] == "gesture_frame_save_failed"
    assert "metadata" in run["events"][-1]["detail"]


def test_router_uses_service_database_directory_for_durable_metadata(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from adapters.screen_ime.gesture_routes import create_gesture_router
    from adapters.screen_ime.routes import screen_ime_error_handler
    from adapters.screen_ime.service import ScreenImeService

    page = _page("legacy-calibration", legacy=LEGACY_SESSION)
    for restart in (False, True):
        service = ScreenImeService(tmp_path / "ime.db", tmp_path / "mapping.json")
        app = FastAPI()
        app.include_router(create_gesture_router(service))
        app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)
        with TestClient(app) as client:
            if not restart:
                assert client.post("/api/v1/devices/ime-01/gesture-client",
                                   json=_hello(page)).status_code == 200
            found = client.get("/api/v1/gesture-protocol/legacy-metadata",
                               params={"deviceId": "ime-01", "sessionId": LEGACY_SESSION})
            assert found.status_code == 200
            assert found.json()["page"] == page
        service.tasks.close()
    assert len(list((tmp_path / "gesture_legacy_metadata").glob("*.json"))) == 1


def test_pure_builder_does_not_change_bundle_or_metadata():
    bundle, metadata = gesture_bundle(), _legacy_metadata()
    before_bundle, before_metadata = deepcopy(bundle), deepcopy(metadata)
    frame = cw.build_gesture_coordinate_frame(
        bundle, metadata, ime_device_id="ime-01", session_id=LEGACY_SESSION)
    gp.validate_coordinate_frame(frame, bundle)
    assert bundle == before_bundle
    assert metadata == before_metadata
