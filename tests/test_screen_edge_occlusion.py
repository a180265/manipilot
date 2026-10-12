"""Bound-phone localization regressions using only synthetic camera frames."""
from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from drivers.screen_edge import ScreenEdgeError, detect_screen_quad, locate_screen_quad


SAVED = np.array(
    [[156, 104], [1156, 123], [1156, 583], [149, 552]], dtype=float)


def phone_frame(corners, *, probe=False, radius=0):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    if radius:
        display = np.zeros((461, 1001, 3), dtype=np.uint8)
        cv2.rectangle(display, (radius, 0), (1000 - radius, 460), (40, 40, 40), -1)
        cv2.rectangle(display, (0, radius), (1000, 460 - radius), (40, 40, 40), -1)
        for x in (radius, 1000 - radius):
            for y in (radius, 460 - radius):
                cv2.circle(display, (x, y), radius, (40, 40, 40), -1)
        mapping = cv2.getPerspectiveTransform(
            np.float32([[0, 0], [1000, 0], [1000, 460], [0, 460]]),
            np.asarray(corners, dtype=np.float32))
        frame = cv2.warpPerspective(display, mapping, (1280, 720))
    else:
        cv2.fillConvexPoly(
            frame, np.rint(corners).astype(np.int32), (40, 40, 40))
    if probe:
        center = np.rint(np.asarray(corners).mean(axis=0) + [180, -20]).astype(int)
        cv2.circle(frame, tuple(center), 42, (0, 0, 0), -1)
        cv2.rectangle(
            frame, (int(center[0]) - 10, int(center[1])),
            (int(center[0]) + 15, 680), (0, 0, 0), -1)
    return frame


@pytest.mark.parametrize("offset", [(0, 0), (5, 3)])
def test_probe_intrusion_preserves_the_measured_phone_pose(offset):
    saved = SAVED.copy()
    live = saved + offset
    located = locate_screen_quad(
        phone_frame(live, probe=True), saved.tolist(),
        tolerance_px=32, require_fittable=True)
    np.testing.assert_allclose(located["corners"], live, atol=1.5)
    np.testing.assert_array_equal(saved, SAVED)
    assert (located["frameW"], located["frameH"]) == (1280, 720)


@pytest.mark.parametrize("offset", [(0, 0), (5, 3)])
@pytest.mark.parametrize("probe", [False, True])
def test_rounded_phone_uses_observed_straight_edges(offset, probe):
    live = SAVED + offset
    located = locate_screen_quad(
        phone_frame(live, probe=probe, radius=30), SAVED.tolist(),
        tolerance_px=32, require_fittable=True)
    np.testing.assert_allclose(located["corners"], live, atol=1.5)


@pytest.mark.parametrize("depth", [15, 35, 70])
def test_phone_with_an_unobserved_side_is_not_reconstructed_from_a_box(depth):
    saved = np.array([[150, 100], [1150, 100], [1150, 560], [150, 560]], dtype=float)
    frame = phone_frame(saved)
    cv2.rectangle(frame, (1150 - depth, 125), (1200, 535), (0, 0, 0), -1)
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, saved.tolist(), require_fittable=True)


@pytest.mark.parametrize("probe", [False, True])
def test_fresh_rounded_capture_keeps_its_intersections_after_small_shift(probe):
    saved = detect_screen_quad(phone_frame(SAVED, radius=30))["corners"]
    located = locate_screen_quad(
        phone_frame(SAVED + [5, 3], radius=30, probe=probe), saved,
        require_fittable=True)
    np.testing.assert_allclose(
        located["corners"], np.asarray(saved) + [5, 3], atol=1.5)


@pytest.mark.parametrize("offset", [(15, 0), (5, 3)])
def test_small_displacement_still_uses_camera_observations(offset):
    live = SAVED + offset
    located = locate_screen_quad(
        phone_frame(live, radius=30, probe=True), SAVED.tolist(),
        require_fittable=True)
    np.testing.assert_allclose(located["corners"], live, atol=1.5)


