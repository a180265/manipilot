"""自动手势自检（页面模式与相机视觉 vision_only 模式；2026-09-30 起纯滑动自检默认视觉判定）：状态机、判据与有限调参（规范第 8、22.10 节）。

不负责拟合机械模型；物理动作经注入的可调用执行（运行时由能力通道传入，
fake 与真机使用相同函数）。run 状态按第 14.2 节持久化：物理动作前先写
executing；unknown 不续跑。
"""
from __future__ import annotations

import asyncio
import logging
import inspect
import json
import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from adapters.screen_ime.service import ScreenImeApiError
from drivers import gesture_program as gprog
from drivers import screen_gestures as sg
from drivers.daemon_protocol import SWIPE_DURATION_MAX_MS, SWIPE_DURATION_MIN_MS
from drivers.gesture_verdict import (
    contact_status_from_trace, controller_status_from_result,
)

logger = logging.getLogger(__name__)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


# 规范 22.10：默认动作顺序
ACTION_ORDER = ("scroll_up", "scroll_down", "system_back_left",
                "system_back_right", "double_tap", "system_home")
RUN_TERMINAL_STATES = ("passed", "partial", "failed", "unknown", "cancelled")


class CandidateSkipped(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def run_path(device_id: str, run_id: str, data_root: Path | None = None) -> Path:
    base = Path(data_root) if data_root else Path("data")
    return base / "devices" / device_id / "gesture_validation_runs" / f"{run_id}.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_scroll_program(action: str, candidate: dict, region: dict,
                         bundle: dict, frame: dict,
                         feed_limit: float | None = None) -> tuple[dict, dict]:
    """按候选与测试页区域生成 gesture-program.v1 两点程序。

    时长按距离与候选 F 计算；不能满足 150-5000ms 或 F 超限则跳过候选。
    """
    import math

    feed = float(candidate["feed"])
    limit = feed_limit if feed_limit is not None else 3000.0
    if feed > limit:
        raise CandidateSkipped(f"候选 F{feed:g} 超出接触进给上限 F{limit:g}")
    x = (region["leftPx"] + candidate["x"] * region["widthPx"]) / frame["root_width_px"]
    if action == "scroll_up":
        y_start_frac, y_end_frac = candidate["y_start"], candidate["y_end"]
    elif action == "scroll_down":
        y_start_frac, y_end_frac = candidate["y_end"], candidate["y_start"]
    else:
        raise ValueError(f"非滑动动作：{action}")
    y_start = (region["topPx"] + y_start_frac * region["heightPx"]) / frame["root_height_px"]
    y_end = (region["topPx"] + y_end_frac * region["heightPx"]) / frame["root_height_px"]
    start = gprog._resolve_normalized(x, y_start, bundle, frame)
    end = gprog._resolve_normalized(x, y_end, bundle, frame)
    distance_mm = math.hypot(end["robotX"] - start["robotX"],
                             end["robotY"] - start["robotY"])
    duration = int(math.ceil(distance_mm / feed * 60000))
    if not SWIPE_DURATION_MIN_MS <= duration <= SWIPE_DURATION_MAX_MS:
        raise CandidateSkipped(
            f"候选时长 {duration}ms 不在 "
            f"{SWIPE_DURATION_MIN_MS:g}-{SWIPE_DURATION_MAX_MS:g}ms 区间")
    program = {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": [{"points": [
            {"x": x, "y": y_start, "t_ms": 0},
            {"x": x, "y": y_end, "t_ms": duration}],
            "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0}],
    }
    meta = {"candidate": dict(candidate), "distance_mm": distance_mm,
            "duration_ms": duration, "feed": feed,
            "screen_points": [start, end]}
    return program, meta


def plan_scroll_trial(program: dict, bundle: dict, frame: dict) -> dict:
    return gprog.plan_program(program, bundle, frame)


# ---------- 系统返回（Back/Home）边缘候选（第 10.1-10.3 节） ----------

EDGE_KINDS = ("back_left", "back_right", "home")


def calibration_uncertainty_px(bundle: dict) -> float:
    """校准边缘不确定性 u：与 resolve_point 同一公式（第 10.1 节）。"""
    from drivers import calibration_bundle as cb
    xy = bundle.get("xy") or {}
    camera = (bundle.get("camera") or {}).get("cameraToScreen") or {}
    uncertainty = max(1.0, float(xy.get("maxResidualPx", cb.XY_RESIDUAL_MAX_PX)))
    uncertainty += max(0.0, float(camera.get("maxErrorPx",
                                            cb.CAMERA_REPROJECTION_MAX_PX)))
    return uncertainty


def _edge_inset(system_gesture_insets: Any, key: str) -> float:
    if not isinstance(system_gesture_insets, dict):
        raise gprog.GestureProgramError(
            "navigation_mode_unsupported", "手机未回报 systemGestureInsets")
    value = system_gesture_insets.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not value > 0:
        raise gprog.GestureProgramError(
            "navigation_mode_unsupported",
            f"系统手势 inset {key} 为 0、缺失或非法，本导航模式不可用")
    return float(value)


def edge_swipe_candidates(*, kind: str, system_gesture_insets: Any,
                          display_width_px: float, display_height_px: float,
                          frame: dict, uncertainty_px: float,
                          defaults: dict) -> list[dict]:
    """第 10.1/10.2/10.3 节：系统手势区与校准可执行区求交 → 边缘滑动候选。

    - display 域 inset 区间按 1:1 像素换算到校准 root 域（display - root 原点）；
    - 与校准可执行区 [u, root-u] 求交，交集宽度 < min_intersection_width 否则
      GestureProgramError("edge_gesture_unreachable")；
    - 起点固定取交集中心（不退到系统手势区外，也不放宽校准精度）；
    - inset 为 0/缺失 → GestureProgramError("navigation_mode_unsupported")；
    - 顺序：先横向(y/x)候选后向内长度（第 10.2 节），最多 max_candidates 项；
    - 返回候选含校准 root px 起点/终点；终点越出可执行区则跳过该组合。
    """
    if kind not in EDGE_KINDS:
        raise ValueError(f"未知边缘手势类型：{kind}")
    if gprog.is_internal_coordinate_frame(frame):
        raise gprog.GestureProgramError(
            "navigation_mode_unsupported",
            "系统边缘手势需要手机真实页面坐标与手势区元数据")
    origin_x, origin_y = (float(v) for v in frame["root_origin_display_px"])
    root_w = float(frame["root_width_px"])
    root_h = float(frame["root_height_px"])
    u = float(uncertainty_px)
    back_cfg = defaults["system_back"]
    home_cfg = defaults.get("system_home") or {}
    min_width = (float(home_cfg.get("min_intersection_width_factor",
                                    back_cfg["min_intersection_width_factor"]))
                 * u
                 + float(home_cfg.get("min_intersection_width_px",
                                      back_cfg["min_intersection_width_px"])))
    # F 沿用上滑已通过候选的原值（第 10.2 节），不自行取未验证的中间值
    feed = float(defaults["scroll_self_test"]["candidates"][0]["feed"])

    if kind == "back_left":
        inset = _edge_inset(system_gesture_insets, "left")
        zone_lo, zone_hi = -origin_x, inset - origin_x
    elif kind == "back_right":
        inset = _edge_inset(system_gesture_insets, "right")
        zone_lo = display_width_px - inset - origin_x
        zone_hi = display_width_px - origin_x
    else:
        inset = _edge_inset(system_gesture_insets, "bottom")
        zone_lo = display_height_px - inset - origin_y
        zone_hi = display_height_px - origin_y
    if kind == "home":
        exec_lo, exec_hi = u, root_h - u
    else:
        exec_lo, exec_hi = u, root_w - u
    lo, hi = max(zone_lo, exec_lo), min(zone_hi, exec_hi)
    if hi - lo < min_width:
        raise gprog.GestureProgramError(
            "edge_gesture_unreachable",
            f"系统手势区与校准可执行区交集宽度 {hi - lo:.2f}px 低于 "
            f"最低要求 {min_width:.2f}px")
    start_edge = (lo + hi) / 2.0

    if kind == "home":
        axis_fracs = home_cfg["x_candidates"]
        length_fracs = home_cfg["inward_lengths"]
        max_candidates = int(home_cfg.get("max_candidates", 6))
        length_span_px = float(display_height_px)  # 向上长度取 display 高
        prefix = "HM"
    else:
        axis_fracs = back_cfg["y_candidates"]
        length_fracs = back_cfg["inward_lengths"]
        max_candidates = int(back_cfg["max_candidates"])
        length_span_px = float(display_width_px)   # 向内长度取 display 宽
        prefix = "BL" if kind == "back_left" else "BR"

    candidates: list[dict] = []
    for axis_frac in axis_fracs:
        if kind == "home":
            cross = float(axis_frac) * display_width_px - origin_x
            if not u <= cross <= root_w - u:
                continue  # x 候选越出可执行范围，跳过
        else:
            cross = float(axis_frac) * display_height_px - origin_y
            if not u <= cross <= root_h - u:
                continue  # y 候选越出可执行范围，跳过
        for length_frac in length_fracs:
            length = float(length_frac) * length_span_px
            if kind == "back_left":
                start, end = (start_edge, cross), (start_edge + length, cross)
                if end[0] > root_w - u:
                    continue  # 终点越出可执行区，跳过该组合
            elif kind == "back_right":
                start, end = (start_edge, cross), (start_edge - length, cross)
                if end[0] < u:
                    continue
            else:  # home：向上（-y）
                start, end = (cross, start_edge), (cross, start_edge - length)
                if end[1] < u:
                    continue
            candidates.append({
                "id": f"{prefix}{len(candidates) + 1}",
                "kind": kind,
                "axis_frac": float(axis_frac),
                "length_frac": float(length_frac),
                "start": (round(start[0], 3), round(start[1], 3)),
                "end": (round(end[0], 3), round(end[1], 3)),
                "feed": feed,
            })
            if len(candidates) >= max_candidates:
                return candidates
    return candidates


