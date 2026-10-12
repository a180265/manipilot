from __future__ import annotations

import asyncio

import httpx
import pytest

from runtime.reporter import Reporter


class AuditRecorder:
    def __init__(self):
        self.records = []

    def record(self, action, **kwargs):
        self.records.append((action, kwargs))


class _NoopEvents:
    def publish(self, *args, **kwargs):
        pass


def _reporter_with_outbox(tmp_path, config):
    """带真实回调队列的 Reporter：可断言 manual / retry 落库状态。"""
    from runtime.audit import AuditLog
    from runtime.callback_outbox import CallbackOutbox
    from runtime.store import Store

    store = Store(tmp_path / "fleet.db")
    audit = AuditRecorder()
    outbox = CallbackOutbox(store, AuditLog(store, _NoopEvents()))
    reporter = Reporter(config, audit, callback_outbox=outbox)
    return reporter, outbox


TASK_CONFIG = {"wx_callback_base_url": "http://wx.local",
               "wx_callback_results_path": "/v1/arm-fleet/task-results"}
ESTOP_CONFIG = {"wx_callback_base_url": "http://wx.local",
                "wx_callback_estop_path": "/v1/arm-fleet/emergency-stop"}


def test_confirmed_external_effect_is_preserved_when_cleanup_failed():
    import json
    captured = []
    task = {"task_id": "sent-but-park-failed", "status": "failed", "status_display": "明确失败",
            "home_check": {"home_ok": False},
            "step_log": [{"phase": "done", "outcome": "success", "detail": {
                "effect_confirmation": {"confirmed": True, "provider_message_id": "camera-t1",
                                        "sent_at": "2026-09-11T00:00:00Z",
                                        "evidence_id": "proof-3", "kind": "new_outgoing_bubble"}}}]}

    async def run():
        reporter = Reporter(TASK_CONFIG, AuditRecorder())
        async def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True})
        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await reporter.send_task_result(task)
        finally:
            await reporter.client.aclose()
    asyncio.run(run())
    assert captured[0]["status"] == "unknown"
    assert captured[0]["provider_message_id"] == "camera-t1"
    assert captured[0]["home_check"]["send_confirmation"]["confirmed"] is True
    assert task["status"] == "failed"

# §7.3（P3-3）确认矩阵：expected_status 为回调队列落库状态
# （manual=人工处理不重试；retry=退避重试；None=成功，无需入队）。
MATRIX = [
    ("200 空body", lambda: httpx.Response(200), False, "retry"),
    ("200 空对象", lambda: httpx.Response(200, json={}), False, "retry"),
    ("200 无accepted字段", lambda: httpx.Response(200, json={"foo": "bar"}), False, "retry"),
    ("200 accepted=false", lambda: httpx.Response(200, json={"accepted": False,
                                                             "reason": "托管未运行"}),
     False, "manual"),
    ("200 accepted=true", lambda: httpx.Response(200, json={"accepted": True}), True, None),
    ("4xx", lambda: httpx.Response(400, json={}), False, "manual"),
    ("5xx", lambda: httpx.Response(503, json={}), False, "retry"),
]


async def _drive_channel(reporter: Reporter, channel: str):
    if channel == "task":
        return await reporter.send_task_result(
            {"task_id": "t-1", "status": "confirmed", "status_display": "已发送"})
    return await reporter.send_emergency_stop("测试急停")


@pytest.mark.parametrize("channel,config", [("task", TASK_CONFIG), ("estop", ESTOP_CONFIG)])
@pytest.mark.parametrize("name,response_factory,expected_sent,expected_status",
                         MATRIX, ids=[m[0] for m in MATRIX])
def test_confirmation_matrix(tmp_path, channel, config, name, response_factory,
                             expected_sent, expected_status):
    """§7.3（P3-3）：task-results 与急停两通道确认矩阵对称。

    200+accepted=false → business_rejected → manual（必测回归：不得改回自动重试）；
    空 body/空对象/缺 accepted → 仍重试；4xx → manual；5xx → retry。
    """
    async def scenario():
        reporter, outbox = _reporter_with_outbox(tmp_path, config)

        async def handler(request: httpx.Request) -> httpx.Response:
            return response_factory()

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            sent = await _drive_channel(reporter, channel)
        finally:
            await reporter.client.aclose()
        assert sent is expected_sent
        queued = [c for c in outbox.list_callbacks() if c["status"] in ("manual", "retry")]
        if expected_status is None:
            assert queued == [], "成功回报不得留下 manual/retry 队列项"
        else:
            assert len(queued) == 1
            assert queued[0]["status"] == expected_status
            if expected_status == "manual" and "accepted" in name:
                assert "业务拒绝" in queued[0]["last_reason"]

    asyncio.run(scenario())


@pytest.mark.parametrize("channel,config", [("task", TASK_CONFIG), ("estop", ESTOP_CONFIG)],
                         ids=["task", "estop"])
