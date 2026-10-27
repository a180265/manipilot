"""Calibration publication must refresh the mirror, not grant execution mode."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from drivers import calibration_bundle as cb
from runtime.audit import AuditLog
from runtime.devices import DeviceManager
from runtime.events import EventBus
from runtime.status import DEVICE_MODE
from runtime.store import Store
from tests.test_live_calibration_gate import ready_bundle


def _manager(tmp_path):
    store = Store(tmp_path / "fleet.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="real")
    bundle = ready_bundle()
    cb.save_bundle(bundle, tmp_path)
    device = {
        "device_id": "arm-01", "binding_mode": "real", "driver_mode": "daemon",
        "camera_id": bundle["cameraId"],
        "camera_device_unique_id": bundle["cameraDeviceUniqueId"],
        "binding_revision": 1, "camera_binding_revision": bundle["bindingRevision"],
        "daemon_addr": bundle["daemonAddr"], "daemon_session": bundle["daemonSession"],
        "expected_frame_width": 64, "expected_frame_height": 48,
        "status": "ready", "presence_state": "present",
        "calibration_status": "due", "calibration_validated_at": None,
        "device_mode": "unmanaged", "device_mode_display": DEVICE_MODE["unmanaged"],
        "estop_latched": False, "lease_id": None,
    }

    async def read_state():
        return {"connected": True, "session": bundle["daemonSession"],
                "estop_latched": device["estop_latched"]}

    manager._devices["arm-01"] = device
    manager._arms["arm-01"] = SimpleNamespace(
        connected=True, data_root=tmp_path, read_state=read_state)
    store.save_device(device)
    return manager, device, bundle


def test_read_refreshes_completed_calibration_without_restoring_execution(tmp_path):
    manager, device, bundle = _manager(tmp_path)
    before = cb.bundle_path("arm-01", tmp_path).read_bytes()

    result = manager.get("arm-01")

    assert result["calibration_checks"] == []
    assert result["calibration_status"] == "validated"
    assert result["calibration_status_display"] == "已校准"
    assert result["calibration_validated_at"] == bundle["updatedAt"]
    assert result["device_mode"] == "unmanaged"
    assert result["status"] == "ready"
    assert result["binding_revision"] == 1
    assert cb.bundle_path("arm-01", tmp_path).read_bytes() == before


@pytest.mark.parametrize("status", ["paused", "quarantined", "maintenance"])
@pytest.mark.parametrize("serial_connected", [True, False])
def test_heartbeat_reconnects_bridge_without_releasing_operator_hold(
        tmp_path, status, serial_connected):
    """Startup before manual CONNECT must not strand a newly calibrated held device."""
    manager, device, bundle = _manager(tmp_path)
    device.update(status=status, daemon_session=None, pause_reason="operator hold",
                  calibration_marked_status="validated")
    bridge = manager._arms["arm-01"]
    bridge.connected = False
    calls = []

    async def read_state():
        return {"connected": bridge.connected,
                "session": bundle["daemonSession"] if bridge.connected else None,
                "estop_latched": False}

    async def connect():
        calls.append("connect")
        if not serial_connected:
            raise RuntimeError("daemon serial is not connected")
        bridge.connected = True
        return {"connected": True, "session": bundle["daemonSession"]}

    bridge.read_state, bridge.connect = read_state, connect
    before = cb.bundle_path("arm-01", tmp_path).read_bytes()
    assert manager.get("arm-01")["calibration_checks"][0]["code"] == "daemon_session_unavailable"

    asyncio.run(manager._heartbeat_device("arm-01"))

    current = manager.get("arm-01")
    assert calls == ["connect"]
    assert current["calibration_status"] == ("validated" if serial_connected else "due")
    assert current["daemon_session"] == (bundle["daemonSession"] if serial_connected else None)
    assert current["status"] == status
    assert current["pause_reason"] == "operator hold"
    assert current["device_mode"] == "unmanaged"
    assert cb.bundle_path("arm-01", tmp_path).read_bytes() == before


def test_heartbeat_uses_camera_binding_revision_like_registered_gate(tmp_path):
    manager, device, _bundle = _manager(tmp_path)

    asyncio.run(manager._heartbeat_device("arm-01"))

    assert device["calibration_status"] == "validated"
    assert device["device_mode"] == "ready_manual"
    assert device["binding_revision"] == 1
    assert device["camera_binding_revision"] == 7


@pytest.mark.parametrize("status", ["paused", "quarantined", "maintenance", "busy"])
def test_calibration_refresh_never_releases_safety_or_busy_state(tmp_path, status):
    manager, device, _bundle = _manager(tmp_path)
    device.update(status=status, pause_reason="manual", quarantine_reason="unknown")
    asyncio.run(manager._heartbeat_device("arm-01"))
    result = manager.get("arm-01")
    assert result["calibration_status"] == "validated"
    assert result["status"] == status
    assert result["device_mode"] == "unmanaged"
    assert result["pause_reason"] == "manual"
    assert result["quarantine_reason"] == "unknown"


@pytest.mark.parametrize("broken", ["session", "camera_revision", "disconnected", "invalid_z"])
def test_nonmatching_archive_stays_due(tmp_path, broken):
    manager, device, bundle = _manager(tmp_path)
    if broken == "session":
        device["daemon_session"] = "different-session"
    elif broken == "camera_revision":
        device["camera_binding_revision"] = 8
    elif broken == "disconnected":
        manager._arms["arm-01"].connected = False
    else:
        bundle["z"]["validated"] = False
        cb.save_bundle(bundle, tmp_path)
    result = manager.get("arm-01")
    assert result["calibration_status"] == "due"
    assert result["calibration_checks"]
    assert result["device_mode"] == "unmanaged"


def test_validated_unmanaged_mode_is_not_implicitly_enabled_by_heartbeat(tmp_path):
    manager, device, _bundle = _manager(tmp_path)
    device["calibration_status"] = "validated"
    asyncio.run(manager._heartbeat_device("arm-01"))
    assert device["device_mode"] == "unmanaged"


@pytest.mark.parametrize("marked", ["due", "unknown"])
def test_explicit_manual_calibration_hold_survives_refresh(tmp_path, marked):
    manager, device, _bundle = _manager(tmp_path)
    manager.set_calibration_status("arm-01", marked, "manual verification required")
    asyncio.run(manager._heartbeat_device("arm-01"))
    result = manager.get("arm-01")
    assert result["calibration_status"] == marked
    assert result["device_mode"] == "unmanaged"
    assert result["calibration_validated_at"] is None


def test_wake_has_separate_gate_and_reuses_existing_camera_broker(tmp_path, monkeypatch):
    from tests.test_device_calibration_state import device_client

    calls = []

    async def gate(**kwargs):
        calls.append(kwargs)
        return [{"code": "camera_waiting", "message": "waiting"}], {}, None

    monkeypatch.setattr("runtime.calibration_gate.check_live_calibration", gate)
    with device_client(tmp_path, monkeypatch) as (_client, runtime, _daemon, _archive):
        arm = runtime.devices._arms["arm-01"]
        result = asyncio.run(arm.screen_wake_gate())
        assert result == ([{"code": "camera_waiting", "message": "waiting"}], {}, None)
        assert calls[-1]["screen_wake"] is True
        assert "task_id" not in calls[-1]
        assert calls[-1]["camera"] is runtime.devices.camera_broker("arm-01")
        assert calls[-1]["bundle"]["daemonSession"] == calls[-1]["daemon_session"]
        asyncio.run(arm.live_calibration_gate())
        assert "screen_wake" not in calls[-1]
        assert "task_id" not in calls[-1]
