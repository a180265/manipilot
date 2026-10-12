"""Debug gestures pass the same fresh phone pose to the production driver."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import motor_debug_server as debug  # noqa: E402


@pytest.mark.parametrize("entrypoint", ["basic", "profile"])
@pytest.mark.parametrize("blocked", [False, True])
def test_debug_motion_gate_requires_and_returns_phone_pose(
        tmp_path, monkeypatch, entrypoint, blocked):
    pose = {"matrix": [[1, 0, 5], [0, 1, 3], [0, 0, 1]],
            "corners": [[35, 33], [275, 33], [275, 273], [35, 273]],
            "movementPx": 5.83, "fitErrorPx": 0}
    checks = ([{"code": "screen_pose_unavailable", "message": "phone not located"}]
              if blocked else [])
    context = {"camera_id": "cam-test", "frame_width": 300, "frame_height": 300}
    gate_calls, gate_results = [], []

    class Camera:
        async def capture(self):
            raise AssertionError("Gate wiring test must not capture hardware")

        async def close(self):
            pass

    class Arm:
        def __init__(self, device, *, daemon_addr, **kwargs):
            self.daemon_addr = daemon_addr

        async def read_state(self):
            return {"session": "session-test"}

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def swipe(self, gesture):
            gate_results.append(await self.live_calibration_gate())
            gate_results.append(await self.live_calibration_gate(screen_edge_advisory=True))
            raise ValueError("gate wiring inspection complete")

    async def check_live_calibration(**kwargs):
        gate_calls.append(kwargs)
        return checks, {"_screen_pose": pose} if not blocked else {}, None

    monkeypatch.setattr(debug, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "DaemonArmDriver", Arm)
    monkeypatch.setattr(debug, "require_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(debug, "_gesture_camera", lambda *args: Camera())
    monkeypatch.setattr(debug, "_basic_record_path", lambda *args: tmp_path / "basic.json")
    monkeypatch.setattr(debug, "_basic_binding", lambda *args: ({"test": True}, {}))
    monkeypatch.setattr(debug, "device_config", lambda *args: {})
    monkeypatch.setattr(debug, "_daemon_address", lambda *args: ("127.0.0.1", 1))
    monkeypatch.setattr(debug.calibration_bundle, "load_bundle", lambda *args: {})
    monkeypatch.setattr(debug.gesture_profiles, "check_binding", lambda *args: None)
    monkeypatch.setattr(debug, "check_live_calibration", check_live_calibration)
    gesture = {"start": {}, "end": {}}
    if entrypoint == "basic":
        status, evidence = asyncio.run(debug._run_basic_gesture(
            "arm-test", {"contextId": "context-test", "requestId": "request-test",
                         "action": "swipe", "frameSeq": 1, "binding": {"test": True}},
            gesture))
    else:
        status, evidence = asyncio.run(debug._run_gesture_test(
            "arm-test", "context-test",
            {"pending_test": {"direction": "search", "id": "test"},
             "config": {"settle_ms": 0, "search_gesture": gesture,
                        "swipe_duration_ms": 600},
             "profile_id": "profile", "version": 1}))
    assert status == "failed"
    assert evidence["reason"] == "gate wiring inspection complete"
    assert [call["require_pose"] for call in gate_calls] == [True, False]
    assert [call["screen_edge_advisory"] for call in gate_calls] == [False, True]
    assert gate_results == [{"checks": checks, "pose": None if blocked else pose}] * 2
