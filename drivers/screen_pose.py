"""Fresh screen geometry shared by the motion gate and coordinate resolver."""
from __future__ import annotations

import math

import cv2
import numpy as np

from drivers import calibration_bundle as cb

MAX_COMPENSATION_PX = 16.0
MAX_FIT_ERROR_PX = 2.0
MAX_SCALE_CHANGE = 0.01


def validate_corners(corners: list) -> np.ndarray:
    """Require four distinct, resolvable corners in convex perimeter order."""
    try:
        quad = np.asarray(corners, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("手机屏幕四角无效") from exc
    if quad.shape != (4, 2) or not np.isfinite(quad).all():
        raise ValueError("手机屏幕四角无效")
    with np.errstate(over="ignore", invalid="ignore"):
        edges = np.roll(quad, -1, axis=0) - quad
        following = np.roll(edges, -1, axis=0)
        turns = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
        lengths = np.linalg.norm(edges, axis=1)
    if (not np.isfinite(lengths).all() or not np.isfinite(turns).all()
            or (lengths < 1.0).any()
            or not ((turns > 1.0).all() or (turns < -1.0).all())):
        raise ValueError("手机屏幕四角不是有效凸四边形")
    return quad


def fit_pose(saved: list, live: list, *, max_movement: float = MAX_COMPENSATION_PX) -> dict:
    """Fit a near-rigid camera-plane transform; never extrapolate an uncertain edge."""
    src, dst = validate_corners(saved), validate_corners(live)
    from drivers.screen_edge import _fit_similarity

    scale, rotation, translation = _fit_similarity(src, dst)
    fitted = scale * (src @ rotation.T) + translation
    residual = float(np.max(np.linalg.norm(fitted - dst, axis=1)))
    movement = float(np.max(np.linalg.norm(fitted - src, axis=1)))
    if (residual > MAX_FIT_ERROR_PX or abs(scale - 1) > MAX_SCALE_CHANGE
            or movement > max_movement):
        raise ValueError(
            f"手机位置无法可靠补偿（位移 {movement:.1f}px、"
            f"拟合误差 {residual:.1f}px、尺度变化 {abs(scale - 1):.3f}）")
    matrix = np.eye(3)
    matrix[:2, :2] = scale * rotation
    matrix[:2, 2] = translation
    return {"matrix": matrix.tolist(), "corners": dst.tolist(),
            "movementPx": movement, "fitErrorPx": residual}


def align_screen(frame: np.ndarray, saved: list, pose: dict) -> np.ndarray:
    """Return calibrated camera coordinates with everything outside the phone hidden."""
    height, width = frame.shape[:2]
    matrix = np.asarray(pose["matrix"], dtype=float)
    corners = np.asarray(saved, dtype=np.float32)
    if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
            or corners.shape != (4, 2) or not np.isfinite(corners).all()):
        raise ValueError("手机姿态或校准屏幕区域无效")
    aligned = cv2.warpAffine(
        frame, matrix[:2].astype(np.float32), (width, height),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(127, 127, 127))
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillConvexPoly(mask, np.rint(corners).astype(np.int32), 255)
    aligned[mask == 0] = 127
    return aligned


