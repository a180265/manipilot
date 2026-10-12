"""新鲜黑帧只等待：临时库、合成画面、假机械臂，不连接现场。"""
import asyncio
from copy import deepcopy
from pathlib import Path

import cv2
import numpy as np
import pytest

from drivers.real_camera import RealCameraDriver
from runtime.device_eligibility import compute_device_eligibility
from runtime.errors import ApiError
from tests.helpers import make_device_configs, make_runtime, probe_payload


class FreshFrames:
    stream_alive = True

    def __init__(self):
        self.frame = phone_frame(lit=False)

    def __call__(self):
        return self.frame


def phone_frame(*, lit: bool, background: int = 65):
    image = np.full((240, 320, 3), background, np.uint8)
    image[30:210, 95:225] = (
        np.random.default_rng(23).integers(90, 230, (180, 130, 3), dtype=np.uint8)
        if lit else 0)
    return image


def encoded(image):
    ok, data = cv2.imencode(".png", image)
    assert ok
    return data.tobytes()


def test_quality_samples_phone_instead_of_dark_surroundings():
    image = phone_frame(lit=True, background=0)
    metrics = RealCameraDriver._analyze(encoded(image),
                                        expected_corners=[[95, 30], [224, 30],
                                                          [224, 209], [95, 209]])
    assert metrics["brightness"] > 90
    assert RealCameraDriver._classify(metrics, False) == "clear"


def test_quality_locates_rounded_lit_screen_on_black_background():
    image = np.zeros((240, 320, 3), np.uint8)
    mask = np.zeros(image.shape[:2], np.uint8)
    cv2.rectangle(mask, (42, 32), (278, 208), 255, -1)
    for x in (42, 278):
        for y in (32, 208):
            cv2.circle(mask, (x, y), 14, 0, -1)
    image[mask != 0] = np.random.default_rng(8).integers(
        30, 80, (int((mask != 0).sum()), 3), dtype=np.uint8)
    metrics = RealCameraDriver._analyze(encoded(image))
    assert RealCameraDriver._classify(metrics, False) == "clear"


def test_quality_classifies_supplied_lit_phone_photo_when_available():
    sample = Path("/tmp/wx-camera-check-20260929.jpg")
    if not sample.is_file():
        pytest.skip("local camera photo not present")
    metrics = RealCameraDriver._analyze(sample.read_bytes())
    assert metrics["brightness"] > RealCameraDriver.BRIGHTNESS_BLOCKED_MAX
    assert RealCameraDriver._classify(metrics, False) == "clear"


def test_quality_waits_for_located_black_phone_instead_of_bright_surroundings():
    image = phone_frame(lit=False, background=220)
    metrics = RealCameraDriver._analyze(encoded(image),
                                        expected_corners=[[95, 30], [224, 30],
                                                          [224, 209], [95, 209]])
    assert metrics["brightness"] < 18
    assert RealCameraDriver._classify(metrics, False) == "waiting"


def test_quality_rejects_larger_surrounding_rectangle_when_phone_not_fittable():
    image = np.zeros((480, 640, 3), np.uint8)
    image[20:460, 30:610] = 180
    image[100:380, 190:450] = 0
    # The saved phone location does not match the rectangle now visible;
    # background pixels must not become a substitute screen ROI.
    saved = [[120, 80], [420, 80], [420, 400], [120, 400]]
    with pytest.raises(RuntimeError, match="无法定位"):
        RealCameraDriver._analyze(encoded(image), expected_corners=saved,
                                  require_phone=True)


def test_quality_requires_rigid_phone_edge_even_if_region_is_near_saved():
    # 测试更正（§4.5，2026-10-03）：§7.1 开灯常态回退——_analyze 在预测邻域
    # （每边 ±32px 梯度带）按真实屏幕边定位（"边就是边"），带内漂移不再
    # 一票拒绝；ROI 必须采自真实屏幕边（不是背景替代），带外位移仍拒绝。
    image = np.zeros((240, 320, 3), np.uint8)
    image[30:210, 95:235] = 180  # 右边比 saved 漂 10px（带内）
    saved = [[95, 30], [224, 30], [224, 209], [95, 209]]
    metrics = RealCameraDriver._analyze(encoded(image), expected_corners=saved,
                                        require_phone=True)
    assert metrics["brightness"] == 180, "ROI 必须取自真实屏幕边内的亮屏"
    gone = np.zeros((240, 320, 3), np.uint8)
    gone[30:210, 128:268] = 180  # 整体右移 33px（带外）：背景不得替代屏幕
    with pytest.raises(RuntimeError, match="无法定位"):
        RealCameraDriver._analyze(encoded(gone), expected_corners=saved,
                                  require_phone=True)


