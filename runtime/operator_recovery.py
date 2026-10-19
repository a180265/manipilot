"""人工通信核查及独立停车；未知结果持久化，永不续跑原任务。"""
from __future__ import annotations

import hashlib
import json
import math
import threading

from drivers import target_archive
from runtime.errors import ApiError
from runtime.leases import TERMINAL_TASK_STATUSES
from runtime.permissions import check_operation
from runtime.util import iso, utc_now

CONTRACT = "operator-recovery.v1"
CONFIRMATIONS = ("probe_clear", "setup_unchanged", "path_clear", "arm_stopped")


class OperatorRecovery:
    def __init__(self, runtime):
        self.rt = runtime
        self._locks: dict[str, threading.Lock] = {}
        runtime.store._write("""CREATE TABLE IF NOT EXISTS operator_recovery_operations (
            request_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, json TEXT NOT NULL)""")

    def _get(self, request_id):
        row = self.rt.store._query_one(
            "SELECT json FROM operator_recovery_operations WHERE request_id=?", (request_id,))
        return json.loads(row["json"]) if row else None

    def _save(self, record):
        self.rt.store._write(
            "UPDATE operator_recovery_operations SET json=? WHERE request_id=?",
            (json.dumps(record, ensure_ascii=False), record["client_request_id"]))

    def _records(self, device_id):
        return [json.loads(row["json"]) for row in self.rt.store._query_all(
            "SELECT json FROM operator_recovery_operations WHERE device_id=? ORDER BY rowid DESC",
            (device_id,))]

    def _base_blockers(self, device_id, *, review=False):
        rt = self.rt
        device = rt.devices.get(device_id)
        blocks = []
        def block(code, message):
            blocks.append({"code": code, "message": message})
        if rt.pre_enable_maintenance:
            block("pre_enable_maintenance", "启用前维护中，仅允许只读检查")
        if rt.estop.active:
            block("estop_active", "全局急停尚未复位")
        if device.get("status") != "paused":
            block("device_paused", "请先暂停设备，再核查通信或回停车位")
        tasks = [t for t in rt.store.all_tasks() if t.get("device_id") == device_id]
        if any(t.get("status") not in TERMINAL_TASK_STATUSES or
               (rt._exec_tasks.get(t["task_id"]) is not None and
                not rt._exec_tasks[t["task_id"]].done()) for t in tasks):
            block("device_busy", "设备还有未结束任务或执行尚未退出")
        if any(t.get("status") == "unknown" and not t.get("reviewed_at") for t in tasks):
            block("result_unknown_pending", "请先人工核对原任务，原任务仍保留结果未知")
        lease = rt.leases.get(device_id)
        if lease:
            task = rt.store.get_task(lease.get("task_id"))
            # 仅已人工核对且协程退出的未知任务租约可在新操作前释放。
            if not (task and task.get("device_id") == device_id and
                    task.get("status") == "unknown" and task.get("reviewed_at") and
                    (rt._exec_tasks.get(task["task_id"]) is None or
                     rt._exec_tasks[task["task_id"]].done())):
                block("lease_conflict", "设备仍被其他操作占用")
        if not review and any(r["status"] in {"pending", "unknown"} and not r.get("reviewed_at")
                              for r in self._records(device_id)):
            block("result_unknown_pending", "上一次人工操作结果未知，请先核对该次操作")
        return blocks

    def _park_blockers(self, device_id, state):
        blocks = []
        def block(code, message):
            blocks.append({"code": code, "message": message})
        if state.get("communication_recovery_required"):
            block("state_transition_invalid", "请先核查并恢复通信，再回停车位")
        if state.get("estop_locked"):
            block("estop_active", "设备停止锁尚未解除")
        if state.get("controller_state") != "Idle" or not isinstance(state.get("position"), dict):
            block("home_not_confirmed", "尚未读到控制器静止状态及当前位置")
        arm = self.rt.devices.driver(device_id)
        try:
            calibration = target_archive.load_runtime_calibration(device_id, arm.data_root) or {}
            park = calibration.get("safePark") or {}
            session = calibration.get("daemonSession", calibration.get("session"))
            valid = (self.rt.devices.get(device_id).get("calibration_status") == "validated"
                     and session and session == state.get("session")
                     and park.get("confirmed") is True and park.get("daemonSession") == session
                     and all(type(park.get(k)) in (int, float) and math.isfinite(park[k])
                             for k in ("parkX", "parkY", "parkZ")))
        except (AttributeError, TypeError, ValueError, OSError):
            valid = False
        if not valid:
            block("calibration_invalid", "当前会话没有有效校准及已确认安全停车位")
        return blocks

    async def view(self, device_id, *, client_request_id=None):
        self.rt.devices.get(device_id)
        arm = self.rt.devices.driver(device_id)
        try:
            state = await arm.recovery_state()
        except Exception as exc:
            state = {"connected": False, "error": str(exc)}
        common = self._base_blockers(device_id, review=True)
        if not state.get("connected") or not state.get("session"):
            common.append({"code": "device_offline", "message": "控制器连接或会话未确认，请检查设备"})
        if state.get("motion_lease"):
            common.append({"code": "lease_conflict", "message": "控制器仍有运动租约"})
        recovery = list(common)
        if not state.get("communication_recovery_required"):
            recovery.append({"code": "state_transition_invalid", "message": "当前没有待人工核查的通信超时"})
        if state.get("estop_latched"):
            recovery.append({"code": "estop_active", "message": "人工急停锁尚未复位"})
        park = list(common) + self._park_blockers(device_id, state)
        if any(r["status"] in {"pending", "unknown"} and not r.get("reviewed_at")
               for r in self._records(device_id)):
            park.append({"code": "result_unknown_pending", "message": "上一次人工操作结果未知，请先核对该次操作"})
        records = self._records(device_id)
        review_ready = (not self._base_blockers(device_id, review=True)
                        and state.get("connected") is True
                        and state.get("controller_state") == "Idle"
                        and bool(state.get("position")) and not state.get("communication_recovery_required")
                        and not state.get("motion_lease")
                        and not self._locks.setdefault(device_id, threading.Lock()).locked())
        pending = []
        for record in records:
            if record["status"] in {"pending", "unknown"} and not record.get("reviewed_at"):
                item = self._public(record)
                item["can_review"] = bool(review_ready and (
                    record["expected_session"] == state.get("session") or not self._park_blockers(device_id, state)))
                pending.append(item)
        return {"contract_version": CONTRACT, "device_id": device_id,
                "session": state.get("session"), "recovery_id": state.get("recovery_id"),
                "communication_recovery_required": bool(state.get("communication_recovery_required")),
                "recovery_reason": state.get("recovery_reason"), "position": state.get("position"),
                "controller_state": state.get("controller_state"),
                "can_recover_communication": not recovery, "can_park": not park,
                "blocking_reasons": common,
                "recover_communication_blocking_reasons": recovery, "park_blocking_reasons": park,
                "last_operation": self._public(records[0]) if records else None,
                "pending_operations": pending,
                "can_review": any(r["can_review"] for r in pending),
                "requested_operation": next((self._public(r) for r in records
                                             if r["client_request_id"] == client_request_id), None),
                "status_display": "设备保持暂停；人工核查不会继续原任务",
                "observed_at": iso(utc_now())}

    @staticmethod
    def _public(record):
        return {k: v for k, v in record.items() if k != "fingerprint"}

    def _validate(self, operation, body, context):
        check_operation("device_" + operation, context)
        if body.get("contract_version") != CONTRACT:
            raise ApiError("unsupported_contract")
        if not isinstance(body.get("expected_session"), str) or not body["expected_session"].strip():
            raise ApiError("missing_field", "必须提交刚刚核查的设备会话 expected_session")
        confirmations = body.get("confirmations")
        if not isinstance(confirmations, dict) or any(confirmations.get(k) is not True for k in CONFIRMATIONS):
            raise ApiError("confirmation_required", "须确认机械臂已静止、探头离屏、未挪动设备及手机、停车路径无遮挡")

    async def execute(self, device_id, operation, body, context):
        self._validate(operation, body, context)
        request_id = context["client_request_id"]
        fingerprint = hashlib.sha256(json.dumps(
            [device_id, operation, body, context], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        lock = self._locks.setdefault(device_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ApiError("device_busy", "设备正在执行人工核查操作，请查询原请求结果")
        lease = None
        try:
            previous = self._get(request_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ApiError("idempotency_conflict")
                return {**self._public(previous), "replayed": True}
            if operation == "operator_recovery_review":
                return await self._review(device_id, body, context, fingerprint)
            view = await self.view(device_id)
            blocks = view["park_blocking_reasons" if operation == "park" else "recover_communication_blocking_reasons"]
            if blocks:
                raise ApiError(blocks[0]["code"], blocks[0]["message"])
            if view["session"] != body["expected_session"]:
                raise ApiError("state_transition_invalid", "设备会话已变化，旧位置及确认失效")
            if operation == "recover_communication" and (not body.get("recovery_id") or
                                                         body["recovery_id"] != view["recovery_id"]):
                raise ApiError("state_transition_invalid", "通信故障编号已变化，请重新核查")
            self._release_reviewed_lease(device_id, context)
            lease = self.rt.leases.acquire(device_id, "operator-recovery:" + request_id, ttl_seconds=300)
            self.rt.supervision.stop(device_id, "人工核查设备，保持停止托管")
            record = self._new_record(device_id, operation, body, context, fingerprint)
            self._claim(record)
            self._set_pending(device_id, True)
            arm = self.rt.devices.driver(device_id)
            binding = self.rt.devices.get(device_id).get("binding_revision")
            def execution_guard():
                device = self.rt.devices.get(device_id)
                current_lease = self.rt.leases.get(device_id)
                if (self.rt.estop.active or device.get("status") != "paused"
                        or device.get("binding_revision") != binding
                        or self.rt.devices.driver(device_id) is not arm
                        or not current_lease or current_lease.get("lease_id") != lease["lease_id"]
                        or self._get(request_id).get("status") != "pending"):
                    raise ApiError("state_transition_invalid", "设备或租约在停车期间变化，停止后续动作")
            issued = False
            try:
                # 查询期间可能收到急停或其他生命周期写入；下发前再核对。
                if self.rt.estop.active or self.rt.devices.get(device_id).get("status") != "paused":
                    raise ApiError("state_transition_invalid", "设备状态在核查期间变化，本次未执行")
                issued = True
                if operation == "recover_communication":
                    receipt = await arm.recover_communication(body["expected_session"],
                                                              context["operator_id"], body["recovery_id"])
                    confirmed = receipt.get("recovered") is True and receipt.get("session") == body["expected_session"]
                else:
                    receipt = await arm.park(expected_session=body["expected_session"],
                                             execution_guard=execution_guard)
                    confirmed = (receipt.get("ok") is True and receipt.get("park") is True
                                 and receipt.get("arrived") is True and receipt.get("tap_executed") is False
                                 and receipt.get("role") == "calibration_safe_park")
                record.update(status="confirmed" if confirmed else "unknown", receipt=receipt,
                              park_confirmed=operation == "park" and confirmed)
            except Exception as exc:
                rejected = not issued or getattr(exc, "motion_not_started", False) is True
                record.update(status="rejected" if rejected else "unknown", receipt={"error": str(exc)})
            record["status_display"] = {"confirmed": "通信已核查，设备仍暂停" if operation == "recover_communication" else "已回到校准安全停车位，设备仍暂停",
                                        "unknown": "本次结果未知，需人工核对，禁止自动重试",
                                        "rejected": "本次操作未执行"}[record["status"]]
            record["finished_at"] = iso(utc_now())
            self._save(record)
            self._set_pending(device_id, record["status"] == "unknown")
            self._audit(record, context)
            return self._public(record)
        finally:
            if lease:
                self.rt.leases.release(device_id, lease["lease_id"])
            lock.release()

    def _new_record(self, device_id, operation, body, context, fingerprint):
        return {"contract_version": CONTRACT, "device_id": device_id, "operation": operation,
                "client_request_id": context["client_request_id"], "expected_session": body["expected_session"],
                "session": body["expected_session"], "fingerprint": fingerprint,
                "status": "pending", "status_display": "操作已登记，结果尚未确认；请勿重复操作",
                "park_confirmed": False, "replayed": False, "created_at": iso(utc_now())}

    def _claim(self, record):
        cur = self.rt.store._write(
            "INSERT OR IGNORE INTO operator_recovery_operations VALUES (?, ?, ?)",
            (record["client_request_id"], record["device_id"], json.dumps(record, ensure_ascii=False)))
        if cur.rowcount != 1:
            raise ApiError("idempotency_conflict", "请求编号已经由另一操作认领")

    def _set_pending(self, device_id, pending):
        device = self.rt.devices.get(device_id)
        device["operator_recovery_pending"] = pending or any(
            r["status"] in {"pending", "unknown"} and not r.get("reviewed_at")
            for r in self._records(device_id))
        self.rt.store.save_device(device)

    def _release_reviewed_lease(self, device_id, context):
        lease = self.rt.leases.get(device_id)
        if lease:
            # _base_blockers 已核对任务终局、reviewed_at 及协程退出。
            self.rt.leases.release(device_id, lease["lease_id"])
            self.rt.audit.record("task_lease_released_after_review", device_id=device_id,
                                 operator_id=context["operator_id"], task_id=lease["task_id"],
                                 reason="人工核查新操作前释放已复核未知任务租约")

    def _audit(self, record, context):
        action = {
            "recover_communication": "device_recover_communication",
            "park": "device_park",
            "operator_recovery_review": "device_operator_recovery_review",
        }[record["operation"]]
        self.rt.audit.record(action, device_id=record["device_id"],
                             operator_id=context["operator_id"], operator_role=context.get("role"),
                             reason=context["reason"], correlation_id=context["correlation_id"],
                             client_request_id=context["client_request_id"],
                             outcome="success" if record["status"] == "confirmed" else "failed",
                             detail=self._public(record))

    async def _review(self, device_id, body, context, fingerprint):
        previous = self._get(body.get("operation_request_id"))
        if not previous or previous["device_id"] != device_id or previous["status"] not in {"pending", "unknown"}:
            raise ApiError("state_transition_invalid", "没有匹配的待核对人工操作")
        blocks = self._base_blockers(device_id, review=True)
        if blocks:
            raise ApiError(blocks[0]["code"], blocks[0]["message"])
        state = await self.rt.devices.driver(device_id).recovery_state()
        if (not state.get("connected") or state.get("session") != body["expected_session"]
                or state.get("controller_state") != "Idle" or not state.get("position")
                or state.get("communication_recovery_required") or state.get("motion_lease")):
            raise ApiError("home_not_confirmed", "须在原会话通信正常、静止且位置可读后人工核对")
        if previous["expected_session"] != body["expected_session"] and self._park_blockers(device_id, state):
            raise ApiError("calibration_invalid", "会话已变化，须完成当前会话校准后才能核对旧操作")
        record = self._new_record(device_id, "operator_recovery_review", body, context, fingerprint)
        self._claim(record)
        previous.update(reviewed_at=iso(utc_now()), reviewed_by=context["operator_id"])
        self._save(previous)
        record.update(status="confirmed", status_display="已人工核对；旧操作结果仍未知，设备仍暂停",
                      reviewed_operation_request_id=previous["client_request_id"], finished_at=iso(utc_now()))
        self._save(record)
        self._set_pending(device_id, False)
        self._audit(record, context)
        return self._public(record)


def service(runtime):
    if not hasattr(runtime, "operator_recovery"):
        runtime.operator_recovery = OperatorRecovery(runtime)
    return runtime.operator_recovery


def pending_operator_recovery(store, device_id):
    """重启/恢复守卫只读持久结果；不创建表、不改变未知结论。"""
    if not store._query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='operator_recovery_operations'"):
        return False
    return any(json.loads(row["json"]).get("status") in {"pending", "unknown"}
               and not json.loads(row["json"]).get("reviewed_at")
               for row in store._query_all(
                   "SELECT json FROM operator_recovery_operations WHERE device_id=?", (device_id,)))
