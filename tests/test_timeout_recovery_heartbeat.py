import asyncio

import pytest

from runtime.audit import AuditLog
from runtime.devices import DeviceManager
from runtime.errors import ApiError
from runtime.events import EventBus
from runtime.store import Store
from tests.test_hotplug_device_management import _cfg


def test_retained_timeout_pauses_without_changing_session(tmp_path):
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")

    class FaultedBridge:
        async def read_state(self):
            return {"connected": True, "session": "same-session", "daemon_estop_locked": True,
                    "communication_recovery_required": True, "recovery_id": "fault-1",
                    "recovery_reason": "回包超时", "position": None}

    async def scenario():
        await manager.register_and_selfcheck(_cfg())
        device = manager.get("arm-hot-01")
        device.update(daemon_session="same-session", home_ok=True, driver_mode="daemon")
        manager._arms["arm-hot-01"] = FaultedBridge()
        await manager._heartbeat_device("arm-hot-01")
        assert device["daemon_session"] == "same-session"
        assert device["status"] == "paused"
        assert device["home_ok"] is False
        assert device["communication_recovery_id"] == "fault-1"
        with pytest.raises(ApiError, match="通信"):
            manager.resume("arm-hot-01", "人工恢复")

    asyncio.run(scenario())


def test_pending_operator_action_survives_registration_and_blocks_resume(tmp_path):
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
    device = manager.register_device(_cfg())
    device.update(operator_recovery_pending=True, home_ok=True)
    store.save_device(device)
    restarted = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
    new_device = restarted.register_device(_cfg())
    assert new_device["operator_recovery_pending"] is True
    with pytest.raises(ApiError, match="停车"):
        restarted.resume("arm-hot-01", "恢复")
    with pytest.raises(ApiError):
        restarted._set_status("arm-hot-01", "ready")
