"""Independent wake safety probes; fake daemon and temporary calibration only."""
import asyncio
import socket
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb, screen_gestures as sg
from drivers.arm import DriverError
from drivers.wake_screen import resolve_wake_target, wake_frame_quality
from runtime import calibration_gate
from tests.test_daemon_arm import FakeDaemon
from tests.test_live_calibration_gate import ready_bundle, run_gate
from tests.test_screen_gesture_driver import gesture_bundle, rig
from tests.test_screen_wake_driver import prepare_wake, validated_bundle, wake_frame


@pytest.fixture(autouse=True)
def only_this_test_fake_daemon(monkeypatch):
    allowed_ports = set()
    original_start = FakeDaemon.start
    original_connect = socket.socket.connect

    def start(daemon):
        allowed_ports.add(daemon.port)
        return original_start(daemon)

    def connect(connection, address):
        assert (
            isinstance(address, tuple)
            and address[0] == "127.0.0.1"
            and address[1] in allowed_ports
        ), f"Review tests must not contact external or live endpoints: {address!r}"
        return original_connect(connection, address)

    monkeypatch.setattr(FakeDaemon, "start", start)
    monkeypatch.setattr(socket.socket, "connect", connect)


class ChangingCamera:
    def __init__(self, bundle, *, change, at, detectable_dark=False):
        self.bundle = bundle
        self.change = change
        self.at = at
        self.detectable_dark = detectable_dark
        self.calls = 0

    async def capture(self):
        self.calls += 1
        changed = self.calls >= self.at
        frame = wake_frame(
            self.bundle, lit=changed and self.change == "lit", sequence=self.calls)
        if self.detectable_dark and not (changed and self.change == "lit"):
            image = np.zeros((frame["height"], frame["width"], 3), np.uint8)
            corners = np.asarray(
                self.bundle["screen"]["edgeModel"]["corners"], dtype=np.int32)
            cv2.fillConvexPoly(image, corners, (12, 12, 12))
            ok, encoded = cv2.imencode(".png", image)
            assert ok
            frame["image_bytes"] = encoded.tobytes()
            assert wake_frame_quality(frame) == "waiting"
        if changed:
            if self.change == "binding_revision":
                frame["binding_revision"] += 1
            elif self.change == "device_unique_id":
                frame["device_unique_id"] = "replacement-camera"
            elif self.change == "camera_id":
                frame["camera_id"] = "cam-02"
            elif self.change == "device_id":
                frame["device_id"] = "arm-02"
            elif self.change == "frozen":
                frame["frozen"] = True
            elif self.change == "stale":
                frame["source_captured_at"] = (
                    datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            elif self.change == "bad_image":
                frame["image_bytes"] = b"invalid-png"
            elif self.change == "capture_failure":
                raise RuntimeError("injected capture failure")
        return frame


@pytest.mark.parametrize("change", [
    "binding_revision", "device_unique_id", "camera_id", "device_id",
])
def test_review_wake_gate_rejects_binding_change_on_dark_frame(monkeypatch, change):
    bundle = ready_bundle()
    camera = ChangingCamera(bundle, change=change, at=1)
    checks, _, _ = run_gate(
        monkeypatch, bundle=bundle, camera=camera, screen_wake=True, wechat_send=False)
    assert checks
    assert camera.calls == 1


def test_review_wake_gate_does_not_return_dark_first_frame_after_screen_lights(
        monkeypatch):
    bundle = ready_bundle()
    camera = ChangingCamera(bundle, change="lit", at=2)
    checks, frame, _ = run_gate(
        monkeypatch, bundle=bundle, camera=camera, screen_wake=True, wechat_send=False)
    assert not checks
    assert wake_frame_quality(frame) == "waiting"
    assert frame["source_frame_seq"] == 1
    assert camera.calls == 1


@pytest.mark.parametrize("change", ["binding_revision", "lit"])
def test_review_next_dark_frame_change_blocks_motion(rig, monkeypatch, change):
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    original = cb.bundle_path("arm-01", root).read_bytes()
    camera = ChangingCamera(
        bundle, change=change, at=2, detectable_dark=True)
    monkeypatch.setattr(calibration_gate, "EDGE_CONSENSUS_INTERVAL_S", 0)

    async def gate():
        return await calibration_gate.check_live_calibration(
            device_id=bundle["deviceId"], camera_id=bundle["cameraId"],
            daemon_addr=bundle["daemonAddr"], daemon_session=daemon.session,
            estop_locked=False, camera=camera, bundle=bundle, screen_wake=True,
            require_pose=any(cmd.startswith("TAP2 ") for cmd in daemon.commands))

    driver.screen_wake_gate = gate
    try:
        asyncio.run(driver.wake_screen({
            "binding": sg.binding_snapshot(bundle), "confirmed": True}))
    except DriverError:
        pass
    assert camera.calls >= 2
    assert cb.bundle_path("arm-01", root).read_bytes() == original
    assert not daemon.commands


@pytest.mark.parametrize("change", ["frozen", "stale", "bad_image", "capture_failure"])
def test_review_next_camera_fault_remains_blocked(monkeypatch, change):
    bundle = ready_bundle()
    camera = ChangingCamera(bundle, change=change, at=1)
    checks, _, _ = run_gate(
        monkeypatch, bundle=bundle, camera=camera, screen_wake=True, wechat_send=False)
    assert "camera_unavailable" in {item["code"] for item in checks}


@pytest.mark.parametrize("change", ["old_run", "duplicate_ids", "failed", "infinite_error"])
def test_review_validation_point_provenance_cannot_be_substituted(change):
    bundle = validated_bundle(gesture_bundle())
    if change == "old_run":
        bundle["autoCalibration"]["runId"] = "new-calibration"
    elif change == "duplicate_ids":
        bundle["validation"]["points"][1]["targetId"] = (
            bundle["validation"]["points"][0]["targetId"])
    elif change == "failed":
        bundle["validation"]["points"] = [
            {**point, "passed": False} for point in bundle["validation"]["points"]]
    else:
        bundle["validation"]["points"][0]["touchTargetErrorPx"] = float("inf")
    original = deepcopy(bundle)
    with pytest.raises(ValueError):
        resolve_wake_target(bundle)
    assert bundle == original


@pytest.mark.parametrize("change", ["session", "estop", "stop", "disconnect"])
def test_review_context_revocation_after_travel_never_presses_or_replays(rig, change):
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    original = cb.bundle_path("arm-01", root).read_bytes()
    normal_gate = driver.screen_wake_gate
    changed = False

    async def gate():
        nonlocal changed
        if daemon.commands and not changed:
            changed = True
            if change == "session":
                daemon.session = "replacement-session"
            elif change == "estop":
                driver._estop_latched = True
            elif change == "stop":
                await driver.stop()
            else:
                await driver.disconnect()
        return await normal_gate()

    driver.screen_wake_gate = gate
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.wake_screen({
            "binding": sg.binding_snapshot(bundle), "confirmed": True}))
    assert daemon.commands and all(cmd.startswith("XY ") for cmd in daemon.commands)
    assert error.value.category == "driver_disconnected"
    assert not getattr(error.value, "motion_not_started", False)
    assert not driver.connected
    assert cb.bundle_path("arm-01", root).read_bytes() == original


