"""arm_daemon3 physical-session lifecycle guards."""
from __future__ import annotations

import threading
import json
import socket
import time

import pytest
import serial

import scripts.arm_daemon3 as daemon_module
from scripts.arm_daemon3 import ArmHolder, ArmSession, LeaseManager


class _OpenableSerial:
    instances = []
    open_failures = []
    flush_failure = None
    dtr_failure = None
    rts_failure = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.closed = False
        self.is_open = False
        self.writes = []
        type(self).instances.append(self)

    def open(self):
        failure = type(self).open_failures.pop(0) if type(self).open_failures else None
        if failure is not None:
            raise failure
        self.is_open = True

    def close(self):
        self.closed = True
        self.is_open = False

    def setDTR(self, _value):
        if type(self).dtr_failure is not None:
            raise type(self).dtr_failure
        return None

    def setRTS(self, _value):
        if type(self).rts_failure is not None:
            raise type(self).rts_failure
        return None

    def reset_input_buffer(self):
        if type(self).flush_failure is not None:
            raise type(self).flush_failure
        return None


class _FakeArm:
    def __init__(self):
        self.session = "test-session"
        self.closed = False
        self.serial = _FakeSerial()

    def close(self):
        self.closed = True


class _FakeSerial:
    def __init__(self):
        self.writes: list[bytes] = []
        self.timeout = 0.6
        self.pending = b""

    def write(self, data: bytes):
        self.writes.append(data)
        return len(data)

    def read(self, size=1):
        if not self.pending:
            self.pending = self.readline()
        result, self.pending = self.pending[:size], self.pending[size:]
        return result


class _SilentSerial:
    timeout = 0.6

    def write(self, _data: bytes):
        return 0

    def read(self, _size=1):
        return b""


class _FakeConn:
    def __init__(self, reads):
        self.reads = iter(reads)
        self.sent = []
        self.closed = False
        self.timeout = None
        self.stream = self

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, _mode):
        return self

    def readline(self, _size=-1):
        value = next(self.reads)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True


class _NoopSlot:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


