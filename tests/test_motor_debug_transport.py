from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts import motor_debug_server as debug


class FakeStream:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.writes = []
        self.closed = False

    def readline(self):
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply() if callable(reply) else reply

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeSocket:
    def __init__(self, *replies, welcome=b"arm-daemon3 ready device=arm-01\n"):
        self.stream = FakeStream([welcome, *replies])
        self.closed = False
        self.shutdown_called = False

    def makefile(self, _mode):
        return self.stream

    def recv(self, _size):
        try:
            return self.stream.readline()
        except StopIteration:
            raise socket.timeout()

    def settimeout(self, timeout):
        self.timeout = timeout

    def shutdown(self, _how):
        self.shutdown_called = True

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch, tmp_path):
    sockets = []
    opened = []

    def create_connection(address, timeout):
        assert sockets, f"Unexpected connection: {address}"
        sock = sockets.pop(0)
        opened.append((address, sock))
        return sock

    monkeypatch.setattr(debug.socket, "create_connection", create_connection)
    monkeypatch.setattr(debug.DaemonClient, "_lease_renew_loop", lambda self: None)
    monkeypatch.setattr(debug, "_runtime_device_records", lambda: {})
    monkeypatch.setattr(debug, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "DAEMONS", {})
    monkeypatch.setattr(debug, "CONTEXTS", {})
    monkeypatch.setattr(debug, "WIZARDS", {})
    monkeypatch.setattr(debug, "_STATUS_CACHES", {})
    monkeypatch.setattr(debug, "_STATUS_LOCKS", {})
    monkeypatch.setattr(debug, "DEVICE_CONFIGS", {
        "arm-01": {"device_id": "arm-01", "daemon_addr": "127.0.0.1:18895"},
    })
    monkeypatch.setattr(debug, "camera_status", lambda device: {"device_id": device})
    client = debug.daemon_for("arm-01")
    yield client, sockets, opened
    client.close()


def status_reply(**overrides):
    return json.dumps({
        "deviceId": "arm-01", "devicePresent": True, "connected": True,
        "session": "session-01", "estopLocked": False, **overrides,
    }).encode() + b"\n"


def test_idle_unleased_channel_is_replaced_before_claim_without_serial_reconnect(transport):
    client, sockets, opened = transport
    expired = FakeSocket(b"")
    next(expired.stream.replies)
    client.sock, client.f = expired, expired.stream
    fresh = FakeSocket(b'{"claimed": true, "lease_id": "new-lease"}\n')
    sockets.append(fresh)

    client.ensure_debug_lease()

    assert expired.closed and expired.stream.closed
    assert expired.stream.writes == []
    assert len(opened) == 1
    assert fresh.stream.writes == [b"CLAIM debug motor_debug_server\n"]
    assert client.lease_id == "new-lease"


@pytest.mark.parametrize("failure", [socket.timeout(), OSError("connection reset"), b""])
def test_motion_failure_closes_both_handles_and_never_replays(transport, failure):
    client, sockets, opened = transport
    broken = FakeSocket(failure)
    fresh = FakeSocket(b'{"released": true}\n')
    sockets.extend([broken, fresh])
    client.lease_id = "old-lease"
    with pytest.raises(debug.DaemonCommandError, match="结果未知") as error:
        client.cmd("XY 1 0 F500", timeout=0.01)
    assert broken.stream.writes == [b"XY 1 0 F500\n"]
    assert len(opened) == 1
    assert broken.closed and broken.stream.closed and broken.shutdown_called
    assert client.sock is None and client.f is None and client.lease_id is None
    if isinstance(failure, socket.timeout):
        code, payload = debug.api_error(error.value)
        assert code == 504
        assert payload["error"]["code"] == "daemon_command_timeout"
    client.cmd("RELEASE")
    assert fresh.stream.writes == [b"RELEASE\n"]


