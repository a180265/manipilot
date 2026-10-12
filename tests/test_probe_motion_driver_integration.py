"""Real probe tracking and driver protocol, with synthetic frames and fake TCP only."""
import asyncio
from collections import deque
import hashlib
import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import urllib.request

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb, real_stubs, screen_gestures as sg
from drivers.arm import DriverError
from drivers.daemon_arm import DaemonArmDriver
from drivers.daemon_protocol import DOUBLE_TAP_SHORT_LIFT_FEATURE
from drivers.probe_motion import PROFILE_NAME, ProbeMotionVerifier, register_probe
from tests.test_daemon_arm import CALIB_SESSION, FakeDaemon
from tests.test_ui_targets import ready_bundle


class MovingProbeCamera:
    """Deliver intermediate frames while FakeDaemon withholds each motion reply."""

    def __init__(self):
        self.patch = np.random.default_rng(42).integers(40, 230, (40, 40, 3), dtype=np.uint8)
        self.position = (100, 100)
        self.seq = 0
        self.calls = 0
        self.active_captures = 0
        self.cancelled_captures = 0
        self.frames = []
        self.background_only = None
        self.block_command = None
        self.capture_blocked = asyncio.Event()
        self.reply_ready = threading.Event()
        self.lock = threading.Lock()
        self.command = None
        self.positions = deque()
        self.timed_out = False

    def frame(self):
        self.seq += 1
        image = np.full((300, 420, 3), 20, dtype=np.uint8)
        image[40:200, 40:280] = 60 if self.seq % 2 else 180
        x, y = (round(value) - 20 for value in self.position)
        image[y:y + 40, x:x + 40] = self.patch
        raw = cv2.imencode(".png", image)[1].tobytes()
        return {
            "image_bytes": raw, "sha256": hashlib.sha256(raw).hexdigest(),
            "captured_at": "2026-09-14T12:00:00Z", "source_frame_seq": self.seq,
            "device_id": "arm-01", "camera_id": "cam-01", "device_unique_id": "uvc-01",
            "binding_revision": 7, "width": 420, "height": 300, "frozen": False,
        }

    async def capture(self):
        self.calls += 1
        self.active_captures += 1
        try:
            with self.lock:
                command = self.command
            if command and command.split()[0] == self.block_command:
                self.capture_blocked.set()
                await asyncio.Future()
            with self.lock:
                if self.positions:
                    self.position = self.positions.popleft()
                frame = self.frame()
                self.frames.append({
                    "seq": frame["source_frame_seq"], "sha256": frame["sha256"],
                    "position": self.position, "command": command,
                })
                if command and not self.positions:
                    self.reply_ready.set()
            return frame
        except asyncio.CancelledError:
            self.cancelled_captures += 1
            raise
        finally:
            self.active_captures -= 1

    def motion_reply(self, line, reply, connection_id):
        parts = line.split()
        kind = parts[0]
        if kind not in {"XY", "TAP2", "SWIPE"}:
            return reply
        with self.lock:
            x, y = self.position
            if kind == "TAP2":
                positions = [(x, y + offset) for offset in (6, 5, 6, 0)]
            else:
                # The synthetic calibration is ten camera pixels per robot mm.
                dx, dy = float(parts[1]) * 10, float(parts[2]) * 10
                positions = [(x + dx * t, y + dy * t) for t in (.25, .5, .75, 1)]
            if kind == self.background_only:
                positions = [self.position] * 4
            self.positions = deque(positions)
            self.command = line
            self.reply_ready.clear()
        if not self.reply_ready.wait(3):
            self.timed_out = True
            return None
        with self.lock:
            self.command = None
        return reply


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    daemon = FakeDaemon(mpos=(6, 6, 2)).start()
    address = ("127.0.0.1", daemon.port)
    connect = socket.create_connection

    def isolated_connect(target, *args, **kwargs):
        assert target == address, f"Non-fixture TCP connection: {target}"
        return connect(target, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Probe integration tests must not access live services or cameras")

    monkeypatch.setattr(socket, "create_connection", isolated_connect)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(cv2, "VideoCapture", forbidden)
    monkeypatch.setattr(real_stubs, "require_confirmed_hardware", lambda: None)
    driver = None
    camera = MovingProbeCamera()
    try:
        daemon.motion_features.append(DOUBLE_TAP_SHORT_LIFT_FEATURE)
        bundle = ready_bundle(session=CALIB_SESSION)
        bundle["daemonAddr"] = f"127.0.0.1:{daemon.port}"
        bundle["frame"] = {"width": 420, "height": 300}
        bundle["screen"].update(orientation="landscape", screenWidth=240, screenHeight=160)
        bundle["screen"]["edgeModel"]["corners"] = [[40, 40], [280, 40], [280, 200], [40, 200]]
        bundle["xy"]["robotToScreen"] = {"a": 10, "b": 0, "c": 0, "d": 0, "e": 10, "f": 0}
        bundle["camera"]["cameraToScreen"]["homography"] = [
            [1, 0, -40], [0, 1, -40], [0, 0, 1]]
        bundle["bindingFingerprint"] = cb.binding_fingerprint(
            device_id="arm-01", camera_id="cam-01", camera_device_unique_id="uvc-01",
            daemon_addr=bundle["daemonAddr"], frame_width=420, frame_height=300)
        cb.save_bundle(bundle, tmp_path)
        baseline = cb.bundle_path("arm-01", tmp_path).read_bytes()
        register_probe(camera.frame(), {"x": 80, "y": 80, "width": 40, "height": 40},
                       device_id="arm-01", data_root=tmp_path, operator="simulation")
        verifier = ProbeMotionVerifier(
            camera, device_id="arm-01", data_root=tmp_path, evidence_dir=tmp_path / "evidence")
        driver = DaemonArmDriver(
            "arm-01", daemon_addr=bundle["daemonAddr"], data_root=tmp_path,
            task_id="probe-integration")
        driver.motion_verifier = verifier
        driver.live_calibration_gate = AsyncMock(return_value=[])
        daemon.response_hook = camera.motion_reply
        asyncio.run(driver.connect())
        yield SimpleNamespace(
            daemon=daemon, driver=driver, camera=camera, verifier=verifier,
            bundle=bundle, root=tmp_path)
        assert cb.bundle_path("arm-01", tmp_path).read_bytes() == baseline
    finally:
        camera.reply_ready.set()
        if driver is not None:
            asyncio.run(driver.disconnect())
        daemon.stop()
    assert not daemon.lease and not daemon.transactions
    assert not camera.timed_out
    assert not any(command.startswith(("CONNECT", "DISCONNECT")) for _, command in daemon.requests)


def gesture(rig, action):
    point = {"source": "screen_normalized", "x": .5, "y": .5}
    points = ({"point": point} if action == "double_tap" else
              {"start": point, "end": {**point, "y": .75}})
    return {"binding": sg.binding_snapshot(rig.bundle), **points}


async def execute(rig, action):
    result = await asyncio.wait_for(getattr(rig.driver, action)(gesture(rig, action)), 5)
    assert rig.verifier._sampler is None
    assert rig.camera.active_captures == 0
    count = rig.camera.calls
    await asyncio.sleep(.08)
    assert rig.camera.calls == count, "Sampling must stop before the event loop shuts down"
    assert not rig.driver._motion_lock.locked()
    return result


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_continuous_registered_probe_confirms_real_driver_phases(rig, action):
    result = asyncio.run(execute(rig, action))
    assert result["motion_verified"] is True
    assert result["outcome_hint"] == "success"
    assert result["effect_status"] == "unverified"
    macro = "TAP2 " if action == "double_tap" else "SWIPE "
    assert len(rig.daemon.commands) == 2
    assert rig.daemon.commands[0].split()[:3] == ["XY", "6.000", "2.000"]
    assert rig.daemon.commands[-1].startswith(macro)
    assert rig.daemon.mpos == ([12, 8, 2] if action == "double_tap" else [12, 12, 2])
    assert len(result["phases"]) == len(rig.verifier.history) == 2
    for phase in result["phases"]:
        proof = phase["verification"]
        trace = proof["trace"]
        assert proof["verified"] is True
        assert proof["measurement_kind"] == "registered_probe_trace"
        assert proof["sample_count"] == len(trace) >= 4
        assert all(a["seq"] < b["seq"] for a, b in zip(trace, trace[1:]))
        during = {frame["seq"] for frame in rig.camera.frames if frame["command"] == phase["cmd"]}
        assert len(during.intersection(point["seq"] for point in trace)) >= 3
        persisted = json.loads((rig.root / "evidence" / f"{phase['phase']}-trace.json").read_text())
        assert persisted == proof
    assert result["phases"][0]["verification"]["displacement_px"] == [60, 20]
    final = result["phases"][-1]["verification"]
    if action == "double_tap":
        assert result["tap_count"] == 2
        assert final["displacement_px"] == [0, 0]
        assert final["max_excursion_px"] == 6
    else:
        assert result["swipe_executed"] is True
        assert final["displacement_px"] == [0, 40]


@pytest.mark.parametrize("action,stationary_phase", [
    ("double_tap", "XY"), ("double_tap", "TAP2"), ("swipe", "SWIPE"),
])
def test_phone_background_changes_cannot_confirm_stationary_probe(rig, action, stationary_phase):
    rig.camera.background_only = stationary_phase
    result = asyncio.run(execute(rig, action))
    assert result["motion_verified"] is False
    assert result["outcome_hint"] == "unknown"
    assert result["effect_status"] == "unverified"
    proof = result["phases"][-1]["verification"]
    assert proof["verified"] is False and proof["reason"]
    assert proof["sample_count"] >= 4
    assert proof["max_excursion_px"] == 0
    assert len({point["sha256"] for point in proof["trace"]}) >= 2
    assert result["phases"][-1]["cmd"].split()[0] == stationary_phase
    assert len(rig.daemon.commands) == (1 if stationary_phase == "XY" else 2)
    if stationary_phase == "XY":
        assert result["tap_count"] == 0
        assert not any(command.startswith("TAP") for command in rig.daemon.commands)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_missing_registered_template_rejects_before_claim_or_motion(rig, action):
    (rig.root / "devices" / "arm-01" / PROFILE_NAME).unlink()
    with pytest.raises(DriverError) as error:
        asyncio.run(execute(rig, action))
    assert error.value.category == "driver_policy_rejected"
    assert not rig.daemon.commands
    assert not any(command.startswith(("CLAIM ", "BEGIN_MOTION"))
                   for _, command in rig.daemon.requests)
    assert rig.camera.calls == 0
    assert rig.verifier._sampler is None


@pytest.mark.parametrize("blocked_phase", ["XY", "TAP2"])
def test_cancelling_driver_stops_inflight_sampling_without_followup_motion(rig, blocked_phase):
    async def cancel_during_capture():
        rig.camera.block_command = blocked_phase
        operation = asyncio.create_task(rig.driver.double_tap(gesture(rig, "double_tap")))
        try:
            await asyncio.wait_for(rig.camera.capture_blocked.wait(), 3)
            sampler = rig.verifier._sampler
            assert sampler is not None and not sampler.done()
            assert rig.camera.active_captures == 1
            assert rig.daemon.commands[-1].split()[0] == blocked_phase
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 1)
            assert sampler.done() and rig.verifier._sampler is None
            assert rig.camera.cancelled_captures == 1
            assert rig.camera.active_captures == 0
            count = rig.camera.calls
            commands = list(rig.daemon.commands)
            await asyncio.sleep(.08)
            assert rig.camera.calls == count
            assert rig.daemon.commands == commands
            assert not rig.driver.connected and not rig.driver._motion_lock.locked()
        finally:
            rig.camera.reply_ready.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    asyncio.run(cancel_during_capture())
    assert len(rig.daemon.commands) == (1 if blocked_phase == "XY" else 2)
