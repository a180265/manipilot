"""Bound IME input observes mechanical receipts before waiting or allowing sends."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from adapters.screen_ime import input_task


@pytest.fixture
def input_rig(monkeypatch):
    events = []
    class Tasks:
        def list_devices(self):
            return [{"deviceId": "phone", "online": True, "keyboardActive": True}]

        async def create(self, body):
            events.append("create")
            return body, True

        async def wait_for_status(self, task_id, statuses, timeout, probe=None):
            events.append(("wait", timeout))
            if "displayed" in statuses:
                return {"status": "displayed"}
            return {"status": "inserted", "typedCharacterCount": 2}

        async def cancel(self, task_id):
            events.append("cancel")
            return {"status": "cancelled"}

    class Context:
        task = {"task_id": "bound-input", "payload": {"text": "你好"}}
        device_context = {"ime_device_id": "phone"}
        click = {"ok": True}
        park = {"ok": True, "arrived": True}
        state = {}

        def state_set(self, key, value):
            self.state[key] = value

        async def call_capability(self, name, body):
            events.append((name, body))
            result = self.click if name == "arm.click" else self.park
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(input_task, "_input_disallowed", lambda ctx: False)
    monkeypatch.setattr(input_task, "get_default_service", lambda: SimpleNamespace(tasks=Tasks()))
    return Context(), events


@pytest.mark.parametrize("receipt", [
    {"ok": True, "outcome_hint": "unknown"},
    {"ok": False}, {"ok": True, "controller_completed": False},
    {"ok": True, "motion_verified": False},
    {"ok": True, "outcome": "unknown"}, None,
    {"controller_completed": True, "motion_observation_only": True, "outcome_hint": "unknown"},
])
def test_unknown_candidate_click_cancels_without_waiting_for_inserted(input_rig, receipt):
    ctx, events = input_rig
    ctx.click = receipt
    result = asyncio.run(input_task.insert_bound_text(ctx))
    assert result["outcome"] == "unknown"
    assert events == ["create", ("wait", 20),
                      ("arm.click", {"target_id": "ime_candidate_bar"}), "cancel"]


@pytest.mark.parametrize("receipt", [
    {"ok": True},
    {"ok": True, "motion_verified": True, "outcome_hint": "success"},
    {"ok": True, "controller_completed": True, "motion_verified": False,
     "motion_observation_only": True, "outcome_hint": "success"},
])
def test_completed_candidate_click_waits_then_parks_before_success(input_rig, receipt):
    ctx, events = input_rig
    ctx.click = receipt
    result = asyncio.run(input_task.insert_bound_text(ctx))
    assert result["outcome"] == "success"
    assert events == ["create", ("wait", 20),
                      ("arm.click", {"target_id": "ime_candidate_bar"}),
                      ("wait", 60), ("arm.park", {})]
    assert ctx.state["active_ime_task_id"] is None


@pytest.mark.parametrize("receipt", [
    {"ok": False}, {"ok": True, "arrived": False},
    {"ok": True, "outcome_hint": "unknown"}, RuntimeError("park disconnected"),
])
def test_park_failure_after_inserted_is_unknown_and_never_replays(input_rig, receipt):
    ctx, events = input_rig
    ctx.park = receipt
    result = asyncio.run(input_task.insert_bound_text(ctx))
    assert result["outcome"] == "unknown"
    assert events.count(("arm.click", {"target_id": "ime_candidate_bar"})) == 1
    assert events[-2:] == [("arm.park", {}), "cancel"]
