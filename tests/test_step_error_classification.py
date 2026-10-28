"""步骤异常分类：业务级拒绝不该冒充"结果未知"（2026-10-28）。

背景：能力层抛出的 ApiError / RegionContextError 是普通 Exception 子类，
此前会直穿 _run_step 的 typed 分支落到兜底 except，被判成 unknown →
设备自动暂停 + 不清理 + 不归位 + 租约保留，必须人工 resume。

口径：零动作时是"明确失败"（可重试）；已发生动作仍判 unknown（人工核对）。
"""
import asyncio

from runtime.errors import ApiError
from runtime.phone_region_context import RegionContextError
from tasks.plugin_api import TaskPlugin, make_step
from tests.helpers import PROBE_MANIFEST, make_device_configs, make_runtime, probe_payload
from copy import deepcopy


class RaisingProbe(TaskPlugin):
    """计划第一步就抛指定异常，用于验证分类边界。"""

    def __init__(self, exc):
        self.exc = exc
        self.executed: list[int] = []
        self.hooks: list[str] = []

    async def validate(self, task, device_context):
        return {"ok": True}

    async def plan(self, task, device_context):
        return [make_step(1, "probe_raise", "抛异常步骤", is_action=False,
                          required_capabilities=[], evidence_required=False, params={})]

    async def execute_step(self, step, ctx):
        self.executed.append(step["step_no"])
        raise self.exc

    async def cleanup(self, ctx):
        self.hooks.append("cleanup")
        return {"cleaned": True}


async def _run(tmp_path, exc, *, action_call_count: int = 0):
    plugin = RaisingProbe(exc)
    runtime = make_runtime(tmp_path, plugin=plugin, manifest=deepcopy(PROBE_MANIFEST))
    await runtime.devices.register_all(make_device_configs())
    task = runtime.submit_task(probe_payload([]), source="console", context={})
    lease = runtime.leases.acquire("arm-01", task["task_id"])
    task.update(status="awaiting_device", lease_id=lease["lease_id"])
    if action_call_count:
        task["action_call_count"] = action_call_count
    runtime.store.save_task(task)
    runtime.devices._set_status("arm-01", "busy", "测试任务取得租约")
    try:
        result = await runtime.executor.execute(task["task_id"])
        state = {
            "device_status": runtime.devices.get("arm-01").get("status"),
            "lease": runtime.leases.get("arm-01"),
            "unknown_hold": runtime.devices.get("arm-01").get("unknown_hold_task_id"),
        }
        return result, plugin, state
    finally:
        await runtime.shutdown()


def test_region_error_without_action_is_explicit_failure(tmp_path):
    result, plugin, state = asyncio.run(_run(
        tmp_path, RegionContextError("calibration_invalid", "滑动观察缺少本台校准")))
    assert plugin.executed == [1]
    assert result["status"] == "failed", result.get("status_display")
    assert result["error_category"] == "calibration_invalid"
    # 文案用中文（RegionContextError 自带的中文 message），英文键只留在 error_category
    assert "滑动观察缺少本台校准" in (result.get("failure_reason") or "")
    assert "calibration_invalid" not in (result.get("failure_reason") or "")
    # 零动作 → 允许清理与归位，设备不该被自动暂停等人。
    assert result["home_verified"] is True


def test_api_error_without_action_is_explicit_failure(tmp_path):
    result, _plugin, state = asyncio.run(_run(
        tmp_path, ApiError("invalid_request", "位移测量需要至少两帧证据")))
    assert result["status"] == "failed"
    assert result["error_category"] == "invalid_request"
    assert "请求参数无效" in (result.get("failure_reason") or "")
    assert result["home_verified"] is True
    # 插件抛异常也必须留下执行现场（不让异常分支挤掉执行器兜底）
    failed_step = [e for e in result["step_log"] if e.get("phase") == "done"][-1]
    assert failed_step["error_category"] == "invalid_request"
    assert failed_step["detail"]["failure_context"]["action"] == "probe_raise"


