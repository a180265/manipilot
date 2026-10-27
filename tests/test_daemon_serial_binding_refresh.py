"""真实 watcher 路径：换口后只更新断开状态的监听绑定，不打开串口。"""
import json
import threading
from types import SimpleNamespace

import pytest

from scripts import arm_daemon3 as daemon


class WatchFinished(Exception):
    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    config = tmp_path / "devices.json"
    config.write_text(json.dumps({"devices": [
        {"device_id": "arm-02", "serial_port": "/dev/cu.other"},
        {"device_id": "arm-01", "serial_port": "/dev/cu.new"}]}))
    monkeypatch.setenv("ARM_DAEMON_CONFIG_FILE", str(config))
    monkeypatch.setattr(daemon, "DEVICE_ID", "arm-01")
    monkeypatch.setattr(daemon, "PORT", "/dev/cu.old")
    monkeypatch.setattr(daemon.os.path, "exists", lambda p: p != "/dev/cu.old")
    monkeypatch.setattr(daemon.serial, "Serial", lambda *a, **k: pytest.fail("不得打开串口"))
    monkeypatch.setattr(daemon.time, "sleep", lambda _: (_ for _ in ()).throw(WatchFinished()))
    holder = object.__new__(daemon.ArmHolder)
    holder.arm = None
    holder.device_present = False
    holder.lock = threading.Lock()
    holder.cmd_lock = threading.Lock()
    holder.leases = daemon.LeaseManager()
    holder.estop_locked = False
    holder.communication_recovery = None
    return holder, config


def tick(holder):
    with pytest.raises(WatchFinished):
        holder._watch()


def test_watch_adopts_saved_port_and_reports_present_without_connecting(rig):
    holder, _ = rig
    tick(holder)
    assert daemon.PORT == "/dev/cu.new"
    status = holder.status()
    assert status["devicePresent"] is True
    assert status["connected"] is False and status["session"] is None


def test_watch_preserves_connected_session_and_port(rig, monkeypatch):
    holder, _ = rig
    holder.arm = SimpleNamespace(session="existing-session")
    monkeypatch.setattr(daemon.os.path, "exists", lambda _: True)
    tick(holder)
    assert daemon.PORT == "/dev/cu.old"
    assert holder.arm.session == "existing-session"


@pytest.mark.parametrize("contents", ["bad json", "{}", '{"devices": []}',
    '{"devices": [{"device_id": "arm-01", "serial_port": "/etc/passwd"}]}'])
def test_invalid_binding_preserves_current_port(rig, contents):
    holder, config = rig
    config.write_text(contents)
    tick(holder)
    assert daemon.PORT == "/dev/cu.old"


def test_standalone_daemon_without_config_keeps_explicit_port(rig, monkeypatch):
    holder, _ = rig
    monkeypatch.delenv("ARM_DAEMON_CONFIG_FILE")
    tick(holder)
    assert daemon.PORT == "/dev/cu.old"


def test_watch_does_not_change_port_while_lease_is_held(rig):
    holder, _ = rig
    holder.leases.claim(123, "debug", "operator")
    tick(holder)
    assert daemon.PORT == "/dev/cu.old"


def test_duplicate_port_ownership_is_not_adopted(rig):
    holder, config = rig
    config.write_text(json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.new"},
        {"device_id": "arm-02", "serial_port": "/dev/cu.new"}]}))
    tick(holder)
    assert daemon.PORT == "/dev/cu.old"


def test_manual_connect_refreshes_port_before_next_watch_tick(rig, monkeypatch):
    holder, _ = rig
    opened = []

    def fake_session():
        opened.append(daemon.PORT)
        return SimpleNamespace(session="new-session")

    monkeypatch.setattr(daemon, "ArmSession", fake_session)
    assert holder.connect()["session"] == "new-session"
    assert opened == ["/dev/cu.new"]


def test_watch_checks_presence_under_connect_lock(rig, monkeypatch):
    holder, _ = rig
    checks = []

    def exists(port):
        assert holder.cmd_lock.locked(), "在位检测必须与 CONNECT 串行"
        checks.append(port)
        return True

    monkeypatch.setattr(daemon.os.path, "exists", exists)
    tick(holder)
    assert checks and set(checks) == {"/dev/cu.new"}


def test_watch_skips_presence_check_while_command_is_running(rig, monkeypatch):
    holder, _ = rig
    monkeypatch.setattr(daemon.os.path, "exists", lambda _: pytest.fail("不得并发检测"))
    with holder.cmd_lock:
        tick(holder)
    assert daemon.PORT == "/dev/cu.old"
