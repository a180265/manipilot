"""F6 仿真手机几何：真实解码、定位、姿态拟合与光流证明六种像素场景。

对应《手机定位、校准持久化、黑屏唤醒与 WX 滑动修复验收规范》§10.2：
测量链与生产 runtime.measure_scroll 完全一致（locate_screen_quad →
fit_pose → align_screen → warpPerspective → measure_displacement），
不 mock 任何结论函数。
"""
import cv2
import numpy as np
import pytest

from drivers import fake_camera
from drivers.screen_edge import ScreenEdgeError, locate_screen_quad
from drivers.screen_pose import align_screen, fit_pose
from drivers.scroll_observation import measure_displacement

# §10.1 独立夹具几何：1280x720 帧、手机四角、屏幕 800x600、平移单应。
PHONE_CORNERS = [[80, 40], [880, 40], [880, 640], [80, 640]]
SCREEN_SIZE = (800, 600)
HOMOGRAPHY = np.array([[1, 0, -80], [0, 1, -40], [0, 0, 1]], dtype=float)
# 相机域 ROI（落在手机内部），转屏幕域后测位移。
CAMERA_ROI = {"x": 200, "y": 120, "width": 480, "height": 420}


def make_camera() -> fake_camera.FakeCameraDriver:
    camera = fake_camera.FakeCameraDriver("arm-01", width=1280, height=720)
    camera.configure_simulation_frame(
        1280, 720, screen_corners=[list(corner) for corner in PHONE_CORNERS])
    return camera


def decode(frame) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(frame["image"], np.uint8), cv2.IMREAD_COLOR)


def phone_plane(frame) -> np.ndarray:
    """与 runtime.measure_scroll 相同的去姿态 + 矫正到屏幕平面。"""
    decoded = decode(frame)
    live = locate_screen_quad(decoded, PHONE_CORNERS, tolerance_px=32,
                              require_fittable=True)
    pose = fit_pose(PHONE_CORNERS, live["corners"])
    aligned_gray = cv2.cvtColor(align_screen(decoded, PHONE_CORNERS, pose),
                                cv2.COLOR_BGR2GRAY)
    return cv2.warpPerspective(aligned_gray, HOMOGRAPHY, SCREEN_SIZE)


def screen_roi() -> dict:
    corners = [HOMOGRAPHY @ np.array([CAMERA_ROI["x"] + dx, CAMERA_ROI["y"] + dy, 1.0])
               for dx in (0, CAMERA_ROI["width"]) for dy in (0, CAMERA_ROI["height"])]
    xs = [c[0] / c[2] for c in corners]
    ys = [c[1] / c[2] for c in corners]
    return {"x": min(xs), "y": min(ys),
            "width": max(xs) - min(xs), "height": max(ys) - min(ys)}


def measured(camera) -> dict:
    frame = camera.capture_sync()
    return phone_plane(frame)


def test_s01_content_moves_without_phone_motion_in_both_directions():
    """§10.2.1/.2：手机不动、内容 ±32px → displaced 且 dy=∓32±1。"""
    camera = make_camera()
    before = measured(camera)
    camera.scroll_by(32)  # 内容向上 32px（scroll_by 正值 = 内容上移）
    after_up = measured(camera)
    result = measure_displacement(before, after_up, screen_roi())
    assert result["status"] == "displaced", result
    assert result["dy_px"] == pytest.approx(-32, abs=1)

    camera2 = make_camera()
    base = measured(camera2)
    camera2.scroll_by(-32)  # 内容向下 32px
    after_down = measured(camera2)
    result = measure_displacement(base, after_down, screen_roi())
    assert result["status"] == "displaced", result
    assert result["dy_px"] == pytest.approx(32, abs=1)


def test_s01_phone_shift_with_static_content_is_stationary_after_pose_removal():
    """§10.2.3：内容不动、手机平移 (8,4) → 去姿态后 stationary、|d|<=1。"""
    camera = make_camera()
    before = measured(camera)
    camera.set_phone_offset(8, 4)
    after = measured(camera)
    result = measure_displacement(before, after, screen_roi())
    assert result["status"] == "stationary", result
    assert abs(result["dx_px"]) <= 1 and abs(result["dy_px"]) <= 1


def test_s01_phone_shift_does_not_count_as_content_scroll():
    """§10.2.4：手机平移 (8,4) 且内容上移 32px → 去姿态后仍 -32±1。"""
    camera = make_camera()
    before = measured(camera)
    camera.scroll_by(32)
    camera.set_phone_offset(8, 4)
    after = measured(camera)
    result = measure_displacement(before, after, screen_roi())
    assert result["status"] == "displaced", result
    assert result["dy_px"] == pytest.approx(-32, abs=1)


def test_s01_background_change_alone_never_reads_as_displaced():
    """§10.2.5：只有背景变化 → stationary 或 unknown，绝不能 displaced。"""
    camera = make_camera()
    before = camera.capture_sync()
    after = camera.capture_sync()
    # 真实场景模拟：桌面光照变化只影响手机以外区域（接触手机内部的像素不动）。
    decoded = decode(after)
    mask = np.zeros(decoded.shape[:2], dtype=np.uint8)
    cv2.fillConvexPoly(mask, np.asarray(PHONE_CORNERS, dtype=np.int32), 255)
    decoded[mask == 0] = np.clip(decoded[mask == 0].astype(int) + 45, 0, 255).astype(np.uint8)
    encoded = cv2.imencode(".png", decoded)[1].tobytes()
    altered = {**after, "image": encoded}

    plane_a, plane_b = phone_plane(before), phone_plane(altered)
    result = measure_displacement(plane_a, plane_b, screen_roi())
    assert result["status"] in {"stationary", "unknown"}, result
    assert result["status"] != "displaced"
    assert np.asarray(plane_a).shape == np.asarray(plane_b).shape


