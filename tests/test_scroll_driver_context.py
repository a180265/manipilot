"""Real daemon transport checks the pinned task immediately before sendall."""
import asyncio

import pytest

from drivers import screen_gestures as sg
from drivers.arm import DriverError
from tests.test_screen_gesture_driver import point, rig  # noqa: F401


@pytest.mark.parametrize("boundary", ["none", "first_motion", "contact"])
def test_scroll_execution_guard_reaches_transport_lock(rig, boundary):
    daemon, driver, bundle, _ = rig
    transport = driver._transport
    original = transport.cmd
    changed = []
    guarded_under_lock = []

    def cmd(line, *, timeout=None, input_claim=None):
        if not changed and (
                boundary == "first_motion" and line.startswith(("XY ", "Z "))
                or boundary == "contact" and line.startswith("SWIPE ")):
            changed.append(True)
        return original(line, timeout=timeout, input_claim=input_claim)

    def guard():
        if transport._lock.locked():
            guarded_under_lock.append(True)
        if changed:
            assert transport._lock.locked(), "Context must be rechecked inside send lock"
            error = DriverError("driver_uncalibrated", "offline context changed")
            error.motion_not_started = True
            raise error

    transport.cmd = cmd
    request = {"start": point(.5, .75), "end": point(.5, .3),
               "binding": sg.binding_snapshot(bundle)}
    if boundary == "none":
        result = asyncio.run(driver.swipe(request, execution_guard=guard))
        assert result["controller_completed"] and result["swipe_executed"]
        assert any(line.startswith("SWIPE ") for line in daemon.commands)
    else:
        with pytest.raises(DriverError) as caught:
            asyncio.run(driver.swipe(request, execution_guard=guard))
        assert changed
        assert not any(line.startswith("SWIPE ") for line in daemon.commands)
        motion = [line for line in daemon.commands if line.startswith(("XY ", "Z "))]
        if boundary == "first_motion":
            assert motion == []
            assert caught.value.motion_not_started is True
        else:
            assert motion
            assert getattr(caught.value, "motion_not_started", False) is False
    assert guarded_under_lock
