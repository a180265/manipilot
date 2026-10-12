"""fake WX 服务：联调夹具（外部系统仿真）。

模拟 WX 后端接收 Arm Fleet 的版本化回报，并向 Arm Fleet 提交已批准任务：
- POST /v1/arm-fleet/customer-messages  接收可见消息观测（customer-message.v1）
- POST /v1/arm-fleet/task-results       接收任务结果（task-result.v1，三类可对账）
- POST /v1/arm-fleet/emergency-stop     接收全局急停通知

fake WX 不了解机械臂、坐标、OCR 或摄像头内部实现，只消费契约。
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from jsonschema import validate as schema_validate

from runtime.util import iso, sha256_text, utc_now

CONTRACTS = Path(__file__).resolve().parents[1] / "api" / "contracts"

# 仿真可见消息内容（每次扫描递增，便于验证去重与增量回报）
SIMULATED_MESSAGES = ["你好，请问还在吗？", "好的，麻烦尽快发我。", "收到，谢谢！"]


class FakeWX:
    def __init__(self):
        self.app = FastAPI(title="fake WX 联调服务")
        self.received_messages: list[dict] = []
        self.results_by_task: dict[str, dict] = {}
        self.results_by_outbox: dict[str, dict] = {}
        self.estop_events: list[dict] = []
        self.outbox_sent: dict[str, dict] = {}  # outbox_id -> 提交的任务载荷
        self._schema_messages = json.loads((CONTRACTS / "customer-message.v1.schema.json").read_text(encoding="utf-8"))
        self._schema_results = json.loads((CONTRACTS / "task-result.v1.schema.json").read_text(encoding="utf-8"))
        self._setup_routes()

    def _setup_routes(self):
        @self.app.post("/v1/arm-fleet/customer-messages")
        async def customer_messages(request: Request):
            body = await request.json()
            schema_validate(body, self._schema_messages)
            self.received_messages.append(body)
            return {"accepted": True, "message_id": body.get("external_message_id")}

        @self.app.post("/v1/arm-fleet/task-results")
        async def task_results(request: Request):
            body = await request.json()
            schema_validate(body, self._schema_results)
            self.results_by_task[body["task_id"]] = body
            return {"accepted": True}

        @self.app.post("/v1/arm-fleet/emergency-stop")
        async def emergency_stop(request: Request):
            body = await request.json()
            self.estop_events.append(body)
            return {"accepted": True}

    # ---------- 构造已批准的发送任务（业务 M5 语义） ----------
    def build_chat_send(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        device_id: str,
        wechat_account_id: str,
        external_conversation_id: str,
        text: str,
        outbox_id: str,
        expires_in_seconds: int = 300,
        text_hash: str | None = None,
    ) -> dict:
        payload = {
            "task_id": task_id,
            "task_type": "chat_send",
            "idempotency_key": idempotency_key,
            "device_id": device_id,
            "wechat_account_id": wechat_account_id,
            "external_conversation_id": external_conversation_id,
            "expires_at": iso(utc_now() + timedelta(seconds=expires_in_seconds)),
            "payload": {
                "outbox_id": outbox_id,
                "conversation_id": external_conversation_id,
                "draft_id": f"draft-{outbox_id}",
                "text": text,
                "text_hash": text_hash or sha256_text(text),
                "approved_at": iso(utc_now()),
                "opportunity_bindings": [],
                "fact_snapshot_ids": [],
                "client_request_id": f"wx-req-{outbox_id}",
            },
            "contract_version": "chat-send.v1",
            "correlation_id": f"wx-corr-{outbox_id}",
        }
        self.outbox_sent[outbox_id] = payload
        return payload

    # ---------- 对账 ----------
    def reconcile(self) -> dict:
        """对账：WX 提交的每个 Outbox 任务必须恰好收到一个结果，三类可识别。"""
        confirmed = failed = unknown = 0
        missing: list[str] = []
        for outbox_id, payload in self.outbox_sent.items():
            result = self.results_by_task.get(payload["task_id"])
            if result is None:
                missing.append(outbox_id)
            elif result["status"] == "confirmed":
                confirmed += 1
            elif result["status"] == "failed":
                failed += 1
            elif result["status"] == "unknown":
                unknown += 1
        return {
            "total_outbox": len(self.outbox_sent),
            "total_results": len(self.results_by_task),
            "confirmed_display": "已发送",
            "failed_display": "明确失败",
            "unknown_display": "结果未知",
            "confirmed": confirmed,
            "failed": failed,
            "unknown": unknown,
            "missing_results": missing,
            "reconciled": not missing,
        }