@pytest.mark.parametrize("scene", [
    pytest.param("no-phone", id="no-phone-full-frame-texture"),
    pytest.param("uniform", id="uniformly-lit-frame"),
    pytest.param("shift-beyond-limit", id="shift-beyond-compensation-limit"),
])
def test_s01_unverifiable_scenes_are_unknown_never_fabricated(scene):
    """§10.2.6：无手机、整帧均匀亮、明显超限偏移 → 定位/补偿失败，不伪造几何。"""
    camera = make_camera()
    if scene == "no-phone":
        camera.configure_simulation_frame(1280, 720)  # 回到全帧纹理：画面内无手机
        frame = camera.capture_sync()
    elif scene == "uniform":
        frame = camera.capture_sync()
        decoded = decode(frame)
        decoded[:] = 250  # 整帧均匀亮：无任何可定位边缘
        frame = {**frame,
                 "image": cv2.imencode(".png", decoded)[1].tobytes()}
    else:
        camera.set_phone_offset(40, 20)  # > 16px 补偿上限
        frame = camera.capture_sync()
    with pytest.raises((ScreenEdgeError, ValueError)):
        phone_plane(frame)  # 生产链在同样位置转 unknown


def test_s02_mid_render_scene_change_yields_a_whole_snapshot(monkeypatch):
    """§10.2 快照语义：渲染中途同时滚动并平移手机 → 整帧为旧或新快照，无撕裂。"""
    camera = make_camera()
    before_bytes = camera.capture_sync()["image"]
    encode = fake_camera.encode_png

    def mutate_mid_frame(width, height, pixel):
        def sample(x, y):
            if x == 0 and y == height // 2:
                camera.scroll_by(32)
                camera.set_phone_offset(6, 3)
            return pixel(x, y)
        return encode(width, height, sample)

    camera._render_cache = None  # 强制重新渲染，使中途突变落在渲染窗口内
    with monkeypatch.context() as patch:
        patch.setattr(fake_camera, "encode_png", mutate_mid_frame)
        torn_candidate = camera.capture_sync()["image"]
    after_bytes = camera.capture_sync()["image"]
    # 撕裂帧会既不同于纯旧帧、也不同于纯新帧（边界行上下半帧错位）。
    assert torn_candidate in {before_bytes, after_bytes}


def test_phone_boundary_does_not_move_with_list_scroll():
    """§10.1：手机边界不随列表滚动移动。"""
    camera = make_camera()
    quads = []
    for _ in range(2):
        frame = camera.capture_sync()
        live = locate_screen_quad(decode(frame), PHONE_CORNERS,
                                  tolerance_px=32, require_fittable=True)
        quads.append(np.asarray(live["corners"], dtype=float))
        camera.scroll_by(-96)
    deviation = float(np.max(np.linalg.norm(quads[0] - quads[1], axis=1)))
    assert deviation <= 1.0


def test_phone_offset_moves_boundary_and_content_together():
    """§10.1：set_phone_offset 平移整个手机及内容；背景锚定相机帧不动。"""
    camera = make_camera()
    before = decode(camera.capture_sync())
    camera.set_phone_offset(8, 4)
    after = decode(camera.capture_sync())

    live = locate_screen_quad(after, PHONE_CORNERS, tolerance_px=32,
                              require_fittable=True)
    moved = np.asarray(live["corners"], dtype=float)
    expected = np.asarray(PHONE_CORNERS, dtype=float) + [8, 4]
    # 检测角点为亮区像素中心，与几何边界有 ≤1px 的固有取整差。
    assert np.max(np.linalg.norm(moved - expected, axis=1)) <= 1.5

    # 背景不动：手机外左上角区域逐像素一致。
    corner = before[:36, :72]
    assert np.array_equal(corner, after[:36, :72])
    # 手机内容随手机平移：手机内部左上角区域整体位移 (8,4) 后一致。
    phone_before = before[44:200, 88:400]
    phone_after = after[48:204, 96:408]
    assert np.array_equal(phone_before, phone_after)


@pytest.mark.parametrize("corners, error", [
    ([[0, 0], [800, 0], [800, 600]], "四个"),
    ([[0, 0], [800, 0], [800, 600], [0, 600], [1, 1]], "四个"),
    ([[0, 0], [800, 0], [900, 600], [0, 600]], "轴对齐"),
    ([[-80, 40], [880, 40], [880, 640], [-80, 640]], "帧内"),
    ([[80, 40], [81, 40], [81, 640], [80, 640]], "2 像素"),
    ([["80", 40], [880, 40], [880, 640], [80, 640]], "数值点"),
])
def test_invalid_screen_corners_are_rejected(corners, error):
    camera = fake_camera.FakeCameraDriver("arm-01", width=1280, height=720)
    with pytest.raises(ValueError) as exc:
        camera.configure_simulation_frame(1280, 720, screen_corners=corners)
    assert error in str(exc.value)


@pytest.mark.parametrize("offset", [(True, 0), (0, "nan"), (float("nan"), 0),
                                    (float("inf"), 2), (None, 0)])
def test_invalid_phone_offset_is_rejected(offset):
    camera = make_camera()
    with pytest.raises(ValueError):
        camera.set_phone_offset(*offset)