def test_business_error_after_action_stays_unknown(tmp_path):
    """已发生动作时不得降级为 failed：那会让自动重试造成二次动作。"""
    result, _plugin, state = asyncio.run(_run(
        tmp_path, RegionContextError("frame_chain_invalid", "画面来源与原校准绑定不符"),
        action_call_count=1))
    assert result["status"] == "unknown"
    assert result["error_category"] == "frame_chain_invalid"
    # unknown 走 result_message（failed 才用 failure_reason），文案必须点名"已发生动作"。
    assert "此前已发生动作" in (result.get("result_message") or "")


def test_unknown_semantics_key_stays_unknown(tmp_path):
    """白名单之外（语义不确定"是否已动过"）一律保持 unknown，不猜测。"""
    result, _plugin, state = asyncio.run(_run(tmp_path, ApiError("hardware_unconfirmed")))
    assert result["status"] == "unknown"
    assert result["error_category"] == "hardware_unconfirmed"
    assert result["home_verified"] is False


def test_whitelisted_key_with_prior_action_stays_unknown(tmp_path):
    result, _plugin, state = asyncio.run(_run(
        tmp_path, ApiError("invalid_request", "参数非法"), action_call_count=2))
    assert result["status"] == "unknown"
    assert result["error_category"] == "invalid_request"
    assert "此前已发生动作" in (result.get("result_message") or "")


def test_failure_runs_cleanup_and_home_but_unknown_does_not(tmp_path):
    """failed 与 unknown 的收尾副作用不同：这是改判 failed 的代价，必须守住。"""
    failed_result, failed_plugin, failed_state = asyncio.run(_run(
        tmp_path, ApiError("invalid_request", "参数非法")))
    assert failed_plugin.hooks == ["cleanup"], "failed 必须跑清理"
    assert failed_result["cleanup_result"].get("cleaned") is True
    assert "skip" not in (failed_result["home_check"].get("detail") or {})
    assert failed_state["unknown_hold"] is None
    assert failed_state["lease"] is None, "failed 必须释放租约"

    unknown_result, unknown_plugin, unknown_state = asyncio.run(_run(
        tmp_path, ApiError("hardware_unconfirmed"), action_call_count=1))
    assert unknown_plugin.hooks == [], "unknown 禁止自动清理"
    assert unknown_result["cleanup_result"].get("cleaned") is False
    # unknown 一律不得归位：home_ok 必须为 False（具体 skip 文案随分支不同）
    assert unknown_result["home_check"].get("home_ok") is False
    assert unknown_state["unknown_hold"] is not None, "unknown 必须置人工核对位"
    assert unknown_state["lease"] is not None, "unknown 保留租约等人"


def test_motion_not_started_wins_over_prior_action_count(tmp_path):
    """能力层已证实零动作时，即使计数已递增也不得判 unknown（生产主路径）。"""
    exc = ApiError("device_not_eligible", "设备未校准")
    exc.motion_not_started = True
    result, _plugin, state = asyncio.run(_run(tmp_path, exc, action_call_count=1))
    assert result["status"] == "failed"
    assert result["error_category"] == "device_not_eligible"
    # 不得再谎报"此前已发生动作"
    assert "此前已发生动作" not in (result.get("failure_reason") or "")
    assert state["unknown_hold"] is None


def test_whitelist_never_covers_control_loss_keys():
    """白名单只放"确定零副作用"的键；失去控制/断连/停止未知一律不许降级。"""
    from runtime.executor import DEMOTABLE_STEP_ERROR_KEYS as keys
    for forbidden in ("lease_conflict", "driver_disconnected", "stop_result_unknown",
                      "result_unknown_pending", "input_settings_commit_unknown",
                      "effect_unknown", "device_offline", "device_busy",
                      "device_quarantined", "hardware_unconfirmed"):
        assert forbidden not in keys, f"{forbidden} 绝不能降级为 failed"


def test_error_without_key_or_code_falls_back_to_unknown(tmp_path):
    """既无 key 也无 code 的异常：分类回落 result_unknown_pending，状态保持 unknown。"""
    result, _plugin, _state = asyncio.run(_run(tmp_path, ApiError("")))
    assert result["status"] == "unknown"
    assert result["error_category"] == "result_unknown_pending"
