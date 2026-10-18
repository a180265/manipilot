"""任务执行器：统一任务生命周期。

已提交 -> 参数校验 -> 能力/权限校验 -> 等待设备 -> 获取租约 -> 执行步骤
      -> 采集证据 -> 计算结果 -> 清理现场 -> 回到安全状态 -> 释放租约
      -> 已发送/已完成 / 明确失败 / 结果未知

不变量：
- 动作开始后取消必须先停止、清理和取证。
- 断连、超时、证据不足、运行时异常一律结果未知，禁止猜成明确失败或已完成。
- 结果未知时自动暂停设备进入人工路径，禁止自动重试。
- 每个步骤前后落库（步骤游标），崩溃后按记录恢复，不能重复执行动作。
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

from drivers.arm import DriverError
from tasks.plugin_api import PluginViolationError, StepContext
from runtime.device_eligibility import INPUT_TASK_TYPES
from runtime.errors import ApiError
from runtime.status import TASK_STATUS, display
from runtime.screen_wake import is_screen_wake_task, record_unstarted_result
from runtime.util import iso, parse_iso, utc_now

TERMINAL_STATUSES = {"confirmed", "failed", "unknown", "cancelled", "expired"}
CANCEL_HOOK_TIMEOUT_SECONDS = 10.0


class TaskExecutor:
    def __init__(self, runtime):
        self._runtime = runtime

        # S1（第六轮）：协程早期异常且任务暂不可读时的待对账队列
        self._pending_early_finalize = {}

    # ---------- 工具 ----------
    def _save(self, task: dict) -> None:
        self._runtime._save_task_progress(task)
        self._runtime.events.publish(
            "task.updated",
            {"task_id": task["task_id"], "status": task["status"], "status_display": task["status_display"]},
        )

    def _timeline(self, task: dict, note: str, detail: dict | None = None) -> None:
        task.setdefault("timeline", []).append(
            {"at": iso(utc_now()), "status": task["status"], "status_display": task["status_display"], "note": note, "detail": detail or {}}
        )

    def _cancel_requested(self, task: dict) -> bool:
        """取消标记由 cancel_task 写入存储，执行器每次从存储读取最新值。"""
        latest = self._runtime.store.get_task(task["task_id"])
        return bool(latest and latest.get("cancel_requested"))

    def _owns_execution_lease(self, task: dict, *, allow_expired: bool = False) -> bool:
        lease = self._runtime.leases.get(task["device_id"])
        if (not lease or lease.get("task_id") != task["task_id"]
                or lease.get("lease_id") != task.get("lease_id")):
            return False
        expires = parse_iso(lease.get("expires_at"))
        return allow_expired or bool(expires and expires > utc_now())

    def _input_switch_on(self, device_id: str) -> bool:
        """A-开关（2026-10-03 规格 §4）：操作者持久选择；损坏/缺失=禁止。"""
        try:
            data_root = self._runtime.devices.settings_data_root(device_id)
        except Exception:
            data_root = None
        from runtime.input_settings import is_enabled
        return is_enabled(device_id, data_root)

    def _auto_wake_enabled(self) -> bool:
        return bool(self._runtime.config.get("auto_wake_enabled", True))

    def _submit_auto_wake(self, task: dict) -> dict:
        """A6（2026-10-01 拍板）：暗屏代提一次 screen_wake（已校准双击），
        原任务退回队列自动重试一次；总开关 auto_wake_enabled 控制。"""
        import uuid as _uuid
        from datetime import timedelta
        from drivers import calibration_bundle as cb
        from drivers import screen_gestures as sg
        device_id = task["device_id"]
        data_root = getattr(self._runtime.devices, "_gesture_data_root", None)
        bundle = cb.load_bundle(device_id, data_root(device_id) if data_root else None)
        if bundle is None:
            raise DriverError("driver_uncalibrated", "暗屏代提唤醒失败：无校准档案")
        binding = sg.binding_snapshot(bundle)
        request = {
            "task_id": f"wake-{_uuid.uuid4().hex[:12]}",
            "task_type": "screen_wake", "capability": "arm.wake_screen",
            "contract_version": "screen-wake.v1", "device_id": device_id,
            "idempotency_key": f"wake-{task['task_id']}",
            "correlation_id": task["task_id"],
            "expires_at": iso(utc_now() + timedelta(seconds=90)),
            "payload": {"binding": binding, "confirmed": True},
        }
        return self._runtime.submit_task(
            request, source="console",
            context={"operator_id": "系统",
                     "reason": f"暗屏自动唤醒（任务 {task['task_id']} 重试前置）"})

    async def _finish_unstarted_camera_wait(self, task: dict, entry: dict) -> dict | None:
        if is_screen_wake_task(task):
            return None
        runtime = self._runtime
        device_id = task["device_id"]
        device = runtime.devices.get(device_id)
        # Only untouched tasks qualify; a per-call rejection cannot prove that
        # earlier steps did not move. Do not run recovery/cleanup in this case.
        if (device.get("camera_quality") != "waiting" or device["status"] != "busy"
                or task.get("plan") or task.get("step_log")
                or task.get("action_call_count", 0) or task.get("capability_call_count", 0)
                or task.get("stop_result_unknown") or task.get("cancellation_handled")
                or self._cancel_requested(task) or runtime.estop.active
                or not self._owns_execution_lease(task)):
            return None
        requires_input = entry.get("requires_input", False) or task["task_type"] in INPUT_TASK_TYPES
        if requires_input and not self._input_switch_on(device_id):
            # A-开关关：零动作输入任务直接失败并回报（不退回队列挂起）
            task.update(status="failed", error_category="input_switch_disabled",
                        result_message="输入与发送未允许（开关关闭），任务未执行",
                        finished_at=iso(utc_now()))
            self._timeline(task, task["result_message"])
            self._save(task)
            runtime.leases.release(device_id, task.get("lease_id"))
            return task
        if requires_input:
            pass  # 开关开=继续正常执行
        eligibility = runtime.devices.eligibility(
            device_id, require_input=requires_input and device.get("enforce_input_gate", False),
            ime_device_id=(task.get("payload") or {}).get("ime_device_id"),
            require_keyboard_active=entry.get("requires_keyboard_active", True),
            require_ime_ready=entry.get("focuses_input_before_typing") is not True,
        )
        if set(eligibility["blocking_checks"]) - {"device_busy"} != {"camera_waiting"}:
            return None

        # A6（2026-10-01 拍板）；P1-1（第五轮复审）：必须先退队/释放租约/
        # 置 ready 再提交 wake——原任务持租约且在途时提交，唤醒前置检查
        # 结构性命中 lease_conflict+device_busy 必败。代提失败写 timeline
        # 留痕：原任务已 queued，将被再次派发并按暗屏失败处理（wake_retried
        # 已置，不循环）。
        if (task.get("source") == "wx" and self._auto_wake_enabled()
                and not task.get("wake_retried")):
            message = "画面等待中：先退队并代提一次暗屏唤醒，本任务将自动重试一次"
            task["wake_retried"] = True
            task.update(status="queued", status_display=display(TASK_STATUS, "queued"),
                        result_message=message, wake_retried=True)
            self._timeline(task, message)
            self._save(task)
            runtime.leases.release(device_id, task["lease_id"])
            runtime.devices._set_status(device_id, "ready",
                                        "暗屏：已退队并代提唤醒，任务排队重试一次")
            try:
                self._submit_auto_wake(task)
                self._timeline(task, "暗屏唤醒任务已提交（重试前置）")
            except Exception as exc:
                self._timeline(task, f"暗屏唤醒代提失败（原任务将按暗屏失败处理）：{exc}")
            self._save(task)
            return task

        message = "等待画面恢复；本任务尚未开始，未执行动作、恢复或归位，不自动重放"
        task.update(
            status="failed", status_display=display(TASK_STATUS, "failed"),
            error_category="camera_waiting", result_message=message, failure_reason=message,
            finished_at=iso(utc_now()), motion_not_started=True, home_verified=False,
            home_check={"home_ok": False, "checked_at": None,
                        "skipped": "零动作任务无需归位；未取得新的画面或主页证明"},
            cleanup_result={"cleaned": True, "skipped": "尚未执行，无需清理"},
        )
        self._timeline(task, message)
        self._save(task)
        runtime.leases.release(device_id, task["lease_id"])
        runtime.devices._set_status(device_id, "ready", "等待画面恢复，尚未开始任务")
        if task.get("source") == "wx":
            await runtime.reporter.send_task_result(task)
        return task

    # ---------- 主流程 ----------
    async def execute_guarded(self, task_id: str) -> dict:
        """P1/第六轮 S1：执行协程的守卫收尾调用者。

        execute 本体异常（含取任务/插件等 try 前早期异常、finalize 自身失败）
        在此终结：无插件恢复/清理/归位；诚实终态+租约归属核对+设备阻断+
        审计+WX 回报，且回报/放行受持久事实约束。本方法自身不得再抛。
        """
        try:
            return await self.execute(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self.finalize_early_error(task_id, exc)

    async def finalize_early_error(self, task_id: str, exc: Exception,
                                    _retry: bool = False) -> dict:
        """协程早期异常/守卫兜底的正式收尾（第六轮 S1 重审）。

        - 存储不可读 ≠ 任务不存在：不可读时挂入待对账队列，存储恢复后由
          调度循环重试本方法，不得让任务永久悬挂 awaiting_device/busy。
        - 终态保存失败不得回报 WX/置 ready（回报必须与持久事实一致）。
        - 已有终态时幂等补齐缺失的无动作义务（释放/审计/回报），绝不重跑插件。
        - 动作事实=step_log 动作 + 停止/回执未知 + action_receipt pending/unknown
          + 已尝试恢复——空 step_log 不等于"未执行"。
        - 设备状态只在租约归属本任务时变更（A 迟到收尾不碰 B）。
        """
        runtime = self._runtime
        task = None
        unreadable = False
        try:
            task = runtime.store.get_task(task_id)
        except Exception:
            unreadable = True
        if unreadable:
            if not _retry:
                self._pending_early_finalize[task_id] = str(exc)
            try:
                runtime.audit.record(
                    "executor_early_error", operator_id="runtime",
                    task_id=task_id,
                    reason=f"任务记录暂不可读：{exc}；已挂起待存储恢复后收尾")
            except Exception:
                pass
            return {}
        if task is None:
            try:
                runtime.audit.record(
                    "executor_early_error", operator_id="runtime",
                    task_id=task_id, reason=f"任务记录不存在：{exc}")
            except Exception:
                pass
            self._pending_early_finalize.pop(task_id, None)
            return {}

        def _audit(reason):
            try:
                runtime.audit.record("executor_early_error", operator_id="runtime",
                                     task_id=task_id,
                                     device_id=task.get("device_id"), reason=reason)
            except Exception:
                pass

        # ---- 已有终态：幂等补齐缺失的无动作义务，不重跑插件
        already_terminal = task.get("status") in TERMINAL_STATUSES
        if already_terminal and not _retry:
            self._pending_early_finalize.pop(task_id, None)
            completed = []
            if self._owns_execution_lease(task, allow_expired=True):
                if runtime.leases.release(task["device_id"], task.get("lease_id")):
                    completed.append("lease_released")
            if not task.get("executor_early_finalized"):
                task["executor_early_finalized"] = True
                try:
                    self._save(task)
                except Exception:
                    pass
                _audit(f"终态已存在（{task.get('status')}），幂等补齐：{completed or '无缺失义务'}")
            if (task.get("source") == "wx"
                    and not task.get("result_reported_at")
                    and task.get("status") in ("failed", "unknown", "cancelled")):
                try:
                    await runtime.reporter.send_task_result(task)
                    task["result_reported_at"] = iso(utc_now())
                    try:
                        self._save(task)
                    except Exception:
                        pass
                except Exception:
                    pass
            return task

        acted = any(entry.get("is_action") for entry in task.get("step_log") or [])
        receipt = task.get("action_receipt") or {}
        in_flight = bool(task.get("stop_result_unknown")
                         or task.get("motion_result_unknown")
                         or receipt.get("state") in ("pending", "unknown")
                         or task.get("recovery_attempted"))
        unknown = acted or in_flight
        # 租约归属核对：迟到收尾只处理自己的租约与设备状态
        owns = False
        try:
            owns = self._owns_execution_lease(task, allow_expired=True)
        except Exception:
            owns = False
        released = False
        if owns:
            released = runtime.leases.release(
                task["device_id"], task.get("lease_id"))
        task["status"] = "unknown" if unknown else "failed"
        task["status_display"] = display(TASK_STATUS, task["status"])
        task["error_category"] = "runtime_error"
        task["result_message"] = (
            f"执行协程早期异常（{'此前已有动作或未知回执，结果待人工核对' if unknown else '任务未执行'}"
            f"{'，租约已释放' if released else ''}）：{exc}")
        task["finished_at"] = iso(utc_now())
        task.setdefault("timeline", []).append({
            "at": task["finished_at"], "status": task["status"],
            "note": task["result_message"],
            "detail": {"error_category": "runtime_error"}
        })
        task["executor_early_finalized"] = True
        saved = True
        try:
            self._save(task)
        except Exception:
            saved = False
            _audit(f"终态无法持久化，内存态：{task['result_message']}；"
                   "回报与设备放行暂缓，待存储恢复对账")
        self._pending_early_finalize.pop(task_id, None)
        if saved:
            _audit(task["result_message"])
        try:
            device = runtime.devices.get(task["device_id"])
            lease_now = runtime.leases.get(task["device_id"])
            lease_owned = bool(lease_now and lease_now.get("task_id") == task_id)
            if unknown:
                runtime.devices.hold_unknown(
                    task["device_id"], task_id,
                    "执行协程早期异常且任务已有动作或未知回执，暂停待人工核对")
            elif (saved and device and device.get("status") == "busy"
                  and (lease_now is None or lease_now.get("task_id") == task_id)):
                # L47 归属条件：仅当设备无租约或租约仍归属本任务时才可恢复
                # ready；他任务（B）持新租约的 busy 一概不动
                runtime.devices._set_status(
                    task["device_id"], "ready", "执行协程早期异常，设备空闲")
        except Exception:
            pass
        if task.get("source") == "wx" and saved:
            try:
                await runtime.reporter.send_task_result(task)
                task["result_reported_at"] = iso(utc_now())
                try:
                    self._save(task)
                except Exception:
                    pass
            except Exception:
                pass
        return task

    async def execute(self, task_id: str) -> dict:
        runtime = self._runtime
        # P1（第五轮§6.4）：取任务/插件原在主体 try 外，协程早期异常会被
        # asyncio 默认吞掉使任务永久悬挂 awaiting_device——早期失败直接抛给
        # execute_guarded 的守卫收尾（finalize_early_error，无插件恢复/归位）。
        task = runtime.store.get_task(task_id)
        if task is None or task["status"] in TERMINAL_STATUSES:
            return task or {}
        plugin, task_type_entry = runtime.plugins.instance_for_task_type(task["task_type"])
        device_id = task["device_id"]
        try:
            requires_input_gate = (
                task_type_entry.get("requires_input", False) or task["task_type"] in INPUT_TASK_TYPES)
            device = runtime.devices.get(device_id)
            waiting = await self._finish_unstarted_camera_wait(task, task_type_entry)
            if waiting is not None:
                return waiting
            if requires_input_gate and not self._input_switch_on(device_id):
                acted = any(entry.get("is_action") for entry in task.get("step_log", []))
                return await self.finalize(
                    task, "unknown" if acted else "failed", plugin=plugin,
                    error_category="input_switch_disabled",
                    message="输入与发送未允许（开关关闭），禁止继续输入任务")
            if requires_input_gate:
                owner_verdict = self._runtime.check_input_owner(task)
                if owner_verdict is not None:
                    # §4.5.3：许可被撤销/缺失的排队任务=原许可失效——终结保留查询，
                    # 不因重新开启复活，也不补办当前许可。
                    return await self.finalize(
                        task, "cancelled", plugin=plugin,
                        error_category=owner_verdict["category"],
                        message=owner_verdict["message"])
            if requires_input_gate and device.get("enforce_input_gate", False):
                payload_body = task.get("payload") if isinstance(task.get("payload"), dict) else {}
                eligibility = runtime.devices.eligibility(
                    task["device_id"], require_input=True,
                    ime_device_id=payload_body.get("ime_device_id"),
                    require_keyboard_active=task_type_entry.get("requires_keyboard_active", True),
                    require_ime_ready=task_type_entry.get("focuses_input_before_typing") is not True,
                )
                blocking = list(eligibility["blocking_checks"])
                # C 组阻断缺陷修复：派发器取得租约后设备进入忙碌，忙碌由本任务
                # 自身租约导致时不属于输入门禁阻断项；其他阻断仍照常拦截。
                lease = runtime.leases.get(task["device_id"])
                if "device_busy" in blocking and lease and lease.get("task_id") == task_id:
                    blocking = [item for item in blocking if item != "device_busy"]
                if blocking:
                    return await self.finalize(
                        task, "failed", plugin=plugin, error_category="device_not_eligible",
                        message=f"设备不具备输入任务资格：{'、'.join(blocking)}",
                    )
            # ---- 参数/权限校验 ----
            self._set_status(task, "validating", "校验任务参数与权限")
            try:
                runtime.validate_task_preconditions(task)
            except ApiError as exc:
                return await self.finalize(
                    task, "unknown" if task.get("step_log") else "failed", plugin=plugin,
                    error_category=exc.key, message=str(exc))
            device_context = runtime.devices.get(device_id)
            verdict = await plugin.validate(deepcopy(task), deepcopy(device_context))
            if not verdict.get("ok"):
                return await self.finalize(
                    task,
                    "failed",
                    plugin=plugin,
                    error_category=verdict.get("error_category", "invalid_request"),
                    message=verdict.get("message", "参数或权限校验未通过"),
                )

            waiting = await self._finish_unstarted_camera_wait(task, task_type_entry)
            if waiting is not None:
                return waiting
            # ---- 步骤计划 ----
            steps = await plugin.plan(deepcopy(task), deepcopy(runtime.devices.get(device_id)))
            if task.get("read_only_task") and any(
                    step.get("is_action") or any(str(capability).startswith(("arm.", "input."))
                                                for capability in step.get("required_capabilities", []))
                    for step in steps):
                return await self.finalize(
                    task, "failed", plugin=plugin, error_category="capability_not_allowed",
                    message="只读任务计划包含物理动作或输入能力，已拒绝执行")
            # Match the invocation gate's registered manifest, without invoking capabilities.
            manifest = runtime.plugins.get(plugin.plugin_id)["manifest"]
            undeclared = sorted({
                capability for step in steps
                for capability in step.get("required_capabilities", [])
                if capability not in manifest.get("capabilities", [])
            })
            if undeclared:
                message = f"计划所需能力未在插件清单中声明：{'、'.join(undeclared)}，已拒绝执行"
                runtime.audit.record(
                    "plugin_violation_blocked", task_id=task_id, device_id=device_id,
                    reason=message,
                )
                if (task.get("step_log") or task.get("step_cursor")
                        or task.get("action_call_count", 0) or task.get("capability_call_count", 0)
                        or task.get("stop_result_unknown")):
                    return await self.finalize(
                        task, "unknown", plugin=plugin, error_category="capability_not_allowed",
                        message=f"{message}；此前执行结果需人工核对")
                # An untouched task needs no recovery, cleanup, stop, or home movement.
                task.update(
                    status="failed", status_display=display(TASK_STATUS, "failed"),
                    error_category="capability_not_allowed", result_message=message,
                    failure_reason=message, finished_at=iso(utc_now()),
                    motion_not_started=True, home_verified=False,
                    home_check={"home_ok": False, "checked_at": None,
                                "skipped": "计划预检拒绝，未执行动作或取得新的归位证明"},
                    cleanup_result={"cleaned": True, "skipped": "尚未执行，无需清理"},
                )
                self._timeline(task, message)
                self._save(task)
                if self._owns_execution_lease(task, allow_expired=True):
                    runtime.leases.release(device_id, task["lease_id"])
                    if runtime.devices.get(device_id)["status"] == "busy":
                        runtime.devices._set_status(device_id, "ready", "计划预检拒绝，未执行动作")
                if task.get("source") == "wx":
                    await runtime.reporter.send_task_result(task)
                return task
            task["plan"] = steps
            self._timeline(task, f"生成步骤计划，共 {len(steps)} 步")
            self._set_status(task, "executing", "开始执行步骤")
            task["started_at"] = iso(utc_now())
            self._save(task)

            # ---- 步骤循环 ----
            while task["step_cursor"] < len(steps):
                if self._cancel_requested(task):
                    return await self._cancel_flow(task, plugin)
                if runtime.estop.active:
                    return await self._cancel_flow(task, plugin, reason="全局急停触发，停止执行")
                if requires_input_gate and not self._input_switch_on(device_id):
                    acted = any(entry.get("is_action") for entry in task.get("step_log", []))
                    return await self.finalize(
                        task, "unknown" if acted else "failed", plugin=plugin,
                        error_category="input_switch_disabled",
                        message="输入与发送未允许（开关被关闭），停止后续步骤；已发生的动作需核对")

                step = steps[task["step_cursor"]]
                task.setdefault("step_log", []).append(
                    {
                        "step_no": step["step_no"],
                        "action": step["action"],
                        "action_display": step["action_display"],
                        "is_action": step.get("is_action", False),
                        "phase": "started",
                        "at": iso(utc_now()),
                    }
                )
                self._save(task)

                ctx = StepContext(runtime, task, plugin.manifest)
                result = await self._run_step(plugin, step, ctx)
                if task.get("motion_result_unknown"):
                    uncertainty = task.get("motion_uncertainty_reason", "动作回执未确认")
                    if result.get("message") and result["message"] != uncertainty:
                        uncertainty = result["message"] + "；" + uncertainty
                    result = {
                        **result, "outcome": "unknown",
                        "message": uncertainty,
                    }

                log_entry = {
                    "step_no": step["step_no"],
                    "action": step["action"],
                    "action_display": step["action_display"],
                    "is_action": step.get("is_action", False),
                    "phase": "done",
                    "outcome": result["outcome"],
                    "message": result.get("message", ""),
                    "detail": result.get("detail") or {},
                    "at": iso(utc_now()),
                }
                task.setdefault("step_log", []).append(log_entry)
                task["step_cursor"] += 1
                self._timeline(task, f"步骤 {step['step_no']}：{step['action_display']} → {self._outcome_display(result['outcome'])}")
                self._save(task)

                if step.get("evidence_required", True):
                    await self._capture_evidence(task, f"步骤{step['step_no']}取证：{step['action_display']}")

                if result["outcome"] == "unknown":
                    return await self.finalize(
                        task, "unknown", plugin=plugin,
                        message=result.get("message", "步骤结果无法确认"),
                        extras=result.get("detail") or {},
                    )
                if result["outcome"] == "failed":
                    return await self.finalize(
                        task, "failed", plugin=plugin,
                        error_category=result.get("error_category", "driver_error"),
                        message=result.get("message", "步骤明确失败"),
                        extras=result.get("detail") or {},
                    )
                # 步骤成功后、收尾前：取消/急停仍然生效（先停止、清理和取证）
                if self._cancel_requested(task) or self._runtime.estop.active:
                    return await self._cancel_flow(
                        task, plugin,
                        reason="全局急停触发，停止执行" if self._runtime.estop.active else "收到取消请求",
                    )

            # ---- 全部步骤成功 ----
            return await self.finalize(task, "confirmed", plugin=plugin, extras=self._aggregate_results(task))
        except Exception as exc:  # 未预期异常：结果未知，不能猜
            return await self.finalize(
                task, "unknown", plugin=plugin,
                message=f"运行时异常，动作结果无法确认：{exc}",
            )

    async def _run_step(self, plugin, step: dict, ctx: StepContext) -> dict:
        timeout = float(step.get("timeout_seconds", 15))
        try:
            result = await asyncio.wait_for(plugin.execute_step(step, ctx), timeout=timeout)
            outcome = result.get("outcome")
            if outcome not in ("success", "failed", "unknown"):
                result = {"outcome": "unknown", "message": f"插件返回未登记的步骤结果：{outcome}"}
            return result
        except asyncio.TimeoutError:
            return {"outcome": "unknown", "error_category": "driver_timeout", "message": "步骤执行超时，动作结果无法确认"}
        except PluginViolationError as exc:
            self._runtime.audit.record(
                "plugin_violation_blocked",
                task_id=ctx.task["task_id"],
                device_id=ctx.device_id,
                reason=f"插件越权已拦截：{exc}",
            )
            return {"outcome": "failed", "error_category": "capability_not_allowed", "message": f"插件越权已拦截：{exc}"}
        except DriverError as exc:
            if exc.category == "driver_disconnected":
                return {"outcome": "unknown", "error_category": exc.category, "message": f"连接中断，动作结果无法确认：{exc.message}"}
            if exc.category == "input_switch_disabled":
                if getattr(exc, "motion_not_started", False):
                    # P05 边界认领失败：驱动证实零动作——明确失败，非未知
                    return {"outcome": "failed", "error_category": exc.category,
                            "message": exc.message}
                if ctx.task.get("action_call_count", 0):
                    # §4.5：步骤中途开关被关=已认领动作不可重放也不可承诺回滚——
                    # 保留已发生动作的核对语义（unknown），错误码仍指向开关。
                    return {"outcome": "unknown", "error_category": exc.category,
                            "message": f"{exc.message}；此前已发生动作，需人工核对"}
            if (exc.category == "driver_uncalibrated"
                    and ctx.task.get("action_call_count", 0)
                    and not self._input_switch_on(ctx.device_id)):
                return {"outcome": "unknown", "error_category": exc.category,
                        "message": f"{exc.message}；此前已发生动作，需人工核对"}
            return {"outcome": "failed", "error_category": exc.category, "message": exc.message, "detail": {"category_display": exc.category_display}}

    # ---------- 取消 ----------
    async def _cancel_flow(self, task: dict, plugin, reason: str = "收到取消请求",
                           *, outcome_unknown: bool = False) -> dict:
        if not self._owns_execution_lease(task, allow_expired=True):
            task["cancellation_handled"] = True
            return await self.finalize(
                task, "unknown", plugin=plugin, error_category="lease_conflict",
                message="原设备租约已失效，无法由旧任务确认停止；未干扰新任务，需人工核对",
            )
        task["cancel_requested"] = True
        task.setdefault("cancel_reason", reason)
        task["cancellation_handled"] = True
        self._save(task)
        ctx = StepContext(self._runtime, task, plugin.manifest)
        attempts = task.get("stop_attempt_count", 0)
        try:
            result = await asyncio.wait_for(
                plugin.cancel(ctx), timeout=CANCEL_HOOK_TIMEOUT_SECONDS)
        except Exception as exc:
            result = {"stopped": False, "outcome_hint": "unknown",
                      "message": f"插件取消未确认：{exc or type(exc).__name__}"}
        task["cancel_result"] = result
        if not self._owns_execution_lease(task, allow_expired=True):
            return await self.finalize(
                task, "unknown", plugin=plugin, error_category="lease_conflict",
                message="取消期间原租约已失效，旧任务不补发停止；需人工核对原动作结果",
            )
        # 未调用停止能力的插件仍须经过驱动确认；已尝试的 STOP 绝不自动重放。
        if not task.get("read_only_task") and task.get("stop_attempt_count", 0) == attempts:
            try:
                await self._runtime._stop_task_arm(task)
            except Exception:
                pass
        evidence = await self._capture_evidence(task, "取消前取证")
        # 清理现场与归位由 finalize 统一执行
        uncertain = (outcome_unknown or task.get("stop_result_unknown")
                     or not isinstance(result, dict) or result.get("stopped") is not True
                     or result.get("ok") is False or result.get("outcome_hint") == "unknown")
        uncertain = uncertain or any(
            e.get("phase") == "started" and e.get("is_action") and not self._step_completed(e, task)
            for e in task.get("step_log", [])
        )
        outcome = "unknown" if uncertain else "cancelled"
        message = ("取消时停止或动作结果无法确认，需人工确认" if uncertain
                   else f"任务已取消并确认停止：{task.get('cancel_reason') or reason}")
        return await self.finalize(
            task, outcome, plugin=plugin,
            message=message,
            error_category="result_unknown_pending" if uncertain else None,
            extras={"cancel_reason": reason, "evidence_id": (evidence or {}).get("evidence_id")},
            skip_final_evidence=True,
        )

    def _step_completed(self, started_entry: dict, task: dict) -> bool:
        for entry in task.get("step_log", []):
            if (entry.get("phase") == "done" and entry.get("outcome") == "success"
                    and entry.get("step_no") == started_entry.get("step_no")):
                return True
        return False

    # ---------- 收尾：清理、归位、结果、回报 ----------
    async def finalize(
        self,
        task: dict,
        outcome: str,
        *,
        plugin=None,
        error_category: str | None = None,
        message: str | None = None,
        extras: dict | None = None,
        skip_final_evidence: bool = False,
    ) -> dict:
        runtime = self._runtime
        device_id = task["device_id"]
        # A failed cleanup/navigation step must not erase a proven send.
        extras = {**self._aggregate_results(task), **(extras or {})}
        if task.get("motion_result_unknown"):
            outcome = "unknown"
            error_category = "result_unknown_pending"
            message = message or task.get("motion_uncertainty_reason", "动作回执未确认，禁止继续动作")

        if plugin is None:
            # P1（第五轮§6.4）：插件解析自身失败不得让收尾整体逃逸——
            # plugin 保持 None，后续清理/恢复分支已有 None 守卫。
            try:
                plugin, _ = runtime.plugins.instance_for_task_type(task["task_type"])
            except Exception:
                plugin = None
        # R2-07/FIX-08 ⑦：纯视觉未知（机械完成 + 已持久视觉暂停）不伪造机械
        # 未知保持：登记视觉问题后释放任务租约；阻断由滑动动作存储的
        # manual_resume_required 承担（恢复走 resume_reviewed）。
        visual_pause = False
        pause_reason = None
        for entry in reversed(task.get("step_log") or []):
            step_detail = entry.get("detail") or {}
            if step_detail.get("mechanical_status") or step_detail.get("pause_issue_id"):
                visual_pause = (step_detail.get("mechanical_status") == "confirmed"
                                and bool(step_detail.get("pause_issue_id")))
                pause_reason = step_detail.get("pause_issue_id")
                break
        if outcome == "unknown" and self._owns_execution_lease(task, allow_expired=True):
            if not visual_pause:
                runtime.devices.hold_unknown(device_id, task["task_id"],
                                             "任务结果未知，自动暂停待人工确认")
            else:
                runtime.audit.record("visual_pause_lease_released",
                                     device_id=device_id,
                                     task_id=task["task_id"],
                                     reason=str(pause_reason))
        if (self._owns_execution_lease(task, allow_expired=True)
                and (self._cancel_requested(task) or runtime.estop.active)
                and not task.get("cancellation_handled")):
            if (is_screen_wake_task(task) and outcome == "failed"
                    and extras.get("motion_not_started") is True
                    and extras.get("controller_completed") is False
                    and extras.get("tap_count") == 0
                    and not task.get("stop_result_unknown") and not runtime.estop.active):
                task["cancellation_handled"] = True
                outcome, message = "cancelled", "唤醒在运动前取消，未下发动作"
            else:
                return await self._cancel_flow(
                    task, plugin, outcome_unknown=outcome == "unknown",
                    reason="全局急停触发，停止执行" if runtime.estop.active else "收到取消请求",
                )
        if is_screen_wake_task(task):
            return await self._finalize_screen_wake(
                task, outcome, error_category=error_category, message=message,
                extras=extras, skip_final_evidence=skip_final_evidence)

        if task.get("stop_result_unknown"):
            outcome = "unknown"
            error_category = "result_unknown_pending"
            message = "机械臂停止结果未知，禁止继续动作或自动重试，需人工确认"
        lease = runtime.leases.get(device_id)
        lease_replaced = bool(lease and (
            lease.get("task_id") != task["task_id"]
            or lease.get("lease_id") != task.get("lease_id")))
        if outcome == "unknown" and not lease_replaced:
            if not visual_pause:
                runtime.devices.hold_unknown(device_id, task["task_id"],
                                             "任务结果未知，自动暂停待人工确认")
            else:
                runtime.audit.record("visual_pause_lease_released",
                                     device_id=device_id,
                                     task_id=task["task_id"],
                                     reason=str(pause_reason))

        # 业务步骤失败或结果未知时，先给插件一次有上限的恢复机会。
        # 取消/急停只允许停止动作，不在此处重试界面动作。
        recovery_result = task.get("recovery_result")
        if (outcome == "failed" and task.get("plan")
                and not task.get("recovery_attempted") and not task.get("cancellation_handled")
                and not task.get("stop_result_unknown") and self._owns_execution_lease(task)):
            task["recovery_attempted"] = True
            task["finalization_phase"] = "recover"
            self._save(task)
            failed_entry = next(
                (
                    entry
                    for entry in reversed(task.get("step_log", []))
                    if entry.get("phase") == "done" and entry.get("outcome") in ("failed", "unknown")
                ),
                None,
            )
            failure = {
                "action": (failed_entry or {}).get("action"),
                "step_no": (failed_entry or {}).get("step_no"),
                "outcome": (failed_entry or {}).get("outcome", outcome),
                "message": (failed_entry or {}).get("message") or message or "步骤结果无法确认",
                "error_category": error_category,
            }
            try:
                recovery_result = await plugin.recover(
                    failure, StepContext(runtime, task, plugin.manifest)
                )
            except Exception as exc:
                recovery_result = {
                    "recovered": False,
                    "home_verified": False,
                    "attempted": True,
                    "message": f"恢复钩子异常：{exc}，需人工确认",
                }
            task["recovery_result"] = recovery_result or {
                "recovered": False,
                "home_verified": False,
                "attempted": True,
                "message": "未获得恢复结果，需人工确认",
            }
            task["finalization_phase"] = None
            task.setdefault("recovery_log", []).append(
                {
                    "action": "recover_after_failure",
                    "action_display": "失败后返回微信主界面（有限恢复）",
                    "failed_action": failure.get("action"),
                    "outcome": "success" if task["recovery_result"].get("home_verified") else "unknown",
                    "message": task["recovery_result"].get("message", ""),
                    "detail": task["recovery_result"],
                    "at": iso(utc_now()),
                }
            )
            self._timeline(task, "失败后恢复处理", task["recovery_result"])
            self._save(task)

        if task.get("stop_result_unknown") or task.get("motion_result_unknown"):
            outcome = "unknown"
            error_category = "result_unknown_pending"
            message = message or task.get("motion_uncertainty_reason") or "恢复期间动作或停止结果未知，禁止清理或归位，需人工确认"
            if not lease_replaced:
                runtime.devices.hold_unknown(device_id, task["task_id"],
                                             "任务结果未知，自动暂停待人工确认")

        # 结束取证
        if not skip_final_evidence:
            try:
                await self._capture_evidence(task, "任务结束取证")
            except Exception:
                pass

        # 清理现场 + 归位
        ctx = StepContext(runtime, task, plugin.manifest if plugin else {"plugin_id": task.get("plugin_id", "")})
        cleanup_result = {"cleaned": True, "message": "无需清理"}
        try:
            if outcome == "unknown":
                cleanup_result = {"cleaned": False, "message": "结果未知，禁止自动清理"}
            elif not self._owns_execution_lease(task):
                cleanup_result = {"cleaned": False, "message": "原租约已失效，旧任务不执行清理"}
            elif plugin is not None:
                task["finalization_phase"] = "cleanup"
                self._save(task)
                cleanup_result = await plugin.cleanup(ctx)
        except Exception as exc:
            cleanup_result = {"cleaned": False, "message": f"清理异常：{exc}"}
        task["finalization_phase"] = None
        task["cleanup_result"] = cleanup_result
        self._save(task)
        if task.get("stop_result_unknown") or task.get("motion_result_unknown"):
            outcome = "unknown"
            error_category = "result_unknown_pending"
            message = message or task.get("motion_uncertainty_reason") or "清理期间动作或停止结果未知，禁止归位，需人工确认"
            if not lease_replaced:
                runtime.devices.hold_unknown(device_id, task["task_id"],
                                             "任务结果未知，自动暂停待人工确认")
        home_check = {"home_ok": False, "detail": {"skip": "设备离线或无机械臂"}}
        if outcome == "unknown":
            home_check["detail"]["skip"] = "结果未知，禁止自动停止或归位；仅保留只读取证"
        elif not self._owns_execution_lease(task):
            home_check["detail"]["skip"] = "原租约已失效，不执行安全检查或归位"
        elif task.get("stop_result_unknown") or (task.get("cancellation_handled") and outcome == "unknown"):
            home_check["detail"]["skip"] = "取消或停止结果未知，禁止自动归位"
        else:
            try:
                task["finalization_phase"] = "home_check"
                self._save(task)
                cancel_at_check = self._cancel_requested(task)
                home_check = await runtime.devices.home_check(
                    device_id, read_only=bool(task.get("read_only_task")),
                    execution_guard=lambda: (
                        self._owns_execution_lease(task)
                        and self._cancel_requested(task) == cancel_at_check
                        and not runtime.estop.active))
            except Exception as exc:
                home_check = {"home_ok": False, "detail": {"fail_reason": str(exc)}}
            if home_check.get("detail", {}).get("motion_result_unknown"):
                message = "安全检查期间动作回执未确认，需人工核对"
                runtime._hold_task_motion_unknown(task, message)
                outcome, error_category = "unknown", "result_unknown_pending"
            task["finalization_phase"] = None
            self._save(task)

        # 对聊天任务，平台归位检查还不够；必须同时证明应用主界面已确认。
        # 没有生成步骤计划意味着任务在动作前被拒绝，平台检查即可证明未改变界面。
        park_result = None
        if task["task_type"] == "chat_send":
            return_main_ok = any(
                entry.get("action") == "return_main"
                and entry.get("phase") == "done"
                and entry.get("outcome") == "success"
                for entry in task.get("step_log", [])
            )
            if not task.get("plan"):
                return_main_ok = True
            recovery_home_ok = bool((recovery_result or {}).get("home_verified"))
            return_main_ok = return_main_ok or recovery_home_ok
            # 业务界面停靠证据并入收尾核对：业务 的 conversation_turn_completed
            # 依赖 home_check 里的 park_* 字段区分“已完成”和“部分完成”。
            park_done = [entry for entry in task.get("step_log", [])
                         if entry.get("action") == "park_ime_delete_key"
                         and entry.get("phase") == "done"]
            if park_done:
                park_entry = park_done[-1]
                park_detail = park_entry.get("detail") or {}
                park_result = {
                    "park_completed": park_entry.get("outcome") == "success",
                    "park_target_id": park_detail.get("park_target_id"),
                    "park_tap_executed": bool(park_detail.get("park_tap_executed")),
                    "park_role": park_detail.get("role"),
                    "park_outcome": park_entry.get("outcome"),
                    "park_message": park_entry.get("message", ""),
                }
            elif not task.get("plan"):
                park_result = {"park_completed": True, "park_target_id": None,
                               "park_tap_executed": False, "park_outcome": "skipped",
                               "park_message": "任务在动作前被拒绝，未发生停靠"}
        else:
            return_main_ok = True
        home_verified = outcome != "unknown" and bool(home_check.get("home_ok")) and bool(return_main_ok)
        task["home_verified"] = home_verified
        task["home_verification"] = {
            "platform_home_check": bool(home_check.get("home_ok")),
            "wechat_main_confirmed": bool(return_main_ok),
            "recovery_attempted": bool(task.get("recovery_attempted")),
            "checked_at": home_check.get("checked_at"),
        }

        # 设备状态处理
        device = runtime.devices.get(device_id)
        current_lease = runtime.leases.get(device_id)
        lease_replaced = bool(current_lease and (
            current_lease.get("task_id") != task["task_id"]
            or current_lease.get("lease_id") != task.get("lease_id")))
        if not self._owns_execution_lease(task):
            task["execution_lease_lost"] = True
            task["home_verified"] = home_verified = False
            task["home_verification"]["platform_home_check"] = False
            if outcome in {"confirmed", "cancelled"}:
                outcome = "unknown"
                error_category = "lease_conflict"
                message = "原设备租约已失效，无法确认安全收尾；未重新执行动作，需人工核对"
        if lease_replaced:
            runtime.audit.record(
                "plugin_violation_blocked", task_id=task["task_id"], device_id=device_id,
                reason="原租约已被替换，旧任务不修改新任务设备状态或释放新租约")
        elif outcome == "unknown":
            runtime.devices.hold_unknown(device_id, task["task_id"],
                                         "任务结果未知，自动暂停待人工确认")
            runtime.audit.record("auto_pause_device", device_id=device_id, task_id=task["task_id"], reason="任务结果未知，自动暂停待人工确认")
            runtime.devices._alert(
                device_id, "critical",
                f"任务结果未知：{message}；设备已暂停，需人工确认",
                task_id=task["task_id"], action="auto_pause_device",
            )
        else:
            refreshed = runtime.devices.get(device_id)
            if not home_verified:
                # 未确认主界面或平台归位时，设备进入恢复态，绝不进入下一任务。
                blocked_status = "recovering" if task.get("task_type") == "chat_send" else "degraded"
                if refreshed["status"] not in ("offline", "quarantined", "paused", "recovering"):
                    runtime.devices._set_status(device_id, blocked_status, "任务结束但微信主界面/归位未确认")
                refreshed["home_ok"] = False
                refreshed["status_reason"] = "任务结束但微信主界面/归位未确认，需人工处理"
                runtime.store.save_device(refreshed)
                runtime.devices._alert(
                    device_id,
                    "critical",
                    "任务结束但微信主界面或归位未确认，设备不能领取新任务",
                    task_id=task["task_id"],
                    action="home_verification_failed",
                )
            elif refreshed["status"] == "busy":
                if home_verified:
                    runtime.devices._set_status(device_id, "ready", "任务完成，归位检查通过")
                else:
                    runtime.devices._set_status(device_id, "degraded", "任务结束但归位未确认")

        # 任务状态
        task["status"] = outcome
        if outcome == "confirmed":
            registry = runtime.plugins.task_type_registry()
            confirmed_display = registry.get(task["task_type"], {}).get("confirmed_display", "已完成")
            task["status_display"] = confirmed_display
        else:
            task["status_display"] = display(TASK_STATUS, outcome)
        task["finished_at"] = iso(utc_now())
        if error_category:
            task["error_category"] = error_category
        if message:
            task["result_message"] = message
        if outcome == "unknown":
            task["uncertainty_reason"] = message or "动作结果无法确认"
        if outcome == "failed":
            task["failure_reason"] = message
        if outcome == "cancelled":
            task["cancel_reason"] = extras.get("cancel_reason", task.get("cancel_reason"))
        task["cleanup_result"] = cleanup_result
        task["home_check"] = {
            "home_ok": home_check.get("home_ok"),
            "checked_at": home_check.get("checked_at"),
            # 停靠证据随 home_check 一起进入 task-result.v1，供 业务 判定
            # conversation_turn_completed 的完成状态；非聊天任务没有这些字段。
            **(park_result or {}),
        }
        if park_result is not None:
            task["home_check"]["parking_semantics"] = (
                "校准安全停车位（无屏幕接触，非硬件回零）"
                if park_result.get("park_role") == "calibration_safe_park" else
                "业务界面停靠位（ime_delete_key），非硬件回零"
            )
        if extras.get("provider_message_id"):
            task["provider_message_id"] = extras["provider_message_id"]
        if extras.get("sent_at"):
            task["sent_at"] = extras["sent_at"]
        self._timeline(task, f"任务结束：{task['status_display']}", {"message": message or ""})
        self._save(task)

        # 只有完整 home_verified 才能释放租约。未确认时保留租约，等待人工
        # reset/resume 或设备重新挂载显式接管，避免下一任务趁界面未知时进入。
        lease = runtime.leases.get(device_id)
        matching_lease = bool(lease and lease.get("task_id") == task["task_id"]
                              and lease.get("lease_id") == task.get("lease_id"))
        if matching_lease and home_verified:
            runtime.leases.release(device_id, lease["lease_id"])
        elif matching_lease:
            task["lease_retained"] = True
            task["lease_retained_reason"] = "home_verified 未通过，等待人工处理"
            runtime.audit.record(
                "task_lease_retained",
                task_id=task["task_id"],
                device_id=device_id,
                reason=task["lease_retained_reason"],
                detail={"lease_id": lease.get("lease_id")},
            )
        # 租约字段和审计结果需要在回报前持久化，便于进程重启后对账。
        self._save(task)

        # 回报外部系统（业务）
        if task.get("source") == "wx":
            await runtime.reporter.send_task_result(task)

        # 回报可见消息观测（customer-message.v1，平台统一回报，插件不直接联网）
        for event in (extras or {}).get("outbound_events", []) or []:
            try:
                await runtime.reporter.send_customer_message(event, task_id=task["task_id"])
            except Exception:
                pass

        return task

    async def _finalize_screen_wake(self, task, outcome, *, error_category, message,
                                    extras, skip_final_evidence):
        """Wake is not a navigation task: never recover, home, or revoke calibration."""
        runtime = self._runtime
        device_id = task["device_id"]
        detail = {"controller_completed": False, "tap_count": 0,
                  "effect_status": "unverified", "motion_not_started": False}
        if not task.get("wake_driver_started") and not any(
                entry.get("phase") == "done" and entry.get("detail", {}).get("contract_version") == "screen-wake.v1"
                for entry in task.get("step_log", [])):
            record_unstarted_result(task, "failed", message or "人工唤醒未开始")
        for entry in task.get("step_log", []):
            if entry.get("phase") == "done" and entry.get("action") in {"wake_screen", "wake_preflight"}:
                detail.update({key: entry.get("detail", {}).get(key, value)
                               for key, value in detail.items()})
        detail.update({key: extras[key] for key in detail if key in extras})
        if task.get("stop_result_unknown"):
            outcome, error_category = "unknown", "result_unknown_pending"
            message = "停止结果未知，禁止追加动作或自动重试"
        if not self._owns_execution_lease(task, allow_expired=True) and not (
                outcome == "failed" and detail["motion_not_started"] is True
                and detail["controller_completed"] is False and detail["tap_count"] == 0):
            outcome, error_category = "unknown", "lease_conflict"
            message = "原租约已失效，未干扰新任务；动作结果需人工核对"
        if outcome == "confirmed" and not (
                detail["controller_completed"] is True and detail["tap_count"] == 2):
            outcome, error_category = "unknown", "result_unknown_pending"
            message = "未取得完整双击确认，禁止重试"
        lease = runtime.leases.get(device_id)
        replaced = lease and (lease.get("task_id") != task["task_id"]
                              or lease.get("lease_id") != task.get("lease_id"))
        if outcome == "unknown" and not replaced:
            runtime.devices.hold_unknown(device_id, task["task_id"],
                                         "唤醒动作结果未知，暂停待人工核对")
        if not skip_final_evidence:
            await self._capture_evidence(task, "人工唤醒结束画面（不识图、不解锁）")
        task.update(detail)
        task.update(
            status=outcome, status_display=display(TASK_STATUS, outcome),
            finished_at=iso(utc_now()), result_message=message or "双击已完成，亮屏效果请核对",
            home_verified=False,
            home_check={"home_ok": False, "checked_at": None,
                        "skipped": "人工唤醒不追加归位，也不生成新的主页或校准证明"},
            cleanup_result={"cleaned": True, "skipped": "人工唤醒无恢复或清理动作"},
        )
        if outcome == "confirmed":
            task["status_display"] = ("双击已完成，已观察到亮屏" if detail["effect_status"] == "awake"
                                      else "双击已完成，亮屏未核验")
        if error_category:
            task["error_category"] = error_category
        if outcome == "failed":
            task["failure_reason"] = task["result_message"]
        if outcome == "unknown":
            task["uncertainty_reason"] = task["result_message"]
        device = runtime.devices.get(device_id)
        if not replaced:
            if outcome == "unknown":
                runtime.devices._alert(
                    device_id, "critical", task["result_message"],
                    task_id=task["task_id"], action="auto_pause_device")
            elif lease and device["status"] == "busy":
                runtime.devices._set_status(device_id, "ready", "人工唤醒结束；原安全门禁保持")
        self._timeline(task, f"人工唤醒结束：{task['status_display']}")
        self._save(task)
        if lease and not replaced:
            runtime.leases.release(device_id, lease["lease_id"])
        if task.get("source") == "wx":
            await runtime.reporter.send_task_result(task)
        return task

    # ---------- 内部 ----------
    async def _capture_evidence(self, task: dict, label: str) -> dict | None:
        try:
            camera = self._runtime.devices.camera_broker(task["device_id"])
            record = self._runtime.evidence.capture_screenshot(
                device_id=task["device_id"], camera=camera, task_id=task["task_id"], label=label
            )
            task.setdefault("evidence_ids", []).append(record["evidence_id"])
            self._runtime._save_task_progress(task)
            return record
        except Exception:
            return None

    def _aggregate_results(self, task: dict) -> dict:
        extras: dict = {}
        outbound_events: list[dict] = []
        for entry in task.get("step_log", []):
            detail = entry.get("detail") or {}
            for key in ("provider_message_id", "sent_at", "text_hash"):
                if detail.get(key):
                    extras[key] = detail[key]
            if detail.get("outbound_events"):
                outbound_events.extend(detail["outbound_events"])
        if outbound_events:
            extras["outbound_events"] = outbound_events
        return extras

    def _outcome_display(self, outcome: str) -> str:
        return {"success": "成功", "failed": "明确失败", "unknown": "结果未知"}.get(outcome, outcome)

    def _set_status(self, task: dict, status: str, note: str) -> None:
        task["status"] = status
        task["status_display"] = display(TASK_STATUS, status)
        self._timeline(task, note)
        self._save(task)