def build_edge_program(candidate: dict, bundle: dict,
                       frame: dict) -> tuple[dict, dict]:
    """边缘候选（校准 root px）→ gesture-program.v1 两点程序。

    时长 = 距离/F*60000 向上取整并 clamp 到 150-5000ms；F 用已验证滑动参数
    candidate["feed"]（第 10.2 节），坐标归一化后与 build_scroll_program 同构。
    """
    import math

    feed = float(candidate["feed"])
    (sx, sy), (ex, ey) = candidate["start"], candidate["end"]
    x0, y0 = sx / frame["root_width_px"], sy / frame["root_height_px"]
    x1, y1 = ex / frame["root_width_px"], ey / frame["root_height_px"]
    start = gprog._resolve_normalized(x0, y0, bundle, frame)
    end = gprog._resolve_normalized(x1, y1, bundle, frame)
    distance_mm = math.hypot(end["robotX"] - start["robotX"],
                             end["robotY"] - start["robotY"])
    duration = int(math.ceil(distance_mm / feed * 60000))
    duration = int(min(SWIPE_DURATION_MAX_MS,
                       max(SWIPE_DURATION_MIN_MS, duration)))
    program = {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": [{"points": [
            {"x": x0, "y": y0, "t_ms": 0},
            {"x": x1, "y": y1, "t_ms": duration}],
            "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0}],
    }
    meta = {"candidate": dict(candidate), "distance_mm": distance_mm,
            "duration_ms": duration, "feed": feed,
            "screen_points": [start, end]}
    return program, meta


def evaluate_back_trial(*, controller_result: Any, trace: Any,
                        page_events: Any, verify_result: Any) -> dict:
    """第 22.13 节 Back 单次判定：页面事件 + verify_page 双通道。

    - completed + back_committed（+verify_page 未注入或 confirmed）→ trial_passed；
    - completed + 完整轨迹 + 无提交 + verify_page confirmed=False → no_effect；
    - 只有 back_cancelled / 缺正向证据 → unknown（不推断下一候选安全）；
    - 无 DOWN 且采集完整 → no_contact（不加深 Z、不盲调边缘偏移）。
    """
    controller_status = controller_status_from_result(controller_result)
    if controller_status != "completed":
        return _outcome("unknown", "controller_unknown", controller_status,
                        "not_observed", "unknown")
    events = [e for e in (page_events or []) if isinstance(e, dict)]
    committed = any(e.get("kind") == "back_committed"
                    and e.get("committed") is True for e in events)
    cancelled = any(e.get("kind") == "back_cancelled" for e in events)
    contact = contact_status_from_trace(trace)
    if committed:
        if verify_result is None or verify_result.get("confirmed") is True:
            # 触发系统导航允许终止触摸事件：明确提交 + 目标页证据替代完整 UP
            return _outcome("trial_passed", None, controller_status, contact,
                            "verified")
        return _outcome("unknown", "page_evidence_uncertain", controller_status,
                        contact, "unknown")
    if page_events is None and verify_result is None:
        # 页面事件与 verify_page 双通道都缺失：明确跳过，不冒充通过
        return _outcome("skipped", "skipped", controller_status, contact,
                        "not_checked")
    touch_events = (trace or {}).get("events") or []
    has_down = any(e.get("action") == "down" for e in touch_events)
    if not has_down and trace is not None:
        return _outcome("no_contact", "no_contact", controller_status,
                        "not_observed", "no_effect")
    full_trace = trace is not None and contact == "continuous"
    if full_trace and verify_result is not None \
            and verify_result.get("confirmed") is False:
        # 完整 DOWN/MOVE/UP + 无提交 + 页面仍稳定在测试页 → no_effect
        return _outcome("no_effect", "effect_not_observed", controller_status,
                        contact, "no_effect")
    reason = "back_cancelled" if cancelled else "back_evidence_missing"
    return _outcome("unknown", reason, controller_status, contact, "unknown")


def evaluate_home_trial(*, controller_result: Any, trace: Any,
                        verify_result: Any) -> dict:
    """第 10.3/22.13 节 Home 单次判定：桌面模板验证（verify_page）。"""
    controller_status = controller_status_from_result(controller_result)
    if controller_status != "completed":
        return _outcome("unknown", "controller_unknown", controller_status,
                        "not_observed", "unknown")
    contact = contact_status_from_trace(trace)
    if verify_result is not None and verify_result.get("confirmed") is True:
        # 控制器完整 + 新画面匹配桌面锚点；触发 Home 允许终止触摸事件
        return _outcome("trial_passed", None, controller_status, contact,
                        "verified")
    touch_events = (trace or {}).get("events") or []
    has_down = any(e.get("action") == "down" for e in touch_events)
    if not has_down and trace is not None:
        return _outcome("no_contact", "no_contact", controller_status,
                        "not_observed", "no_effect")
    if verify_result is not None and verify_result.get("confirmed") is False \
            and trace is not None and contact == "continuous":
        return _outcome("no_effect", "effect_not_observed", controller_status,
                        contact, "no_effect")
    return _outcome("unknown", "home_evidence_missing", controller_status,
                    contact, "unknown")


