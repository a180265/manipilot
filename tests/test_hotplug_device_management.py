from __future__ import annotations

import asyncio

import pytest

from fastapi.testclient import TestClient

from api.app import create_app
from runtime.audit import AuditLog
from runtime.config import PlatformConfig
from runtime.devices import DeviceManager
from runtime.errors import ApiError
from runtime.events import EventBus
from runtime.runtime import ArmFleetRuntime
from runtime.store import Store


def _cfg(device_id="arm-hot-01", **extra):
    value = {
        "device_id": device_id,
        "arm_slot": 21,
        "camera_id": "cam-hot-01",
        "wechat_account_id": "wx-hot-01",
        "ime_device_id": "ime-hot-01",
        "enforce_input_gate": True,
        "calibration_status": "due",
    }
    value.update(extra)
    return value


def test_hotplug_attach_remove_and_binding_change(tmp_path):
    async def scenario():
        # 使用同一 store/audit 组合，避免测试依赖真实硬件。
        store = Store(tmp_path / "real.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
        first = await manager.register_and_selfcheck(_cfg())
        assert first["presence_state"] == "present"
        assert first["calibration_status"] == "due"
        manager.set_input_status_provider(lambda: [{"deviceId": "ime-hot-01", "online": True, "keyboardActive": True}])
        assert manager.eligibility("arm-hot-01", require_input=True)["can_accept_task"] is False
        await manager.attach_device(_cfg(camera_id="cam-hot-02"))
        assert manager.get("arm-hot-01")["calibration_status"] == "due"
        removed = await manager.remove_device("arm-hot-01", "USB 拔出")
        assert removed["presence_state"] == "removed"
        assert manager.eligibility("arm-hot-01", require_input=True)["can_view"] is False

    asyncio.run(scenario())


def test_api_register_and_eligibility_gate(tmp_path):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.device_configs = []
    with TestClient(app) as client:
        response = client.post("/v1/devices/register", json=_cfg())
        assert response.status_code == 200
        body = response.json()
        assert body["device_id"] == "arm-hot-01"
        assert body["eligibility"]["calibration_status"] == "due"
        assert "calibration_due" in body["eligibility"]["blocking_checks"]
        status = client.get("/v1/devices/arm-hot-01/eligibility")
        assert status.status_code == 200
        removed = client.post("/v1/devices/arm-hot-01/remove", json={"reason": "USB 拔出"})
        assert removed.status_code == 200
        assert removed.json()["presence_state"] == "removed"


def test_gate_requires_calibration_but_allows_after_validation(tmp_path):
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    # 测试更正（§4.5，2026-10-03）：A-开关（2026-10-03 规格 §4.6）——
    # require_input 资格要求设备级「允许输入与发送」持久开关；管理器钉在
    # tmp 配置根并预写开关，避免读写真实 data 目录。
    manager = DeviceManager(store, AuditLog(store, events), events,
                            hardware_mode="simulation", config_root=tmp_path)

    async def scenario():
        await manager.register_and_selfcheck(_cfg())
        from runtime import input_settings as iset
        try:
            current = iset.load_settings("arm-hot-01", data_root=tmp_path / "data")
            revision = int(current.get("revision") or 0)
        except iset.InputSettingsError:
            revision = 0
        iset.update_settings(
            "arm-hot-01", data_root=tmp_path / "data", enabled=True,
            ime_device_id="ime-hot-01", expected_revision=revision,
            operator_id="测试", reason="hotplug-gate",
            client_request_id="hotplug-gate-switch", calibration_ready=True)
        manager.set_input_status_provider(lambda: [{"deviceId": "ime-hot-01", "online": True, "keyboardActive": True}])
        before = manager.eligibility("arm-hot-01", require_input=True)
        assert before["can_accept_task"] is False
        manager.set_calibration_status("arm-hot-01", "validated", "五点校准验收通过")
        after = manager.eligibility("arm-hot-01", require_input=True)
        assert after["can_accept_task"] is True

    asyncio.run(scenario())


def test_discovery_reports_known_unknown_and_removal(tmp_path):
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")

    async def scenario():
        await manager.register_and_selfcheck(_cfg())
        first = await manager.scan_discovery(ports=[{"path": "/dev/tty.hot", "fingerprint": "fp-1"}])
        # 配置默认没有该路径，第一次扫描建立基线；未知串口仍需通过事件提示人工登记。
        assert first["probe_available"] is True
        assert first["unknown_ports"][0]["fingerprint"] == "fp-1"
        manager._registered_configs[0]["serial_port"] = "/dev/tty.hot"
        await manager.scan_discovery(ports=[{"path": "/dev/tty.hot", "fingerprint": "fp-1"}])
        await manager.scan_discovery(ports=[])
        assert manager.get("arm-hot-01")["presence_state"] == "removed"

    asyncio.run(scenario())


def test_discovery_reinsert_auto_attaches_and_self_checks(tmp_path):
    """已登记串口拔出后重新插入：自动 attach，恢复 present 且可继续自检。"""
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")

    async def scenario():
        cfg = _cfg(serial_port="/dev/tty.hot")
        await manager.register_and_selfcheck(cfg)
        await manager.scan_discovery(ports=[{"path": "/dev/tty.hot", "fingerprint": "fp-1"}])
        await manager.scan_discovery(ports=[])
        assert manager.get("arm-hot-01")["presence_state"] == "removed"
        await manager.scan_discovery(ports=[{"path": "/dev/tty.hot", "fingerprint": "fp-1"}])
        current = manager.get("arm-hot-01")
        assert current["presence_state"] == "present"
        assert current["status"] == "ready"
        assert "自动挂载" in current["status_reason"]

    asyncio.run(scenario())


def test_heartbeat_reconnects_business_daemon_bridge_after_debug_connect(tmp_path):
    """调试台重新 CONNECT 后，业务驱动桥不能继续停在 offline。"""
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")

    class RecoveringDaemonBridge:
        def __init__(self):
            self.connected = False
            self.connect_calls = 0

        async def read_state(self):
            return {
                "connected": self.connected,
                "session": "session-new" if self.connected else None,
                "estop_latched": False,
            }

        async def connect(self):
            self.connect_calls += 1
            self.connected = True
            return {"connected": True, "session": "session-new"}

    async def scenario():
        await manager.register_and_selfcheck(_cfg())
        device = manager.get("arm-hot-01")
        device.update({
            "driver_mode": "daemon",
            "presence_state": "present",
            "status": "offline",
            "status_display": "离线",
            "daemon_session": "session-old",
            "calibration_status": "validated",
            "device_mode": "ready_manual",
        })
        manager._store.save_device(device)
        bridge = RecoveringDaemonBridge()
        manager._arms["arm-hot-01"] = bridge

        await manager._heartbeat_device("arm-hot-01")

        current = manager.get("arm-hot-01")
        assert bridge.connect_calls == 1
        assert current["status"] == "ready"
        assert current["status_reason"] == "daemon 连接恢复，业务驱动桥已重连"
        assert current["daemon_session"] == "session-new"
        # 新 daemon 会话必须撤销旧校准，不能因为状态恢复而绕过重标定。
        assert current["calibration_status"] == "due"
        assert current["device_mode"] == "unmanaged"

    asyncio.run(scenario())


def test_heartbeat_clears_stale_manual_eligibility_when_daemon_is_disconnected(tmp_path):
    """daemon 未建立会话时，离线镜像不能继续显示旧校准和人工资格。"""
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")

    class DisconnectedDaemonBridge:
        async def read_state(self):
            return {
                "connected": False,
                "session": None,
                "estop_latched": False,
            }

    async def scenario():
        await manager.register_and_selfcheck(_cfg(calibration_status="validated"))
        device = manager.get("arm-hot-01")
        device.update({
            "driver_mode": "daemon",
            "presence_state": "present",
            "status": "offline",
            "status_display": "离线",
            "status_reason": "旧进程遗留状态",
            "calibration_status": "validated",
            "calibration_status_display": "已校准",
            "device_mode": "ready_manual",
            "device_mode_display": "可人工输入",
        })
        manager._store.save_device(device)
        manager._arms["arm-hot-01"] = DisconnectedDaemonBridge()

        await manager._heartbeat_device("arm-hot-01")

        current = manager.get("arm-hot-01")
        assert current["status"] == "offline"
        assert current["calibration_status"] == "due"
        assert current["calibration_status_display"] == "待校准"
        assert current["device_mode"] == "unmanaged"
        assert current["device_mode_display"] == "未托管"

    asyncio.run(scenario())


def test_heartbeat_recovery_restores_matching_calibration_without_recalibration(tmp_path, monkeypatch):
    """daemon 恢复但会话未变化时，不应把已完成校准永久留在待校准。"""
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")

    class RecoveringDaemonBridge:
        async def read_state(self):
            return {
                "connected": True,
                "session": "session-same",
                "estop_latched": False,
            }

    async def scenario():
        await manager.register_and_selfcheck(_cfg())
        device = manager.get("arm-hot-01")
        device.update({
            "driver_mode": "daemon",
            "presence_state": "present",
            "status": "offline",
            "status_display": "离线",
            "daemon_session": "session-same",
            "calibration_status": "due",
            "device_mode": "unmanaged",
            "camera_id": "cam-hot-01",
            "camera_device_unique_id": "physical-cam-01",
            "binding_revision": 1,
            "daemon_addr": "127.0.0.1:8795",
            "expected_frame_width": 1280,
            "expected_frame_height": 720,
        })
        manager._store.save_device(device)
        manager._arms["arm-hot-01"] = RecoveringDaemonBridge()

        monkeypatch.setattr(
            "drivers.calibration_bundle.load_bundle",
            lambda device_id, data_root=None: {
                "status": "ready",
                "deviceId": device_id,
                "cameraId": "cam-hot-01",
                "cameraDeviceUniqueId": "physical-cam-01",
                "bindingRevision": 1,
                "daemonAddr": "127.0.0.1:8795",
                "daemonSession": "session-same",
                "frame": {"width": 1280, "height": 720},
            },
        )

        await manager._heartbeat_device("arm-hot-01")

        current = manager.get("arm-hot-01")
        assert current["status"] == "ready"
        assert current["calibration_status"] == "validated"
        assert current["device_mode"] == "ready_manual"

    asyncio.run(scenario())


def test_camera_fault_revokes_execution_without_invalidating_calibration(tmp_path):
    """摄像头异常暂停执行，但不能把已完成的几何校准改成待校准。"""
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")

    async def scenario():
        await manager.register_and_selfcheck(_cfg(calibration_status="validated"))
        device = manager.get("arm-hot-01")
        device["status"] = "ready"
        device["device_mode"] = "ready_manual"
        manager._store.save_device(device)

        manager.revoke_execution_mode("arm-hot-01", "摄像头画面异常")

        current = manager.get("arm-hot-01")
        assert current["device_mode"] == "unmanaged"
        assert current["calibration_status"] == "validated"

    asyncio.run(scenario())


def test_remove_and_reattach_disconnect_old_arm_and_revoke_session(tmp_path):
    from drivers.daemon_arm import DaemonArmDriver
    from runtime.leases import LeaseManager

    class FakeTransport:
        def __init__(self):
            self.closed = False

        def is_open(self):
            return not self.closed

        def close(self):
            self.closed = True

    async def scenario():
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events)
        leases = LeaseManager(store, manager)
        manager.set_lease_manager(leases)
        cfg = _cfg(calibration_status="validated")
        await manager.register_and_selfcheck(cfg)
        for operation in ("attach", "remove"):
            arm = DaemonArmDriver("arm-hot-01")
            transport = FakeTransport()
            arm._transport = transport
            arm._connected = True
            arm._daemon_session = "old-session"
            manager._arms["arm-hot-01"] = arm
            device = manager.get("arm-hot-01")
            device.update(daemon_session="old-session", calibration_status="validated")
            leases.acquire("arm-hot-01", "fake-task")
            generation = arm._stop_generation
            if operation == "attach":
                await manager.attach_device(cfg)
            else:
                await manager.remove_device("arm-hot-01")
            assert transport.closed
            assert arm._stop_generation > generation
            assert not arm.connected
            assert leases.get("arm-hot-01") is None
            current = manager.get("arm-hot-01")
            assert current.get("daemon_session") is None
            assert current["calibration_status"] == "due"
            assert current["device_mode"] == "unmanaged"
        manager.mark_daemon_unavailable("arm-hot-01", "late process exit")
        assert manager.get("arm-hot-01")["presence_state"] == "removed"
        await asyncio.sleep(0)

    asyncio.run(scenario())