@pytest.mark.parametrize("welcome", [socket.timeout(), b"", b"bad\n",
                                    b"arm-daemon3 ready device=arm-02\n"])
def test_failed_handshake_closes_every_handle_without_sending(transport, welcome):
    client, sockets, _opened = transport
    sock = FakeSocket(welcome=welcome)
    sockets.append(sock)
    with pytest.raises(RuntimeError):
        client.cmd("TAP 1 F1500")
    assert sock.closed and sock.stream.closed
    assert sock.stream.writes == []
    assert client.sock is None


def test_status_and_emergency_channels_bypass_blocked_motion(transport):
    client, sockets, _opened = transport
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(3)
        return b'{"x": 1, "y": 0, "z": 0}\n'

    motion = FakeSocket(blocked)
    status = FakeSocket(status_reply())
    stop = FakeSocket(b'{"stopped": true}\n')
    estop = FakeSocket(b'{"estop_locked": true}\n')
    sockets.extend([motion, status, stop, estop])
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(client.cmd, "XY 1 0 F500")
        try:
            assert entered.wait(1)
            assert client.motion_active
            assert pool.submit(client.status).result(timeout=1)["session"] == "session-01"
            assert pool.submit(client.stop).result(timeout=1)["stopped"]
            assert pool.submit(client.daemon_estop, "test").result(timeout=1)["estop_locked"]
            assert not pending.done()
            assert not motion.closed
            assert all(s.closed and s.stream.closed for s in (status, stop, estop))
        finally:
            release.set()
        pending.result(timeout=1)
    assert motion.stream.writes == [b"XY 1 0 F500\n"]
    assert stop.stream.writes == [b"STOP\n"]
    assert estop.stream.writes == [b"ESTOP_LOCK test\n"]
    assert not client.motion_active


def test_motion_scope_serializes_other_ordinary_commands_and_nests(transport):
    client, sockets, _opened = transport
    first, release, attempted = threading.Event(), threading.Event(), threading.Event()
    sock = FakeSocket(b"{}\n", b"{}\n", b"{}\n")
    sockets.append(sock)

    def sequence():
        with client.motion_scope():
            client.cmd("Z 1 F1500")
            first.set()
            assert release.wait(3)
            with client.motion_scope():
                client.cmd("TAP 1 F1500")

    def other():
        attempted.set()
        return client.cmd("RELEASE")

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(sequence)
        try:
            assert first.wait(1)
            pending = pool.submit(other)
            assert attempted.wait(1)
            time.sleep(0.03)
            assert not pending.done()
        finally:
            release.set()
        running.result(timeout=1)
        pending.result(timeout=1)
    assert sock.stream.writes == [b"Z 1 F1500\n", b"TAP 1 F1500\n", b"RELEASE\n"]


def test_position_reads_serialize_without_reporting_motion(transport, monkeypatch):
    client, sockets, _opened = transport
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    position = b'{"x": 1, "y": 2, "z": -8, "state": "Idle"}\n'

    def blocked():
        entered.set()
        assert release.wait(3)
        return position

    sockets.extend([FakeSocket(blocked), FakeSocket(position)])
    monkeypatch.setattr(client, "status", lambda: {
        "connected": True, "session": "session-01", "device_id": "arm-01",
        "device_present": True, "estop_locked": False,
    })

    def calibration_read():
        attempted.set()
        return client.mpos()

    with ThreadPoolExecutor(max_workers=2) as pool:
        polling = pool.submit(client.mpos)
        try:
            assert entered.wait(1)
            assert not client.motion_active
            pending = pool.submit(calibration_read)
            assert attempted.wait(1)
            time.sleep(0.03)
            assert not pending.done()
            snapshot = debug.status_snapshot("arm-01")
            assert snapshot["status_stale"] and snapshot["mpos"] is None
            assert not client.motion_active
        finally:
            release.set()
        assert polling.result(timeout=1)["state"] == "Idle"
        assert pending.result(timeout=1)["state"] == "Idle"
    assert not sockets
    assert client.motion_revision == 0


