"""Pre-enable maintenance stays isolated from task and hardware execution."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess

import pytest

from runtime.config import PlatformConfig, load_platform_config
from runtime.errors import ApiError
from runtime.runtime import ArmFleetRuntime


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("ARM_FLEET_"):
            monkeypatch.delenv(name)
    def forbidden_io(*args, **kwargs):
        raise AssertionError("Tests must not contact hardware, networks or services")
    monkeypatch.setattr(socket.socket, "connect", forbidden_io)
    monkeypatch.setattr(socket, "create_connection", forbidden_io)
    monkeypatch.setattr(subprocess, "Popen", forbidden_io)


@pytest.mark.parametrize("from_environment", [False, True])
def test_maintenance_configuration_overrides_execution_channels(tmp_path, monkeypatch,
                                                             from_environment):
    unsafe = {
        "pre_enable_maintenance": True,
        "screen_ime_enabled": True,
        "wx_compat_enabled": True,
        "daemon_supervisor_enabled": True,
        "daemon_supervisor_stop_on_shutdown": True,
        "hotplug_discovery_enabled": True,
        "wx_callback_base_url": "http://callback.invalid",
        "wx_camera_introspect_url": "http://callback.invalid/introspect",
    }
    if from_environment:
        monkeypatch.setenv("ARM_FLEET_PRE_ENABLE", "1")
        monkeypatch.setenv("ARM_FLEET_SCREEN_IME", "1")
        monkeypatch.setenv("ARM_FLEET_WX_COMPAT", "1")
        monkeypatch.setenv("ARM_FLEET_DAEMON_SUPERVISOR", "1")
        monkeypatch.setenv("ARM_FLEET_WX_CALLBACK", unsafe["wx_callback_base_url"])
        monkeypatch.setenv("ARM_FLEET_WX_INTROSPECT_URL",
                           unsafe["wx_camera_introspect_url"])
        config = load_platform_config(tmp_path)
    else:
        config = PlatformConfig(unsafe, root=tmp_path)

    assert config.get("pre_enable_maintenance") is True
    for key in ("screen_ime_enabled", "wx_compat_enabled",
                "daemon_supervisor_enabled", "daemon_supervisor_stop_on_shutdown",
                "hotplug_discovery_enabled"):
        assert config.get(key) is False
        assert config.to_dict()[key] is False
    assert config.get("wx_callback_base_url") == ""
    assert config.get("wx_camera_introspect_url") == ""
    assert unsafe["screen_ime_enabled"] is True


@pytest.mark.parametrize("value", [None, "0"])
def test_default_startup_configuration_is_unchanged(tmp_path, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("ARM_FLEET_PRE_ENABLE", value)
    config = load_platform_config(tmp_path)
    assert config.get("pre_enable_maintenance") is False
    assert config.get("screen_ime_enabled") is True
    assert config.get("daemon_supervisor_enabled") is True
    assert config.get("hotplug_discovery_enabled") is True


def make_maintenance_runtime(tmp_path, *, hardware_mode="simulation"):
    return ArmFleetRuntime(PlatformConfig({
        "pre_enable_maintenance": True,
        "hardware_mode": hardware_mode,
    }, root=tmp_path))


def install_fake_drivers(runtime, monkeypatch):
    from drivers.fake_arm import FakeArmDriver
    from drivers.fake_camera import FakeCameraDriver
    from drivers.fake_input import FakeInputDriver

    arm = FakeArmDriver("arm-01")
    arm.data_root = runtime.config.root / "data"
    camera = FakeCameraDriver("cam-01")
    inp = FakeInputDriver("arm-01")
    monkeypatch.setattr(runtime.devices, "_create_drivers",
                        lambda cfg: (arm, camera, inp, "isolated test",
                                     runtime.config.hardware_mode))
    return arm, camera


def test_maintenance_lifecycle_preserves_work_without_background_effects(tmp_path,
                                                                       monkeypatch):
    runtime = make_maintenance_runtime(tmp_path)
    arm, camera = install_fake_drivers(runtime, monkeypatch)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("forbidden")
        raise AssertionError("Maintenance entered an execution or reconciliation path")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    for owner, name in (
        (runtime, "_reload_activated_plugins"),
        (runtime, "_reconcile_input_state"),
        (runtime, "layout_bundle"),
        (runtime, "recover_tasks"),
        (runtime.capabilities, "bootstrap"),
        (runtime.leases, "clear_all"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    for owner, name in (
        (runtime.daemon_supervisor, "start"),
        (runtime.daemon_supervisor, "stop_all"),
        (runtime.devices, "start_background"),
        (runtime.devices, "register_all"),
        (runtime.views, "start_background"),
        (runtime.executor, "execute"),
        (runtime.executor, "execute_guarded"),
        (runtime.callback_outbox, "flush"),
        (arm, "connect"), (arm, "disconnect"), (arm, "read_state"),
        (arm, "stop"), (arm, "go_home"),
    ):
        monkeypatch.setattr(owner, name, forbidden_async)

    runtime.store.save_device({
        "device_id": "arm-01", "status": "paused", "device_mode": "unmanaged",
        "pause_reason": "keep manual hold", "calibration_status": "due",
        "lease_id": "old-lease", "home_ok": False,
    })
    for status in ("queued", "validating", "awaiting_device", "executing"):
        runtime.store.save_task({
            "task_id": f"old-{status}", "idempotency_key": status,
            "device_id": "arm-01", "status": status, "task_type": "legacy",
            "plan": [{"step_no": 1}], "step_log": [
                {"step_no": 1, "phase": "done", "outcome": "success"}],
        })
    lease = {"device_id": "arm-01", "task_id": "old-executing",
             "lease_id": "old-lease"}
    runtime.store.save_lease(lease)
    runtime.store.save_callback({
        "callback_id": "old-callback", "status": "pending",
        "next_attempt_at": "2000-01-01T00:00:00+00:00",
        "path": "/do-not-deliver", "payload": {},
    })
    preserved = {
        path: b"untouched maintenance fixture"
        for path in (runtime.config.screen_ime_db_path,
                     tmp_path / "data" / "layout.db",
                     tmp_path / "data" / "ime_pairing.db")
    }
    for path, content in preserved.items():
        path.write_bytes(content)
    old_tasks = runtime.store.all_tasks()
    old_callbacks = runtime.store.all_callbacks()
    config = {"device_id": "arm-01", "camera_id": "cam-01",
              "calibration_status": "due"}

    async def run():
        try:
            assert runtime.pre_enable_maintenance is True
            assert runtime.input_startup_reconciling is True
            await runtime.startup(device_configs=[config])
            await asyncio.sleep(0.12)
            assert runtime._running is True
            assert runtime.input_startup_reconciling is True
            assert runtime.input_switch_allowed("arm-01") is False
            assert runtime._dispatch_task is None
            assert runtime._callback_flush_task is None
            assert runtime._exec_tasks == {}
            device = runtime.devices.get("arm-01")
            assert device["status"] == "paused"
            assert device["device_mode"] == "unmanaged"
            assert device["calibration_status"] == "due"
            assert device["pause_reason"] == "keep manual hold"
            assert device["lease_id"] == "old-lease"
            assert device["camera_quality"] == "clear"
            assert camera.is_open()
            with pytest.raises(ApiError) as caught:
                runtime.submit_task({}, source="console", context={})
            assert caught.value.key == "pre_enable_maintenance"
            assert caught.value.http == 503
        finally:
            await runtime.shutdown(stop_daemons=True)
        assert not camera.is_open()
        assert runtime.input_startup_reconciling is True
        assert runtime.store.all_tasks() == old_tasks
        assert runtime.store.all_callbacks() == old_callbacks
        assert runtime.store.get_lease("arm-01") == lease
        assert calls == []
        assert arm.action_log == []
        for path, content in preserved.items():
            assert path.read_bytes() == content

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_property_is_a_boolean_and_matches_effective_config(tmp_path, enabled):
    runtime = ArmFleetRuntime(PlatformConfig({
        "pre_enable_maintenance": enabled,
    }, root=tmp_path))
    assert runtime.pre_enable_maintenance is enabled
    assert runtime.input_startup_reconciling is enabled
    assert runtime.config.get("pre_enable_maintenance") is enabled
    asyncio.run(runtime.shutdown())


@pytest.mark.parametrize(("hardware_mode", "driver_mode", "frame_url"), [
    ("real", "real", "http://camera.invalid/frame"),
    ("real", None, "http://camera.invalid/frame"),
    ("real", "simulation", "http://camera.invalid/frame"),
    ("simulation", "real", "http://camera.invalid/frame"),
    ("real", "daemon", None),
])
def test_unsafe_device_configuration_fails_before_any_registration(
        tmp_path, monkeypatch, hardware_mode, driver_mode, frame_url):
    runtime = make_maintenance_runtime(tmp_path, hardware_mode=hardware_mode)
    registrations = []
    monkeypatch.setattr(runtime.devices, "register_device",
                        lambda cfg: registrations.append(cfg))
    safe = {"device_id": "arm-01", "camera_id": "cam-01",
            "driver_mode": "daemon", "camera_frame_url": "http://camera.invalid/frame"}
    unsafe = {"device_id": "arm-02", "camera_id": "cam-02",
              "binding_mode": "real", "driver_mode": driver_mode,
              "camera_frame_url": frame_url}

    async def run():
        with pytest.raises(ApiError) as caught:
            await runtime.startup(device_configs=[safe, unsafe])
        assert caught.value.key == "device_config_invalid"
        assert registrations == []
        assert runtime._running is False
        assert runtime.input_startup_reconciling is True
        assert runtime.input_control._resource_lock_files == []
        await runtime.shutdown(stop_daemons=True)

    asyncio.run(run())


def test_real_daemon_binding_uses_fake_camera_without_connect_or_heartbeat(tmp_path,
                                                                         monkeypatch):
    runtime = make_maintenance_runtime(tmp_path, hardware_mode="real")
    arm, camera = install_fake_drivers(runtime, monkeypatch)
    config = {"device_id": "arm-01", "camera_id": "cam-01",
              "driver_mode": "daemon", "binding_mode": "real",
              "camera_frame_url": "http://camera.invalid/frame"}

    async def run():
        try:
            await runtime.startup(device_configs=[config])
            await asyncio.sleep(0.12)
            device = runtime.devices.get("arm-01")
            assert device["driver_mode"] == "daemon"
            assert device["status"] == "paused"
            assert device["device_mode"] == "unmanaged"
            assert device["daemon_session"] is None
            assert runtime.devices._heartbeat_task is None
            assert runtime.devices._camera_task is None
            assert runtime.devices._discovery_task is None
            assert camera.is_open()
        finally:
            await runtime.shutdown(stop_daemons=True)
        assert arm.action_log == []
        assert not camera.is_open()

    asyncio.run(run())


def test_maintenance_holds_exclusive_resource_lock_until_shutdown(tmp_path):
    from runtime.input_control import InputControlError

    first = make_maintenance_runtime(tmp_path)
    second = make_maintenance_runtime(tmp_path)

    async def run():
        try:
            await first.startup(device_configs=[])
            with pytest.raises(InputControlError):
                await second.startup(device_configs=[])
            assert second._running is False
        finally:
            await first.shutdown()
        try:
            await second.startup(device_configs=[])
            assert second._running is True
        finally:
            await second.shutdown()

    asyncio.run(run())


def test_maintenance_camera_failure_does_not_relax_hold(tmp_path, monkeypatch):
    runtime = make_maintenance_runtime(tmp_path)
    arm, _camera = install_fake_drivers(runtime, monkeypatch)

    async def fail_open():
        raise RuntimeError("camera offline")

    monkeypatch.setattr(_camera, "open", fail_open)
    runtime.store.save_device({
        "device_id": "arm-01", "status": "quarantined",
        "quarantine_reason": "keep quarantine", "device_mode": "unmanaged",
    })

    async def run():
        try:
            await runtime.startup(device_configs=[
                {"device_id": "arm-01", "camera_id": "cam-01"}])
            device = runtime.devices.get("arm-01")
            assert device["status"] == "quarantined"
            assert device["quarantine_reason"] == "keep quarantine"
            assert device["device_mode"] == "unmanaged"
            assert device["camera_quality"] == "abnormal"
            assert runtime.input_startup_reconciling is True
        finally:
            await runtime.shutdown()
        assert arm.action_log == []

    asyncio.run(run())


def test_bound_phone_eligibility_and_device_gets_never_create_ime_service(tmp_path,
                                                                      monkeypatch):
    import httpx

    from adapters.screen_ime import service
    from api.app import create_app

    ime_calls = []

    def forbidden_ime(*args, **kwargs):
        ime_calls.append("unexpected IME construction")
        raise AssertionError("Maintenance must not instantiate an IME service")

    monkeypatch.setattr(service, "get_default_service", forbidden_ime)
    monkeypatch.setattr(service.ScreenImeService, "__init__", forbidden_ime)
    runtime = make_maintenance_runtime(tmp_path)
    arm, _camera = install_fake_drivers(runtime, monkeypatch)
    app = create_app(runtime)
    app.state.device_configs = [{
        "device_id": "arm-01", "camera_id": "cam-01",
        "ime_device_id": "phone-maintenance-fixture",
        "enforce_input_gate": True,
    }]

    async def run():
        async with app.router.lifespan_context(app):
            assert runtime.devices._ime_status_provider() == []
            eligibility = runtime.devices.eligibility("arm-01", require_input=True)
            assert eligibility["can_accept_task"] is False
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://isolated.test") as client:
                for path in ("/v1/devices", "/v1/devices/arm-01"):
                    response = await client.get(path)
                    assert response.status_code == 200, response.text
            assert runtime.screen_ime is None
        assert not runtime.config.screen_ime_db_path.exists()
        assert not runtime.config.screen_ime_mappings_path.exists()
        assert arm.action_log == []
        assert ime_calls == []

    asyncio.run(run())


def test_direct_recovery_dispatch_and_input_queries_remain_frozen(tmp_path,
                                                               monkeypatch):
    runtime = make_maintenance_runtime(tmp_path)
    for status in ("queued", "executing"):
        runtime.store.save_task({
            "task_id": status, "idempotency_key": status, "device_id": "arm-01",
            "status": status, "task_type": "legacy",
            "created_at": "2000-01-01T00:00:00+00:00",
            "expires_at": "2000-01-02T00:00:00+00:00",
            "plan": [{"step_no": 1}],
            "step_log": [{"step_no": 1, "phase": "done", "outcome": "success"}],
        })
    old_tasks = runtime.store.all_tasks()
    monkeypatch.setattr(runtime.input_control, "load_settings_verified",
                        lambda *args, **kwargs: {"enabled": True})

    async def run():
        try:
            await runtime.startup(device_configs=[])
            assert runtime.recover_tasks() == []
            await runtime._dispatch_once()
            await runtime._callback_flush_loop()
            assert runtime.input_switch_allowed("arm-01") is False
            assert runtime.store.all_tasks() == old_tasks
            assert runtime._exec_tasks == {}
        finally:
            await runtime.shutdown()

    asyncio.run(run())


def test_emergency_stop_is_local_and_environment_cannot_restore_callbacks(tmp_path,
                                                                        monkeypatch):
    import httpx

    monkeypatch.setenv("ARM_FLEET_WX_CALLBACK", "http://callback.invalid")
    runtime = make_maintenance_runtime(tmp_path)
    arm, _camera = install_fake_drivers(runtime, monkeypatch)
    calls = []

    def forbidden_request(request):
        calls.append(request)
        raise AssertionError("Maintenance callbacks must stay disabled")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_request)) as client:
            runtime.reporter.client = client
            try:
                await runtime.startup(device_configs=[
                    {"device_id": "arm-01", "camera_id": "cam-01"}])
                assert await runtime.reporter.send_emergency_stop("test") is False
                result = runtime.estop.trigger("test", "explicit local emergency stop")
                await asyncio.sleep(0)
                assert result["estop_active"] is True
                assert runtime.estop.active is True
                assert runtime.devices.get("arm-01")["estop_latched"] is True
                assert [entry["action"] for entry in arm.action_log] == ["estop"]
                assert runtime.store.all_callbacks() == []
            finally:
                await runtime.shutdown()
        assert calls == []

    asyncio.run(run())
