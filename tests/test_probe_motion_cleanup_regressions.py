"""Swipe cleanup with real threaded camera reads; all frames and TCP are fake."""
import asyncio
import threading

import cv2
import numpy as np
import pytest

from drivers.arm import DriverError
from drivers.probe_motion import ProbeMotionVerifier
from drivers.real_camera import RealCameraDriver
from tests.test_probe_motion_driver_integration import gesture, rig


class BlockingFrameProvider:
    last_camera_id = "cam-01"
    last_device_id = "arm-01"
    last_device_unique_id = "uvc-01"
    last_binding_revision = 7
    stream_alive = True

    def __init__(self, scene):
        self.scene = scene
        self.last_frame_seq = 0
        self.calls = 0
        self.active_reads = 0
        self.max_active_reads = 0
        self.lock = threading.Lock()
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.read_finished = threading.Event()
        self.timed_out = False

    def __call__(self):
        with self.lock:
            self.calls += 1
            call = self.calls
            self.active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            if call == 2:
                self.read_started.set()
                if not self.release_read.wait(3):
                    self.timed_out = True
                    raise RuntimeError("Synthetic camera read was not released")
            with self.lock:
                frame = self.scene.frame()
                self.last_frame_seq = frame["source_frame_seq"]
                return cv2.imdecode(np.frombuffer(frame["image_bytes"], np.uint8), cv2.IMREAD_COLOR)
        finally:
            with self.lock:
                self.active_reads -= 1
            if call == 2:
                self.read_finished.set()


@pytest.mark.parametrize("cancel_motion", [False, True], ids=["after-frame", "cancel-swipe"])
def test_swipe_drains_threaded_read_before_after_frame_or_cancel_completion(rig, cancel_motion):
    provider = BlockingFrameProvider(rig.camera)
    camera = RealCameraDriver(
        "cam-01", frame_provider=provider, screen_device_id="arm-01", data_root=rig.root,
        ocr_engine=object())
    verifier = ProbeMotionVerifier(camera, device_id="arm-01", data_root=rig.root)
    rig.driver.motion_verifier = verifier
    rig.daemon.mpos[:] = [12, 8, 2]
    reply_gate = threading.Event()
    command_started = threading.Event()
    reply_timed_out = []

    def motion_reply(line, reply, connection_id):
        if line.startswith("SWIPE "):
            command_started.set()
            if not provider.read_started.wait(2):
                reply_timed_out.append("read")
            if cancel_motion and not reply_gate.wait(3):
                reply_timed_out.append("reply")
        return reply

    rig.daemon.response_hook = motion_reply

    async def exercise():
        operation = asyncio.create_task(rig.driver.swipe(gesture(rig, "swipe")))
        try:
            assert await asyncio.to_thread(provider.read_started.wait, 1)
            assert await asyncio.to_thread(command_started.wait, 1)
            if cancel_motion:
                operation.cancel()
            # Keep the synchronous reader blocked long enough for after()/abort()
            # to run; release it ourselves so both passing and failing runs drain.
            await asyncio.wait({operation}, timeout=.15)
            completed_with_read = operation.done() and provider.active_reads > 0
        finally:
            provider.release_read.set()
            reply_gate.set()
            results = await asyncio.wait_for(
                asyncio.gather(operation, return_exceptions=True), 2)
            assert await asyncio.to_thread(provider.read_finished.wait, 1)
            await camera.close()
        assert not provider.timed_out and not reply_timed_out
        assert not completed_with_read, "Swipe completed/cancelled while its camera read was still running"
        assert provider.max_active_reads == 1, "After-frame read overlapped the cancelled sampler read"
        assert provider.active_reads == 0 and verifier._sampler is None
        if cancel_motion:
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results[0]["motion_verified"] is False
            assert results[0]["outcome_hint"] == "unknown"
        assert not rig.driver._motion_lock.locked()

    asyncio.run(exercise())
    assert len(rig.daemon.commands) == 1
    assert rig.daemon.commands[0].startswith("SWIPE ")


@pytest.mark.parametrize("error_type", [OSError, asyncio.CancelledError])
def test_final_verifier_abort_failure_cannot_skip_release_or_leave_motion_lock(rig, monkeypatch, error_type):
    abort = rig.verifier.abort

    async def abort_after_final_proof():
        await abort()
        if rig.verifier.history and rig.verifier.history[-1]["phase"] == "swipe":
            raise error_type("Synthetic final sampler cleanup failure")

    monkeypatch.setattr(rig.verifier, "abort", abort_after_final_proof)

    async def exercise():
        try:
            result = await rig.driver.swipe(gesture(rig, "swipe"))
        except (Exception, asyncio.CancelledError) as exc:
            result = exc
        commands = [command for _, command in rig.daemon.requests]
        # Inspect before fixture disconnect, which would otherwise hide leaked leases.
        assert commands[-2:] == ["END_MOTION", "RELEASE"]
        assert rig.daemon.lease is None and not rig.daemon.transactions
        assert not rig.driver._motion_lock.locked()
        assert rig.verifier._sampler is None
        if error_type is asyncio.CancelledError:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert isinstance(result, DriverError)
            assert result.category == "driver_disconnected"
        assert len(rig.daemon.commands) == 2
        assert rig.daemon.commands[-1].startswith("SWIPE ")

    asyncio.run(exercise())
