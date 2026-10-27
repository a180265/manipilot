"""执行链收集三缺口回归（2026-10-28 调优缺口）。

只读审计发现的三个缺口，调优时都缺数据：
1) 失败步骤 step_log.detail 恒为 {}（插件没给 detail 时无兜底）；
2) capability_call 审计 detail 恒空，看不到调了什么参数；
3) 证据记录没有结构化步骤键，只能靠 label 中文串反推步骤号。

本文件锁死三条：失败现场兜底、参数摘要不落明文、证据带步骤归属。
"""
import asyncio
from copy import deepcopy

from runtime.executor import TaskExecutor
from runtime.plugin_capabilities import capability_params_digest
from tasks.plugin_api import TaskPlugin, make_step
from tests.helpers import (
    PROBE_MANIFEST,
    make_device_configs,
    make_runtime,
    probe_payload,
)


def test_failure_context_fills_missing_detail():
    task = {
        "device_id": "arm-01",
        "error_category": "device_offline",
        "capability_call_count": 6,
        "action_call_count": 2,
        "step_log": [
            {"step_no": 4, "phase": "started", "at": "2026-10-27T20:27:22.530Z"},
            {"step_no": 4, "phase": "done", "outcome": "success"},
            {"step_no": 5, "phase": "started", "at": "2026-10-27T20:27:25.543Z"},
        ],
    }
    step = {"step_no": 5, "action": "input_text", "is_action": True}
    # 兜底函数不依赖实例状态，未绑定调用即可验证。
    detail = TaskExecutor._failure_context(None, task, step, {"outcome": "failed"})
    context = detail["failure_context"]
    assert context["step_no"] == 5 and context["action"] == "input_text"
    assert context["is_action"] is True
    assert context["device_id"] == "arm-01"
    assert context["error_category"] == "device_offline"
    assert context["capability_call_count"] == 6 and context["action_call_count"] == 2
    assert context["plugin_supplied_detail"] is False
    # 用该步 started 算耗时，而不是整任务耗时。
    assert context["elapsed_ms"] is not None and context["elapsed_ms"] >= 0


def test_failure_context_tolerates_missing_started_entry():
    detail = TaskExecutor._failure_context(
        None, {"device_id": "arm-01", "step_log": []},
        {"step_no": 1, "action": "check_session"}, {"outcome": "unknown"})
    assert detail["failure_context"]["elapsed_ms"] is None
    assert detail["failure_context"]["outcome"] == "unknown"


def test_capability_params_digest_keeps_shape_not_plaintext():
    params = {"text": "澳门可以拍～", "target_id": "input_box", "attempts": [1, 2]}
    digest = capability_params_digest(params)
    assert digest["keys"] == ["attempts", "target_id", "text"]
    assert digest["bytes"] > 0 and len(digest["sha256_8"]) == 8
    assert "澳门" not in str(digest)  # 消息正文不得进审计库
    assert capability_params_digest(params) == digest  # 同参稳定
    assert capability_params_digest({"text": "别的"})["sha256_8"] != digest["sha256_8"]
    assert capability_params_digest(None) == {"keys": [], "bytes": 0, "sha256_8": ""}
    assert capability_params_digest("not-a-dict")["keys"] == []


def test_capture_screenshot_records_step_attribution(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            camera = runtime.devices.camera_broker("arm-01")
            record = runtime.evidence.capture_screenshot(
                device_id="arm-01", camera=camera, task_id="step-attribution",
                label="步骤5取证：通过机械臂绑定输入法输入原文",
                observation_context={"step_no": 5, "step_action": "input_text"},
            )
            assert record["observation_context"] == {
                "step_no": 5, "step_action": "input_text"}
            assert record["task_id"] == "step-attribution"
            assert record["kind"] == "screenshot"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_capture_screenshot_without_context_stays_compatible(tmp_path):
    """老调用点（任务结束取证/取消取证）不传步骤，记录不得凭空长字段。"""

    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            camera = runtime.devices.camera_broker("arm-01")
            record = runtime.evidence.capture_screenshot(
                device_id="arm-01", camera=camera, task_id="legacy",
                label="任务结束取证")
            assert "observation_context" not in record
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


class FailingProbe(TaskPlugin):
    """第 2 步明确失败且不返回 detail，用于验证执行器兜底真的接线。"""

    def __init__(self):
        self.executed: list[int] = []

    async def validate(self, task, device_context):
        return {"ok": True}

    async def plan(self, task, device_context):
        return [
            make_step(1, "probe_1", "测试步骤1", is_action=False,
                      required_capabilities=[], evidence_required=False, params={}),
            make_step(2, "probe_2", "测试步骤2", is_action=True,
                      required_capabilities=[], evidence_required=False, params={}),
        ]

    async def execute_step(self, step, ctx):
        self.executed.append(step["step_no"])
        if step["step_no"] == 2:
            return {"outcome": "failed", "message": "机械臂绑定的手机输入法未就绪"}
        return {"outcome": "success", "message": "测试步骤完成"}

    async def cleanup(self, ctx):
        return {"cleaned": True}


def test_failed_step_records_execution_context_end_to_end(tmp_path):
    async def scenario():
        plugin = FailingProbe()
        runtime = make_runtime(tmp_path, plugin=plugin, manifest=deepcopy(PROBE_MANIFEST))
        await runtime.devices.register_all(make_device_configs())
        task = runtime.submit_task(probe_payload([]), source="console", context={})
        lease = runtime.leases.acquire("arm-01", task["task_id"])
        task.update(status="awaiting_device", lease_id=lease["lease_id"])
        runtime.store.save_task(task)
        runtime.devices._set_status("arm-01", "busy", "测试任务取得租约")
        try:
            result = await runtime.executor.execute(task["task_id"])
            done = [e for e in result["step_log"] if e.get("phase") == "done"]
            assert [e["outcome"] for e in done] == ["success", "failed"]
            # 成功步保留插件 detail 口径（空即空），失败步必须有现场。
            assert done[0]["detail"] == {}
            context = done[1]["detail"]["failure_context"]
            assert context["step_no"] == 2 and context["action"] == "probe_2"
            assert context["is_action"] is True
            assert context["device_id"] == "arm-01"
            assert context["plugin_supplied_detail"] is False
            assert context["elapsed_ms"] is not None
            assert result["status"] == "failed"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
