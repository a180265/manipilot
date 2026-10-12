"""Wake-specific finalization, race safety and crash recovery."""
import asyncio
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest

from api.app import create_app
from runtime.util import iso, utc_now
from tasks.plugin_api import PluginViolationError
from tests.helpers import op_context, wait_terminal
from tests.test_screen_wake_runtime import wake_runtime


@pytest.mark.parametrize("result,status,effect", [
    ({"controller_completed": True, "tap_count": 2, "effect_status": "awake"}, "confirmed", "unverified"),
    ({"controller_completed": True, "tap_count": 2, "effect_status": "screen_lit"}, "confirmed", "unverified"),
    ({"controller_completed": True, "tap_count": 1, "effect_status": "unverified"}, "unknown", "unverified"),
    ({"controller_completed": False, "tap_count": 2, "effect_status": "unverified"}, "unknown", "unverified"),
    ({"controller_completed": True, "tap_count": 2, "effect_status": "awake", "stopped": True}, "unknown", "unverified"),
    ({"controller_completed": False, "tap_count": 0, "effect_status": "unverified",
      "motion_not_started": True}, "failed", "unverified"),
    (None, "unknown", "unverified"),
])
def test_controller_completion_and_phone_effect_are_independent(tmp_path, monkeypatch, result, status, effect):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)

        async def wake(params, *, execution_guard):
            execution_guard()
            calls.append(params)
            return result

        monkeypatch.setattr(runtime.devices.driver("arm-01"), "wake_screen", wake)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        finished = await wait_terminal(runtime, task["task_id"])
        assert finished["status"] == status
        assert finished["effect_status"] == effect
        assert not finished["home_verified"]
        assert runtime.leases.get("arm-01") is None
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"status": "paused"}, {"status": "quarantined"}, {"home_ok": False},
    {"estop_latched": True}, {"camera_quality": "frozen"},
    {"camera_quality": "clear"}, {"daemon_session": "changed"},
    {"binding_enabled": False},
])
def test_post_plan_gate_rejects_without_clearing_other_safety_state(tmp_path, monkeypatch, change):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        plugin, _ = runtime.plugins.instance_for_task_type("screen_wake")
        original = plugin.plan

        async def plan(task, device):
            result = await original(task, device)
            runtime.devices.get("arm-01").update(change)
            return result

        monkeypatch.setattr(plugin, "plan", plan)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "failed"
        assert result["motion_not_started"] is True
        assert calls == []
        for key, value in change.items():
            assert runtime.devices.get("arm-01")[key] == value
        assert runtime.devices.get("arm-01")["calibration_status"] == "validated"
        assert runtime.leases.get("arm-01") is None

    asyncio.run(scenario())


