"""连接/恢复的成功必须来自控制器状态，全部使用假串口。"""
import json
import threading

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
    assert all(item == b"?" for item in wire.writes)
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
    assert wire.writes == [b"~", b"?", b"?"]
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


@pytest.mark.parametrize("banner_at", [3.2, 4.0, 5.5, 6.3])
def test_connect_delayed_boot_tail_does_not_drop_manual_jog_session(rig, monkeypatch, banner_at):
    """现场链：CONNECT 成功后下一轮位置查询才读到启动尾包。"""
    from scripts import motor_debug_server as debug
    holder, wire, clock = rig
    holder.arm = None
    open_fake(monkeypatch, wire)
    write = wire.write
    sent_banner = False

    def respond(data):
        nonlocal sent_banner
        result = write(data)
        if data == b"?":
            if clock.now >= banner_at and not sent_banner:
                wire.fragments.append(b"Grbl 1.3b ['$' for help]\n")
                sent_banner = True
            wire.fragments.append(POSITION)
        return result

    wire.write = respond
    client = object.__new__(debug.DaemonClient)
    client._lock = threading.RLock()
    client.lease_id = None
    client._close_ordinary_channel = lambda: None
    client.device_id = daemon.DEVICE_ID
    client.daemon_addr = ("127.0.0.1", 8795)
    client._status_cmd = lambda line, **_kw: daemon.handle(line, 12)
    monkeypatch.setattr(client, "cmd", lambda line: daemon.handle(line, 11))
    client.connect_arm()
    clock.sleep(3)
    try:
        daemon.handle("MPOS", 12)  # 页面状态轮询，不下发运动
    except RuntimeError:
        pass
    context_id = "ctx-delayed-boot"
    monkeypatch.setattr(debug, "CONTEXTS", {context_id: {
        "device_id": daemon.DEVICE_ID, "expires_at": debug.time.time() + 60,
        "daemon_addr": "127.0.0.1:8795"}})
    monkeypatch.setattr(debug, "daemon_for", lambda _d: client)
    # 同一个手动点动守卫不得因尚未收完的开机尾包而报 daemon_unreachable。
    debug.require_context(daemon.DEVICE_ID, context_id, motion=True, manual=True)
    assert sent_banner and holder.arm is not None
    assert all(item == b"?" for item in wire.writes)


def test_status_retains_disconnect_reason_until_new_manual_connection(rig, monkeypatch):
    holder, wire, _clock = rig
    wire.fragments.append(b"Grbl 1.3b ['$' for help]\n")
    with pytest.raises(RuntimeError, match="启动/复位"):
        daemon.handle("MPOS", 12)
    status = json.loads(daemon.handle("STATUS", 12))
    assert status["connected"] is False
    assert "启动/复位" in status["disconnectReason"]
    replacement = type("FakeArm", (), {"session": "new-session"})()
    monkeypatch.setattr(daemon, "ArmSession", lambda: replacement)
    daemon.handle("CONNECT", 11)
    status = json.loads(daemon.handle("STATUS", 12))
    assert status["session"] == "new-session"
    assert status["disconnectReason"] is None
