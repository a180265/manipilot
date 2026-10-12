"""设备注册自检的离线鲁棒性。

机械臂与摄像头都不可用时（如部署初期未接硬件），自检必须把设备如实
保持在离线并留告警，不允许状态迁移异常炸掉整个 8788 启动。
"""
from __future__ import annotations

import asyncio

import numpy as np

from drivers.real_camera import RealCameraDriver
from runtime.audit import AuditLog
from runtime.devices import DeviceManager
from runtime.events import EventBus
from runtime.store import Store
from runtime.util import iso, utc_now


def _cfg(device_id: str = "arm-x") -> dict:
    return {"device_id": device_id, "camera_id": f"cam-{device_id}",
            "ime_device_id": f"ime-{device_id}"}


def test_camera_failure_after_arm_offline_keeps_offline(tmp_path, monkeypatch):
    from drivers.fake_arm import FakeArmDriver
    from drivers.fake_camera import FakeCameraDriver

    async def fail_connect(self):
        raise RuntimeError("daemon 不可达（未接机械臂）")

    async def fail_open(self):
        raise RuntimeError("摄像头采集异常：拒绝（未接摄像头）")

    monkeypatch.setattr(FakeArmDriver, "connect", fail_connect)
    monkeypatch.setattr(FakeCameraDriver, "open", fail_open)

    async def scenario():
        store = Store(tmp_path / "offline.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events,
                                hardware_mode="simulation")
        device = await manager.register_and_selfcheck(_cfg())
        assert device["status"] == "offline"  # 如实离线，不炸启动
        assert manager.get("arm-x")["status"] == "offline"
        alerts = store.all_alerts()
        assert any("摄像头" in (alert.get("message") or "")
                   for alert in alerts)
        assert any("机械臂" in (alert.get("message") or "")
                   for alert in alerts)

    asyncio.run(scenario())


def test_camera_failure_alone_degrades(tmp_path, monkeypatch):
    """只有摄像头失败时仍应降级（保持既有语义）。"""
    from drivers.fake_camera import FakeCameraDriver

    async def fail_open(self):
        raise RuntimeError("摄像头采集异常：拒绝")

    monkeypatch.setattr(FakeCameraDriver, "open", fail_open)

    async def scenario():
        store = Store(tmp_path / "degraded.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events,
                                hardware_mode="simulation")
        device = await manager.register_and_selfcheck(_cfg("arm-y"))
        assert device["status"] == "degraded"

    asyncio.run(scenario())


def test_offline_startup_records_fresh_camera_binding(tmp_path, monkeypatch):
    from drivers.fake_arm import FakeArmDriver
    from drivers.fake_camera import FakeCameraDriver

    async def fail_connect(self):
        raise RuntimeError("控制器会话不可用")

    monkeypatch.setattr(FakeArmDriver, "connect", fail_connect)
    monkeypatch.setattr(FakeCameraDriver, "last_source_info", lambda self: {
        "camera_id": self.device_id, "binding_revision": 2,
        "received_at": iso(utc_now()), "source": "camera_helper",
    }, raising=False)

    async def scenario():
        store = Store(tmp_path / "offline-camera.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events,
                                hardware_mode="simulation")
        device = await manager.register_and_selfcheck(_cfg())
        assert device["status"] == "offline"
        assert device["binding_revision"] == 1
        assert device["camera_binding_revision"] == 2
        assert device["camera_source"] == "camera_helper"
        eligibility = manager.eligibility("arm-x")
        assert eligibility["can_view"]
        assert not eligibility["can_accept_task"]

    asyncio.run(scenario())


def test_localization_failure_still_updates_camera_source(tmp_path):
    class FreshSource:
        stream_alive = True
        last_camera_id = "cam-arm-x"
        last_device_id = "arm-x"
        last_device_unique_id = "camera-physical-x"
        last_binding_revision = 2
        last_frame_seq = 10
        last_frame_source = "camera_helper"
        legacy_mode = False

        def __call__(self):
            self.last_received_at = iso(utc_now())
            self.last_captured_at = self.last_received_at
            return np.full((240, 320, 3), 200, dtype=np.uint8)

    async def scenario():
        store = Store(tmp_path / "localization-camera.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events,
                                hardware_mode="simulation")
        device = await manager.register_and_selfcheck(_cfg())
        device.update(status="offline", status_display="离线",
                      pause_reason="原任务待核对", camera_quality="clear",
                      last_camera_check_at=None)
        manager._cameras["arm-x"] = RealCameraDriver(
            "cam-arm-x", frame_provider=FreshSource())
        manager._running = True
        task = asyncio.create_task(manager._camera_loop())
        try:
            for _ in range(100):
                await asyncio.sleep(.01)
                if device.get("last_camera_check_at") is not None:
                    await asyncio.sleep(.01)
                    break
            else:
                raise AssertionError("定位异常阻断了相机状态更新")
            assert device["camera_quality"] == "abnormal"
            assert device["camera_binding_revision"] == 2
            assert device["camera_frame_age_ms"] < 3000
            assert device["status"] == "offline"
            assert device["pause_reason"] == "原任务待核对"
            assert manager.eligibility("arm-x")["can_view"]
            assert not manager.eligibility("arm-x")["can_accept_task"]
        finally:
            manager._running = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
