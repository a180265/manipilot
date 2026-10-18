"""A quiet camera frame must not prevent an otherwise confirmed single contact."""
import asyncio

import pytest

from drivers.arm import DriverError
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401


class QuietFrames:
    measurement_kind = 'frame_change'
    observation_only = True

    async def before(self, label):
        pass

    async def after(self, label):
        return {'verified': False, 'diff_score': .7, 'threshold': 1.5}


def test_quiet_camera_does_not_stop_before_single_contact(target_rig):
    rig = target_rig
    rig.arm.motion_verifier = QuietFrames()
    claims = []
    result = asyncio.run(rig.arm.click({'target_id': 'wechat_chats_tab',
        'input_claim': {'callback': lambda: claims.append(list(motion(rig)))}}))
    assert len([cmd for cmd in motion(rig) if cmd.startswith('TAP ')]) == 1
    assert len(claims) == 1 and not any(c.startswith('TAP ') for c in claims[0])
    assert result['controller_completed'] is True
    assert result['motion_observation_only'] is True
    assert result['motion_verified'] is False
    assert result['outcome_hint'] == 'success'
    assert [p['phase'] for p in result['phases']][-1] == 'tap'


def test_quiet_camera_does_not_hide_controller_endpoint_error(target_rig):
    rig = target_rig
    rig.arm.motion_verifier = QuietFrames()
    original = rig.transport.cmd
    import json
    def bad_endpoint(line, *, timeout=None, input_claim=None):
        response = json.loads(original(line, timeout=timeout, input_claim=input_claim))
        if line.startswith('XY '):
            response['x'] += 1
        return json.dumps(response)
    rig.transport.cmd = bad_endpoint
    with pytest.raises(DriverError, match='终点'):
        asyncio.run(rig.arm.click({'target_id': 'wechat_chats_tab'}))
    assert not any(cmd.startswith('TAP ') for cmd in motion(rig))
