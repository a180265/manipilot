"""阶段 B 验收测试：队列、幂等、租约、设备锁、单设备串行、十设备并行、取消和重启恢复。

退出条件：重复请求不重复执行；过期、取消、重启恢复测试通过。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from runtime.status import TASK_STATUS, display
from runtime.util import iso, utc_now
from tests.helpers import (
    PROBE_MANIFEST,
    ScriptedProbePlugin,
    make_device_configs,
    make_runtime,
    probe_payload,
    wait_terminal,
)


async def _idempotency_scenario(tmp_path):
    plugin = ScriptedProbePlugin()
    runtime = make_runtime(tmp_path, 1, plugin=plugin)
    await runtime.startup(device_configs=make_device_configs(1))
    try:
        key = f"probe-{uuid.uuid4().hex[:8]}"
        payload = probe_payload([{"action_display": "步骤一", "sleep": 0.05}], key=key)
        task1 = runtime.submit_task(payload, source="console", context={"operator_id": "操作员甲"})
        task2 = runtime.submit_task(payload, source="console", context={"operator_id": "操作员甲"})
        assert task1["task_id"] == task2["task_id"], "同一幂等键必须返回同一任务"
        done = await wait_terminal(runtime, task1["task_id"])
        assert done["status"] == "confirmed"
        assert done["status_display"] == "已完成"
        await asyncio.sleep(0.2)
        assert len(plugin.executed_tasks) == 1, "重复请求不得重复执行"

        # 相同幂等键不同内容 -> 幂等冲突
        different = probe_payload([{"action_display": "别的步骤"}], key=key)
        with pytest.raises(Exception) as exc_info:
            runtime.submit_task(different, source="console", context={"operator_id": "操作员甲"})
        assert exc_info.value.key == "idempotency_conflict"

        blocked = [e for e in runtime.audit.list_events(limit=100) if e["action"] == "task_duplicate_blocked"]
        assert blocked, "重复提交必须留下拦截审计"
    finally:
        await runtime.shutdown()


def test_idempotency_same_key_executes_once(tmp_path):
    asyncio.run(_idempotency_scenario(tmp_path))


def test_completed_task_id_cannot_be_replaced_with_a_new_idempotency_key(tmp_path):
    from runtime.errors import ApiError

    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            first = probe_payload([{"action_display": "原任务"}], key="original-key")
            first["task_id"] = "immutable-task"
            runtime.submit_task(first, source="console", context={"operator_id": "测试"})
            original = await wait_terminal(runtime, first["task_id"])
            replacement = probe_payload([{"action_display": "替换任务"}], key="replacement-key")
            replacement["task_id"] = first["task_id"]
            with pytest.raises(ApiError) as caught:
                runtime.submit_task(replacement, source="console", context={"operator_id": "测试"})
            assert caught.value.key == "task_id_conflict"
            assert runtime.get_task(first["task_id"]) == original
            assert runtime.plugins.instance(PROBE_MANIFEST["plugin_id"]).step_executions == [
                ("immutable-task", 1)]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("conflict", ["none", "task_id", "idempotency_key"])
def test_concurrent_creation_keeps_one_immutable_task(tmp_path, monkeypatch, conflict):
    from copy import deepcopy
    from threading import Barrier
    from runtime.errors import ApiError

    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            payload = probe_payload([{"action_display": "唯一任务"}], key="concurrent-key")
            payload["task_id"] = "concurrent-task"
            second = deepcopy(payload)
            if conflict == "task_id":
                second["idempotency_key"] = "conflicting-key"
            elif conflict == "idempotency_key":
                second["payload"]["steps"][0]["action_display"] = "冲突内容"
            barrier = Barrier(2)
            original = runtime.store.find_task_by_idempotency_key

            def find(key):
                result = original(key)
                barrier.wait(timeout=5)
                return result

            monkeypatch.setattr(runtime.store, "find_task_by_idempotency_key", find)

            def submit(body):
                try:
                    return runtime.submit_task(
                        body, source="console", context={"operator_id": "测试"})
                except ApiError as exc:
                    return exc.key

            results = await asyncio.gather(
                asyncio.to_thread(submit, payload), asyncio.to_thread(submit, second))
            errors = [r for r in results if isinstance(r, str)]
            expected = {"none": [], "task_id": ["task_id_conflict"],
                        "idempotency_key": ["idempotency_conflict"]}
            assert errors == expected[conflict]
            task = await wait_terminal(runtime, "concurrent-task")
            assert task["status"] == "confirmed"
            assert len(runtime.list_tasks()) == 1
            assert runtime.plugins.instance(PROBE_MANIFEST["plugin_id"]).step_executions == [
                ("concurrent-task", 1)]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_expired_task_never_executes(tmp_path):
    """过期任务不得取得租约。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            payload = probe_payload([{"action_display": "步骤一"}])
            payload["expires_at"] = iso(utc_now() - timedelta(seconds=5))
            task = runtime.submit_task(payload, source="console", context={"operator_id": "操作员甲"})
            assert task["status"] == "expired"
            assert task["status_display"] == "已过期"
            await asyncio.sleep(0.3)
            assert plugin.executed_tasks == [], "过期任务不得执行"
            assert runtime.leases.get("arm-01") is None
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_task_expiring_while_queued_never_leases(tmp_path):
    """排队期间过期：领取租约前被拦截。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            runtime.op_pause("arm-01", {"operator_id": "操作员甲", "role": "operator", "reason": "先暂停设备",
                                        "correlation_id": "c1", "client_request_id": "r1"})
            payload = probe_payload([{"action_display": "步骤一"}])
            payload["expires_at"] = iso(utc_now() + timedelta(seconds=0.4))
            task = runtime.submit_task(payload, source="console", context={"operator_id": "操作员甲"})
            assert task["status"] == "queued"
            await asyncio.sleep(0.8)  # 设备仍暂停，任务在队列中过期
            expired = runtime.get_task(task["task_id"])
            assert expired["status"] == "expired", "无需恢复设备，排队任务也必须到期结束"
            assert expired["finished_at"]
            assert expired["attempts"] == 0
            assert not expired["step_log"]
            assert runtime.devices.get("arm-01")["status"] == "paused"
            runtime.op_resume("arm-01", {"operator_id": "操作员甲", "role": "operator", "reason": "恢复设备",
                                         "correlation_id": "c2", "client_request_id": "r2"})
            await asyncio.sleep(0.5)
            final = runtime.get_task(task["task_id"])
            assert final["status"] == "expired", f"排队过期的任务不能执行，实际：{final['status']}"
            assert plugin.executed_tasks == []
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_single_device_serial_execution(tmp_path):
    """同一设备内部严格串行。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            step = {"action_display": "耗时步骤", "sleep": 0.25}
            task1 = runtime.submit_task(probe_payload([step]), source="console", context={"operator_id": "操作员甲"})
            task2 = runtime.submit_task(probe_payload([step]), source="console", context={"operator_id": "操作员甲"})
            done1 = await wait_terminal(runtime, task1["task_id"])
            done2 = await wait_terminal(runtime, task2["task_id"])
            assert done1["status"] == "confirmed" and done2["status"] == "confirmed"
            from runtime.util import parse_iso

            start1 = parse_iso(done1["started_at"])
            end1 = parse_iso(done1["finished_at"])
            start2 = parse_iso(done2["started_at"])
            assert start2 >= end1, "同一设备的任务执行窗口不得重叠（严格串行）"
            timeline2 = [e["status"] for e in done2.get("timeline", [])]
            assert "awaiting_device" in timeline2, "第二个任务必须经历等待设备"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_expired_recovered_task_with_prior_action_stays_unknown(tmp_path):
    """已动作的恢复任务不能因等待超时变成“未执行”。"""
    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        runtime.op_pause("arm-01", {"operator_id": "操作员", "role": "operator",
                                   "reason": "模拟中断", "client_request_id": "pause",
                                   "correlation_id": "expiry-recovery"})
        task = runtime.submit_task(probe_payload([
            {"action_display": "点击", "is_action": True},
            {"action_display": "确认画面", "is_action": False},
        ]), source="console", context={"operator_id": "操作员"})
        # 持久化崩溃夹具：已有点击，正在等待可重做的观察步骤。
        task.update(status="executing", expires_at=iso(utc_now() - timedelta(seconds=1)),
                    step_cursor=1, started_at=iso(utc_now()),
                    step_log=[{"step_no": 1, "phase": "done", "is_action": True,
                               "outcome": "success"},
                              {"step_no": 2, "phase": "started", "is_action": False}])
        runtime.store.save_task(task)
        await runtime.shutdown()
        recovered = make_runtime(tmp_path, 1)
        await recovered.startup(device_configs=make_device_configs(1))
        try:
            done = await wait_terminal(recovered, task["task_id"])
            assert done["status"] == "unknown"
            assert "已发生动作" in done["uncertainty_reason"]
            assert recovered.devices.get("arm-01")["status"] == "paused"
        finally:
            await recovered.shutdown()
    asyncio.run(scenario())


