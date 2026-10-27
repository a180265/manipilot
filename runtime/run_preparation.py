"""Operator-started preparation for new work; never replay or move old work."""
from __future__ import annotations

import asyncio

from drivers import calibration_bundle as cb
from runtime.errors import ApiError
from runtime.operator_recovery import pending_operator_recovery
from runtime.permissions import check_operation
from runtime.util import iso, utc_now

REASON = "操作者已切回目标程序主页并确认探头停稳；放弃旧任务，准备新运行"
PREPARING = "正在清理旧任务并核对新运行条件"
TERMINAL = {"confirmed", "failed", "unknown", "cancelled", "expired"}


def _idle(runtime, device_id):
    if runtime.leases.get(device_id):
        raise ApiError("device_busy", "仍有任务租约，请等待当前动作结束后恢复")
    tasks = [t for t in runtime.store.all_tasks() if t["device_id"] == device_id]
    for task in tasks:
        running = runtime._exec_tasks.get(task["task_id"])
        if (running is not None and not running.done()) or task["status"] not in TERMINAL | {"queued"}:
            raise ApiError("device_busy", "仍有在途任务，不能在执行中清理或恢复")
        if task["status"] == "queued" and (
                any(s.get("is_action") for s in task.get("step_log", []))
                or (task.get("action_receipt") or {}).get("state") in {"pending", "unknown"}):
            raise ApiError("device_busy", "队列中仍有待对账动作，不能当作未执行任务丢弃")
    return tasks


def _available(runtime, device_id):
    device = runtime.devices.get(device_id)
    if runtime.estop.active or device.get("estop_latched"):
        raise ApiError("estop_active")
    if runtime.pre_enable_maintenance:
        raise ApiError("pre_enable_maintenance")
    if device.get("status") in {"offline", "starting", "quarantined", "maintenance"}:
        raise ApiError("state_transition_invalid", "设备离线、隔离或维护中，请先恢复连接或解除对应状态")
    if device.get("binding_enabled") is False or device.get("presence_state") == "removed":
        raise ApiError("state_transition_invalid", "设备未启用或已移除")
    if device.get("calibration_status") != "validated":
        raise ApiError("calibration_unavailable", device.get("calibration_status_reason") or "请先完成当前设备校准")
    if (device.get("communication_recovery_required") or device.get("operator_recovery_pending")
            or pending_operator_recovery(runtime.store, device_id)):
        raise ApiError("home_not_confirmed", "当前通信或人工停车操作仍待核实，请先处理该故障")
    return device


def _fingerprint(runtime, device_id):
    bundle = cb.load_bundle(device_id, runtime._gesture_data_root(device_id))
    return cb.motion_calibration_fingerprint(bundle) if bundle else None