@pytest.mark.parametrize("session", ["new-session", None])
def test_heartbeat_session_boundary_releases_old_lease(tmp_path, monkeypatch, session):
    from runtime.leases import LeaseManager

    class Bridge:
        async def read_state(self):
            return {"connected": True, "session": session, "estop_latched": False}

    monkeypatch.setattr("drivers.calibration_bundle.load_bundle", lambda *args: None)

    async def scenario():
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events)
        leases = LeaseManager(store, manager)
        manager.set_lease_manager(leases)
        await manager.register_and_selfcheck(_cfg(calibration_status="validated"))
        device = manager.get("arm-hot-01")
        device.update(daemon_session="old-session", driver_mode="daemon")
        leases.acquire("arm-hot-01", "fake-task")
        manager._arms["arm-hot-01"] = Bridge()
        await manager._heartbeat_device("arm-hot-01")
        assert leases.get("arm-hot-01") is None
        assert device["daemon_session"] == session
        assert device["calibration_status"] == "due"
        assert device["device_mode"] == "unmanaged"
        if session is None:
            assert device["status"] == "offline"
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["remove", "daemon_exit"])
def test_stale_heartbeat_cannot_undo_lifecycle_invalidation(tmp_path, boundary):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class Bridge:
            async def read_state(self):
                started.set()
                await release.wait()
                return {"connected": True, "session": "old-session"}

            async def disconnect(self):
                pass

        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events)
        await manager.register_and_selfcheck(_cfg(calibration_status="validated"))
        manager._arms["arm-hot-01"] = Bridge()
        pending = asyncio.create_task(manager._heartbeat_device("arm-hot-01"))
        await started.wait()
        if boundary == "remove":
            await manager.remove_device("arm-hot-01")
        else:
            manager.mark_daemon_unavailable("arm-hot-01", "fake exit")
        release.set()
        await pending
        device = manager.get("arm-hot-01")
        assert device["status"] == "offline"
        assert device["calibration_status"] == "due"
        assert device.get("daemon_session") is None
    asyncio.run(scenario())
