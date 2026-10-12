"""插件能力边界回归：只使用临时数据库和仿真设备。"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from tests.helpers import (
    PROBE_MANIFEST, ScriptedProbePlugin, make_device_configs, make_runtime,
    probe_payload, wait_terminal,
)


def test_plugin_task_snapshot_cannot_redirect_device_actions_or_private_state(tmp_path):
    class RedirectingPlugin(ScriptedProbePlugin):
        async def execute_step(self, step, ctx):
            ctx.task["device_id"] = "arm-02"
            ctx.task["payload"]["steps"].clear()
            ctx.state_set("selected", "original-device")
            await ctx.call_capability("arm.home")
            return {"outcome": "success", "message": "完成"}

    async def scenario():
        runtime = make_runtime(tmp_path, 2, plugin=RedirectingPlugin())
        await runtime.startup(device_configs=make_device_configs(2))
        try:
            other_arm = runtime.devices.driver("arm-02")
            before = list(other_arm.action_log)
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed"
            assert done["device_id"] == "arm-01" and done["payload"]["steps"] == [{}]
            assert other_arm.action_log == before
            assert runtime.plugin_state_get(
                PROBE_MANIFEST["plugin_id"], "arm-01", "selected") == "original-device"
            assert runtime.plugin_state_get(
                PROBE_MANIFEST["plugin_id"], "arm-02", "selected") is None
            assert runtime.leases.get("arm-01") is None
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_action_budget_is_enforced_even_if_plugin_changes_its_snapshot(tmp_path):
    class BudgetProbe(ScriptedProbePlugin):
        async def execute_step(self, step, ctx):
            await ctx.call_capability("camera.capture")
            await ctx.call_capability("arm.home")
            ctx.task["action_call_count"] = 0
            await ctx.call_capability("arm.home")
            return {"outcome": "success", "message": "不应到达"}

    async def scenario():
        manifest = deepcopy(PROBE_MANIFEST)
        manifest["limits"]["max_actions_per_task"] = 1
        runtime = make_runtime(tmp_path, plugin=BudgetProbe(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "failed"
            assert done["error_category"] == "capability_not_allowed"
            assert "上限" in done["failure_reason"]
            assert done["action_call_count"] == 1
            calls = [event["reason"] for event in runtime.audit.list_events()
                     if event["action"] == "capability_call"]
            assert calls.count("能力调用：arm.home") == 1
            assert "能力调用：arm.stop" in calls, "动作超限不能阻止安全停止"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("resource,capability", [
    ("arm", "arm.home"), ("camera", "camera.capture"), ("input", "input.type_text"),
])
def test_declared_capability_cannot_use_an_undeclared_resource(tmp_path, resource, capability):
    class ResourceProbe(ScriptedProbePlugin):
        async def execute_step(self, step, ctx):
            await ctx.call_capability(capability, {"text": "禁止输入"})
            return {"outcome": "success", "message": "不应到达"}

    async def scenario():
        manifest = deepcopy(PROBE_MANIFEST)
        manifest["capabilities"].append("input.type_text")
        manifest["resources"][resource] = False
        runtime = make_runtime(tmp_path, plugin=ResourceProbe(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "failed"
            assert done["error_category"] == "capability_not_allowed"
            assert "资源" in done["failure_reason"]
            assert runtime.devices.input_driver("arm-01").typed_text() == ""
            assert not [event for event in runtime.audit.list_events()
                        if event["action"] == "capability_call"
                        and capability in event["reason"]]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("lease_state", ["missing", "other_task", "expired"])
def test_plugin_cannot_act_after_its_lease_is_lost(tmp_path, lease_state):
    class WaitingProbe(ScriptedProbePlugin):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def execute_step(self, step, ctx):
            self.entered.set()
            await self.release.wait()
            await ctx.call_capability("arm.home")
            return {"outcome": "success", "message": "不应到达"}

    async def scenario():
        plugin = WaitingProbe()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            await asyncio.wait_for(plugin.entered.wait(), 2)
            lease = runtime.leases.get("arm-01")
            runtime.leases.release("arm-01", lease["lease_id"])
            if lease_state == "other_task":
                runtime.leases.acquire("arm-01", "other-task")
            elif lease_state == "expired":
                expired = runtime.leases.acquire("arm-01", task["task_id"], ttl_seconds=-1)
                # Preserve the original grant ID so this specifically tests expiry.
                expired["lease_id"] = lease["lease_id"]
                runtime.store.save_lease(expired)
            plugin.release.set()
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "failed"
            assert done["error_category"] == "capability_not_allowed"
            assert "租约" in done["failure_reason"]
            assert not [event for event in runtime.audit.list_events()
                        if event["action"] == "capability_call"
                        and event["reason"] == "能力调用：arm.home"]
        finally:
            plugin.release.set()
            await runtime.shutdown()

    asyncio.run(scenario())


def test_capture_helper_cannot_bypass_camera_resource_permission(tmp_path):
    class CaptureProbe(ScriptedProbePlugin):
        async def execute_step(self, step, ctx):
            await ctx.capture_evidence("插件申请截图")
            return {"outcome": "success", "message": "不应到达"}

    async def scenario():
        manifest = deepcopy(PROBE_MANIFEST)
        manifest["resources"]["camera"] = False
        runtime = make_runtime(tmp_path, plugin=CaptureProbe(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "failed"
            assert done["error_category"] == "capability_not_allowed"
            assert "资源" in done["failure_reason"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["validate", "plan"])
def test_lifecycle_parameters_cannot_change_canonical_task_or_device(tmp_path, phase):
    class LifecycleProbe(ScriptedProbePlugin):
        def mutate(self, task, device_context):
            task["device_id"] = "arm-02"
            task["payload"]["text"] = "替换正文"
            device_context["status"] = "offline"

        async def validate(self, task, device_context):
            if phase == "validate":
                self.mutate(task, device_context)
            return {"ok": True}

        async def plan(self, task, device_context):
            if phase == "plan":
                self.mutate(task, device_context)
            return await super().plan(task, device_context)

        async def execute_step(self, step, ctx):
            await ctx.call_capability("arm.home")
            return {"outcome": "success", "message": "完成"}

    async def scenario():
        runtime = make_runtime(tmp_path, 2, plugin=LifecycleProbe())
        await runtime.startup(device_configs=make_device_configs(2))
        try:
            other_actions = list(runtime.devices.driver("arm-02").action_log)
            payload = probe_payload([{}])
            payload["payload"]["text"] = "原文"
            task = runtime.submit_task(
                payload, source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed"
            assert done["device_id"] == "arm-01" and done["payload"]["text"] == "原文"
            assert runtime.devices.get("arm-01")["status"] == "ready"
            assert runtime.devices.get("arm-02")["status"] == "ready"
            assert runtime.devices.driver("arm-02").action_log == other_actions
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["failure", "cancel"])
@pytest.mark.parametrize("replacement", ["new_task", "new_grant"])
def test_old_executor_does_not_clean_up_or_change_new_lease_owner(tmp_path, finish, replacement):
    class HeldProbe(ScriptedProbePlugin):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.recovery_calls = self.cleanup_calls = self.cancel_calls = 0

        async def execute_step(self, step, ctx):
            self.entered.set()
            await self.release.wait()
            return {"outcome": "failed" if finish == "failure" else "success",
                    "error_category": "driver_error", "message": "受控结束"}

        async def recover(self, failure, ctx):
            self.recovery_calls += 1
            return await super().recover(failure, ctx)

        async def cleanup(self, ctx):
            self.cleanup_calls += 1
            return await super().cleanup(ctx)

        async def cancel(self, ctx):
            self.cancel_calls += 1
            return await super().cancel(ctx)

    async def scenario():
        from tests.helpers import op_context

        plugin = HeldProbe()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            await asyncio.wait_for(plugin.entered.wait(), 2)
            old_lease = runtime.leases.get("arm-01")
            runtime.leases.release("arm-01", old_lease["lease_id"])
            new_lease = runtime.leases.acquire(
                "arm-01", "new-task" if replacement == "new_task" else task["task_id"])
            arm = runtime.devices.driver("arm-01")
            before = list(arm.action_log)
            if finish == "cancel":
                await runtime.cancel_task(task["task_id"], op_context())
            plugin.release.set()
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] in {"failed", "unknown"}
            assert arm.action_log == before, "旧任务不能执行停止、归位或清理新任务设备"
            assert plugin.recovery_calls == plugin.cleanup_calls == plugin.cancel_calls == 0
            assert runtime.leases.get("arm-01") == new_lease
            assert runtime.devices.get("arm-01")["status"] == "busy"
            assert done["home_check"]["home_ok"] is False
        finally:
            plugin.release.set()
            await runtime.shutdown()

    asyncio.run(scenario())


def test_lease_replaced_while_cancel_hook_waits_cannot_trigger_fallback_stop(tmp_path):
    class CancelWaitProbe(ScriptedProbePlugin):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelling = asyncio.Event()
            self.finish_cancel = asyncio.Event()

        async def execute_step(self, step, ctx):
            self.entered.set()
            await self.release.wait()
            return {"outcome": "success", "message": "完成"}

        async def cancel(self, ctx):
            self.cancelling.set()
            await self.finish_cancel.wait()
            return {"stopped": True, "message": "插件钩子返回"}

    async def scenario():
        from tests.helpers import op_context

        plugin = CancelWaitProbe()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            await asyncio.wait_for(plugin.entered.wait(), 2)
            await runtime.cancel_task(task["task_id"], op_context())
            plugin.release.set()
            await asyncio.wait_for(plugin.cancelling.wait(), 2)
            lease = runtime.leases.get("arm-01")
            runtime.leases.release("arm-01", lease["lease_id"])
            runtime.leases.acquire("arm-01", "new-task")
            arm = runtime.devices.driver("arm-01")
            before = list(arm.action_log)
            plugin.finish_cancel.set()
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "unknown"
            assert arm.action_log == before
            assert runtime.devices.get("arm-01")["status"] == "busy"
        finally:
            plugin.release.set()
            plugin.finish_cancel.set()
            await runtime.shutdown()

    asyncio.run(scenario())


def test_plugin_must_declare_assigned_device_scope(tmp_path):
    class DeviceScopeProbe(ScriptedProbePlugin):
        async def execute_step(self, step, ctx):
            await ctx.call_capability("arm.home")
            return {"outcome": "success", "message": "不应到达"}

    async def scenario():
        manifest = deepcopy(PROBE_MANIFEST)
        manifest["resources"]["devices"] = {}
        runtime = make_runtime(tmp_path, plugin=DeviceScopeProbe(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "failed"
            assert "设备范围" in done["failure_reason"]
            assert not [event for event in runtime.audit.list_events()
                        if event["action"] == "capability_call"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("write", ["private_state", "ui_state"])
def test_late_context_write_cannot_modify_replacement_owner(tmp_path, write):
    class LateWriteProbe(ScriptedProbePlugin):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def execute_step(self, step, ctx):
            self.entered.set()
            await self.release.wait()
            if write == "private_state":
                ctx.state_set("session", "stale")
            else:
                await ctx.update_observed_ui_state("conversation", .99)
            return {"outcome": "success", "message": "不应到达"}

    async def scenario():
        plugin = LateWriteProbe()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "测试"})
            await asyncio.wait_for(plugin.entered.wait(), 2)
            lease = runtime.leases.get("arm-01")
            runtime.leases.release("arm-01", lease["lease_id"])
            runtime.leases.acquire("arm-01", "replacement")
            runtime.plugin_state_set(PROBE_MANIFEST["plugin_id"], "arm-01", "session", "new")
            before_ui = runtime.devices.get("arm-01")["current_ui_state"]
            plugin.release.set()
            done = await wait_terminal(runtime, task["task_id"])
            assert runtime.plugin_state_get(
                PROBE_MANIFEST["plugin_id"], "arm-01", "session") == "new"
            assert runtime.devices.get("arm-01")["current_ui_state"] == before_ui
            assert done["status"] == "failed"
        finally:
            plugin.release.set()
            await runtime.shutdown()

    asyncio.run(scenario())
