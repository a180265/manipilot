"""屏幕标定拟合：机械臂坐标 ↔ 手机页面坐标 的仿射映射与可点击范围计算。

输入：N 组配对（机械臂坐标 mm，页面坐标 CSS px，均来自同一次标定的实测数据）。
模型：page = A · machine + offset（2×2 矩阵 A + 平移，最小二乘拟合，N≥3）。
由于每次标定都用现场数据拟合，浏览器/机型/系统缩放差异被自动吸收。

产出：
- A、offset、各点残差（px）；
- scale_px_per_mm = sqrt(|det A|)，rotation_deg = atan2(A[v][0], A[u][0])；
- screen_corners_machine：页面四角反解到机械臂坐标 = 当前姿态下的可点击范围。
"""
from __future__ import annotations

import math

import numpy as np


class CalibrationError(Exception):
    pass


def fit_affine(machine_points: list[tuple[float, float]], page_points: list[tuple[float, float]]) -> dict:
    """最小二乘拟合仿射映射。machine 单位 mm，page 单位 px。"""
    if len(machine_points) != len(page_points):
        raise CalibrationError("机械臂坐标与页面坐标数量不一致")
    n = len(machine_points)
    if n < 3:
        raise CalibrationError("仿射标定至少需要 3 个点")

    M = np.array([[x, y, 1.0] for x, y in machine_points])  # N×3
    U = np.array([p[0] for p in page_points])
    V = np.array([p[1] for p in page_points])
    cu, *_ = np.linalg.lstsq(M, U, rcond=None)
    cv, *_ = np.linalg.lstsq(M, V, rcond=None)
    A = np.array([[cu[0], cu[1]], [cv[0], cv[1]]])
    offset = np.array([cu[2], cv[2]])

    predicted = M[:, :2] @ A.T + offset
    actual = np.stack([U, V], axis=1)
    residuals = np.linalg.norm(actual - predicted, axis=1)

    det = float(np.linalg.det(A))
    scale = math.sqrt(abs(det))
    if scale < 1e-6:
        raise CalibrationError("拟合矩阵退化（点共线或重复），请检查标定点分布")
    rotation = math.degrees(math.atan2(A[1, 0], A[0, 0]))

    return {
        "A": A.tolist(),
        "offset": offset.tolist(),
        "residuals_px": [round(float(r), 2) for r in residuals],
        "residual_max_px": round(float(residuals.max()), 2),
        "residual_rms_px": round(float(math.sqrt((residuals ** 2).mean())), 2),
        "scale_px_per_mm": round(scale, 3),
        "rotation_deg": round(rotation, 2),
        "n_points": n,
    }


def page_to_machine(calibration: dict, page_point: tuple[float, float]) -> tuple[float, float]:
    """页面坐标反解为机械臂坐标（拟合矩阵求逆）。"""
    A = np.array(calibration["A"])
    offset = np.array(calibration["offset"])
    inv = np.linalg.inv(A)
    out = inv @ (np.array(page_point) - offset)
    return float(out[0]), float(out[1])


def screen_range_machine(calibration: dict, viewport: dict, inset_mm: float = 2.0) -> dict:
    """当前姿态下屏幕可点击范围：页面四角反解为机械臂坐标（mm）。

    inset_mm：向内收缩的安全边距（避开屏幕边缘与弧角）。
    返回 machine_corners（四角 mm 坐标）与 safe_min/safe_max（内缩后的行程范围）。
    """
    w, h = float(viewport["w"]), float(viewport["h"])
    corners_page = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
    corners = [page_to_machine(calibration, c) for c in corners_page]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return {
        "machine_corners": {"page_origin": corners[0], "page_right": corners[1],
                            "page_bottom_right": corners[2], "page_bottom": corners[3]},
        "safe_min": [min(xs) + inset_mm, min(ys) + inset_mm],
        "safe_max": [max(xs) - inset_mm, max(ys) - inset_mm],
        "inset_mm": inset_mm,
    }