def test_position_read_inside_motion_scope_and_busy_poll(transport):
    client, sockets, _opened = transport
    entered, release = threading.Event(), threading.Event()
    sockets.append(FakeSocket(b'{"x": 1, "y": 2, "z": -8, "state": "Idle"}\n'))

    def movement():
        with client.motion_scope():
            position = client.mpos()
            entered.set()
            assert release.wait(3)
            return position

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(movement)
        try:
            assert entered.wait(1)
            assert client.motion_active
            with pytest.raises(debug.DaemonCommandError, match="当前位置暂不可确认"):
                client.mpos(wait=False)
        finally:
            release.set()
        assert pending.result(timeout=1)["state"] == "Idle"
    assert not client.motion_active


def test_position_read_recovers_after_long_contention_without_replaying(transport):
    client, sockets, opened = transport
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    position = FakeSocket(b'{"x": 7, "y": 8, "z": -8, "state": "Idle"}\n')
    sockets.append(position)

    def hold():
        with client._motion_state_lock:
            entered.set()
            assert release.wait(3)

    def read():
        attempted.set()
        return client.mpos()

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(hold)
        try:
            assert entered.wait(1)
            pending = pool.submit(read)
            assert attempted.wait(1)
            time.sleep(0.35)
            assert not pending.done()
            with pytest.raises(debug.DaemonCommandError) as busy:
                client.mpos(wait=False)
            assert busy.value.code == "status_read_busy"
            assert not opened
        finally:
            release.set()
        held.result(timeout=1)
        assert pending.result(timeout=1)["x"] == 7
    assert position.stream.writes == [b"MPOS\n"]
    assert len(opened) == 1
    assert client.sock is None and client.motion_revision == 0


@pytest.mark.parametrize("cancelled", [False, True])
def test_position_wait_is_bounded_and_cancellable_without_sending(transport, cancelled):
    client, _sockets, opened = transport
    entered, release = threading.Event(), threading.Event()
    cancel = threading.Event()

    def hold():
        with client._motion_state_lock:
            entered.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(hold)
        try:
            assert entered.wait(1)
            started = time.monotonic()
            pending = pool.submit(client.mpos, timeout=0.15 if not cancelled else 2.0,
                                  cancel_event=cancel)
            if cancelled:
                time.sleep(0.05)
                assert not pending.done()
                cancel.set()
            with pytest.raises(debug.DaemonCommandError) as error:
                pending.result(timeout=0.8)
            assert error.value.code == (
                "status_read_cancelled" if cancelled else "status_read_busy")
            assert time.monotonic() - started < 0.8
            assert not opened
        finally:
            release.set()
        held.result(timeout=1)


def test_position_response_timeout_does_not_retry_or_close_motion_channel(transport):
    client, sockets, opened = transport
    ordinary = FakeSocket()
    client.sock, client.f, client.lease_id = ordinary, ordinary.stream, "lease-01"
    failing = FakeSocket(socket.timeout())
    sockets.append(failing)

    with pytest.raises(debug.DaemonCommandError) as error:
        client.mpos()

    assert error.value.code == "daemon_command_timeout"
    assert len(opened) == 1 and failing.stream.writes == [b"MPOS\n"]
    assert failing.closed and not ordinary.closed
    assert client.sock is ordinary and client.lease_id == "lease-01"


def test_resume_uses_lease_channel_and_does_not_use_emergency_channel(transport, monkeypatch):
    client, sockets, _opened = transport
    ordinary = FakeSocket(b'{"lease_id": "lease-01"}\n', b'{"resumed": true}\n')
    sockets.append(ordinary)
    monkeypatch.setattr(client, "status", lambda: {
        "connected": True, "session": "session-01", "estop_locked": False,
    })
    assert client.resume()["resumed"]
    assert ordinary.stream.writes == [b"CLAIM debug motor_debug_server\n", b"RESUME\n"]


