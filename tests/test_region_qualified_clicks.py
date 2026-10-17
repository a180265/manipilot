"""Region-qualified calibration cannot fall back to an unbounded tap."""
import asyncio
from copy import deepcopy

import pytest

from adapters.vision.ocr import OcrWord
from drivers import calibration_bundle as cb
from drivers.arm import DriverError
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401
from tests.test_layout_text_location import TextEngine
from tests.test_layout_observation import offline_only  # noqa: F401
from tests.test_pre_enable_s3_driver import locate


def region_driver(driver, monkeypatch):
    # Qualification/geometry evidence is tested in bundle tests; exercise the
    # real target-resolution and daemon path with a region-qualified snapshot.
    original = cb.load_bundle
    # This fixture toggles only the execution-policy branch, without creating
    # a complete region calibration. Preserve its actual source calibration
    # identity when the IME coordinate bridge validates the frozen binding.
    from drivers import screen_gestures as sg
    original_binding = sg.binding_snapshot
    base = original(driver.device_id, driver.data_root)
    base_binding = original_binding(base)
    monkeypatch.setattr(sg, 'binding_snapshot', lambda bundle:
                        deepcopy(base_binding) if (bundle.get('validation') or {}).get('criterion') == 'target_region_v1'
                        else original_binding(bundle))

    def located(*args, **kwargs):
        bundle = deepcopy(original(*args, **kwargs))
        bundle.setdefault('validation', {})['criterion'] = 'target_region_v1'
        bundle['xy'].pop('clickPointPolicy', None)
        return bundle

    # Layout observations and final contact guards must see the same snapshot.
    monkeypatch.setattr(cb, 'load_bundle', located)
    envelope = cb.mapping_error_bound
    # Preserve the fixture's measured envelope so published layout safe_rects
    # remain valid. Region proof parsing is covered by the contract tests.
    measured_bound = envelope(original(driver.device_id, driver.data_root))
    if measured_bound is None:  # minimal IME motion fixture has no fit diagnostics
        measured_bound = 3.5
    monkeypatch.setattr(cb, 'mapping_error_bound', lambda bundle, **kwargs:
                        measured_bound if (bundle.get('validation') or {}).get('criterion') == 'target_region_v1'
                        else envelope(bundle, **kwargs))


def test_region_qualified_tiny_ocr_never_falls_back_to_centre(target_rig, monkeypatch):
    rig = target_rig
    region_driver(rig.arm, monkeypatch)
    rig.camera.ocr_engine = TextEngine(words=[OcrWord('Ann', 12, 20, 8, 6, .99)])
    located = asyncio.run(rig.camera.locate_text('Ann', region='account', exact=True))
    target = {'target_id': 'conversation_item', 'target_ref': {'source': 'camera_ocr', **located}}
    with pytest.raises(DriverError, match='安全区|落点|交集'):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


def test_region_qualified_ocr_with_room_still_taps_once(target_rig, monkeypatch):
    rig = target_rig
    region_driver(rig.arm, monkeypatch)
    target = locate(rig)
    result = asyncio.run(rig.arm.click(target))
    safety = result['click_point']['safety']
    assert safety['inset_px'] > 0
    assert sum(command.startswith('TAP ') for command in motion(rig)) == 1


def test_region_qualified_point_only_visual_target_is_rejected(target_rig, monkeypatch):
    rig = target_rig
    region_driver(rig.arm, monkeypatch)
    with pytest.raises(DriverError, match='真实可点击范围'):
        rig.arm._resolve_business_target({'target_id': 'gesture_point', 'target_ref': {
            'source': 'camera_vision', 'cameraX': 148, 'cameraY': 258,
            'frameW': 1280, 'frameH': 720}})
    assert motion(rig) == []
