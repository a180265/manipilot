"""通用画面位移测量（规范第 9.2、22.14 节）。

纯测量：OpenCV 稀疏光流正反向追踪 + 中位数/MAD 去离群 + OCR 行互证。
不包含微信标题、客户身份或业务规则；只证明列表内容移动，
不证明客户身份或机械整体精度。默认参数唯一来源
configs/gesture_defaults.json（第 22.1 节），运行时加载后冻结传入。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "configs" / "gesture_defaults.json"
_DEFAULT_PARAMS: dict | None = None


def default_params() -> dict:
    global _DEFAULT_PARAMS
    if _DEFAULT_PARAMS is None:
        config = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
        _DEFAULT_PARAMS = {**config["scroll_observation"],
                           "gesture_rules_version": config["gesture_rules_version"]}
    return dict(_DEFAULT_PARAMS)


def _crop(image: np.ndarray, roi: dict) -> np.ndarray:
    x, y = int(roi["x"]), int(roi["y"])
    width, height = int(roi["width"]), int(roi["height"])
    return image[y:y + height, x:x + width]


def _exclusion_mask(shape, roi: dict, exclusions: list | None) -> np.ndarray | None:
    if not exclusions:
        return None
    mask = np.full(shape, 255, dtype=np.uint8)
    for region in exclusions:
        x0 = max(int(region["x"]) - int(roi["x"]), 0)
        y0 = max(int(region["y"]) - int(roi["y"]), 0)
        # 右/下边界按未截断坐标起算：区域横跨 ROI 边界时只遮蔽 ROI 内部分
        x1 = min(int(region["x"]) + int(region["width"]) - int(roi["x"]), shape[1])
        y1 = min(int(region["y"]) + int(region["height"]) - int(roi["y"]), shape[0])
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = 0
    return mask


def _track(before_roi: np.ndarray, after_roi: np.ndarray,
           mask: np.ndarray | None, params: dict) -> dict:
    corners = cv2.goodFeaturesToTrack(
        before_roi, mask=mask, maxCorners=int(params["max_corners"]),
        qualityLevel=float(params["quality_level"]),
        minDistance=float(params["min_distance"]),
        blockSize=int(params["block_size"]))
    if corners is None or len(corners) < int(params["min_inliers"]):
        return {"ok": False, "reason_code": "insufficient_features",
                "corners": 0, "valid": 0, "retained": 0.0,
                "dx": None, "dy": None, "mad_dy": None}
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                int(params["criteria_count"]), float(params["criteria_eps"]))
    win = (int(params["win_size"]),) * 2
    forward, st_fwd, _err_fwd = cv2.calcOpticalFlowPyrLK(
        before_roi, after_roi, corners, None, winSize=win,
        maxLevel=int(params["max_level"]), criteria=criteria)
    backward, st_bwd, _err_bwd = cv2.calcOpticalFlowPyrLK(
        after_roi, before_roi, forward, None, winSize=win,
        maxLevel=int(params["max_level"]), criteria=criteria)
    max_fb = float(params["max_forward_backward_error_px"])
    fb = np.linalg.norm(corners - backward, axis=2).reshape(-1)
    status_ok = (st_fwd.reshape(-1) > 0) & (st_bwd.reshape(-1) > 0)
    keep_mask = status_ok & (fb <= max_fb)
    keep = np.flatnonzero(keep_mask)
    valid = int(keep.size)
    retained = valid / len(corners)
    if valid < int(params["min_inliers"]) or retained < float(params["min_retained_ratio"]):
        return {"ok": False, "reason_code": "insufficient_inliers",
                "corners": len(corners), "valid": valid,
                "retained": retained, "dx": None, "dy": None, "mad_dy": None}
    deltas = (forward.reshape(-1, 2)[keep] - corners.reshape(-1, 2)[keep])
    dx = float(np.median(deltas[:, 0]))
    dy = float(np.median(deltas[:, 1]))
    mad_dy = float(np.median(np.abs(deltas[:, 1] - dy)))
    return {"ok": True, "reason_code": None, "corners": len(corners),
            "valid": valid, "retained": retained,
            "dx": dx, "dy": dy, "mad_dy": mad_dy}


def _match_ocr_rows(rows_before: list | None, rows_after: list | None,
                    dy: float, tolerance_px: float) -> int:
    """唯一文本名配对；同名多候选不强行配对（第 9.2 节第 7 点）。"""
    if not rows_before or not rows_after:
        return 0
    before_counts: dict[str, int] = {}
    for row in rows_before:
        text = str(row.get("text") or "")
        if not text.strip():
            continue  # 空/纯空白行不构成互证（恒等键配对会稀释 ≥2 行门禁）
        before_counts[text] = before_counts.get(text, 0) + 1
    after_counts: dict[str, int] = {}
    for row in rows_after:
        text = str(row.get("text") or "")
        if not text.strip():
            continue
        after_counts[text] = after_counts.get(text, 0) + 1
    matched = 0
    for text, count in before_counts.items():
        if count != 1 or after_counts.get(text) != 1:
            continue
        y_before = next(row["y"] for row in rows_before if row["text"] == text)
        y_after = next(row["y"] for row in rows_after if row["text"] == text)
        if abs((y_after - y_before) - dy) <= tolerance_px:
            matched += 1
    return matched


def _result(status: str, tracking: dict, params: dict,
            matched_ocr_rows: int = 0, reason_code: str | None = None) -> dict:
    return {
        "status": status,
        "dy_px": tracking.get("dy"),
        "dx_px": tracking.get("dx"),
        "inlier_ratio": round(tracking.get("retained", 0.0), 3),
        "valid_points": tracking.get("valid", 0),
        "matched_ocr_rows": matched_ocr_rows,
        "mad_dy_px": tracking.get("mad_dy"),
        "reason_code": reason_code,
        "gesture_rules_version": params["gesture_rules_version"],
    }


def measure_displacement(before_gray: np.ndarray, after_gray: np.ndarray,
                         roi: dict, exclusions: list | None = None,
                         params: dict | None = None, *,
                         ocr_rows_before: list | None = None,
                         ocr_rows_after: list | None = None) -> dict:
    """测量 ROI 内列表位移：displaced / stationary / unknown 三态。

    - displaced：|median_dy| ≥ min_displacement_ratio*ROI 高，且（提供 OCR 行时）
      至少两条唯一文本位移方向一致；
    - stationary：满足静止阈值（0.01 比例，见 gesture_defaults）；
    - 介于两者、特征不足、追踪失败、横向位移过大 → unknown，
      算法失败不判成没滚动。
    """
    params = params or default_params()
    before_roi = _crop(before_gray, roi)
    after_roi = _crop(after_gray, roi)
    tracking = _track(before_roi, after_roi,
                      _exclusion_mask(before_roi.shape, roi, exclusions), params)
    if not tracking["ok"]:
        return _result("unknown", tracking, params,
                       reason_code=tracking["reason_code"])
    width, height = roi["width"], roi["height"]
    dx, dy, mad_dy = tracking["dx"], tracking["dy"], tracking["mad_dy"]
    if (abs(dx) > float(params["max_horizontal_ratio"]) * width
            or mad_dy > float(params["max_dy_mad_ratio"]) * height):
        return _result("unknown", tracking, params,
                       reason_code="incoherent_motion")
    ocr_tolerance = float(params["ocr_row_position_tolerance_ratio"]) * height
    matched = _match_ocr_rows(ocr_rows_before, ocr_rows_after, dy, ocr_tolerance)
    stationary_cfg = params.get("stationary") or {}
    stationary_ratio = float(stationary_cfg.get("median_ratio", 0.01))
    if abs(dy) >= float(params["min_displacement_ratio"]) * height:
        if ocr_rows_before is not None and ocr_rows_after is not None and matched < 2:
            return _result("unknown", tracking, params, matched,
                           reason_code="ocr_rows_not_consistent")
        return _result("displaced", tracking, params, matched)
    if (abs(dx) <= stationary_ratio * width
            and abs(dy) <= stationary_ratio * height
            and mad_dy <= stationary_ratio * height):
        return _result("stationary", tracking, params, matched)
    return _result("unknown", tracking, params, matched,
                   reason_code="between_thresholds")


def measure_stationary_list(before_gray: np.ndarray, after_gray: np.ndarray,
                            roi: dict, exclusions: list | None = None,
                            params: dict | None = None, *,
                            ocr_rows_before: list | None = None,
                            ocr_rows_after: list | None = None) -> dict:
    """回顶边界探测的静止测量（第 22.14 节）。

    判 stationary 必须同时满足：光流质量、0.01 比例静止阈值、
    至少 3 条唯一 OCR 行各轴变化 ≤0.01 比例；缺 OCR 行不认证静止。
    """
    params = params or default_params()
    before_roi = _crop(before_gray, roi)
    after_roi = _crop(after_gray, roi)
    tracking = _track(before_roi, after_roi,
                      _exclusion_mask(before_roi.shape, roi, exclusions), params)
    if not tracking["ok"]:
        return _result("unknown", tracking, params,
                       reason_code=tracking["reason_code"])
    width, height = roi["width"], roi["height"]
    stationary_cfg = params.get("stationary") or {}
    ratio = float(stationary_cfg.get("median_ratio", 0.01))
    dx, dy, mad_dy = tracking["dx"], tracking["dy"], tracking["mad_dy"]
    ocr_tolerance = ratio * max(width, height)
    matched = _match_ocr_rows(ocr_rows_before, ocr_rows_after, dy, ocr_tolerance)
    if abs(dy) >= float(params["min_displacement_ratio"]) * height:
        return _result("displaced", tracking, params, matched)
    flow_stationary = (abs(dx) <= ratio * width and abs(dy) <= ratio * height
                       and mad_dy <= ratio * height)
    if not flow_stationary:
        return _result("unknown", tracking, params, matched,
                       reason_code="between_thresholds")
    min_rows = int(stationary_cfg.get("min_unique_ocr_rows", 3))
    if matched < min_rows:
        return _result("unknown", tracking, params, matched,
                       reason_code="insufficient_ocr_rows")
    return _result("stationary", tracking, params, matched)

def measure_pair_quality(before_gray: np.ndarray, after_gray: np.ndarray,
                         roi: dict, exclusions: list | None = None,
                         params: dict | None = None) -> dict:
    """帧链单对测量（第 9.2 节链式累加用）。

    只做特征质量与相干性判定，不做位移阈值分类：链中间对的位移天然
    低于单次阈值，由链整体累加后再分类。质量失败返回 ok=False。
    """
    params = params or default_params()
    before_roi = _crop(before_gray, roi)
    after_roi = _crop(after_gray, roi)
    tracking = _track(before_roi, after_roi,
                      _exclusion_mask(before_roi.shape, roi, exclusions), params)
    if not tracking["ok"]:
        return {"ok": False, "reason_code": tracking["reason_code"],
                "dy_px": None, "dx_px": None,
                "valid_points": tracking.get("valid", 0),
                "mad_dy_px": None,
                "inlier_ratio": tracking.get("retained", 0.0)}
    width, height = roi["width"], roi["height"]
    dx, dy, mad_dy = tracking["dx"], tracking["dy"], tracking["mad_dy"]
    if (abs(dx) > float(params["max_horizontal_ratio"]) * width
            or mad_dy > float(params["max_dy_mad_ratio"]) * height):
        return {"ok": False, "reason_code": "incoherent_motion",
                "dy_px": None, "dx_px": None,
                "valid_points": tracking["valid"], "mad_dy_px": mad_dy,
                "inlier_ratio": tracking["retained"]}
    return {"ok": True, "reason_code": None, "dy_px": dy, "dx_px": dx,
            "valid_points": tracking["valid"], "mad_dy_px": mad_dy,
            "inlier_ratio": tracking["retained"]}


def ocr_vertical_shift(rows_before: list | None, rows_after: list | None) -> float | None:
    """首尾 OCR 行的纵向位移中位数（唯一文本名配对）；不足一对返回 None。

    供链式归并用同一次累计位移互证（2026-10-03 规格 §6.3）：只估计，
    不做门槛判定；矛盾判定在 aggregate_scroll_chain 内完成。
    """
    if not rows_before or not rows_after:
        return None
    shifts: list[float] = []
    before_counts: dict[str, int] = {}
    for row in rows_before:
        text = str(row.get("text") or "")
        if text.strip():
            before_counts[text] = before_counts.get(text, 0) + 1
    after_counts: dict[str, int] = {}
    for row in rows_after:
        text = str(row.get("text") or "")
        if text.strip():
            after_counts[text] = after_counts.get(text, 0) + 1
    for text, count in before_counts.items():
        if count != 1 or after_counts.get(text) != 1:
            continue
        y_before = next(row["y"] for row in rows_before if row["text"] == text)
        y_after = next(row["y"] for row in rows_after if row["text"] == text)
        shifts.append(float(y_after) - float(y_before))
    if not shifts:
        return None
    return float(np.median(shifts))


def aggregate_scroll_chain(pieces: list, roi_width: float, roi_height: float,
                           params: dict | None = None,
                           ocr_shift_px: float | None = None) -> dict:
    """共享帧链归并（2026-10-03 规格 §6.3）：逐对指标保留 + 累计分类。

    - 任一对失败 → 整链 unknown（reason_code 取该对原因），不判成没滚动；
    - 累计 dy/dx 后分类：位移门槛、静止阈值（含累计横移与逐对 MAD）、
      其余为 unknown；
    - 累计横向路径（sum|dx|）单独设限：逐对横移不过限但累计超限也是 unknown；
    - 提供首尾 OCR 位移（ocr_shift_px）时与累计 dy 互证，矛盾判 unknown；
      方向一致才算互证通过，不挑方便的结论。
    返回与 measure_displacement 同构的结果字典（chain_frames 额外给出）。
    """
    params = params or default_params()
    width = max(1.0, float(roi_width))
    height = max(1.0, float(roi_height))
    version = params["gesture_rules_version"]

    def _result(status: str, *, dy=None, dx=None, valid=None, ratio=None,
                mad=None, reason=None, matched=None) -> dict:
        return {"status": status, "dy_px": dy, "dx_px": dx,
                "inlier_ratio": round(ratio, 3) if ratio is not None else None,
                "valid_points": valid, "matched_ocr_rows": matched,
                "mad_dy_px": mad, "reason_code": reason,
                "gesture_rules_version": version,
                "chain_frames": len(pieces) + 1}

    if not pieces:
        return _result("unknown", reason="empty_chain")
    total_dy = 0.0
    total_dx = 0.0
    lateral_path = 0.0
    worst_ratio = 1.0
    worst_mad = 0.0
    min_points = None
    for piece in pieces:
        if not piece.get("ok"):
            return _result("unknown", valid=piece.get("valid_points"),
                           ratio=piece.get("inlier_ratio"),
                           reason=piece.get("reason_code") or "pair_failed")
        total_dy += float(piece["dy_px"])
        total_dx += float(piece["dx_px"])
        lateral_path += abs(float(piece["dx_px"]))
        worst_ratio = min(worst_ratio, float(piece["inlier_ratio"]))
        worst_mad = max(worst_mad, float(piece.get("mad_dy_px") or 0.0))
        points = piece.get("valid_points")
        if points is not None:
            min_points = points if min_points is None else min(min_points, points)
    if lateral_path > float(params["max_horizontal_ratio"]) * width:
        return _result("unknown", dy=total_dy, dx=total_dx, valid=min_points,
                       ratio=worst_ratio, mad=worst_mad,
                       reason="cumulative_lateral_excess")
    stationary_cfg = params.get("stationary") or {}
    stationary_ratio = float(stationary_cfg.get("median_ratio", 0.01))
    if abs(total_dy) >= float(params["min_displacement_ratio"]) * height:
        if ocr_shift_px is not None and ocr_shift_px * total_dy < 0:
            return _result("unknown", dy=total_dy, dx=total_dx, valid=min_points,
                           ratio=worst_ratio, mad=worst_mad,
                           reason="ocr_flow_conflict")
        return _result("displaced", dy=total_dy, dx=total_dx, valid=min_points,
                       ratio=worst_ratio, mad=worst_mad)
    if (abs(total_dy) <= stationary_ratio * height
            and abs(total_dx) <= stationary_ratio * width
            and worst_mad <= stationary_ratio * height
            # 明显上下往返（净位移接近零但逐对位移大）不是静止（§6.3）
            and all(abs(float(piece["dy_px"])) <= stationary_ratio * height
                    for piece in pieces)):
        return _result("stationary", dy=total_dy, dx=total_dx, valid=min_points,
                       ratio=worst_ratio, mad=worst_mad)
    return _result("unknown", dy=total_dy, dx=total_dx, valid=min_points,
                   ratio=worst_ratio, mad=worst_mad, reason="chain_uncertain")
