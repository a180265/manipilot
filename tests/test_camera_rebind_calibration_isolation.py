"""A different USB channel's registration must not expire this arm's calibration."""
from copy import deepcopy
import json

from drivers import calibration_bundle as cb
from runtime.calibration_gate import check_registered_calibration
from scripts import camera_frame_server as frame
from scripts import camera_binding_v2 as binding
from tests.test_camera_frame_server import request
from tests.test_camera_rebind_concurrency import capture, packet, upload  # noqa: F401
from tests.test_camera_usb_rebind import bridge, write  # noqa: F401
from tests.test_live_calibration_gate import ready_bundle


def test_other_channel_rebind_preserves_saved_calibration_with_new_frames(bridge, capture):
    session = capture()
    data, headers = packet(session)
    assert upload(bridge, session, data, headers)[0] == 200
    status, source, _ = request(bridge['base'], '/cameras/cam-01/latest.jpg')
    assert status == 200
    bundle = ready_bundle()
    bundle['cameraDeviceUniqueId'] = source['X-Camera-Unique-Id']
    bundle['bindingRevision'] = int(source['X-Binding-Revision'])
    bundle['bindingFingerprint'] = cb.binding_fingerprint(
        device_id='arm-01', camera_id='cam-01',
        camera_device_unique_id=bundle['cameraDeviceUniqueId'],
        daemon_addr=bundle['daemonAddr'], frame_width=64, frame_height=48)
    cb.save_bundle(bundle, bridge['root'])
    saved = cb.bundle_path('arm-01', bridge['root']).read_bytes()
    facts = {
        'device_id': 'arm-01', 'camera_id': 'cam-01',
        'camera_device_unique_id': bundle['cameraDeviceUniqueId'],
        'camera_binding_revision': bundle['bindingRevision'],
        'daemon_addr': bundle['daemonAddr'], 'daemon_session': bundle['daemonSession'],
        'expected_frame_width': 64, 'expected_frame_height': 48,
    }
    assert check_registered_calibration(facts, bundle) == []
    prior_binding = deepcopy(frame._binding_entry_for('cam-01'))
    global_revision = frame.BINDING_REVISION
    assert write(bridge, camera='cam-02', unique='other-usb', request_id='other-port')[0] == 200
    assert frame.BINDING_REVISION > global_revision
    assert frame._binding_entry_for('cam-01') == prior_binding
    # Prove fresh frames still pass, not just a cached frame from before the write.
    data, headers = packet(session, seq=2)
    assert upload(bridge, session, data, headers)[0] == 200
    status, current, _ = request(bridge['base'], '/cameras/cam-01/latest.jpg')
    assert status == 200
    facts['camera_binding_revision'] = int(current['X-Binding-Revision'])
    assert check_registered_calibration(facts, cb.load_bundle('arm-01', bridge['root'])) == []
    status, _, raw = request(bridge['base'], '/cameras/cam-01/status')
    assert status == 200
    facts['camera_binding_revision'] = json.loads(raw)['binding_revision']
    assert check_registered_calibration(facts, bundle) == []
    assert cb.bundle_path('arm-01', bridge['root']).read_bytes() == saved


def test_legacy_revision_is_frozen_before_other_channel_writes_and_restart(bridge):
    assert write(bridge, unique='usb-old', request_id='legacy-source')[0] == 200
    path = bridge['root'] / 'bindings.json'
    legacy = json.loads(path.read_text())
    legacy['revision'] = 7
    legacy['bindings'][0].pop('bindingRevision')
    path.write_text(json.dumps(legacy))

    def restart():
        frame.configure_state(bindings_path=path, devices_path=bridge['devices'],
            events_path=bridge['root'] / 'events.jsonl', data_root=bridge['root'],
            record_restart=False)

    def current_revision():
        status, _, raw = request(bridge['base'], '/cameras/cam-01/status')
        assert status == 200
        return json.loads(raw)['binding_revision']

    restart()
    assert current_revision() == 7
    old_identity = frame._binding_entry_for('cam-01')['cameraBindingId']
    for action in ('bind', 'renew', 'rename'):
        if action == 'rename':
            # Rename is a persistence API; no public HTTP rename route is exposed.
            binding.save_binding_v2(path, {
                'request_id': 'rename-other', 'action': 'rename',
                'camera_id': 'cam-02', 'display_name': '另一台摄像头',
            })
            frame._reload_bindings_view()
        else:
            status, result = write(bridge, camera='cam-02', unique='usb-other',
                request_id=action + '-other', action=action)
            assert status == 200, result
        stored = json.loads(path.read_text())
        assert stored['revision'] > 7
        entry = next(item for item in stored['bindings'] if item['cameraId'] == 'cam-01')
        assert entry['bindingRevision'] == 7 and entry['cameraBindingId'] == old_identity
        assert current_revision() == 7
        restart()
        assert current_revision() == 7