@pytest.mark.parametrize("offset", [(17, 0), (0, 18), (25, 0)])
def test_probe_and_rounding_do_not_bypass_the_16px_gate(offset):
    image = phone_frame(SAVED + offset, radius=30, probe=True)
    assert locate_screen_quad(image, SAVED.tolist())["corners"]
    with pytest.raises(ScreenEdgeError, match="无法可靠补偿"):
        locate_screen_quad(image, SAVED.tolist(), require_fittable=True)


def test_phone_rotation_within_the_displacement_gate_is_measured():
    angle = math.radians(.5)
    rotation = np.array([
        [math.cos(angle), -math.sin(angle)],
        [math.sin(angle), math.cos(angle)]])
    center = SAVED.mean(axis=0)
    live = (SAVED - center) @ rotation.T + center
    located = locate_screen_quad(
        phone_frame(live, radius=30, probe=True), SAVED.tolist(),
        require_fittable=True)
    np.testing.assert_allclose(located["corners"], live, atol=1.5)


def test_line_fitting_does_not_bypass_the_one_percent_scale_gate():
    center = SAVED.mean(axis=0)
    live = (SAVED - center) * 1.02 + center
    image = phone_frame(live, radius=30, probe=True)
    assert locate_screen_quad(image, SAVED.tolist())["corners"]
    with pytest.raises(ScreenEdgeError, match="无法可靠补偿"):
        locate_screen_quad(image, SAVED.tolist(), require_fittable=True)


def test_line_fitting_does_not_bypass_the_two_pixel_pose_residual_gate():
    live = SAVED.copy()
    live[0] += [0, 12]
    image = phone_frame(live, radius=30, probe=True)
    assert locate_screen_quad(image, SAVED.tolist())["corners"]
    with pytest.raises(ScreenEdgeError, match="无法可靠补偿"):
        locate_screen_quad(image, SAVED.tolist(), require_fittable=True)


@pytest.mark.parametrize("brightness", [0, 30, 220])
def test_absent_phone_is_not_inferred_from_the_saved_template(brightness):
    image = np.full((720, 1280, 3), brightness, dtype=np.uint8)
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(image, SAVED.tolist(), require_fittable=True)


def test_an_environment_rectangle_cannot_replace_the_calibrated_phone():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(image, (110, 65), (1220, 630), (240, 240, 240), -1)
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(image, SAVED.tolist(), require_fittable=True)


def test_environment_brightness_does_not_move_the_located_phone():
    live = SAVED + [5, 3]
    image = phone_frame(live, radius=30, probe=True)
    cv2.rectangle(image, (10, 10), (1265, 65), (240, 240, 240), -1)
    located = locate_screen_quad(image, SAVED.tolist(), require_fittable=True)
    np.testing.assert_allclose(located["corners"], live, atol=1.5)


def test_black_display_is_localized_before_assessing_bright_surroundings():
    live = SAVED + [5, 3]
    display = phone_frame(live, radius=30)
    image = np.full_like(display, 220)
    image[display[:, :, 0] > 0] = 0
    located = locate_screen_quad(image, SAVED.tolist(), require_fittable=True)
    np.testing.assert_allclose(located["corners"], live, atol=1.5)


def test_bound_location_keeps_small_camera_fixture_compatibility():
    saved = [[4, 4], [59, 4], [59, 43], [4, 43]]
    image = np.zeros((48, 64, 3), dtype=np.uint8)
    image[5:43, 5:59] = 120
    for y in range(10, 40, 6):
        image[y:y + 2, 10:50] = 220
    encoded_ok, encoded = cv2.imencode(".jpg", image)
    assert encoded_ok
    decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    located = locate_screen_quad(decoded, saved, require_fittable=True)
    np.testing.assert_allclose(located["corners"], saved, atol=.1)