def test_client_thread_start_failure_closes_socket_and_releases_slot(monkeypatch):
    def fail_start(_thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    conn, slot = _FakeConn([b""]), _NoopSlot()
    assert not daemon_module.start_client_thread(conn, 1, slot)
    assert conn.closed
    assert slot.released == 1


def test_disconnect_watcher_start_failure_cleans_up(holder, monkeypatch):
    def fail_start(_thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    conn, slot = _FakeConn([b""]), _NoopSlot()
    daemon_module.serve_client(conn, 1, slot)
    assert conn.closed
    assert slot.released == 1
    assert not holder.active_commands


def test_client_cancel_stops_active_motion_and_invalidates_generation(holder):
    holder.leases.claim(1, "debug", "test")
    event = holder.begin_client_command(1, motion=True)

    result = holder.cancel_client(1)

    assert result["stopped"] is True
    assert event.is_set()
    assert holder.estop_locked
    assert holder.stop_generation == 1
    assert holder.arm.serial.writes == [b"!\n"]
    holder.cancel_client(1)
    assert holder.arm.serial.writes == [b"!\n"]


def test_cancel_readonly_or_nonowner_never_stops_other_motion(holder):
    holder.leases.claim(1, "business", "test")
    for conn_id, motion in [(2, False), (2, True), (1, False)]:
        event = holder.begin_client_command(conn_id, motion=motion)
        assert holder.cancel_client(conn_id)["stopped"] is False
        assert event.is_set()
        holder.end_client_command(conn_id, event)
    assert holder.arm.serial.writes == []
    assert not holder.estop_locked


def test_reset_estop_does_not_apply_after_request_disconnect(holder):
    holder.estop_locked = True
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(RuntimeError, match="请求已取消"):
        daemon_module.handle("RESET_ESTOP operator", 1, cancel_event=cancelled)

    assert holder.estop_locked is True


def test_client_slots_reserve_capacity_for_emergency_connections():
    normal_slots = threading.BoundedSemaphore(daemon_module.MAX_CLIENTS)
    emergency_slots = threading.BoundedSemaphore(
        daemon_module.EMERGENCY_RESERVED_CLIENTS)

    acquired = [
        daemon_module.acquire_client_slot(normal_slots, emergency_slots)
        for _ in range(daemon_module.MAX_CLIENTS + daemon_module.EMERGENCY_RESERVED_CLIENTS)
    ]

    assert all(acquired)
    assert acquired[-1] is emergency_slots
    assert daemon_module.acquire_client_slot(normal_slots, emergency_slots) is None

    for slot in acquired:
        slot.release()


def test_reset_estop_busy_request_never_unlocks_later(holder):
    holder.estop_locked = True
    holder.cmd_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="急停未复位"):
            daemon_module.handle("RESET_ESTOP operator", 1)
    finally:
        holder.cmd_lock.release()
    assert holder.estop_locked


def test_partial_stop_write_is_not_reported_as_stopped(holder):
    holder.arm.serial.write = lambda _data: 0
    result = json.loads(daemon_module.handle("ESTOP_LOCK test", 1))
    assert result["estop_locked"]
    assert not result["stopped"]
    assert result["outcome_hint"] == "unknown"


def test_tcp_disconnect_stops_tap_before_remaining_serial_commands(holder):
    arm = object.__new__(ArmSession)
    arm.session = "test-session"
    arm.serial = _FakeSerial()
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original_write = arm.serial.write

    def write(data):
        result = original_write(data)
        if data == b"!\n":
            stopped.set()
        return result

    def readline():
        entered.set()
        assert release.wait(3)
        return b"ok\n"

    arm.serial.write = write
    arm.serial.readline = readline
    holder.arm = arm
    holder.leases.claim(1, "debug", "test")
    client, server = socket.socketpair()
    client.settimeout(1)
    worker = threading.Thread(target=daemon_module.serve_client, args=(server, 1))
    worker.start()
    try:
        assert client.recv(1024).startswith(b"arm-daemon3 ready")
        client.sendall(b"TAP 1\n")
        assert entered.wait(1)
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        assert stopped.wait(1)
        assert holder.estop_locked
    finally:
        release.set()
        client.close()
        worker.join(3)
    assert not worker.is_alive()
    assert arm.serial.writes == [b"G91\n", b"!\n"]
    assert holder.leases.snapshot() is None
    assert not holder.active_commands


@pytest.mark.parametrize("command", ["STOP", "ESTOP_LOCK test"])
def test_reserved_connection_accepts_emergency_when_normal_pool_full(holder, command):
    normal = threading.BoundedSemaphore(1)
    reserved = threading.BoundedSemaphore(1)
    assert normal.acquire(blocking=False)
    slot = daemon_module.acquire_client_slot(normal, reserved)
    assert slot is reserved
    client, server = socket.socketpair()
    client.settimeout(1)
    worker = threading.Thread(
        target=daemon_module.serve_client, args=(server, 1, slot),
        kwargs={"emergency_only": True})
    worker.start()
    stream = client.makefile("rb")
    try:
        assert stream.readline().startswith(b"arm-daemon3 ready")
        client.sendall(b"STATUS\n")
        assert json.loads(stream.readline())["deviceId"] == daemon_module.DEVICE_ID
        client.sendall((command + "\n").encode())
        assert json.loads(stream.readline())["stopped"]
    finally:
        client.shutdown(socket.SHUT_RDWR)
        stream.close()
        client.close()
        worker.join(2)
        normal.release()
    assert not worker.is_alive()
    assert reserved.acquire(blocking=False)
    reserved.release()


@pytest.mark.parametrize("command", ["CLAIM debug test", "RESET_ESTOP test", "MPOS", "CONNECT"])
def test_reserved_connection_rejects_ordinary_or_unlock_commands(holder, command):
    holder.estop_locked = True
    conn = _FakeConn([(command + "\n").encode()])
    daemon_module.serve_client(conn, 1, emergency_only=True)
    assert conn.sent[-1].startswith(b"ERROR:")
    assert holder.estop_locked
    assert not holder.arm.serial.writes


def test_reserved_idle_connection_expires_quickly(holder, monkeypatch):
    monkeypatch.setattr(daemon_module, "EMERGENCY_CLIENT_TIMEOUT_S", 0.05)
    client, server = socket.socketpair()
    client.settimeout(1)
    worker = threading.Thread(target=daemon_module.serve_client, args=(server, 1),
                              kwargs={"emergency_only": True})
    worker.start()
    try:
        assert client.recv(1024).startswith(b"arm-daemon3 ready")
        assert client.recv(1024) == b""
    finally:
        client.close()
        worker.join(2)
    assert not worker.is_alive()


def test_physical_session_invalidation_releases_lease_for_new_connection():
    leases = LeaseManager(ttl=3600)
    leases.claim(11, "business", "wx-01", "task-01")
    leases.invalidate_all("串口设备已拔出")
    assert leases.snapshot_locked() is None
    replacement = leases.claim(12, "debug", "console-01")
    assert replacement["owner_id"] == "console-01"


def test_drop_clears_lease_and_transactions_at_session_boundary():
    holder = object.__new__(ArmHolder)
    holder.arm = _FakeArm()
    holder.lock = threading.Lock()
    holder.cmd_lock = threading.Lock()
    holder.transactions = {11: "tx-01"}
    holder.leases = LeaseManager(ttl=3600)
    holder.leases.claim(11, "business", "wx-01", "task-01")

    arm = holder.arm
    holder._drop(arm, reason="串口设备已拔出")

    assert holder.arm is None
    assert arm.closed is True
    assert holder.transactions == {}
    assert holder.leases.snapshot_locked() is None


def test_unresponsive_position_query_drops_physical_session(monkeypatch):
    arm = object.__new__(ArmSession)
    arm.serial = _SilentSerial()
    monkeypatch.setattr(daemon_module, "MPOS_RESPONSE_TIMEOUT_S", 0.01)

    holder = object.__new__(ArmHolder)
    holder.arm = arm
    holder.lock = threading.Lock()
    holder.cmd_lock = threading.Lock()
    holder.transactions = {}
    holder.leases = LeaseManager(ttl=3600)
    holder.estop_locked = False

    with pytest.raises(RuntimeError, match="串口异常，会话已重置"):
        holder.run(lambda active: active.mpos())

    assert holder.arm is None


def test_stop_bypasses_blocked_normal_command_lock():
    holder = object.__new__(ArmHolder)
    holder.arm = _FakeArm()
    holder.lock = threading.Lock()
    holder.cmd_lock = threading.Lock()
    holder.transactions = {}
    holder.leases = LeaseManager(ttl=3600)
    holder.estop_locked = False

    holder.cmd_lock.acquire()
    try:
        result = holder.stop_now()
    finally:
        holder.cmd_lock.release()

    assert result == {"stopped": True, "connected": True}
    assert holder.arm.serial.writes == [b"!\n"]


@pytest.fixture
def holder(monkeypatch):
    monkeypatch.setattr(ArmHolder, "_watch", lambda self: None)
    value = ArmHolder()
    value.arm = _FakeArm()
    monkeypatch.setattr(daemon_module, "HOLDER", value)
    return value


@pytest.mark.parametrize("command", [
    "Z nan", "XY nan 0", "XY 0 inf", "TAP nan", "TAP -1",
    "TAP 1 Pnan", "TAP 1 Pinf", "TAP 1 P-1", "TAP 1 P6",
])
def test_invalid_motion_rejected_before_serial_write(holder, command):
    holder.leases.claim(1, "debug", "test")
    with pytest.raises(ValueError):
        daemon_module.handle(command, 1)
    assert holder.arm.serial.writes == []


@pytest.mark.parametrize("command", [
    "TAP2 nan", "TAP2 -1", "TAP2 11", "TAP2 1 F29",
    "TAP2 1 I0", "TAP2 1 I0.501", "TAP2 1 Inan", "TAP2 1 Iinf",
    "TAP2 1 P6", "TAP2 1 I0.1 I0.2",
])
def test_invalid_double_tap_rejected_before_serial_write(holder, command):
    holder.leases.claim(1, "debug", "test")
    with pytest.raises(ValueError):
        daemon_module.handle(command, 1)
    assert holder.arm.serial.writes == []


@pytest.mark.parametrize("command,expected", [
    ("TAP 1 F1500 P0.05", [
        "G91", "G1 Z1.000 F1500", "G4 P0.05", "G1 Z-1.000 F1500", "G90",
    ]),
    ("TAP2 1 F1500 P0.05 I0.12", [
        "G91", "G1 Z1.000 F1500", "G4 P0.05", "G1 Z-1.000 F1500",
        "G4 P0.12", "G1 Z1.000 F1500", "G4 P0.05", "G1 Z-1.000 F1500", "G90",
    ]),
    ("TAP2 3 F3000 I0.05", [
        "G91", "G1 Z3.000 F3000", "G1 Z-3.000 F3000",
        "G4 P0.05", "G1 Z3.000 F3000", "G1 Z-3.000 F3000", "G90",
    ]),
])
def test_tap_macro_orders_controller_commands_and_waits_once(holder, command, expected):
    calls = []
    holder.arm.send = lambda cmd: calls.append(cmd)
    holder.arm.idle = lambda: calls.append("IDLE") or {"x": 0, "y": 0, "z": 2}
    holder.leases.claim(1, "debug", "test")
    result = json.loads(daemon_module.handle(command, 1))
    assert calls == expected + ["IDLE"]
    assert result["z"] == 2


@pytest.mark.parametrize("command", [
    "TAP 3 F3000", "Z 3 F3000", "SWIPE 10 0 3 F1500 Z3000",
    "TAP2 3 F3001", "TAP2 3 Fnan", "TAP2 3 Finf",
])
def test_fast_feed_is_rejected_outside_double_tap_envelope(holder, command):
    holder.leases.claim(1, "debug", "test")
    with pytest.raises(ValueError):
        daemon_module.handle(command, 1)
    assert holder.arm.serial.writes == []


def test_double_tap_stop_does_not_send_second_press(holder):
    arm = object.__new__(ArmSession)
    arm.serial = _FakeSerial()
    holder.arm = arm
    holder.leases.claim(1, "debug", "test")
    reads = 0

    def readline():
        nonlocal reads
        reads += 1
        if reads == 3:
            holder.stop_now()
        return b"ok\n"

    arm.serial.readline = readline
    with pytest.raises(RuntimeError, match="停止"):
        daemon_module.handle("TAP2 1 I0.12", 1)
    assert arm.serial.writes.count(b"G1 Z1.000 F1500\n") == 1
    assert not any(b"G4" in line for line in arm.serial.writes)


def test_double_tap_short_lift_preserves_contact_and_final_hover(holder):
    calls = []
    holder.arm.send = lambda cmd: calls.append(cmd)
    holder.arm.idle = lambda: calls.append("IDLE") or {"x": 0, "y": 0, "z": -8}
    holder.leases.claim(1, "debug", "test")
    daemon_module.handle("TAP2 8 F1500 I0.12 R1", 1)
    assert calls == ["G91", "G1 Z8.000 F1500", "G1 Z-1.000 F1500",
                     "G4 P0.12", "G1 Z1.000 F1500", "G1 Z-8.000 F1500", "G90", "IDLE"]
    assert "tap2.short-lift.v1" in holder.status()["motionFeatures"]

@pytest.mark.parametrize("command", [
    "TAP2 8 F1500 I0.12 R1", "SWIPE 20 0 8 F2000 Z1500",
])
def test_partial_macro_failure_stops_and_latches_without_recovery(holder, command):
    calls = []

    def send(line):
        calls.append(line)
        if len(calls) == 3:
            raise RuntimeError("controller rejected macro segment")

    holder.arm.send = send
    holder.arm.idle = lambda: pytest.fail("failed macro must not continue")
    holder.leases.claim(1, "debug", "test")
    with pytest.raises(RuntimeError, match="controller rejected"):
        daemon_module.handle(command, 1)
    assert len(calls) == 3
    assert holder.arm.serial.writes == [b"!\n"]
    assert holder.estop_locked
    with pytest.raises(ValueError, match="急停"):
        daemon_module.handle("TAP 1 F1500", 1)
    assert len(calls) == 3


@pytest.mark.parametrize("command", ["TAP2 8 R0", "TAP2 8 Rnan", "TAP2 8 Rinf",
                                     "TAP2 0.5 R1", "TAP2 8 R2", "TAP 8 R1",
                                     "TAP2 8 R1 R1"])
def test_invalid_short_lift_does_not_write_serial(holder, command):
    holder.leases.claim(1, "debug", "test")
    with pytest.raises(ValueError):
        daemon_module.handle(command, 1)
    assert holder.arm.serial.writes == []


def test_double_tap_feature_is_advertised_and_tracked_as_motion(holder):
    assert "tap2.v1" in holder.status()["motionFeatures"]
    assert "TAP2" in daemon_module.MOTION_COMMANDS


@pytest.mark.parametrize("command", [
    "SWIPE 20 0 1 F2000 Z1500",
    "SWIPE -20 10 1 F2000 Z1500",
])
def test_swipe_contact_is_continuous_with_one_final_wait(holder, command):
    calls = []
    holder.arm.send = lambda cmd: calls.append(cmd)
    holder.arm.idle = lambda: calls.append("IDLE") or {"x": 20, "y": 0, "z": 2}
    holder.leases.claim(1, "debug", "test")
    json.loads(daemon_module.handle(command, 1))
    parts = command.split()
    assert calls == [
        "G91", "G1 Z1.000 F1500",
        f"G1 X{float(parts[1]):.3f} Y{float(parts[2]):.3f} F2000",
        "G1 Z-1.000 F1500", "G90", "IDLE",
    ]
    assert "swipe.v1" in holder.status()["motionFeatures"]
    assert "SWIPE" in daemon_module.MOTION_COMMANDS


@pytest.mark.parametrize("command", [
    "SWIPE nan 0 1 F1000 Z1500", "SWIPE 0 inf 1 F1000 Z1500",
    "SWIPE 0 0 1 F1000 Z1500", "SWIPE 151 0 1 F1000 Z1500",
    "SWIPE 120 120 1 F1000 Z1500", "SWIPE 20 0 11 F1000 Z1500",
    "SWIPE 20 0 -1 F1000 Z1500", "SWIPE 20 0 1 F3001 Z1500",
    "SWIPE 20 0 1 F99 Z1500", "SWIPE 20 0 1 Fnan Z1500",
    "SWIPE 20 0 1 F1000 Z29", "SWIPE 20 0 1 F1000 Zinf",
    "SWIPE 20 0 1 F1000", "SWIPE 20 0 1 F1000 Z1500 Z1000",
])
def test_swipe_invalid_parameters_never_touch_serial(holder, command):
    holder.leases.claim(1, "debug", "test")
    with pytest.raises(ValueError):
        daemon_module.handle(command, 1)
    assert holder.arm.serial.writes == []


@pytest.mark.parametrize("command", ["SWIPE 20 0 1 F1000 Z1500", "TAP2 1 I0.12"])
def test_contact_macro_cancellation_prevents_remaining_writes(holder, command):
    arm = object.__new__(ArmSession)
    arm.serial = _FakeSerial()
    holder.arm = arm
    holder.leases.claim(1, "debug", "test")
    cancel = threading.Event()
    reads = 0

    def readline():
        nonlocal reads
        reads += 1
        if reads == 2:
            cancel.set()
        return b"ok\n"

    arm.serial.readline = readline
    with pytest.raises(RuntimeError, match="停止"):
        daemon_module.handle(command, 1, cancel)
    assert arm.serial.writes == [b"G91\n", b"G1 Z1.000 F1500\n", b"!\n"]
    assert holder.estop_locked


def test_resume_cannot_bypass_estop_or_lease(holder):
    holder.estop_locked = True
    with pytest.raises(ValueError, match="急停"):
        daemon_module.handle("RESUME", 1)
    holder.estop_locked = False
    with pytest.raises(ValueError, match="租约"):
        daemon_module.handle("RESUME", 1)
    assert holder.arm.serial.writes == []


def test_status_expires_stale_lease(holder):
    holder.leases.claim(1, "debug", "test")
    holder.leases.lease["expires_at"] = time.time() - 1
    assert holder.status()["lease"] is None


def test_estop_reports_failed_stop_delivery(holder):
    def fail(_data):
        raise OSError("unplugged")
    holder.arm.serial.write = fail
    result = json.loads(daemon_module.handle("ESTOP_LOCK test", 1))
    assert result["estop_locked"] is True
    assert result["stopped"] is False
    assert result["outcome_hint"] == "unknown"


def test_stop_aborts_remaining_tap_commands_even_after_reset(holder):
    arm = object.__new__(ArmSession)
    arm.serial = _FakeSerial()
    arm.serial.readline = lambda: b"ok\n"
    holder.arm = arm
    holder.leases.claim(1, "debug", "test")
    original_write = arm.serial.write
    def write(data):
        result = original_write(data)
        return result
    arm.serial.write = write
    reads = 0
    def readline():
        nonlocal reads
        reads += 1
        if reads == 1:
            holder.stop_now()
        return b"ok\n"
    arm.serial.readline = readline
    with pytest.raises(RuntimeError, match="停止"):
        daemon_module.handle("TAP 1", 1)
    assert not any(b"G1" in line for line in arm.serial.writes)


def test_client_idle_timeout_releases_connection_and_lease(monkeypatch, holder):
    holder.leases.claim(1, "debug", "test")
    slot = _NoopSlot()
    conn = _FakeConn([socket.timeout()])
    monkeypatch.setattr(daemon_module, "CLIENT_IDLE_TIMEOUT_S", 0.01)

    daemon_module.serve_client(conn, 1, slot)

    assert conn.timeout == 0.01
    assert conn.closed
    assert slot.released == 1
    assert holder.leases.snapshot() is None


def test_busy_serial_command_fails_after_bounded_lock_wait(holder, monkeypatch):
    monkeypatch.setattr(daemon_module, "COMMAND_LOCK_TIMEOUT_S", 0.01)
    holder.cmd_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="daemon 正忙"):
            holder.run(lambda _arm: "never")
    finally:
        holder.cmd_lock.release()


