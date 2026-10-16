"""笔杆遮挡清理回归：擦亮（两遍修复）+ 填暗（原逻辑）互不复活。

素材为合成帧（离线单测惯例，同 detect_screen_quad 家族）：
- glare：笔杆金属反光细条贴屏幕边缘伸出屏外（2026-10-16 换臂现场形态，
  凸包出界触发遮挡守卫拒绝）；
- notch：笔杆暗遮挡切入屏幕角（原 33px 漂移族，填暗负责）；
- 全暗帧原样返回（清理尽力而为契约）。
"""
import cv2
import numpy as np
import pytest

from drivers import screen_edge
from drivers.calibration_wizard import clean_pen_occlusion
from drivers.screen_edge import ScreenEdgeError, locate_screen_quad

# 合成屏幕：亮区 200 / 背景 5，四角顺时针 TL→TR→BR→BL
TRUE_CORNERS = [(200.0, 150.0), (1000.0, 150.0), (1000.0, 560.0), (200.0, 560.0)]
W, H = 1280, 720


def _screen_frame() -> np.ndarray:
    frame = np.full((H, W, 3), 5, np.uint8)
    cv2.fillConvexPoly(frame, np.int32(TRUE_CORNERS), (200, 200, 200))
    return frame


def _max_corner_error(corners) -> float:
    return float(max(
        np.hypot(x - tx, y - ty)
        for (x, y), (tx, ty) in zip(corners, TRUE_CORNERS)))


def test_glare_outside_screen_recovered_by_clean():
    """屏外反光细条：原始帧定位失败，清理后恢复且四角贴合真值。"""
    frame = _screen_frame()
    # 反光条贴底边伸出 15px（粗细/深度同现场 85x19 一族）
    frame[560:575, 600:900] = 200
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, tolerance_px=32, require_fittable=True)
    cleaned = clean_pen_occlusion(frame)
    quad = locate_screen_quad(cleaned, tolerance_px=32, require_fittable=True)
    assert _max_corner_error(quad["corners"]) <= 6.0


def test_dark_notch_inside_screen_still_filled():
    """屏内暗遮挡（原 33px 漂移族）：清理后四角贴合真值（原逻辑不回退）。"""
    frame = _screen_frame()
    frame[150:210, 840:1000] = 5  # 右上角切入的笔杆暗遮挡
    cleaned = clean_pen_occlusion(frame)
    quad = locate_screen_quad(cleaned, tolerance_px=32, require_fittable=True)
    assert _max_corner_error(quad["corners"]) <= 6.0


def test_glare_and_notch_together():
    """现场真实形态=反光（屏外）+暗遮挡（屏内）并存，两段清理协同。"""
    frame = _screen_frame()
    frame[560:575, 600:900] = 200   # 屏外反光
    frame[150:210, 840:1000] = 5    # 屏内暗遮挡
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, tolerance_px=32, require_fittable=True)
    cleaned = clean_pen_occlusion(frame)
    quad = locate_screen_quad(cleaned, tolerance_px=32, require_fittable=True)
    assert _max_corner_error(quad["corners"]) <= 6.0


def test_all_dark_frame_returned_unchanged():
    dark = np.zeros((240, 320, 3), np.uint8)
    assert np.array_equal(clean_pen_occlusion(dark), dark)


def test_ridge_fit_prefers_longer_edge_population():
    """主脊拟合：反光条与屏幕底边平行时锁定多数点群（真边），不取两人群均值。"""
    from drivers.calibration_wizard import _ridge_fit_quad
    frame = _screen_frame()
    frame[560:575, 600:900] = 200  # 与底边平行的反光条（两平行点群）
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    _, threshold = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(
        threshold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    contour = max(contours, key=cv2.contourArea)
    seed = np.asarray(
        screen_edge._quad_from_contour(cv2.convexHull(contour)))
    quad = _ridge_fit_quad(contour, seed)
    assert quad is not None
    assert _max_corner_error(quad) <= 6.0