def physical_point(bundle: dict, pose: dict, camera_x: float, camera_y: float,
                   *, already_live: bool = False) -> tuple[float, float]:
    """Map a calibrated content point (or fresh camera observation) to robot XY."""
    matrix = np.asarray(pose["matrix"], dtype=float)
    corners = np.asarray(pose["corners"], dtype=np.float32)
    point = np.array([camera_x, camera_y, 1.0], dtype=float)
    if not already_live:
        point = matrix @ point
    cx, cy = point[:2] / point[2]
    frame = bundle["frame"]
    if (not np.isfinite([cx, cy]).all()
            or not 0 <= cx < frame["width"] or not 0 <= cy < frame["height"]
            or cv2.pointPolygonTest(corners, (float(cx), float(cy)), True) < 0):
        raise ValueError("补偿后的目标超出当前手机屏幕")
    sx, sy = cb.apply_homography(
        bundle["camera"]["cameraToScreen"]["homography"], float(cx), float(cy))
    width, height = bundle["screen"]["screenWidth"], bundle["screen"]["screenHeight"]
    xy = bundle["xy"]
    uncertainty = max(1.0, float(xy.get("maxResidualPx", cb.XY_RESIDUAL_MAX_PX)))
    uncertainty += max(0.0, float(
        bundle["camera"]["cameraToScreen"].get("maxErrorPx", cb.CAMERA_REPROJECTION_MAX_PX)))
    # The screen-space calibration coordinates are fixed to the robot, so a small
    # physical translation can extend beyond their original 0..width domain.
    extension = float(pose["movementPx"]) * max(width, height) / max(
        frame["width"], frame["height"])
    if min(sx, sy, width - sx, height - sy) < uncertainty - extension:
        raise ValueError("补偿后目标超出已验证的屏幕行程")
    rx, ry = cb.robot_from_screen(xy["robotToScreen"], sx, sy)
    if not math.isfinite(rx) or not math.isfinite(ry):
        raise ValueError("补偿后机械坐标无效")
    rx, ry = round(rx, 3), round(ry, 3)
    check_x, check_y = cb.screen_from_robot(xy["robotToScreen"], rx, ry)
    if (not math.isfinite(check_x) or not math.isfinite(check_y)
            or math.hypot(check_x - sx, check_y - sy) > uncertainty):
        raise ValueError("补偿后屏幕坐标反解未收敛，禁止使用不确定的机械位置")
    return rx, ry


def fit_pose_robust(saved: list, live: list, *,
                    max_movement: float = MAX_COMPENSATION_PX,
                    single_corner_residual: float = 1.2,
                    allow_single_corner_drop: bool = True) -> dict:
    """fit_pose + 单角检测噪声容忍（2026-09-29 真机定案）。

    真实手机是刚体：移动/旋转/缩放必然四角齐动；单角大幅偏移只可能是
    边缘检测噪声（真机实测：三角 ±1px、一角漂 8px，触发 2px 刚性门）。
    先试严格 fit_pose；失败时逐角剔除后用其余三角拟合相似变换，某次
    剔除使残差 ≤single_corner_residual 且位移/尺度仍在限内 → 判稳，
    返回基于保留三角的姿态矩阵（被剔角沿用 saved，保守不外推）。
    任意两角及以上不一致仍拒绝（真实移动照常拦截）。

    allow_single_corner_drop=False（POSE-POLICY.md，R4-08-C）：普通动作/
    验收接触门不得用剔角容忍放行非刚性形变——单角大幅偏移在动作语境下
    按"不可核实"失败关闭（剔角容忍只属于采样姿态复核的检测噪声语义）。
    """
    strict_error: ValueError | None = None
    try:
        return fit_pose(saved, live, max_movement=max_movement)
    except ValueError as exc:
        strict_error = exc
    if not allow_single_corner_drop:
        # 动作/验收门语义：严格拟合失败即失败关闭，保留原始原因
        #（位移/尺度/残差），不以剔角容忍解释非刚性形变。
        raise strict_error
    saved_q = validate_corners(saved)
    live_q = validate_corners(live)
    from drivers.screen_edge import _fit_similarity

    best = None
    for drop in range(4):
        keep = [i for i in range(4) if i != drop]
        src = saved_q[keep]
        dst = live_q[keep]
        scale, rotation, translation = _fit_similarity(src, dst)
        fitted = scale * (src @ rotation.T) + translation
        residual = float(np.max(np.linalg.norm(fitted - dst, axis=1)))
        if residual > single_corner_residual:
            continue
        movement = float(np.max(np.linalg.norm(fitted - src, axis=1)))
        if movement > max_movement or abs(scale - 1) > MAX_SCALE_CHANGE:
            continue
        corners_out = live_q.copy()
        corners_out[drop] = saved_q[drop]  # 被剔角沿用基准（保守）
        matrix = np.eye(3)
        matrix[:2, :2] = scale * rotation
        matrix[:2, 2] = translation
        candidate = {"matrix": matrix.tolist(), "corners": corners_out.tolist(),
                     "movementPx": movement, "fitErrorPx": residual,
                     "droppedCorner": drop}
        if best is None or candidate["fitErrorPx"] < best["fitErrorPx"]:
            best = candidate
    if best is None:
        raise ValueError(
            "手机位置无法可靠补偿（多角不一致或超限，疑似真实移动或画面干扰）")
    return best