def test_resume_rejects_estop_without_sending(transport, monkeypatch):
    client, _sockets, opened = transport
    monkeypatch.setattr(client, "status", lambda: {
        "connected": True, "session": "session-01", "estop_locked": True,
    })
    with pytest.raises(RuntimeError, match="急停"):
        client.resume()
    assert not opened


def test_existing_lease_is_renewed_even_when_near_expiry(transport):
    client, sockets, _opened = transport
    client.lease_id = "lease-01"
    client.lease_expires = time.time() + 5
    renew = FakeSocket(b'{"renewed": true, "lease_id": "lease-01"}\n')
    next(renew.stream.replies)  # The existing lease belongs to an open connection.
    client.sock, client.f = renew, renew.stream

    client.ensure_debug_lease()

    assert renew.stream.writes == [b"RENEW\n"]
    assert client.lease_id == "lease-01"


def test_status_honors_daemon_connected_flag(transport):
    client, sockets, _opened = transport
    sockets.append(FakeSocket(status_reply(connected=False)))

    result = client.status()

    assert result["connected"] is False
    assert result["device_present"] is True


def test_known_disconnected_device_is_not_marked_status_stale(transport):
    client, sockets, _opened = transport
    sockets.append(FakeSocket(status_reply(connected=False, session=None)))

    result = debug.status_snapshot("arm-01")

    assert result["daemon"]["connected"] is False
    assert result["daemon"]["device_present"] is True
    assert result["daemon"].get("status_stale") is not True
    assert result.get("status_stale") is not True
    assert result["mpos"] is None


def test_status_snapshot_invalidates_mpos_when_status_changes(transport, monkeypatch):
    client, _sockets, _opened = transport
    first = {
        "connected": True, "session": "session-01",
        "device_id": "arm-01", "device_present": True,
        "daemon_addr": "127.0.0.1:18895", "estop_locked": False,
    }
    second = {**first, "session": "session-02"}
    statuses = iter([first, second])
    monkeypatch.setattr(client, "status", lambda: next(statuses))
    monkeypatch.setattr(client, "mpos", lambda **kwargs: {"x": 1, "y": 2, "z": 3})

    result = debug.status_snapshot("arm-01")

    assert result["status_stale"] is True
    assert result["mpos"] is None
    assert result["daemon"]["session"] == "session-02"
    assert result["usable"] is False


def test_cached_snapshot_is_not_usable_or_current(transport):
    client, _sockets, _opened = transport
    original = {
        "daemon": {"connected": True, "session": "session-01"},
        "mpos": {"x": 1, "y": 2, "z": 3}, "anchorMatch": True, "usable": True,
        "runtimeCalibration": {"usable": True},
    }
    debug._STATUS_CACHES["arm-01"] = original
    lock = debug._STATUS_LOCKS.setdefault("arm-01", threading.Lock())
    with lock:
        result = debug.status_snapshot("arm-01")
    assert result["status_stale"] and result["daemon"]["status_stale"]
    assert not result["usable"] and not result["anchorMatch"]
    assert not result["runtimeCalibration"]["usable"]
    assert result["mpos"] is None
    assert original["usable"] and original["runtimeCalibration"]["usable"]


@pytest.mark.parametrize("failure", [socket.timeout(), OSError("connection refused")])
def test_failed_status_clears_session_and_cannot_make_mapping_usable(transport, failure):
    client, sockets, _opened = transport
    client.session = "session-01"
    sockets.append(FakeSocket(failure))
    result = debug.status_snapshot("arm-01")
    assert not result["usable"] and not result["anchorMatch"]
    assert result["status_stale"]
    assert client.session is None
    assert not sockets