@pytest.mark.parametrize("size", [(64, 48), (96, 72), (128, 96)])
@pytest.mark.parametrize("border", [2, 4, 8, 10])
def test_quality_waits_on_small_dark_phone_inside_bright_border(size, border):
    width, height = size
    image = np.full((height, width, 3), 30, np.uint8)
    image[border:height - border, border:width - border] = 220
    left, top = width // 4, height // 4
    right, bottom = width * 3 // 4, height * 3 // 4
    image[top:bottom, left:right] = 0
    corners = [[left, top], [right - 1, top],
               [right - 1, bottom - 1], [left, bottom - 1]]
    metrics = RealCameraDriver._analyze(encoded(image), expected_corners=corners)
    assert RealCameraDriver._classify(metrics, False) == "waiting"


def test_quality_cannot_infer_a_phone_from_featureless_or_misplaced_region():
    corners = [[95, 30], [224, 30], [224, 209], [95, 209]]
    black = encoded(np.zeros((240, 320, 3), np.uint8))
    assert RealCameraDriver._classify(RealCameraDriver._analyze(black), False) == "waiting"
    assert RealCameraDriver._classify(
        RealCameraDriver._analyze(black, expected_corners=corners), False) == "waiting"
    with pytest.raises(RuntimeError, match="无法定位"):
        RealCameraDriver._analyze(black, expected_corners=corners, require_phone=True)
    misplaced = np.full((240, 320, 3), 220, np.uint8)
    misplaced[30:210, 15:145] = 0
    with pytest.raises(RuntimeError, match="无法定位"):
        RealCameraDriver._analyze(encoded(misplaced), expected_corners=corners)
    with pytest.raises(RuntimeError, match="无法定位"):
        RealCameraDriver._analyze(encoded(np.full((240, 320, 3), 120, np.uint8)))


def test_unlocatable_bright_frame_does_not_reach_ui_matching():
    camera = RealCameraDriver(
        "cam-test", frame_provider=lambda: np.full((240, 320, 3), 120, np.uint8))
    camera.register_reference("known", encoded(np.full((240, 320, 3), 120, np.uint8)))
    result = asyncio.run(camera.identify_ui_state())
    assert result["ui_state"] == "unknown"
    assert result["confidence"] == 0
    assert "无法定位" in result["note"]


def test_fresh_black_frames_wait_and_recover_without_reopening():
    async def scenario():
        source = FreshFrames()
        camera = RealCameraDriver("cam-test", frame_provider=source)
        for _ in range(3):
            quality = await camera.check_quality()
            assert quality["quality"] == "waiting"
            assert quality["quality_display"] == "等待画面恢复"
            assert quality["frozen"] is False
        source.frame = phone_frame(lit=True)
        assert (await camera.check_quality())["quality"] == "clear"
        source.stream_alive = False
        assert (await camera.check_quality())["quality"] == "frozen"

    asyncio.run(scenario())


def test_black_but_frozen_is_not_a_waiting_live_stream():
    camera = RealCameraDriver("cam-test", frame_provider=FreshFrames())
    metrics = {"brightness": 0, "texture": 0, "blur_variance": 0}
    assert camera._classify(metrics, True) == "frozen"


async def poll_once(manager):
    """Run the real monitor body, cancelling only its inter-poll sleep."""
    manager.get("arm-01")["last_camera_check_at"] = None
    task = asyncio.create_task(manager._camera_loop())
    manager._running = True
    before = manager.get("arm-01").get("last_camera_check_at")
    try:
        for _ in range(100):
            await asyncio.sleep(.01)
            if manager.get("arm-01").get("last_camera_check_at") != before:
                await asyncio.sleep(.01)
                return
        raise AssertionError("camera monitor did not complete")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        manager._running = False


