"""微信列表自动导航纯业务模块（规范第 9、22.14 节）。

只做四件事，不含 OCR 引擎、相机与机械驱动，也不连网：

1. 布局识别（§9.1）：输入是调用方生产的"已带框 OCR 结果"（校准平面像素），
   按 wechat-list-layout.v1 正向规则识别微信主界面并推导列表 ROI；
2. 搜索步骤规划（§9.3）：search_next / search_previous（及 return_top 的
   下滑方向）生成 gesture-program.v1 两点程序；§8.5 只允许一次 ×1.2 长度调整；
3. 回顶边界探测状态机（§22.14）：moving -> stationary_once -> suspected_top
   -> stopped，每轮最多两次静止探测，探测额度耗尽不清零；
4. 轮次持久化（§22.14）：data/devices/{device_id}/navigation_runs/
   {navigation_id}.json，首步必须 1、后续严格 +1、同 task/hash 幂等重送、
   8 步 / 8 物理动作 / 90 秒 / 2 次探测硬预算，close 语义按调用方原因收尾。

软件默认值唯一来源 configs/gesture_defaults.json（§22.1），加载后冻结传入；
运行时修改配置不影响已创建的轮次快照。位移测量复用
drivers/scroll_observation.py（本模块不重复实现光流）。
"""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from drivers.daemon_protocol import (
    SWIPE_DURATION_MAX_MS,
    SWIPE_DURATION_MIN_MS,
)
from drivers.gesture_program import GestureProgramError

DEFAULTS_PATH = (Path(__file__).resolve().parents[2] / "configs"
                 / "gesture_defaults.json")
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"

RUNS_DIRNAME = "navigation_runs"
NAVIGATION_RUN_FIELDS = (
    "navigation_id", "device_id", "profile_ref", "binding", "action", "target",
    "session_ref", "started_at", "deadline_at", "last_step_index",
    "active_task_id", "steps", "physical_count", "boundary_probe_count",
    "state",
)
NAVIGATION_ACTIONS = ("search_next", "search_previous", "return_top",
                      "back_to_list", "home")
SEARCH_ACTIONS = ("search_next", "search_previous")
CLOSE_REASONS = ("caller_finished", "caller_cancelled")

# §9.3：上滑 y 为 ROI 的 0.75 -> 0.30，下滑反向；基线长度 = 0.45 * ROI 高。
SWIPE_START_FRACTION = 0.75
SWIPE_END_FRACTION = 0.30
SWIPE_SPAN_CENTER_FRACTION = (SWIPE_START_FRACTION + SWIPE_END_FRACTION) / 2

_DEFAULTS: dict | None = None


# ---------- 默认值（§22.1：唯一来源 configs/gesture_defaults.json） ----------

def load_defaults() -> dict:
    """加载并冻结 gesture_defaults.json 全量配置（深拷贝）。"""
    global _DEFAULTS
    if _DEFAULTS is None:
        _DEFAULTS = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(_DEFAULTS)


def _section(defaults: dict | None, key: str) -> dict:
    """取 defaults[key]；调用方直接传节本身（键匹配）时原样返回。"""
    if not isinstance(defaults, dict):
        raise ValueError(f"defaults 缺少 {key} 配置")
    if key in defaults:
        section = defaults[key]
        if isinstance(section, dict):
            return section
        raise ValueError(f"defaults[{key}] 必须是对象")
    probe = {"wechat_list_layout": ("chinese_tabs", "title_region"),
             "business_scroll_adjustment": ("max_length_multiplier",),
             "navigation_limits": ("max_steps", "timeout_ms"),
             "launcher_template": ("min_anchor_correlation",),
             "top_template": ("min_regions", "max_regions")}[key]
    if any(field in defaults for field in probe):
        return defaults
    raise ValueError(f"defaults 缺少 {key} 配置")


# ---------- 1. 布局识别（§9.1） ----------

def _box_center(box: dict) -> tuple[float, float]:
    return (float(box["x"]) + float(box["w"]) / 2.0,
            float(box["y"]) + float(box["h"]) / 2.0)


def _in_region(box: dict, region: list[float], width_px: float,
               height_px: float) -> bool:
    rx, ry, rw, rh = region
    cx, cy = _box_center(box)
    return (rx * width_px <= cx <= (rx + rw) * width_px
            and ry * height_px <= cy <= (ry + rh) * height_px)


def _strip_title_suffix(text: str) -> str:
    """剥掉合法未读数后缀：\"微信(3)\" -> \"微信\"（§9.1 允许后缀）。"""
    for open_br, _close in (("(", ")"), ("（", "）")):
        index = text.find(open_br)
        if index > 0:
            text = text[:index]
    return text.strip()


