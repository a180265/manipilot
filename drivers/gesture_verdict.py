"""三层结果归约（规范第 3 节 gesture_verdict.v1）。

把控制器回执、手机触点轨迹与视觉证据归约成唯一判定：
- controller_status ∈ not_started / completed / unknown
- contact_status   ∈ not_observed / continuous / broken / cancelled / unknown
- effect_status    ∈ verified / no_effect / unknown / not_checked

本模块是纯函数：不连硬件、不读全局设备、不改档案。
只有 controller completed 不满足任何自动业务成功条件（调用方负责）。
"""
from __future__ import annotations

from typing import Any

CONTROLLER_STATUSES = ("not_started", "completed", "unknown")
CONTACT_STATUSES = ("not_observed", "continuous", "broken", "cancelled", "unknown")
EFFECT_STATUSES = ("verified", "no_effect", "unknown", "not_checked")
RETRY_POLICIES = ("none", "test_fixture_next_candidate", "business_next_step",
                  "business_boundary_probe")

_VERDICT_KEYS = ("controller_status", "contact_status", "effect_status",
                 "reason_code", "message", "evidence_refs", "retry_policy")


def _check_enum(value: Any, allowed: tuple, label: str) -> str:
    if value not in allowed:
        raise ValueError(f"{label} 必须是 {allowed} 之一，收到 {value!r}")
    return value


def controller_status_from_result(result: Any) -> str:
    """从驱动回包归约控制器状态；不修改旧 gesture_motion_confirmed 语义。

    控制器层与效果层分离（三层判定）：controller_completed 是驱动在
    租约事务内逐段执行 + MPos 端点校验的结论，属于控制器事实；
    摄像头探针证据（motion_verified）属于效果层，观察模式下降级
    不得把已完成执行的控制器状态拖成 unknown。
    """
    if not isinstance(result, dict):
        return "unknown" if result else "not_started"
    if result.get("controller_status") in CONTROLLER_STATUSES:
        return result["controller_status"]
    if not result:
        return "not_started"
    if result.get("motion_verified") is True and result.get("ok") is True:
        return "completed"
    if result.get("controller_completed") is True and result.get("ok") is True:
        return "completed"
    if result.get("outcome_hint") == "success" and result.get("ok") is True:
        return "completed"
    if result.get("outcome_hint") == "unknown":
        return "unknown"
    return "unknown"


def contact_status_from_trace(trace: Any) -> str:
    """从手机 Trace 归约接触状态；业务页面无采样时为 not_observed。"""
    if not isinstance(trace, dict):
        return "not_observed"
    events = trace.get("events")
    if not isinstance(events, list):
        return "not_observed"
    if not events:
        return "not_observed"
    end_reason = trace.get("endReason")
    if end_reason == "cancel":
        return "cancelled"
    downs = [e for e in events if e.get("action") == "down"]
    ups = [e for e in events if e.get("action") == "up"]
    pointer_ids = {e.get("pointerId") for e in events}
    if trace.get("droppedSampleCount") or end_reason in {"overflow", "multi_touch", "page_lost"}:
        return "broken"
    if end_reason == "up" and len(downs) == 1 and len(ups) == 1 \
            and len(pointer_ids) == 1 and downs[0].get("pointerId") == ups[0].get("pointerId"):
        return "continuous"
    if end_reason == "timeout":
        # 双击超时只有一次接触：不算连续滑动，也不算断触
        return "unknown"
    return "broken"


def make_verdict(*, controller_status: str, contact_status: str, effect_status: str,
                 reason_code: str | None = None, message: str = "",
                 evidence_refs: list | None = None,
                 retry_policy: str = "none") -> dict:
    """构造唯一判定输出；字段与枚举固定，不能只返回 ok=true。"""
    verdict = {
        "controller_status": _check_enum(controller_status, CONTROLLER_STATUSES, "controller_status"),
        "contact_status": _check_enum(contact_status, CONTACT_STATUSES, "contact_status"),
        "effect_status": _check_enum(effect_status, EFFECT_STATUSES, "effect_status"),
        "reason_code": reason_code,
        "message": str(message or ""),
        "evidence_refs": list(evidence_refs or []),
        "retry_policy": _check_enum(retry_policy, RETRY_POLICIES, "retry_policy"),
    }
    return verdict


def effect_from_scroll_displacement(displacement: Any, policy: dict) -> str:
    """视觉位移证据 → effect（规范第 9.2 节阈值由调用方冻结在 policy）。

    只认测量模块的三态结论：status=displaced 且满足 policy 位移阈值
    （与 expected_sign 方向，若提供）才 verified；stationary → no_effect；
    unknown、缺 status、缺 min_displacement_px 一律 unknown——
    算法不背书时既不判成功也不判没滚动（fail-closed）。
    """
    minimum = policy.get("min_displacement_px")
    if not isinstance(displacement, dict):
        return "unknown"
    status = displacement.get("status")
    if status == "stationary":
        return "no_effect"
    if status != "displaced" or minimum is None:
        return "unknown"
    dy = displacement.get("dy_px")
    if not isinstance(dy, (int, float)):
        return "unknown"
    if abs(dy) < float(minimum):
        return "no_effect"
    expected_sign = policy.get("expected_sign")
    if type(expected_sign) is int and expected_sign in (1, -1) \
            and dy * expected_sign < 0:
        return "no_effect"
    return "verified"


def evaluate_gesture_program(plan: dict, controller: Any, phone_trace: Any,
                             visual: Any, policy: dict | None) -> dict:
    """通用手势程序的三层判定。

    policy.effect ∈ scroll_displacement / page_transition / fixture_trace / manual_only：
    - manual_only 或缺失：effect=not_checked（自动结果 unknown，由任务层映射）；
    - scroll_displacement：独立列表位移证据；
    - page_transition：目标页面证据；
    - fixture_trace：测试页轨迹证据（自检使用）。
    """
    policy = policy or {}
    controller_status = controller_status_from_result(controller)
    contact_status = contact_status_from_trace(phone_trace)
    effect_kind = policy.get("effect") or "manual_only"
    reason_code = None
    if effect_kind in {"manual_only", None}:
        effect_status = "not_checked"
        reason_code = "manual_review_required"
    elif effect_kind == "scroll_displacement":
        effect_status = effect_from_scroll_displacement(visual, policy)
    elif effect_kind == "page_transition":
        effect_status = ("verified" if isinstance(visual, dict)
                         and visual.get("confirmed") is True else "unknown")
        if effect_status == "unknown":
            reason_code = "effect_unknown"
    elif effect_kind == "fixture_trace":
        effect_status = "verified" if contact_status == "continuous" else "no_effect"
    else:
        raise ValueError(f"未知效果验证器类型：{effect_kind}")
    if controller_status != "completed" and effect_status in {"verified", "no_effect"}:
        # 控制结果未知时 verified 与 no_effect 都不可信：前者是错误放行，
        # 后者会走 failed 重试路径重放真机动作——统一降级 unknown
        effect_status = "unknown"
        reason_code = "effect_unknown"
    return make_verdict(controller_status=controller_status,
                        contact_status=contact_status,
                        effect_status=effect_status,
                        reason_code=reason_code,
                        message=policy.get("message", ""),
                        evidence_refs=policy.get("evidence_refs"),
                        retry_policy=policy.get("retry_policy", "none"))