def _point_segment_distance(px: float, py: float, ax: float, ay: float,
                            bx: float, by: float) -> float:
    import math
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def evaluate_scroll_trial(*, controller_result: Any, trace: Any, plan: dict,
                          bundle: dict, defaults: dict,
                          camera_result: dict | None = None) -> dict:
    """单次列表自检：触摸轨迹与独立摄像头位移均须通过。"""
    thresholds = defaults["scroll_self_test"]["pass_thresholds"]
    tolerance = max(float(thresholds["endpoint_tolerance_min_px"]),
                    thresholds["endpoint_tolerance_factor"]
                    * float((bundle.get("xy") or {}).get("maxResidualPx", 3.0)))
    controller_status = controller_status_from_result(controller_result)
    if controller_status != "completed":
        return _outcome("unknown", "controller_unknown", controller_status,
                        "not_observed", "unknown")
    position = (controller_result or {}).get("position") or {}
    hover = (plan.get("profile") or {}).get("hoverZ")
    if hover is not None and isinstance(position.get("z"), (int, float)):
        if abs(float(position["z"]) - float(hover)) > 0.05:
            return _outcome("unknown", "motion_not_retracted", controller_status,
                            "not_observed", "unknown")
    contact = contact_status_from_trace(trace)
    if camera_result is not None and camera_result.get("status") not in (
            "displaced", "stationary"):
        return _outcome("unknown", "camera_displacement_unknown",
                        controller_status, contact, "unknown")
    events = (trace or {}).get("events") or []
    if not events:
        return _outcome("no_contact", "no_contact", controller_status, contact,
                        "no_effect")
    if contact != "continuous":
        return _outcome("contact_broken", "contact_broken", controller_status,
                        contact, "no_effect")
    points = plan["program"]["strokes"][0]["points"]
    root_w, root_h = _frame_size(bundle)
    start_px = (points[0]["x"] * root_w, points[0]["y"] * root_h)
    end_px = (points[1]["x"] * root_w, points[1]["y"] * root_h)
    first, last = events[0], events[-1]
    if (abs(first["xPx"] - start_px[0]) > tolerance
            or abs(first["yPx"] - start_px[1]) > tolerance
            or abs(last["xPx"] - end_px[0]) > tolerance
            or abs(last["yPx"] - end_px[1]) > tolerance):
        return _outcome("mapping_mismatch", "mapping_mismatch", controller_status,
                        contact, "no_effect")
    import math
    deviations = sorted(_point_segment_distance(e["xPx"], e["yPx"],
                                                start_px[0], start_px[1],
                                                end_px[0], end_px[1])
                        for e in events)
    # 分位取自配置（scroll_self_test.pass_thresholds.path_deviation_percentile），
    # 写死 0.95 会让该配置项成为假入口
    percentile = float((defaults.get("scroll_self_test") or {})
                       .get("pass_thresholds", {})
                       .get("path_deviation_percentile", 0.95))
    p95 = deviations[min(len(deviations) - 1,
                         int(math.ceil(percentile * len(deviations))) - 1)]
    if p95 > tolerance:
        return _outcome("mapping_mismatch", "mapping_mismatch", controller_status,
                        contact, "no_effect")
    plan_len = abs(end_px[1] - start_px[1])
    direction = -1 if end_px[1] < start_px[1] else 1
    ordered = sorted(events, key=lambda e: e["tMs"])
    net = abs(ordered[-1]["yPx"] - ordered[0]["yPx"])
    reverse = sum(max(0.0, -direction * (cur["yPx"] - prev["yPx"]))
                  for prev, cur in zip(ordered, ordered[1:]))
    if net < thresholds["net_displacement_min_ratio"] * plan_len \
            or reverse > thresholds["reverse_max_ratio"] * plan_len:
        return _outcome("direction_mismatch", "direction_mismatch",
                        controller_status, contact, "no_effect")
    scroll = (trace or {}).get("scroll")
    if not isinstance(scroll, dict):
        return _outcome("unknown", "trace_incomplete", controller_status,
                        contact, "unknown")
    if scroll["resetCounterBefore"] != scroll["resetCounterAfter"]:
        return _outcome("unknown", "fixture_reset_during_trial", controller_status,
                        contact, "unknown")
    if not scroll.get("settled"):
        return _outcome("unknown", "scroll_not_settled", controller_status,
                        contact, "unknown")
    row_h = scroll["rowHeightPx"]
    for key_y, key_row, key_off in (("beforeY", "firstVisibleRowBefore", "rowOffsetBeforePx"),
                                    ("afterY", "firstVisibleRowAfter", "rowOffsetAfterPx")):
        y = scroll[key_y]
        if int(y // row_h) != scroll[key_row] or int(y % row_h) != scroll[key_off]:
            return _outcome("unknown", "row_offset_inconsistent", controller_status,
                            contact, "unknown")
    delta = scroll["afterY"] - scroll["beforeY"]
    viewport = scroll["viewportHeightPx"]
    if abs(delta) < thresholds["scroll_min_ratio"] * viewport:
        return _outcome("no_effect", "effect_not_observed", controller_status,
                        contact, "no_effect")
    # Android scrollY 语义（2026-09-23 真机定案）：手指上滑（direction=-1）
    # = 内容上移 = scrollY 增大；手指下滑（direction=1）= scrollY 减小。
    # 实测：手指上移 974px → scrollY 7708→8660(+952)，1:1 跟随。
    if direction == -1 and delta <= 0 or direction == 1 and delta >= 0:
        return _outcome("direction_mismatch", "direction_mismatch",
                        controller_status, contact, "no_effect")
    boundary_before = scroll["boundaryBefore"]
    # 边界方向（2026-09-23 真机定案）：上滑(-1)=scrollY 增大，已在大 scrollY
    # （bottom 边界）时才无空间；下滑(+1)受阻于 top。写反会把能滚的组合判
    # fixture_at_boundary 终止整个 run（R4 P1-2）。
    if (direction == -1 and boundary_before == "bottom") or \
            (direction == 1 and boundary_before == "top"):
        return _outcome("unknown", "fixture_at_boundary", controller_status,
                        contact, "unknown")
    status = (camera_result or {}).get("status")
    if status == "stationary":
        return _outcome("no_effect", "camera_stationary", controller_status,
                        contact, "no_effect")
    if status != "displaced":
        return _outcome("unknown", "camera_displacement_unknown", controller_status,
                        contact, "unknown")
    dy = camera_result.get("dy_px")
    if type(dy) not in (int, float) or not math.isfinite(dy):
        return _outcome("unknown", "camera_displacement_unknown", controller_status,
                        contact, "unknown")
    if (direction == -1 and dy >= 0) or (direction == 1 and dy <= 0):
        return _outcome("direction_mismatch", "camera_direction_mismatch",
                        controller_status, contact, "no_effect")
    return _outcome("trial_passed", None, controller_status, contact, "verified")


def evaluate_double_tap_trial(*, controller_result: Any, trace: Any,
                              target_px: tuple[float, float],
                              tolerance: float) -> dict:
    """第 11.1 节：两次接触 + 手势识别器回报才认证。"""
    controller_status = controller_status_from_result(controller_result)
    if controller_status != "completed":
        return _outcome("unknown", "controller_unknown", controller_status,
                        "not_observed", "unknown")
    events = sorted((trace or {}).get("events") or [], key=lambda e: e["tMs"])
    actions = [e.get("action") for e in events]
    downs = [e for e in events if e.get("action") == "down"]
    ups = [e for e in events if e.get("action") == "up"]
    recognition = (trace or {}).get("recognition") or {}
    # 双击的"连续"= 两个各自完整的接触周期（第 5.5 节），不适用单接触规则
    two_cycles = (actions == ["down", "up", "down", "up"]
                  and len({e.get("pointerId") for e in events}) == 1)
    if "cancel" in actions or (trace or {}).get("droppedSampleCount"):
        contact = "broken"
    elif two_cycles:
        contact = "continuous"
    else:
        contact = "unknown"
    if len(downs) != 2 or len(ups) != 2 or not two_cycles:
        return _outcome("contact_broken", "contact_broken", controller_status,
                        contact, "no_effect")
    if any(abs(e["xPx"] - target_px[0]) > tolerance
           or abs(e["yPx"] - target_px[1]) > tolerance for e in downs + ups):
        return _outcome("mapping_mismatch", "mapping_mismatch", controller_status,
                        contact, "no_effect")
    if recognition.get("recognized") is not True:
        return _outcome("no_effect", "effect_not_observed", controller_status,
                        contact, "no_effect")
    return _outcome("trial_passed", None, controller_status, contact, "verified")


def _outcome(classification, reason_code, controller_status, contact_status,
             effect_status):
    return {"classification": classification, "reason_code": reason_code,
            "controller_status": controller_status,
            "contact_status": contact_status,
            "effect_status": effect_status}


def _frame_size(bundle: dict) -> tuple[float, float]:
    screen = bundle.get("screen") or {}
    width = float(screen.get("screenWidth") or 0)
    height = float(screen.get("screenHeight") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("校准档案缺少屏幕尺寸，无法换算滚动期望像素")
    return width, height


def sort_actions(actions: list[str]) -> list[str]:
    return sorted(actions, key=lambda a: ACTION_ORDER.index(a))


class GestureValidationRunner:
    """自检 run：候选顺序、连续 3 次、预算、复位竞态、unknown 不续跑。"""

    def __init__(self, *, defaults: dict, bundle: dict, coordinate_frame: dict,
                 device_id: str, ime_device_id: str, coordinator,
                 execute_swipe: Callable, execute_double_tap: Callable,
                 capture_frame: Callable, verify_page: Callable | None = None,
                 measure_scroll: Callable | None = None,
                 vision_only: bool = False,
                 data_root: Path | None = None,
                 run_id: str | None = None,
                 budget_override: dict | None = None,
                 feed_limit_override: float | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep=asyncio.sleep):
        self.defaults = defaults
        self.bundle = bundle
        self.frame = coordinate_frame
        self.device_id = device_id
        self.ime_device_id = ime_device_id
        self.coordinator = coordinator
        self.execute_swipe = execute_swipe
        self.execute_double_tap = execute_double_tap
        self.capture_frame = capture_frame
        self.measure_scroll = measure_scroll
        # 视觉优先模式（拍板 2026-09-30）：手势页心跳不可用的手机上跳过
        # 页面会话/握手/trace，滑动效果以相机前后帧光流判定并直接发证。
        self.vision_only = bool(vision_only)
        self._region_override = None
        # 页面验证器（第 10/22.13 节）：Back 验证父页、Home 验证桌面模板；
        # None 时 Home 直接 skipped(launcher_template_missing)，Back 退回页面事件。
        self.verify_page = verify_page
        self.data_root = data_root
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        budget = defaults["self_test_budget"]
        self.timeout_ms = int(budget_override and budget_override.get(
            "timeout_ms", budget["timeout_ms"]) or budget["timeout_ms"])
        self.physical_limit = int(budget_override and budget_override.get(
            "max_physical_actions", budget["max_physical_actions"])
            or budget["max_physical_actions"])
        self.consecutive_needed = int(budget["consecutive_passes_required"])
        self.feed_limit = (feed_limit_override if feed_limit_override is not None
                           else 3000.0)
        self._clock = clock
        self._sleep = sleep
        self._started = clock()
        self._physical_count = 0
        self._transitions: list[dict] = []
        self._run: dict = {}
        self._session_id: str | None = None
        self._terminal = False

    # ---------- 持久化 ----------

    def _transition(self, state: str, detail: str | None = None) -> None:
        self._transitions.append({"state": state, "at": _now_iso(),
                                  "detail": detail})
        self._run.update({"state": state if state in RUN_TERMINAL_STATES
                          else self._run.get("state", "running"),
                          "transitions": list(self._transitions),
                          "budget": self._budget_view(),
                          "updated_at": _now_iso()})
        _atomic_write_json(run_path(self.device_id, self.run_id, self.data_root),
                           self._run)

    def _budget_view(self) -> dict:
        return {"physical_count": self._physical_count,
                "physical_limit": self.physical_limit,
                "elapsed_ms": int((self._clock() - self._started) * 1000),
                "timeout_ms": self.timeout_ms}

    def _init_run(self, actions: list[str]) -> None:
        from drivers import screen_gestures as sg_mod
        self._run = {
            "run_id": self.run_id, "device_id": self.device_id,
            "actions_requested": list(actions), "state": "queued",
            "action_results": [], "transitions": [],
            "scroll_observations": [],
            "input_binding": sg_mod.binding_snapshot(self.bundle),
            "coordinate_frame_hash": self.frame["coordinate_frame_hash"],
            "gesture_rules_version": self.defaults["gesture_rules_version"],
            "published_profile_version": None,
            "budget": self._budget_view(),
            "created_at": _now_iso(), "updated_at": _now_iso(),
        }
        self._transition("queued")

    def _budget_left(self) -> bool:
        return (self._physical_count < self.physical_limit
                and (self._clock() - self._started) * 1000 < self.timeout_ms)

    async def _execute_physical(self, action: str, callback, params):
        try:
            return await _maybe_await(callback(params))
        except Exception as exc:
            if getattr(exc, "motion_not_started", False) is not True:
                raise
            # 调用前保守计数；仅受控门禁明确证明未进驱动时撤销本次计数。
            self._physical_count -= 1
            reason = (getattr(exc, "reason_code", None) or getattr(exc, "key", None)
                      or "gesture_precondition_failed")
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code=reason,
                controller="not_started")) from exc

    def _finish_action(self, result: dict) -> None:
        self._run["action_results"].append(result)
        self._transition("evaluating", result["gesture_id"])

    # ---------- 主流程 ----------

    async def run(self, actions: list[str]) -> dict:
        actions = sort_actions(list(actions))
        self.frame = gprog.frame_for_internal_gesture(self.bundle, self.frame)
        self._init_run(actions)
        self._transition("precheck")
        try:
            binding = sg.binding_snapshot(self.bundle)
            gprog.validate_coordinate_frame(self.frame, self.bundle)
        except (ValueError, gprog.GestureProgramError) as exc:
            self._transition("failed", str(exc))
            return self._finalize("failed")
        if self.vision_only:
            # 纵深防线（插件层已挡，runner 不信任调用方）：vision 模式没有
            # 页面会话，tap/back/home 试用无协调器可走——只允许纯滑动集。
            if any(a not in ("scroll_up", "scroll_down") for a in actions):
                self._transition("failed", "vision_requires_scroll_only")
                self._run["stop_reason"] = "vision_requires_scroll_only"
                return self._finalize("failed")
            # 视觉优先（拍板 2026-09-30）：手势页心跳不可用的手机上跳过
            # 页面会话，滑动效果以相机前后帧光流判定并直接发证。
            self._transition("vision_only",
                             "手势页心跳不可用：跳过页面会话，滑动以相机光流判定")
        else:
            self._transition("waiting_phone")
            # 第 8.1 节 waiting_phone：校准完成自动触发时手机仍在校准页，
            # 需给操作者最多 30 秒切到「手势测试页」；期间每秒重试注册。
            wait_budget = float(self.defaults["self_test_budget"].get(
                "waiting_phone_timeout_ms", 30000)) / 1000.0
            deadline = self._clock() + wait_budget
            session = None
            while True:
                try:
                    session = await self.coordinator.create_session(
                        device_id=self.device_id, ime_device_id=self.ime_device_id,
                        binding=binding, actions=actions, run_id=self.run_id,
                        session_id=str(uuid.uuid4()))
                    break
                except ScreenImeApiError as exc:
                    if exc.code != "client_not_foreground" or self._clock() >= deadline:
                        self._transition("failed", exc.code)
                        self._run["stop_reason"] = exc.code
                        await self._safe_finish_session("failed")
                        return self._finalize("failed")
                    await self._sleep(1.0)
            self._session_id = session["sessionId"]

        run_state = "failed"
        stop_reason = None
        try:
            for action in actions:
                if self._terminal:
                    break
                if not self._budget_left():
                    run_state = "partial" if self._any_valid() else "failed"
                    stop_reason = "gesture_budget_exhausted"
                    break
                try:
                    outcome = await self._run_action(action)
                except _RunUnknown as exc:
                    outcome = exc.result
                    run_state, stop_reason = "unknown", outcome.get("reason_code")
                    self._terminal = True
                except _RunStop as exc:
                    outcome = exc.result
                    run_state = "partial" if self._any_valid() else "failed"
                    stop_reason = outcome.get("reason_code")
                    self._terminal = True
                if outcome is not None:
                    self._finish_action(outcome)
            else:
                # 最后一个动作以 unknown/停止结束时保持已定终态，
                # 不得被 _final_state 改写（第 8.1/14.2 节 unknown 不续跑）
                if not self._terminal:
                    run_state = self._final_state()
            if not self._terminal and stop_reason is None:
                run_state = self._final_state()
        except BaseException as exc:
            # 会话泄漏防线：非预期异常（执行器超时取消、驱动拒绝等）也必须
            # 终结协调器会话并落终态——单活动会话互斥会把该设备的后续
            # 全部自检拒之门外，只能重启进程恢复；清理后重抛由任务层归约。
            self._run["stop_reason"] = f"runner_exception:{type(exc).__name__}"
            self._transition("unknown")
            await self._safe_finish_session("unknown")
            raise
        if stop_reason:
            self._run["stop_reason"] = stop_reason
        await self._safe_finish_session(run_state)
        return self._finalize(run_state)

    def _final_state(self) -> str:
        results = self._run["action_results"]
        if not results:
            return "failed"
        # 全部 valid 即 passed：预算恰在最后一次判定后耗尽不改变既成事实，
        # 判 partial 会让任务层按 failed 处理（消息与事实矛盾）
        if all(r["state"] == "valid" for r in results):
            return "passed"
        if not self._budget_left():
            return "partial" if self._any_valid() else "failed"
        if any(r["state"] == "valid" for r in results):
            return "partial"
        return "failed"

    def _any_valid(self) -> bool:
        return any(r["state"] == "valid" for r in self._run["action_results"])

    async def _safe_finish_session(self, outcome: str) -> None:
        try:
            if self._session_id:
                await self.coordinator.finish_session(self._session_id, outcome)
        except Exception as exc:
            # 收尾失败不阻断 run 终态落盘，但必须留痕（否则会话泄漏无从排查）
            logger.warning("自检 run %s 终结协调器会话失败（%s）：%s",
                           self.run_id, outcome, exc)

    def _finalize(self, state: str) -> dict:
        self._run["state"] = state
        self._run["budget"] = self._budget_view()
        self._run["updated_at"] = _now_iso()
        _atomic_write_json(run_path(self.device_id, self.run_id, self.data_root),
                           self._run)
        return json.loads(json.dumps(self._run, ensure_ascii=False))

    async def _run_action(self, action: str) -> dict:
        if action.startswith("system_") and gprog.is_internal_coordinate_frame(self.frame):
            return self._action_result(
                action, state="unsupported",
                reason_code="navigation_mode_unsupported",
                controller="not_started")
        if action in ("scroll_up", "scroll_down"):
            return await self._run_scroll(action)
        if action == "double_tap":
            return await self._run_double_tap()
        if action in ("system_back_left", "system_back_right"):
            return await self._run_system_back(action)
        if action == "system_home":
            return await self._run_system_home()
        # 未知名动作不冒充通过
        return self._action_result(action, state="unverified",
                                   reason_code="skipped",
                                   controller="not_started")

    async def _run_scroll(self, action: str) -> dict:
        candidates = self.defaults["scroll_self_test"]["candidates"]
        streak = 0
        streak_program_hash = None
        candidate_index = 0
        tried_s2 = False
        trial_ids: list[str] = []
        last_reason = None
        while candidate_index < len(candidates):
            if not self._budget_left():
                return self._action_result(
                    action, state="unverified",
                    reason_code="gesture_budget_exhausted", trial_ids=trial_ids)
            candidate = candidates[candidate_index]
            try:
                program, meta = build_scroll_program(
                    action, candidate, self._region(), self.bundle, self.frame,
                    feed_limit=self.feed_limit)
            except CandidateSkipped as exc:
                self._transition("evaluating", f"skip:{exc.reason}")
                candidate_index += 1
                last_reason = f"candidate_skipped:{exc.reason}"
                continue
            trial_id = str(uuid.uuid4())
            trial_ids.append(trial_id)
            outcome = await self._execute_scroll_trial(action, trial_id, program)
            classification = outcome["classification"]
            last_reason = outcome["reason_code"] or classification
            if classification == "trial_passed":
                program_hash = gprog.program_hash(program)
                if program_hash != streak_program_hash:
                    streak = 0
                    streak_program_hash = program_hash
                streak += 1
                if streak >= self.consecutive_needed:
                    return self._action_result(
                        action, state="valid", reason_code=None,
                        trial_ids=trial_ids,
                        controller=outcome["controller_status"],
                        contact=outcome["contact_status"],
                        effect="verified", program=program)
                continue
            streak = 0
            if classification == "unknown":
                raise _RunUnknown(self._action_result(
                    action, state="unverified", reason_code=outcome["reason_code"],
                    trial_ids=trial_ids,
                    controller=outcome["controller_status"],
                    contact=outcome["contact_status"],
                    effect=outcome["effect_status"]))
            if classification == "no_effect":
                candidate_index += 1
                continue
            if classification == "contact_broken":
                if not tried_s2 and candidate_index != 1:
                    tried_s2 = True
                    candidate_index = 1  # 仅尝试一次 S2
                    continue
                raise _RunStop(self._action_result(
                    action, state="unverified", reason_code="contact_broken",
                    trial_ids=trial_ids))
            # no_contact / mapping_mismatch / direction_mismatch：立即结束 run
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code=classification,
                trial_ids=trial_ids))
        return self._action_result(action, state="unverified",
                                   reason_code=last_reason or "effect_not_observed",
                                   trial_ids=trial_ids)

    @staticmethod
    def _reversed_scroll_plan(plan: dict) -> dict:
        """复位滑计划：把被测行程反向（列表推回起始位置的视觉替代）。"""
        import copy

        reversed_plan = copy.deepcopy(plan)
        for stroke in reversed_plan.get("program", {}).get("strokes", []):
            points = stroke.get("points") or []
            if len(points) >= 2:
                stroke["points"] = [points[1], points[0]]
        return reversed_plan

    async def _execute_scroll_trial_vision(self, action: str, trial_id: str,
                                           program: dict) -> dict:
        """视觉优先滑动试用（拍板 2026-09-30）：无页面会话/握手/trace——
        复位滑恢复列表位置 → 前帧 → 被测滑 → 后帧 → 相机光流判定。
        方向由机械行程保证（MPos 差值=计划向量），效果以相机位移为准。"""
        plan = plan_scroll_trial(program, self.bundle, self.frame)
        self._transition("preparing_trial", f"{action}:{trial_id}:vision")
        # 复位滑也是真实臂动作：与被测滑一样计入物理预算（否则 manifest
        # 36 次限额实际可执行 72 次）。计数在执行前登记，motion_not_started
        # 回退由 _execute_physical 统一撤销——与页面模式记账顺序一致。
        reset_plan = self._reversed_scroll_plan(plan)
        self._physical_count += 1
        await self._execute_physical(action, self.execute_swipe, reset_plan)
        await self._sleep(0.6)
        self._transition("executing", trial_id)
        before = await _maybe_await(self.capture_frame())
        self._physical_count += 1
        controller_result = await self._execute_physical(action, self.execute_swipe, plan)
        self._transition("collecting", trial_id)
        await self._sleep(0.8)
        after = await _maybe_await(self.capture_frame())
        before_id = (before or {}).get("evidence_id") if isinstance(before, dict) else None
        after_id = (after or {}).get("evidence_id") if isinstance(after, dict) else None
        camera_result = None
        if not before_id or not after_id:
            # 取证故障（缺帧）按 unknown 上报，不得降级为效果判定
            camera_result = {"status": "unknown",
                             "reason_code": "camera_evidence_missing"}
        elif before_id == after_id:
            camera_result = {"status": "unknown",
                             "reason_code": "camera_frame_not_advanced"}
        elif self.measure_scroll is not None:
            try:
                camera_result = await _maybe_await(self.measure_scroll(
                    before, after, self._region_override or self._region()))
            except Exception:
                camera_result = {"status": "unknown",
                                 "reason_code": "camera_measure_failed"}
        if camera_result is None:
            camera_result = {"status": "unknown",
                             "reason_code": "camera_measure_missing"}
        controller_status = controller_status_from_result(controller_result)
        # 判定门与页面路径同源：控制器未确认完成 → unknown；光流位移方向
        # 必须与计划行程同向（上滑 dy<0 / 下滑 dy>0）且有限——只看 status
        # 会把反向光流也发证。
        points = (plan.get("program") or {}).get("strokes", [{}])[0].get("points") or []
        direction = -1 if len(points) >= 2 and points[-1].get("y", 0) < points[0].get("y", 0) else 1
        dy = camera_result.get("dy_px")
        if camera_result.get("status") == "displaced" and controller_status != "completed":
            outcome = _outcome("unknown", "controller_unknown", controller_status,
                               "not_observed", "unknown")
        elif camera_result.get("status") == "displaced" and (
                type(dy) not in (int, float) or not math.isfinite(dy)
                or (direction == -1 and dy >= 0) or (direction == 1 and dy <= 0)):
            outcome = _outcome("no_effect", "vision_direction_mismatch",
                               controller_status, "not_observed", "no_effect")
        elif camera_result.get("status") == "displaced":
            # 分类与页面路径一致（trial_passed 进 3 连计数）
            outcome = _outcome("trial_passed", "vision_verified", controller_status,
                               "not_observed", "verified")
        elif camera_result.get("status") == "stationary":
            outcome = _outcome("no_effect", "vision_no_displacement",
                               controller_status, "not_observed", "no_effect")
        else:
            outcome = _outcome("unknown", "vision_camera_unknown", controller_status,
                               "not_observed", "unknown")
        # 取证链与页面路径同构落盘：事后可回溯每次发证的帧与光流结论
        outcome["before_frame"] = before
        outcome["after_frame"] = after
        outcome["camera_observation"] = camera_result
        self._run["scroll_observations"].append({
            "trial_id": trial_id, "action": action,
            "classification": outcome["classification"],
            "before_evidence_id": before_id,
            "after_evidence_id": after_id,
            "camera_observation": camera_result,
        })
        return outcome

    async def _execute_scroll_trial(self, action: str, trial_id: str,
                                    program: dict) -> dict:
        if self.vision_only:
            return await self._execute_scroll_trial_vision(
                action, trial_id, program)
        self._transition("preparing_trial", f"{action}:{trial_id}")
        try:
            await self.coordinator.prepare_trial(
                self._session_id, trial_id,
                {"kind": "reset_list", "payload": {"offsetFraction": 0.5},
                 "coordinateFrame": self.frame,
                 "calibrationFingerprint":
                     self._run["input_binding"]["calibration_fingerprint"]})
        except ScreenImeApiError as exc:
            self._transition("failed", exc.code)
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code=exc.code)) from exc
        ready = await self.coordinator.wait_ready(self._session_id, trial_id,
                                                  self.defaults[
                                                      "self_test_budget"][
                                                      "waiting_phone_timeout_ms"])
        if not ready.get("ready"):
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code="trial_not_ready"))
        self._region_override = (ready.get("readyPayload") or {}).get("fixtureRegion")
        try:
            await self.coordinator.mark_executing(self._session_id, trial_id)
        except ScreenImeApiError as exc:
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code=exc.code)) from exc
        # 物理动作前持久写 executing；写失败则不动（第 8.1 节）
        self._transition("executing", trial_id)
        self._physical_count += 1
        plan = plan_scroll_trial(program, self.bundle, self.frame)
        before = await _maybe_await(self.capture_frame())
        controller_result = await self._execute_physical(action, self.execute_swipe, plan)
        self._transition("collecting", trial_id)
        evidence = await self.coordinator.wait_evidence(
            self._session_id, trial_id,
            int(self.defaults["command_timing"]["collect_grace_ms"]) + 3500)
        after = await _maybe_await(self.capture_frame())
        trace = evidence.get("trace")
        camera_result = None
        if (self.measure_scroll is not None and isinstance(before, dict)
                and isinstance(after, dict) and before.get("evidence_id")
                and after.get("evidence_id")
                and before["evidence_id"] != after["evidence_id"]):
            try:
                camera_result = await _maybe_await(self.measure_scroll(
                    before, after, self._region_override or self._region()))
            except Exception as exc:
                logger.warning("自检 %s 摄像头位移测量失败：%s", trial_id, exc)
                camera_result = {"status": "unknown",
                                 "reason_code": "camera_measure_failed"}
        if camera_result is None:
            camera_result = {"status": "unknown",
                             "reason_code": "camera_evidence_missing"}
        outcome = evaluate_scroll_trial(
            controller_result=controller_result, trace=trace, plan=plan,
            bundle=self.bundle, defaults=self.defaults,
            camera_result=camera_result)
        trial_state = {"trial_passed": "passed", "no_effect": "failed",
                       "contact_broken": "failed", "no_contact": "failed",
                       "mapping_mismatch": "failed",
                       "direction_mismatch": "failed",
                       "unknown": "unknown"}.get(outcome["classification"],
                                                  "unknown")
        await self.coordinator.finish_trial(self._session_id, trial_id, trial_state)
        outcome["before_frame"] = before
        outcome["after_frame"] = after
        outcome["camera_observation"] = camera_result
        self._run["scroll_observations"].append({
            "trial_id": trial_id, "action": action,
            "classification": outcome["classification"],
            "before_evidence_id": (before or {}).get("evidence_id")
            if isinstance(before, dict) else None,
            "after_evidence_id": (after or {}).get("evidence_id")
            if isinstance(after, dict) else None,
            "camera_observation": camera_result,
        })
        return outcome

    async def _run_double_tap(self) -> dict:
        region = self._region()
        target = (region["leftPx"] + 0.5 * region["widthPx"],
                  region["topPx"] + 0.5 * region["heightPx"])
        primitive = {
            "kind": "calibrated_double_tap",
            "point": {"x": target[0] / self.frame["root_width_px"],
                      "y": target[1] / self.frame["root_height_px"]},
            "binding": self._run["input_binding"], "interval_ms": 50,
        }
        trial_ids: list[str] = []
        streak = 0
        tolerance = max(12.0, 2 * float(
            (self.bundle.get("xy") or {}).get("maxResidualPx", 3.0)))
        while streak < self.consecutive_needed:
            if not self._budget_left():
                return self._action_result(
                    "double_tap", state="unverified",
                    reason_code="gesture_budget_exhausted", trial_ids=trial_ids)
            trial_id = str(uuid.uuid4())
            trial_ids.append(trial_id)
            self._transition("preparing_trial", f"double_tap:{trial_id}")
            await self.coordinator.prepare_trial(
                self._session_id, trial_id,
                {"kind": "prepare_double_tap",
                 "payload": {"targetX": int(target[0]), "targetY": int(target[1])},
                 "coordinateFrame": self.frame,
                 "calibrationFingerprint":
                     self._run["input_binding"]["calibration_fingerprint"]})
            ready = await self.coordinator.wait_ready(
                self._session_id, trial_id,
                self.defaults["self_test_budget"]["waiting_phone_timeout_ms"])
            if not ready.get("ready"):
                raise _RunStop(self._action_result(
                    "double_tap", state="unverified",
                    reason_code="trial_not_ready", trial_ids=trial_ids))
            await self.coordinator.mark_executing(self._session_id, trial_id)
            self._transition("executing", trial_id)
            self._physical_count += 1
            controller_result = await self._execute_physical(
                "double_tap", self.execute_double_tap,
                {key: value for key, value in primitive.items() if key != "kind"})
            self._transition("collecting", trial_id)
            evidence = await self.coordinator.wait_evidence(
                self._session_id, trial_id,
                int(self.defaults["command_timing"]["collect_grace_ms"]) + 3500)
            outcome = evaluate_double_tap_trial(
                controller_result=controller_result,
                trace=evidence.get("trace"), target_px=target,
                tolerance=tolerance)
            trial_state = {"trial_passed": "passed", "no_effect": "failed",
                           "contact_broken": "failed",
                           "mapping_mismatch": "failed",
                           "unknown": "unknown"}.get(outcome["classification"],
                                                      "unknown")
            await self.coordinator.finish_trial(self._session_id, trial_id,
                                                trial_state)
            if outcome["classification"] == "trial_passed":
                streak += 1
                continue
            if outcome["classification"] == "unknown":
                raise _RunUnknown(self._action_result(
                    "double_tap", state="unverified",
                    reason_code=outcome["reason_code"], trial_ids=trial_ids))
            raise _RunStop(self._action_result(
                "double_tap", state="unverified",
                reason_code=outcome["reason_code"], trial_ids=trial_ids))
        result = self._action_result("double_tap", state="valid",
                                   reason_code=None, trial_ids=trial_ids,
                                   controller="completed", contact="continuous",
                                   effect="verified")
        # 双击沿用原子双击能力，不能伪装成未经执行的连续滑动程序。
        result["validated_primitive"] = json.loads(json.dumps(primitive))
        result["primitive_hash"] = gprog.canonical_hash(primitive)
        return result

    # ---------- 系统 Back/Home 自检（第 10、22.13 节） ----------

    async def _prepare_edge_trial(self, action: str, trial_id: str, kind: str,
                                  payload: dict) -> dict:
        self._transition("preparing_trial", f"{action}:{trial_id}")
        try:
            await self.coordinator.prepare_trial(
                self._session_id, trial_id,
                {"kind": kind, "payload": payload,
                 "coordinateFrame": self.frame,
                 "calibrationFingerprint":
                     self._run["input_binding"]["calibration_fingerprint"]})
        except ScreenImeApiError as exc:
            self._transition("failed", exc.code)
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code=exc.code)) from exc
        ready = await self.coordinator.wait_ready(
            self._session_id, trial_id,
            self.defaults["self_test_budget"]["waiting_phone_timeout_ms"])
        if not ready.get("ready"):
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code="trial_not_ready"))
        return ready.get("readyPayload") or {}

    def _edge_candidate_list(self, kind: str, ready: dict) -> list[dict]:
        if gprog.is_internal_coordinate_frame(self.frame):
            raise gprog.GestureProgramError(
                "navigation_mode_unsupported",
                "系统边缘手势需要手机真实页面坐标与手势区元数据")
        # P1-3（2026-09-30 审查）：契约 Ready additionalProperties:false，
        # systemGestureInsets 只在 PageMetadata（ready.page）；顶层读取仅为
        # 旧测试夹具兼容，生产合规手机从 page 域取。
        insets = ((ready.get("page") or {}).get("systemGestureInsets")
                  or ready.get("systemGestureInsets"))
        return edge_swipe_candidates(
            kind=kind,
            system_gesture_insets=insets,
            display_width_px=self.frame["display_width_px"],
            display_height_px=self.frame["display_height_px"],
            frame=self.frame,
            uncertainty_px=calibration_uncertainty_px(self.bundle),
            defaults=self.defaults)

    async def _run_edge_swipe_trial(self, action: str, trial_id: str,
                                    plan: dict) -> dict:
        try:
            await self.coordinator.mark_executing(self._session_id, trial_id)
        except ScreenImeApiError as exc:
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code=exc.code)) from exc
        # 物理动作前持久写 executing；写失败则不动（第 8.1 节）
        self._transition("executing", trial_id)
        self._physical_count += 1
        before = await _maybe_await(self.capture_frame())
        controller_result = await self._execute_physical(action, self.execute_swipe, plan)
        self._transition("collecting", trial_id)
        evidence = await self.coordinator.wait_evidence(
            self._session_id, trial_id,
            int(self.defaults["command_timing"]["collect_grace_ms"]) + 3500)
        after = await _maybe_await(self.capture_frame())
        verify_result = None
        if self.verify_page is not None:
            page_kind = "home" if action == "system_home" else "back"
            verify_result = await _maybe_await(self.verify_page(page_kind))
        if action == "system_home":
            outcome = evaluate_home_trial(
                controller_result=controller_result,
                trace=evidence.get("trace"), verify_result=verify_result)
        else:
            outcome = evaluate_back_trial(
                controller_result=controller_result,
                trace=evidence.get("trace"),
                page_events=evidence.get("pageEvents"),
                verify_result=verify_result)
        trial_state = {"trial_passed": "passed", "no_effect": "failed",
                       "no_contact": "failed", "skipped": "failed",
                       "unknown": "unknown"}.get(outcome["classification"],
                                                  "unknown")
        await self.coordinator.finish_trial(self._session_id, trial_id, trial_state)
        outcome["before_frame"] = before
        outcome["after_frame"] = after
        return outcome

    def _edge_plan(self, action: str, trial_id: str,
                   candidate: dict) -> dict | None:
        """候选 → 编译计划；不可行时记跳过并返回 None（不消耗物理预算）。"""
        try:
            program, _meta = build_edge_program(candidate, self.bundle,
                                                self.frame)
            return plan_scroll_trial(program, self.bundle, self.frame)
        except (CandidateSkipped, gprog.GestureProgramError, ValueError) as exc:
            reason = getattr(exc, "reason_code", None) or str(exc)
            self._transition("evaluating", f"skip:{reason}")
            return None

    async def _run_system_back(self, action: str) -> dict:
        kind = "back_left" if action == "system_back_left" else "back_right"
        payload = {"parentId": "gesture-parent", "childId": "gesture-child"}
        trial_ids: list[str] = []
        candidates: list[dict] | None = None
        index = 0
        streak = 0
        last_reason: str | None = None
        while candidates is None or index < len(candidates):
            if not self._budget_left():
                return self._action_result(
                    action, state="unverified",
                    reason_code="gesture_budget_exhausted", trial_ids=trial_ids)
            trial_id = str(uuid.uuid4())
            trial_ids.append(trial_id)
            ready = await self._prepare_edge_trial(action, trial_id,
                                                   "prepare_back", payload)
            if candidates is None:
                try:
                    candidates = self._edge_candidate_list(kind, ready)
                except gprog.GestureProgramError as exc:
                    await self.coordinator.finish_trial(self._session_id,
                                                        trial_id, "failed")
                    return self._action_result(
                        action, state="unverified", reason_code=exc.reason_code,
                        trial_ids=trial_ids, controller="not_started")
                index = 0
                if not candidates:
                    await self.coordinator.finish_trial(self._session_id,
                                                        trial_id, "failed")
                    return self._action_result(
                        action, state="unverified",
                        reason_code="edge_gesture_unreachable",
                        trial_ids=trial_ids, controller="not_started")
            candidate = candidates[index]
            plan = self._edge_plan(action, trial_id, candidate)
            if plan is None:
                await self.coordinator.finish_trial(self._session_id, trial_id,
                                                    "failed")
                last_reason = "edge_candidate_unplannable"
                index += 1
                continue
            outcome = await self._run_edge_swipe_trial(action, trial_id, plan)
            classification = outcome["classification"]
            last_reason = outcome["reason_code"] or classification
            if classification == "trial_passed":
                streak += 1
                if streak >= self.consecutive_needed:
                    return self._action_result(
                        action, state="valid", reason_code=None,
                        trial_ids=trial_ids,
                        controller=outcome["controller_status"],
                        contact=outcome["contact_status"], effect="verified",
                        program=plan["program"])
                # verified：恢复父子初态后以同候选补足连续 3 次（第 22.13 节）
                continue
            streak = 0
            if classification == "unknown":
                raise _RunUnknown(self._action_result(
                    action, state="unverified", reason_code=outcome["reason_code"],
                    trial_ids=trial_ids,
                    controller=outcome["controller_status"]))
            if classification == "no_effect":
                # 恢复同初态，按第 10 节顺序换下一候选（§22.13）
                index += 1
                continue
            if classification == "skipped":
                # 页面事件与 verify_page 双通道都缺失：动作跳过，继续下一动作
                return self._action_result(
                    action, state="unverified", reason_code="skipped",
                    trial_ids=trial_ids,
                    controller=outcome["controller_status"])
            # no_contact：停止 run；不加深 Z、不盲调边缘偏移
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code=classification,
                trial_ids=trial_ids))
        # 候选用尽：动作 unverified，继续下一动作（第 22.10 节）
        return self._action_result(action, state="unverified",
                                   reason_code=last_reason
                                   or "effect_not_observed",
                                   trial_ids=trial_ids)

    async def _run_system_home(self) -> dict:
        action = "system_home"
        if self.verify_page is None:
            # 第 10.3 节：无 launcher 模板验证器时禁止自动试 Home（不新增
            # 权限绕过），显示 launcher_template_missing 且不产生物理动作。
            return self._action_result(
                action, state="unverified",
                reason_code="launcher_template_missing",
                controller="not_started")
        trial_ids: list[str] = []
        candidates: list[dict] | None = None
        index = 0
        streak = 0
        last_reason: str | None = None
        while candidates is None or index < len(candidates):
            if not self._budget_left():
                return self._action_result(
                    action, state="unverified",
                    reason_code="gesture_budget_exhausted", trial_ids=trial_ids)
            trial_id = str(uuid.uuid4())
            trial_ids.append(trial_id)
            ready = await self._prepare_edge_trial(action, trial_id,
                                                   "prepare_home", {})
            if candidates is None:
                try:
                    candidates = self._edge_candidate_list("home", ready)
                except gprog.GestureProgramError as exc:
                    await self.coordinator.finish_trial(self._session_id,
                                                        trial_id, "failed")
                    return self._action_result(
                        action, state="unverified", reason_code=exc.reason_code,
                        trial_ids=trial_ids, controller="not_started")
                index = 0
                if not candidates:
                    await self.coordinator.finish_trial(self._session_id,
                                                        trial_id, "failed")
                    return self._action_result(
                        action, state="unverified",
                        reason_code="edge_gesture_unreachable",
                        trial_ids=trial_ids, controller="not_started")
            candidate = candidates[index]
            plan = self._edge_plan(action, trial_id, candidate)
            if plan is None:
                await self.coordinator.finish_trial(self._session_id, trial_id,
                                                    "failed")
                last_reason = "edge_candidate_unplannable"
                index += 1
                continue
            outcome = await self._run_edge_swipe_trial(action, trial_id, plan)
            classification = outcome["classification"]
            last_reason = outcome["reason_code"] or classification
            if classification == "trial_passed":
                streak += 1
                if streak >= self.consecutive_needed:
                    return self._action_result(
                        action, state="valid", reason_code=None,
                        trial_ids=trial_ids,
                        controller=outcome["controller_status"],
                        contact=outcome["contact_status"], effect="verified",
                        program=plan["program"])
                # 第 10.3 节：首次成功后立即结束 Home 候选枚举（不再试其他
                # 候选、不在桌面继续滑动）。完整实现需经预先登记的本 App 桌面
                # 启动入口恢复测试页并完成新 ready（§22.10 恢复入口要求）；
                # 本版按同候选连续执行 3 次的简化，不盲点或后台强拉起。
                continue
            streak = 0
            if classification == "unknown":
                raise _RunUnknown(self._action_result(
                    action, state="unverified", reason_code=outcome["reason_code"],
                    trial_ids=trial_ids,
                    controller=outcome["controller_status"]))
            if classification == "no_effect":
                index += 1  # 换下一候选（第 10.3 节顺序）
                continue
            # no_contact：停止 run；不加深 Z、不盲调边缘偏移
            raise _RunStop(self._action_result(
                action, state="unverified", reason_code=classification,
                trial_ids=trial_ids))
        # 全部候选 no_effect：动作 unverified，继续下一动作（第 22.10 节）
        return self._action_result(action, state="unverified",
                                   reason_code=last_reason
                                   or "effect_not_observed",
                                   trial_ids=trial_ids)

    def _region(self) -> dict:
        region = getattr(self, "_region_override", None)
        if isinstance(region, dict) and region.get("heightPx"):
            return region
        return {"leftPx": 0, "topPx": 250,
                "widthPx": self.frame["root_width_px"],
                "heightPx": self.frame["root_height_px"] - 420}

    def _action_result(self, gesture_id: str, *, state: str,
                       reason_code: str | None, trial_ids: list | None = None,
                       controller: str = "unknown", contact: str = "not_observed",
                       effect: str | None = None,
                       program: dict | None = None) -> dict:
        result = {
            "gesture_id": gesture_id, "state": state,
            "validation_status": ("three_passes" if state == "valid"
                                  else "not_tested"),
            "trial_ids": list(trial_ids or []),
            "controller_status": controller,
            "contact_status": contact,
            "effect_status": effect or ("verified" if state == "valid"
                                        else "not_checked"),
            "reason_code": reason_code,
            "evidence_refs": [],
        }
        if state == "valid" and program is not None:
            result["program"] = json.loads(json.dumps(program))
            result["program_hash"] = gprog.program_hash(program)
        return result


class _RunUnknown(Exception):
    def __init__(self, result: dict):
        self.result = result
        super().__init__(result.get("reason_code"))


class _RunStop(Exception):
    def __init__(self, result: dict):
        self.result = result
        super().__init__(result.get("reason_code"))


ScreenImeApiError = ScreenImeApiError  # 导出供测试/调用方引用
