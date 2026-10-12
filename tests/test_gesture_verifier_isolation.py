"""Verifier routing through real drivers, legacy frame diff and fake probe frames."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from drivers.arm import DriverError
from drivers.probe_motion import PROFILE_NAME
from drivers.real_camera import CameraMotionVerifier
from tests.test_probe_motion_driver_integration import gesture, rig


@pytest.fixture()
def split_rig(rig, monkeypatch):
    rig.legacy = CameraMotionVerifier(rig.camera)
    rig.probe = rig.verifier
    for verifier in (rig.legacy, rig.probe):
        for method in ("before", "after"):
            monkeypatch.setattr(verifier, method, AsyncMock(wraps=getattr(verifier, method)))
        abort = getattr(verifier, "abort", None)
        monkeypatch.setattr(verifier, "abort", AsyncMock(wraps=abort), raising=False)
    monkeypatch.setattr(rig.probe, "readiness", Mock(wraps=rig.probe.readiness))
    rig.driver.motion_verifier = rig.legacy
    rig.driver.gesture_motion_verifier = rig.probe
    return rig


def payload(rig, action, legacy_coordinates=False):
    point = {"source": "camera_vision", "cameraX": 160, "cameraY": 120,
             "frameW": 420, "frameH": 300}
    if action == "click" or (action == "double_tap" and legacy_coordinates):
        return {"target_id": "gesture_point", "target_ref": point}
    if legacy_coordinates:
        return {"start": point, "end": {**point, "cameraY": 160}, "durationMs": 600}
    return gesture(rig, action)


def assert_unused(verifier):
    verifier.before.assert_not_called()
    verifier.after.assert_not_called()
    verifier.abort.assert_not_called()
    assert verifier.history == []


async def assert_released(rig):
    commands = [command for _, command in rig.daemon.requests]
    assert commands[-2:] == ["END_MOTION", "RELEASE"]
    assert rig.daemon.lease is None and not rig.daemon.transactions
    assert not rig.driver._motion_lock.locked()
    assert rig.probe._sampler is None
    assert rig.camera.active_captures == 0
    count = rig.camera.calls
    await asyncio.sleep(.06)
    assert rig.camera.calls == count


@pytest.mark.parametrize("missing_probe_template", [False, True])
def test_click_keeps_legacy_verifier_even_when_dedicated_probe_is_not_ready(
        split_rig, missing_probe_template):
    rig = split_rig
    rig.daemon.response_hook = None
    if missing_probe_template:
        (rig.root / "devices" / "arm-01" / PROFILE_NAME).unlink()

    async def exercise():
        result = await rig.driver.click(payload(rig, "click"))
        await assert_released(rig)
        return result

    result = asyncio.run(exercise())
    assert result["motion_verified"] is True and result["outcome_hint"] == "success"
    assert [phase["phase"] for phase in result["phases"]] == ["travel_xy", "tap"]
    assert all("diff_score" in phase["verification"] for phase in result["phases"])
    assert len(rig.legacy.history) == 2
    assert rig.legacy.before.await_count == rig.legacy.after.await_count == 2
    rig.legacy.abort.assert_awaited_once()
    rig.probe.readiness.assert_not_called()
    assert_unused(rig.probe)
    assert len(rig.daemon.commands) == 2
    assert rig.daemon.commands[-1] == "TAP 3.000 F1500"


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("legacy_coordinates", [False, True], ids=["normalized", "camera-legacy"])
def test_gestures_use_dedicated_probe_for_every_phase_and_cleanup(
        split_rig, action, legacy_coordinates):
    rig = split_rig

    async def exercise():
        result = await getattr(rig.driver, action)(payload(rig, action, legacy_coordinates))
        await assert_released(rig)
        return result

    result = asyncio.run(exercise())
    assert result["motion_verified"] is True and result["outcome_hint"] == "success"
    assert all(phase["verification"]["measurement_kind"] == "registered_probe_trace"
               for phase in result["phases"])
    assert len(rig.probe.history) == len(result["phases"]) == 2
    assert rig.probe.before.await_count == rig.probe.after.await_count == 2
    assert rig.probe.abort.await_count >= 1
    assert_unused(rig.legacy)
    macro = "TAP2 " if action == "double_tap" else "SWIPE "
    assert len(rig.daemon.commands) == 2 and rig.daemon.commands[-1].startswith(macro)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_unready_dedicated_probe_rejects_without_falling_back(split_rig, action):
    rig = split_rig
    (rig.root / "devices" / "arm-01" / PROFILE_NAME).unlink()
    with pytest.raises(DriverError) as error:
        asyncio.run(getattr(rig.driver, action)(payload(rig, action)))
    assert error.value.category == "driver_policy_rejected"
    rig.probe.readiness.assert_called_once()
    assert_unused(rig.legacy)
    assert_unused(rig.probe)
    assert not rig.daemon.commands
    assert not any(command.startswith(("CLAIM ", "BEGIN_MOTION"))
                   for _, command in rig.daemon.requests)
    assert not rig.driver._motion_lock.locked()


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_unconfigured_gesture_verifier_retains_existing_verifier_fallback(split_rig, action):
    rig = split_rig
    rig.driver.gesture_motion_verifier = None
    rig.driver.motion_verifier = rig.probe

    async def exercise():
        result = await getattr(rig.driver, action)(payload(rig, action))
        await assert_released(rig)
        return result

    result = asyncio.run(exercise())
    assert result["motion_verified"] is True and result["outcome_hint"] == "success"
    assert rig.probe.before.await_count == rig.probe.after.await_count == 2
    assert_unused(rig.legacy)


@pytest.mark.parametrize("action", ["click", "double_tap", "swipe"])
def test_failed_selected_verifier_stops_before_touch_and_cleans_only_itself(
        split_rig, monkeypatch, action):
    rig = split_rig
    selected, unused = ((rig.legacy, rig.probe) if action == "click" else (rig.probe, rig.legacy))
    if action == "click":
        rig.daemon.response_hook = None
    monkeypatch.setattr(selected, "after", AsyncMock(side_effect=OSError("Synthetic evidence failure")))

    async def exercise():
        result = await getattr(rig.driver, action)(payload(rig, action))
        await assert_released(rig)
        return result

    result = asyncio.run(exercise())
    assert result["motion_verified"] is False and result["outcome_hint"] == "unknown"
    assert len(rig.daemon.commands) == 1 and rig.daemon.commands[0].startswith("XY ")
    selected.after.assert_awaited_once()
    assert selected.abort.await_count >= 1
    assert_unused(unused)
    if action == "double_tap":
        assert result["tap_count"] == 0
    elif action == "swipe":
        assert result["swipe_executed"] is False


@pytest.mark.parametrize("action", ["click", "double_tap", "swipe"])
@pytest.mark.parametrize("error_type", [OSError, asyncio.CancelledError])
def test_selected_verifier_final_abort_failure_still_releases_without_cross_cleanup(
        split_rig, monkeypatch, action, error_type):
    rig = split_rig
    selected, unused = ((rig.legacy, rig.probe) if action == "click" else (rig.probe, rig.legacy))
    if action == "click":
        rig.daemon.response_hook = None
    abort = selected.abort

    async def fail_final_abort():
        await abort()
        if len(selected.history) == 2:
            raise error_type("Synthetic selected-verifier cleanup failure")

    monkeypatch.setattr(selected, "abort", AsyncMock(side_effect=fail_final_abort))

    async def exercise():
        with pytest.raises(DriverError if error_type is OSError else asyncio.CancelledError) as error:
            await getattr(rig.driver, action)(payload(rig, action))
        await assert_released(rig)
        if error_type is OSError:
            assert error.value.category == "driver_disconnected"

    asyncio.run(exercise())
    assert len(selected.history) == len(rig.daemon.commands) == 2
    assert_unused(unused)
    assert not rig.driver.connected
