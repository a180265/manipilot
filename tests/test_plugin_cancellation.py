"""Cancellation regressions use isolated simulation devices only."""
import asyncio
from copy import deepcopy

import pytest

from drivers.arm import DriverError
from tasks.plugin_api import StepContext
from tests.helpers import (
    PROBE_MANIFEST, ScriptedProbePlugin, make_device_configs, make_runtime,
    op_context, probe_payload, wait_terminal,
)


CANCELLATION_MANIFEST = deepcopy(PROBE_MANIFEST)
CANCELLATION_MANIFEST["capabilities"] += [
    "arm.click", "arm.double_tap", "arm.swipe", "arm.move_target",
    "arm.move_preset", "input.type_text",
]
CANCELLATION_MANIFEST["resources"]["input"] = True


def leased_task(runtime, task, manifest):
    task.update(
        plugin_id=manifest["plugin_id"], status="executing", action_call_count=0,
        lease_id=runtime.leases.acquire(task["device_id"], task["task_id"])["lease_id"])
    runtime.store.save_task(deepcopy(task))
    return task


@pytest.mark.parametrize("capture", ["capability", "context"])
def test_capture_preserves_cancellation_from_another_task_copy(tmp_path, capture):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = {
                "task_id": "capture-cancel",
                "device_id": "arm-01",
                "status": "executing",
                "status_display": "executing",
                "cancel_requested": False,
            }
            leased_task(runtime, task, PROBE_MANIFEST)
            await runtime.cancel_task(task["task_id"], op_context(reason="stop requested"))
            ctx = StepContext(runtime, task, PROBE_MANIFEST)
            if capture == "capability":
                await ctx.call_capability("camera.capture")
            else:
                await ctx.capture_evidence("cancellation evidence")
            saved = runtime.get_task(task["task_id"])
            assert saved["cancel_requested"] is True
            assert saved["cancel_reason"] == "stop requested"
            assert saved["evidence_ids"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


class CancellationProbe(ScriptedProbePlugin):
    def __init__(self, outcome="success", *, call_stop=True):
        super().__init__()
        self.reached = asyncio.Event()
        self.proceed = asyncio.Event()
        self.outcome = outcome
        self.call_stop = call_stop
        self.recovery_calls = 0
        self.cleanup_calls = 0
        self.blocked = []

    async def execute_step(self, step, ctx):
        await ctx.call_capability("arm.click", {"target_id": "input_box"})
        self.reached.set()
        await self.proceed.wait()
        await ctx.call_capability("camera.capture")
        await ctx.capture_evidence("after cancellation request")
        try:
            await ctx.call_capability("arm.swipe", {
                "start": {"x": 10, "y": 10}, "end": {"x": 20, "y": 20},
            })
        except DriverError:
            self.blocked.append("swipe")
        return {"outcome": self.outcome, "message": "probe finished"}

    async def cancel(self, ctx):
        self.cancel_called = True
        if self.call_stop:
            await ctx.call_capability("arm.stop")
        # Deliberately misreport the result: the runtime must retain driver facts.
        return {"stopped": True}

    async def recover(self, failure, ctx):
        self.recovery_calls += 1
        await ctx.call_capability("arm.click", {"target_id": "back_button"})
        return {"home_verified": True}

    async def cleanup(self, ctx):
        self.cleanup_calls += 1
        try:
            await ctx.call_capability("arm.move_target", {"target_id": "ime_delete_key"})
        except DriverError:
            self.blocked.append("cleanup")
        return {"cleaned": True}


@pytest.mark.parametrize("stop_result", [
    {"ok": False, "stopped": False},
    {"ok": True, "stopped": True, "outcome_hint": "unknown"},
    None,
    DriverError("driver_disconnected", "simulated stop failure"),
    "timeout",
])
def test_uncertain_stop_cannot_be_hidden_by_plugin_or_trigger_recovery(tmp_path, monkeypatch, stop_result):
    async def scenario():
        plugin = CancellationProbe()
        runtime = make_runtime(tmp_path, plugin=plugin, manifest=CANCELLATION_MANIFEST)
        await runtime.startup(device_configs=make_device_configs())
        try:
            arm = runtime.devices.driver("arm-01")
            stop_calls, home_calls = [], []

            async def stop():
                stop_calls.append("stop")
                if stop_result == "timeout":
                    await asyncio.Event().wait()
                if isinstance(stop_result, Exception):
                    raise stop_result
                return deepcopy(stop_result)

            async def home_check(device_id):
                home_calls.append(device_id)
                return {"home_ok": True}

            monkeypatch.setattr(arm, "stop", stop)
            monkeypatch.setattr("runtime.runtime.TASK_STOP_TIMEOUT_SECONDS", 0.03)
            monkeypatch.setattr(runtime.devices, "home_check", home_check)
            task = runtime.submit_task(
                probe_payload([{"is_action": True}]), source="console",
                context={"operator_id": "test"},
            )
            await asyncio.wait_for(plugin.reached.wait(), 2)
            await runtime.cancel_task(task["task_id"], op_context(reason="cancel mid-step"))
            plugin.proceed.set()
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "unknown"
            assert done["cancel_requested"] is True
            assert done["cancel_reason"] == "cancel mid-step"
            assert done["stop_result_unknown"] is True
            assert done["evidence_ids"]
            assert done["home_verified"] is False
            assert done["lease_retained"] is True
            assert runtime.leases.get("arm-01") is not None
            assert runtime.devices.get("arm-01")["status"] in {"paused", "quarantined"}
            assert stop_calls == ["stop"]
            assert not home_calls
            assert plugin.cancel_called and plugin.cleanup_calls == 0
            assert plugin.recovery_calls == 0
            assert plugin.blocked == ["swipe"]
            assert [row["action"] for row in arm.action_log].count("click") == 1
        finally:
            plugin.proceed.set()
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "failed", "unknown"])
@pytest.mark.parametrize("call_stop", [True, False])
def test_midstep_cancel_uses_driver_stop_once_and_never_recovers(tmp_path, outcome, call_stop):
    async def scenario():
        plugin = CancellationProbe(outcome, call_stop=call_stop)
        runtime = make_runtime(tmp_path, plugin=plugin, manifest=CANCELLATION_MANIFEST)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{"is_action": True}]), source="console",
                context={"operator_id": "test"},
            )
            await asyncio.wait_for(plugin.reached.wait(), 2)
            await runtime.cancel_task(task["task_id"], op_context())
            plugin.proceed.set()
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == ("cancelled" if outcome == "success" else "unknown")
            assert done["stop_result"]["stopped"] is True
            assert done["stop_attempt_count"] == 1
            assert plugin.cancel_called and plugin.recovery_calls == 0
            assert plugin.blocked == (["swipe", "cleanup"] if outcome == "success" else ["swipe"])
            actions = [row["action"] for row in runtime.devices.driver("arm-01").action_log]
            assert actions.count("stop") == 1
            assert actions.count("click") == 1
            assert "swipe" not in actions
        finally:
            plugin.proceed.set()
            await runtime.shutdown()

    asyncio.run(scenario())


