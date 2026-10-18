"""在现有设备租约内执行输入法任务；手机路由只读取 Arm Fleet 绑定。"""
from __future__ import annotations
import asyncio

from adapters.screen_ime.service import get_default_service
from runtime.util import sha256_text


def _input_disallowed(ctx) -> bool:
    """A-开关（2026-10-03 规格 §4.6）：每次输入副作用前查操作者持久开关。

    开关损坏/缺失一律按禁止处理；E 延后标记已退役，不再参与判定。
    取消/回执类动作不经过本检查（关闭不得阻断安全收尾）。
    """
    from runtime.input_settings import is_enabled
    try:
        data_root = ctx._runtime.devices.settings_data_root(ctx.device_id)
    except Exception:
        data_root = None
    return not is_enabled(ctx.device_id, data_root)


def _disallowed_result(*, staged: bool = False) -> dict:
    return {
        "outcome": "unknown" if staged else "failed",
        "error_category": "input_switch_disabled",
        "message": ("输入与发送未允许（input_switch_disabled），已请求取消候选；请核对手机，不自动重发"
                    if staged else "输入与发送未允许（input_switch_disabled），未发布输入候选"),
    }


def _motion_completed(receipt) -> bool:
    """Preserve explicit uncertainty; frame difference may be observation only."""
    if not isinstance(receipt, dict) or any(receipt.get(key) is False
            for key in ("ok", "controller_completed", "arrived")):
        return False
    if any(receipt.get(key) in {"unknown", "failed", "cancelled", "stopped"}
           for key in ("outcome", "outcome_hint", "status")):
        return False
    if any(receipt.get(key) is True for key in ("unknown", "failed", "cancelled", "stopped")):
        return False
    observed_completion = (receipt.get("controller_completed") is True
                           and receipt.get("motion_observation_only") is True
                           and receipt.get("outcome_hint") == "success")
    if receipt.get("motion_verified") is False and not observed_completion:
        return False
    return receipt.get("ok") is True or observed_completion


async def insert_bound_text(ctx) -> dict:
    if _input_disallowed(ctx):
        return _disallowed_result()
    text = ctx.task["payload"]["text"]
    ime_id = ctx.device_context.get("ime_device_id")
    if not ime_id:
        return {"outcome": "failed", "error_category": "device_offline",
                "message": "机械臂尚无有效的输入法绑定，请在设备调试台核对"}
    service = get_default_service()
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        device = next((row for row in service.tasks.list_devices()
                       if row["deviceId"] == ime_id), None)
        if device and device["online"] and device["keyboardActive"]:
            break
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.2)
        if _input_disallowed(ctx):
            return _disallowed_result()
    if _input_disallowed(ctx):
        return _disallowed_result()
    if not device or not device["online"] or not device["keyboardActive"]:
        return {"outcome": "failed", "error_category": "device_offline",
                "message": "机械臂绑定的手机输入法未就绪，请打开该手机的输入框"}
    task_id = f"{ctx.task['task_id']}-ime"
    ctx.state_set("active_ime_task_id", task_id)
    task, _ = await service.tasks.create({
        "taskId": task_id, "deviceId": ime_id,
        "targetUser": ctx.task.get("external_conversation_id") or ctx.task["task_id"],
        "content": text, "typingSpeed": "random", "source": "api",
    })
    if task["content"] != text:
        await service.tasks.cancel(task_id)
        return {"outcome": "failed", "error_category": "text_hash_mismatch",
                "message": "输入法返回的正文与人工原文不一致，已取消且未点击"}
    # Input contexts can carry an operation body absent from the parent task.
    # Bind the verified body to this active IME task without altering its payload.
    ctx.state_set("active_ime_text_hash", sha256_text(text))

    def _closed_probe() -> bool:
        # FIX-02：关闭唤醒等待——等待期间开关关闭立即中止长等（§4.8.6-7）
        return _input_disallowed(ctx)

    try:
        if _input_disallowed(ctx):
            await service.tasks.cancel(task_id)
            return _disallowed_result(staged=True)
        displayed = await service.tasks.wait_for_status(
            task_id, {"displayed", "inserted", "typing_failed", "cancelled"}, 20,
            probe=_closed_probe)
        if displayed["status"] == "cancelled" and _input_disallowed(ctx):
            return _disallowed_result(staged=True)
        if _input_disallowed(ctx):
            await service.tasks.cancel(task_id)
            return _disallowed_result(staged=True)
        if displayed["status"] != "displayed":
            await service.tasks.cancel(task_id)
            return {"outcome": "unknown", "message": "输入法候选状态未确认，已请求取消；请核对手机，不自动重发"}
        clicked = await ctx.call_capability("arm.click", {"target_id": "ime_candidate_bar"})
        if not _motion_completed(clicked):
            await service.tasks.cancel(task_id)
            return {"outcome": "unknown", "error_category": "ime_candidate_click_unknown",
                    "message": "候选栏点击结果未知，已请求取消；停止输入与发送，不自动重试",
                    "detail": {"ime_task_id": task_id}}
        result = await service.tasks.wait_for_status(
            task_id, {"inserted", "typing_failed", "cancelled"}, 60,
            probe=_closed_probe)
        if result["status"] == "cancelled" and _input_disallowed(ctx):
            return _disallowed_result(staged=True)
    except BaseException:
        await service.tasks.cancel(task_id)
        raise
    if _input_disallowed(ctx):
        await service.tasks.cancel(task_id)
        return _disallowed_result(staged=True)
    if result["status"] != "inserted" or result.get("typedCharacterCount") != len(text):
        await service.tasks.cancel(task_id)
        return {"outcome": "unknown", "message": "输入法未确认完整输入，可能残留部分文字；停止发送，等待人工核对",
                "detail": {"ime_task_id": task_id, "ime_status": result["status"]}}
    try:
        parked = await ctx.call_capability("arm.park", {})
    except Exception:
        parked = None
    if not _motion_completed(parked):
        await service.tasks.cancel(task_id)
        return {"outcome": "unknown", "error_category": "ime_input_park_unknown",
                "message": "输入法已确认输入，但机械臂停车未确认；停止发送，不自动重试",
                "detail": {"ime_task_id": task_id, "ime_status": result["status"]}}
    ctx.state_set("active_ime_task_id", None)
    ctx.state_set("active_ime_text_hash", None)
    return {"outcome": "success", "message": "机械臂侧输入法已确认完整输入原文",
            "detail": {"ime_task_id": task_id, "text_hash": sha256_text(text)}}


async def cancel_bound_input(ctx) -> None:
    task_id = ctx.state_get("active_ime_task_id")
    if task_id:
        await get_default_service().tasks.cancel(str(task_id))
