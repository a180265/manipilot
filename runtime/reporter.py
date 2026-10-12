"""WX 回报器：任务结果、可见消息观测和急停通知，按版本化契约回报 WX。

- 任务结果符合 task-result.v1；已发送/明确失败/结果未知三类可对账。
- 通用日志不写客户原文；正文只以受控引用与哈希流转。
- 未配置回调地址或回调失败时记录审计，不阻塞本地任务记录。
"""
from __future__ import annotations

import os

import httpx

from runtime.config import task_callback_base_url
from runtime.util import iso, utc_now


class Reporter:
    def __init__(self, config, audit, evidence_store=None, callback_outbox=None):
        self._config = config
        self._audit = audit
        self._evidence = evidence_store
        # §10 回调持久队列：回调失败时入队，由后台任务按退避重试。
        self.callback_outbox = callback_outbox
        self.client: httpx.AsyncClient | None = None  # 可注入（联调用 ASGI transport）

    def _base_url(self) -> str:
        return task_callback_base_url(self._config)

    def _record_failure(self, *, kind: str, path: str, payload: dict,
                        callback_id: str, outcome: str, reason: str) -> None:
        if self.callback_outbox is None:
            return
        self.callback_outbox.record_failure(
            kind=kind, path=path, payload=payload, callback_id=callback_id,
            reason=reason, outcome=outcome)

    async def _post(self, path: str, payload: dict, *, action: str, task_id: str | None = None,
                    callback_id: str | None = None) -> bool:
        base = self._base_url()
        if not base:
            self._audit.record(action, task_id=task_id, outcome="failed", reason="未配置 WX 回调地址，回报挂起")
            self._record_failure(kind="task_result", path=path, payload=payload,
                                 callback_id=callback_id or f"{action}:{task_id or path}",
                                 outcome="no_callback_url", reason="未配置 WX 回调地址")
            return False
        client = self.client or httpx.AsyncClient(timeout=5)
        try:
            response = await client.post(base.rstrip("/") + path, json=payload)
            # §问题10：HTTP 2xx 只表示“收到请求”，不单独表示业务成功。
            # 必须收到 WX 结构化确认（accepted=true）才算成功；空对象、缺确认
            # 字段或无法解析时进入重试，不得标记成功。
            # §7.3（P3-3）：2xx 且 accepted=false 是 WX 业务拒绝（参数面问题），
            # 与 customer 通道对称进入人工处理（business_rejected），不自动重试。
            confirmed = False
            business_rejected = False
            reject_reason = ""
            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                confirmed = isinstance(body, dict) and body.get("accepted") is True
                business_rejected = isinstance(body, dict) and body.get("accepted") is False
                if business_rejected:
                    reject_reason = str(
                        body.get("reason") or body.get("error_display")
                        or body.get("message") or "")
            ok = confirmed
            reason = ("回报成功" if ok
                      else f"WX 业务拒绝该回报（{reject_reason}），需人工处理" if business_rejected
                      else f"回报未确认：HTTP {response.status_code}"
                      if 200 <= response.status_code < 300
                      else f"回报失败：HTTP {response.status_code}")
            self._audit.record(action, task_id=task_id,
                               outcome="success" if ok else "failed", reason=reason)
            if not ok and self.callback_outbox is not None and callback_id:
                if business_rejected:
                    outcome = "business_rejected"
                elif 200 <= response.status_code < 300:
                    outcome = "unconfirmed_2xx"
                elif response.status_code < 500:
                    outcome = "rejected_4xx"
                else:
                    outcome = "server_error"
                self._record_failure(
                    kind="task_result", path=path, payload=payload, callback_id=callback_id,
                    outcome=outcome, reason=reason)
            return ok
        except Exception as exc:
            self._audit.record(action, task_id=task_id, outcome="failed", reason=f"回报异常：{exc}")
            self._record_failure(kind="task_result", path=path, payload=payload,
                                 callback_id=callback_id or f"{action}:{task_id or path}",
                                 outcome="network_error", reason=f"回报异常：{exc}")
            return False
        finally:
            if self.client is None:
                await client.aclose()

    async def _post_customer_message(self, path: str, payload: dict, *, task_id: str | None = None) -> dict:
        """回报消息时保留 WX 的业务门禁结果，不把 HTTP 200 当成业务成功。"""
        base = self._base_url()
        message_id = str(payload.get("external_message_id") or task_id or "")
        callback_id = f"customer-message:{message_id}"
        if not base:
            reason = "未配置 WX 回调地址，消息回报挂起"
            self._audit.record("customer_message_reported", task_id=task_id, outcome="failed", reason=reason)
            self._record_failure(kind="customer_message", path=path, payload=payload,
                                 callback_id=callback_id, outcome="no_callback_url", reason=reason)
            return {"accepted": False, "retryable": True, "reason": reason}

        client = self.client or httpx.AsyncClient(timeout=5)
        try:
            response = await client.post(base.rstrip("/") + path, json=payload)
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}

            reason = str(body.get("reason") or body.get("error_display") or body.get("message") or "")
            if response.status_code >= 500:
                result = {"accepted": False, "retryable": True,
                          "reason": reason or f"WX 回报失败：HTTP {response.status_code}"}
                outcome = "server_error"
            elif response.status_code >= 400:
                result = {"accepted": False, "rejected": True, "retryable": False,
                          "reason": reason or f"WX 拒绝消息回报：HTTP {response.status_code}"}
                outcome = "rejected_4xx"
            elif body.get("accepted") is False:
                # WX 业务拒绝通常仍返回 200，例如进入托管前需要人工核对。
                result = {**body, "accepted": False, "rejected": True,
                          "retryable": False, "reason": reason or "WX 尚未允许该消息进入业务流程"}
                outcome = "business_rejected"
            elif body.get("accepted") is True:
                result = {**body, "accepted": True, "retryable": False,
                          "reason": reason or "WX 已接收客户消息"}
                outcome = None
            else:
                # §问题10：2xx 但返回空对象、缺少 accepted 确认字段或格式无法
                # 解析（body 已归一为 {}）——只能进入重试，不得标记成功。
                result = {"accepted": False, "retryable": True,
                          "reason": "WX 返回 2xx 但缺少结构化确认（accepted=true），等待重试"}
                outcome = "server_error"
            if outcome is not None and self.callback_outbox is not None:
                # 业务拒绝（如托管未运行）与 4xx 是参数面问题，进入人工处理；
                # 5xx/网络异常进入退避重试。仅“已接收”或可重试的挂起不入队。
                self._record_failure(kind="customer_message", path=path, payload=payload,
                                     callback_id=callback_id, outcome=outcome,
                                     reason=result["reason"])
            self._audit.record(
                "customer_message_reported", task_id=task_id,
                outcome="success" if result["accepted"] else "failed",
                reason=result["reason"],
            )
            return result
        except Exception as exc:
            reason = f"消息回报异常：{exc}"
            self._audit.record("customer_message_reported", task_id=task_id, outcome="failed", reason=reason)
            self._record_failure(kind="customer_message", path=path, payload=payload,
                                 callback_id=callback_id, outcome="network_error", reason=reason)
            return {"accepted": False, "retryable": True, "reason": reason}
        finally:
            if self.client is None:
                await client.aclose()

    async def send_task_result(self, task: dict) -> bool:
        """按 task-result.v1 回报三类结果（已发送 / 明确失败 / 结果未知）。"""
        status = task["status"]
        if status not in ("confirmed", "failed", "unknown"):
            return False
        payload: dict = {
            "task_id": task["task_id"],
            "status": status,
            "status_display": task.get("status_display", ""),
            "reported_at": iso(utc_now()),
            "contract_version": "task-result.v1",
        }
        evidence_hash = self._last_evidence_hash(task)
        if status == "confirmed":
            payload["provider_message_id"] = task.get("provider_message_id", "")
            payload["sent_at"] = task.get("sent_at", iso(utc_now()))
            payload["evidence_hash"] = evidence_hash
            payload["home_check"] = task.get("home_check", {"home_ok": True})
        elif status == "failed":
            payload["error_category"] = task.get("error_category", "driver_error")
            payload["retryable"] = False  # 平台不自行重试，由业务方决定
            payload["evidence_hash"] = evidence_hash
        else:
            payload["uncertainty_reason"] = task.get("uncertainty_reason", "动作结果无法确认")
            payload["evidence_hash"] = evidence_hash
        effects = [(entry.get("detail") or {}).get("effect_confirmation")
                   for entry in task.get("step_log", [])
                   if entry.get("phase") == "done" and entry.get("outcome") == "success"]
        effect = next((item for item in reversed(effects)
                       if isinstance(item, dict) and item.get("confirmed") is True
                       and item.get("provider_message_id") and item.get("sent_at")
                       and item.get("evidence_id")), None)
        if effect:
            payload.update(provider_message_id=effect["provider_message_id"], sent_at=effect["sent_at"])
            payload["home_check"] = {**task.get("home_check", {}),
                                     "send_confirmation": effect,
                                     "task_status": status}
            if status != "confirmed":
                payload.update(status="unknown", status_display="发送已确认，收尾待核对",
                               uncertainty_reason="发送效果已由证据确认，但任务收尾未完成；禁止重新发送")
        sent = await self._post(
            str(self._config.get("wx_callback_results_path")), payload,
            action="task_result_reported", task_id=task["task_id"],
            callback_id=f"task-result:{task['task_id']}:{status}",
        )
        task["reported_to_wx"] = sent
        task["reported_at"] = payload["reported_at"]
        if sent and self.callback_outbox is not None:
            self.callback_outbox.mark_succeeded(f"task-result:{task['task_id']}:{status}")
        return sent

    async def send_customer_message(self, message: dict, task_id: str | None = None) -> dict:
        """按 customer-message.v1 回报可见消息观测。"""
        result = await self._post_customer_message(
            str(self._config.get("wx_callback_messages_path")), message,
            task_id=task_id,
        )
        if result.get("accepted") and self.callback_outbox is not None:
            message_id = str(message.get("external_message_id") or task_id or "")
            self.callback_outbox.mark_succeeded(f"customer-message:{message_id}")
        return result

    def build_emergency_stop_payload(self, reason: str, *, occurred_at: str | None = None) -> dict:
        """急停回报负载（唯一构造点）：先持久化与直接投递必须使用同一 payload。"""
        return {
            "event": "emergency_stop",
            "event_display": "全局急停",
            "reason": reason,
            "occurred_at": occurred_at or iso(utc_now()),
            "request": "请暂停 Outbox 和跟进调度",
        }

    def persist_emergency_stop_callback(self, reason: str) -> dict | None:
        """§7.6（P3-6）：急停回报先持久化再投递。

        向 callback_outbox 落一条待投递的 emergency-stop 回调
        （callback_id=emergency-stop:{occurred_at}，payload 与 send_emergency_stop
        完全一致），返回该 payload 供随后投递复用；未配置回调队列时返回
        None，由调用方保持 fire-and-forget 降级。事件循环立即退出时，该行
        由后台 flush 兜底重投。
        """
        if self.callback_outbox is None:
            return None
        payload = self.build_emergency_stop_payload(reason)
        self.callback_outbox.record_pending(
            kind="emergency_stop",
            path=str(self._config.get("wx_callback_estop_path")),
            payload=payload,
            callback_id=f"emergency-stop:{payload['occurred_at']}",
            reason="急停回报已先落本地回调队列，等待投递",
        )
        return payload

    async def send_emergency_stop(self, reason: str, *, payload: dict | None = None) -> bool:
        payload = payload if payload is not None else self.build_emergency_stop_payload(reason)
        sent = await self._post(str(self._config.get("wx_callback_estop_path")), payload,
                                action="task_result_reported",
                                callback_id=f"emergency-stop:{payload['occurred_at']}")
        if sent and self.callback_outbox is not None:
            self.callback_outbox.mark_succeeded(f"emergency-stop:{payload['occurred_at']}")
        return sent

    def _last_evidence_hash(self, task: dict) -> str:
        if not self._evidence:
            return ""
        for evidence_id in reversed(task.get("evidence_ids", [])):
            record = self._evidence.get(evidence_id)
            if record:
                return record["sha256"]
        return ""