def test_failed_stop_blocks_later_actions_even_without_cancellation(tmp_path, monkeypatch):
    async def scenario():
        runtime = make_runtime(tmp_path, manifest=CANCELLATION_MANIFEST)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = {"task_id": "failed-stop", "device_id": "arm-01"}
            leased_task(runtime, task, CANCELLATION_MANIFEST)
            ctx = StepContext(runtime, task, CANCELLATION_MANIFEST)
            arm = runtime.devices.driver("arm-01")
            original_stop = arm.stop

            async def stop():
                return {"ok": False, "stopped": False, "outcome_hint": "unknown"}

            monkeypatch.setattr(arm, "stop", stop)
            assert not (await ctx.call_capability("arm.stop"))["stopped"]
            monkeypatch.setattr(arm, "stop", original_stop)
            assert (await ctx.call_capability("arm.stop"))["stopped"]
            with pytest.raises(DriverError, match="禁止"):
                await ctx.call_capability("arm.click", {"target_id": "back_button"})
            assert runtime.get_task(task["task_id"])["stop_result_unknown"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("capability", [
    "arm.click", "arm.double_tap", "arm.swipe", "arm.move_target",
    "arm.move_preset", "arm.home", "input.type_text",
])
@pytest.mark.parametrize("trigger", ["cancel", "estop"])
def test_cancel_blocks_next_action_but_allows_stop(tmp_path, capability, trigger):
    async def scenario():
        runtime = make_runtime(tmp_path, manifest=CANCELLATION_MANIFEST)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = {
                "task_id": "between-calls",
                "device_id": "arm-01",
                "status": "executing",
                "status_display": "executing",
                "cancel_requested": False,
            }
            leased_task(runtime, task, CANCELLATION_MANIFEST)
            if trigger == "cancel":
                await runtime.cancel_task(task["task_id"], op_context())
            else:
                runtime.store.kv_set("estop_active", "1")
            ctx = StepContext(runtime, task, CANCELLATION_MANIFEST)
            arm = runtime.devices.driver("arm-01")
            before = list(arm.action_log)
            with pytest.raises(DriverError) as exc:
                await ctx.call_capability(capability, {
                    "target_id": "input_box", "position_id": "work", "text": "not sent",
                    "start": {"x": 10, "y": 10}, "end": {"x": 20, "y": 20},
                })
            assert exc.value.category == "driver_stopped"
            assert arm.action_log == before
            assert runtime.devices.input_driver("arm-01").typed_text() == ""
            assert (await ctx.call_capability("arm.stop"))["stopped"] is True
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