def test_connect_retries_one_open_einval_and_closes_failed_serial(monkeypatch):
    _OpenableSerial.instances.clear()
    _OpenableSerial.open_failures[:] = [
        serial.SerialException(22, "Invalid argument"),
    ]
    monkeypatch.setattr(daemon_module.serial, "Serial", _OpenableSerial)
    monkeypatch.setattr(daemon_module.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(daemon_module.time, "sleep", lambda _seconds: None)

    arm = ArmSession()

    assert arm.serial is _OpenableSerial.instances[-1]
    assert arm.serial.is_open
    assert _OpenableSerial.instances[0].closed
    assert "exclusive" not in arm.serial.kwargs
    arm.close()


def test_connect_failure_after_open_closes_serial_and_reports_stage(monkeypatch):
    _OpenableSerial.instances.clear()
    _OpenableSerial.open_failures[:] = []
    _OpenableSerial.flush_failure = OSError(22, "Invalid argument")
    monkeypatch.setattr(daemon_module.serial, "Serial", _OpenableSerial)
    monkeypatch.setattr(daemon_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="startup_flush"):
        ArmSession()

    assert _OpenableSerial.instances[0].closed
    _OpenableSerial.flush_failure = None


@pytest.mark.parametrize("field", ["dtr_failure", "rts_failure"])
def test_connect_ignores_optional_line_control_einval(monkeypatch, field):
    _OpenableSerial.instances.clear()
    _OpenableSerial.open_failures[:] = []
    setattr(_OpenableSerial, field, OSError(22, "Invalid argument"))
    monkeypatch.setattr(daemon_module.serial, "Serial", _OpenableSerial)
    monkeypatch.setattr(daemon_module.time, "sleep", lambda _seconds: None)

    arm = ArmSession()

    assert arm.serial.is_open
    arm.close()
    setattr(_OpenableSerial, field, None)
