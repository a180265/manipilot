"""Action-local clean camera evidence qualifies approach occlusion, never saved corners."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb
from drivers.arm import DriverError
from drivers.daemon_arm import DaemonArmDriver
from runtime import calibration_gate as gate
from tests.test_live_calibration_gate import ready_bundle
from tests.test_screen_edge_contact_reference import CORNERS, clean_and_bridged, phone


class Camera:
    seq = 0
    metadata = {}

    def __init__(self, image):
        self.image = image
        self.metadata = {}

    async def capture(self):
        self.seq += 1
        ok, encoded = cv2.imencode('.png', self.image)
        assert ok
        now = datetime.now(timezone.utc).isoformat()
        return dict(image_bytes=encoded.tobytes(), camera_id='cam-01',
                    device_id='arm-01', device_unique_id='uvc-01', binding_revision=7,
                    width=1280, height=720, frozen=False, source_frame_seq=self.seq,
                    source_received_at=now, source_captured_at=now,
                    camera_binding_id='mount-1', source_geometry_id='geometry-1') | self.metadata


def bundle():
    value = ready_bundle()
    value['frame'] = dict(width=1280, height=720)
    value['screen']['edgeModel']['corners'] = CORNERS.tolist()
    value['bindingFingerprint'] = cb.binding_fingerprint(
        device_id='arm-01', camera_id='cam-01', camera_device_unique_id='uvc-01',
        daemon_addr='127.0.0.1:8795', frame_width=1280, frame_height=720)
    return value


def textured(image):
    image = image.copy()
    for y in range(150, 510, 40):
        cv2.putText(image, 'WX calibration evidence 1234', (230, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (20, 20, 20), 2)
    return image


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(gate, 'EDGE_CONSENSUS_INTERVAL_S', 0)
    clean, occluded, _ = clean_and_bridged()
    camera = Camera(textured(clean))
    options = dict(device_id='arm-01', camera_id='cam-01',
                   daemon_addr='127.0.0.1:8795', daemon_session='session-01',
                   estop_locked=False, camera=camera, bundle=bundle(), require_pose=True)
    return camera, textured(occluded), options


def run(options, **overrides):
    return asyncio.run(gate.check_live_calibration(**(options | overrides)))


def qualify(options):
    checks, frame, _ = run(options)
    assert checks == [], checks
    reference = frame['_screen_pose']['_contact_reference']
    assert isinstance(reference, gate.ContactReference)
    return reference


@pytest.mark.parametrize('region', [None, {'rect': {'x': .05, 'y': .05, 'width': .9, 'height': .85}}])
def test_real_gate_qualifies_clean_frames_then_measures_occluded_phone(rig, region):
    camera, occluded, options = rig
    options['search_region'] = region
    reference = qualify(options)
    camera.image = occluded
    assert run(options)[0], 'The original strict path must catch the actual approach symptom'
    checks, frame, _ = run(options, contact_reference=reference)
    assert checks == [], checks
    np.testing.assert_allclose(frame['_screen_pose']['corners'], CORNERS, atol=.2)
    assert '_contact_reference' not in frame['_screen_pose'], 'Contact checks cannot renew evidence'


@pytest.mark.parametrize('change', ['expired', 'future', 'seq', 'source', 'binding', 'region',
                                   'session', 'forged', 'wake', 'advisory', 'no_pose'])
def test_reference_binding_and_lifetime_are_mandatory(rig, change):
    camera, occluded, options = rig
    reference = qualify(options)
    camera.image = occluded
    extra = {}
    if change == 'expired':
        reference = replace(reference, observed_at=reference.observed_at - 11)
    elif change == 'future':
        reference = replace(reference, observed_at=reference.observed_at + 100)
    elif change == 'seq':
        camera.metadata['source_frame_seq'] = reference.frame['source_frame_seq']
    elif change == 'source':
        camera.metadata['source_geometry_id'] = 'changed'
    elif change == 'binding':
        extra['bundle'] = options['bundle'] | {'bindingRevision': 8}
    elif change == 'region':
        extra['search_region'] = {'rect': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}
    elif change == 'session':
        extra['daemon_session'] = 'changed'
    elif change == 'forged':
        reference = {'corners': CORNERS.tolist()}
    elif change == 'wake':
        extra['screen_wake'] = True
    elif change == 'advisory':
        extra['screen_edge_advisory'] = True
    elif change == 'no_pose':
        extra['require_pose'] = False
    checks, _, _ = run(options, contact_reference=reference, **extra)
    assert checks


@pytest.mark.parametrize('change', ['movement', 'missing_edge', 'second_display', 'dark', 'frozen'])
def test_reference_preserves_current_observation_and_quality_guards(rig, change):
    camera, occluded, options = rig
    options['search_region'] = {'rect': {'x': .02, 'y': .02, 'width': .96, 'height': .96}}
    reference = qualify(options)
    camera.image = occluded.copy()
    if change == 'movement':
        camera.image = textured(phone(CORNERS + (5, 0)))
    elif change == 'missing_edge':
        cv2.rectangle(camera.image, (1110, 80), (1200, 580), (0, 0, 0), -1)
    elif change == 'second_display':
        # Contact continuation classifies external displays at the existing
        # screen_edge 5% area floor; this is a real qualifying second screen.
        cv2.rectangle(camera.image, (35, 120), (140, 540), (160, 160, 160), -1)
    elif change == 'dark':
        camera.image[:] = 0
    else:
        camera.metadata['frozen'] = True
    assert run(options, contact_reference=reference)[0]


def test_daemon_guard_passes_qualified_reference_and_rejects_gate_failure(rig, tmp_path):
    camera, occluded, options = rig
    checks, frame, _ = run(options)
    assert checks == []
    pose = frame['_screen_pose']
    driver = DaemonArmDriver('arm-01', data_root=tmp_path)
    received = []

    async def live_gate(**kwargs):
        received.append(kwargs.get('contact_reference'))
        checks, frame, _ = await gate.check_live_calibration(**(options | kwargs))
        return {'checks': checks, 'pose': frame.get('_screen_pose')}

    driver.live_calibration_gate = live_gate
    camera.image = occluded
    asyncio.run(driver._contact_pose_guard(pose)())
    assert received == [pose['_contact_reference']]
    camera.image = textured(phone(CORNERS + (5, 0)))
    with pytest.raises(DriverError):
        asyncio.run(driver._contact_pose_guard(pose)())
    assert driver.action_log == []
