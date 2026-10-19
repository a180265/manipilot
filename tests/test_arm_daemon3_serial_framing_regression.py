"""Serial framing regressions with no device, socket, or watcher access."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
from types import SimpleNamespace

import pytest

from scripts import arm_daemon3 as daemon


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1000.0 + self.now

    def sleep(self, seconds):
        self.now += seconds


class _ScriptedSerial(io.RawIOBase):
    """Use the same IOBase.readline as pyserial; floats are empty-read delays."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.timeout = 0.6
        self.fragments = deque()
        self.pending = bytearray()
        self.writes = []

    def readable(self):
        return True

    def read(self, size=1):
        assert not self.closed
        if not self.pending:
            if not self.fragments:
                self.clock.sleep(self.timeout)
                return b""
            fragment = self.fragments.popleft()
            if isinstance(fragment, float):
                self.clock.sleep(fragment)
                return b""
            self.pending.extend(fragment)
        count = len(self.pending) if size < 0 else size
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def write(self, data):
        assert not self.closed
        self.writes.append(data)
        return len(data)


@pytest.fixture
def rig(monkeypatch):
    def reject_hardware(*_args, **_kwargs):
        pytest.fail("This regression must never open a physical serial port")

    monkeypatch.setattr(daemon.serial, "Serial", reject_hardware)
    clock = _Clock()
    monkeypatch.setattr(daemon, "time", SimpleNamespace(
        monotonic=clock.monotonic, time=clock.time, sleep=clock.sleep,
    ))
    wire = _ScriptedSerial(clock)
    arm = object.__new__(daemon.ArmSession)
    arm.session = "framing-test-session"
    arm.serial = wire

    holder = object.__new__(daemon.ArmHolder)
    holder.arm = arm
    holder.device_present = True
    holder.lock = threading.Lock()
    holder.cmd_lock = threading.Lock()
    holder.estop_locked = False
    holder.stop_generation = 0
    holder.transactions = {}
    holder.active_commands = {}
    holder.leases = daemon.LeaseManager(ttl=3600)
    monkeypatch.setattr(daemon, "HOLDER", holder)
    yield holder, wire, clock
    wire.close()


@pytest.mark.parametrize("fragments", [
    [b"<Id", 0.6, b"le|MPos:1.000,2.000,3.500>\n"],
    [b"<Idle|MPos:1.000,2.000,3", 0.6, b".500>\n"],
], ids=["split-header", "split-coordinate"])
def test_fragmented_mpos_within_deadline_preserves_position_and_session(rig, fragments):
    holder, wire, clock = rig
    wire.fragments.extend(fragments)

    position = json.loads(daemon.handle("MPOS", 11))

    assert position == {"x": 1.0, "y": 2.0, "z": 3.5, "state": "Idle"}
    assert clock.now < daemon.MPOS_RESPONSE_TIMEOUT_S
    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is True
    assert status["session"] == "framing-test-session"


@pytest.mark.parametrize("invalid", [
    b"<Idle|MPos:1.000,2.000,3\n",
    b"<Idle|MPos:1,2,nan>\n",
    b"<Idle|MPos:1,inf,3>\n",
    b"<Idle|MPos:1,2,3,4>\n",
    b"<Idle|MPos:1,2,3|MPos:4,5,6>\n",
    b"<<Idle|MPos:1,2,3>>\n",
])
def test_malformed_position_never_truncates_or_supersedes_complete_position(rig, invalid):
    _, wire, _ = rig
    wire.fragments.extend([invalid, b"<Idle|MPos:1.000,2.000,3.500>\n"])
    position = json.loads(daemon.handle("MPOS", 11))
    assert position == {"x": 1.0, "y": 2.0, "z": 3.5, "state": "Idle"}
    assert wire.writes == [b"?"]
    assert not wire.closed
    assert len(wire.writes) == 1
    assert wire.writes[0].startswith(b"?")


def test_fragmented_ack_within_deadline_does_not_latch_or_drop_session(rig):
    holder, wire, clock = rig
    holder.leases.claim(11, "debug", "framing-test")
    wire.fragments.extend([b"o", 0.6, b"k\n"])

    response = holder.run(lambda arm: arm.send("G91"), 11, motion=True)

    assert response == ["ok"]
    assert clock.now < 10.0
    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is True
    assert status["session"] == "framing-test-session"
    assert status["estopLocked"] is False
    assert not wire.closed
    assert wire.writes == [b"G91\n"]


def test_unterminated_mpos_is_not_accepted_as_a_position(rig):
    holder, wire, _clock = rig
    wire.fragments.extend([b"<Idle|MPos:1.000,2.000,3.500", 0.6])

    with pytest.raises(RuntimeError):
        daemon.handle("MPOS", 11)

    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is True
    assert status["session"] == "framing-test-session"
    assert status["communicationRecoveryRequired"] is True
    assert not wire.closed


def test_mpos_deadline_covers_a_slow_drip_complete_line(rig):
    holder, wire, clock = rig
    wire.fragments.append(b"<Idle|MPos:1.000,2.000,3.500>\n")
    original_read = wire.read

    def slow_read(size=1):
        clock.sleep(0.1)
        return original_read(min(size, 1))

    wire.read = slow_read
    with pytest.raises(RuntimeError):
        daemon.handle("MPOS", 11)

    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is True
    assert status["session"] == "framing-test-session"
    assert status["communicationRecoveryRequired"] is True
    assert not wire.closed


def test_actual_query_silence_preserves_session_and_requires_human_recovery(rig):
    holder, wire, _clock = rig

    with pytest.raises(RuntimeError):
        daemon.handle("MPOS", 11)

    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is True
    assert status["session"] == "framing-test-session"
    assert status["communicationRecoveryRequired"] is True
    assert status["estopLocked"] is True
    assert not wire.closed
    assert len(wire.writes) == 2
    assert wire.writes[0].startswith(b"?")


