"""阶段 C 验收测试：证据与哈希、权限、审计、错误分类、隔离、归位、急停。

退出条件：故障进入人工路径；急停停止新任务；证据可追溯。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime.errors import ApiError
from tests.helpers import (
    ScriptedProbePlugin,
    make_device_configs,
    make_runtime,
    op_context,
    probe_payload,
    wait_terminal,
)


def test_evidence_traceable_with_hash(tmp_path):
    """证据可追溯：截图落盘、SHA-256 哈希一致、按任务可查。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            task = runtime.submit_task(
                probe_payload([{"action_display": "动作步骤", "is_action": True}]),
                source="console", context={"operator_id": "操作员甲"},
            )
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed"
            assert len(done.get("evidence_ids", [])) >= 1
            evidence_list = runtime.evidence.for_task(done["task_id"])
            assert evidence_list
            for record in evidence_list:
                from pathlib import Path

                file_path = tmp_path / "evidence" / record["file_path"]
                assert file_path.exists(), "证据文件必须落盘"
                from runtime.util import sha256_bytes

                assert sha256_bytes(file_path.read_bytes()) == record["sha256"], "证据哈希必须与文件一致"
                assert record["label"], "证据必须带中文说明"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_permission_roles_enforced(tmp_path):
    """权限：只读拒绝写操作；急停需要应急角色；危险操作需要二次确认。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            # 只读角色不能暂停设备
            with pytest.raises(ApiError) as exc_info:
                runtime.op_pause("arm-01", op_context(role="readonly"))
            assert exc_info.value.key == "permission_denied"

            # 运维可以暂停，但隔离是危险操作，缺二次确认会被拒绝
            runtime.op_pause("arm-01", op_context())
            runtime.op_resume("arm-01", op_context())
            with pytest.raises(ApiError) as exc_info:
                runtime.op_quarantine("arm-01", op_context())
            assert exc_info.value.key == "confirmation_required"

            # 运维 + 二次确认：允许隔离
            runtime.op_quarantine("arm-01", op_context(confirmation_token="确认执行"))
            assert runtime.devices.get("arm-01")["status"] == "quarantined"

            # 急停需要应急角色
            with pytest.raises(ApiError) as exc_info:
                runtime.op_estop(op_context(role="operator", confirmation_token="确认执行"))
            assert exc_info.value.key == "permission_denied"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_operation_missing_fields(tmp_path):
    """写操作必须带操作者、原因、关联编号和幂等请求编号。"""
    from runtime.permissions import check_operation

    with pytest.raises(ApiError) as exc_info:
        check_operation("device_pause", {"operator_id": "", "reason": "原因", "correlation_id": "c", "client_request_id": "r"})
    assert exc_info.value.key == "missing_field"

    with pytest.raises(ApiError) as exc_info:
        check_operation("device_pause", {"operator_id": "甲", "reason": "中文原因", "correlation_id": "", "client_request_id": ""})
    assert exc_info.value.key == "missing_field"


@pytest.mark.parametrize("reason", ["ok", "123", "!@#$", "確認済み", "确认正常"])
def test_operation_reason_accepts_any_language(reason):
    from runtime.permissions import check_operation

    check_operation("device_pause", op_context(reason=reason))


@pytest.mark.parametrize("reason", ["", " ", "\t\n", None])
def test_operation_reason_rejects_empty_text(reason):
    from runtime.permissions import check_operation

    with pytest.raises(ApiError) as exc_info:
        check_operation("device_pause", op_context(reason=reason))
    assert exc_info.value.key == "missing_field"


@pytest.mark.parametrize("reason", [123, True, ["ok"], {"text": "ok"}])
def test_operation_reason_rejects_non_text(reason):
    from runtime.permissions import check_operation

    with pytest.raises(ApiError) as exc_info:
        check_operation("device_pause", op_context(reason=reason))
    assert exc_info.value.key == "invalid_request"


def test_audit_records_chinese_operations(tmp_path):
    """审计：操作、操作者、操作原因、结果可追溯。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            context = op_context(operator_id="操作员甲", reason="现场巡检，暂停设备进行例行检查")
            runtime.op_pause("arm-01", context)
            runtime.op_resume("arm-01", op_context(operator_id="操作员乙"))
            events = runtime.audit.list_events(limit=50)
            pause_events = [e for e in events if e["action"] == "device_pause"]
            assert pause_events
            event = pause_events[0]
            assert event["operator_id"] == "操作员甲"
            assert event["action_display"] == "暂停设备"
            assert event["outcome_display"] == "成功"
            assert "暂停" in event["reason"]

            # 被拒绝的操作也留审计痕迹（权限拒绝通过 check_operation 抛出前不产生，
            # 但重复幂等提交、插件越权等拦截必须留痕）
            plugin = ScriptedProbePlugin()
            runtime.plugins.register_instance_for_tests(plugin, plugin.manifest)
            key = "dup-audit-001"
            payload = probe_payload([{"action_display": "步骤一"}], key=key)
            runtime.submit_task(payload, source="console", context={"operator_id": "操作员甲"})
            await wait_terminal(runtime, runtime.list_tasks()[0]["task_id"])
            runtime.submit_task(payload, source="console", context={"operator_id": "操作员甲"})
            blocked = [e for e in runtime.audit.list_events(limit=50) if e["action"] == "task_duplicate_blocked"]
            assert blocked and "重复" in blocked[0]["reason"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_error_classification_structured(tmp_path):
    """错误分类：驱动错误带结构化类别和中文说明，任务记录错误类别。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            task = runtime.submit_task(
                probe_payload([{"action_display": "失败步骤", "outcome": "failed",
                                "error_category": "driver_motion_failed", "message": "运动超出软限位"}]),
                source="console", context={"operator_id": "操作员甲"},
            )
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "failed"
            assert done["status_display"] == "明确失败"
            assert done["error_category"] == "driver_motion_failed"
            assert "软限位" in done["failure_reason"]
            # 设备归位后仍可用
            assert runtime.devices.get("arm-01")["status"] == "ready"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_quarantine_blocks_tasks_and_manual_unquarantine(tmp_path):
    """隔离：隔离设备不领取任务；人工解除隔离走恢复检查。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            runtime.op_quarantine("arm-01", op_context(confirmation_token="确认执行"))
            task = runtime.submit_task(probe_payload([{"action_display": "步骤一"}]), source="console", context={"operator_id": "操作员甲"})
            await asyncio.sleep(0.4)
            assert runtime.get_task(task["task_id"])["status"] == "queued", "隔离设备不得领取任务"

            # 解除隔离是危险操作，需要二次确认；之后进入恢复中并完成归位检查
            with pytest.raises(ApiError):
                runtime.op_unquarantine("arm-01", op_context())
            runtime.op_unquarantine("arm-01", op_context(confirmation_token="确认执行"))
            assert runtime.devices.get("arm-01")["status"] == "recovering"
            await runtime.devices.finish_recovering("arm-01")
            assert runtime.devices.get("arm-01")["status"] == "ready"
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_home_failure_blocks_next_task(tmp_path):
    """归位失败：设备不能领取下一个任务，进入人工路径。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            runtime.driver = runtime.devices.driver  # 便于阅读
            arm = runtime.devices.driver("arm-01")
            arm.faults["fail_home"] = True
            task = runtime.submit_task(probe_payload([{"action_display": "步骤一"}]), source="console", context={"operator_id": "操作员甲"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed"  # 步骤本身成功
            assert done["home_check"]["home_ok"] is False, "归位失败必须记录"
            device = runtime.devices.get("arm-01")
            assert device["status"] == "degraded" or device["home_ok"] is False
            # 归位未确认：新任务不能领取
            followup = runtime.submit_task(probe_payload([{"action_display": "步骤二"}]), source="console", context={"operator_id": "操作员甲"})
            await asyncio.sleep(0.4)
            assert runtime.get_task(followup["task_id"])["status"] == "queued", "归位失败后不能领取新任务"
            assert any(i["kind"] == "home_not_confirmed" for i in runtime.takeovers()), "归位失败必须进入人工处理"
            # 人工确认归位后恢复
            arm.faults.clear()
            runtime.op_reset_home("arm-01", op_context(confirmation_token="确认执行"))
            await asyncio.sleep(0.4)
            assert runtime.get_task(followup["task_id"])["status"] in ("awaiting_device", "validating", "executing", "confirmed")
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_emergency_stop_blocks_new_tasks(tmp_path):
    """急停：停止领取新任务；运行任务走停止/清理路径；复位后恢复。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            running = runtime.submit_task(
                probe_payload([{"action_display": "长步骤", "sleep": 0.8}]),
                source="console", context={"operator_id": "操作员甲"},
            )
            await asyncio.sleep(0.25)
            assert runtime.get_task(running["task_id"])["status"] == "executing"

            result = runtime.op_estop(op_context(role="responder", confirmation_token="确认执行"))
            assert result["estop_active"] is True
            assert runtime.estop.active

            done = await wait_terminal(runtime, running["task_id"])
            assert done["status"] in ("cancelled", "unknown"), "急停时运行任务必须停止"

            queued = runtime.submit_task(probe_payload([{"action_display": "步骤一"}]), source="console", context={"operator_id": "操作员甲"})
            await asyncio.sleep(0.4)
            assert runtime.get_task(queued["task_id"])["status"] == "queued", "急停后不得领取新任务"

            # 复位需要应急角色 + 二次确认
            with pytest.raises(ApiError):
                await runtime.op_estop_reset(op_context(role="operator", confirmation_token="确认执行"))
            await runtime.op_estop_reset(op_context(role="responder", confirmation_token="确认执行"))
            assert not runtime.estop.active
            await asyncio.sleep(0.2)
            assert runtime.get_task(queued["task_id"])["status"] == "queued"
            # Resetting the stop lock is not proof of the interrupted task's safe position.
            runtime.op_reset_home("arm-01", op_context(confirmation_token="确认执行"))
            runtime.op_resume("arm-01", op_context())
            done2 = await wait_terminal(runtime, queued["task_id"])
            assert done2["status"] == "confirmed", "复位并人工确认安全位置后任务恢复执行"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_operation_dedup_by_client_request_id(tmp_path):
    """运维操作幂等：同一 client_request_id 不重复执行。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            context = op_context()
            first = runtime.op_pause("arm-01", context)
            second = runtime.op_pause("arm-01", context)
            assert first["device_id"] == second["device_id"]
            events = [e for e in runtime.audit.list_events(limit=50) if e["action"] == "device_pause"]
            assert len(events) == 1, "同一幂等请求编号只应产生一次操作审计"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_camera_fault_alerts(tmp_path):
    """摄像头遮挡/冻结必须被识别并告警。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            camera = runtime.devices.camera("arm-01")
            camera.set_quality("blocked")
            await asyncio.sleep(2.5)  # 等待摄像头巡检周期
            device = runtime.devices.get("arm-01")
            assert device["camera_quality"] == "blocked"
            assert device["camera_quality_display"] == "遮挡"
            alerts = runtime.store.all_alerts()
            assert any("遮挡" in a["message"] for a in alerts), "摄像头异常必须产生告警"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
