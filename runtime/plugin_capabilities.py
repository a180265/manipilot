"""插件能力调用段（越权拦截门禁在此层）。

Auto_CS#7 自 runtime.py 外迁（构建脚本切片纯搬家，零行为变更）：能力调用越权
拦截、动作回执日志边界、能力分发、自动手势能力分发、任务进度持久化。
ArmFleetRuntime 经 Mixin 继承本模块；tasks/plugin_api 与 runtime/executor 的
既有引用路径不变。
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

from runtime.action_receipts import motion_receipt_unknown
from runtime.device_eligibility import INPUT_TASK_TYPES
from runtime.errors import ApiError
from runtime import screen_wake
from runtime.util import iso, parse_iso, sha256_bytes, utc_now

# 自 runtime.py 随唯一消费者 _stop_task_arm 迁入；字符串路径补丁已跟随本模块
# （test_plugin_cancellation）。
TASK_STOP_TIMEOUT_SECONDS = 5.0


class PluginCapabilityMixin:
    # ================= 插件能力调用（越权拦截在此） =================
    def _save_task_progress(self, task: dict) -> None:
        """保留其他请求写入的取消标记，不让执行中的旧副本覆盖。"""
        latest = self.store.get_task(task["task_id"])
        if latest:
            if "layout_execution_binding" in latest:
                task["layout_execution_binding"] = deepcopy(latest["layout_execution_binding"])
            for field in ("cancel_requested", "cancel_reason", "stop_result_unknown",
                          "motion_result_unknown", "motion_uncertainty_reason"):
                if latest.get(field):
                    task[field] = latest[field]
            if latest.get("stop_attempt_count", 0) > task.get("stop_attempt_count", 0):
                task["stop_attempt_count"] = latest["stop_attempt_count"]
                task["stop_result"] = latest.get("stop_result")
        self.store.save_task(task)

    def _pin_task_layout(self, stored, task):
        """Freeze the selected versions on first visual use, including no selection."""
        from drivers.arm import DriverError
        from runtime.layout_observation import LayoutObservationError

        try:
            current = self.layout_observation.execution_binding(task["device_id"])
        except LayoutObservationError as exc:
            raise DriverError("driver_policy_rejected", str(exc)) from exc
        if ("layout_execution_binding" in stored
                and stored["layout_execution_binding"] != current):
            error = DriverError("driver_policy_rejected", "任务期间模板或粗框版本已变化，请重新核验")
            error.motion_not_started = True
            raise error
        task["layout_execution_binding"] = deepcopy(current)
        self._save_task_progress(task)
        return current

    def _hold_task_motion_unknown(self, task: dict, message: str) -> None:
        task["motion_result_unknown"] = True
        task["motion_uncertainty_reason"] = message
        lease = self.leases.get(task["device_id"])
        if not lease or (lease.get("task_id") == task["task_id"]
                         and lease.get("lease_id") == task.get("lease_id")):
            self.devices.hold_unknown(task["device_id"], task["task_id"], message)
        self._save_task_progress(task)

    async def _run_task_action(self, task: dict, capability_id: str, invoke):
        """Journal at the driver boundary, including recovery and cleanup actions."""
        from drivers.arm import DriverError

        stored = self.store.get_task(task["task_id"]) or {}
        if (stored.get("motion_result_unknown")
                or (stored.get("action_receipt") or {}).get("state") == "pending"):
            error = DriverError("driver_stopped", "前一动作尚未确认，禁止继续动作")
            error.motion_not_started = True
            raise error
        receipt = {
            "capability_id": capability_id, "state": "pending",
            "started_at": iso(utc_now()),
        }
        task["action_receipt"] = receipt
        self._save_task_progress(task)
        # R2-04（camera-binding 附件 §4.1 / FIX-05）：动作认领后由实际执行者
        # 非阻塞取得本设备共享安装锁，持有到最后安全收尾（回执落库/未知阻断
        # 完成）再释放；绑定写（排他锁）或另一动作进行中 → 立即拒绝不排队。
        # 锁根必须与调试台（INSTALLATION_LOCK_ROOT）/帧桥（INSTALLATION_DATA_ROOT）
        # 同源：<data_root>/installation_locks/{device_id}.lock。
        action_lock = self._installation_action_lock(task["device_id"])
        try:
            action_lock.__enter__()
        except Exception as exc:
            receipt["state"] = "not_started"
            receipt["message"] = str(exc)
            self._save_task_progress(task)
            error = DriverError("driver_policy_rejected",
                                f"设备维护占用中（绑定写或另一动作进行）：{exc}")
            error.motion_not_started = True
            raise error from exc
        try:
            result = await invoke()
            receipt["state"] = "unknown" if motion_receipt_unknown(result) else "confirmed"
            if isinstance(result, dict) and result.get("motion_not_started") is True:
                if receipt["state"] != "unknown":
                    receipt["state"] = "not_started"
            return result
        except BaseException as exc:
            receipt["state"] = "not_started" if getattr(exc, "motion_not_started", False) else "unknown"
            receipt["message"] = str(exc) or type(exc).__name__
            raise
        finally:
            receipt["finished_at"] = iso(utc_now())
            # P04：认领过的输入 effect 按回执收尾（not_started 仅限驱动证实未下发）
            self._finish_pending_input_effect(task, receipt.get("state") or "unknown")
            if receipt["state"] == "unknown":
                self._hold_task_motion_unknown(
                    task, f"动作 {capability_id} 回执未确认"
                    + (f"：{receipt['message']}" if receipt.get("message") else "")
                    + "；禁止后续动作、清理或归位")
            else:
                self._save_task_progress(task)
            # R2-04：最后安全收尾（回执持久化/未知阻断）完成后再释放安装锁
            try:
                action_lock.__exit__(None, None, None)
            except Exception:
                pass

    def _installation_action_lock(self, device_id: str):
        """本设备共享安装文件锁（动作侧，非阻塞；R2-04/FIX-05 §4.1）。"""
        import scripts.camera_binding_v2 as camera_v2
        data_root = Path(self.config.root) / "data"
        return camera_v2.installation_lock(data_root, device_id, exclusive=False)

    async def _stop_task_arm(self, task: dict) -> dict:
        """记录驱动停止事实；插件的成功摘要不能覆盖未知结果。"""
        task["stop_attempt_count"] = int(task.get("stop_attempt_count", 0)) + 1
        result = {"ok": False, "stopped": False, "outcome_hint": "unknown",
                  "message": "停止结果尚未确认"}
        task["stop_result"] = result
        self._save_task_progress(task)
        try:
            result = await asyncio.wait_for(
                self.devices.driver(task["device_id"]).stop(),
                timeout=TASK_STOP_TIMEOUT_SECONDS,
            )
            if not isinstance(result, dict):
                result = {"ok": False, "stopped": False, "outcome_hint": "unknown",
                          "message": "驱动未返回有效的停止结果"}
            return result
        except BaseException as exc:
            result = {"ok": False, "stopped": False, "outcome_hint": "unknown",
                      "message": f"停止未确认：{exc or type(exc).__name__}"}
            raise
        finally:
            task["stop_result"] = result
            if (result.get("ok") is not True or result.get("stopped") is not True
                    or result.get("outcome_hint") not in (None, "success")):
                task["stop_result_unknown"] = True
            self._save_task_progress(task)

    def gesture_profile_view(self, device_id: str, profile_id: str) -> dict:
        from drivers import calibration_bundle, gesture_profiles

        device = self.devices.get(device_id)
        root = getattr(self.devices.driver(device_id), "data_root", None) or self.config.root / "data"
        entry = gesture_profiles.load_profile(device_id, profile_id, root)
        if entry is None:
            raise ApiError("gesture_profile_not_found", "手势档案尚未登记")
        bundle = calibration_bundle.load_bundle(device_id, root)
        view = gesture_profiles.view_profile(entry, bundle, device.get("daemon_session"))
        return {
            "contract_version": "gesture-profile.v1", "device_id": device_id,
            **{key: view.get(key) for key in
               ("profile_id", "version", "state", "usable", "blocking_reasons")},
            "owner_plugin": entry.get("owner_plugin"),
            "navigation_limits": {
                "max_swipes": int((view.get("config") or {}).get("max_swipes", 0)),
                "return_to_top": bool((view.get("config") or {}).get("return_to_top", False)),
            },
        }

    def validate_task_preconditions(self, task: dict) -> None:
        """插件负责业务资格；平台只提供当前设备快照与设备数据根。"""
        if screen_wake.claims_screen_wake(task):
            screen_wake.validate_preconditions(self, task)
        plugin, _ = self.plugins.instance_for_task_type(task["task_type"])
        verdict = plugin.validate_preconditions(
            deepcopy(task), deepcopy(self.devices.get(task["device_id"])),
            data_root=self._gesture_data_root(task["device_id"]),
            evidence_root=self.config.evidence_dir, evidence_get=self.evidence.get)
        if not verdict.get("ok"):
            raise ApiError(verdict.get("error_category", "device_not_eligible"),
                           verdict.get("message", "任务实时前置条件不满足，未执行动作"))

    def _check_physical_preconditions(self, plugin_id: str, task: dict) -> None:
        """无 await 的最终门禁；异常标记仅证明本次调用尚未进入驱动。"""
        from drivers.arm import DriverError
        from tasks.plugin_api import PluginViolationError
        from runtime.device_eligibility import compute_device_eligibility
        try:
            stored, _ = self.validate_plugin_context(plugin_id, task)
            if stored.get("cancel_requested") or self.estop.active or stored.get("stop_result_unknown"):
                raise DriverError("driver_stopped", "任务已取消、急停或停止结果未知，禁止继续动作")
            blockers = self.plugin_context_blockers(plugin_id, task["device_id"])
            if blockers:
                raise PluginViolationError("；".join(blockers))
            snapshot = deepcopy(self.devices.get(task["device_id"]))
            if screen_wake.is_screen_wake_task(stored) and snapshot.get("camera_quality") == "waiting":
                snapshot["camera_quality"] = "clear"
            safety = compute_device_eligibility(snapshot)
            # 当前任务已持有租约；busy 只禁止接收另一任务，不豁免任何安全条件。
            blockers = [code for code in safety["blocking_checks"] if code != "device_busy"]
            if blockers:
                raise ApiError("device_not_eligible", "设备实时安全条件已失效：" + "、".join(blockers))
            self.validate_task_preconditions(stored)
        except Exception as exc:
            exc.motion_not_started = True
            raise

    def validate_plugin_context(self, plugin_id: str, task: dict, *,
                                allow_expired: bool = False) -> tuple[dict, dict]:
        """能力调用和状态写入共用原任务、原设备和租约检查。"""
        from tasks.plugin_api import PluginViolationError

        device_id = task["device_id"]
        stored = self.store.get_task(task["task_id"])
        if (not stored or stored.get("device_id") != device_id
                or stored.get("plugin_id") != plugin_id
                or stored.get("lease_id") != task.get("lease_id")
                or stored.get("status") not in {"awaiting_device", "validating", "executing"}):
            raise PluginViolationError("任务归属或执行状态不匹配，禁止调用设备能力")
        lease = self.leases.get(device_id)
        if (not lease or lease.get("task_id") != stored["task_id"]
                or lease.get("lease_id") != stored.get("lease_id")):
            raise PluginViolationError("任务不持有原设备租约，禁止调用设备能力")
        if not allow_expired:
            expires = parse_iso(lease.get("expires_at"))
            if not expires or expires <= utc_now():
                raise PluginViolationError("任务租约已过期，禁止继续调用设备能力")
        return stored, lease

    async def plugin_invoke_capability(self, plugin_id: str, task: dict, capability_id: str, params: dict) -> dict:
        try:
            return await self._plugin_invoke_capability(plugin_id, task, capability_id, params)
        except Exception as exc:
            stored = self.store.get_task(task["task_id"])
            if (capability_id == screen_wake.CAPABILITY
                    and screen_wake.is_screen_wake_task(task)
                    and not task.get("wake_driver_started")
                    and not (stored or {}).get("wake_driver_started")):
                exc.motion_not_started = True
            raise

    async def _plugin_invoke_capability(self, plugin_id: str, task: dict, capability_id: str, params: dict) -> dict:
        from drivers.arm import DriverError
        from tasks.plugin_api import PluginViolationError

        device_id = task["device_id"]
        # STOP 不受时间/预算上限阻挡，但不能借旧任务停止另一任务的设备。
        stored, lease = self.validate_plugin_context(
            plugin_id, task, allow_expired=capability_id == "arm.stop")
        if (self.devices.get(device_id).get("unknown_hold_task_id")
                and capability_id.startswith(("arm.", "input."))
                and capability_id != "arm.stop"):
            raise DriverError("driver_stopped", "结果未知任务仍在人工门禁中，禁止机械臂或输入动作")
        if (capability_id.startswith(("arm.", "input."))
                and capability_id != "arm.stop"
                and (stored.get("cancel_requested") or self.estop.active
                     or stored.get("stop_result_unknown"))):
            # §4.5：取消/急停优先于输入开关——关闭不是急停，但急停/取消拦一切新动作
            raise DriverError("driver_stopped", "任务已取消、急停或停止结果未知，禁止继续动作")
        if screen_wake.is_screen_wake_task(stored) and capability_id not in {
                screen_wake.CAPABILITY, "arm.stop", "camera.capture"}:
            raise PluginViolationError("人工唤醒任务不得调用解锁、输入、识图或其他动作")
        if capability_id == screen_wake.CAPABILITY and (
                stored.get("wake_driver_started") or task.get("wake_driver_started")
                or stored.get("action_call_count", 0) or task.get("action_call_count", 0)):
            raise PluginViolationError("人工唤醒只允许一次动作申请，禁止自动重试")
        if capability_id != "arm.stop":
            self.leases.renew(device_id, lease["lease_id"])
        record = self.plugins.get(plugin_id)
        if stored.get("read_only_task") and capability_id.startswith(("arm.", "input.")):
            raise PluginViolationError("只读任务禁止调用机械臂或输入写能力")
        if capability_id not in record["manifest"].get("capabilities", []):
            self.audit.record(
                "plugin_violation_blocked",
                task_id=task["task_id"], device_id=device_id,
                reason=f"插件 {plugin_id} 试图调用未声明能力 {capability_id}，已拒绝",
            )
            raise PluginViolationError(f"能力 {capability_id} 未在插件清单中声明")
        if capability_id not in self.devices.get(device_id).get("capabilities", []):
            raise PluginViolationError(f"设备 {device_id} 不支持能力 {capability_id}")
        resources = record["manifest"].get("resources") or {}
        if (resources.get("devices") or {}).get("mode") != "assigned":
            raise PluginViolationError("插件未声明仅使用任务指定设备范围，禁止调用设备能力")
        resource = capability_id.split(".", 1)[0]
        if resource in {"arm", "input"} and capability_id != "arm.stop":
            blockers = self.plugin_context_blockers(plugin_id, device_id)
            if blockers:
                raise PluginViolationError("；".join(blockers))
        if resource in {"arm", "camera", "input"} and resources.get(resource) is not True:
            resource_name = {"arm": "机械臂", "camera": "摄像头", "input": "输入设备"}[resource]
            raise PluginViolationError(f"插件未声明{resource_name}资源，禁止使用该资源")
        layout_capabilities = {
            "camera.identify_ui_state", "camera.identify_account_conversation",
            "camera.observe_regions", "camera.locate_text", "camera.confirm_view",
            "arm.click", "arm.move_target", "arm.double_tap",
        }
        if (capability_id in layout_capabilities
                and not (capability_id == "arm.double_tap" and "point" in params)):
            pinned = self._pin_task_layout(stored, task)
            if capability_id in {"arm.click", "arm.double_tap"}:
                params["_layout_binding"] = deepcopy(pinned)
        task_type = stored.get("task_type")
        entry = next(
            (item for item in record["manifest"]["task_types"]
             if item["task_type"] == task_type), {})
        requires_input = not entry or entry.get("requires_input", False) or task_type in INPUT_TASK_TYPES
        # 按当前任务而非插件资源分类；同一插件的纯导航仍可执行。
        if (resource == "input" or (
                resource == "arm" and requires_input
                and capability_id not in {"arm.stop", "arm.home"})):
            if not self.input_switch_allowed(device_id):
                raise DriverError(
                    "input_switch_disabled",
                    "输入与发送未允许（input_switch_disabled），禁止继续输入或发送")
        # §4.8.4-3/4：受控副作用固定 slot 并持久 prepared；最终认领由
        # 驱动下发边界消费（P05）——串口/网络等待不占任何锁。
        # FIX-15（§11.13.2）：live 候选/删除点击在规划前必须先取得新鲜几何
        # 快照并按实时 View 坐标规划；失败最多再取一次只读快照，仍失败即停止，
        # 绝不落回 ui_targets 旧固定坐标。此门在输入 effect 预置之前执行，
        # 失败=零 prepared 零接触。
        if capability_id == "arm.click" and isinstance(params, dict) \
                and params.get("target_id") in ("ime_candidate_bar", "ime_delete_key"):
            params["target_ref"] = await self._ime_live_geometry_target(
                device_id, str(params["target_id"]))
        effect_kind = self._input_effect_kind(capability_id, params)
        if effect_kind is not None:
            self._prepare_input_effect(stored, task, effect_kind, params)
        if capability_id != "arm.stop" and resource in {"arm", "input"}:
            limit = (record["manifest"].get("limits") or {}).get("max_actions_per_task")
            used = max(
                int(stored.get("action_call_count", stored.get("capability_call_count", 0))),
                int(task.get("action_call_count", task.get("capability_call_count", 0))),
            )
            if limit is not None and used >= limit:
                raise PluginViolationError("任务动作次数达到清单上限，禁止继续执行")
            task["action_call_count"] = used + 1
        task["capability_call_count"] = int(task.get("capability_call_count", 0)) + 1
        self._save_task_progress(task)
        if capability_id != "arm.stop" and capability_id.startswith(("arm.", "input.")):
            if task.get("cancel_requested") or self.estop.active or task.get("stop_result_unknown"):
                raise DriverError("driver_stopped", "任务已请求取消、急停或停止结果未知，禁止继续动作")
        if capability_id not in {"arm.stop", "arm.home"} and resource in {"arm", "input"}:
            self._check_physical_preconditions(plugin_id, task)
        self.audit.record(
            "capability_call",
            operator_id=f"plugin:{plugin_id}",
            task_id=task["task_id"], device_id=device_id,
            reason=f"能力调用：{capability_id}",
        )

        if capability_id == "device.gesture_profile":
            from drivers import gesture_profiles

            root = getattr(self.devices.driver(device_id), "data_root", None) or self.config.root / "data"
            try:
                config = gesture_profiles.resolve_profile(
                    device_id, params["profile_id"], params["version"], owner_plugin=plugin_id,
                    current_session=self.devices.get(device_id).get("daemon_session"), data_root=root)
            except (ValueError, KeyError) as exc:
                raise DriverError("driver_uncalibrated", str(exc)) from exc
            return {"config": config, "profile_id": params["profile_id"], "version": params["version"]}
        if capability_id == "arm.home":
            return await self._run_task_action(
                task, capability_id, self.devices.driver(device_id).go_home)
        if capability_id == "arm.move_preset":
            return await self._run_task_action(
                task, capability_id,
                lambda: self.devices.driver(device_id).move_preset(params["position_id"]))
        if capability_id == "arm.move_target":
            driver = self.devices.driver(device_id)
            options = ({"expected_layout": deepcopy(task["layout_execution_binding"])}
                       if callable(getattr(driver, "attach_layout_provider", None)) else {})
            return await self._run_task_action(
                task, capability_id,
                lambda: driver.move_target(params["target_id"], **options))
        if capability_id == "arm.click":
            return await self._run_task_action(
                task, capability_id, lambda: self.devices.driver(device_id).click(params))
        if capability_id == "arm.double_tap":
            return await self._run_task_action(
                task, capability_id, lambda: self.devices.driver(device_id).double_tap(params))
        if capability_id == screen_wake.CAPABILITY:
            if not screen_wake.is_screen_wake_task(stored) or params != stored["payload"]:
                exc = PluginViolationError("人工唤醒能力只接受已校验任务的原始绑定和确认")
                exc.motion_not_started = True
                raise exc
            wake = getattr(self.devices.driver(device_id), "wake_screen", None)
            if not callable(wake):
                raise ApiError("capability_unavailable", "当前驱动不支持人工唤醒")
            task["wake_driver_started"] = True
            self._save_task_progress(task)
            # W13/§9.3(5)：按任务 ID 取服务端确认记录并在受理边界原子消费一次；
            # 普通任务载荷不携带确认字段，记录只豁免该任务的实时手机姿态。
            manual = screen_wake.consume_manual_confirmation(self, stored)
            if manual is None:
                return await self._run_task_action(
                    task, capability_id, lambda: wake(
                        deepcopy(params),
                        execution_guard=lambda: self._check_physical_preconditions(plugin_id, task)))
            task["wake_manual_confirmation_id"] = manual["confirmation_id"]
            self._save_task_progress(task)
            return await self._run_task_action(
                task, capability_id, lambda: wake(
                    deepcopy(params),
                    execution_guard=lambda: self._check_physical_preconditions(plugin_id, task),
                    manual_confirmation=manual))
        if capability_id == "arm.swipe":
            from runtime.scroll_context import scroll_motion_guard
            guard = scroll_motion_guard(device_id, task["task_id"])
            if task.get("task_type") == "calibrated_scroll" and guard is None:
                error = DriverError("driver_policy_rejected", "校准滑动缺少原采样上下文")
                error.motion_not_started = True
                raise error
            options = {"execution_guard": guard} if guard is not None else {}
            return await self._run_task_action(
                task, capability_id,
                lambda: self.devices.driver(device_id).swipe(params, **options))
        if capability_id == "arm.stop":
            return await self._stop_task_arm(task)
        if capability_id == "camera.capture":
            camera = self.devices.camera_broker(device_id)
            record_evd = self.evidence.capture_screenshot(
                device_id=device_id, camera=camera, task_id=task["task_id"],
                label=params.get("label", "插件截图取证"),
                operator=f"plugin:{plugin_id}",
            )
            task.setdefault("evidence_ids", []).append(record_evd["evidence_id"])
            self._save_task_progress(task)
            return {"evidence_id": record_evd["evidence_id"], "sha256": record_evd["sha256"], "captured_at": record_evd["captured_at"]}
        if capability_id == "camera.confirm_view":
            camera = self.devices.camera(device_id)
            return await camera.confirm_main_view(params.get("expected_ui_state", "wechat_main"))
        if capability_id == "camera.observe_regions":
            observe = getattr(self.devices.camera(device_id), "observe_regions", None)
            if observe is None:
                raise ApiError("capability_unavailable", "当前摄像头不支持登记区域视觉证据")
            observed = await observe(include_image=True)
            image = observed.pop("_image_bytes")
            record = self.evidence.save_file_evidence(
                device_id=device_id, task_id=task["task_id"], data=image,
                label="登记区域视觉证据原图", suffix=".png")
            task.setdefault("evidence_ids", []).append(record["evidence_id"])
            self._save_task_progress(task)
            return {**observed, "evidence_id": record["evidence_id"]}
        if capability_id == "camera.identify_ui_state":
            camera = self.devices.camera(device_id)
            return await camera.identify_ui_state()
        if capability_id == "camera.identify_account_conversation":
            camera = self.devices.camera(device_id)
            return await camera.identify_account_and_conversation()
        if capability_id == "camera.locate_text":
            camera = self.devices.camera(device_id)
            locate = getattr(camera, "locate_text", None)
            if locate is None:
                raise ApiError("capability_unavailable", "当前摄像头驱动不支持 OCR 文字定位")
            kwargs = {"region": params.get("region", "messages")}
            for key in ("min_confidence", "exact", "roi"):
                if key in params:
                    kwargs[key] = params[key]
            return await locate(params["text"], **kwargs)
        if capability_id == "input.ime_create":
            # §4.8.4 受控候选：插件只提供 content/targetUser/typingSpeed；
            # 手机与 taskId 由服务端从 owner/绑定构造，不接受插件指定。
            from adapters.screen_ime.service import get_default_service
            claim = params.pop("input_claim", None) if isinstance(params, dict) else None
            if claim and isinstance(claim.get("callback"), dict) is False:
                claim["callback"]()
            service = get_default_service()
            from runtime.input_control import record_id_for_owner
            owner = self.store.input_record_get(
                record_id_for_owner("fleet_task", task["task_id"]))
            ime_device_id = ((owner or {}).get("ime_device_id")
                             or self.devices.input_binding(device_id).get("ime_device_id")
                             or "unbound")
            task_id = f"{task['task_id']}-ime"
            created, _ = await service.tasks.create({
                "taskId": task_id, "deviceId": ime_device_id,
                "targetUser": params.get("targetUser") or task["task_id"],
                "content": params.get("content"),
                "typingSpeed": params.get("typingSpeed") or "random",
                "source": "api"})
            return {"ok": True, "taskId": created.get("taskId", task_id),
                    "ime_device_id": ime_device_id}
        if capability_id == "input.type_text":
            return await self._run_task_action(
                task, capability_id,
                lambda: self.devices.input_driver(device_id).type_text(
                    params["text"], input_claim=params.get("input_claim")))
        if capability_id in {"device.automatic_gesture_profile", "device.gesture_capture",
                             "arm.execute_gesture_program", "camera.measure_scroll",
                             "camera.measure_probe_path", "arm.test_calibrated_double_tap"}:
            return await self._invoke_gesture_capability(
                plugin_id, task, capability_id, params, record)
        raise ApiError("capability_unknown", capability_id)

    def _gesture_data_root(self, device_id: str) -> Path:
        try:
            driver = self.devices.driver(device_id)
        except KeyError as exc:
            # 未知设备先拒：裸 dict 访问的 KeyError 会以 500 兜底送达
            from runtime.errors import ApiError as _ApiError
            raise _ApiError("device_not_found",
                            f"未知设备：{device_id}") from exc
        root = getattr(driver, "data_root", None)
        return Path(root) if root else self.config.root / "data"

    async def _invoke_gesture_capability(self, plugin_id: str, task: dict,
                                          capability_id: str, params: dict,
                                          record: dict) -> dict:
        """自动手势能力分发（规范第 12.4 节）；不改旧 gesture_motion_verified。"""
        import numpy as np
        from drivers import calibration_bundle as cb
        from drivers import gesture_program as gprog
        from drivers.arm import DriverError
        from runtime import automatic_gestures as ag
        from runtime.errors import ApiError as _ApiError
        from tasks.plugin_api import PluginViolationError

        device_id = task["device_id"]
        data_root = self._gesture_data_root(device_id)

        if capability_id == "device.automatic_gesture_profile":
            op = params.get("op", "read")
            if op == "resolve_navigation":
                return ag.resolve_navigation_action(
                    device_id, params.get("navigation_profile"), params.get("binding"),
                    params.get("action"), bundle=cb.load_bundle(device_id, data_root),
                    frame=gprog.load_coordinate_frame(device_id, data_root), data_root=data_root,
                    evidence_root=self.config.evidence_dir, evidence_get=self.evidence.get)
            if op == "read":
                bundle = cb.load_bundle(device_id, data_root)
                frame = gprog.load_coordinate_frame(device_id, data_root)
                return ag.gestures_view(device_id, bundle=bundle, frame=frame,
                                        data_root=data_root)
            if op == "publish":
                run_result = params.get("run_result")
                if not isinstance(run_result, dict):
                    raise _ApiError("invalid_request", "run_result 必须为对象")
                if plugin_id != "gesture-validation-adapter":
                    raise PluginViolationError("只有自检插件可以发布自动动作证书")
                return ag.publish_run_results(
                    device_id, run_result, data_root=data_root,
                    base_version=params.get("base_version"))
            raise _ApiError("invalid_request", f"未知操作：{op}")

        if capability_id == "device.gesture_capture":
            if plugin_id != "gesture-validation-adapter":
                raise PluginViolationError("手势采样协调仅自检插件可用")
            from adapters.screen_ime.service import get_default_service
            coordinator = getattr(get_default_service(), "gesture_capture", None)
            if coordinator is None:
                raise _ApiError("capability_unavailable", "手势采样协调器未挂载")
            op = params.get("op")
            if op == "create_session":
                return await coordinator.create_session(
                    device_id, params["ime_device_id"], params["binding"],
                    params.get("actions") or [], params["run_id"],
                    session_id=params.get("session_id"))
            if op == "prepare_trial":
                return await coordinator.prepare_trial(
                    params["session_id"], params["trial_id"], params["command"])
            if op == "wait_ready":
                return await coordinator.wait_ready(
                    params["session_id"], params["trial_id"],
                    int(params.get("timeout_ms", 30000)))
            if op == "mark_executing":
                await coordinator.mark_executing(params["session_id"],
                                                 params["trial_id"])
                return {"ok": True}
            if op == "wait_evidence":
                return await coordinator.wait_evidence(
                    params["session_id"], params["trial_id"],
                    int(params.get("timeout_ms", 8000)))
            if op == "finish_trial":
                return await coordinator.finish_trial(
                    params["session_id"], params["trial_id"], params["state"])
            if op == "finish_session":
                return await coordinator.finish_session(
                    params["session_id"], params.get("outcome", "failed"))
            raise _ApiError("invalid_request", f"未知采样操作：{op}")

        if capability_id == "arm.execute_gesture_program":
            # 开关已删（2026-09-30 拍板）：执行资格由校准门禁与物理前置检查
            # 承担（_check_physical_preconditions + plan_program 绑定校验）。
            plan = params.get("plan")
            if not isinstance(plan, dict):
                bundle = cb.load_bundle(device_id, data_root)
                frame = gprog.load_coordinate_frame(device_id, data_root)
                driver = self.devices.driver(device_id)
                features = []
                read_state = getattr(driver, "read_state", None)
                if read_state is not None:
                    try:
                        state = await read_state()
                        features = state.get("motion_features") or []
                    except Exception:
                        features = []
                try:
                    plan = gprog.plan_program(
                        params["program"], bundle, frame,
                        limits={"path_feature_available": "path.v1" in features})
                except Exception as exc:
                    exc.motion_not_started = True
                    raise
            # 22.7：多 stroke 必须在第一次动作前额外检查剩余预算并逐 stroke 扣减
            strokes = max(1, len(plan.get("strokes") or []))
            limit = (record["manifest"].get("limits") or {}).get(
                "max_actions_per_task")
            if limit is not None and strokes > 1:
                current = int(task.get("action_call_count", 0))
                if current + strokes - 1 > int(limit):
                    error = PluginViolationError(
                        "多段路径将超出任务动作上限，禁止开始执行")
                    error.motion_not_started = True
                    raise error
                task["action_call_count"] = current + strokes - 1
                self._save_task_progress(task)
            driver = self.devices.driver(device_id)
            # read_state/规划可能让出事件循环；必须在真正 driver 调用前重新读取资格。
            self._check_physical_preconditions(plugin_id, task)
            # swipe 模式或裸 swipe 参数（有 start/end 无 strokes）→ 走 driver.swipe()
            is_swipe_mode = (plan.get("execution_mode") == "swipe"
                             or ("start" in plan and "end" in plan
                                 and "strokes" not in plan))
            if is_swipe_mode:
                if "strokes" in plan:  # 完整 plan → 提取 swipe 参数
                    stroke = plan["strokes"][0]
                    # 编译结果 screen_points 是像素；原程序才是屏幕归一化点。
                    points = plan["program"]["strokes"][0]["points"]
                    gesture = {
                        "start": {"x": points[0]["x"],
                                  "y": points[0]["y"],
                                  "source": "screen_normalized"},
                        "end": {"x": points[1]["x"],
                                "y": points[1]["y"],
                                "source": "screen_normalized"},
                        "binding": plan["binding"],
                        "durationMs": stroke["duration_ms"]}
                    return await self._run_task_action(
                        task, capability_id, lambda: driver.swipe(gesture))
                return await self._run_task_action(
                    task, capability_id, lambda: driver.swipe(plan))
            execute = getattr(driver, "execute_gesture_program", None)
            if execute is None:
                error = DriverError("driver_policy_rejected",
                                    "当前驱动不支持连续路径（path.v1 未加载）")
                error.motion_not_started = True
                raise error
            return await self._run_task_action(task, capability_id, lambda: execute(plan))

        if capability_id == "camera.measure_scroll":
            from runtime.scroll_context import ScrollContext, unknown_measurement
            from runtime.phone_region_context import RegionContextError, context_pointer

            op = params.get("op", "measure")
            if op == "grab":
                pinned = ScrollContext(self, device_id)
                camera = self.devices.camera_broker(device_id)
                frame = await camera.capture()
                pinned.ensure_current()
                pinned.validate_frame(frame)
                record_evd = self.evidence.archive_camera_frame(
                    frame, pinned.archive_context(),
                    device_id=device_id, camera_id=camera.camera_id, task_id=task["task_id"],
                    label=params.get("label", "位移测量帧"),
                    operator=f"plugin:{plugin_id}")
                task.setdefault("evidence_ids", []).append(
                    record_evd["evidence_id"])
                self._save_task_progress(task)
                return {"evidence_id": record_evd["evidence_id"],
                        "sha256": record_evd["sha256"],
                        "captured_at": record_evd["captured_at"],
                        "search_region_pointer": context_pointer(pinned.region)}
            import cv2
            from drivers import scroll_observation as so
            from drivers.screen_edge import ScreenEdgeError, locate_screen_quad
            from drivers.screen_pose import align_screen, fit_pose
            evidence_ids = list(params.get("evidence_ids") or [])
            if len(evidence_ids) < 2:
                evidence_ids = [params.get("before_evidence_id"),
                                params.get("after_evidence_id")]
            evidence_records = [self.evidence.get(item) for item in evidence_ids]
            if not evidence_records or any(item is None for item in evidence_records):
                raise _ApiError("invalid_request", "位移测量需要至少两帧证据")
            # Remove phone motion in each evidence frame before mapping content
            # to the calibrated screen plane. Surroundings never enter the flow.
            try:
                pinned = ScrollContext(self, device_id)
                if params.get("search_region_pointer") != context_pointer(pinned.region):
                    raise RegionContextError("region_context_changed", "原帧链粗框指针缺失或变化")
                source_keys = (
                    "device_unique_id", "camera_binding_id", "source_geometry_id",
                    "binding_revision", "source_generation_id", "source_clock_id",
                    "broker_generation_id",
                )
                first_source = {k: (evidence_records[0].get("sampling") or {}).get(k)
                                for k in source_keys}
                first_action = evidence_records[0].get("action_instance_id")
                previous_seq = -1
                for record in evidence_records:
                    seq = pinned.validate_record(record, task["task_id"])
                    if seq <= previous_seq:
                        raise RegionContextError("frame_chain_invalid", "位移证据帧序重复或倒退")
                    previous_seq = seq
                    if (record.get("action_instance_id") != first_action
                            or {k: (record.get("sampling") or {}).get(k) for k in source_keys}
                            != first_source):
                        raise RegionContextError("frame_chain_invalid", "位移证据混入其他动作或来源")
            except RegionContextError as exc:
                return unknown_measurement(exc.code, len(evidence_records))
            bundle = pinned.bundle
            camera_model = ((bundle or {}).get("camera") or {}).get(
                "cameraToScreen") or {}
            matrix_raw = camera_model.get("homography")
            screen_model = (bundle or {}).get("screen") or {}
            if (bundle is None or not matrix_raw
                    or not screen_model.get("screenWidth")
                    or not screen_model.get("screenHeight")):
                raise _ApiError("calibration_invalid",
                                "缺少校准单应或屏幕尺寸，无法矫正到位移测量平面")
            matrix = np.asarray(matrix_raw, dtype=float).reshape(3, 3)
            size = (int(screen_model["screenWidth"]),
                    int(screen_model["screenHeight"]))
            saved_corners = (screen_model.get("edgeModel") or {}).get("corners")
            from runtime.phone_region_context import locate_quad_in_region
            measure_region = (pinned.region or {}).get("region")
            planes = []
            for record in evidence_records:
                try:
                    raw = self.evidence.read_bytes(record)
                    if sha256_bytes(raw) != record.get("sha256"):
                        raise ValueError("位移证据原字节与归档摘要不符")
                    decoded = cv2.imdecode(
                        np.frombuffer(raw, np.uint8),
                        cv2.IMREAD_COLOR)
                    if (decoded is None or not saved_corners
                            or decoded.shape[1] != bundle["frame"]["width"]
                            or decoded.shape[0] != bundle["frame"]["height"]):
                        raise ValueError("位移证据缺少有效的校准手机画面")
                    if measure_region is not None:
                        live = locate_quad_in_region(
                            decoded, measure_region, saved_corners)
                    else:
                        live = locate_screen_quad(
                            decoded, saved_corners, tolerance_px=32,
                            require_fittable=True)
                    pose = fit_pose(saved_corners, live["corners"])
                    aligned = align_screen(decoded, saved_corners, pose)
                    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                except RegionContextError as exc:
                    return unknown_measurement(exc.code, len(evidence_records))
                except (ScreenEdgeError, ValueError, TypeError, KeyError, OSError, cv2.error):
                    return {
                        "status": "unknown", "dy_px": None, "dx_px": None,
                        "inlier_ratio": 0.0, "valid_points": None,
                        "matched_ocr_rows": None, "mad_dy_px": None,
                        "reason_code": "screen_pose_unavailable",
                        "gesture_rules_version": so.default_params()["gesture_rules_version"],
                        "chain_frames": len(evidence_records),
                    }
                planes.append(cv2.warpPerspective(gray, matrix, size))

            def _point_to_screen(px: float, py: float) -> tuple[float, float]:
                vec = matrix @ np.array([float(px), float(py), 1.0])
                return float(vec[0] / vec[2]), float(vec[1] / vec[2])

            def _rect_to_screen(rect: dict) -> dict:
                x, y = float(rect["x"]), float(rect["y"])
                w = float(rect.get("width", rect.get("w", 0)))
                h = float(rect.get("height", rect.get("h", 0)))
                corners = [_point_to_screen(x + dx, y + dy)
                           for dx in (0, w) for dy in (0, h)]
                xs = [c[0] for c in corners]
                ys = [c[1] for c in corners]
                left, top = max(0.0, min(xs)), max(0.0, min(ys))
                return {"x": left, "y": top,
                        "width": min(float(size[0]), max(xs)) - left,
                        "height": min(float(size[1]), max(ys)) - top}

            def _convert_rows(rows):
                # 未提供 OCR 行返回 None：measure_displacement 的 OCR 互证门禁
                # 只在调用方确实提供行时生效（转成 [] 会把缺省当"0 行互证"误拒）
                if rows is None:
                    return None
                converted = []
                for row in rows or []:
                    sx, sy = _point_to_screen(row.get("x", 0), row.get("y", 0))
                    converted.append({"text": row.get("text"), "x": sx, "y": sy})
                return converted

            # ROI 坐标域（默认 camera 相机帧像素，经 H 矫正）：
            # - screen：已是校准屏幕平面像素，直接使用；
            # - screen_norm：校准屏幕平面归一化（手势程序坐标域），乘平面尺寸。
            if not isinstance(params.get("roi"), dict):
                raise _ApiError("invalid_request",
                                "measure 需要 roi 矩形对象（x/y/width/height）")
            roi_frame = params.get("roi_frame") or "camera"
            if roi_frame not in ("camera", "screen", "screen_norm"):
                raise _ApiError("invalid_request",
                                f"roi_frame 只允许 camera/screen/screen_norm，"
                                f"收到 {roi_frame!r}")
            if roi_frame == "camera":
                roi_screen = _rect_to_screen(params["roi"])
                exclusions = [_rect_to_screen(item)
                              for item in (params.get("exclusions") or [])]
            elif roi_frame == "screen":
                roi_raw = params["roi"]
                roi_screen = {"x": float(roi_raw["x"]), "y": float(roi_raw["y"]),
                              "width": float(roi_raw["width"]),
                              "height": float(roi_raw["height"])}
                exclusions = [dict(item) for item in (params.get("exclusions") or [])]
            else:
                def _norm_rect(rect: dict) -> dict:
                    return {"x": float(rect["x"]) * size[0],
                            "y": float(rect["y"]) * size[1],
                            "width": float(rect["width"]) * size[0],
                            "height": float(rect["height"]) * size[1]}
                roi_screen = _norm_rect(params["roi"])
                exclusions = [_norm_rect(item)
                              for item in (params.get("exclusions") or [])]

            if len(planes) == 2:
                measured = so.measure_displacement(
                    planes[0], planes[1], roi_screen, exclusions=exclusions,
                    ocr_rows_before=_convert_rows(params.get("ocr_rows_before")),
                    ocr_rows_after=_convert_rows(params.get("ocr_rows_after")))
                try:
                    pinned.ensure_current()
                except RegionContextError as exc:
                    return unknown_measurement(exc.code, len(evidence_records))
                return measured
            # 相邻帧链（§9.2 / 2026-10-03 规格 §6.3）：整页滑动单对跨度超出
            # LK 能力，逐对测量后交共享归并（scroll_observation.
            # aggregate_scroll_chain）分类；任一对失败=整链 unknown，
            # 累计横移路径与首尾 OCR 互证同样在归并内判定。
            pieces = [so.measure_pair_quality(prev_plane, next_plane, roi_screen,
                                              exclusions=exclusions)
                      for prev_plane, next_plane in zip(planes, planes[1:])]
            ocr_shift = so.ocr_vertical_shift(
                _convert_rows(params.get("ocr_rows_before")),
                _convert_rows(params.get("ocr_rows_after")))
            merged = so.aggregate_scroll_chain(
                pieces, roi_screen["width"], roi_screen["height"],
                ocr_shift_px=ocr_shift)
            merged["dy_px"] = None if merged["dy_px"] is None else round(merged["dy_px"], 2)
            merged["dx_px"] = None if merged["dx_px"] is None else round(merged["dx_px"], 2)
            if merged["status"] == "unknown" and merged["reason_code"] is None:
                merged["reason_code"] = "between_thresholds"
            # OCR 行数在链式路径不做逐对配对门禁：matched_ocr_rows 如实报
            # None（未测量），不误报 0（R2 N11）。
            merged["matched_ocr_rows"] = None
            try:
                pinned.ensure_current()
            except RegionContextError as exc:
                return unknown_measurement(exc.code, len(evidence_records))
            return merged

        if capability_id == "camera.measure_probe_path":
            verifier = getattr(self.devices.driver(device_id),
                               "gesture_motion_verifier", None)
            if verifier is None:
                raise _ApiError("capability_unavailable",
                                "当前设备没有探头实体跟踪，不支持边界探测")
            # 只读测量：返回最近一次登记的探头观测证据引用
            recent = getattr(verifier, "last_trace", None)
            if recent is None:
                return {"contract_version": "probe-path-observation.v1",
                        "verified": False,
                        "reason_code": "probe_evidence_missing",
                        "points": [], "frame_seqs": [], "plan_id": None,
                        "binding": None}
            return recent

        if capability_id == "arm.test_calibrated_double_tap":
            if plugin_id != "gesture-validation-adapter":
                raise PluginViolationError("校准双击自检仅自检插件可用")
            allowed = {"run_id", "trial_id", "plan_id", "binding", "point",
                       "interval_ms"}
            if set(params) != allowed:
                raise _ApiError("invalid_request",
                                f"双击自检参数必须且仅有 {sorted(allowed)}")
            driver = self.devices.driver(device_id)
            execute = getattr(driver, "test_calibrated_double_tap", None)
            if execute is None:
                raise DriverError("driver_policy_rejected",
                                  "当前驱动不支持校准双击自检")
            return await self._run_task_action(task, capability_id, lambda: execute(params))
        raise _ApiError("capability_unknown", capability_id)

    async def plugin_capture_evidence(self, plugin_id: str, task: dict, label: str) -> dict | None:
        result = await self.plugin_invoke_capability(
            plugin_id, task, "camera.capture", {"label": label},
        )
        return self.evidence.get(result["evidence_id"])
