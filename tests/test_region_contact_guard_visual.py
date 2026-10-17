"""Shared-layout and OCR final position guards use actual controller positions."""
import asyncio

import pytest

from drivers.arm import DriverError
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401
from tests.test_layout_observation import offline_only  # noqa: F401
from tests.test_pre_enable_s3_driver import locate
from tests.test_region_qualified_clicks import region_driver


@pytest.mark.parametrize('source,drift,blocked', [('ocr', 20, True), ('ocr', .01, False),
                                                ('shared', 20, True), ('shared', 0, False)])
def test_region_final_contact_stays_within_original_target(
        target_rig, monkeypatch, source, drift, blocked):
    rig = target_rig
    region_driver(rig.arm, monkeypatch)
    target = locate(rig) if source == 'ocr' else {'target_id': 'wechat_chats_tab'}

    def after_travel(line):
        if line.startswith('XY '):
            rig.transport.position['x'] += drift

    rig.transport.after_send = after_travel
    if blocked:
        with pytest.raises(DriverError, match='安全范围|安全区'):
            asyncio.run(rig.arm.click(target))
        assert not any(cmd.startswith('TAP') for cmd in motion(rig))
    else:
        asyncio.run(rig.arm.click(target))
        assert sum(cmd.startswith('TAP ') for cmd in motion(rig)) == 1


@pytest.mark.parametrize('source', ['ocr', 'shared'])
def test_region_final_guard_keeps_pose_compensated_targets_valid(target_rig, monkeypatch, source):
    import cv2
    import numpy as np
    from drivers import screen_pose

    rig = target_rig
    region_driver(rig.arm, monkeypatch)
    matrix = np.float32([[1, 0, 7], [0, 1, 10]])
    rig.source.image = cv2.warpAffine(rig.source.image, matrix, (1280, 720),
                                     borderValue=(20, 20, 20))
    saved = rig.bundle['screen']['edgeModel']['corners']
    rig.pose = screen_pose.fit_pose(saved, (np.asarray(saved) + [7, 10]).tolist())
    target = locate(rig) if source == 'ocr' else {'target_id': 'wechat_chats_tab'}
    asyncio.run(rig.arm.click(target))
    assert sum(cmd.startswith('TAP ') for cmd in motion(rig)) == 1