def test_missing_motion_ack_still_latches_and_never_replays(rig):
    holder, wire, _clock = rig
    holder.leases.claim(11, "debug", "framing-test")

    with pytest.raises(RuntimeError):
        daemon.handle("Z 1", 11)

    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is True
    assert status["session"] == "framing-test-session"
    assert status["communicationRecoveryRequired"] is True
    assert status["estopLocked"] is True
    assert status["lease"] is None
    assert not wire.closed
    assert wire.writes == [b"G91\n", b"!"]


def test_unrelated_complete_lines_do_not_break_mpos(rig):
    _holder, wire, _clock = rig
    wire.fragments.extend([
        b"ok\n", b"[MSG:Caution: Unlocked]\n",
        b"<Idle|MPos:1.000,2.000,3.500>\n",
    ])

    position = json.loads(daemon.handle("MPOS", 11))

    assert position == {"x": 1.0, "y": 2.0, "z": 3.5, "state": "Idle"}
    assert not wire.closed
    assert len(wire.writes) == 1


@pytest.mark.parametrize("banner", [
    b"[MSG:controller-ready]\n",
    b"Grbl 1.1h ['$' for help]\n",
], ids=["controller-ready-msg", "grbl-version-banner"])
def test_controller_reset_banner_invalidates_session_lease_and_transactions(
        rig, banner):
    """P11：同一串口会话内出现控制器启动/复位提示 → 撤销会话/租约/事务，
    复位后的归零 MPos 不得当作旧坐标系中的有效位置。"""
    holder, wire, _clock = rig
    holder.leases.claim(11, "debug", "framing-test")
    wire.fragments.extend([banner, b"<Idle|MPos:0.000,0.000,0.000>\n"])

    with pytest.raises(Exception, match="复位|坐标基准"):
        daemon.handle("MPOS", 11)

    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is False
    assert status["session"] is None
    assert status["lease"] is None
    assert wire.closed


def test_reset_zeroed_mpos_equal_to_old_values_still_never_validates(rig):
    """P12：复位后 MPos 恰与旧值相同也不能凭数值相等恢复资格——
    横幅先到即撤销会话，位置行根本不会被读取采纳。"""
    holder, wire, _clock = rig
    old = {"x": 1.0, "y": 2.0, "z": 3.5}
    holder.leases.claim(11, "debug", "framing-test")
    wire.fragments.extend([
        b"[MSG:controller-ready]\n",
        f"<Idle|MPos:{old['x']:.3f},{old['y']:.3f},{old['z']:.3f}>\n".encode(),
    ])

    with pytest.raises(Exception, match="复位|坐标基准"):
        daemon.handle("MPOS", 11)
    assert json.loads(daemon.handle("STATUS", 12))["connected"] is False


def test_status_never_reads_serial_and_mpos_readers_are_mutually_exclusive(rig):
    _holder, wire, _clock = rig
    wire.fragments.extend([
        b"<Idle|MPos:1.000,2.000,3.500>\n",
        b"<Idle|MPos:4.000,5.000,6.500>\n",
    ])
    entered, release, second_started = (
        threading.Event(), threading.Event(), threading.Event(),
    )
    original_read = wire.read

    def blocked_read(size=1):
        if not entered.is_set():
            entered.set()
            assert release.wait(2), "test failed to release the fake serial reader"
        return original_read(size)

    def second_query():
        second_started.set()
        return daemon.handle("MPOS", 12)

    wire.read = blocked_read
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(daemon.handle, "MPOS", 11)
        try:
            assert entered.wait(1)
            second = pool.submit(second_query)
            assert second_started.wait(1)
            status = json.loads(pool.submit(daemon.handle, "STATUS", 13).result(timeout=1))
            assert status["connected"] is True
            assert len(wire.writes) == 1
            assert wire.writes[0].startswith(b"?")
        finally:
            release.set()
        assert json.loads(first.result(timeout=1))["z"] == 3.5
        assert json.loads(second.result(timeout=1))["z"] == 6.5
    assert not wire.closed
    assert len(wire.writes) == 2
    assert all(data.startswith(b"?") for data in wire.writes)


def test_connect_time_boot_banner_is_expected_and_skipped(rig):
    """P11 修正：CONNECT 时的开机横幅是预期输出（开串口必复位 ESP32），
    启动宽限内跳过不撤销会话；首次成功位置交换后宽限关闭，再见到横幅仍撤销。"""
    holder, wire, _clock = rig
    holder.leases.claim(11, "debug", "framing-test")
    arm = holder.arm
    arm._boot_banner_grace = True  # 生产 __init__ 在连接时置位
    wire.fragments.extend([
        b"Grbl 1.3b ['$' for help]\n",
        b"<Idle|MPos:0.000,0.000,0.000>\n",
    ])

    position = json.loads(daemon.handle("MPOS", 11))

    assert position == {"x": 0.0, "y": 0.0, "z": 0.0, "state": "Idle"}
    assert not wire.closed
    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is True
    assert arm._boot_banner_grace is False  # 宽限已随首次成功交换关闭

    # 宽限关闭后，同会话再出现横幅 = 隐藏复位，仍须撤销（P11 原语义不变）。
    wire.fragments.extend([
        b"[MSG:controller-ready]\n",
        b"<Idle|MPos:0.000,0.000,0.000>\n",
    ])
    with pytest.raises(Exception, match="复位|坐标基准"):
        daemon.handle("MPOS", 13)
    assert json.loads(daemon.handle("STATUS", 14))["connected"] is False
