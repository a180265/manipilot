"""USB 换口：采集独立、旧来源撤销、多通道隔离（临时 HTTP/文件，无硬件）。"""
from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

import pytest

from scripts import camera_binding_v2 as binding
from scripts import camera_frame_server as frame
from tests.test_camera_frame_server import request


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    devices = tmp_path / 'devices.json'
    devices.write_text(json.dumps({'devices': [
        {'device_id': 'arm-01', 'camera_id': 'cam-01'},
        {'device_id': 'arm-02', 'camera_id': 'cam-02'},
    ]}))
    frame.configure_state(bindings_path=tmp_path / 'bindings.json', devices_path=devices,
                          events_path=tmp_path / 'events.jsonl', data_root=tmp_path,
                          record_restart=False)
    token_file = tmp_path / 'token'
    token = binding.generate_control_token(token_file)
    monkeypatch.setattr(frame, 'CONTROL_TOKEN_FILE', token_file)
    monkeypatch.setattr(frame, 'V2_ENABLED', True)
    monkeypatch.setattr(frame, '_V2_FRAME_RECEIPTS', {})
    # 机械状态服务完全不存在，摄像头登记仍必须独立工作。
    for key in ('CAMERA_FLEET_SAFETY_BASE_URL', 'CAMERA_FLEET_SAFETY_KEYS_FILE',
                'ARMFLEET_WX_NAV_KEYS_FILE'):
        monkeypatch.delenv(key, raising=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), frame.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {'base': f'http://127.0.0.1:{server.server_port}', 'devices': devices,
           'root': tmp_path, 'auth': {'Authorization': f'Bearer {token}'}}
    yield env
    server.shutdown()
    server.server_close()
    thread.join(2)


def write(env, camera='cam-01', unique='usb-new', request_id='request-1', action='bind'):
    suffix = '' if action == 'bind' else '/' + action
    status, _, raw = request(env['base'], f'/cameras/{camera}/binding-v2{suffix}',
        method='PUT' if action == 'bind' else 'POST',
        body=json.dumps({'requestId': request_id, 'deviceUniqueId': unique,
                         'displayName': 'USB相机'}).encode(), headers=env['auth'])
    return status, json.loads(raw)


def test_camera_channels_exist_without_matching_arm(bridge):
    status, _, raw = request(bridge['base'], '/cameras')
    assert status == 200
    channels = {c['camera_id']: c for c in json.loads(raw)['cameras']}
    assert set(frame.DEFAULT_CAMERA_IDS) <= channels.keys()
    assert channels['cam-09']['device_id'] == 'cam-09'  # 未分配不虚构 arm-09
    assert write(bridge, camera='cam-09')[0] == 200


def test_binding_without_mechanical_status_clears_old_frames_only(bridge):
    assert write(bridge, unique='usb-old')[0] == 200
    assert write(bridge, camera='cam-02', unique='usb-other', request_id='other')[0] == 200
    for camera, generation in [('cam-01', 'old-1'), ('cam-02', 'other-2')]:
        frame.CHANNELS[camera].update(frame=b'old-frame', seq=12,
            v2_generation_id=generation, v2_session={'source_generation_id': generation},
            last_error={'code': 'camera_binding_mismatch'})
        frame._V2_FRAME_RECEIPTS[generation, 12] = {'jpeg_sha256': camera}
    other = copy.deepcopy(frame.CHANNELS['cam-02'])
    status, saved = write(bridge, request_id='usb-port-moved')
    assert status == 200, saved
    assert frame.BINDINGS['cam-01']['device_unique_id'] == 'usb-new'
    assert frame.CHANNELS['cam-01']['frame'] == b''
    assert frame.CHANNELS['cam-01'].get('v2_session') is None
    assert frame.CHANNELS['cam-01'].get('last_error') is None
    assert ('old-1', 12) not in frame._V2_FRAME_RECEIPTS
    assert frame.CHANNELS['cam-02'] == other
    assert frame._V2_FRAME_RECEIPTS['other-2', 12] == {'jpeg_sha256': 'cam-02'}


def test_replayed_binding_does_not_clear_new_capture(bridge):
    status, saved = write(bridge)
    assert status == 200, saved
    frame.CHANNELS['cam-01'].update(frame=b'new-frame', seq=7,
        v2_generation_id='fresh', v2_session={'source_generation_id': 'fresh'})
    before = copy.deepcopy(frame.CHANNELS['cam-01'])
    assert write(bridge) == (status, saved)
    assert frame.CHANNELS['cam-01'] == before


def test_unbind_clears_frame_and_session_without_mechanical_status(bridge):
    assert write(bridge)[0] == 200
    frame.CHANNELS['cam-01'].update(frame=b'old-frame', v2_generation_id='old',
                                  v2_session={'source_generation_id': 'old'})
    status, result = write(bridge, action='unbind', request_id='remove')
    assert status == 200, result
    assert frame.current_capture_session('cam-01') is None
    assert frame.CHANNELS['cam-01']['frame'] == b''
    assert frame.channel_generation('cam-01') is None


def test_active_motion_lock_still_blocks_binding(bridge):
    with binding.installation_lock(bridge['root'], 'arm-01', exclusive=False):
        status, result = write(bridge)
    assert status == 409 and result['error']['code'] == 'device_maintenance_busy'
    assert not (bridge['root'] / 'bindings.json').exists()


def test_duplicate_camera_and_unknown_channel_do_not_change_disk(bridge):
    assert write(bridge)[0] == 200
    before = (bridge['root'] / 'bindings.json').read_bytes()
    status, result = write(bridge, camera='cam-02', request_id='duplicate')
    assert status == 409 and result['error']['code'] == 'camera_unique_id_conflict'
    assert write(bridge, camera='cam-99', unique='unregistered', request_id='unknown')[0] == 404
    assert (bridge['root'] / 'bindings.json').read_bytes() == before


def test_parallel_channel_writes_do_not_lose_bindings(bridge):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: write(bridge, camera=f'cam-{i:02}',
            unique=f'usb-{i}', request_id=f'parallel-{i}'), range(1, 7)))
    assert all(status == 200 for status, _ in results), results
    stored = binding.load_bindings_v2(bridge['root'] / 'bindings.json')
    assert len(stored['bindings']) == 6 and stored['revision'] == 6


def test_mapping_reload_invalidates_only_changed_channels(bridge):
    assert write(bridge)[0] == 200
    for camera in ('cam-01', 'cam-02'):
        frame.CHANNELS[camera].update(frame=b'old', v2_generation_id=camera,
                                    v2_session={'source_generation_id': camera})
        frame._V2_FRAME_RECEIPTS[camera, 1] = {'jpeg_sha256': camera}
    other = copy.deepcopy(frame.CHANNELS['cam-02'])
    bridge['devices'].write_text(json.dumps({'devices': [
        {'device_id': 'arm-01', 'camera_id': 'cam-03'},
        {'device_id': 'arm-02', 'camera_id': 'cam-02'},
    ]}))
    assert frame.channel_device_id('cam-03') == 'arm-01'
    assert frame.channel_device_id('cam-01') == 'cam-01'
    assert frame.current_capture_session('cam-01') is None
    assert frame.CHANNELS['cam-01']['frame'] == b''
    assert frame.CHANNELS['cam-02'] == other
    assert ('cam-02', 1) in frame._V2_FRAME_RECEIPTS
