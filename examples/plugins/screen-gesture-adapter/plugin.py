"""单次屏幕手势：只走任务能力接口，机械完成不等于手机效果。"""
from __future__ import annotations

import math
import re

from drivers.arm import DriverError, gesture_motion_confirmed
from tasks.plugin_api import PluginViolationError, TaskPlugin, make_step


VERSION = "screen-gesture.v1"
SCROLL_VERSION = "calibrated-scroll.v1"
SCROLL_BINDING_FIELDS = {
    "device_id", "daemon_session", "camera_id", "camera_device_unique_id",
    "camera_binding_id", "source_geometry_id", "binding_revision",
    "calibration_fingerprint", "frame_width", "frame_height",
}
ACTIONS = {"double_tap": "屏幕双击", "swipe": "屏幕两点滑动"}
SCROLL_ACTIONS = {"swipe_up": "上滑", "swipe_down": "下滑"}
BINDING_FIELDS = {
    "device_id", "daemon_session", "camera_id", "camera_device_unique_id",
    "binding_revision", "calibration_fingerprint", "frame_width", "frame_height",
}
PRE_MOTION_REJECTIONS = {
    "driver_policy_rejected", "driver_uncalibrated", "driver_gesture_invalid",
    "driver_limit_exceeded", "driver_estop_latched",
    "driver_camera_waiting",
}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _validate_scroll(task, device):
    """calibrated-scroll.v1 入参校验（客户端只选方向 + 完整 v3 绑定）。"""
    if not isinstance(task, dict) or task.get("task_type") != "calibrated_scroll":
        return "任务类型必须为 calibrated_scroll"
    if task.get("contract_version") != SCROLL_VERSION:
        return f"契约必须为 {SCROLL_VERSION}"
    if any(not _text(task.get(key)) for key in ("task_id", "idempotency_key", "device_id")):
        return "缺少稳定任务编号、幂等键或设备编号"
    payload = task.get("payload")
    if not isinstance(payload, dict):
        return "payload 必须为对象"
    if set(payload) != {"action", "binding"}:
        return "payload 只允许 action 与 binding（客户端只选方向，禁止机械参数/证书/模板字段）"
    if payload["action"] not in SCROLL_ACTIONS:
        return "action 只允许 swipe_up 或 swipe_down"
    binding = payload["binding"]
    if not isinstance(binding, dict) or set(binding) != SCROLL_BINDING_FIELDS:
        return "binding 必须为能力查询返回的完整 v3 绑定（十字段）"
    for key in ("device_id", "daemon_session", "camera_id", "camera_device_unique_id",
                "camera_binding_id", "source_geometry_id"):
        if not _text(binding[key]):
            return f"绑定字段 {key} 必须为非空文本"
    for key, minimum in (("binding_revision", 0), ("frame_width", 1), ("frame_height", 1)):
        if type(binding[key]) is not int or binding[key] < minimum:
            return f"绑定字段 {key} 必须为有效整数"
    if not re.fullmatch(r"[a-f0-9]{64}", str(binding["calibration_fingerprint"])):
        return "完整校准摘要必须为小写 SHA-256"
    if binding["device_id"] != task["device_id"] or device.get("device_id") != task["device_id"]:
        return "任务与设备绑定不一致"
    for key in ("camera_id", "daemon_session"):
        if key in device and device[key] != binding[key]:
            return f"当前设备的 {key} 已变化，禁止沿用旧绑定"
    return None