def test_review_cancellation_after_tap_does_not_append_retract_or_replay(rig):
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    original = cb.bundle_path("arm-01", root).read_bytes()
    normal_gate = driver.screen_wake_gate

    async def gate():
        if any(cmd.startswith("TAP2 ") for cmd in daemon.commands):
            raise asyncio.CancelledError
        return await normal_gate()

    driver.screen_wake_gate = gate
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(driver.wake_screen({
            "binding": sg.binding_snapshot(bundle), "confirmed": True}))
    assert daemon.commands[-1].startswith("TAP2 ")
    assert sum(cmd.startswith("TAP2 ") for cmd in daemon.commands) == 1
    assert not driver.connected
    assert cb.bundle_path("arm-01", root).read_bytes() == original


@pytest.mark.parametrize("failed_command", ["END_MOTION", "RELEASE"])
def test_review_cleanup_failure_after_wake_is_unknown_without_replay(rig, failed_command):
    daemon, driver, bundle, _, _ = prepare_wake(rig)
    daemon.response_hook = lambda command, reply, _: (
        "ERROR: injected cleanup rejection" if command == failed_command else reply)
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.wake_screen({
            "binding": sg.binding_snapshot(bundle), "confirmed": True}))
    assert error.value.category == "driver_disconnected"
    assert not getattr(error.value, "motion_not_started", False)
    assert sum(cmd.startswith("TAP2 ") for cmd in daemon.commands) == 1
    assert not driver.connected