def detect_chat_list_layout(ocr_boxes: list[dict], screen_width_px: float,
                            screen_height_px: float, *, defaults: dict) -> dict:
    """按 wechat-list-layout.v1 正向规则识别微信主界面并推导列表 ROI。

    - 顶部 [0,0,1,0.18] 内匹配标题“微信”/“WeChat”（允许括号未读后缀）；
    - 底部 [0,0.78,1,1] 内同语言四标签齐全、confidence ≥ min_tab_confidence、
      按 x 排序互不重叠；
    - 列表 top = 标题框 bottom + list_top_gap_ratio*screenH，
      bottom = 四标签最小 top - list_bottom_gap_ratio*screenH，
      left/right = list_left_ratio/list_right_ratio 屏宽，高 ≥ min_list_height_ratio*screenH。

    不满足（含键盘/弹窗/搜索页导致标题或标签缺失）一律
    ``{"matched": False, "reason_code": "navigation_layout_unsupported",
    "reason": 中文}``，不降阈值猜区域。
    """
    cfg = _section(defaults, "wechat_list_layout")
    width = float(screen_width_px)
    height = float(screen_height_px)
    boxes = [box for box in (ocr_boxes or [])
             if isinstance(box, dict) and box.get("text")]

    def _reject(reason: str) -> dict:
        return {"matched": False, "reason_code": "navigation_layout_unsupported",
                "reason": reason}

    title_region = list(cfg["title_region"])
    tab_region = list(cfg["tab_region"])
    languages = (("zh", cfg["chinese_tabs"]), ("en", cfg["english_tabs"]))
    for language, tabs in languages:
        title_text = tabs[0]
        title_candidates = [
            box for box in boxes
            if _in_region(box, title_region, width, height)
            and str(box["text"]).startswith(title_text)
            and _strip_title_suffix(str(box["text"])) == title_text]
        if not title_candidates:
            continue
        title = max(title_candidates, key=lambda b: float(b.get("confidence", 0.0)))
        found: list[dict] = []
        missing = []
        low_confidence = []
        for label in tabs:
            candidates = [
                box for box in boxes
                if _in_region(box, tab_region, width, height)
                and str(box["text"]) == label]
            if not candidates:
                missing.append(label)
                continue
            best = max(candidates,
                       key=lambda b: float(b.get("confidence", 0.0)))
            if float(best.get("confidence", 0.0)) < float(cfg["min_tab_confidence"]):
                low_confidence.append(label)
            found.append(best)
        if missing:
            return _reject(
                f"底部导航标签缺失或不属于同一语言：缺少 {'、'.join(missing)}，"
                "页面可能是键盘、弹窗或搜索页，不猜测列表区域")
        if low_confidence:
            return _reject(
                f"导航标签置信度不足：{'、'.join(low_confidence)} 低于 "
                f"{float(cfg['min_tab_confidence']):.2f}，不降阈值猜区域")
        ordered = sorted(found, key=lambda b: float(b["x"]))
        for left, right in zip(ordered, ordered[1:]):
            if float(left["x"]) + float(left["w"]) > float(right["x"]):
                return _reject(
                    f"导航标签按 x 排序后重叠（{left['text']} 与 {right['text']}），"
                    "布局不受支持")
        top = float(title["y"]) + float(title["h"]) + \
            float(cfg["list_top_gap_ratio"]) * height
        bottom = min(float(box["y"]) for box in ordered) - \
            float(cfg["list_bottom_gap_ratio"]) * height
        left_px = float(cfg["list_left_ratio"]) * width
        right_px = float(cfg["list_right_ratio"]) * width
        list_height = bottom - top
        if list_height < float(cfg["min_list_height_ratio"]) * height:
            return _reject(
                f"推导列表高度 {list_height:.0f}px 低于 "
                f"{float(cfg['min_list_height_ratio']) * height:.0f}px，布局不受支持")
        return {
            "matched": True,
            "layout_version": cfg.get("layout_version", "wechat-list-layout.v1"),
            "language": language,
            "title": title_text,
            "title_box": {"x": float(title["x"]), "y": float(title["y"]),
                          "w": float(title["w"]), "h": float(title["h"])},
            "roi": {"x": round(left_px, 2), "y": round(top, 2),
                    "width": round(right_px - left_px, 2),
                    "height": round(list_height, 2)},
            "tabs": [{"text": str(box["text"]),
                      "x": float(box["x"]), "y": float(box["y"]),
                      "w": float(box["w"]), "h": float(box["h"]),
                      "confidence": float(box.get("confidence", 0.0))}
                     for box in ordered],
        }
    return _reject(
        "顶部区域内未找到“微信/WeChat”标题（或标签语言与标题不一致），"
        "当前页面不是受支持的微信主界面")


# ---------- 2. 搜索步骤规划（§9.3 / §8.5） ----------

def _swipe_geometry(roi: dict, length: float) -> tuple[float, float, float]:
    """返回 (x, y_start, y_end)：长度绕基线中心缩放，方向由调用方决定。"""
    roi_x = float(roi["x"])
    roi_y = float(roi["y"])
    roi_w = float(roi["width"])
    roi_h = float(roi["height"])
    if roi_w <= 0 or roi_h <= 0:
        raise GestureProgramError("invalid_program", "ROI 尺寸必须为正")
    x = roi_x + roi_w / 2.0
    half = length / 2.0
    center_y = roi_y + SWIPE_SPAN_CENTER_FRACTION * roi_h
    return x, center_y + half, center_y - half  # (x, y_low, y_high)


def _duration_for(length: float, feed: float) -> int:
    """时长 = ceil(距离 / feed * 60000)，clamp 到滑动时长窗口，越界拒绝。"""
    if not feed > 0:
        raise GestureProgramError("path_timing_unachievable",
                                  "validated_feed 必须为正数")
    duration = int(math.ceil(length / feed * 60000.0))
    if duration > SWIPE_DURATION_MAX_MS:
        raise GestureProgramError(
            "path_timing_unachievable",
            f"请求时长 {duration}ms 超过滑动时长上限 "
            f"{SWIPE_DURATION_MAX_MS:g}ms")
    return max(duration, int(SWIPE_DURATION_MIN_MS))


