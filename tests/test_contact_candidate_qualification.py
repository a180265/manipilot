"""Real region/contact gate: screen content is not another physical display."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from runtime import calibration_gate as gate
from runtime.layout_geometry import LayoutGeometryError, locate_in_region
from runtime.layout_region import _screen_candidates
from tests.test_contact_reference_gate import Camera, bundle, qualify, run


CORNERS = np.array([[100, 100], [800, 100], [800, 450], [100, 450]], dtype=float)
REGION = {'rect': {'x': .01, 'y': .01, 'width': .98, 'height': .98}}


def scene(corners=CORNERS):
    image = np.full((720, 1280, 3), 20, np.uint8)
    cv2.fillConvexPoly(image, np.int32(corners), (180, 180, 180))
    for y in range(160, 420, 45):
        cv2.putText(image, 'camera evidence 1234', (180, y),
                    cv2.FONT_HERSHEY_SIMPLEX, .8, (60, 60, 60), 2)
    return image


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(gate, 'EDGE_CONSENSUS_INTERVAL_S', 0)
    camera = Camera(scene())
    calibration = bundle()
    calibration['screen']['edgeModel']['corners'] = CORNERS.tolist()
    options = dict(device_id='arm-01', camera_id='cam-01',
                   daemon_addr='127.0.0.1:8795', daemon_session='session-01',
                   estop_locked=False, camera=camera, bundle=calibration,
                   require_pose=True, search_region=REGION)
    reference = qualify(options)
    return camera, options, reference


@pytest.mark.parametrize('change', ['icon', 'large_content', 'reflection', 'bridged_icon'])
def test_contact_measures_current_sides_before_qualifying_other_candidates(rig, change):
    camera, options, reference = rig
    if change in ('icon', 'bridged_icon'):
        cv2.rectangle(camera.image, (300, 220), (355, 275), (5, 5, 5), -1)
    elif change == 'large_content':
        cv2.rectangle(camera.image, (220, 190), (650, 370), (5, 5, 5), -1)
    else:
        cv2.rectangle(camera.image, (400, 520), (450, 620), (220, 220, 220), -1)
    if change in ('bridged_icon', 'large_content'):
        cv2.rectangle(camera.image, (490, 70), (530, 94), (180, 180, 180), -1)
        cv2.rectangle(camera.image, (506, 94), (514, 104), (180, 180, 180), -1)
    checks, frame, _ = run(options, contact_reference=reference)
    assert checks == [], checks
    np.testing.assert_allclose(frame['_screen_pose']['corners'], CORNERS, atol=.2)


@pytest.mark.parametrize('change', ['second_screen', 'third_dark_screen',
                                   'moved_3px', 'missing_side', 'clipped'])
def test_contact_preserves_physical_ambiguity_movement_and_four_side_guards(rig, change):
    camera, options, reference = rig
    overrides = {}
    if change in ('second_screen', 'third_dark_screen'):
        if change == 'third_dark_screen':
            cv2.rectangle(camera.image, (400, 520), (450, 620), (220, 220, 220), -1)
        cv2.rectangle(camera.image, (900, 140), (1200, 500),
                      (5, 5, 5) if change == 'third_dark_screen' else (180, 180, 180), -1)
    elif change == 'moved_3px':
        camera.image = scene(CORNERS + (3, 0))
    elif change == 'missing_side':
        cv2.rectangle(camera.image, (770, 80), (850, 480), (20, 20, 20), -1)
    else:
        overrides['search_region'] = {'rect': {'x': .1, 'y': .01, 'width': .89, 'height': .98}}
    checks, _, _ = run(options, contact_reference=reference, **overrides)
    assert checks
    if change in ('second_screen', 'third_dark_screen'):
        with pytest.raises(LayoutGeometryError, match='multiple_screen_candidates'):
            locate_in_region(camera.image, REGION, CORNERS.tolist(),
                             contact_reference_corners=CORNERS.tolist())


def test_complete_enumeration_does_not_hide_third_dark_screen():
    image = scene()
    cv2.rectangle(image, (400, 520), (450, 620), (220, 220, 220), -1)
    cv2.rectangle(image, (900, 140), (1200, 500), (5, 5, 5), -1)
    default = _screen_candidates(image)
    complete = _screen_candidates(image, limit=None)
    assert len(default) == 2
    assert not any(candidate[:, 0].mean() > 900 for candidate in default)
    assert any(candidate[:, 0].mean() > 900 for candidate in complete)


def test_contact_replays_real_v12_hover_without_screen_content_false_positive(monkeypatch):
    # Captures contain private content and stay local; both exact hashes are pinned.
    capture_dir = Path(os.environ.get('SCREEN_EDGE_V12_CAPTURE_DIR',
        str(Path(__file__).resolve().parents[1] / 'evidence/store/'
            'wx-session-19891fe17f004c2b963a4347f3c21041')))
    captures = [
        ('evd-9f3cf3c24ba7468db9fa.png',
         '1a56b4dd586e1293e537f76f7b97105f4acfc176180a2d42217b6aea577c2c5b'),
        ('evd-57a23b53805845168c8b.png',
         '9a6f09bc89c7d9a5d9e964021f073d51750ec7ab4d36276805e493fbb0e38efc'),
    ]
    if not all((capture_dir / name).is_file() for name, _ in captures):
        pytest.skip('Private v12 captures are not installed')
    images = []
    for name, digest in captures:
        data = (capture_dir / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        images.append(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR))
    monkeypatch.setattr(gate, 'EDGE_CONSENSUS_INTERVAL_S', 0)
    calibration = bundle()
    calibration['screen']['edgeModel']['corners'] = [
        [117.40038028493916, 69.02240535628027], [1155.6388277156227, 81.51613150704537],
        [1160.450592067772, 549.5444224209796], [102.51861050799332, 539.6475398360182]]
    camera = Camera(images[0])
    options = dict(device_id='arm-01', camera_id='cam-01',
                   daemon_addr='127.0.0.1:8795', daemon_session='session-01',
                   estop_locked=False, camera=camera, bundle=calibration, require_pose=True,
                   search_region={'rect': {'x': .021875, 'y': .05416666666666667,
                       'width': .93828125, 'height': .8194444444444444}})
    reference = qualify(options)
    camera.image = images[1]
    checks, frame, _ = run(options, contact_reference=reference)
    assert checks == [], checks
    assert frame['_screen_pose']['movementPx'] < 2
    assert '_contact_reference' not in frame['_screen_pose']
