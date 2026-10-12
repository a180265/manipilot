"""daemon 进程监督器验收：每设备独立启动、崩溃有限重启和安全停止。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from runtime.daemon_supervisor import DaemonSupervisor


@pytest.fixture(autouse=True)
def isolated_supervisor(monkeypatch):
    import runtime.daemon_supervisor as module

    monkeypatch.setattr(module, "_port_is_open", lambda *args: False)
    monkeypatch.setattr(module, "_existing_daemon", lambda *args: None)

    def forbidden_process(*args, **kwargs):
        raise AssertionError("Tests must supply a fake process factory")

    monkeypatch.setattr(module.subprocess, "Popen", forbidden_process)


async def _wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout=2)


class FakeProcess:
    _next_pid = 4000

    def __init__(self):
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.terminated = True
        self.returncode = -9

    def wait(self, _timeout=None):
        return self.returncode or 0


def _config(tmp_path: Path, device_id: str = "arm-01") -> dict:
    script = tmp_path / "scripts" / "arm_daemon3.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# test daemon\n", encoding="utf-8")
    return {
        "device_id": device_id,
        "driver_mode": "daemon",
        "daemon_addr": "127.0.0.1:19895",
        "serial_port": "/dev/cu.test",
    }


def test_supervisor_starts_one_process_per_real_daemon(tmp_path):
    created = []

    def factory(command, **kwargs):
        created.append((command, kwargs))
        return FakeProcess()

    async def scenario():
        cfg = _config(tmp_path)
        supervisor = DaemonSupervisor(
            tmp_path, hardware_mode="real", poll_interval_seconds=0.01,
            restart_delay_seconds=0, process_factory=factory)
        try:
            states = await supervisor.start([cfg])
            assert states[0]["status"] == "running"
            assert len(created) == 1
            command, kwargs = created[0]
            assert command[-1].endswith("arm_daemon3.py")
            assert kwargs["env"]["ARM_DAEMON_DEVICE_ID"] == "arm-01"
            assert kwargs["env"]["ARM_DAEMON_PORT"] == "19895"
            assert kwargs["env"]["ARM_DAEMON_SERIAL"] == "/dev/cu.test"
        finally:
            await supervisor.stop_all()

    asyncio.run(scenario())


def test_supervisor_restarts_crashed_process_with_limit(tmp_path):
    processes = []

    def factory(_command, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    async def scenario():
        cfg = _config(tmp_path)
        supervisor = DaemonSupervisor(
            tmp_path, hardware_mode="real", poll_interval_seconds=0.01,
            restart_delay_seconds=0, max_restarts=1, process_factory=factory)
        await supervisor.start([cfg])
        try:
            processes[0].returncode = 17
            await _wait_until(lambda: len(processes) >= 2)
            assert len(processes) >= 2
            assert supervisor.state("arm-01")["restart_count"] == 1
            processes[-1].returncode = 18
            await _wait_until(lambda: supervisor.state("arm-01")["status"] == "failed")
            assert supervisor.state("arm-01")["status"] == "failed"
            assert len(processes) == 2
        finally:
            await supervisor.stop_all()

    asyncio.run(scenario())


def test_supervisor_skips_simulation_and_rejects_invalid_config(tmp_path):
    async def scenario():
        supervisor = DaemonSupervisor(tmp_path, hardware_mode="simulation", process_factory=FakeProcess)
        cfg = _config(tmp_path)
        states = await supervisor.start([cfg])
        assert states[0]["status"] == "disabled"
        await supervisor.stop_all()

        real = DaemonSupervisor(tmp_path, hardware_mode="real", process_factory=FakeProcess)
        bad = {**cfg, "daemon_addr": "127.0.0.1:not-a-port"}
        states = await real.start([bad])
        try:
            assert states[0]["status"] == "config_error"
            assert "host:port" in states[0]["last_error"]
        finally:
            await real.stop_all()

    asyncio.run(scenario())


def test_existing_matching_daemon_is_reused_without_connect_or_restart(tmp_path, monkeypatch):
    import runtime.daemon_supervisor as module
    monkeypatch.setattr(module, "_port_is_open", lambda *a: True)
    monkeypatch.setattr(module, "_existing_daemon", lambda host, port, device: {"deviceId": device, "session": "kept"})
    spawned = []
    async def scenario():
        supervisor = DaemonSupervisor(tmp_path, hardware_mode="real",
                                      process_factory=lambda *a, **kw: spawned.append(a))
        states = await supervisor.start([_config(tmp_path)])
        assert states[0]["status"] == "external"
        assert states[0]["daemon_session"] == "kept"
        assert not spawned
        await supervisor.stop_all()
        assert supervisor.state("arm-01")["status"] == "external"
        assert not spawned
    asyncio.run(scenario())


def test_detach_preserves_owned_daemon_for_runtime_reload(tmp_path):
    processes = []

    def factory(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    async def scenario():
        supervisor = DaemonSupervisor(
            tmp_path, hardware_mode="real", process_factory=factory,
            poll_interval_seconds=0.01)
        await supervisor.start([_config(tmp_path)])
        await supervisor.detach_all()
        assert len(processes) == 1
        assert processes[0].terminated is False
        assert supervisor._processes == {}
        assert supervisor._monitor_task is None

    asyncio.run(scenario())


def test_wrong_existing_service_is_still_a_port_conflict(tmp_path, monkeypatch):
    import runtime.daemon_supervisor as module
    monkeypatch.setattr(module, "_port_is_open", lambda *a: True)
    monkeypatch.setattr(module, "_existing_daemon", lambda *a: None)
    async def scenario():
        supervisor = DaemonSupervisor(tmp_path, hardware_mode="real")
        states = await supervisor.start([_config(tmp_path)])
        assert states[0]["status"] == "port_conflict"
        await supervisor.stop_all()
    asyncio.run(scenario())


@pytest.mark.parametrize("max_restarts", [0, 2])
def test_failed_spawn_uses_finite_retry_budget(tmp_path, max_restarts):
    attempts, events = [], []

    def fail(*args, **kwargs):
        attempts.append(kwargs["stdout"])
        raise OSError("fake spawn failure")

    async def scenario():
        supervisor = DaemonSupervisor(
            tmp_path, hardware_mode="real", max_restarts=max_restarts,
            restart_delay_seconds=0, process_factory=fail, event_handler=events.append)
        supervisor._configs = {"arm-01": _config(tmp_path)}
        await supervisor.start_device("arm-01")
        for _ in range(6):
            await supervisor._monitor_once()
        assert len(attempts) == 1 + max_restarts
        assert all(handle.closed for handle in attempts)
        assert supervisor.state("arm-01")["status"] == "failed"
        assert not supervisor._restart_due
        assert sum(e["kind"] == "daemon_restart_exhausted" for e in events) == 1

    asyncio.run(scenario())


def test_log_setup_failure_is_also_bounded(tmp_path):
    async def scenario():
        cfg = _config(tmp_path)
        supervisor = DaemonSupervisor(
            tmp_path, hardware_mode="real", log_dir=tmp_path / "scripts" / "arm_daemon3.py",
            max_restarts=1, restart_delay_seconds=0)
        supervisor._configs = {"arm-01": cfg}
        await supervisor.start_device("arm-01")
        await supervisor._monitor_once()
        assert supervisor.state("arm-01")["status"] == "failed"
        assert supervisor._processes == {}
    asyncio.run(scenario())


def test_stop_cancels_pending_retry(tmp_path):
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise OSError("fake failure")

    async def scenario():
        supervisor = DaemonSupervisor(
            tmp_path, hardware_mode="real", restart_delay_seconds=0,
            process_factory=fail)
        supervisor._configs = {"arm-01": _config(tmp_path)}
        await supervisor.start_device("arm-01")
        await supervisor.stop_device("arm-01")
        await supervisor._monitor_once()
        assert len(attempts) == 1
        assert supervisor.state("arm-01")["status"] == "stopped"
        assert not supervisor._restart_due
    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["new-session", None, "wrong-service"])
def test_external_change_revokes_runtime_lease_and_calibration(tmp_path, monkeypatch, replacement):
    from tests.helpers import make_device_configs, make_runtime

    current = {"deviceId": "arm-01", "session": "old-session", "connected": True}
    monkeypatch.setattr("runtime.daemon_supervisor._port_is_open", lambda *args: True)
    monkeypatch.setattr("runtime.daemon_supervisor._existing_daemon",
                        lambda *args: dict(current) if current is not None else None)

    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.devices.register_and_selfcheck(make_device_configs(1)[0])
        device = runtime.devices.get("arm-01")
        device.update(daemon_session="old-session", calibration_status="validated",
                      device_mode="managed")
        runtime.leases.acquire("arm-01", "fake-task")
        events = []

        async def event_handler(event):
            events.append(event)
            await runtime._handle_daemon_event(event)

        supervisor = DaemonSupervisor(
            tmp_path, hardware_mode="real", event_handler=event_handler)
        supervisor._configs = {"arm-01": _config(tmp_path)}
        await supervisor.start_device("arm-01")
        await supervisor._monitor_once()
        assert events == []
        nonlocal current
        current = (None if replacement == "wrong-service" else {
            "deviceId": "arm-01", "session": replacement,
            "connected": replacement is not None})
        await supervisor._monitor_once()
        assert runtime.leases.get("arm-01") is None
        assert device["daemon_session"] is None
        assert device["calibration_status"] == "due"
        assert device["device_mode"] == "unmanaged"
        assert device["status"] == "offline"
        assert not supervisor._processes
        assert not supervisor._restart_due
        assert events[0]["kind"] == "daemon_exited"
        await supervisor._monitor_once()
        assert len(events) == 1
        await asyncio.sleep(0)
        await supervisor.stop_all()

    asyncio.run(scenario())


def test_external_disappearance_then_failed_spawn_is_bounded(tmp_path, monkeypatch):
    listening, attempts = [True], []
    monkeypatch.setattr("runtime.daemon_supervisor._port_is_open",
                        lambda *args: listening[0])
    monkeypatch.setattr("runtime.daemon_supervisor._existing_daemon",
                        lambda *args: {"deviceId": "arm-01", "session": "old"}
                        if listening[0] else None)

    def fail(*args, **kwargs):
        attempts.append(1)
        raise OSError("fake failure")

    async def scenario():
        supervisor = DaemonSupervisor(tmp_path, hardware_mode="real",
                                      max_restarts=2, restart_delay_seconds=0,
                                      process_factory=fail)
        supervisor._configs = {"arm-01": _config(tmp_path)}
        await supervisor.start_device("arm-01")
        listening[0] = False
        for _ in range(5):
            await supervisor._monitor_once()
        assert len(attempts) == 2
        assert supervisor.state("arm-01")["status"] == "failed"
    asyncio.run(scenario())