def test_expiry_during_planning_prevents_driver_entry(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        plugin, _ = runtime.plugins.instance_for_task_type("screen_wake")
        original = plugin.plan

        async def plan(task, device):
            result = await original(task, device)
            # Shift the policy clock, without waiting for a real minute.
            monkeypatch.setattr("runtime.screen_wake.utc_now",
                                lambda: utc_now() + timedelta(seconds=61))
            return result

        monkeypatch.setattr(plugin, "plan", plan)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "failed"
        assert result["motion_not_started"] is True
        assert not calls
        assert runtime.leases.get("arm-01") is None

    asyncio.run(scenario())


def test_replaced_lease_survives_old_task_finalization(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        replacement = {}

        async def wake(params, *, execution_guard):
            execution_guard()
            calls.append(params)
            old = runtime.leases.get("arm-01")
            runtime.leases.release("arm-01", old["lease_id"])
            replacement.update(runtime.leases.acquire("arm-01", "new-task"))
            return {"controller_completed": True, "tap_count": 2, "effect_status": "unverified"}

        monkeypatch.setattr(runtime.devices.driver("arm-01"), "wake_screen", wake)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "unknown"
        assert runtime.leases.get("arm-01")["lease_id"] == replacement["lease_id"]
        assert runtime.devices.get("arm-01")["status"] == "busy"
        assert runtime.devices.get("arm-01")["calibration_status"] == "validated"
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("capability", [
    "arm.double_tap", "arm.home", "input.type_text", "camera.identify_ui_state",
])
def test_wake_task_does_not_gain_other_capabilities_even_if_manifest_allows(tmp_path, monkeypatch, capability):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        record = runtime.plugins.get("screen-wake-adapter")
        record["manifest"]["capabilities"].append(capability)
        runtime.store.save_plugin(record)
        lease = runtime.leases.acquire("arm-01", task["task_id"])
        task.update(status="executing", lease_id=lease["lease_id"])
        runtime.store.save_task(task)
        runtime.devices.get("arm-01")["status"] = "busy"
        with pytest.raises(PluginViolationError):
            await runtime.plugin_invoke_capability("screen-wake-adapter", task, capability, {})
        assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("recorded_outcome", [None, "failed", "unknown", "success"])
def test_restart_never_replays_started_wake(tmp_path, monkeypatch, recorded_outcome):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        plugin, _ = runtime.plugins.instance_for_task_type("screen_wake")
        task.update(status="executing", plan=await plugin.plan(task, {}),
                    wake_driver_started=True, action_call_count=1,
                    step_cursor=0 if recorded_outcome is None else 1)
        task["step_log"] = [{"step_no": 1, "phase": "started", "is_action": True,
                             "action": "wake_screen", "action_display": "人工唤醒"}]
        if recorded_outcome:
            task["step_log"].append({
                "step_no": 1, "phase": "done", "is_action": True,
                "action": "wake_screen", "action_display": "人工唤醒",
                "outcome": recorded_outcome, "detail": {
                    "controller_completed": recorded_outcome == "success",
                    "tap_count": 2 if recorded_outcome == "success" else 0,
                    "effect_status": "unverified",
                },
            })
        runtime.store.save_task(task)
        runtime.devices.get("arm-01")["status"] = "ready"
        runtime.recover_tasks()
        assert runtime.get_task(task["task_id"])["status"] == "unknown"
        assert not runtime._exec_tasks, "recovery must not reenter a wake executor"
        for future in runtime._exec_tasks.values():
            await future
        await runtime._dispatch_once()
        assert calls == []
        assert runtime.leases.get("arm-01") is None
        assert runtime.get_task(task["task_id"])["status"] == "unknown"
        assert runtime.devices.get("arm-01")["status"] == "paused"
        assert runtime.devices.get("arm-01")["home_ok"] is False

    asyncio.run(scenario())


def test_api_submission_keeps_contract_and_original_idempotency(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        app = create_app(runtime)
        app.state.runtime = runtime
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            submitted = await client.post("/v1/tasks", json=request)
            assert submitted.status_code == 200
            await runtime._dispatch_once()
            finished = await wait_terminal(runtime, request["task_id"])
            assert finished["status"] == "confirmed"
            duplicate = await client.post("/v1/tasks", json=request)
            assert duplicate.status_code == 200
            assert duplicate.json()["task_id"] == submitted.json()["task_id"]
            bad = deepcopy(request)
            bad["payload"]["confirmed"] = False
            conflict = await client.post("/v1/tasks", json=bad)
            assert conflict.status_code == 409
            result = await client.get("/v1/tasks/" + request["task_id"])
            assert result.json()["effect_status"] == "unverified"
        assert len(calls) == 1

    asyncio.run(scenario())


def test_contract_allows_one_attempt_even_with_a_looser_plugin_budget(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        record = runtime.plugins.get("screen-wake-adapter")
        record["manifest"]["limits"]["max_actions_per_task"] = 5
        runtime.store.save_plugin(record)
        lease = runtime.leases.acquire("arm-01", task["task_id"])
        task.update(status="executing", lease_id=lease["lease_id"])
        runtime.store.save_task(task)
        runtime.devices.get("arm-01")["status"] = "busy"
        await runtime.plugin_invoke_capability(
            "screen-wake-adapter", task, "arm.wake_screen", request["payload"])
        with pytest.raises(PluginViolationError) as error:
            await runtime.plugin_invoke_capability(
                "screen-wake-adapter", task, "arm.wake_screen", request["payload"])
        assert getattr(error.value, "motion_not_started", False) is False
        assert len(calls) == 1

    asyncio.run(scenario())


def test_clear_frame_only_allowed_to_finish_an_already_started_original_lease(tmp_path, monkeypatch):
    from runtime.screen_wake import wake_readiness

    async def scenario():
        runtime, request, _ = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        device = runtime.devices.get("arm-01")
        device["camera_quality"] = "clear"
        view = wake_readiness(runtime, "arm-01")
        assert not view["usable"]
        assert "screen_not_waiting" in [item["code"] for item in view["blocking_reasons"]]
        lease = runtime.leases.acquire("arm-01", task["task_id"])
        task.update(lease_id=lease["lease_id"], status="executing", wake_driver_started=True)
        runtime.store.save_task(task)
        device["status"] = "busy"
        assert wake_readiness(runtime, "arm-01", task=task)["usable"]
        device["estop_latched"] = True
        assert not wake_readiness(runtime, "arm-01", task=task)["usable"]

    asyncio.run(scenario())


def test_preflight_lease_loss_is_failed_when_driver_never_started(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        plugin, _ = runtime.plugins.instance_for_task_type("screen_wake")
        original = plugin.execute_step

        async def execute(step, ctx):
            lease = runtime.leases.get("arm-01")
            runtime.leases.release("arm-01", lease["lease_id"])
            return await original(step, ctx)

        monkeypatch.setattr(plugin, "execute_step", execute)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "failed"
        assert result["motion_not_started"] is True
        assert runtime.devices.get("arm-01")["status"] != "paused"
        assert not calls

    asyncio.run(scenario())


def test_cancel_after_claim_releases_original_lease_without_home(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        await runtime.cancel_task(task["task_id"], op_context())
        for future in runtime._exec_tasks.values():
            await future
        result = runtime.get_task(task["task_id"])
        assert result["status"] == "cancelled"
        assert result["motion_not_started"] is True
        assert not calls and runtime.leases.get("arm-01") is None
        assert runtime.devices.get("arm-01")["calibration_status"] == "validated"
        assert not any(item.get("action") == "go_home"
                       for item in runtime.devices.driver("arm-01").action_log)

    asyncio.run(scenario())
