"""审计层加固（2026-09-07 真机事故）。

8788 agent 因守护掉线事件 daemon_unavailable 未登记审计白名单，
audit.record 抛 ValueError 未捕获、直接杀死整个 agent 进程——监控上报
路径绝不允许击穿宿主。事前静态扫描同时发现第二颗雷：api/app.py 的
supervision_exit_conversation。
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from runtime.audit import ACTION_DISPLAY, AuditLog
from tests.helpers import make_device_configs, make_runtime

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _caller_keys() -> set[str]:
    """全仓 .record("key") 调用点静态扫描（与生产白名单对账）。"""
    keys: set[str] = set()
    for p in ROOT.rglob("*.py"):
        s = str(p)
        if ".venv" in s or "/tests/" in s:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(r"\.record\(\s*\"([a-z_]+)\"", src):
            keys.add(m.group(1))
    return keys


class _MemStore:
    def __init__(self):
        self.events: list[dict] = []

    def append_audit(self, event: dict) -> int:
        self.events.append(event)
        return len(self.events)

    def audit_events(self, limit: int = 200, device_id: str | None = None) -> list[dict]:
        return list(self.events)


class _MemEvents:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))


def test_all_audit_caller_keys_are_registered():
    """全部 .record("...") 调用键必须在白名单——2026-09-07 事故根因。"""
    missing = _caller_keys() - set(ACTION_DISPLAY)
    assert not missing, f"未登记的审计动作键：{sorted(missing)}"


def test_audit_records_every_caller_key_without_error():
    """逐键落盘：每个调用键都能 record 成功且 display 非空。"""
    store, events = _MemStore(), _MemEvents()
    log = AuditLog(store, events)
    for key in sorted(_caller_keys()):
        ev = log.record(key, device_id="arm-01", reason=f"批量核验 {key}")
        assert ev["action_display"], key


def test_audit_unknown_action_never_raises():
    """未知键降级落盘+告警事件，绝不抛异常击穿调用方（监控路径保命）。"""
    store, events = _MemStore(), _MemEvents()
    log = AuditLog(store, events)
    ev = log.record("some_future_key", device_id="arm-01", reason="测试")
    assert ev["audit_id"] == 1
    assert store.events and store.events[0]["action"] == "some_future_key"
    assert any(topic == "audit.unregistered_action" for topic, _ in events.published)


def test_daemon_exit_event_does_not_kill_runtime(tmp_path):
    """事故复现：daemon_exited 事件链（_handle_daemon_event →
    mark_daemon_unavailable → audit.record）全程不得抛异常，且设备
    转 offline/unmanaged、审计落痕。"""

    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            await runtime._handle_daemon_event({
                "kind": "daemon_exited", "device_id": "arm-01",
                "last_error": "串口消失（USB 重新枚举）"})
            dev = runtime.devices.get("arm-01")
            assert dev["status"] == "offline"
            assert dev["device_mode"] == "unmanaged"
            assert any(e["action"] == "daemon_unavailable"
                       for e in runtime.audit.list_events())
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