def _validate(task, device):
    if isinstance(task, dict) and task.get("task_type") == "calibrated_scroll":
        return _validate_scroll(task, device)
    if not isinstance(task, dict) or task.get("task_type") != "screen_gesture":
        return "任务类型必须为 screen_gesture"
    if task.get("contract_version") != VERSION:
        return "屏幕手势契约必须为 screen-gesture.v1"
    if any(not _text(task.get(key)) for key in ("task_id", "idempotency_key", "device_id")):
        return "缺少稳定任务编号、幂等键或设备编号"
    payload = task.get("payload")
    if not isinstance(payload, dict) or payload.get("action") not in ("double_tap", "swipe"):
        return "屏幕手势只允许双击或滑动"
    action = payload["action"]
    points = ("point",) if action == "double_tap" else ("start", "end")
    timing = "interval_ms" if action == "double_tap" else "duration_ms"
    required = {"action", "binding", *points}
    if not required <= payload.keys() or payload.keys() - required - {timing}:
        return "手势参数缺失或包含未允许字段，禁止机械参数覆盖"
    binding = payload["binding"]
    if not isinstance(binding, dict) or binding.keys() != BINDING_FIELDS:
        return "设备绑定必须完整且不得包含附加字段"
    for key in ("device_id", "daemon_session", "camera_id", "camera_device_unique_id"):
        if not _text(binding[key]):
            return f"设备绑定字段 {key} 必须为非空文本"
    for key, minimum in (("binding_revision", 0), ("frame_width", 1), ("frame_height", 1)):
        if type(binding[key]) is not int or binding[key] < minimum:
            return f"设备绑定字段 {key} 必须为有效整数"
    fingerprint = binding["calibration_fingerprint"]
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        return "完整校准摘要必须为小写 SHA-256"
    if binding["device_id"] != task["device_id"] or device.get("device_id") != task["device_id"]:
        return "任务与设备绑定不一致"
    for key in ("camera_id", "daemon_session"):
        if key in device and device[key] != binding[key]:
            return f"当前设备的 {key} 已变化，禁止沿用旧绑定"
    for key in points:
        point = payload[key]
        if (not isinstance(point, dict) or point.keys() != {"x", "y"}
                or any(not _number(point[axis], 0, 1) for axis in ("x", "y"))):
            return f"{key} 必须只含正向物理屏幕的归一化 x、y（0 到 1）"
    bounds = (50, 500) if action == "double_tap" else (150, 5000)
    if timing in payload and not _number(payload[timing], *bounds):
        return f"{timing} 必须为 {bounds[0]} 到 {bounds[1]} 的有限数值"
    if action == "swipe" and payload["start"] == payload["end"]:
        return "滑动起点和终点不能相同"
    return None


def _uncertain(result):
    if not isinstance(result, dict) or result.get("ok") is not True:
        return True
    if any(key in result and result[key] is not False
           for key in ("unknown", "stopped", "cancelled", "aborted")):
        return True
    return any(key in result and result[key] not in ("success", "confirmed", "completed")
               for key in ("outcome_hint", "outcome", "status"))


def _evidence(record, task):
    if (not isinstance(record, dict)
            or record.get("device_id") != task["device_id"]
            or record.get("task_id") != task["task_id"]
            or not _text(record.get("evidence_id"))
            or not _text(record.get("captured_at"))
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])):
        raise ValueError("缺少本任务设备的完整截图证据")
    return {key: record[key] for key in ("evidence_id", "sha256", "captured_at")}


def _detail(action):
    return {
        "contract_version": VERSION, "action": action, "action_display": ACTIONS[action],
        "effect_status": "unverified", "effect_status_display": "手机效果未核验",
        "mechanical_status": "not_started", "mechanical_status_display": "机械动作未开始",
        "motion_verified": None, "before_evidence": None, "after_evidence": None,
        "manual_review_required": False,
    }


