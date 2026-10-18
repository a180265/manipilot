"""A completed camera-observed swipe may proceed to independent UI review."""
import json

import pytest

from drivers.arm import DriverError
from tests.test_gesture_completion_reply import (  # noqa: F401
    EvidenceVerifier, execute, rig,
)


def camera_verifier():
    verifier = EvidenceVerifier(observation_only=True)
    verifier.measurement_kind = "frame_change"
    return verifier


def test_completed_camera_swipe_allows_ui_review_without_claiming_visual_motion(rig):
    rig.driver.gesture_motion_verifier = camera_verifier()
    result = execute(rig, "swipe")
    assert result["controller_completed"] is True
    assert result["swipe_executed"] is True
    assert result["motion_verified"] is False
    assert result["motion_observation_only"] is True
    assert result["outcome_hint"] == "success"
    assert result["effect_status"] == "unverified"
    assert result["message"] is None
    assert sum(cmd.startswith("SWIPE ") for cmd in rig.daemon.commands) == 1


def test_active_probe_verifier_does_not_inherit_camera_observation_permission(rig):
    rig.driver.motion_verifier = camera_verifier()
    rig.driver.gesture_motion_verifier = EvidenceVerifier(observation_only=True)
    result = execute(rig, "swipe")
    assert result["controller_completed"] is True
    assert result["motion_verified"] is False
    assert result["motion_observation_only"] is False
    assert result["outcome_hint"] == "unknown"


@pytest.mark.parametrize("stop_phase", ["swipe_to_start", "swipe"])
def test_camera_stop_required_is_unknown_even_when_controller_reply_is_valid(rig, stop_phase):
    verifier = camera_verifier()

    async def after(label):
        return {"verified": False, "stop_required": label == stop_phase}

    verifier.after = after
    rig.driver.gesture_motion_verifier = verifier
    result = execute(rig, "swipe")
    assert result["controller_completed"] is False
    assert result["swipe_executed"] is False
    assert result["outcome_hint"] == "unknown"
    if stop_phase == "swipe_to_start":
        assert not any(cmd.startswith("SWIPE ") for cmd in rig.daemon.commands)


def test_camera_observation_does_not_accept_wrong_controller_endpoint(rig):
    rig.driver.gesture_motion_verifier = camera_verifier()

    def wrong_endpoint(line, original, _connection):
        if line.startswith("SWIPE "):
            return json.dumps({**json.loads(original), "x": 10000})
        return original

    rig.daemon.response_hook = wrong_endpoint
    with pytest.raises(DriverError, match="结果未知"):
        execute(rig, "swipe")
    assert not rig.driver.connected
    assert sum(cmd.startswith("SWIPE ") for cmd in rig.daemon.commands) == 1