def test_review_bright_frame_is_not_unlock_or_physical_motion_evidence(rig):
    _, driver, bundle, _, _ = prepare_wake(rig)
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True}))
    assert result["effect_status"] == "awake"
    assert result["controller_completed"] is True
    assert result["motion_verified"] is False
    assert not result.get("unlocked") and not result.get("home_verified")
    assert all(phase["verification"]["verified"] is False for phase in result["phases"])


@pytest.mark.parametrize("action", ["cancel", "stop", "estop", "disconnect"])
def test_review_pending_tap_reply_is_interruptible_without_retract_or_replay(rig, action):
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    original = cb.bundle_path("arm-01", root).read_bytes()
    blocked, release = threading.Event(), threading.Event()
    params = {"binding": sg.binding_snapshot(bundle), "confirmed": True}

    def hold_tap(command, reply, _):
        if command.startswith("TAP2 "):
            blocked.set()
            release.wait(3)
        return reply

    daemon.response_hook = hold_tap

    async def run():
        motion = asyncio.create_task(driver.wake_screen(params))
        try:
            assert await asyncio.to_thread(blocked.wait, 2)
            if action == "cancel":
                motion.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(motion, 1)
            else:
                await asyncio.wait_for(getattr(driver, action)(), 1)
                with pytest.raises(DriverError):
                    await asyncio.wait_for(motion, 1)
            assert not release.is_set()
            assert not driver.connected
            assert daemon.commands[-1].startswith("TAP2 ")
            commands = list(daemon.commands)
            with pytest.raises(DriverError):
                await driver.wake_screen(params)
            assert daemon.commands == commands
            assert sum(cmd.startswith("TAP2 ") for cmd in commands) == 1
        finally:
            release.set()
            if not motion.done():
                motion.cancel()
            await asyncio.gather(motion, return_exceptions=True)

    asyncio.run(run())
    assert cb.bundle_path("arm-01", root).read_bytes() == original


@pytest.mark.parametrize("fault", [
    "frozen", "stale", "bad_image", "binding_revision", "capture_failure",
])
def test_review_post_tap_observation_fault_is_unknown_without_followup_motion(rig, fault):
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    original = cb.bundle_path("arm-01", root).read_bytes()
    normal_gate = driver.screen_wake_gate

    async def gate():
        checks, frame, deviation = await normal_gate()
        if any(cmd.startswith("TAP2 ") for cmd in daemon.commands):
            if fault == "frozen":
                frame["frozen"] = True
            elif fault == "stale":
                frame["source_captured_at"] = (
                    datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            elif fault == "bad_image":
                frame["image_bytes"] = b"invalid-png"
            elif fault == "binding_revision":
                frame["binding_revision"] += 1
            else:
                raise RuntimeError("injected post-tap capture failure")
        return checks, frame, deviation

    driver.screen_wake_gate = gate
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True}))
    assert result["controller_completed"] is False
    assert result["outcome_hint"] == "unknown"
    assert result["effect_status"] == "unverified"
    assert result["motion_verified"] is False
    assert not result.get("motion_not_started")
    assert daemon.commands[-1].startswith("Z ")
    assert sum(cmd.startswith("TAP2 ") for cmd in daemon.commands) == 1
    assert sum(cmd.startswith("Z ") for cmd in daemon.commands) >= 1
    assert cb.bundle_path("arm-01", root).read_bytes() == original
