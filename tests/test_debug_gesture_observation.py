"""Supervised debug execution does not depend on a probe appearance template."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from drivers import calibration_bundle as cb
from drivers.probe_motion import PROFILE_NAME, ProbeMotionVerifier
from scripts import motor_debug_server as debug
from tests.test_motor_debug_basic_gestures import env, request
from tests.test_probe_motion_driver_integration import rig, gesture, execute


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("appearance", ["missing", "obsolete", "dark"])
def test_debug_runs_complete_gesture_without_claiming_visual_success(rig, action, appearance):
    rig.verifier.observation_only = True
    if appearance == "missing":
        (rig.root / "devices" / "arm-01" / PROFILE_NAME).unlink()
    else:
        rig.camera.patch[:] = 0 if appearance == "dark" else 255
    result = asyncio.run(execute(rig, action))
    assert result["controller_completed"] is True
    assert result["motion_verified"] is False
    assert result["outcome_hint"] == "unknown"
    assert result["tap_count"] == 2 if action == "double_tap" else result["swipe_executed"]
    assert len(rig.daemon.commands) == 2
    assert rig.daemon.commands[-1].startswith("TAP2 " if action == "double_tap" else "SWIPE ")
    assert len(rig.verifier.history) == 2
    assert all(not item["verified"] for item in rig.verifier.history)


def test_debug_camera_binding_mismatch_still_prevents_dispatch(rig):
    rig.verifier.observation_only = True
    capture = rig.camera.capture

    async def wrong_camera():
        return {**await capture(), "device_unique_id": "different-camera"}

    rig.camera.capture = wrong_camera
    with pytest.raises(ValueError, match="绑定"):
        asyncio.run(rig.driver.double_tap(gesture(rig, "double_tap")))
    assert not rig.daemon.commands


@pytest.mark.parametrize("fault", [{"image_bytes": b"not-an-image"}, {"frozen": True}])
def test_debug_invalid_camera_frame_is_not_a_template_warning(rig, fault):
    rig.verifier.observation_only = True
    (rig.root / "devices" / "arm-01" / PROFILE_NAME).unlink()
    capture = rig.camera.capture

    async def invalid_frame():
        return {**await capture(), **fault}

    rig.camera.capture = invalid_frame
    with pytest.raises(ValueError):
        asyncio.run(rig.driver.swipe(gesture(rig, "swipe")))
    assert not rig.daemon.commands


def test_debug_before_failure_preserves_image_and_phase(rig, monkeypatch):
    rig.verifier.observation_only = True
    bundle = cb.load_bundle("arm-01", rig.root)
    context = {
        "camera_id": bundle["cameraId"], "camera_device_unique_id": bundle["cameraDeviceUniqueId"],
        "binding_revision": bundle["bindingRevision"], "frame_width": 420, "frame_height": 300,
    }
    binding, _ = ({}, bundle)
    monkeypatch.setattr(debug, "DATA_DIR", rig.root)
    monkeypatch.setattr(debug, "require_context", lambda *a, **k: context)
    monkeypatch.setattr(debug, "_gesture_camera", lambda *a: rig.camera)
    monkeypatch.setattr(debug, "device_config", lambda *_: {"daemon_addr": rig.bundle["daemonAddr"]})
    monkeypatch.setattr(debug, "_basic_binding", lambda *a: (binding, bundle))
    monkeypatch.setattr(debug, "check_live_calibration", AsyncMock(return_value=([], {}, {})))
    rig.camera.close = AsyncMock()
    monkeypatch.setattr(debug, "DaemonArmDriver", lambda *a, **k: rig.driver)

    class Failure(ProbeMotionVerifier):
        async def before(self, label):
            self._last_frame = await self._camera.capture()
            raise ValueError("绑定变化")

    monkeypatch.setattr(debug, "CameraMotionVerifier", Failure)
    body = {"contextId": "ctx", "requestId": "before-failure", "binding": binding,
            "frameSeq": 1, "action": "double_tap"}
    outcome, evidence = asyncio.run(debug._run_basic_gesture(
        "arm-01", body, gesture(rig, "double_tap")))
    assert outcome == "failed"
    assert evidence["blockedPhase"] == "travel_xy"
    assert evidence["before"]["sha256"]
    assert not rig.daemon.commands


def test_executed_debug_result_does_not_demand_another_manual_release(env, monkeypatch):
    run = AsyncMock(return_value=("executed", {
        "motionConfirmed": False, "controllerCompleted": True,
        "observationNote": "探头照片不匹配", "effectStatus": "manual_confirmation_required",
    }))
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    result = debug.test_basic_gesture("arm-01", request())
    assert result["outcome"] == "executed"
    assert "控制器" in result["message"]
    assert not debug.basic_gesture_state("arm-01", "ctx")["blocked"]
    debug.test_basic_gesture("arm-01", request())
    assert run.await_count == 2


def test_debug_no_longer_requires_probe_registration_to_plan(env):
    daemon, _, _ = env
    assert not any("探头" in reason or "实体运动" in reason
                   for reason in debug._calculated_gesture_blockers("arm-01", daemon.status()))
