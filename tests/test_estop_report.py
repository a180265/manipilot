"""§7.6（P3-6）急停回报测试：先落本地回调队列再投递；事件循环立即退出由 flush 兜底。

- 触发急停 → callback_outbox 有 emergency-stop 行（先持久化）；
- 投递 accepted=true → 行标记 succeeded（mark_succeeded 复用）；
- create_task 抛出/未执行（模拟事件循环立即退出）→ 后台 flush 兜底重投成功；
- WX 暂停/恢复语义仅由 reporter 返回值体现（mock 层面，不跨进程）；
- 未配置回调队列（None）→ 保持 fire-and-forget 降级，不报错。
"""
from __future__ import annotations

import asyncio
import json

import httpx

from runtime.audit import AuditLog
from runtime.callback_outbox import CallbackOutbox
from runtime.estop import EstopController
from runtime.reporter import Reporter
from runtime.store import Store

ESTOP_CONFIG = {"wx_callback_base_url": "http://wx.local",
                "wx_callback_estop_path": "/v1/arm-fleet/emergency-stop"}


class _Events:
    def publish(self, *args, **kwargs):
        pass


class _NoDevices:
    """无设备桩：急停本地状态与 WX 回报解耦，设备遍历为空。"""

    def all(self):
        return []

    def _alert(self, *args, **kwargs):
        pass


def _make(tmp_path, *, with_outbox=True):
    store = Store(tmp_path / "fleet.db")
    audit = AuditLog(store, _Events())
    outbox = CallbackOutbox(store, audit) if with_outbox else None
    reporter = Reporter(dict(ESTOP_CONFIG), audit, callback_outbox=outbox)
    controller = EstopController(store, audit, _Events(), _NoDevices(), reporter)
    return controller, reporter, outbox, store


def test_estop_persists_callback_row_before_delivery(tmp_path):
    """① 触发 → 先落库：create_task 尚未执行时，callback 表已有待投递行。"""
    controller, reporter, outbox, store = _make(tmp_path)

    async def scenario():
        # 即便投递任务永远不执行（mock 客户端 503），行也已先落本地队列
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = controller.trigger("op-1", "测试急停")
            # trigger 返回后、任何 await 之前：行已持久化且未被投递触碰
            assert result["estop_active"] is True and result["already_active"] is False
            rows = store.all_callbacks()
            assert len(rows) == 1
            row = rows[0]
            assert row["callback_id"].startswith("emergency-stop:")
            assert row["kind"] == "emergency_stop"
            assert row["path"] == "/v1/arm-fleet/emergency-stop"
            assert row["status"] == "pending"
            assert row["payload"]["event"] == "emergency_stop"
            assert row["payload"]["reason"] == "测试急停"
            assert row["payload"]["occurred_at"] in row["callback_id"]
        finally:
            await reporter.client.aclose()

    asyncio.run(scenario())


def test_estop_delivery_accepted_marks_succeeded(tmp_path):
    """② 投递 accepted=true → 同一 callback_id 标记 succeeded，负载一致。"""
    controller, reporter, outbox, store = _make(tmp_path)
    requests: list[httpx.Request] = []

    async def scenario():
        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"accepted": True})

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            controller.trigger("op-1", "现场安全急停")
            await asyncio.sleep(0.05)  # 让 create_task 投递执行
        finally:
            await reporter.client.aclose()

    asyncio.run(scenario())
    rows = store.all_callbacks()
    assert len(rows) == 1 and rows[0]["status"] == "succeeded"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/arm-fleet/emergency-stop"
    body = json.loads(requests[0].read())
    assert body == rows[0]["payload"], "投递负载必须与先持久化的 payload 完全一致"
    assert body["event"] == "emergency_stop" and body["reason"] == "现场安全急停"


def test_estop_report_survives_event_loop_exit_via_flush(tmp_path, monkeypatch):
    """③ 事件循环立即退出（create_task 抛出）→ 后台 flush 兜底重投成功。"""
    controller, reporter, outbox, store = _make(tmp_path)

    def _no_loop(coro):
        raise RuntimeError("事件循环立即退出，急停投递任务未执行")

    async def scenario():
        monkeypatch.setattr(asyncio, "create_task", _no_loop)
        result = controller.trigger("op-1", "急停后进程退出")
        # 急停本地生效不被投递失败阻塞；回调已落库等待兜底
        assert result["estop_active"] is True
        assert controller.active
        rows = store.all_callbacks()
        assert len(rows) == 1 and rows[0]["status"] == "pending"

    asyncio.run(scenario())

    async def flush_scenario():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await outbox.flush("http://wx.local", client=client)
        finally:
            await client.aclose()

    summary = asyncio.run(flush_scenario())
    assert summary["delivered"] == 1
    assert store.all_callbacks()[0]["status"] == "succeeded"


def test_estop_report_wx_pause_and_resume_via_reporter_result(tmp_path):
    """④ WX 暂停/恢复（仅 mock 层面）：接收→True；未确认→False+队列保留；恢复→补投成功。"""
    controller, reporter, outbox, store = _make(tmp_path)

    async def paused_scenario():
        # 业务 接收急停（暂停 Outbox 调度）：accepted=true → reporter 返回 True
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": True, "paused": True})

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await reporter.send_emergency_stop("WX 已接收急停")
        finally:
            await reporter.client.aclose()

    assert asyncio.run(paused_scenario()) is True

    async def rejected_scenario():
        # 业务 未确认（5xx）：reporter 返回 False，急停回调留在本地队列
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await reporter.send_emergency_stop("WX 暂停未确认")
        finally:
            await reporter.client.aclose()

    assert asyncio.run(rejected_scenario()) is False
    rows = [r for r in store.all_callbacks() if r["status"] == "retry"]
    assert len(rows) == 1 and rows[0]["payload"]["reason"] == "WX 暂停未确认"
    # 退避到期（模拟时间推进）后由后台 flush 领取
    from runtime.util import iso, utc_now
    due = store.get_callback(rows[0]["callback_id"])
    due["next_attempt_at"] = iso(utc_now())
    store.save_callback(due)

    async def flush_scenario():
        # 业务 恢复（mock）：后台 flush 补投成功，语义等同于急停通知最终生效
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": True, "paused": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await outbox.flush("http://wx.local", client=client)
        finally:
            await client.aclose()

    summary = asyncio.run(flush_scenario())
    assert summary["delivered"] == 1
    assert all(r["status"] == "succeeded" for r in store.all_callbacks())


def test_estop_without_outbox_degrades_to_fire_and_forget(tmp_path):
    """未配置回调队列（callback_outbox=None）：保持 fire-and-forget，不报错。"""
    controller, reporter, outbox, store = _make(tmp_path, with_outbox=False)
    assert reporter.callback_outbox is None

    async def scenario():
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accepted": True})

        reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = controller.trigger("op-1", "降级急停")
            await asyncio.sleep(0.05)
            return result
        finally:
            await reporter.client.aclose()

    result = asyncio.run(scenario())
    assert result["estop_active"] is True
    assert store.all_callbacks() == []
