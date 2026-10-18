"""Verified reconnect retains the measured Android coordinate origin for input."""
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb, gesture_program as gp
from drivers.calibration_wizard import build_gesture_coordinate_frame
from runtime import automatic_gestures, recovery
from tests.test_gesture_coordinate_frame import _page, CLIENT_INSTANCE_ID, LEGACY_SESSION
from tests.test_screen_gesture_driver import gesture_bundle


@pytest.mark.parametrize('metadata_fault', [None, 'session', 'device', 'missing'])
def test_verified_reconnect_rebuilds_only_same_calibration_metadata(tmp_path, monkeypatch, metadata_fault):
    # This seam tests recovery's derived sidecar; qualification is covered separately.
    monkeypatch.setattr(cb, 'validate_calibration_bundle', lambda *a, **kw: [])
    bundle = gesture_bundle()
    bundle['imeDeviceId'] = 'ime-01'
    bundle['autoCalibration'] = {'runId': 'run-original', 'phoneSessionId': LEGACY_SESSION}
    metadata = {'deviceId': 'ime-01', 'sessionId': LEGACY_SESSION,
                'clientInstanceId': CLIENT_INSTANCE_ID,
                'page': _page('legacy-calibration', legacy=LEGACY_SESSION)}
    bundle['screen']['displayCoordinateMetadata'] = deepcopy(metadata)
    original = build_gesture_coordinate_frame(bundle, metadata, ime_device_id='ime-01', session_id=LEGACY_SESSION)
    gp.save_coordinate_frame('arm-01', original, tmp_path)
    if metadata_fault == 'session':
        bundle['screen']['displayCoordinateMetadata']['sessionId'] = 'other'
    elif metadata_fault == 'device':
        bundle['screen']['displayCoordinateMetadata']['deviceId'] = 'another-phone'
    elif metadata_fault == 'missing':
        del bundle['screen']['displayCoordinateMetadata']
    cb.save_bundle(bundle, tmp_path)
    monkeypatch.setattr(automatic_gestures, 'restore_certificates', lambda *a, **kw: {})
    result = recovery.attempt_recovery('arm-01', bundle=bundle, new_session='reconnected',
                                       data_root=tmp_path, arm_anchor_verifier=lambda _: True)
    assert result['recovered'] is True
    frame = gp.load_coordinate_frame('arm-01', tmp_path)
    if metadata_fault:
        assert frame == original
    else:
        restored = cb.load_bundle('arm-01', tmp_path)
        gp.validate_coordinate_frame(frame, restored)
        assert frame['binding']['daemon_session'] == 'reconnected'
        assert frame['root_origin_display_px'] == [0, 80]
        assert frame['calibration_run_id'] == 'run-original'
        assert restored['xy'] == bundle['xy']
