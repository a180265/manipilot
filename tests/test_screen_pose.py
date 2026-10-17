import cv2
import math
import numpy as np
import pytest

from drivers import calibration_bundle as cb
from drivers.screen_pose import fit_pose, fit_pose_robust, physical_point


SAVED = [[30, 10], [90, 10], [90, 80], [30, 80]]


def bundle():
    return {
        "frame": {"width": 128, "height": 96},
        "screen": {"screenWidth": 128, "screenHeight": 96},
        "camera": {"cameraToScreen": {
            "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "maxErrorPx": 1}},
        "xy": {"robotToScreen": {
            "a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0},
            "maxResidualPx": 1},
    }


def test_rigid_phone_translation_compensates_a_calibrated_point():
    live = [[x + 5, y + 3] for x, y in SAVED]
    pose = fit_pose(SAVED, live)
    assert pose["movementPx"] == pytest.approx(5.831, abs=.01)
    assert physical_point(bundle(), pose, 50, 45) == (55, 48)
    assert physical_point(bundle(), pose, 55, 48, already_live=True) == (55, 48)


@pytest.mark.parametrize("live", [
    [[x + 55, y] for x, y in SAVED],
    [[30, 10], [100, 10], [90, 80], [30, 80]],
    [[28, 7], [92, 7], [92, 83], [28, 83]],
])
def test_unsafe_phone_geometry_never_produces_robot_coordinates(live):
    with pytest.raises(ValueError):
        fit_pose(SAVED, live)


def test_swipe_nudge_translation_within_runtime_ceiling_compensates():
    """2026-10-17 业主场景：手动滑手机蹭动 2-5mm（≈30-45px 相机像素），
    动作链应按姿态补偿继续执行——45px 上限内纯平移全程可补偿。"""
    live = [[x + 30, y + 12] for x, y in SAVED]
    pose = fit_pose(SAVED, live)
    assert pose["movementPx"] == pytest.approx((30 ** 2 + 12 ** 2) ** .5, abs=.01)
    rx, ry = physical_point(bundle(), pose, 50, 45)
    assert math.isfinite(rx) and math.isfinite(ry)


@pytest.mark.parametrize("corners", [
    [[10, 5]] * 4,
    [[10, 5], [30, 5], [50, 5], [70, 5]],
    [[30, 10], [90, 10], [90, 10], [30, 80]],
    [[30, 10], [90, 80], [90, 10], [30, 80]],
    [[30, 10], [90, 10], [50, 30], [30, 80]],
    [[30, 10], [90, 10], [90, 10.000001], [30, 10.000001]],
], ids=("coincident", "collinear", "duplicate", "crossed", "concave", "collapsed"))
@pytest.mark.parametrize("side", ("saved", "live", "both"))
def test_invalid_four_corners_never_count_as_a_stable_phone(corners, side):
    saved = corners if side in ("saved", "both") else SAVED
    live = corners if side in ("live", "both") else SAVED
    with pytest.raises(ValueError, match="四角"):
        fit_pose(saved, live)


def test_valid_perspective_quad_can_still_be_compensated():
    saved = [[30, 10], [90, 14], [85, 80], [33, 76]]
    live = [[x + 5, y + 3] for x, y in saved]
    pose = fit_pose(saved, live)
    assert physical_point(bundle(), pose, 50, 45) == (55, 48)


def test_outside_live_phone_is_rejected_even_when_inside_frame():
    pose = fit_pose(SAVED, SAVED)
    with pytest.raises(ValueError, match="当前手机屏幕"):
        physical_point(bundle(), pose, 10, 40)


@pytest.mark.parametrize("already_live", (False, True))
def test_compensated_quadratic_inverse_must_converge(already_live):
    calibration = bundle()
    calibration["frame"] = {"width": 300, "height": 120}
    calibration["screen"] = {"screenWidth": 300, "screenHeight": 120}
    model = {
        "a": 10., "b": 0., "c": 0., "d": 0., "e": 1., "f": 0.,
        "qx2": -100 / (4 * 158), "qxy": 0., "qy2": 0.,
        "px2": 0., "pxy": 0., "py2": 0.,
    }
    calibration["xy"]["robotToScreen"] = model
    saved = [[100, 10], [200, 10], [200, 100], [100, 100]]
    stable = physical_point(calibration, fit_pose(saved, saved), 146, 45)
    assert cb.screen_from_robot(model, *stable) == pytest.approx((146, 45), abs=.02)

    live = [[x + 16, y] for x, y in saved]
    moved = fit_pose(saved, live)
    target_x = 162 if already_live else 146
    with pytest.raises(ValueError, match="反解未收敛"):
        physical_point(calibration, moved, target_x, 45, already_live=already_live)


@pytest.mark.parametrize("translation,expect_ok", [
    pytest.param((15.0, 0.0), True, id="movement-within-passes"),
    pytest.param((44.0, 0.0), True, id="movement-below-new-limit-passes"),
    pytest.param((45.0, 0.0), True, id="movement-at-limit-passes"),
    pytest.param((45.01, 0.0), False, id="movement-over-limit-rejects"),
    pytest.param((31.82, 31.82), False, id="diagonal-over-limit-rejects"),
])
def test_g03_compensation_boundary_exact_limit_passes_and_epsilon_over_rejects(
        translation, expect_ok):
    """G03：16px 补偿上限——等于上限可用，超过即拒绝。"""
    live = [[x + translation[0], y + translation[1]] for x, y in SAVED]
    if expect_ok:
        pose = fit_pose(SAVED, live)
        assert pose["movementPx"] == pytest.approx(
            (translation[0] ** 2 + translation[1] ** 2) ** .5, abs=.01)
    else:
        with pytest.raises(ValueError, match="补偿"):
            fit_pose(SAVED, live)


@pytest.mark.parametrize("scale,expect_ok", [
    # 0.01 边界在浮点下不可精确命中（规范 §8.2：避免检测舍入伪失败），
    # 用紧贴边界的安全间距：1e-6 内侧可用、1e-6 外侧拒绝。
    pytest.param(1.009999, True, id="scale-change-at-limit-passes"),
    pytest.param(1.010001, False, id="scale-change-over-limit-rejects"),
    pytest.param(0.990001, True, id="scale-down-at-limit-passes"),
    pytest.param(0.989999, False, id="scale-down-over-limit-rejects"),
])
def test_g03_scale_boundary_exact_limit_passes_and_epsilon_over_rejects(
        scale, expect_ok):
    """G03：|scale-1|≤0.01——等于上限可用，超过即拒绝。"""
    cx, cy = 60.0, 45.0
    live = [[cx + (x - cx) * scale, cy + (y - cy) * scale] for x, y in SAVED]
    if expect_ok:
        fit_pose(SAVED, live)
    else:
        with pytest.raises(ValueError, match="补偿"):
            fit_pose(SAVED, live)


@pytest.mark.parametrize("corner_offset,expect_ok", [
    pytest.param(1.9, True, id="fit-error-below-limit-passes"),
    pytest.param(3.0, False, id="fit-error-over-limit-rejects"),
])
def test_g03_fit_error_boundary_keeps_two_pixel_ceiling(corner_offset, expect_ok):
    """G03：拟合误差 2px 上限——非刚性角点扰动超限即拒绝。"""
    live = [[x, y] for x, y in SAVED]
    live[1] = [live[1][0] + corner_offset, live[1][1]]  # 单角外拉：非刚性
    if expect_ok:
        fit_pose(SAVED, live)
    else:
        with pytest.raises(ValueError, match="补偿"):
            fit_pose(SAVED, live)


def test_fit_pose_robust_tolerates_single_corner_noise_only():
    """真机 2026-09-29：三角稳定、一角漂 8px = 检测噪声 → 判稳；
    真实移动（四角齐动）或两角不一致仍拒绝。"""
    saved = [[30, 10], [90, 10], [90, 80], [30, 80]]
    noisy = [[38, 6], [90, 10], [90, 80], [30, 80]]  # 仅 TL 漂 8px
    with pytest.raises(ValueError):
        fit_pose(saved, noisy)
    pose = fit_pose_robust(saved, noisy)
    assert pose["droppedCorner"] == 0
    assert pose["fitErrorPx"] <= 1.2

    moved = [[x + 5, y + 3] for x, y in saved]  # 真实平移：四角齐动
    with pytest.raises(ValueError):
        fit_pose_robust(saved, moved, max_movement=4.0)
    pose = fit_pose_robust(saved, moved, max_movement=16.0)
    assert pose["movementPx"] == pytest.approx(5.831, abs=.01)

    torn = [[35, 12], [86, 14], [90, 80], [30, 80]]  # 两角各自乱动：拒绝
    with pytest.raises(ValueError):
        fit_pose_robust(saved, torn)