def test_tiny_bezel_bumps_are_not_occluders():
    """真机（2026-09-29）复现：手机屏幕轮廓的边框圆弧/JPEG 噪声让凸包点以
    ~2px 超出直线边交点——这是拟合噪声不是遮挡（真实遮挡是成片 ≥15px 外凸）。
    遮挡容差必须容纳像素级凸起，否则亮屏校准页四边不可定位、首次点击死锁。"""
    corners = SAVED.copy()
    frame = phone_frame(corners)
    # 在两条边的中点各加一个 3px 半径的同色凸起（中心在边界上，外扩 3px）。
    top_mid = np.rint([(corners[0] + corners[1]) / 2]).astype(int)[0]
    left_mid = np.rint([(corners[0] + corners[3]) / 2]).astype(int)[0]
    cv2.circle(frame, tuple(top_mid), 3, (40, 40, 40), -1)
    cv2.circle(frame, tuple(left_mid), 3, (40, 40, 40), -1)

    located = locate_screen_quad(frame, corners.tolist(),
                                 tolerance_px=32, require_fittable=True)
    np.testing.assert_allclose(located["corners"], corners, atol=2.5)

    quad = detect_screen_quad(frame)
    np.testing.assert_allclose(quad["corners"], corners, atol=2.5)


def test_gradient_line_locate_works_on_bright_desk_dark_screen():
    """开灯常态（用户拍板 2026-09-29）：桌面比屏幕亮，阈值区域法锁到背景；
    梯度直线法在已保存四角预测附近直接拟合四条物理边，与明暗无关。"""
    corners = SAVED.copy()
    frame = np.full((720, 1280, 3), 185, dtype=np.uint8)   # 亮桌面
    cv2.fillConvexPoly(frame, np.rint(corners).astype(np.int32) - 6, (15, 15, 15))  # 黑边框
    cv2.fillConvexPoly(frame, np.rint(corners).astype(np.int32), (95, 90, 100))     # 暗屏（比桌面暗）
    cv2.circle(frame, (640, 40), 26, (250, 250, 250), -1)  # 亮色干扰物（灯）

    from drivers.screen_edge import locate_quad_by_gradient_lines
    located = locate_quad_by_gradient_lines(frame, corners.tolist(), tolerance_px=32)
    np.testing.assert_allclose(located["corners"], corners, atol=3.0)

    # 暗背景（灯关）同样可用
    dark = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.fillConvexPoly(dark, np.rint(corners).astype(np.int32), (110, 105, 115))
    located = locate_quad_by_gradient_lines(dark, corners.tolist(), tolerance_px=32)
    np.testing.assert_allclose(located["corners"], corners, atol=3.0)


def test_gradient_line_locate_rejects_when_phone_left_prediction():
    from drivers.screen_edge import ScreenEdgeError, locate_quad_by_gradient_lines
    corners = SAVED.copy()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    moved = corners + [220, 40]
    cv2.fillConvexPoly(frame, np.rint(moved).astype(np.int32), (110, 105, 115))
    with pytest.raises(ScreenEdgeError, match="移动|缺少边缘梯度|支持点不足"):
        locate_quad_by_gradient_lines(frame, corners.tolist(), tolerance_px=32)


def test_gradient_locate_deviation_guard_applies_on_converged_path(monkeypatch):
    """P1-5（2026-09-30 审查）：收敛（锚自洽 ≤0.75px）路径同样必须执行对
    原始 expected_corners 的偏离守卫——滞回把锚逐次拉向别的物理边界时，
    锁错目标不得无守卫放行。旧实现守卫只在 3 轮不收敛路径执行。"""
    from drivers import screen_edge as se
    from drivers.screen_edge import ScreenEdgeError, locate_quad_by_gradient_lines
    corners = SAVED.copy()
    locked_far = (corners + [220.0, 40.0]).tolist()  # 相对移动锚自洽（偏差 0），远离预测 220px
    calls: list[list] = []

    def fake_once(frame, anchor, *, tolerance_px):
        calls.append([list(p) for p in anchor])
        return {"corners": [list(map(float, p)) for p in locked_far], "support": 40}

    monkeypatch.setattr(se, "_locate_quad_by_gradient_lines_once", fake_once)
    with pytest.raises(ScreenEdgeError, match="手机可能已移动"):
        locate_quad_by_gradient_lines(
            np.zeros((720, 1280, 3), dtype=np.uint8),
            corners.tolist(), tolerance_px=32)
    assert calls, "应至少迭代一次"
