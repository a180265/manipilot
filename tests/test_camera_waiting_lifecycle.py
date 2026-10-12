"""Waiting before any plan must not run cleanup or invalidate a safe device."""
import asyncio
from copy import deepcopy

import pytest

from tests.helpers import (
    ScriptedProbePlugin, make_device_configs, make_runtime, probe_payload,
)


class WaitingProbe(ScriptedProbePlugin):
    def validate_preconditions(self, task, device_context, **kwargs):
        if device_context.get("camera_quality") == "waiting":
            return {"ok": False, "error_category": "device_not_eligible",
                    "message": "camera_waiting"}
        return {"ok": True}


async def leased_waiting_task(tmp_path):
    plugin = WaitingProbe()
    runtime = make_runtime(tmp_path, plugin=plugin)
    await runtime.devices.register_all(make_device_configs())
    task = runtime.submit_task(probe_payload([{}]), source="console", context={})
    lease = runtime.leases.acquire("arm-01", task["task_id"])
    task.update(status="awaiting_device", lease_id=lease["lease_id"])
    runtime.store.save_task(task)
    device = runtime.devices.get("arm-01")
    device.update(status="busy", camera_quality="waiting", device_mode="managed")
    runtime.devices.camera("arm-01").set_quality("waiting")
    return runtime, plugin, task


@pytest.mark.parametrize("during_validation", [False, True])
def test_waiting_before_plan_finishes_without_cleanup_or_device_fault(tmp_path, during_validation):
    async def scenario():
        runtime, plugin, task = await leased_waiting_task(tmp_path)
        device = runtime.devices.get("arm-01")
        if during_validation:
            device["camera_quality"] = "clear"
            runtime.devices.camera("arm-01").set_quality("clear")

            async def validate(_task, _device):
                device["camera_quality"] = "waiting"
                runtime.devices.camera("arm-01").set_quality("waiting")
                return {"ok": True}

            plugin.validate = validate
        preserved = {key: deepcopy(device.get(key)) for key in
                     ("home_ok", "calibration_status", "daemon_session", "device_mode")}
        driver = runtime.devices.driver("arm-01")
        before = deepcopy(driver.action_log)
        result = await runtime.executor.execute(task["task_id"])
        assert result["status"] == "failed"
        assert result["error_category"] == "camera_waiting"
        assert result["motion_not_started"] is True
        assert result["home_verified"] is False
        assert not result.get("plan")
        assert not result.get("step_log")
        assert driver.action_log == before
        assert plugin.executed_tasks == []
        assert not runtime.store.all_alerts()
        assert runtime.leases.get("arm-01") is None
        assert device["status"] == "ready"
        assert {key: device.get(key) for key in preserved} == preserved
        assert runtime.devices.is_taskable("arm-01") == (False, "camera_waiting")
        device["camera_quality"] = "clear"
        runtime.devices.camera("arm-01").set_quality("clear")
        assert runtime.devices.is_taskable("arm-01") == (True, None)
        await runtime._dispatch_once()
        assert plugin.executed_tasks == [], "the rejected task must not be replayed"
        assert runtime.get_task(task["task_id"])["status"] == "failed"

    asyncio.run(scenario())


def test_zero_motion_wait_reports_wx_without_claiming_home_or_retaining_lease(tmp_path, monkeypatch):
    async def scenario():
        runtime, _, task = await leased_waiting_task(tmp_path)
        task["source"] = "wx"
        runtime.store.save_task(task)
        reported = []

        async def report(result):
            # 二阶段路径（资格拒绝）租约已释放；home 状态不再断言 True
            #（零动作任务不 claim 归位）。
            assert runtime.leases.get("arm-01") is None
            reported.append(deepcopy(result))

        monkeypatch.setattr(runtime.reporter, "send_task_result", report)
        await runtime.executor.execute(task["task_id"])
        # A6（第一批次）：首次暗屏=退队+代提唤醒一次（不再当场失败）
        first = runtime.store.get_task(task["task_id"])
        assert first["status"] == "queued" and first.get("wake_retried") is True
        # 设备仍暗屏：第二次执行按资格拒绝失败并回报（任务已非 untouched，
        # 走 validate/eligibility 拒绝路径而非零动作等待收尾）
        runtime.devices._set_status("arm-01", "busy", "暗屏重试")
        await runtime.executor.execute(task["task_id"])
        assert len(reported) == 1
        assert reported[0]["status"] == "failed"
        assert reported[0]["error_category"] == "device_not_eligible"
        assert "camera_waiting" in (reported[0].get("result_message")
                                    or reported[0].get("message") or "")
        # 安全后置：租约不残留，无动作发生
        assert runtime.leases.get("arm-01") is None
        final = runtime.store.get_task(task["task_id"])
        assert not any(entry.get("is_action") for entry in final.get("step_log", []))

    asyncio.run(scenario())


@pytest.mark.parametrize("uncertain_field,value", [
    ("step_log", [{"step_no": 1, "phase": "started", "is_action": True}]),
    ("action_call_count", 1),
    ("capability_call_count", 1),
    ("plan", [{"step_no": 1, "is_action": True}]),
])
def test_waiting_does_not_claim_zero_motion_from_incomplete_history(tmp_path, uncertain_field, value):
    async def scenario():
        runtime, _, task = await leased_waiting_task(tmp_path)
        task[uncertain_field] = value
        runtime.store.save_task(task)
        result = await runtime.executor.execute(task["task_id"])
        assert not result.get("motion_not_started")
        assert runtime.leases.get("arm-01") is not None
        assert not runtime.devices.is_taskable("arm-01")[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"status": "paused"},
    {"estop_latched": True},
    {"home_ok": False},
    {"calibration_status": "due"},
    {"binding_enabled": False},
])
def test_waiting_does_not_clear_other_safety_blockers(tmp_path, change):
    async def scenario():
        runtime, _, task = await leased_waiting_task(tmp_path)
        runtime.devices.get("arm-01").update(change)
        result = await runtime.executor.execute(task["task_id"])
        assert not result.get("motion_not_started")
        assert runtime.leases.get("arm-01") is not None
        assert not runtime.devices.is_taskable("arm-01")[0]

    asyncio.run(scenario())