def _two_point_program(x: float, y_start: float, y_end: float,
                       duration_ms: int) -> dict:
    return {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": [{
            "points": [
                {"x": round(float(x), 6), "y": round(float(y_start), 6),
                 "t_ms": 0},
                {"x": round(float(x), 6), "y": round(float(y_end), 6),
                 "t_ms": int(duration_ms)},
            ],
            "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0,
        }],
    }


def plan_search_step(action: str, roi: dict, *, validated_feed: float = 2250.0,
                     validated_program: dict | None = None,
                     length_multiplier: float = 1.0,
                     defaults: dict | None = None) -> dict:
    """生成一步列表搜索的 gesture-program.v1 两点程序（§9.3）。

    - roi 为归一化矩形（0-1，校准根平面）；x = ROI 中心；
      search_next 上滑 y 为 ROI 的 0.75 -> 0.30，search_previous / return_top
      下滑反向；
    - validated_feed 为与 roi 同一平面单位的已验证速度（每分钟），
      由调用方把机械 F 换算到该平面后传入；时长 = 距离/feed*60000 向上取整，
      clamp 到 [150, 5000]ms，越界抛 GestureProgramError
      ("path_timing_unachievable")；
    - length_multiplier 受 §8.5 约束：乘积不超过列表高的
      max_length_ratio_of_list（0.55）。

    返回 ``{"program": ..., "plan": {...}}``；plan 元数据含
    direction/x/y_start/y_end/length/length_multiplier/adjusted/feed/duration_ms。
    """
    if action in ("search_next",):
        direction = "up"
    elif action in ("search_previous", "return_top"):
        direction = "down"
    else:
        raise ValueError(f"非搜索动作：{action}")
    if not length_multiplier > 0:
        raise GestureProgramError("invalid_program",
                                  "length_multiplier 必须为正数")
    cfg = _section(defaults or load_defaults(), "business_scroll_adjustment")
    roi_h = float(roi["height"])
    base_length = (SWIPE_START_FRACTION - SWIPE_END_FRACTION) * roi_h
    length = min(base_length * float(length_multiplier),
                 float(cfg["max_length_ratio_of_list"]) * roi_h)
    x, y_low, y_high = _swipe_geometry(roi, length)
    if direction == "up":
        y_start, y_end = y_low, y_high
    else:
        y_start, y_end = y_high, y_low
    if validated_program is not None:
        from copy import deepcopy
        from drivers.gesture_program import validate_program
        validate_program(validated_program)
        strokes = validated_program["strokes"]
        if len(strokes) != 1 or len(strokes[0]["points"]) != 2:
            raise GestureProgramError("invalid_program", "列表导航仅适配已验证的单段垂直两点程序")
        start, end = strokes[0]["points"]
        dy = end["y"] - start["y"]
        if (abs(end["x"] - start["x"]) > 1e-6 or not dy
                or (dy < 0) != (direction == "up")):
            raise GestureProgramError("invalid_program", "已验证程序与列表滑动方向不匹配")
        # 同一校准平面、垂直路径：按已验收行程/时长缩放，保留接触停顿。
        validated_feed = abs(dy) / end["t_ms"] * 60000.0
        duration = _duration_for(abs(y_end - y_start), validated_feed)
        program = deepcopy(validated_program)
        program["strokes"][0]["points"] = [
            {**start, "x": round(x, 6), "y": round(y_start, 6), "t_ms": 0},
            {**end, "x": round(x, 6), "y": round(y_end, 6), "t_ms": duration},
        ]
        validate_program(program)
    else:
        duration = _duration_for(length, float(validated_feed))
        program = _two_point_program(x, y_start, y_end, duration)
    return {
        "program": program,
        "plan": {
            "action": action,
            "direction": direction,
            "x": round(x, 6),
            "y_start": round(y_start, 6),
            "y_end": round(y_end, 6),
            "length": round(length, 6),
            "length_multiplier": float(length_multiplier),
            "adjusted": float(length_multiplier) > 1.0,
            "feed": float(validated_feed),
            "duration_ms": duration,
            "max_length": round(float(cfg["max_length_ratio_of_list"]) * roi_h, 6),
        },
    }


def _program_points(plan_input: dict) -> tuple[list[dict], float | None]:
    """从 plan_search_step 结果或裸 program dict 中取两点与已验证 F。

    裸 program 没有速度元数据，返回 None 由调用方按 长度/时长 反推。"""
    if "strokes" in plan_input:
        program = plan_input
        feed = None
    else:
        program = plan_input.get("program") or {}
        feed = (plan_input.get("plan") or {}).get("feed")
    strokes = program.get("strokes") or []
    if not strokes:
        raise GestureProgramError("invalid_program", "无法从上一步程序读取路径")
    points = strokes[0].get("points") or []
    if len(points) < 2:
        raise GestureProgramError("invalid_program", "上一步程序不是两点路径")
    return points, (float(feed) if feed else None)