async def prepare_run(runtime, device_id, body, context, *, before_resume=None):
    """Explicit click acknowledges old uncertainty, cancels queued work, then resumes.

    Optional application hook closes its own old sessions before generic readiness.
    This endpoint never clears estop/communication faults, starts supervision or moves.
    """
    check_operation("device_resume", context)
    if context.get("role") in {"readonly", "wx"}:
        raise ApiError("permission_denied", "一键恢复需要本机操作者确认")
    if body.get("confirmed") is not True:
        raise ApiError("confirmation_required", "请确认已切回目标程序主页、探头停稳，并放弃旧任务")
    key = f"prepare-run:{device_id}:{context['client_request_id']}"
    locks = getattr(runtime, "_run_preparation_locks", None)
    if locks is None:
        locks = runtime._run_preparation_locks = {}
    lock = locks.setdefault(device_id, asyncio.Lock())
    if lock.locked():
        raise ApiError("device_busy", "一键恢复正在处理，请等待结果")
    async with lock:
        previous = runtime.store.get_op_result(key)
        if previous is not None:
            # A delayed retry must not undo a later pause or report historical ready.
            return {**previous["result"], "status": runtime.devices.get(device_id)["status"],
                    "eligibility": runtime.device_eligibility(device_id, require_input=False)}
        device = _available(runtime, device_id)
        _idle(runtime, device_id)
        fingerprint = _fingerprint(runtime, device_id)
        session = device.get("daemon_session")
        runtime.devices.pause(device_id, PREPARING)
        supervision = getattr(runtime, "supervision", None)
        if supervision is not None:
            supervision.stop(device_id, context["reason"])
        try:
            arm = runtime.devices.driver(device_id)
            async def check_arm():
                state = await arm.read_state()
                if (not state.get("connected") or state.get("communication_recovery_required")
                        or any(state.get(k) for k in ("estop_latched", "estop_locked", "daemon_estop_locked"))):
                    raise ApiError("home_not_confirmed", "机械臂连接、急停或通信状态尚未就绪")
                if state.get("motion_lease") or state.get("controller_state") not in {None, "Idle"}:
                    raise ApiError("device_busy", "机械臂仍在运动或被占用，请等探头停稳")
                if session and state.get("session") != session:
                    raise ApiError("home_not_confirmed", "机械臂连接会话已变化，请重新校准")
                device["last_heartbeat_at"] = iso(utc_now())
            await check_arm()
            camera = runtime.devices.camera(device_id)
            quality = await camera.check_quality()
            if quality.get("quality") != "clear":
                raise ApiError("camera_unavailable", quality.get("note") or "请恢复清晰画面后再点一键恢复")
            ui = await camera.identify_ui_state()
            device.update(camera_quality=quality["quality"], camera_quality_display=quality["quality_display"],
                          current_ui_state=ui["ui_state"], current_ui_state_display=ui["ui_state_display"],
                          ui_confidence=ui.get("confidence", 0))
            runtime.store.save_device(device)
            _available(runtime, device_id)
            _idle(runtime, device_id)
            if _fingerprint(runtime, device_id) != fingerprint:
                raise ApiError("calibration_unavailable", "恢复期间校准已变化，请重新核对")
            discarded = []
            async def discard_queued():
                for task in _idle(runtime, device_id):
                    if task["status"] == "queued":
                        await runtime.cancel_task(task["task_id"], context)
                        discarded.append(task["task_id"])
            await discard_queued()
            if before_resume is not None:
                await before_resume(runtime, device_id, context)
            await check_arm()
            current = _available(runtime, device_id)
            if current is not device or current.get("status") != "paused" or current.get("pause_reason") != PREPARING:
                raise ApiError("state_transition_invalid", "恢复期间设备状态已变化，请刷新后重新核对")
            if current.get("camera_quality") != "clear":
                raise ApiError("camera_unavailable", "画面已变化，请恢复清晰画面后重试")
            if _fingerprint(runtime, device_id) != fingerprint or device.get("daemon_session") != session:
                raise ApiError("calibration_unavailable", "恢复期间校准或连接发生变化，请重新核对")
            await discard_queued()
            reviewed = []
            for task in _idle(runtime, device_id):
                if task["status"] == "unknown" and not task.get("reviewed_at"):
                    runtime.mark_task_reviewed(task["task_id"], context)
                    reviewed.append(task["task_id"])
            # Explicit operator click supplies the same acknowledgement as reset-home;
            # it is not inferred from a camera frame or a successful calibration.
            runtime.devices.mark_home_ok(device_id, context["operator_id"], context["reason"])
            ready = runtime.devices.resume(device_id, "已一键恢复，等待新任务；旧任务不再执行")
            result = {"device_id": device_id, "status": ready["status"],
                      "discarded_task_count": len(discarded), "reviewed_task_count": len(reviewed),
                      "message": "已就绪，旧任务已结束处理；未启动任何动作或托管",
                      "eligibility": runtime.device_eligibility(device_id, require_input=False)}
            runtime.audit.record("device_prepare_run", device_id=device_id,
                                 operator_id=context["operator_id"], reason=context["reason"],
                                 client_request_id=context["client_request_id"],
                                 detail={"discarded_task_ids": discarded, "reviewed_task_ids": reviewed})
            runtime.store.save_op_result(key, {"result": result, "completed_at": iso(utc_now())})
            return result
        except Exception as exc:
            # Retain the pause on partial failure; a fresh click can finish idempotently.
            if (runtime.devices.get(device_id) is device and device.get("status") == "paused"
                    and device.get("pause_reason") == PREPARING):
                device["status_reason"] = "一键恢复未完成：" + str(getattr(exc, "detail", None) or exc)
                runtime.store.save_device(device)
            raise
