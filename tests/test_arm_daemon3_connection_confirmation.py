"""连接/恢复的成功必须来自控制器状态，全部使用假串口。"""
import json

import pytest

from scripts import arm_daemon3 as daemon
from tests.test_arm_daemon3_serial_framing_regression import rig  # noqa: F401


POSITION = b"<Idle|MPos:-29.800,-26.900,-1.000>\n"


def query_responses(wire, replies):
    source = iter(replies)
    write = wire.write

    def respond(data):
        result = write(data)
        if data.startswith(b"?"):
            wire.fragments.extend(next(source, [POSITION]))
        return result

    wire.write = respond


def open_fake(monkeypatch, wire):
    monkeypatch.setattr(daemon.ArmSession, "_open_serial", lambda _self: wire)
    wire.setDTR = lambda _value: None
    wire.setRTS = lambda _value: None
    wire.reset_input_buffer = lambda: None


def test_connect_waits_for_delayed_banner_then_stable_position(rig, monkeypatch):
    holder, wire, clock = rig
    holder.arm = None
    open_fake(monkeypatch, wire)
    query_responses(wire, [[POSITION], [b"Grbl 1.3b ['$' for help]\n", POSITION]])
    result = json.loads(daemon.handle("CONNECT", 11))
    assert result["connected"]
    assert len(wire.writes) >= 4
    assert all(item == b"?\n" for item in wire.writes)
    assert clock.now >= 3.0
    assert holder.arm._boot_banner_grace is False
    # 新连接完成后，真实运行期横幅仍立即使会话失效。
    wire.fragments.append(b"Grbl 1.3b ['$' for help]\n")
    with pytest.raises(RuntimeError, match="启动/复位"):
        daemon.handle("MPOS", 11)
    assert holder.arm is None


def test_connect_silence_never_publishes_connected_session(rig, monkeypatch):
    holder, wire, _clock = rig
    holder.arm = None
    open_fake(monkeypatch, wire)
    with pytest.raises(RuntimeError, match="握手|无响应"):
        daemon.handle("CONNECT", 11)
    assert holder.arm is None
    assert wire.closed


@pytest.mark.parametrize("reply", [
    [b"ok\n", b"Grbl 1.3b ['$' for help]\n", POSITION],
    [POSITION.replace(b"Idle", b"Run")],
])
def test_connect_never_accepts_repeated_restart_or_running_controller(rig, monkeypatch, reply):
    holder, wire, _clock = rig
    holder.arm = None
    open_fake(monkeypatch, wire)
    query_responses(wire, [reply] * 100)
    with pytest.raises(RuntimeError, match="握手"):
        daemon.handle("CONNECT", 11)
    assert holder.arm is None and wire.closed


@pytest.mark.parametrize("state", ["Idle", "Run"])
def test_resume_confirms_controller_state_and_sends_no_motion_replay(rig, state):
    holder, wire, clock = rig
    holder.leases.claim(11, "debug", "resume-test")
    query_responses(wire, [[b"<Hold:0|MPos:-29.8,-26.9,-1>\n"],
                           [POSITION.replace(b"Idle", state.encode())]])
    result = json.loads(daemon.handle("RESUME", 11))
    assert result["resumed"] is True
    assert result["state"] == state
    assert wire.writes == [b"~", b"?\n", b"?\n"]
    assert clock.now < 2.0


@pytest.mark.parametrize("failure", ["silence", "hold", "alarm", "short-write"])
def test_resume_never_claims_success_without_observed_release(rig, failure):
    holder, wire, clock = rig
    holder.leases.claim(11, "debug", "resume-test")
    if failure == "hold":
        query_responses(wire, [[b"<Hold:0|MPos:0,0,0>\n"]] * 100)
    elif failure == "alarm":
        query_responses(wire, [[b"<Alarm|MPos:0,0,0>\n"]])
    elif failure == "short-write":
        wire.write = lambda _data: 0
    with pytest.raises(RuntimeError):
        daemon.handle("RESUME", 11)
    assert clock.now < 2.0
    assert holder.estop_locked is True
    assert not any(item.startswith((b"G", b"$")) for item in wire.writes)
