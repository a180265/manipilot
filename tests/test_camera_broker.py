"""摄像头并发保护测试（实施文档 5.7.1：每摄像头唯一物理采集入口）。"""
from __future__ import annotations

import asyncio
import time

import cv2
import numpy as np
from drivers.camera_broker import BrokerEvent, CameraFrameBroker
from drivers.real_camera import RealCameraDriver
from runtime.util import iso, sha256_bytes, utc_now


class GrabbingDriver:
    """带并发观测的假驱动：capture_sync 慢速执行以暴露并发。"""

    def __init__(self, *, fail: bool = False):
        self.count = 0
        self.active = 0
        self.max_active = 0
        self.fail = fail

    def capture_sync(self) -> dict:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        self.active -= 1
        if self.fail:
            raise RuntimeError("USB 断开")
        image = np.full((8, 8, 3), self.count, dtype=np.uint8)
        ok, png = cv2.imencode(".png", image)
        assert ok
        self.count += 1
        return {"image_bytes": png.tobytes(), "sha256": sha256_bytes(png.tobytes()),
                "captured_at": iso(utc_now()), "frozen": False}


def _run(coro):
    return asyncio.run(coro)


def test_concurrent_captures_are_serialized():
    driver = GrabbingDriver()
    broker = CameraFrameBroker(driver, camera_id="cam-01")

    async def scenario():
        return await asyncio.gather(*[broker.capture() for _ in range(8)])

    frames = _run(scenario())
    assert len(frames) == 8
    assert driver.count == 8
    assert driver.max_active == 1  # 底层采集严格串行


def test_sync_and_async_capture_share_the_same_physical_lock():
    """实时查看/证据的同步取帧不能和监听异步取帧并发进入驱动。"""
    driver = GrabbingDriver()
    broker = CameraFrameBroker(driver, camera_id="cam-01")

    async def scenario():
        return await asyncio.gather(
            broker.capture(),
            asyncio.to_thread(broker.capture_sync),
            broker.capture(),
            asyncio.to_thread(broker.capture_sync),
        )

    frames = _run(scenario())
    assert len(frames) == 4
    assert driver.count == 4
    assert driver.max_active == 1
    assert broker.max_concurrent_grabs == 1


def test_latest_frame_served_without_new_grab():
    driver = GrabbingDriver()
    broker = CameraFrameBroker(driver, camera_id="cam-01")

    async def scenario():
        await broker.capture()
        latest = [await broker.latest_or_capture() for _ in range(5)]
        return latest

    latest = _run(scenario())
    assert driver.count == 1  # 最近帧缓存直接服务，不驱动底层采集
    assert broker.latest is not None
    assert all(frame["sha256"] == latest[0]["sha256"] for frame in latest)


def test_stale_cached_frame_is_not_reused():
    """缓存超过年龄阈值时必须重新取帧，不能把旧画面冒充实时。"""
    driver = GrabbingDriver()
    broker = CameraFrameBroker(driver, camera_id="cam-01", max_age_seconds=0.01)

    async def scenario():
        await broker.capture()
        broker._latest["captured_at"] = "2000-01-01T00:00:00+00:00"
        return await broker.latest_or_capture()

    frame = _run(scenario())
    assert frame["sha256"]
    assert driver.count == 2


def test_unavailable_broadcast_and_recovery():
    driver = GrabbingDriver(fail=True)
    broker = CameraFrameBroker(driver, camera_id="cam-01")
    events: list[BrokerEvent] = []
    broker.subscribe(events.append)

    async def scenario():
        try:
            await broker.capture()
        except RuntimeError:
            pass
        driver.fail = False
        return await broker.capture()

    frame = _run(scenario())
    statuses = [event.status for event in events]
    assert "camera_unavailable" in statuses  # 断开时所有订阅者收到同一状态
    assert "camera_recovered" in statuses
    assert broker.unavailable is False
    assert frame["sha256"]


def test_check_quality_reuses_driver_metrics_without_extra_grab():
    provider_frames = {"n": 0}

    def provider():
        provider_frames["n"] += 1
        return np.full((48, 64, 3), 200, dtype=np.uint8)

    driver = RealCameraDriver("arm-01", frame_provider=provider)
    broker = CameraFrameBroker(driver, camera_id="cam-01")

    async def scenario():
        await broker.capture()
        return await broker.check_quality()

    # An unlocatable bright scene must not be treated as a usable phone frame.
    quality = _run(scenario())
    assert quality["quality"] == "abnormal"
    assert quality["quality_display"] == "画面异常"
    assert "无法定位" in quality["note"]
    assert provider_frames["n"] == 1  # 质量判定复用最近帧，未再次驱动采集


def test_check_quality_uses_the_bound_phone_geometry(monkeypatch):
    image = np.full((240, 320, 3), 220, dtype=np.uint8)
    image[30:210, 95:225] = 0
    driver = RealCameraDriver("cam-01", frame_provider=lambda: image)
    broker = CameraFrameBroker(driver, camera_id="cam-01")
    calls = []
    corners = [[95, 30], [224, 30], [224, 209], [95, 209]]

    def geometry(frame):
        calls.append(frame["sha256"])
        return corners, 8.0

    monkeypatch.setattr(driver, "_quality_geometry", geometry)

    async def scenario():
        await broker.capture()
        return await broker.check_quality()

    quality = _run(scenario())
    assert len(calls) == 1
    assert quality["quality"] == "waiting"
    assert quality["quality_display"] == "等待画面恢复"
    assert quality["metrics"]["brightness"] == 0
    assert broker.grab_count == 1


def test_quality_binding_failure_does_not_fall_back_to_environment(monkeypatch):
    driver = RealCameraDriver(
        "cam-01", frame_provider=lambda: np.zeros((240, 320, 3), dtype=np.uint8))
    broker = CameraFrameBroker(driver, camera_id="cam-01")

    def wrong_binding(frame):
        raise RuntimeError("画面与校准绑定不符")

    monkeypatch.setattr(driver, "_quality_geometry", wrong_binding)
    quality = _run(broker.check_quality())
    assert quality["quality"] == "abnormal"
    assert "绑定不符" in quality["note"]
