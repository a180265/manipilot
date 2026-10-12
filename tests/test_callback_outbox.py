"""§10 回调可靠投递测试：失败入队、退避重试、4xx 人工、最终放弃与查询。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from runtime.audit import AuditLog
from runtime.callback_outbox import CallbackOutbox
from runtime.events import EventBus
from runtime.reporter import Reporter
from runtime.store import Store
from runtime.util import iso, utc_now


class _Events:
    def publish(self, *args, **kwargs):
        pass


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "fleet.db")


@pytest.fixture()
def outbox(store: Store) -> CallbackOutbox:
    audit = AuditLog(store, _Events())
    return CallbackOutbox(store, audit)


def test_failure_is_queued_with_backoff(outbox: CallbackOutbox):
    """5xx/网络失败进入重试队列，退避至少 1s/5s/30s/5min。"""
    first = outbox.record_failure(
        kind="customer_message", path="/v1/arm-fleet/customer-messages",
        payload={"external_message_id": "m-1"}, callback_id="customer-message:m-1",
        reason="WX 服务错误：HTTP 503", outcome="server_error")
    assert first["status"] == "retry"
    assert first["attempt_count"] == 1

    again = outbox.record_failure(
        kind="customer_message", path="/v1/arm-fleet/customer-messages",
        payload={"external_message_id": "m-1"}, callback_id="customer-message:m-1",
        reason="网络异常：超时", outcome="network_error")
    assert again["attempt_count"] == 2
    # 同一 callback_id 只有一行（稳定幂等键）
    assert len([c for c in outbox.list_callbacks() if c["callback_id"] == "customer-message:m-1"]) == 1


def test_4xx_goes_to_manual_without_retry(outbox: CallbackOutbox):
    """4xx 参数错误进入人工处理，不无限重试。"""
    item = outbox.record_failure(
        kind="customer_message", path="/v1/arm-fleet/customer-messages",
        payload={}, callback_id="customer-message:m-bad",
        reason="WX 拒绝消息回报：HTTP 400", outcome="rejected_4xx")
    assert item["status"] == "manual"
    # manual 状态不会被领取重投
    assert outbox._store.callbacks_due(iso(utc_now())) == []


def test_flush_delivers_on_2xx_and_marks_success(outbox: CallbackOutbox):
    async def scenario():
        outbox.record_failure(
            kind="customer_message", path="/v1/arm-fleet/customer-messages",
            payload={"external_message_id": "m-1"}, callback_id="customer-message:m-1",
            reason="网络异常", outcome="network_error")

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/arm-fleet/customer-messages"
            return httpx.Response(200, json={"accepted": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            # 重试队列有 next_attempt_at；到期后可投递
            item = outbox._store.get_callback("customer-message:m-1")
            item["next_attempt_at"] = iso(utc_now())
            outbox._store.save_callback(item)
            summary = await outbox.flush("http://wx.local", client=client)
        finally:
            await client.aclose()
        assert summary["delivered"] == 1
        assert outbox._store.get_callback("customer-message:m-1")["status"] == "succeeded"

    asyncio.run(scenario())


def test_flush_business_rejection_goes_manual(outbox: CallbackOutbox):
    """WX 200 + accepted=false（如托管未运行）→ 人工处理，不无限重试。"""
    async def scenario():
        outbox.record_failure(
            kind="customer_message", path="/v1/arm-fleet/customer-messages",
            payload={"external_message_id": "m-2"}, callback_id="customer-message:m-2",
            reason="网络异常", outcome="network_error")

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": False, "reason": "托管未运行"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            item = outbox._store.get_callback("customer-message:m-2")
            item["next_attempt_at"] = iso(utc_now())
            outbox._store.save_callback(item)
            summary = await outbox.flush("http://wx.local", client=client)
        finally:
            await client.aclose()
        assert summary["manual"] == 1
        assert outbox._store.get_callback("customer-message:m-2")["status"] == "manual"

    asyncio.run(scenario())


def test_flush_empty_2xx_is_not_success(outbox: CallbackOutbox):
    """问题 10：空 body / 空对象 / 缺 accepted 字段的 2xx 不得标记成功，必须重试。"""
    async def scenario():
        responses = [
            httpx.Response(200),                      # 空 body
            httpx.Response(200, json={}),             # 空对象
            httpx.Response(200, json={"foo": "bar"}), # 缺确认字段
        ]
        outbox.record_failure(
            kind="customer_message", path="/v1/arm-fleet/customer-messages",
            payload={"external_message_id": "m-empty"}, callback_id="customer-message:m-empty",
            reason="网络异常", outcome="network_error")

        async def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            for _ in responses:
                item = outbox._store.get_callback("customer-message:m-empty")
                item["next_attempt_at"] = iso(utc_now())
                outbox._store.save_callback(item)
                summary = await outbox.flush("http://wx.local", client=client)
                # 三种 2xx 都未确认 → 全部进入重试，绝不 delivered
                assert summary["delivered"] == 0
                assert summary["rescheduled"] == 1
            current = outbox._store.get_callback("customer-message:m-empty")
            assert current["status"] == "retry"
            assert "结构化确认" in current["last_reason"]
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_flush_abandons_after_max_attempts(outbox: CallbackOutbox):
    """持续 5xx：按最大次数重试后最终放弃。"""
    calls = {"n": 0}

    async def scenario():
        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, json={"detail": "重启中"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            outbox.record_failure(
                kind="task_result", path="/v1/arm-fleet/task-results",
                payload={"task_id": "t-1"}, callback_id="task-result:t-1:failed",
                reason="WX 服务错误", outcome="server_error")
            for _ in range(outbox.max_attempts):
                item = outbox._store.get_callback("task-result:t-1:failed")
                item["next_attempt_at"] = iso(utc_now())
                item["status"] = "retry"
                outbox._store.save_callback(item)
                await outbox.flush("http://wx.local", client=client)
        finally:
            await client.aclose()
        final = outbox._store.get_callback("task-result:t-1:failed")
        assert final["status"] == "abandoned"
        summary = outbox.summary()
        assert summary["最终放弃"] == 1

    asyncio.run(scenario())


def test_flush_summary_counts_abandoned_correctly(outbox: CallbackOutbox):
    """§7.2（P3-2）：重试耗尽转终态放弃时摘要计入 abandoned，不再混入 rescheduled。"""
    async def scenario():
        outbox.max_attempts = 3
        outbox.backoff_seconds = (1, 5, 30)
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, json={"detail": "重启中"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            outbox.record_failure(
                kind="task_result", path="/v1/arm-fleet/task-results",
                payload={"task_id": "t-ab"}, callback_id="task-result:t-ab:failed",
                reason="WX 服务错误", outcome="server_error")
            summaries = []
            for _ in range(outbox.max_attempts):
                item = outbox._store.get_callback("task-result:t-ab:failed")
                if item is None or item["status"] == "abandoned":
                    break
                item["next_attempt_at"] = iso(utc_now())
                item["status"] = "retry"
                outbox._store.save_callback(item)
                summaries.append(await outbox.flush("http://wx.local", client=client))
        finally:
            await client.aclose()
        # 首轮 flush → 仍可重试；次轮 flush → 放弃：放弃行只计入 abandoned
        assert [(s["rescheduled"], s["abandoned"]) for s in summaries] == [(1, 0), (0, 1)]
        assert outbox._store.get_callback("task-result:t-ab:failed")["status"] == "abandoned"
        # 放弃后再 flush：终态行不会被重新领取，也不产生任何计数
        async def handler_ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": True})

        client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler_ok))
        try:
            after = await outbox.flush("http://wx.local", client=client2)
        finally:
            await client2.aclose()
        assert after == {"delivered": 0, "manual": 0, "rescheduled": 0, "abandoned": 0, "skipped": 0}
        assert outbox.summary()["最终放弃"] == 1

    asyncio.run(scenario())


def test_reporter_queues_failures_and_resend_via_reporter(store: Store):
    """Reporter 回报失败自动入队；成功回报把队列项标记为已投递。"""
    audit = AuditLog(store, _Events())
    callback_outbox = CallbackOutbox(store, audit)
    reporter = Reporter(
        {"wx_callback_base_url": "http://wx.local",
         "wx_callback_messages_path": "/v1/arm-fleet/customer-messages"},
        audit, callback_outbox=callback_outbox)

    async def failing():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await reporter.send_customer_message(
                {"external_message_id": "m-9", "text": "你好"})
        finally:
            await reporter.client.aclose()

    result = asyncio.run(failing())
    assert result["retryable"] is True
    queued = store.get_callback("customer-message:m-9")
    assert queued is not None and queued["status"] == "retry"

    # WX 恢复后由 flush 成功投递并标记
    async def recovering():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": True, "queued": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            queued["next_attempt_at"] = iso(utc_now())
            store.save_callback(queued)
            return await callback_outbox.flush("http://wx.local", client=client)
        finally:
            await client.aclose()

    summary = asyncio.run(recovering())
    assert summary["delivered"] == 1
    assert store.get_callback("customer-message:m-9")["status"] == "succeeded"
