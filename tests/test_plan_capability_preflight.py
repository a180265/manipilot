"""计划权限预检：隔离数据库、仿真设备，不访问现场服务或硬件。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import pytest

from tests.helpers import (
    PROBE_MANIFEST, make_device_configs, make_runtime, probe_payload,
)
from tasks.plugin_api import TaskPlugin, make_step


class PlanProbe(TaskPlugin):
    def __init__(self, steps):
        self.steps = steps
        self.executed_steps = []
        self.hooks = []

    async def validate(self, task, device_context):
        return {"ok": True}

    async def plan(self, task, device_context):
        return deepcopy(self.steps)

    async def execute_step(self, step, ctx):
        self.executed_steps.append(step["step_no"])
        for capability, params in step["params"].get("calls", []):
            await ctx.call_capability(capability, params)
        return {"outcome": "success", "message": "测试步骤完成"}

    async def recover(self, failure, ctx):
        self.hooks.append("recover")
        return await super().recover(failure, ctx)

    async def cleanup(self, ctx):
        self.hooks.append("cleanup")
        if not ctx.task.get("read_only_task"):
            await ctx.call_capability("arm.home")
        return {"cleaned": True}


def step(number, capabilities, *, calls=(), is_action=False):
    return make_step(
        number, f"probe_{number}", f"测试步骤{number}",
        is_action=is_action, required_capabilities=capabilities,
        evidence_required=False, params={"calls": list(calls)},
    )


SWIPE = ("arm.swipe", {"start": {"x": 0.5, "y": 0.8},
                       "end": {"x": 0.5, "y": 0.2}})
MEASURE = ("camera.measure_scroll", {"op": "grab"})
CAPTURE = ("camera.capture", {})   # 身份无关能力：不需要校准档案即可执行


async def leased_task(tmp_path, steps, *, read_only=False, capabilities=()):
    manifest = deepcopy(PROBE_MANIFEST)
    manifest["capabilities"].extend(["arm.swipe", *capabilities])
    manifest["task_types"][0]["read_only_task"] = read_only
    plugin = PlanProbe(steps)
    runtime = make_runtime(tmp_path, plugin=plugin, manifest=manifest)
    await runtime.devices.register_all(make_device_configs())
    task = runtime.submit_task(probe_payload([]), source="console", context={})
    lease = runtime.leases.acquire("arm-01", task["task_id"])
    task.update(status="awaiting_device", lease_id=lease["lease_id"])
    runtime.store.save_task(task)
    runtime.devices._set_status("arm-01", "busy", "测试任务取得租约")
    return runtime, plugin, task


@pytest.mark.parametrize("later_step", [False, True], ids=["same-step", "later-step"])
def test_missing_declared_capability_rejects_entire_plan_before_any_action(tmp_path, later_step):
    async def scenario():
        if later_step:
            steps = [
                step(1, ["arm.swipe"], calls=[SWIPE], is_action=True),
                step(2, ["camera.measure_scroll"], calls=[MEASURE]),
            ]
        else:
            steps = [step(1, ["arm.swipe", "camera.measure_scroll"],
                          calls=[SWIPE, MEASURE], is_action=True)]
        runtime, plugin, task = await leased_task(tmp_path, steps)
        arm = runtime.devices.driver("arm-01")
        before = deepcopy(arm.action_log)
        registered = deepcopy(runtime.plugins.get(plugin.plugin_id)["manifest"])
        try:
            result = await runtime.executor.execute(task["task_id"])

            assert plugin.executed_steps == [], "必须在第一步之前拒绝完整计划"
            assert arm.action_log == before, "预检失败不能滑动、停止或归位"
            assert plugin.hooks == [], "预检失败不能进入恢复或清理动作"
            assert result["status"] == "failed"
            assert result["status_display"] == "明确失败"
            assert result["error_category"] == "capability_not_allowed"
            assert "camera.measure_scroll" in result["failure_reason"]
            assert result["motion_not_started"] is True
            assert not result.get("step_log")
            assert result.get("action_call_count", 0) == 0
            assert result.get("capability_call_count", 0) == 0
            assert result["home_verified"] is False
            assert runtime.leases.get("arm-01") is None
            assert runtime.devices.get("arm-01")["status"] == "ready"
            assert runtime.plugins.get(plugin.plugin_id)["manifest"] == registered
            violations = [event for event in runtime.audit.list_events()
                          if event["action"] == "plugin_violation_blocked"]
            assert any("camera.measure_scroll" in event["reason"] for event in violations)
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_declared_plan_executes_normally_without_changing_permissions(tmp_path):
    async def scenario():
        steps = [
            step(1, ["arm.swipe"], calls=[SWIPE], is_action=True),
            # 本用例验的是"计划声明的能力被预检放行后正常执行"，与 grab 能否跑通无关：
            # grab 需要校准档案与身份帧，由 test_scroll_context_chain 覆盖。
            step(2, ["camera.measure_scroll"], calls=[CAPTURE]),
        ]
        runtime, plugin, task = await leased_task(
            tmp_path, steps, capabilities=["camera.measure_scroll"])
        registered = deepcopy(runtime.plugins.get(plugin.plugin_id)["manifest"])
        try:
            result = await runtime.executor.execute(task["task_id"])
            assert result["status"] == "confirmed"
            assert result["status_display"] == "已完成"
            assert plugin.executed_steps == [1, 2]
            assert plugin.hooks == ["cleanup"]
            assert len([action for action in runtime.devices.driver("arm-01").action_log
                        if action["action"] == "swipe"]) == 1
            assert result["capability_call_count"] == 3
            assert result["action_call_count"] == 2
            assert result["evidence_ids"]
            assert runtime.leases.get("arm-01") is None
            assert runtime.plugins.get(plugin.plugin_id)["manifest"] == registered
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("capability,is_action", [
    (None, True),
    ("arm.swipe", False),
    ("arm.stop", False),
    ("input.type_text", False),
], ids=["action-flag", "arm", "stop", "input"])
def test_read_only_restrictions_still_reject_before_first_step(tmp_path, capability, is_action):
    async def scenario():
        steps = [
            step(1, ["camera.capture"], calls=[("camera.capture", {})]),
            step(2, [capability] if capability else [], is_action=is_action),
        ]
        runtime, plugin, task = await leased_task(
            tmp_path, steps, read_only=True, capabilities=["input.type_text"])
        before = deepcopy(runtime.devices.driver("arm-01").action_log)
        try:
            result = await runtime.executor.execute(task["task_id"])
            assert result["status"] == "failed"
            assert result["error_category"] == "capability_not_allowed"
            assert "只读" in result["failure_reason"]
            assert plugin.executed_steps == []
            assert runtime.devices.driver("arm-01").action_log == before
            assert runtime.devices.input_driver("arm-01").typed_text() == ""
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_declared_read_only_plan_still_succeeds_without_movement(tmp_path):
    async def scenario():
        runtime, plugin, task = await leased_task(
            tmp_path, [step(1, ["camera.capture"], calls=[("camera.capture", {})])],
            read_only=True)
        before = deepcopy(runtime.devices.driver("arm-01").action_log)
        try:
            result = await runtime.executor.execute(task["task_id"])
            assert result["status"] == "confirmed"
            assert plugin.executed_steps == [1]
            assert result["capability_call_count"] == 1
            assert result.get("action_call_count", 0) == 0
            assert runtime.devices.driver("arm-01").action_log == before
            assert runtime.leases.get("arm-01") is None
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_registered_manifest_cannot_be_overridden_by_plugin_instance(tmp_path):
    async def scenario():
        runtime, plugin, task = await leased_task(
            tmp_path, [step(1, ["camera.measure_scroll"], calls=[MEASURE])])
        plugin.manifest = deepcopy(plugin.manifest)
        plugin.manifest["capabilities"].append("camera.measure_scroll")
        try:
            result = await runtime.executor.execute(task["task_id"])
            assert result["status"] == "failed"
            assert result["error_category"] == "capability_not_allowed"
            assert plugin.executed_steps == []
            assert plugin.hooks == []
            assert "camera.measure_scroll" not in (
                runtime.plugins.get(plugin.plugin_id)["manifest"]["capabilities"])
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("omit_requirements", [False, True], ids=["empty", "absent"])
def test_preflight_does_not_infer_implicit_capabilities(tmp_path, omit_requirements):
    async def scenario():
        planned = step(1, [], calls=[MEASURE])
        if omit_requirements:
            del planned["required_capabilities"]
        runtime, plugin, task = await leased_task(tmp_path, [planned])
        try:
            result = await runtime.executor.execute(task["task_id"])
            assert plugin.executed_steps == [1], "不推断计划未显式声明的能力"
            assert result["status"] == "failed"
            assert result["error_category"] == "capability_not_allowed"
            assert "camera.measure_scroll" in result["failure_reason"]
            assert not result.get("motion_not_started")
            calls = [event["reason"] for event in runtime.audit.list_events()
                     if event["action"] == "capability_call"]
            assert "能力调用：camera.measure_scroll" not in calls
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("history", [
    {"step_log": [{"step_no": 1, "phase": "started", "is_action": True}]},
    {"action_call_count": 1},
    {"capability_call_count": 1},
    {"step_cursor": 1},
    {"stop_result_unknown": True},
], ids=["step-log", "action-count", "capability-count", "cursor", "stop-unknown"])
def test_existing_execution_history_is_not_relabelled_zero_motion(tmp_path, history):
    async def scenario():
        runtime, plugin, task = await leased_task(
            tmp_path, [step(1, ["camera.measure_scroll"], calls=[MEASURE])])
        task.update(deepcopy(history))
        runtime.store.save_task(task)
        try:
            result = await runtime.executor.execute(task["task_id"])
            assert plugin.executed_steps == []
            assert result["status"] == "unknown"
            assert result["status_display"] == "结果未知"
            assert not result.get("motion_not_started")
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_preflight_rejection_preserves_replacement_lease_and_device_state(tmp_path):
    async def scenario():
        runtime, plugin, task = await leased_task(
            tmp_path, [step(1, ["camera.measure_scroll"], calls=[MEASURE])])
        runtime.leases.release("arm-01", task["lease_id"])
        replacement = runtime.leases.acquire("arm-01", "replacement-task")
        before = deepcopy(runtime.devices.driver("arm-01").action_log)
        try:
            result = await runtime.executor.execute(task["task_id"])
            assert result["status"] == "failed"
            assert plugin.executed_steps == plugin.hooks == []
            assert runtime.devices.driver("arm-01").action_log == before
            assert runtime.leases.get("arm-01") == replacement
            assert runtime.devices.get("arm-01")["status"] == "busy"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_preflight_rejection_reports_wx_after_releasing_original_lease(tmp_path, monkeypatch):
    async def scenario():
        runtime, _, task = await leased_task(
            tmp_path, [step(1, ["camera.measure_scroll"], calls=[MEASURE])])
        task["source"] = "wx"
        runtime.store.save_task(task)
        reported = []

        async def report(result):
            assert runtime.leases.get("arm-01") is None
            reported.append(deepcopy(result))

        monkeypatch.setattr(runtime.reporter, "send_task_result", report)
        try:
            await runtime.executor.execute(task["task_id"])
            assert len(reported) == 1
            assert reported[0]["status"] == "failed"
            assert reported[0]["motion_not_started"] is True
            assert reported[0]["home_verified"] is False
            assert reported[0]["home_check"]["home_ok"] is False
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