class Plugin(TaskPlugin):
    async def validate(self, task, device_context):
        error = _validate(task, device_context)
        if task.get("task_type") == "calibrated_scroll":
            return ({"ok": False, "error_category": "invalid_request", "message": error}
                    if error else {"ok": True, "message": "校准直驱滑动参数校验通过"})
        return ({"ok": False, "error_category": "invalid_request", "message": error}
                if error else {"ok": True, "message": "屏幕手势参数校验通过"})

    async def plan(self, task, device_context):
        error = _validate(task, device_context)
        if error:
            raise ValueError(error)
        if task["task_type"] == "calibrated_scroll":
            # §5.4：只执行一次上滑/下滑；机械与效果两种结论由 execute 给出。
            action = task["payload"]["action"]
            return [make_step(
                1, "swipe", SCROLL_ACTIONS[action], is_action=True,
                required_capabilities=["camera.capture", "arm.swipe", "camera.measure_scroll"],
                timeout_seconds=90, evidence_required=False,
                success_criteria={"mechanical_confirmed": True,
                                  "before_after_evidence": True,
                                  "retryable": False},
                cleanup_action="不追加滑动；机械安全收尾由平台现有规则处理",
            )]
        action = task["payload"]["action"]
        return [make_step(
            1, action, ACTIONS[action], is_action=True,
            required_capabilities=["camera.capture", f"arm.{action}"],
            timeout_seconds=60, evidence_required=False,
            success_criteria={
                "motion_verified": True,
                **({"tap_count": 2} if action == "double_tap" else {"swipe_executed": True}),
                "before_after_evidence": True, "effect_status": "unverified",
            },
            cleanup_action="不追加屏幕动作；平台按现有规则安全收尾",
        )]

    async def execute_step(self, step, ctx):
        task = ctx.task
        error = _validate(task, ctx.device_context)
        if error:
            return {"outcome": "failed", "error_category": "invalid_request", "message": error}
        if task["task_type"] == "calibrated_scroll":
            # 校准直驱上下滑：计划/执行/光流判定集中在 runtime.calibrated_scroll，
            # 插件只承载任务类型与能力调用（§5.2 同一套滑动实现）。
            from runtime.calibrated_scroll import execute_scroll
            return await execute_scroll(ctx)
        action = task["payload"]["action"]
        detail = _detail(action)
        if step.get("action") != action or step.get("step_no") != 1:
            return {"outcome": "failed", "error_category": "invalid_request",
                    "message": "未登记的屏幕手势步骤", "detail": detail}
        try:
            detail["before_evidence"] = _evidence(await ctx.capture_evidence("屏幕手势前取证"), task)
        except Exception:
            return {"outcome": "failed", "error_category": "camera_unavailable",
                    "message": "动作前截图证据不可用，未执行手势", "detail": detail}

        payload = task["payload"]
        params = {"binding": dict(payload["binding"])}
        for key in (("point",) if action == "double_tap" else ("start", "end")):
            params[key] = {"source": "screen_normalized", **payload[key]}
        timing, driver_timing = (("interval_ms", "intervalMs") if action == "double_tap"
                                 else ("duration_ms", "durationMs"))
        if timing in payload:
            params[driver_timing] = payload[timing]

        # Drivers report post-dispatch failures as driver_disconnected, never policy rejections.
        detail.update(mechanical_status="unknown", mechanical_status_display="机械动作结果未知",
                      manual_review_required=True)
        try:
            result = await ctx.call_capability(f"arm.{action}", params)
        except PluginViolationError as exc:
            detail.update(mechanical_status="not_started", mechanical_status_display="机械动作未开始",
                          manual_review_required=False)
            return {"outcome": "failed", "error_category": "capability_not_allowed",
                    "message": f"能力调用前拒绝，未执行手势：{exc}", "detail": detail}
        except DriverError as exc:
            if exc.category in PRE_MOTION_REJECTIONS:
                detail.update(mechanical_status="not_started", mechanical_status_display="机械动作未开始",
                              manual_review_required=False)
                return {"outcome": "failed", "error_category": exc.category,
                        "message": f"动作前安全校验拒绝，未执行手势：{exc.message}", "detail": detail}
            result = None
        except Exception:
            result = None
        if isinstance(result, dict):
            verified = result.get("motion_verified")
            detail["motion_verified"] = verified if type(verified) is bool else None
            if action == "double_tap":
                count = result.get("tap_count")
                detail["tap_count"] = count if type(count) is int and count >= 0 else None
            else:
                executed = result.get("swipe_executed")
                detail["swipe_executed"] = executed if type(executed) is bool else None
        completed = gesture_motion_confirmed(action, result)
        if completed:
            detail.update(mechanical_status="confirmed", mechanical_status_display="机械动作已完成")
        try:
            detail["after_evidence"] = _evidence(await ctx.capture_evidence("屏幕手势后取证"), task)
            if detail["after_evidence"]["evidence_id"] == detail["before_evidence"]["evidence_id"]:
                detail["after_evidence"] = None
                raise ValueError("动作前后证据不可复用")
        except Exception:
            return {"outcome": "unknown", "error_category": "result_unknown_pending",
                    "message": "动作后截图证据不可用，禁止重试；手机效果未核验", "detail": detail}
        if not completed:
            return {"outcome": "unknown", "error_category": "result_unknown_pending",
                    "message": "手势停止、不完整或机械结果未核验，禁止重试；手机效果未核验",
                    "detail": detail}
        detail["manual_review_required"] = False
        return {"outcome": "success", "message": "机械动作已完成，手机效果未核验", "detail": detail}

    async def cancel(self, ctx):
        try:
            result = await ctx.call_capability("arm.stop")
            stopped = (isinstance(result, dict) and result.get("stopped") is True
                       and not _uncertain({key: value for key, value in result.items()
                                           if key != "stopped"
                                           and not (key == "outcome_hint" and value is None)}))
        except Exception:
            stopped = False
        return {"stopped": stopped, "outcome_hint": "success" if stopped else "unknown",
                "message": "已确认停止" if stopped else "停止结果未知，需人工确认"}

    async def recover(self, failure, ctx):
        if failure.get("error_category") in PRE_MOTION_REJECTIONS | {
                "capability_not_allowed", "invalid_request", "camera_unavailable"}:
            return {"recovered": False, "attempted": False,
                    "message": "动作前已拒绝，不追加停止或手势；安全收尾由平台处理"}
        stop = await self.cancel(ctx)
        return {"recovered": False, "attempted": True, "stopped": stop["stopped"],
                "message": "仅请求停止，不重试手势；安全收尾由平台现有规则处理"}

    async def cleanup(self, ctx):
        return {"cleaned": True, "message": "无输入或外部会话状态需要清理，不追加屏幕动作"}