def adjust_search_length(previous_program: dict, roi: dict, *,
                         defaults: dict) -> dict | None:
    """§8.5：位移很小但非边界时，只允许一次把长度 ×1.2，上限列表高的 55%。

    previous_program 可以是 plan_search_step 的返回值（含 plan 元数据，
    按 length_multiplier / adjusted 判断是否已调整过），也可以是裸 program
    dict（视为未调整）。已调整过返回 None（只允许一次）；否则返回与
    plan_search_step 同构的结果，方向与 F 保持不变。
    """
    cfg = _section(defaults, "business_scroll_adjustment")
    if not isinstance(previous_program, dict):
        raise GestureProgramError("invalid_program", "上一步程序无效")
    previous_plan = previous_program.get("plan") if "strokes" not in previous_program else None
    if previous_plan is not None:
        if previous_plan.get("adjusted") or float(
                previous_plan.get("length_multiplier") or 1.0) >= float(
                cfg["max_length_multiplier"]):
            return None
    points, feed = _program_points(previous_program)
    roi_h = float(roi["height"])
    old_length = abs(float(points[-1]["y"]) - float(points[0]["y"]))
    if old_length <= 0:
        raise GestureProgramError("invalid_program", "上一步程序长度为 0")
    if feed is None:
        duration = int(points[-1]["t_ms"])
        if duration <= 0:
            raise GestureProgramError("invalid_program",
                                      "上一步程序时长为 0，无法推导速度")
        feed = old_length / duration * 60000.0
    new_length = min(old_length * float(cfg["max_length_multiplier"]),
                     float(cfg["max_length_ratio_of_list"]) * roi_h)
    x = float(points[0]["x"])
    center_y = (float(points[0]["y"]) + float(points[-1]["y"])) / 2.0
    down = float(points[-1]["y"]) > float(points[0]["y"])
    y_start, y_end = (center_y - new_length / 2.0,
                      center_y + new_length / 2.0) if down else \
        (center_y + new_length / 2.0, center_y - new_length / 2.0)
    duration = _duration_for(new_length, feed)
    multiplier = round(float(cfg["max_length_multiplier"]), 6)
    return {
        "program": _two_point_program(x, y_start, y_end, duration),
        "plan": {
            "action": (previous_plan or {}).get("action") or "adjusted",
            "direction": "down" if down else "up",
            "x": round(x, 6),
            "y_start": round(y_start, 6),
            "y_end": round(y_end, 6),
            "length": round(new_length, 6),
            "length_multiplier": multiplier,
            "adjusted": True,
            "feed": float(feed),
            "duration_ms": duration,
            "max_length": round(float(cfg["max_length_ratio_of_list"]) * roi_h, 6),
        },
    }


# ---------- 3. 回顶边界探测状态机（§22.14） ----------

PROBE_STATES = ("moving", "stationary_once", "suspected_top", "stopped")


def boundary_probe_transition(state: str, *, stationary: bool,
                              probe_count: int) -> dict:
    """回顶边界探测状态机：moving -> stationary_once -> suspected_top -> stopped。

    - stationary=True：moving（未消耗过探测）-> stationary_once（第 1 次探测）；
      stationary_once -> suspected_top（第 2 次探测，terminal）；
      moving 且 probe_count>=1（有效下滑后回到 moving）不得再次进入探测，
      保持 moving 且 probe_allowed=False；
    - stationary=False（有效下滑）：回到 moving；消耗过的探测数不清零
      （probe_count>=1 时 probe_allowed=False）；
    - suspected_top / stopped 为终态，任何输入不再迁移。

    返回 ``{"state", "probe_allowed", "terminal"}``。
    """
    if state not in PROBE_STATES:
        raise ValueError(f"未知探测状态：{state}")
    count = int(probe_count)
    limit = int(_section(load_defaults(), "navigation_limits")
                ["max_boundary_probes"])
    if state in ("suspected_top", "stopped"):
        return {"state": state, "probe_allowed": False, "terminal": True}
    if stationary:
        if state == "stationary_once":
            return {"state": "suspected_top", "probe_allowed": False,
                    "terminal": True}
        # state == "moving"
        if count <= 0 and count < limit:
            return {"state": "stationary_once", "probe_allowed": True,
                    "terminal": False}
        return {"state": "moving", "probe_allowed": False, "terminal": False}
    return {"state": "moving", "probe_allowed": count <= 0 and count < limit,
            "terminal": False}


def _probe_state_fields(probe_state: Any) -> tuple[str, bool]:
    if isinstance(probe_state, str):
        return probe_state, probe_state in ("moving", "stationary_once")
    if isinstance(probe_state, dict):
        state = str(probe_state.get("state"))
        allowed = bool(probe_state.get("probe_allowed"))
        if state == "stationary_once":
            allowed = True
        return state, allowed
    raise ValueError("probe_state 必须是状态字符串或状态机返回值")


