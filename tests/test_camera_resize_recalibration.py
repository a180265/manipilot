"""A trusted replacement format is observable before manual recalibration."""
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb
from runtime.devices import DeviceManager
from scripts import motor_debug_server as debug
from tests.test_calibration_status_refresh import _manager
from tests.test_camera_rebind_concurrency import capture, packet, upload  # noqa: F401
from tests.test_camera_usb_rebind import bridge  # noqa: F401
from tests.test_motor_debug_multi_device import two_devices  # noqa: F401


def test_trusted_new_size_is_acquired_but_only_new_manual_bundle_becomes_valid(bridge, capture, monkeypatch):
    session = capture()
    data, headers = packet(session)
    assert upload(bridge, session, data, headers)[0] == 200
    manager, device, bundle = _manager(bridge['root'])
    cfg = {'device_id': 'arm-01', 'camera_id': 'cam-01', 'camera_mode': 'multi_channel',
           'expected_frame_width': 1280, 'expected_frame_height': 720,
           'camera_frame_url': bridge['base'] + '/cameras/cam-01/latest.jpg'}
    manager._registered_configs = [deepcopy(cfg)]
    device.update(camera_mode='multi_channel', expected_frame_width=1280, expected_frame_height=720)
    driver = DeviceManager._make_real_camera(cfg)
    manager._cameras['arm-01'] = driver
    # Decode through the production HttpFrameSource, preserving source/geometry checks.
    assert driver._frame_provider().shape[:2] == (48, 64)
    info = driver.last_source_info()
    assert (info['frame_width'], info['frame_height']) == (64, 48)
    bundle.update(cameraDeviceUniqueId=info['device_unique_id'], bindingRevision=info['binding_revision'])
    bundle['frame'] = {'width': 1280, 'height': 720}
    bundle['bindingFingerprint'] = cb.binding_fingerprint(
        device_id='arm-01', camera_id='cam-01', camera_device_unique_id=bundle['cameraDeviceUniqueId'],
        daemon_addr=bundle['daemonAddr'], frame_width=1280, frame_height=720)
    cb.save_bundle(bundle, bridge['root'])
    old = manager.get('arm-01')
    assert old['calibration_status'] == 'due'
    assert 'camera_resolution_changed' in {c['code'] for c in old['calibration_checks']}
    assert (old['expected_frame_width'], old['expected_frame_height']) == (64, 48)
    assert manager._registered_configs == [cfg]  # No capture format/configuration write.
    # The user's newly completed calibration records the current decoded dimensions.
    bundle['frame'] = {'width': 64, 'height': 48}
    bundle['bindingFingerprint'] = cb.binding_fingerprint(
        device_id='arm-01', camera_id='cam-01', camera_device_unique_id=bundle['cameraDeviceUniqueId'],
        daemon_addr=bundle['daemonAddr'], frame_width=64, frame_height=48)
    cb.save_bundle(bundle, bridge['root'])
    assert manager.get('arm-01')['calibration_status'] == 'validated'
    monkeypatch.setattr(debug, 'device_config', lambda _: cfg)
    monkeypatch.setattr(debug, 'camera_frame_url_for', lambda _: cfg['camera_frame_url'])
    assert debug.fetch_camera_frame('arm-01')[0] == data


@pytest.mark.parametrize('mode', ['legacy_single', None])
def test_legacy_and_explicit_direct_keep_configured_size_constraints(mode):
    driver = DeviceManager._make_real_camera({
        'device_id': 'arm-01', 'camera_id': 'cam-01', 'camera_mode': mode,
        'camera_source_mode': 'direct', 'camera_device_unique_id': 'fixed-usb',
        'expected_frame_width': 1280, 'expected_frame_height': 720,
        'camera_frame_url': 'http://127.0.0.1:8790/latest.jpg'})
    source = driver._frame_provider
    assert (source.expected_width, source.expected_height) == (1280, 720)
    assert source.expected_device_unique_id == 'fixed-usb'


@pytest.mark.parametrize('mode,trusted', [('multi_channel', True), ('multi_channel', False), ('legacy_single', True)])
def test_debug_new_context_uses_trusted_source_size_and_old_context_rejects_drift(two_devices, monkeypatch, mode, trusted):
    configs, _ = two_devices
    configs['arm-01']['camera_mode'] = mode
    camera = {'camera_id': 'cam-01', 'device_id': 'arm-01', 'device_unique_id': 'uvc-01',
              'binding_revision': 5, 'status': 'streaming', 'width': 640, 'height': 480}
    monkeypatch.setattr(debug, 'camera_status', lambda _: dict(camera))
    monkeypatch.setattr(debug, '_camera_v2_binding', lambda *_: {
        'enabled': trusted, 'cameraBindingId': 'binding-new',
        'sourceGeometryId': 'geometry-new', 'sourceGenerationId': 'generation-new'} if trusted else {'enabled': False})
    view = debug.create_device_context('arm-01')
    checks = {item['code'] for item in view['checks']}
    assert ('camera_resolution_changed' not in checks) == (mode == 'multi_channel' and trusted)
    facts = debug.calibration_binding_facts('arm-01')
    assert (facts['frameWidth'], facts['frameHeight']) == (640, 480)
    camera['width'] = 800
    with pytest.raises(RuntimeError, match='camera_resolution_changed'):
        debug.require_context('arm-01', view['contextId'])


def test_formal_v3_calibration_still_rejects_changed_dimensions_and_geometry():
    from tests.test_calibrated_scroll import v3_bundle
    from tests.test_calibration_bundle import _gate
    bundle = v3_bundle()
    checks = _gate(bundle, frame_width=bundle['frame']['width'] + 1,
                   camera_binding_id=bundle['cameraBindingId'], source_geometry_id='changed-geometry')
    codes = {item['code'] for item in checks}
    assert 'camera_resolution_changed' in codes
    assert any('geometry' in code for code in codes), checks
