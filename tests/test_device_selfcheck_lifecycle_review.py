"""Camera polls from an old attachment must not update the replacement."""
from __future__ import annotations

import asyncio

from drivers.fake_arm import FakeArmDriver
from drivers.fake_camera import FakeCameraDriver
from drivers.fake_input import FakeInputDriver
from runtime import devices as devices_module
from runtime.devices import DeviceManager
from runtime.store import Store
from runtime.util import iso, utc_now


DEVICE_ID = "lifecycle-review"
FIELDS = (
    "camera_binding_revision",
    "camera_quality",
    "current_ui_state",
    "status",
    "device_mode",
    "pause_reason",
    "calibration_status",
)


class MemoryAudit:
    def record(self, *args, **kwargs):
        pass


class MemoryEvents:
    def publish(self, *args, **kwargs):
        pass


class SourceCamera(FakeCameraDriver):
    def __init__(self, cfg):
        super().__init__(
            cfg["camera_id"],
            ui_state=cfg["sim_ui_state"],
            quality=cfg["sim_quality"],
        )
        self.source_revision = cfg["source_revision"]
        self.pause_at = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def last_source_info(self):
        return {
            "camera_id": self.device_id,
            "binding_revision": self.source_revision,
            "source": "camera_helper",
            "received_at": iso(utc_now()),
        }

    async def check_quality(self):
        if self.pause_at == "quality":
            self.entered.set()
            await self.release.wait()
        return await super().check_quality()

    async def identify_ui_state(self):
        if self.pause_at == "ui":
            self.entered.set()
            await self.release.wait()
        return await super().identify_ui_state()


def _cfg(camera_id, revision, quality, ui_state):
    return {
        "device_id": DEVICE_ID,
        "camera_id": camera_id,
        "source_revision": revision,
        "sim_quality": quality,
        "sim_ui_state": ui_state,
        "calibration_status": "validated",
    }


def _state(device):
    return {field: device.get(field) for field in FIELDS}


async def _reattach_during_camera_poll(monkeypatch, boundary, tmp_path):
    store = Store(tmp_path / "fleet.db")
    manager = DeviceManager(
        store, MemoryAudit(), MemoryEvents(), hardware_mode="simulation"
    )

    def fake_drivers(_manager, cfg):
        return (
            FakeArmDriver(cfg["device_id"]),
            SourceCamera(cfg),
            FakeInputDriver(cfg["device_id"]),
            "isolated simulation",
            "simulation",
        )

    monkeypatch.setattr(DeviceManager, "_create_drivers", fake_drivers)

    # Signal at the end of exactly one poll instead of waiting for the real
    # ten-second background interval or observing a partially written result.
    poll_finished = asyncio.Event()

    class PollClock:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            if delay == 10:
                poll_finished.set()
            else:
                await asyncio.sleep(delay)

    monkeypatch.setattr(devices_module, "asyncio", PollClock())

    await manager.register_and_selfcheck(
        _cfg("camera-old", 2, "clear", "wechat_main")
    )
    old_camera = manager.camera(DEVICE_ID)
    old_camera.pause_at = boundary
    manager._running = True
    poll = asyncio.create_task(manager._camera_loop())
    try:
        await asyncio.wait_for(old_camera.entered.wait(), timeout=2)
        await manager.attach_device(
            _cfg("camera-new", 3, "waiting", "unknown")
        )
        assert manager.camera(DEVICE_ID) is not old_camera
        expected = _state(manager._devices[DEVICE_ID])
        assert expected == {
            "camera_binding_revision": 3,
            "camera_quality": "waiting",
            "current_ui_state": "unknown",
            "status": "paused",
            "device_mode": "unmanaged",
            "pause_reason": "设备绑定已变化，等待重新校准",
            "calibration_status": "due",
        }
        assert _state(store.get_device(DEVICE_ID)) == expected

        manager._running = False
        old_camera.release.set()
        await asyncio.wait_for(poll_finished.wait(), timeout=2)
        await asyncio.wait_for(poll, timeout=2)

        assert _state(manager._devices[DEVICE_ID]) == expected
        assert _state(store.get_device(DEVICE_ID)) == expected
    finally:
        manager._running = False
        old_camera.release.set()
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)


def test_reattach_during_quality_cannot_overwrite_new_device(monkeypatch, tmp_path):
    asyncio.run(_reattach_during_camera_poll(monkeypatch, "quality", tmp_path))


def test_reattach_during_ui_cannot_overwrite_new_persisted_device(monkeypatch, tmp_path):
    asyncio.run(_reattach_during_camera_poll(monkeypatch, "ui", tmp_path))