def test_waiting_preserves_mode_calibration_and_holds_queue(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        manager = runtime.devices
        await manager.register_all(make_device_configs())
        device = manager.get("arm-01")
        device.update(device_mode="managed", device_mode_display="托管中")
        preserved = {k: deepcopy(device.get(k)) for k in
                     ("status", "status_reason", "daemon_session",
                      "calibration_status", "home_ok", "device_mode")}
        camera = manager.camera("arm-01")
        request = runtime.submit_task(probe_payload([{}]), source="console", context={})
        for _ in range(2):
            camera.set_quality("waiting")
            await poll_once(manager)
            assert {k: device.get(k) for k in preserved} == preserved
            assert manager.is_taskable("arm-01") == (False, "camera_waiting")
            eligibility = manager.eligibility("arm-01")
            assert not eligibility["can_accept_task"]
            assert eligibility["can_view"]
            assert not eligibility["can_calibrate"]
            assert "camera_waiting" in eligibility["blocking_checks"]
            assert not runtime.store.all_alerts()
            await runtime._dispatch_once()
            assert runtime.get_task(request["task_id"])["status"] == "queued"
            assert runtime.leases.get("arm-01") is None
            assert manager.get("arm-01")["current_ui_state"] == "unknown"
        camera.set_quality("clear")
        await poll_once(manager)
        assert manager.is_taskable("arm-01") == (True, None)
        assert manager.eligibility("arm-01")["can_accept_task"]
        assert {k: device.get(k) for k in preserved} == preserved

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["paused", "offline", "quarantined"])
def test_bright_frame_does_not_clear_other_safety_states(tmp_path, status):
    async def scenario():
        runtime = make_runtime(tmp_path)
        manager = runtime.devices
        await manager.register_all(make_device_configs())
        device = manager.get("arm-01")
        device.update(status=status, status_reason="人工核对原动作", home_ok=False)
        for quality in ("waiting", "clear"):
            manager.camera("arm-01").set_quality(quality)
            await poll_once(manager)
            assert device["status"] == status
            assert device["status_reason"] == "人工核对原动作"
            assert device["home_ok"] is False
            assert not manager.is_taskable("arm-01")[0]

    asyncio.run(scenario())


def test_real_fault_after_wait_still_alerts_and_revokes_mode(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        manager = runtime.devices
        await manager.register_all(make_device_configs())
        device = manager.get("arm-01")
        device.update(device_mode="managed")
        manager.camera("arm-01").set_quality("waiting")
        await poll_once(manager)
        manager.camera("arm-01").set_quality("frozen")
        await poll_once(manager)
        assert device["device_mode"] == "unmanaged"
        assert any("冻结" in a["message"] for a in runtime.store.all_alerts())

    asyncio.run(scenario())


def test_waiting_blocks_at_final_motion_gate_without_clearing_stop(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.devices.register_all(make_device_configs())
        task = runtime.submit_task(probe_payload([{}]), source="console", context={})
        lease = runtime.leases.acquire("arm-01", task["task_id"])
        task.update(status="executing", lease_id=lease["lease_id"])
        runtime.store.save_task(task)
        runtime.devices.get("arm-01").update(status="busy", camera_quality="waiting")
        with pytest.raises(ApiError, match="camera_waiting") as error:
            runtime._check_physical_preconditions(task["plugin_id"], task)
        assert error.value.motion_not_started
        # STOP is still allowed while image-based movements are held.
        result = await runtime.plugin_invoke_capability(task["plugin_id"], task, "arm.stop", {})
        assert result["stopped"]

    asyncio.run(scenario())


def test_startup_black_frame_is_waiting_not_degraded(tmp_path, monkeypatch):
    from drivers.fake_camera import FakeCameraDriver

    original = FakeCameraDriver.open

    async def open_black(self):
        self.set_quality("waiting")
        return await original(self)

    monkeypatch.setattr(FakeCameraDriver, "open", open_black)

    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.devices.register_all(make_device_configs())
        device = runtime.devices.get("arm-01")
        assert device["status"] == "ready"
        assert device["camera_quality"] == "waiting"
        assert not runtime.devices.is_taskable("arm-01")[0]
        assert not runtime.store.all_alerts()

    asyncio.run(scenario())
