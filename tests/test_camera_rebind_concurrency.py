"""生产帧路由的确定性换绑竞态；仅隔离相机/宿主证明与调度时点。

绑定走真实临时 HTTP，采集会话走生产 register_capture_session，JPEG/帧身份/
新鲜度/回执/取图均保留生产实现；没有相机、机械臂或生产服务访问。
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone

import pytest

from scripts import camera_frame_server as frame
from tests.test_camera_frame_server import jpeg, request
from tests.test_camera_usb_rebind import bridge, write  # noqa: F401


@pytest.fixture
def capture(bridge, monkeypatch):
    # 宿主二进制/时钟证明是外部 I/O；这些用例验证证明完成后的原子来源处理。
    monkeypatch.setattr(frame, '_session_proof_strict', lambda: True)
    monkeypatch.setattr(frame, '_verify_session_proof',
                        lambda facts, _clock: (facts['formatId'], facts['helperSha256']))

    def start(camera='cam-01', *, client='first', previous=None):
        entry = frame._binding_entry_for(camera)
        return frame.register_capture_session(
            camera, client_instance_id=f'{camera}-{client}',
            camera_binding_id=entry['cameraBindingId'], geometry_id='geometry-64x48',
            source_clock_id='clock-offline', replaces_generation_id=previous,
            pts_provenance=frame.TIMING_PROVENANCE, capture_uncertainty_ms=1,
            format_id='fourcc:2f323736;size:64x48;pixel-aspect:1/1',
            helper_sha256='a' * 64)

    assert write(bridge, unique='usb-old')[0] == 200
    return start


def packet(session, seq=1):
    data = jpeg((10, 30, 60))
    headers = {
        'Content-Type': 'image/jpeg',
        'X-Camera-Id': session['camera_id'],
        'X-Camera-Binding-Contract': 'camera-binding.v2',
        'X-Frame-Source': 'camera_helper',
        'X-Camera-Binding-Id': session['camera_binding_id'],
        'X-Source-Geometry-Id': session['source_geometry_id'],
        'X-Source-Generation-Id': session['source_generation_id'],
        'X-Source-Clock-Id': session['source_clock_id'],
        'X-Source-Frame-Seq': str(seq),
        'X-Source-Captured-Monotonic-Ns': str(time.monotonic_ns()),
        'X-Source-Capture-Uncertainty-Ms': '1',
        'X-Source-Timing-Provenance': frame.TIMING_PROVENANCE,
        'X-Captured-At': datetime.now(timezone.utc).isoformat(),
    }
    return data, headers


def upload(env, session, data, headers):
    status, _, raw = request(env['base'], f"/cameras/{session['camera_id']}/frame",
                            method='POST', body=data, headers=headers)
    return status, json.loads(raw)


def remap(env):
    pending = env['devices'].with_suffix('.pending')
    pending.write_text(json.dumps({'devices': [
        {'device_id': 'arm-01', 'camera_id': 'cam-03'},
        {'device_id': 'arm-02', 'camera_id': 'cam-02'},
    ]}))
    pending.replace(env['devices'])


@pytest.mark.parametrize('change', ['binding', 'mapping'])
def test_upload_rejects_source_changed_during_decode(bridge, capture, monkeypatch, change):
    session = capture()
    data, headers = packet(session)
    decode = frame._decode_jpeg
    changed = []

    def decode_after_change(payload):
        decoded = decode(payload)
        if change == 'binding':
            status, result = write(bridge, unique='usb-new', request_id='during-decode')
            assert status == 200, result
        else:
            remap(bridge)
        changed.append(change)
        return decoded

    monkeypatch.setattr(frame, '_decode_jpeg', decode_after_change)
    status, result = upload(bridge, session, data, headers)
    assert changed == [change]
    assert status == 409 and result['error']['code'] == 'source_binding_changed'
    assert frame.CHANNELS['cam-01']['frame'] == b''
    assert frame.current_capture_session('cam-01') is None
    assert (session['source_generation_id'], 1) not in frame._V2_FRAME_RECEIPTS


def test_mapping_change_after_validation_never_relabels_old_frame(bridge, capture, monkeypatch):
    session = capture()
    data, headers = packet(session)
    read_binding = frame._binding_entry_for
    reads = []

    def replace_mapping_after_final_read(camera):
        entry = read_binding(camera)
        reads.append(camera)
        if len(reads) == 2:
            # 第一次是请求入场，第二次是提交锁内身份重验；在此完成外部原子写。
            remap(bridge)
        return entry

    monkeypatch.setattr(frame, '_binding_entry_for', replace_mapping_after_final_read)
    status, result = upload(bridge, session, data, headers)
    assert len(reads) == 2
    assert status == 200, result
    assert result['device_id'] == 'arm-01'
    assert result['source_generation_id'] == session['source_generation_id']
    assert frame.CHANNELS['cam-01']['device_id'] == 'arm-01'
    # 下一次观察新映射即撤销先前来源，旧帧不能冒充新归属的实时帧。
    status, _, raw = request(bridge['base'], '/cameras/cam-01/latest.jpg')
    assert status == 503, raw
    assert frame.channel_device_id('cam-01') == 'cam-01'
    assert frame.CHANNELS['cam-01']['frame'] == b''
    assert (session['source_generation_id'], 1) not in frame._V2_FRAME_RECEIPTS


def test_session_replacement_preserves_other_channel_receipts(bridge, capture):
    assert write(bridge, camera='cam-02', unique='usb-other', request_id='bind-other')[0] == 200
    first, other = capture(), capture('cam-02')
    data1, headers1 = packet(first)
    data2, headers2 = packet(other)
    assert upload(bridge, first, data1, headers1)[0] == 200
    status, receipt = upload(bridge, other, data2, headers2)
    assert status == 200
    other_state = copy.deepcopy(frame.CHANNELS['cam-02'])
    new = capture(client='replacement', previous=first['source_generation_id'])
    assert new['source_generation_id'] != first['source_generation_id']
    assert (first['source_generation_id'], 1) not in frame._V2_FRAME_RECEIPTS
    assert frame._V2_FRAME_RECEIPTS[other['source_generation_id'], 1] == receipt
    assert frame.CHANNELS['cam-02'] == other_state
    # 重传仍必须给原回执，不能增序号、刷新首次接收时间或重新归档。
    assert upload(bridge, other, data2, headers2) == (200, receipt)
    assert frame.CHANNELS['cam-02'] == other_state
    status, rejected = upload(bridge, first, data1, headers1)
    assert status == 409 and rejected['error']['code'] == 'source_generation_rejected'


def test_read_returns_one_frame_identity_snapshot_during_rebind(bridge, capture, monkeypatch):
    session = capture()
    data, headers = packet(session)
    assert upload(bridge, session, data, headers)[0] == 200
    status_view = frame._channel_status
    scheduled = threading.Event()
    writer_result = []
    workers = []
    hooked = False

    def concurrent_write():
        # 非阻塞探测只决定调度：锁可取得时先完成写，锁被读快照持有时先让读继续。
        # 无 sleep/时间竞争，不替换任何绑定或取图生产逻辑。
        free = frame.FRAME_LOCK.acquire(blocking=False)
        if free:
            frame.FRAME_LOCK.release()
        else:
            scheduled.set()
        try:
            writer_result.append(write(bridge, unique='usb-new', request_id='during-read'))
        finally:
            scheduled.set()

    def status_with_writer(camera):
        nonlocal hooked
        value = status_view(camera)
        if not hooked:
            hooked = True
            worker = threading.Thread(target=concurrent_write)
            workers.append(worker)
            worker.start()
            assert scheduled.wait(3), '并发写没有到达预定调度点'
        return value

    monkeypatch.setattr(frame, '_channel_status', status_with_writer)
    try:
        status, response_headers, response_data = request(
            bridge['base'], '/cameras/cam-01/latest.jpg')
    finally:
        for worker in workers:
            worker.join(3)
            assert not worker.is_alive(), '并发写未结束'
    assert writer_result and writer_result[0][0] == 200, writer_result
    assert status == 200 and response_data == data
    assert response_headers['X-Camera-Unique-Id'] == 'usb-old'
    assert response_headers['X-Camera-Binding-Id'] == session['camera_binding_id']
    assert response_headers['X-Source-Generation-Id'] == session['source_generation_id']
    assert frame.BINDINGS['cam-01']['device_unique_id'] == 'usb-new'
    assert frame.CHANNELS['cam-01']['frame'] == b''