def test_network_error_is_retryable(tmp_path, channel, config):
    """网络异常 → network_error → 退避重试（两通道）。"""
    async def scenario():
        reporter, outbox = _reporter_with_outbox(tmp_path, config)

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("WX 不可达")

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            sent = await _drive_channel(reporter, channel)
        finally:
            await reporter.client.aclose()
        assert sent is False
        queued = [c for c in outbox.list_callbacks() if c["status"] in ("manual", "retry")]
        assert len(queued) == 1 and queued[0]["status"] == "retry"

    asyncio.run(scenario())


@pytest.mark.parametrize("channel,config", [("task", TASK_CONFIG), ("estop", ESTOP_CONFIG)],
                         ids=["task", "estop"])
def test_business_rejected_manual_not_rescheduled_by_flush(tmp_path, channel, config):
    """§7.3 回归闭环：accepted=false 落 manual 后，flush 不得把 manual 行重投。"""
    async def scenario():
        reporter, outbox = _reporter_with_outbox(tmp_path, config)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": False, "reason": "托管未运行"})

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            sent = await _drive_channel(reporter, channel)
        finally:
            await reporter.client.aclose()
        assert sent is False
        manual = [c for c in outbox.list_callbacks(status="manual")]
        assert len(manual) == 1
        summary = await outbox.flush("http://wx.local")
        assert summary["delivered"] == 0 and summary["rescheduled"] == 0
        assert outbox.list_callbacks(status="manual"), "manual 行必须保持人工处理，不被 flush 重投"

    asyncio.run(scenario())


def test_customer_message_preserves_wx_business_rejection_on_http_200():
    async def scenario():
        audit = AuditRecorder()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "accepted": False,
                "requires_human": True,
                "reason": "托管尚未完成历史消息核对",
            })

        reporter = Reporter(
            {"wx_callback_base_url": "http://wx.local", "wx_callback_messages_path": "/v1/arm-fleet/customer-messages"},
            audit,
        )
        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://wx.local")
        try:
            result = await reporter.send_customer_message({"text": "测试"})
        finally:
            await reporter.client.aclose()
        assert result["accepted"] is False
        assert result["rejected"] is True
        assert result["retryable"] is False
        assert "历史消息核对" in result["reason"]

    asyncio.run(scenario())


def test_customer_message_2xx_without_confirmation_is_retryable():
    """问题 10：2xx 空 body/空对象/缺 accepted → 不算成功，按可重试处理。"""
    async def scenario():
        for response in (httpx.Response(200),
                         httpx.Response(200, json={}),
                         httpx.Response(200, json={"ok": 1})):
            audit = AuditRecorder()

            async def handler(request: httpx.Request, r=response) -> httpx.Response:
                return r

            reporter = Reporter(
                {"wx_callback_base_url": "http://wx.local",
                 "wx_callback_messages_path": "/v1/arm-fleet/customer-messages"},
                audit,
            )
            reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                result = await reporter.send_customer_message({
                    "device_id": "arm-01", "wechat_account_id": "wx-1",
                    "external_conversation_id": "c1", "external_message_id": "m-1",
                    "message_type": "text", "content_hash": "h1",
                    "observed_at": "2026-09-03T00:00:00Z", "captured_at": "2026-09-03T00:00:00Z",
                    "capture_confidence": 0.9, "ui_state": "chat_view",
                    "ui_state_display": "聊天界面", "agent_version": "t",
                    "text": "测试"})
            finally:
                await reporter.client.aclose()
            assert result["accepted"] is False
            assert result["retryable"] is True
            assert "结构化确认" in result["reason"]

    asyncio.run(scenario())


def test_task_result_requires_structured_confirmation():
    """问题 10：task-result 回调 2xx 但无 accepted=true → 未确认，不算成功。"""
    async def scenario():
        audit = AuditRecorder()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)  # 空 body：旧行为会把这当成成功

        reporter = Reporter(
            {"wx_callback_base_url": "http://wx.local",
             "wx_callback_results_path": "/v1/arm-fleet/task-results"},
            audit,
        )
        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            sent = await reporter.send_task_result({
                "task_id": "t-1", "status": "confirmed",
                "status_display": "已发送",
            })
        finally:
            await reporter.client.aclose()
        assert sent is False
        assert audit.records[-1][1]["outcome"] == "failed"
        assert "未确认" in audit.records[-1][1]["reason"]

    asyncio.run(scenario())


def test_task_result_succeeds_only_with_accepted_true():
    async def scenario():
        audit = AuditRecorder()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": True, "applied": True})

        reporter = Reporter(
            {"wx_callback_base_url": "http://wx.local",
             "wx_callback_results_path": "/v1/arm-fleet/task-results"},
            audit,
        )
        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            sent = await reporter.send_task_result({
                "task_id": "t-2", "status": "confirmed",
                "status_display": "已发送",
            })
        finally:
            await reporter.client.aclose()
        assert sent is True

    asyncio.run(scenario())
