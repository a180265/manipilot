"""Wait for a bound IME to return after the input box was focused."""
import asyncio
from types import SimpleNamespace

import pytest
from adapters.screen_ime import input_task


@pytest.mark.parametrize("initial", [None, {"online": False, "keyboardActive": False},
                                     {"online": True, "keyboardActive": False}])
def test_input_waits_for_new_heartbeat_after_focus(monkeypatch, initial):
    rows = [initial, {"online": True, "keyboardActive": True}]
    calls = []
    class Tasks:
        def list_devices(self):
            row = rows.pop(0) if len(rows) > 1 else rows[0]
            calls.append("poll")
            return [{"deviceId": "bound", **row}] if row else []
        async def create(self, payload):
            calls.append("create")
            assert not rows or rows[0]["keyboardActive"]
            return payload, True
        async def wait_for_status(self, task_id, statuses, timeout, probe=None):
            return {"status": "displayed"} if "displayed" in statuses else {
                "status": "inserted", "typedCharacterCount": 2}
        async def cancel(self, task_id):
            calls.append("cancel")
    async def click(*args):
        calls.append("candidate")
        return {"ok": True}
    async def sleep(_):
        calls.append("wait")
    monkeypatch.setattr(input_task, "get_default_service", lambda: SimpleNamespace(tasks=Tasks()))
    monkeypatch.setattr(input_task, "_input_disallowed", lambda ctx: False)
    monkeypatch.setattr(input_task.asyncio, "sleep", sleep)
    ctx = SimpleNamespace(task={"task_id": "t", "payload": {"text": "测试"}},
        device_context={"ime_device_id": "bound"}, state_set=lambda *args: None,
        call_capability=click)
    result = asyncio.run(input_task.insert_bound_text(ctx))
    assert result["outcome"] == "success"
    assert calls == ["poll", "wait", "poll", "create", "candidate"]