def test_daemon_rebinding_retires_cached_client_and_invalidates_derived_cache(transport):
    old, _sockets, _opened = transport
    debug._STATUS_CACHES["arm-01"] = {"usable": True}
    debug.WIZARDS["arm-01"] = object()
    debug.DEVICE_CONFIGS["arm-01"]["daemon_addr"] = "127.0.0.1:18896"
    new = debug.daemon_for("arm-01")
    assert new is not old
    assert new.daemon_addr == ("127.0.0.1", 18896)
    assert "arm-01" not in debug._STATUS_CACHES
    assert "arm-01" not in debug.WIZARDS
    with pytest.raises(RuntimeError, match="绑定"):
        old.cmd("TAP 1 F1500")


def test_control_context_does_not_query_camera_or_status_but_rejects_rebinding(
        transport, monkeypatch):
    client, _sockets, _opened = transport
    context = {
        "device_id": "arm-01", "expires_at": time.time() + 60,
        "daemon_addr": "127.0.0.1:18895",
    }
    debug.CONTEXTS["ctx"] = context
    monkeypatch.setattr(debug, "camera_status", lambda device: pytest.fail("camera gate"))
    monkeypatch.setattr(client, "status", lambda: pytest.fail("status gate"))
    assert debug.require_context("arm-01", "ctx", emergency=True)["device_id"] == "arm-01"
    debug.DEVICE_CONFIGS["arm-01"]["daemon_addr"] = "127.0.0.1:18896"
    with pytest.raises(RuntimeError, match="device_context_mismatch"):
        debug.require_context("arm-01", "ctx", emergency=True)


def legacy_calibration(session="session-old"):
    return {
        "session": session,
        "affine": {"a": 1, "b": 0, "c": 99, "d": 0, "e": 1, "f": 99},
        "samples": [
            {"cameraX": x, "cameraY": y, "robotX": x, "robotY": y, "robotZ": 0,
             "session": session}
            for x, y in ((0, 0), (1, 0), (0, 1))
        ],
    }


