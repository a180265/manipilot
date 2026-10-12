"""One manual double tap; mechanical completion never implies unlock or wake."""
from copy import deepcopy

from runtime.screen_wake import (
    CAPABILITY, CONTRACT_VERSION, is_screen_wake_task, request_validator,
)
from tasks.plugin_api import TaskPlugin, make_step


def _evidence(record, task):
    if (not isinstance(record, dict) or record.get("task_id") != task["task_id"]
            or record.get("device_id") != task["device_id"]):
        raise ValueError("截图不属于本任务设备")
    reference = {key: record.get(key) for key in ("evidence_id", "sha256", "captured_at")}
    request_validator("evidenceRef").validate(reference)
    return reference


def _awake_proven(detail):
    frames = detail["frame_observations"]
    before, after = detail["before_evidence"], detail["after_evidence"]
    return bool(
        before and after and before["evidence_id"] != after["evidence_id"]
        and before["sha256"] != after["sha256"]
        and len(frames) >= 2 and frames[0]["quality"] == "waiting"
        and frames[-1]["quality"] == "clear"
        and frames[-1]["source_frame_seq"] > frames[0]["source_frame_seq"]
        and frames[-1]["sha256"] != frames[0]["sha256"])


class Plugin(TaskPlugin):
    async def validate(self, task, device_context):
        return {"ok": is_screen_wake_task(task), "error_category": "invalid_request",
                "message": "人工唤醒任务身份及参数校验"}

    async def plan(self, task, device_context):
        if not is_screen_wake_task(task):
            raise ValueError("人工唤醒任务未通过校验")
        return [make_step(
            1, "wake_screen", "校准验证点双击唤醒", is_action=True,
            required_capabilities=[CAPABILITY], timeout_seconds=60,
            evidence_required=False,
            success_criteria={"controller_completed": True, "tap_count": 2},
            cleanup_action="不追加归位、解锁或恢复动作；释放原租约",
        )]

    async def execute_step(self, step, ctx):
        detail = {"contract_version": CONTRACT_VERSION, "controller_completed": False,
                  "binding": deepcopy(ctx.task["payload"]["binding"]),
                  "tap_count": 0, "effect_status": "unverified", "motion_not_started": False,
                  "before_evidence": None, "after_evidence": None, "frame_observations": []}
        if not is_screen_wake_task(ctx.task) or step.get("action") != "wake_screen":
            return {"outcome": "failed", "error_category": "invalid_request",
                    "message": "唤醒请求无效，未执行", "detail": {**detail, "motion_not_started": True}}
        try:
            detail["before_evidence"] = _evidence(
                await ctx.capture_evidence("人工唤醒前画面"), ctx.task)
        except Exception:
            return {"outcome": "failed", "error_category": "camera_unavailable",
                    "message": "动作前取证失败，未执行唤醒",
                    "detail": {**detail, "motion_not_started": True}}
        try:
            result = await ctx.call_capability(CAPABILITY, ctx.task["payload"])
        except Exception as exc:
            if getattr(exc, "motion_not_started", False):
                return {"outcome": "failed",
                        "error_category": getattr(exc, "category", getattr(exc, "key", "device_not_eligible")),
                        "message": f"动作前拒绝，未执行唤醒：{exc}",
                        "detail": {**detail, "motion_not_started": True}}
            result = None
        if isinstance(result, dict):
            detail["controller_completed"] = result.get("controller_completed") is True
            if type(result.get("tap_count")) is int and 0 <= result["tap_count"] <= 2:
                detail["tap_count"] = result["tap_count"]
            frames = result.get("frame_observations")
            if request_validator("frameObservations").is_valid(frames):
                detail["frame_observations"] = deepcopy(frames)
            if result.get("motion_not_started") is True and not detail["controller_completed"] \
                    and detail["tap_count"] == 0:
                detail["motion_not_started"] = True
                return {"outcome": "failed", "error_category": "device_not_eligible",
                        "message": "动作前拒绝，未执行唤醒", "detail": detail}
        result = result if isinstance(result, dict) else {}
        try:
            detail["after_evidence"] = _evidence(
                await ctx.capture_evidence("人工唤醒后画面"), ctx.task)
        except Exception:
            # Mechanical completion is retained even if a screenshot cannot be
            # archived. Missing evidence must never promote the phone effect.
            pass
        if result.get("effect_status") == "awake" and _awake_proven(detail):
            detail["effect_status"] = "awake"
        completed = (detail["controller_completed"] and detail["tap_count"] == 2
                     and not any(result.get(key) for key in
                                 ("unknown", "stopped", "cancelled", "aborted"))
                     and result.get("ok") is not False
                     and result.get("outcome_hint") in (None, "success"))
        return {
            "outcome": "success" if completed else "unknown",
            "message": ("双击已完成；已观察到亮屏，不代表已解锁"
                        if completed and detail["effect_status"] == "awake" else
                        "双击已完成，亮屏效果未核验" if completed else
                        "双击执行情况不明，禁止重试"),
            "detail": detail,
        }

    async def cancel(self, ctx):
        result = await ctx.call_capability("arm.stop")
        stopped = (isinstance(result, dict) and result.get("ok") is True
                   and result.get("stopped") is True
                   and result.get("outcome_hint") in (None, "success"))
        return {"stopped": stopped, "outcome_hint": "success" if stopped else "unknown"}

    async def recover(self, failure, ctx):
        return {"recovered": False, "attempted": False, "message": "人工唤醒禁止自动恢复或重试"}

    async def cleanup(self, ctx):
        return {"cleaned": True, "message": "唤醒不追加屏幕或机械归位动作"}
