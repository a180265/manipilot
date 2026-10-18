"""固定支架运行模式：沿用验收映射，页面与效果交给调用方判断。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from drivers.arm import DriverError
from drivers.daemon_arm import DaemonArmDriver
from drivers.real_camera import RealCameraDriver
from runtime import calibration_gate, layout_geometry
from runtime.layout_state import content_sha
from tests.test_live_calibration_gate import FakeCamera, ready_bundle


class BoundCamera(FakeCamera):
    def __init__(self, **overrides):
        super().__init__(brightness=0)
        self.overrides = overrides
        self.calls = 0

    async def capture(self):
        self.calls += 1
        frame = await super().capture()
        now = datetime.now(timezone.utc).isoformat()
        return {**frame, "source_frame_seq": self.calls, "source_captured_at": now,
                "source_received_at": now, "source_legacy_mode": False,
                **self.overrides}


def gate(camera, **overrides):
    options = dict(device_id="arm-01", camera_id="cam-01",
                   daemon_addr="127.0.0.1:8795", daemon_session="session-01",
                   estop_locked=False, bundle=ready_bundle(), camera=camera,
                   require_pose=True)
    return asyncio.run(calibration_gate.check_live_calibration(**(options | overrides)))


def forbidden(*_args, **_kwargs):
    raise AssertionError("固定校准模式不应重测屏幕或重识别目标")


def test_calibrated_gate_uses_one_bound_frame_without_live_geometry(monkeypatch):
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    monkeypatch.setattr(calibration_gate, "_quad_of", forbidden)
    monkeypatch.setattr(calibration_gate, "_frame_checks", forbidden)
    camera = BoundCamera(_screen_pose={"corners": "旧姿态"})
    checks, frame, deviation = gate(camera, search_region={"not": "used"})
    assert checks == []
    assert camera.calls == 1
    assert "_screen_pose" not in frame
    assert frame["geometry_source"] == "saved_calibration"
    assert deviation is None


@pytest.mark.parametrize("frame_override,gate_override", [
    ({"device_unique_id": "other-camera"}, {}),
    ({"frozen": True}, {}),
    ({"source_captured_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}, {}),
    ({}, {"estop_locked": True}),
    ({}, {"motion_result_unknown": True}),
    ({}, {"daemon_session": "other-session"}),
])
def test_calibrated_gate_retains_binding_frame_and_machine_checks(
        monkeypatch, frame_override, gate_override):
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    assert gate(BoundCamera(**frame_override), **gate_override)[0]


def test_default_gate_still_checks_live_geometry(monkeypatch):
    monkeypatch.delenv("ARMFLEET_RUNTIME_VISION_MODE", raising=False)
    check = {"code": "camera_unavailable", "message": "现场画面检查"}
    monkeypatch.setattr(calibration_gate, "_frame_checks", lambda *_a, **_k: ([check], None))
    assert check in gate(BoundCamera())[0]


def test_calibrated_camera_marks_saved_geometry_without_contour_detection(monkeypatch):
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    monkeypatch.setattr(calibration_gate, "_quad_of", forbidden)
    monkeypatch.setattr(layout_geometry, "locate_in_region", forbidden)
    camera = object.__new__(RealCameraDriver)
    camera.device_id, camera.screen_device_id = "cam-01", "arm-01"
    frame = asyncio.run(BoundCamera().capture())
    aligned, pose = camera._aligned_screen_frame(
        frame, ready_bundle(), search_region={"not": "used"}, return_pose=True)
    assert aligned.shape == (48, 64, 3)
    assert pose["geometry_source"] == "saved_calibration"
    assert pose["corners"] == ready_bundle()["screen"]["edgeModel"]["corners"]
    np.testing.assert_array_equal(pose["matrix"], np.eye(3))
    with pytest.raises(ValueError, match="冻结"):
        camera._aligned_screen_frame(frame | {"frozen": True}, ready_bundle())
    with pytest.raises(ValueError, match="尺寸"):
        camera._aligned_screen_frame(frame | {"width": 65}, ready_bundle())


def driver_context(monkeypatch):
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    driver = object.__new__(DaemonArmDriver)
    driver.device_id, driver.data_root = "arm-01", None
    bundle = ready_bundle()
    monkeypatch.setattr("drivers.target_archive.load_runtime_calibration", lambda *_a: bundle)
    driver._layout_provider = forbidden
    driver._text_target_verifier = forbidden
    return driver, bundle


def test_calibrated_fixed_target_skips_reobservation_but_retains_context(monkeypatch):
    driver, bundle = driver_context(monkeypatch)
    layout = SimpleNamespace(ensure_current=Mock(), verify_target_point=forbidden)
    contact, command = driver._layout_motion_guards(
        layout, "back_button", {"targetMode": "tap"}, bundle, None)
    asyncio.run(contact())
    command({"x": 1, "y": 2})
    layout.ensure_current.assert_called_with(bundle)
    layout.ensure_current.side_effect = ValueError("设备绑定已变化")
    with pytest.raises(DriverError, match="设备绑定已变化"):
        command()


def test_calibrated_dynamic_target_skips_repeated_ocr_but_pins_original_ref(monkeypatch):
    driver, bundle = driver_context(monkeypatch)
    ref = {"source": "camera_ocr", "cameraX": 10}
    snapshot = {"frame": None, "execution_binding": None,
                "calibration_sha": content_sha(bundle), "device_binding": None,
                "target_ref": ref, "ref": dict(ref), "text_check": forbidden}
    contact, command = driver._layout_motion_guards(
        None, "conversation_row", {}, bundle, None, dynamic_snapshot=snapshot)
    asyncio.run(contact())
    command()
    ref["cameraX"] = 20
    with pytest.raises(DriverError, match="动作期间被改写"):
        command()
    assert driver._contact_pose_guard({"corners": "unused"}) is None


@pytest.mark.parametrize("mode,expected", [("calibrated", "wechat_list"), ("live", None)])
def test_camera_only_forwards_explicit_page_hint_in_calibrated_mode(monkeypatch, mode, expected):
    from drivers import screen_proof

    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", mode)
    camera = object.__new__(RealCameraDriver)
    camera.device_id, camera.screen_device_id = "cam-01", "arm-01"
    camera.data_root, camera.ocr_engine = None, None
    prepared = Mock(return_value="prepared")
    selected = SimpleNamespace(region={}, prepare=prepared)
    camera._layout_provider = lambda _device: selected
    camera._aligned_screen_frame = lambda *_a, **_k: {"matrix": np.eye(3).tolist()}
    monkeypatch.setattr(screen_proof, "binding", lambda *_a, **_k: ("fingerprint", ready_bundle()))
    frame = asyncio.run(BoundCamera().capture())
    assert asyncio.run(camera._selected_layout_frame(frame, observed_state="wechat_list")) == "prepared"
    assert prepared.call_args.kwargs.get("observed_state") == expected


def test_calibrated_text_admission_keeps_original_proof_without_second_ocr(monkeypatch):
    from drivers.real_camera import _TextTargetAction

    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    camera = object.__new__(RealCameraDriver)
    camera._text_location_record = Mock(return_value={"layout": None})
    camera._validate_text_record = Mock()
    camera._observe_text_record = forbidden
    ref = {"locationId": "original-ocr-proof"}
    check = asyncio.run(camera.verify_text_target(ref))
    check()
    camera._text_location_record.assert_called_with(ref)
    action = _TextTargetAction(camera, ref, {"layout": None})
    check = asyncio.run(action.verify(None))
    check()
    action.close()
    with pytest.raises(ValueError, match="文字动作已结束"):
        check()