def bundle_calibration(session="session-01"):
    return {
        "deviceId": "arm-01", "daemonSession": session, "status": "ready",
        "cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-01",
        "bindingRevision": 1,
        "frame": {"width": 1280, "height": 720},
        "screen": {"screenWidth": 1280, "screenHeight": 720,
                   "edgeModel": {"corners": [[0, 0], [1280, 0], [1280, 720], [0, 720]],
                                 "tolerancePx": 8}},
        "camera": {"cameraToScreen": {"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}},
        "xy": {"robotToScreen": {"a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0}},
    }


def test_stale_legacy_calibration_file_is_ignored(transport, monkeypatch):
    # 旧版仿射档案（calibration.json）已删除：即使文件残留在盘上，
    # 映射解析也只认统一 bundle，不受旧档案会话/内容影响。
    client, _sockets, _opened = transport
    monkeypatch.setattr(client, "status", lambda: {
        "connected": True, "session": "session-01",
    })
    debug._save_json(
        debug.DATA_DIR / "devices" / "arm-01" / "calibration.json",
        legacy_calibration())
    monkeypatch.setattr(debug.calibration_bundle, "load_bundle",
                        lambda *_args: bundle_calibration())
    state = debug.calibration_state("arm-01")
    assert state["usable"] and state["anchorMatch"]
    assert "calibration" not in state
    assert debug.camera_to_robot("arm-01", 640, 360) == pytest.approx((640, 360))


@pytest.mark.parametrize("current,bound", [
    (None, "session-01"), ("session-01", None), ("session-02", "session-01"),
])
def test_bundle_mapping_requires_confirmed_matching_session(transport, monkeypatch, current, bound):
    client, _sockets, _opened = transport
    monkeypatch.setattr(client, "status", lambda: {
        "connected": bool(current), "session": current,
    })
    monkeypatch.setattr(debug.calibration_bundle, "load_bundle",
                        lambda *_args: bundle_calibration(bound))
    with pytest.raises(RuntimeError, match="锚点|确认"):
        debug.camera_to_robot("arm-01", 2, 3)


def test_valid_bundle_mapping_still_works(transport, monkeypatch):
    client, _sockets, _opened = transport
    monkeypatch.setattr(client, "status", lambda: {
        "connected": True, "session": "session-01",
    })
    monkeypatch.setattr(debug.calibration_bundle, "load_bundle",
                        lambda *_args: bundle_calibration())
    # 恒等映射下用屏幕中心点验证：边缘内缩带（校准不确定量）内的点
    # 会被点解析契约正当拒绝，那是另一条测试的职责。
    assert debug.camera_to_robot("arm-01", 640, 360) == pytest.approx((640, 360))


@pytest.mark.parametrize("state", ["draft", "invalidated", None, "ready"])
@pytest.mark.parametrize("legacy", [False, True])
def test_status_requires_ready_bundle_without_changing_calibration(
        transport, monkeypatch, state, legacy):
    client, _sockets, _opened = transport
    bundle = {**bundle_calibration(), "status": state}
    bundle_path = debug.DATA_DIR / "devices" / "arm-01" / "calibration_bundle.json"
    debug._save_json(bundle_path, bundle)
    before = bundle_path.read_bytes()
    if legacy:
        debug._save_json(
            debug.DATA_DIR / "devices" / "arm-01" / "calibration.json",
            legacy_calibration("session-01"))
    monkeypatch.setattr(client, "status", lambda: {
        "connected": True, "session": "session-01",
        "device_id": "arm-01", "device_present": True,
    })
    monkeypatch.setattr(client, "mpos", lambda **kwargs: {
        "x": 1, "y": 2, "z": -8, "state": "Idle",
    })

    result = debug.status_snapshot("arm-01")

    assert result["anchorMatch"] is True
    assert result["usable"] is (state == "ready")
    assert result["runtimeCalibration"]["usable"] is (state == "ready")
    assert result["mpos"]["z"] == -8
    assert bundle_path.read_bytes() == before


def test_position_read_deadline_includes_handshake(transport):
    client, sockets, opened = transport
    welcome = b"arm-daemon3 ready device=arm-01\n"

    def slow():
        time.sleep(0.04)
        return welcome

    sock = FakeSocket(welcome=slow)
    sockets.append(sock)
    with pytest.raises(debug.DaemonCommandError) as error:
        client.mpos(timeout=0.02)
    assert error.value.code == "daemon_command_timeout"
    assert sock.stream.writes == []
    assert sock.closed and len(opened) == 1


def test_position_read_cancels_during_response_without_replay(transport):
    client, sockets, opened = transport
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()

    def response():
        entered.set()
        assert release.wait(1)
        return b'{"x": 999, "y": 2, "z": -8}\n'

    sock = FakeSocket(response)
    sockets.append(sock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.mpos, cancel_event=cancel)
        try:
            assert entered.wait(1)
            cancel.set()
        finally:
            release.set()
        with pytest.raises(debug.DaemonCommandError) as error:
            pending.result(timeout=1)
    assert error.value.code == "status_read_cancelled"
    assert sock.stream.writes == [b"MPOS\n"]
    assert sock.closed and len(opened) == 1


@pytest.mark.parametrize("failure", [OSError("connection reset"), b"",
                                    b'ERROR: device unavailable\n'])
def test_failed_position_read_never_reconnects_or_uses_cached_position(transport, failure):
    client, sockets, opened = transport
    sock = FakeSocket(failure)
    sockets.append(sock)
    debug._STATUS_CACHES["arm-01"] = {"mpos": {"x": 123, "y": 456, "z": -8}}

    with pytest.raises(debug.DaemonCommandError) as error:
        client.mpos()

    assert error.value.code == "daemon_unreachable"
    assert sock.closed and len(opened) == 1
    assert sock.stream.writes == [b"MPOS\n"]