def test_ten_devices_parallel_execution(tmp_path):
    """十设备并行：总耗时明显小于串行累加。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 10)
        await runtime.startup(device_configs=make_device_configs(10))
        try:
            from runtime.util import parse_iso

            step = {"action_display": "并行步骤", "sleep": 0.4}
            tasks = []
            for index in range(1, 11):
                device_id = f"arm-{index:02d}"
                tasks.append(runtime.submit_task(probe_payload([step], device_id=device_id), source="console", context={"operator_id": "操作员甲"}))
            done = []
            for task in tasks:
                done.append(await wait_terminal(runtime, task["task_id"], timeout=15))
            assert all(t["status"] == "confirmed" for t in done)
            starts = [parse_iso(t["started_at"]) for t in done]
            ends = [parse_iso(t["finished_at"]) for t in done]
            wall = (max(ends) - min(starts)).total_seconds()
            assert wall < 2.5, f"十设备应并行执行（每步 0.4 秒，串行需 4 秒），实际墙钟 {wall:.2f} 秒"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_cancel_queued_task(tmp_path):
    """执行前取消：不执行、不租约。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            step = {"action_display": "耗时步骤", "sleep": 0.4}
            runtime.submit_task(probe_payload([step]), source="console", context={"operator_id": "操作员甲"})
            second = runtime.submit_task(probe_payload([step]), source="console", context={"operator_id": "操作员甲"})
            from runtime.permissions import check_operation

            context = {"operator_id": "操作员乙", "role": "operator", "reason": "业务方撤回该任务",
                       "correlation_id": "c-cancel", "client_request_id": "r-cancel"}
            cancelled = await runtime.cancel_task(second["task_id"], context)
            assert cancelled["status"] == "cancelled"
            assert cancelled["status_display"] == "已取消"
            await asyncio.sleep(1.0)
            final = runtime.get_task(second["task_id"])
            assert final["status"] == "cancelled"
            assert second["task_id"] not in plugin.executed_tasks, "取消的任务不得执行"
            assert runtime.leases.get("arm-01") is None or runtime.leases.get("arm-01").get("task_id") != second["task_id"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_cancel_during_execution_stops_cleans_evidences(tmp_path):
    """动作开始后取消：先停止、清理和取证。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            steps = [
                {"action_display": "改变界面步骤", "is_action": True, "sleep": 0.05},
                {"action_display": "长观察步骤", "is_action": False, "sleep": 0.6},
            ]
            task = runtime.submit_task(probe_payload(steps), source="console", context={"operator_id": "操作员甲"})
            await asyncio.sleep(0.3)  # 进入第二步执行中
            context = {"operator_id": "操作员乙", "role": "operator", "reason": "业务方要求中止",
                       "correlation_id": "c-cancel2", "client_request_id": "r-cancel2"}
            await runtime.cancel_task(task["task_id"], context)
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "cancelled"
            assert done["status_display"] == "已取消"
            assert plugin.cancel_called, "取消必须调用插件停止接口"
            assert done.get("evidence_ids"), "取消前必须取证"
            assert done.get("home_check", {}).get("home_ok") is True, "取消后必须归位"
            assert runtime.devices.get("arm-01")["status"] == "ready"
            assert runtime.leases.get("arm-01") is None, "取消后必须释放租约"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_restart_recovery_unstarted_task(tmp_path):
    """未开始任务：重启后重新排队并完成。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        runtime.op_pause("arm-01", {"operator_id": "操作员甲", "role": "operator", "reason": "暂停以挂起任务",
                                    "correlation_id": "c", "client_request_id": "r"})
        task = runtime.submit_task(probe_payload([{"action_display": "步骤一"}]), source="console", context={"operator_id": "操作员甲"})
        await asyncio.sleep(0.2)
        assert runtime.get_task(task["task_id"])["status"] == "queued"
        await runtime.shutdown()

        plugin2 = ScriptedProbePlugin()
        runtime2 = make_runtime(tmp_path, 1, plugin=plugin2)
        await runtime2.startup(device_configs=make_device_configs(1))
        try:
            await asyncio.sleep(0.1)
            recovered = runtime2.get_task(task["task_id"])
            assert recovered.get("restart_note"), "恢复时必须标注重启恢复说明"
            assert recovered["status"] in ("queued", "awaiting_device", "validating", "executing", "confirmed")
            runtime2.op_resume("arm-01", {"operator_id": "操作员甲", "role": "operator", "reason": "恢复设备",
                                          "correlation_id": "c", "client_request_id": "r2"})
            done = await wait_terminal(runtime2, task["task_id"])
            assert done["status"] == "confirmed"
        finally:
            await runtime2.shutdown()

    asyncio.run(scenario())


def test_restart_recovery_action_interrupted_becomes_unknown(tmp_path):
    """动作步骤中断：结果未知 + 设备暂停 + 人工路径。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        task = runtime.submit_task(
            probe_payload([{"action_display": "改变界面动作", "is_action": True}]),
            source="console", context={"operator_id": "操作员甲"},
        )
        done = await wait_terminal(runtime, task["task_id"])
        assert done["status"] == "confirmed"
        # 模拟“下一个任务在动作步骤中崩溃”：直接构造执行中任务状态
        crashed = runtime.submit_task(
            probe_payload([{"action_display": "发送类动作", "is_action": True}]),
            source="console", context={"operator_id": "操作员甲"},
        )
        store_task = runtime.store.get_task(crashed["task_id"])
        store_task["status"] = "executing"
        store_task["status_display"] = display(TASK_STATUS, "executing")
        store_task["step_log"] = [{"step_no": 1, "action": "probe_1", "action_display": "发送类动作",
                                   "is_action": True, "phase": "started", "at": iso(utc_now())}]
        runtime.store.save_task(store_task)
        await runtime.shutdown()

        runtime2 = make_runtime(tmp_path, 1, plugin=ScriptedProbePlugin())
        await runtime2.startup(device_configs=make_device_configs(1))
        try:
            recovered = runtime2.get_task(crashed["task_id"])
            assert recovered["status"] == "unknown"
            assert recovered["status_display"] == "结果未知"
            assert "无法确认" in recovered["uncertainty_reason"]
            device = runtime2.devices.get("arm-01")
            assert device["status"] == "paused", "动作中断的设备必须暂停进入人工路径"
            takeovers = runtime2.takeovers()
            assert any(i["kind"] == "task_unknown" and i["task_id"] == crashed["task_id"] for i in takeovers)
            # 人工确认后设备才能继续
            runtime2.mark_task_reviewed(crashed["task_id"], {"operator_id": "操作员甲", "role": "operator",
                                                             "reason": "已人工核对界面无变化", "correlation_id": "c",
                                                             "client_request_id": "r"})
            runtime2.op_reset_home("arm-01", {"operator_id": "操作员甲", "role": "operator",
                                              "reason": "确认探头位置与安全路径",
                                              "correlation_id": "c", "client_request_id": "home-r",
                                              "confirmation_token": "确认执行"})
            runtime2.op_resume("arm-01", {"operator_id": "操作员甲", "role": "operator", "reason": "人工核对完成",
                                          "correlation_id": "c", "client_request_id": "r"})
            assert runtime2.devices.get("arm-01")["status"] == "ready"
        finally:
            await runtime2.shutdown()

    asyncio.run(scenario())


def test_restart_recovery_nonaction_interrupted_continues(tmp_path):
    """非动作步骤中断：从当前步骤继续执行（观测可重做）。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        task = runtime.submit_task(
            probe_payload([
                {"action_display": "观察界面", "is_action": False},
                {"action_display": "移动动作", "is_action": True},
            ]),
            source="console", context={"operator_id": "操作员甲"},
        )
        await wait_terminal(runtime, task["task_id"])
        # 构造“在第二步动作前的非动作观察中崩溃”的任务
        crashed = runtime.submit_task(
            probe_payload([
                {"action_display": "再次观察", "is_action": False},
                {"action_display": "移动动作", "is_action": True},
            ]),
            source="console", context={"operator_id": "操作员甲"},
        )
        store_task = runtime.store.get_task(crashed["task_id"])
        store_task["status"] = "executing"
        store_task["step_log"] = [{"step_no": 1, "action": "probe_1", "action_display": "再次观察",
                                   "is_action": False, "phase": "started", "at": iso(utc_now())}]
        runtime.store.save_task(store_task)
        await runtime.shutdown()

        plugin2 = ScriptedProbePlugin()
        runtime2 = make_runtime(tmp_path, 1, plugin=plugin2)
        await runtime2.startup(device_configs=make_device_configs(1))
        try:
            await asyncio.sleep(0.1)
            recovered = runtime2.get_task(crashed["task_id"])
            assert recovered.get("restart_note"), "必须记录恢复说明"
            done = await wait_terminal(runtime2, crashed["task_id"])
            assert done["status"] == "confirmed", f"非动作中断应续跑完成，实际 {done['status']}"
            assert runtime2.devices.get("arm-01")["status"] == "ready"
        finally:
            await runtime2.shutdown()

    asyncio.run(scenario())


def test_restart_recovery_completed_steps_finalize_only(tmp_path):
    """步骤全部完成但收尾中断：不重复执行步骤，只补收尾。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        task = runtime.submit_task(
            probe_payload([{"action_display": "步骤一"}]),
            source="console", context={"operator_id": "操作员甲"},
        )
        await wait_terminal(runtime, task["task_id"])
        crashed = runtime.submit_task(
            probe_payload([{"action_display": "步骤一", "detail": {"provider_message_id": "msg-001"}}]),
            source="console", context={"operator_id": "操作员甲"},
        )
        store_task = runtime.store.get_task(crashed["task_id"])
        store_task["status"] = "executing"
        store_task["status_display"] = display(TASK_STATUS, "executing")
        store_task["step_cursor"] = 1
        store_task["step_log"] = [
            {"step_no": 1, "action": "probe_1", "action_display": "步骤一", "is_action": True,
             "phase": "done", "outcome": "success", "message": "成功",
             "detail": {"provider_message_id": "msg-001"}, "at": iso(utc_now())},
        ]
        runtime.store.save_task(store_task)
        executed_before = len(plugin.step_executions)
        await runtime.shutdown()

        plugin2 = ScriptedProbePlugin()
        runtime2 = make_runtime(tmp_path, 1, plugin=plugin2)
        await runtime2.startup(device_configs=make_device_configs(1))
        try:
            await asyncio.sleep(0.3)
            recovered = runtime2.get_task(crashed["task_id"])
            assert recovered["status"] == "confirmed"
            assert recovered.get("provider_message_id") == "msg-001", "补收尾必须保留步骤结果"
            assert len(plugin2.step_executions) == 0, "补收尾不得重复执行任何步骤"
        finally:
            await runtime2.shutdown()

    asyncio.run(scenario())


def test_result_unknown_pauses_device_and_blocks_new_tasks(tmp_path):
    """结果未知：设备暂停、进入人工队列、新任务不被执行、禁止自动重试。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            task = runtime.submit_task(
                probe_payload([{"action_display": "发送后断连", "is_action": True, "outcome": "unknown",
                                "message": "点击发送后连接中断"}]),
                source="console", context={"operator_id": "操作员甲"},
            )
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "unknown"
            assert done["status_display"] == "结果未知"
            assert "连接中断" in done["uncertainty_reason"]
            device = runtime.devices.get("arm-01")
            assert device["status"] == "paused"
            assert done.get("evidence_ids"), "结果未知也必须留证"

            # 新任务不能被领取
            followup = runtime.submit_task(probe_payload([{"action_display": "步骤一"}]), source="console", context={"operator_id": "操作员甲"})
            await asyncio.sleep(0.4)
            assert runtime.get_task(followup["task_id"])["status"] == "queued"
            assert len(plugin.executed_tasks) == 1, "未知结果后禁止自动重试或继续领取"

            takeovers = runtime.takeovers()
            assert any(i["kind"] == "task_unknown" for i in takeovers)
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