def step_outcome_for_return_top(*, scroll_result: dict, probe_state: Any,
                                defaults: dict) -> dict:
    """return_top 单步结果组合（§22.14）。

    - stationary 且探测允许（刚进入 stationary_once）：
      boundary_probe_pending / business_boundary_probe / no_effect /
      top_verified=False / plugin_outcome=success——仅表示该探测步骤完成，
      不表示回顶成功（第 3.2 节明确例外）；
    - stationary 且探测额度用尽（suspected_top 或 moving 不允许探测）：
      top_not_confirmed / retry none / plugin unknown；
    - displaced：本次下滑起效 effect=verified，top_verified=False；
    - unknown：plugin unknown，reason_code 透传测量结果。
    """
    _section(defaults, "navigation_limits")
    if not isinstance(scroll_result, dict):
        raise ValueError("scroll_result 必须是 measure_displacement 的输出")
    status = scroll_result.get("status")
    state, probe_allowed = _probe_state_fields(probe_state)
    if status == "stationary":
        if probe_allowed and state == "stationary_once":
            return {
                "reason_code": "boundary_probe_pending",
                "message": "疑似边界：静止证据成立，尚需一次限定探测，不表示回顶成功",
                "retry_policy": "business_boundary_probe",
                "controller_status": "completed",
                "effect_status": "no_effect",
                "top_verified": False,
                "plugin_outcome": "success",
            }
        return {
            "reason_code": "top_not_confirmed",
            "message": "静止探测额度已用尽或不再允许，无法证明已到顶部",
            "retry_policy": "none",
            "controller_status": "completed",
            "effect_status": "no_effect",
            "top_verified": False,
            "plugin_outcome": "unknown",
        }
    if status == "displaced":
        return {
            "reason_code": None,
            "message": "本次下滑起效，列表内容位移已验证；到顶与否需后续证据",
            "retry_policy": "business_next_step",
            "controller_status": "completed",
            "effect_status": "verified",
            "top_verified": False,
            "plugin_outcome": "success",
        }
    if status == "unknown":
        return {
            "reason_code": scroll_result.get("reason_code") or "effect_unknown",
            "message": "位移测量未知：算法失败不判成没滚动，本步不认证回顶",
            "retry_policy": "none",
            "controller_status": "unknown",
            "effect_status": "unknown",
            "top_verified": False,
            "plugin_outcome": "unknown",
        }
    raise ValueError(f"未知位移状态：{status}")


# ---------- 4. 轮次管理（§22.14 全轮预算持久化） ----------

class NavigationRunError(Exception):
    """导航轮次层拒绝；reason_code 供运行时映射 409/failed。"""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        self.message = message
        super().__init__(f"{reason_code}: {message}")


class NavigationStepConflict(NavigationRunError):
    """步骤重复或乱序（§14.1 navigation_step_conflict）。"""

    def __init__(self, message: str):
        super().__init__("navigation_step_conflict", message)


def navigation_run_path(device_id: str, navigation_id: str,
                        data_root: Path | None = None) -> Path:
    root = Path(data_root) if data_root else DATA_ROOT
    return root / "devices" / str(device_id) / RUNS_DIRNAME / f"{navigation_id}.json"


