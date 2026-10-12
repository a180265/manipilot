"""B 组验收测试：chat_send 收尾步骤化与 ime_delete_key 悬停停靠。

覆盖：
- 计划含 return_main 与 park_ime_delete_key 两个独立收尾步骤，能力声明完整；
- 正常发送后调用 arm.move_target("ime_delete_key")，结果含中文目标名，不 TAP；
- 模式篡改 / 目标未标定 / 断连 / 超时 / 返回主界面失败 / 取消的故障注入矩阵；
- cleanup 幂等；发送已确认但收尾失败不回滚发送结果；停靠与平台 home_check 分离。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")
pytestmark = [needs_wx_example]


from drivers.arm import DriverError
from fakes.fake_wx import FakeWX
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tasks.plugin_api import StepContext
from tests.helpers import make_device_configs

WX_PLUGIN_SOURCE = Path(__file__).parents[1] / "examples" / "plugins" / "wx-wechat-adapter"
TERMINAL = {"confirmed", "failed", "unknown", "cancelled", "expired"}


async def _wait_terminal(runtime, task_id, timeout=10.0):
    waited = 0.0
    while waited < timeout:
        task = runtime.get_task(task_id)
        if task["status"] in TERMINAL:
            return task
        await asyncio.sleep(0.05)
        waited += 0.05
    return runtime.get_task(task_id)


def make_fleet(tmp_path, n_devices=1):
    config = PlatformConfig(
        {"db_path": str(tmp_path / "wx.db"), "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation",
         "wx_callback_base_url": "http://fake-wx.local",
         "heartbeat_interval_seconds": 1, "camera_check_interval_seconds": 2},
        root=tmp_path,
    )
    runtime = ArmFleetRuntime(config)
    fake_wx = FakeWX()
    runtime.reporter.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_wx.app), base_url="http://fake-wx.local"
    )
    return runtime, fake_wx


async def _setup(runtime, fake_wx, tmp_path, sim_window: bool = True):
    runtime.plugins.install(WX_PLUGIN_SOURCE, operator_id="操作员甲", reason="B 组联调安装 WX 微信适配插件")
    runtime.plugins.activate("wx-wechat-adapter", operator_id="操作员甲", reason="B 组联调启用")
    await runtime.startup(device_configs=make_device_configs(1, sim_wechat_window=sim_window))
    # A-开关（2026-10-03 规格 §4）：本套件业务链路需输入域可用
    from tests.helpers import enable_input_switch
    enable_input_switch(runtime, tmp_path)


def _chat_send_payload(*, device_id="arm-01", account="sim-acc-01", conversation="conv-park",
                       text="您好，B 组停靠联调消息。", outbox_id=None):
    from runtime.util import iso, sha256_text, utc_now
    from datetime import timedelta

    outbox_id = outbox_id or f"outbox-{uuid.uuid4().hex[:6]}"
    return {
        "task_id": f"task-{uuid.uuid4().hex[:8]}", "task_type": "chat_send",
        "idempotency_key": outbox_id, "device_id": device_id,
        "wechat_account_id": account, "external_conversation_id": conversation,
        "expires_at": iso(utc_now() + timedelta(seconds=300)),
        "payload": {"outbox_id": outbox_id, "conversation_id": conversation,
                    "draft_id": f"draft-{outbox_id}", "text": text,
                    "text_hash": sha256_text(text), "approved_at": iso(utc_now()),
                    "opportunity_bindings": [], "fact_snapshot_ids": [],
                    "client_request_id": f"wx-req-{outbox_id}"},
        "contract_version": "chat-send.v1", "correlation_id": f"wx-corr-{outbox_id}",
    }


def _submit(runtime, payload):
    return runtime.submit_task(payload, source="wx",
                               context={"operator_id": "wx", "reason": "B 组停靠验收"})


def test_plan_and_manifest_declare_park_step(tmp_path):
    """返回、按配置回顶、悬停各自留证；manifest 声明 arm.move_target。"""
    manifest = __import__("json").loads((WX_PLUGIN_SOURCE / "manifest.json").read_text(encoding="utf-8"))
    assert "arm.move_target" in manifest["capabilities"], "能力清单必须与实际调用一致"

    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        await _setup(runtime, fake_wx, tmp_path)
        try:
            plugin, _ = runtime.plugins.instance_for_task_type("chat_send")
            task = {"task_type": "chat_send", "payload": {}, "device_id": "arm-01"}
            steps = await plugin.plan(task, runtime.devices.get("arm-01"))
            actions = [s["action"] for s in steps]
            assert actions[-3:] == ["return_main", "return_chat_list_top", "park_ime_delete_key"]
            park = steps[-1]
            assert park["action_display"] == "停靠输入法删除键悬停位（只移动，不点击）"
            assert park["required_capabilities"] == ["arm.move_target"]
            assert park["success_criteria"] == {"target_reached": True, "tap_executed": False}
            assert park["is_action"] is True and park["timeout_seconds"] > 0
            ret = steps[-3]
            assert ret["required_capabilities"] == ["arm.click", "camera.confirm_view"]
            assert ret["is_action"] is True
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_confirmed_send_parks_at_delete_key_without_tap(tmp_path):
    """验收 2/3/9：正常发送后停靠，fake 日志无 TAP/删除键点击；结果与 home_check 分离。"""

    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        await _setup(runtime, fake_wx, tmp_path)
        try:
            payload = _chat_send_payload()
            done = await _wait_terminal(runtime, _submit(runtime, payload)["task_id"])
            assert done["status"] == "confirmed", done.get("failure_reason") or done.get("uncertainty_reason")

            arm = runtime.devices.driver("arm-01")
            moves = [e for e in arm.action_log if e.get("action") == "move_target"]
            assert len(moves) == 1 and moves[0]["target"] == "ime_delete_key"
            clicks = [e for e in arm.action_log if e.get("action") == "click"]
            click_targets = [str(e.get("target")) for e in clicks]
            assert "ime_delete_key" not in click_targets, "停靠绝不产生删除键点击"
            assert set(click_targets) == {"conversation_item", "send_button", "back_button"}

            park_log = next(e for e in done["step_log"]
                            if e.get("action") == "park_ime_delete_key" and e.get("phase") == "done")
            assert park_log["outcome"] == "success"
            detail = park_log["detail"]
            assert detail["park_target_display"] == "输入法删除键位置"
            assert detail["park_target_mode"] == "hover_only"
            assert detail["park_tap_executed"] is False
            assert "未执行点击" in park_log["message"]
            assert "非硬件回零" in park_log["message"]
            assert "已回零" not in park_log["message"]

            # 停靠结果与平台级归位检查分别记录
            assert done["home_check"]["home_ok"] is True
            assert "home_check" in detail["home_check_note"] or "归位" in detail["home_check_note"]
            assert "已回零" not in str(done.get("result_message") or "")
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_tampered_target_mode_rejected_without_tap(tmp_path):
    """故障矩阵：停靠目标模式被篡改为 tap → 拒绝并审计，不发送 TAP。"""

    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        await _setup(runtime, fake_wx, tmp_path)
        try:
            import sys

            plugin, _ = runtime.plugins.instance_for_task_type("chat_send")
            module = sys.modules[type(plugin).__module__]
            original = module.DEFAULT_TARGET_MODES
            module.DEFAULT_TARGET_MODES = {**original, "ime_delete_key": "tap"}
            try:
                payload = _chat_send_payload()
                done = await _wait_terminal(runtime, _submit(runtime, payload)["task_id"])
            finally:
                module.DEFAULT_TARGET_MODES = original
            assert done["status"] == "failed"
            assert done["error_category"] == "device_target_mode_invalid"
            assert "hover_only" in done["failure_reason"]
            arm = runtime.devices.driver("arm-01")
            assert not [e for e in arm.action_log if e.get("action") == "move_target"], "模式不符时不得调用停靠"
            assert not [e for e in arm.action_log
                        if e.get("action") == "click" and str(e.get("target")) == "ime_delete_key"]
            # 发送已确认的事实保留在步骤日志，不被收尾失败回滚
            send_log = next(e for e in done["step_log"]
                            if e.get("action") == "send_and_capture" and e.get("phase") == "done")
            assert send_log["outcome"] == "success"
            assert send_log["detail"]["provider_message_id"]
            # 结果按失败回报 WX，不报告成功
            result = fake_wx.results_by_task[done["task_id"]]
            assert result["status"] == "failed"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_return_main_confirm_failure_skips_park(tmp_path):
    """验收 5：返回主界面确认失败 → 不停靠，结果未知，原因可审计。"""

    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        await _setup(runtime, fake_wx, tmp_path)
        try:
            camera = runtime.devices.camera("arm-01")
            original = camera.confirm_main_view
            calls = {"n": 0}

            async def flaky_confirm(expected_ui_state="wechat_main"):
                calls["n"] += 1
                if calls["n"] >= 2:  # 第 1 次是步骤 2 主界面确认，第 2 次是步骤 6 返回确认
                    return {"confirmed": False, "reason": "当前界面为会话界面，与期望不一致",
                            "ui_state": "conversation", "ui_state_display": "会话界面"}
                return await original(expected_ui_state)

            camera.confirm_main_view = flaky_confirm
            payload = _chat_send_payload()
            done = await _wait_terminal(runtime, _submit(runtime, payload)["task_id"])
            assert done["status"] == "unknown"
            assert "无法确认返回主界面" in done["uncertainty_reason"]
            assert "删除键停靠未执行" in done["uncertainty_reason"]
            arm = runtime.devices.driver("arm-01")
            assert not [e for e in arm.action_log if e.get("action") == "move_target"], "返回未确认时不得停靠"
            assert done["home_verified"] is False
            retained = runtime.leases.get("arm-01")
            assert retained and retained["task_id"] == done["task_id"], "主界面未确认时不得释放任务租约"
            device = runtime.devices.get("arm-01")
            assert device["status"] == "paused"
            assert device["unknown_hold_task_id"] == done["task_id"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_park_disconnect_is_unknown_and_keeps_send_result(tmp_path):
    """验收 8 + 故障矩阵：停靠断连 → 结果未知；发送终态仍保留已确认事实。"""

    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        await _setup(runtime, fake_wx, tmp_path)
        try:
            arm = runtime.devices.driver("arm-01")

            async def broken_move(target_id):
                raise DriverError("driver_disconnected", "停靠运动中 daemon 连接中断")

            arm.move_target = broken_move
            payload = _chat_send_payload()
            done = await _wait_terminal(runtime, _submit(runtime, payload)["task_id"])
            assert done["status"] == "unknown"
            assert "停靠" in done["uncertainty_reason"]
            # 发送步骤结果不被回滚：步骤日志保留已确认发送事实
            send_log = next(e for e in done["step_log"]
                            if e.get("action") == "send_and_capture" and e.get("phase") == "done")
            assert send_log["outcome"] == "success"
            assert send_log["detail"]["provider_message_id"]
            # 不重试停靠、不重复点击
            moves = [e for e in arm.action_log if e.get("action") == "move_target"]
            assert len(moves) == 0, "断连后不得自动重试停靠"
            # WX 收到 unknown 结果，带中文不确定性原因
            result = fake_wx.results_by_task[done["task_id"]]
            assert result["status"] == "unknown" and result["uncertainty_reason"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_park_timeout_and_unregistered_target_fail(tmp_path):
    """验收 4/6：停靠超时 → 结果未知；目标未标定 → 明确失败；均不误报成功。"""

    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        await _setup(runtime, fake_wx, tmp_path)
        try:
            arm = runtime.devices.driver("arm-01")

            async def slow_move(target_id):
                await asyncio.sleep(60)
                return {"ok": True}

            arm.move_target = slow_move
            payload = _chat_send_payload()
            done = await _wait_terminal(runtime, _submit(runtime, payload)["task_id"], timeout=30)
            assert done["status"] == "unknown"
            assert "超时" in done["uncertainty_reason"]
            from runtime.errors import ApiError
            with pytest.raises(ApiError):
                runtime.devices.resume("arm-01", "禁止跳过未知结果复核")
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_unregistered_park_target_fails_before_motion(tmp_path):
    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        await _setup(runtime, fake_wx, tmp_path)
        try:
            arm = runtime.devices.driver("arm-01")

            async def unregistered(target_id):
                error = DriverError("driver_policy_rejected",
                                    "目标 ime_delete_key（输入法删除键位置）尚未登记")
                error.motion_not_started = True
                raise error

            arm.move_target = unregistered
            done = await _wait_terminal(runtime, _submit(runtime, _chat_send_payload())["task_id"])
            assert done["status"] == "failed"
            assert done["error_category"] == "driver_policy_rejected"
            assert "尚未登记" in done["failure_reason"]
            assert not done.get("motion_result_unknown")
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_fake_move_target_rejects_non_hover_targets():
    """fake 与真实驱动语义一致：非 hover_only 目标调用 move_target 被拒绝。"""
    from drivers.fake_arm import FakeArmDriver

    driver = FakeArmDriver("arm-test")
    asyncio.run(driver.connect())
    result = asyncio.run(driver.move_target("ime_delete_key"))
    assert result["ok"] is True
    assert result["target_mode"] == "hover_only"
    assert result["tap_executed"] is False
    assert result["target_display"] == "输入法删除键位置"
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.move_target("send_button"))
    assert exc.value.category == "driver_policy_rejected"
    with pytest.raises(DriverError):
        asyncio.run(driver.move_target("unknown_target"))
    assert not [e for e in driver.action_log if e.get("action") == "click"]


def test_cancel_and_estop_during_park_do_not_fake_success(tmp_path):
    """验收 6 + 故障矩阵：停靠中取消/急停 → 停止动作、不误报成功、不重试停靠。"""

    async def scenario(mode: str):
        runtime, fake_wx = make_fleet(tmp_path / mode)
        await _setup(runtime, fake_wx, tmp_path / mode)
        try:
            arm = runtime.devices.driver("arm-01")

            async def slow_park(target_id):
                await asyncio.sleep(2.0)
                return await FakeArmDriver.move_target(arm, target_id)

            arm.move_target = slow_park
            payload = _chat_send_payload()
            task_id = _submit(runtime, payload)["task_id"]
            # 等停靠步骤开始执行
            for _ in range(400):
                task = runtime.get_task(task_id)
                started = [e for e in task.get("step_log", [])
                           if e.get("action") == "park_ime_delete_key" and e.get("phase") == "started"]
                if started:
                    break
                await asyncio.sleep(0.01)
            assert started, "停靠步骤应当开始执行"

            if mode == "cancel":
                await runtime.cancel_task(task_id, {
                    "operator_id": "操作员甲", "role": "operator", "reason": "停靠期间人工取消",
                    "correlation_id": "corr-b-cancel", "client_request_id": "req-b-cancel",
                })
            else:
                runtime.estop.trigger("操作员甲", "停靠期间全局急停")
            done = await _wait_terminal(runtime, task_id, timeout=15)

            assert done["status"] != "confirmed", f"{mode} 后不得报告成功：{done['status']}"
            # 停靠只执行一次，没有重试
            moves = [e for e in arm.action_log if e.get("action") == "move_target"]
            assert len(moves) <= 1, f"{mode} 后不得重试停靠"
            clicks = [e for e in arm.action_log
                      if e.get("action") == "click" and str(e.get("target")) == "ime_delete_key"]
            assert clicks == []
            # 已确认的发送事实保留在步骤日志
            send_log = next(e for e in done["step_log"]
                            if e.get("action") == "send_and_capture" and e.get("phase") == "done")
            assert send_log["outcome"] == "success"
            assert send_log["detail"]["provider_message_id"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario("cancel"))
    asyncio.run(scenario("estop"))


def test_cleanup_is_idempotent_and_adds_no_actions(tmp_path):
    """验收 7：cleanup 重复执行不产生重复点击或停靠。"""

    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        await _setup(runtime, fake_wx, tmp_path)
        try:
            payload = _chat_send_payload()
            task_id = _submit(runtime, payload)["task_id"]
            done = await _wait_terminal(runtime, task_id)
            assert done["status"] == "confirmed"
            plugin, _ = runtime.plugins.instance_for_task_type("chat_send")
            task = runtime.get_task(task_id)
            ctx = StepContext(runtime, task, plugin.manifest)
            arm = runtime.devices.driver("arm-01")
            before = len(arm.action_log)
            first = await plugin.cleanup(ctx)
            second = await plugin.cleanup(ctx)
            assert first["cleaned"] is True
            assert second["cleaned"] is True and second["idempotent"] is True
            assert len(arm.action_log) == before, "cleanup 不产生任何机械动作"
            assert not [e for e in arm.action_log if e.get("action") == "click"
                        and str(e.get("target")) == "ime_delete_key"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
