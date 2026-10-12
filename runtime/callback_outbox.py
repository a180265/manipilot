"""§10 回调可靠投递：Arm Fleet 客户消息与任务结果回调的持久化队列。

- 回调失败写入本地 callback_outbox 持久队列，按指数退避重试。
- 同一事件使用稳定幂等键（callback_id）。
- WX 必须返回结构化确认（2xx 且 accepted=true）才标记成功；返回空对象、
  缺少确认字段或格式无法解析时进入重试，4xx 参数错误进入人工处理。
- 5xx、超时和网络错误进入重试队列；超过最大次数最终放弃（abandoned）。
- 提供待投递、失败、重试和最终放弃查询接口。
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx

from runtime.util import iso, utc_now

BACKOFF_SECONDS = (1, 5, 30, 300)
MAX_ATTEMPTS = 6
STATUS_DISPLAY = {
    "pending": "待投递",
    "retry": "等待重试",
    "succeeded": "已投递",
    "manual": "参数错误待人工处理",
    "abandoned": "最终放弃",
}


def _shift(now, seconds: int) -> str:
    return iso(now + timedelta(seconds=seconds))


class CallbackOutbox:
    """持久化回调队列：失败入队、退避重试、人工与放弃状态可查询。"""

    def __init__(self, store, audit, *, max_attempts: int = MAX_ATTEMPTS,
                 backoff_seconds: tuple[int, ...] = BACKOFF_SECONDS):
        self._store = store
        self._audit = audit
        self.max_attempts = int(max_attempts)
        self.backoff_seconds = tuple(backoff_seconds) or BACKOFF_SECONDS

    # ---------------- 入队 ----------------
    def record_pending(self, *, kind: str, path: str, payload: dict,
                       callback_id: str, reason: str = "待投递", occurred_at=None) -> dict:
        """§7.6（P3-6）：先持久化再投递的初始入队（status=pending）。

        与 record_failure 不同：不计失败次数、不退避，落库后即可由 flush
        领取；供“先落本地队列、再投递”的回报（如急停）使用，事件循环立即
        退出时由后台 flush 兜底重投。同一 callback_id 已存在时幂等返回原行。
        """
        now = occurred_at or iso(utc_now())
        existing = self._store.get_callback(callback_id)
        if existing is not None:
            return existing
        item = {
            "callback_id": callback_id, "kind": kind, "path": path,
            "payload": payload, "status": "pending",
            "status_display": STATUS_DISPLAY["pending"],
            "attempt_count": 0, "next_attempt_at": now,
            "last_reason": reason, "created_at": now, "updated_at": now,
        }
        self._store.save_callback(item)
        return item

    def record_failure(self, *, kind: str, path: str, payload: dict,
                       callback_id: str, reason: str, outcome: str,
                       occurred_at=None) -> dict:
        """按回报结果决定入队方式。

        outcome:
        - rejected_4xx / business_rejected → manual（参数错误，不重试）
        - server_error / network_error / no_callback_url → retry（退避重试）
        """
        now = occurred_at or iso(utc_now())
        attempts = 0
        existing = self._store.get_callback(callback_id)
        if existing and existing.get("status") in {"pending", "retry", "manual"}:
            attempts = int(existing.get("attempt_count") or 0)
        if outcome in {"rejected_4xx", "business_rejected"}:
            status, next_at = "manual", None
        else:
            status = "retry"
            wait = self.backoff_seconds[min(attempts, len(self.backoff_seconds) - 1)]
            next_at = _shift(utc_now(), wait)
        item = {
            "callback_id": callback_id, "kind": kind, "path": path,
            "payload": payload, "status": status,
            "status_display": STATUS_DISPLAY[status],
            "attempt_count": attempts + 1, "next_attempt_at": next_at,
            "last_reason": reason, "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
        }
        self._store.save_callback(item)
        return item

    def mark_succeeded(self, callback_id: str, *, occurred_at=None) -> dict | None:
        item = self._store.get_callback(callback_id)
        if item is None:
            return None
        item["status"] = "succeeded"
        item["status_display"] = STATUS_DISPLAY["succeeded"]
        item["updated_at"] = occurred_at or iso(utc_now())
        item["last_reason"] = ""
        self._store.save_callback(item)
        return item

    # ---------------- 重试 ----------------
    async def flush(self, base_url: str, *, client: httpx.AsyncClient | None = None,
                    limit: int = 20) -> dict:
        """投递到期的回调；返回本轮处理摘要。"""
        summary = {"delivered": 0, "manual": 0, "rescheduled": 0, "abandoned": 0, "skipped": 0}
        due = self._store.callbacks_due(iso(utc_now()))
        if not base_url:
            summary["skipped"] = len(due)
            return summary
        owns_client = client is None
        http = client or httpx.AsyncClient(timeout=5)
        try:
            for item in due[:max(1, int(limit))]:
                await self._deliver(http, base_url.rstrip("/"), item, summary)
        finally:
            if owns_client:
                await http.aclose()
        return summary

    async def _deliver(self, http: httpx.AsyncClient, base_url: str, item: dict, summary: dict) -> None:
        callback_id = item["callback_id"]
        try:
            response = await http.post(base_url + item["path"], json=item.get("payload") or {})
            status_code = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = None
        except Exception as exc:  # noqa: BLE001 - 网络异常按重试处理
            if self._reschedule_or_abandon(item, f"网络异常：{exc}"):
                summary["abandoned"] += 1
            else:
                summary["rescheduled"] += 1
            return
        if 200 <= status_code < 300:
            # §问题10：HTTP 2xx 只表示“收到请求”；必须解析到 业务 结构化确认
            # accepted=true 才算成功。空 body、空对象或缺确认字段一律重试。
            accepted = isinstance(body, dict) and body.get("accepted") is True
            if accepted:
                self.mark_succeeded(callback_id)
                summary["delivered"] += 1
                return
            if isinstance(body, dict) and body.get("accepted") is False:
                # 业务拒绝（200 + accepted=false，例如托管未运行）：参数面
                # 问题，与 4xx 同类，进入人工处理，不无限重试。
                item["status"] = "manual"
                item["status_display"] = STATUS_DISPLAY["manual"]
                item["last_reason"] = "WX 业务拒绝该回调，需人工核对"
                item["updated_at"] = iso(utc_now())
                self._store.save_callback(item)
                summary["manual"] += 1
                return
            if self._reschedule_or_abandon(
                    item, "WX 返回 2xx 但缺少结构化确认（accepted=true），等待重试"):
                summary["abandoned"] += 1
            else:
                summary["rescheduled"] += 1
            return
        if 400 <= status_code < 500:
            item["status"] = "manual"
            item["status_display"] = STATUS_DISPLAY["manual"]
            item["last_reason"] = f"WX 拒绝回调：HTTP {status_code}，需人工处理"
            item["updated_at"] = iso(utc_now())
            self._store.save_callback(item)
            summary["manual"] += 1
            return
        if self._reschedule_or_abandon(item, f"WX 服务错误：HTTP {status_code}"):
            summary["abandoned"] += 1
        else:
            summary["rescheduled"] += 1

    def _reschedule_or_abandon(self, item: dict, reason: str) -> bool:
        """重试或最终放弃；返回是否已转入终态放弃（供摘要正确计数）。"""
        attempts = int(item.get("attempt_count") or 0) + 1
        item["attempt_count"] = attempts
        item["last_reason"] = reason
        item["updated_at"] = iso(utc_now())
        if attempts >= self.max_attempts:
            item["status"] = "abandoned"
            item["status_display"] = STATUS_DISPLAY["abandoned"]
            item["next_attempt_at"] = None
        else:
            wait = self.backoff_seconds[min(attempts - 1, len(self.backoff_seconds) - 1)]
            item["status"] = "retry"
            item["status_display"] = STATUS_DISPLAY["retry"]
            item["next_attempt_at"] = _shift(utc_now(), wait)
        self._store.save_callback(item)
        return item["status"] == "abandoned"

    # ---------------- 查询 ----------------
    def list_callbacks(self, *, status: str | None = None) -> list[dict]:
        items = self._store.all_callbacks()
        if status:
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: item.get("updated_at") or "", reverse=True)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for item in self._store.all_callbacks():
            key = str(item.get("status") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return {
            "counts": counts,
            "counts_display": {STATUS_DISPLAY.get(k, k): v for k, v in counts.items()},
            "待投递": counts.get("pending", 0) + counts.get("retry", 0),
            "人工处理": counts.get("manual", 0),
            "已投递": counts.get("succeeded", 0),
            "最终放弃": counts.get("abandoned", 0),
        }