def _validate_navigation_id(navigation_id: str) -> str:
    try:
        parsed = uuid.UUID(str(navigation_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise NavigationRunError(
            "navigation_run_invalid",
            "navigation_id 必须是标准小写带连字符 UUID（第 22.1 节）") from exc
    if str(navigation_id) != str(parsed):
        raise NavigationRunError(
            "navigation_run_invalid",
            "navigation_id 必须是标准小写带连字符 UUID（第 22.1 节）")
    return str(navigation_id)


class NavigationRunManager:
    """data/devices/{device_id}/navigation_runs/ 的唯一写入口（§22.14）。

    先落盘后动：步骤校验、写入与轮次终态判定在一次调用内完成；
    原子写复用 drivers/ui_targets.py 的临时文件 + fsync + replace 写法。
    90 秒 / 8 步 / 8 物理动作 / 2 次探测全部由 defaults["navigation_limits"]
    参数化（gesture_defaults.json 冻结快照）。
    """

    def __init__(self, device_id: str, data_root: Path | None = None,
                 defaults: dict | None = None):
        self.device_id = str(device_id)
        self.data_root = Path(data_root) if data_root else DATA_ROOT
        self.defaults = defaults or load_defaults()
        self.limits = dict(_section(self.defaults, "navigation_limits"))

    # ---------- 路径与读写 ----------

    def _path(self, navigation_id: str) -> Path:
        return navigation_run_path(self.device_id, navigation_id, self.data_root)

    def _write_run(self, run: dict) -> None:
        run["device_id"] = self.device_id
        path = self._path(run["navigation_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(run, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _load_run(self, navigation_id: str) -> dict:
        path = self._path(navigation_id)
        if not path.exists():
            raise NavigationRunError(
                "navigation_run_not_found",
                f"导航轮 {navigation_id} 不存在（设备 {self.device_id}）")
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NavigationRunError(
                "navigation_run_corrupted",
                f"导航轮 {navigation_id} 持久化文件损坏：{exc}") from exc
        if not isinstance(run, dict) or run.get("navigation_id") != navigation_id:
            raise NavigationRunError(
                "navigation_run_corrupted",
                f"导航轮 {navigation_id} 持久化文件结构无效")
        return run

    def _scan_runs(self) -> list[dict]:
        directory = self._path("_").parent
        if not directory.exists():
            return []
        runs = []
        for path in sorted(directory.glob("*.json")):
            try:
                runs.append(self._load_run(path.stem))
            except NavigationRunError:
                continue
        return runs

    # ---------- 轮次生命周期 ----------

    def start_run(self, *, navigation_id: str, profile_ref: dict,
                  binding: dict, action: str, target: dict | None = None,
                  session_ref: dict | None = None, now_ms: int) -> dict:
        navigation_id = _validate_navigation_id(navigation_id)
        if action not in NAVIGATION_ACTIONS:
            raise NavigationRunError(
                "navigation_mode_unsupported", f"未知导航动作：{action}")
        if action == "back_to_list" and not session_ref:
            raise NavigationRunError(
                "navigation_mode_unsupported",
                "back_to_list 必须携带 session_ref（第 22.6 节）")
        if action != "back_to_list" and session_ref is not None:
            raise NavigationRunError(
                "navigation_mode_unsupported",
                f"{action} 禁止携带 session_ref（第 22.6 节）")
        path = self._path(navigation_id)
        if path.exists():
            existing = self._load_run(navigation_id)
            same = (existing.get("profile_ref") == profile_ref
                    and existing.get("binding") == binding
                    and existing.get("action") == action
                    and existing.get("target") == target
                    and existing.get("session_ref") == session_ref)
            if not (same and existing.get("state") == "active"
                    and not existing.get("manual_review")):
                raise NavigationRunError(
                    "navigation_run_conflict",
                    f"导航轮 {navigation_id} 已存在且关键参数不同或已终态，"
                    "一次 navigation 固定 action/target/profile/binding，"
                    "变化必须先结束旧轮")
            return copy.deepcopy(existing)
        for other in self._scan_runs():
            if other.get("navigation_id") == navigation_id:
                continue
            if other.get("manual_review"):
                continue
            state = other.get("state")
            if state == "active":
                raise NavigationRunError(
                    "navigation_run_conflict",
                    f"设备 {self.device_id} 已有进行中的导航轮 "
                    f"{other.get('navigation_id')}，同一设备只允许一轮 active")
            if state == "unknown":
                raise NavigationRunError(
                    "navigation_run_conflict",
                    f"设备 {self.device_id} 存在未知状态导航轮 "
                    f"{other.get('navigation_id')}，按未知保护阻止新轮")
        run = {
            "navigation_id": navigation_id,
            "device_id": self.device_id,
            "profile_ref": copy.deepcopy(profile_ref),
            "binding": copy.deepcopy(binding),
            "action": action,
            "target": copy.deepcopy(target) if target else None,
            "session_ref": copy.deepcopy(session_ref) if session_ref else None,
            "started_at": int(now_ms),
            "deadline_at": int(now_ms) + int(self.limits["timeout_ms"]),
            "last_step_index": 0,
            "active_task_id": None,
            "steps": [],
            "physical_count": 0,
            "boundary_probe_count": 0,
            "state": "active",
            "state_reason_code": None,
            "closed_at": None,
        }
        self._write_run(run)
        return copy.deepcopy(run)

    def record_step(self, navigation_id: str, *, step_index: int,
                    request_hash: str, task_id: str, submitted_physical: bool,
                    result_ref: dict | None, now_ms: int) -> dict:
        run = self._load_run(navigation_id)
        if run["state"] != "active" or run.get("manual_review"):
            raise NavigationStepConflict(
                f"导航轮 {navigation_id} 状态为 {run['state']}，"
                "终态轮不能再记录步骤（不能重新从 1 开始）")
        step_index = int(step_index)
        steps: list[dict] = run["steps"]
        existing = next((s for s in steps if s["step_index"] == step_index), None)
        idempotent = False
        if existing is not None:
            if (existing.get("task_id") != task_id
                    or existing.get("request_hash") != request_hash):
                raise NavigationStepConflict(
                    f"步骤 {step_index} 已用不同 task_id 或请求 hash 记录，拒绝")
            if existing.get("result_ref") is not None:
                # 同 step 同 task+hash 重送：返回旧结果，不产生新动作。
                idempotent = True
            else:
                existing["result_ref"] = copy.deepcopy(result_ref)
                existing["recorded_at"] = int(now_ms)
        else:
            expected = int(run["last_step_index"]) + 1
            if step_index != expected:
                raise NavigationStepConflict(
                    f"步骤序号应为 {expected}（首步必须 1，后续严格 +1），"
                    f"收到 {step_index}")
            if steps and steps[-1].get("result_ref") is None:
                raise NavigationStepConflict(
                    f"前一步 {steps[-1]['step_index']} 尚未终态，"
                    "不能提交下一步")
            steps.append({
                "step_index": step_index,
                "request_hash": request_hash,
                "task_id": task_id,
                "result_ref": copy.deepcopy(result_ref),
                "submitted_physical": bool(submitted_physical),
                "recorded_at": int(now_ms),
            })
            run["last_step_index"] = step_index
            if submitted_physical:
                run["physical_count"] = int(run["physical_count"]) + 1
        if idempotent:
            return {
                "navigation_id": run["navigation_id"],
                "step_index": step_index,
                "task_id": task_id,
                "idempotent": True,
                "run_state": run["state"],
                "state_reason_code": run.get("state_reason_code"),
                "physical_count": run["physical_count"],
                "boundary_probe_count": run["boundary_probe_count"],
                "result_ref": copy.deepcopy(existing["result_ref"]),
            }
        if isinstance(result_ref, dict):
            if result_ref.get("reason_code") == "boundary_probe_pending":
                run["boundary_probe_count"] = int(run["boundary_probe_count"]) + 1
        run["active_task_id"] = next(
            (s["task_id"] for s in reversed(steps) if s.get("result_ref") is None),
            None)
        if result_ref is None:
            # 仅登记在途（先落盘后动）：不因登记本身决定轮次终态，
            # 也不把未确认运动写成 failed（§22.14）。
            self._write_run(run)
            return {
                "navigation_id": run["navigation_id"],
                "step_index": step_index,
                "task_id": task_id,
                "idempotent": False,
                "run_state": run["state"],
                "state_reason_code": run.get("state_reason_code"),
                "physical_count": run["physical_count"],
                "boundary_probe_count": run["boundary_probe_count"],
                "result_ref": None,
            }
        decision = self._terminal_decision(run, result_ref, now_ms)
        run["state"] = decision["state"]
        run["state_reason_code"] = decision.get("reason_code")
        if run["state"] != "active":
            run["closed_at"] = int(now_ms)
        self._write_run(run)
        return {
            "navigation_id": run["navigation_id"],
            "step_index": step_index,
            "task_id": task_id,
            "idempotent": False,
            "run_state": run["state"],
            "state_reason_code": run.get("state_reason_code"),
            "physical_count": run["physical_count"],
            "boundary_probe_count": run["boundary_probe_count"],
            "result_ref": copy.deepcopy(result_ref),
        }

    def _terminal_decision(self, run: dict, result_ref: dict | None,
                           now_ms: int) -> dict:
        """§22.14 固定优先级：unknown -> completed -> failed(预算) -> active。"""
        result = result_ref if isinstance(result_ref, dict) else {}
        if (result.get("plugin_outcome") == "unknown"
                or result.get("controller_status") == "unknown"
                or result.get("effect_status") == "unknown"
                or result.get("reason_code") == "direction_mismatch"):
            # 反向运动已发生：本步失败是确定的，但当前列表位置不可预测，
            # 后续自动动作须等原轮人工核对，不能继续在 active 轮里提交。
            return {"state": "unknown",
                    "reason_code": result.get("reason_code") or "effect_unknown"}
        if (result.get("target_found") or result.get("top_verified")
                or result.get("returned_to_target_page")):
            return {"state": "completed", "reason_code": None}
        budget = (len(run["steps"]) >= int(self.limits["max_steps"])
                  or int(run["physical_count"]) >= int(
                      self.limits["max_physical_actions"])
                  or int(now_ms) >= int(run["deadline_at"]))
        if budget:
            return {"state": "failed", "reason_code": "gesture_budget_exhausted"}
        return {"state": "active", "reason_code": None}

    def run_view(self, navigation_id: str) -> dict | None:
        try:
            return copy.deepcopy(self._load_run(navigation_id))
        except NavigationRunError as exc:
            if exc.reason_code == "navigation_run_not_found":
                return None
            raise

    def acknowledge_review(self, navigation_id: str, *, task_id: str, now_ms: int) -> dict:
        """运行时已核对任务终态后解除轮次占用，不更改任何动作证据或未知结论。"""
        run = self._load_run(navigation_id)
        steps = run.get("steps") or []
        if not task_id or (steps and steps[-1].get("task_id") != task_id):
            raise NavigationStepConflict("人工核对不是原导航轮最后一个任务，未解除占用")
        previous = run.get("manual_review")
        if previous:
            if previous.get("task_id") != task_id:
                raise NavigationStepConflict("导航轮已由其他任务核对，未覆盖原记录")
            return copy.deepcopy(run)
        run["manual_review"] = {"task_id": task_id, "reviewed_at": int(now_ms)}
        if run["state"] == "active":
            run["state"] = "failed"
            run["state_reason_code"] = "caller_cancelled"
        # unknown 保持 unknown；只解除后续新轮次的保护占用，绝不重开原轮。
        run["closed_at"] = run.get("closed_at") or int(now_ms)
        self._write_run(run)
        return copy.deepcopy(run)

    def close_run(self, navigation_id: str, reason: str, now_ms: int) -> dict:
        if reason not in CLOSE_REASONS:
            raise ValueError(f"close 原因必须是 {'/'.join(CLOSE_REASONS)}")
        run = self._load_run(navigation_id)
        if any(step.get("result_ref") is None for step in run["steps"]):
            raise NavigationStepConflict(
                f"导航轮 {navigation_id} 有在途步骤（active_task_id="
                f"{run.get('active_task_id')}），不能 close")
        if run["state"] == "active":
            if reason == "caller_finished":
                # 只表示调用结束，不补造 target_found/top_verified。
                run["state"] = "completed"
                run["state_reason_code"] = run.get("state_reason_code")
            else:
                run["state"] = "failed"
                run["state_reason_code"] = "caller_cancelled"
            run["closed_at"] = int(now_ms)
            self._write_run(run)
        # unknown 保持原未知；终态轮重复 close 返回原轮摘要。
        return {
            "navigation_id": run["navigation_id"],
            "device_id": run["device_id"],
            "state": run["state"],
            "reason_code": run.get("state_reason_code"),
            "last_step_index": run["last_step_index"],
            "physical_count": run["physical_count"],
        }


# ---------- 5. 顶部/桌面模板匹配（§9.4 / §22.8 / §22.15） ----------

def _anchor_rect(anchor: dict) -> tuple[int, int, int, int]:
    left = anchor.get("left_px", anchor.get("x"))
    top = anchor.get("top_px", anchor.get("y"))
    width = anchor.get("width_px", anchor.get("w"))
    height = anchor.get("height_px", anchor.get("h"))
    if None in (left, top, width, height):
        raise ValueError(
            "锚点区域必须含 left_px/top_px/width_px/height_px（或 x/y/w/h）")
    return int(left), int(top), int(width), int(height)


def _verify_anchor(anchor: dict, frame_gray: np.ndarray, params: dict) -> dict:
    left, top, width, height = _anchor_rect(anchor)
    template = anchor.get("template")
    detail = {"left_px": left, "top_px": top, "width_px": width,
              "height_px": height, "passed": False, "reason": None,
              "correlation": None, "dx_px": None, "dy_px": None}
    frame_h, frame_w = frame_gray.shape[:2]
    if not isinstance(template, np.ndarray) or template.ndim != 2:
        detail["reason"] = "template_missing"
        return detail
    tpl_h, tpl_w = int(template.shape[0]), int(template.shape[1])
    detail["width_px"], detail["height_px"] = tpl_w, tpl_h
    if tpl_w < int(params["min_anchor_size_px"]) or \
            tpl_h < int(params["min_anchor_size_px"]):
        detail["reason"] = "anchor_too_small"
        return detail
    if float(template.std()) < float(params["min_anchor_stddev"]):
        detail["reason"] = "anchor_low_contrast"
        return detail
    margin_x = int(float(params["search_window_ratio"]) * frame_w)
    margin_y = int(float(params["search_window_ratio"]) * frame_h)
    x0 = max(left - margin_x, 0)
    y0 = max(top - margin_y, 0)
    x1 = min(left + tpl_w + margin_x, frame_w)
    y1 = min(top + tpl_h + margin_y, frame_h)
    window = frame_gray[y0:y1, x0:x1]
    if window.shape[0] < tpl_h or window.shape[1] < tpl_w:
        detail["reason"] = "search_window_too_small"
        return detail
    result = cv2.matchTemplate(window, template.astype(np.uint8),
                               cv2.TM_CCOEFF_NORMED)
    # 零方差窗口可能产生 NaN：非有限值按 -1 处理，不参与峰值判定。
    result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
    best_val = float(result.max())
    best_i, best_j = np.unravel_index(int(np.argmax(result)), result.shape)
    detail["correlation"] = best_val
    if not math.isfinite(best_val) or \
            best_val < float(params["min_anchor_correlation"]):
        detail["reason"] = "correlation_below_threshold"
        return detail
    matched_left = x0 + int(best_j)
    matched_top = y0 + int(best_i)
    dx, dy = matched_left - left, matched_top - top
    detail["dx_px"], detail["dy_px"] = dx, dy
    if abs(dx) > float(params["position_tolerance_ratio"]) * frame_w or \
            abs(dy) > float(params["position_tolerance_ratio"]) * frame_h:
        detail["reason"] = "position_drift"
        return detail
    # 最佳值必须比次优非重叠候选高 best_vs_second_margin，防重复纹理假阳性。
    suppressed = result.copy()
    row0 = max(int(best_i) - tpl_h + 1, 0)
    row1 = min(int(best_i) + tpl_h, result.shape[0])
    col0 = max(int(best_j) - tpl_w + 1, 0)
    col1 = min(int(best_j) + tpl_w, result.shape[1])
    suppressed[row0:row1, col0:col1] = -1.0
    second_val = float(suppressed.max())
    detail["second_best"] = second_val if second_val > -1.0 else None
    if second_val > -1.0 and (best_val - second_val) < float(
            params["best_vs_second_margin"]):
        detail["reason"] = "ambiguous_second_candidate"
        return detail
    detail["passed"] = True
    return detail


def verify_top_template(anchor_regions: list[dict], frame_gray: np.ndarray,
                        *, defaults: dict, min_anchors: int | None = None) -> dict:
    """登记模板位置匹配（§22.8 阈值 / §22.15 anchor_regions 契约）。

    anchor_regions 每项为校准域像素矩形（left_px/top_px/width_px/height_px
    或 x/y/w/h）加登记时的模板灰度图（"template"，np.ndarray）。
    每锚点仅在登记位置周围 search_window_ratio*屏幕尺寸 窗口内做
    TM_CCOEFF_NORMED 匹配：相关值 ≥0.92、比次优非重叠候选高 ≥0.05、
    位置偏差 ≤0.02*屏幕对应尺寸；锚点尺寸 ≥24px、灰度标准差 ≥10。
    通过锚点数 ≥ min_anchors（默认 defaults["top_template"]["min_regions"]=1，
    launcher 桌面复用同一函数传 min_anchors=3）才算 matched。
    """
    params = dict(_section(defaults, "launcher_template"))
    top_cfg = _section(defaults, "top_template")
    required = int(min_anchors if min_anchors is not None
                   else top_cfg.get("min_regions", 1))
    if not isinstance(frame_gray, np.ndarray) or frame_gray.ndim != 2:
        raise ValueError("frame_gray 必须是单通道灰度图")
    anchors = list(anchor_regions or [])
    details = [_verify_anchor(anchor, frame_gray, params) for anchor in anchors]
    passed = sum(1 for detail in details if detail["passed"])
    result = {
        "matched": passed >= required and len(anchors) > 0,
        "matched_anchors": passed,
        "total_anchors": len(anchors),
        "min_anchors": required,
        "anchors": details,
    }
    if not result["matched"]:
        reasons = [detail["reason"] for detail in details if detail["reason"]]
        if passed < required and not reasons:
            reasons = ["insufficient_anchors"]
        result.update({
            "reason_code": "top_not_confirmed",
            "reason": "顶部/桌面模板匹配失败（" + "、".join(reasons) + "），"
                      "不能据此认证到顶或桌面",
        })
    return result
