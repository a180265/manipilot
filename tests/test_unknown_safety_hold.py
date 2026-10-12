"""Unknown-outcome safety holds are tested with simulation drivers only."""
import asyncio

import pytest

from drivers.arm import DriverError
from runtime.errors import ApiError
from runtime.util import iso, utc_now
from tests.helpers import (
    ScriptedProbePlugin, make_device_configs, make_runtime, op_context,
    probe_payload, wait_terminal,
)


class UnsafeRecoveryPlugin(ScriptedProbePlugin):
    def __init__(self):
        super().__init__()
        self.hooks = []

    async def recover(self, failure, ctx):
        self.hooks.append("recover")
        await ctx.call_capability("arm.home")
        return {"home_verified": True}

    async def cleanup(self, ctx):
        self.hooks.append("cleanup")
        await ctx.call_capability("arm.home")
        return {"cleaned": True}


class RecoveryStopUnknownPlugin(UnsafeRecoveryPlugin):
    async def recover(self, failure, ctx):
        self.hooks.append("recover")
        await ctx.call_capability("arm.stop")
        try:
            await ctx.call_capability("arm.home")
        except DriverError:
            self.hooks.append("home_blocked")
        return {"home_verified": False}


class CleanupStopUnknownPlugin(UnsafeRecoveryPlugin):
    async def cleanup(self, ctx):
        self.hooks.append("cleanup")
        await ctx.call_capability("arm.stop")
        return {"cleaned": False}


class RecoveryHomeUnknownPlugin(UnsafeRecoveryPlugin):
    async def recover(self, failure, ctx):
        self.hooks.append("recover")
        try:
            await ctx.call_capability("arm.home")
        except DriverError:
            self.hooks.append("receipt_lost")
        try:
            await ctx.call_capability("arm.home")
        except DriverError:
            self.hooks.append("next_action_blocked")
        return {"home_verified": True}


class CleanupHomeUnknownPlugin(UnsafeRecoveryPlugin):
    async def cleanup(self, ctx):
        self.hooks.append("cleanup")
        try:
            await ctx.call_capability("arm.home")
        except DriverError:
            self.hooks.append("receipt_lost")
        return {"cleaned": True}


@pytest.mark.parametrize("phase", ["recover", "cleanup"])
@pytest.mark.parametrize("receipt", ["exception", "unknown", "missing"])
def test_lifecycle_action_uncertainty_blocks_next_motion(tmp_path, monkeypatch, phase, receipt):
    async def scenario():
        plugin = RecoveryHomeUnknownPlugin() if phase == "recover" else CleanupHomeUnknownPlugin()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        try:
            homes = []

            async def home():
                homes.append("home")
                if len(homes) > 1:
                    return {"ok": True}
                if receipt == "exception":
                    raise DriverError("driver_timeout", "归位回执丢失")
                if receipt == "missing":
                    return None
                return {"ok": False, "outcome_hint": "unknown"}

            monkeypatch.setattr(runtime.devices.driver("arm-01"), "go_home", home)
            task = runtime.submit_task(
                probe_payload([{"is_action": True,
                                "outcome": "failed" if phase == "recover" else "success"}]),
                source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "unknown"
            assert homes == ["home"]
            if phase == "recover":
                assert "cleanup" not in plugin.hooks
                assert "next_action_blocked" in plugin.hooks
            assert done["home_verified"] is False
            assert runtime.devices.get("arm-01")["unknown_hold_task_id"] == task["task_id"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["recover", "cleanup", "home_check"])
def test_restart_interrupted_finalization_never_runs_hooks(tmp_path, phase):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        runtime.op_pause("arm-01", op_context(reason="构造中断收尾"))
        task = runtime.submit_task(
            probe_payload([{"is_action": True}]),
            source="console", context={"operator_id": "测试"})
        stored = runtime.store.get_task(task["task_id"])
        stored.update(
            status="executing", step_cursor=1, finalization_phase=phase,
            plan=[{"step_no": 1, "action": "probe_1", "is_action": True}],
            step_log=[{"step_no": 1, "action": "probe_1", "is_action": True,
                       "phase": "done", "outcome": "success"}])
        runtime.store.save_task(stored)
        await runtime.shutdown()

        plugin = UnsafeRecoveryPlugin()
        restarted = make_runtime(tmp_path, plugin=plugin)
        await restarted.startup(device_configs=make_device_configs())
        try:
            # Check startup's decision before a no-lease executor can mask replay.
            done = restarted.get_task(task["task_id"])
            assert done["status"] == "unknown"
            assert plugin.hooks == []
            assert plugin.step_executions == []
            assert restarted.devices.get("arm-01")["unknown_hold_task_id"] == task["task_id"]
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", ["confirmed", "not_started"])
def test_known_lifecycle_receipt_does_not_hold_device(tmp_path, monkeypatch, receipt):
    async def scenario():
        plugin = CleanupHomeUnknownPlugin()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        try:
            calls = []

            async def home():
                calls.append("home")
                if receipt == "not_started" and len(calls) == 1:
                    error = DriverError("driver_uncalibrated", "未进入运动")
                    error.motion_not_started = True
                    raise error
                return {"ok": True}

            monkeypatch.setattr(runtime.devices.driver("arm-01"), "go_home", home)
            task = runtime.submit_task(
                probe_payload([{"is_action": True}]),
                source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed"
            assert calls == ["home", "home"]
            assert not done.get("motion_result_unknown")
            assert not runtime.devices.get("arm-01").get("unknown_hold_task_id")
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", ["exception", "unknown", "missing"])
def test_platform_home_receipt_unknown_prevents_confirmation(tmp_path, monkeypatch, receipt):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            async def home():
                if receipt == "exception":
                    raise DriverError("driver_timeout", "保持位置回执丢失")
                if receipt == "missing":
                    return None
                return {"ok": True, "outcome_hint": "unknown"}

            monkeypatch.setattr(runtime.devices.driver("arm-01"), "go_home", home)
            task = runtime.submit_task(
                probe_payload([{"is_action": True}]),
                source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "unknown"
            assert done["home_verified"] is False
            assert runtime.devices.get("arm-01")["unknown_hold_task_id"] == task["task_id"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_unknown_locks_before_plugin_hooks_and_restart_requires_review(tmp_path, monkeypatch):
    async def scenario():
        plugin = UnsafeRecoveryPlugin()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        arm = runtime.devices.driver("arm-01")
        homes = []

        async def home():
            homes.append("home")
            return {"ok": True}

        monkeypatch.setattr(arm, "go_home", home)
        task = runtime.submit_task(
            probe_payload([{"action_display": "动作结果不明", "is_action": True,
                            "outcome": "unknown"}]),
            source="console", context={"operator_id": "测试"},
        )
        done = await wait_terminal(runtime, task["task_id"])
        assert done["status"] == "unknown"
        assert plugin.hooks == []
        assert homes == []
        assert done["home_verified"] is False
        held = runtime.devices.get("arm-01")
        assert held["unknown_hold_task_id"] == task["task_id"]
        assert held["home_ok"] is False
        await runtime.shutdown()

        restarted = make_runtime(tmp_path)
        await restarted.startup(device_configs=make_device_configs())
        try:
            held = restarted.devices.get("arm-01")
            assert held["status"] == "paused"
            assert held["unknown_hold_task_id"] == task["task_id"]
            check = await restarted.op_home_check("arm-01", op_context())
            assert check["home_ok"] is False
            assert check["detail"]["read_only"] is True
            with pytest.raises(ApiError):
                restarted.op_resume("arm-01", op_context())
            restarted.op_reset_home("arm-01", op_context(confirmation_token="确认执行"))
            with pytest.raises(ApiError):
                restarted.op_resume("arm-01", op_context())
            restarted.mark_task_reviewed(task["task_id"], op_context())
            assert restarted.op_resume("arm-01", op_context())["status"] == "ready"
            assert restarted.devices.get("arm-01")["unknown_hold_task_id"] is None
            assert restarted.get_task(task["task_id"])["status"] == "unknown"
        finally:
            await restarted.shutdown()

        again = make_runtime(tmp_path)
        await again.startup(device_configs=make_device_configs())
        try:
            assert again.devices.get("arm-01")["status"] == "ready"
            assert again.get_task(task["task_id"])["status"] == "unknown"
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_manual_pause_survives_restart_without_unknown_history_gate(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        runtime.op_pause("arm-01", op_context(reason="人工维护"))
        await runtime.shutdown()

        restarted = make_runtime(tmp_path)
        await restarted.startup(device_configs=make_device_configs())
        try:
            device = restarted.devices.get("arm-01")
            assert device["status"] == "paused"
            assert device["pause_reason"] == "人工维护"
            assert restarted.op_resume("arm-01", op_context())["status"] == "ready"
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


def test_offline_reconnect_keeps_unknown_hold(tmp_path, monkeypatch):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{"is_action": True, "outcome": "unknown"}]),
                source="console", context={"operator_id": "测试"})
            await wait_terminal(runtime, task["task_id"])
            device = runtime.devices.get("arm-01")
            device.update(status="offline", status_display="离线", driver_mode="daemon")
            runtime.store.save_device(device)
            arm = runtime.devices.driver("arm-01")

            async def connected_state():
                return {"connected": True, "session": "session-new"}

            monkeypatch.setattr(arm, "read_state", connected_state)
            await runtime.devices._heartbeat_device("arm-01")
            held = runtime.devices.get("arm-01")
            assert held["status"] == "paused"
            assert held["unknown_hold_task_id"] == task["task_id"]
            assert held["home_ok"] is False
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_failed_recovery_stop_becomes_unknown_before_cleanup_or_home(tmp_path, monkeypatch):
    async def scenario():
        plugin = RecoveryStopUnknownPlugin()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        try:
            arm = runtime.devices.driver("arm-01")
            homes = []
            monkeypatch.setattr(arm, "stop", lambda: failed_stop())

            async def home():
                homes.append("home")
                return {"ok": True}

            async def failed_stop():
                return {"ok": False, "stopped": False}

            monkeypatch.setattr(arm, "go_home", home)
            task = runtime.submit_task(
                probe_payload([{"is_action": True, "outcome": "failed"}]),
                source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "unknown"
            assert plugin.hooks == ["recover", "home_blocked"]
            assert homes == []
            assert runtime.devices.get("arm-01")["unknown_hold_task_id"] == task["task_id"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_cleanup_stop_unknown_blocks_home_check(tmp_path, monkeypatch):
    async def scenario():
        plugin = CleanupStopUnknownPlugin()
        runtime = make_runtime(tmp_path, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs())
        try:
            arm = runtime.devices.driver("arm-01")
            homes = []

            async def failed_stop():
                return {"ok": False, "stopped": False}

            async def home():
                homes.append("home")
                return {"ok": True}

            monkeypatch.setattr(arm, "stop", failed_stop)
            monkeypatch.setattr(arm, "go_home", home)
            task = runtime.submit_task(
                probe_payload([{"is_action": True}]),
                source="console", context={"operator_id": "测试"})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "unknown"
            assert plugin.hooks == ["cleanup"]
            assert homes == []
            assert runtime.devices.get("arm-01")["unknown_hold_task_id"] == task["task_id"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_restart_completed_unknown_step_never_runs_finalization(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        runtime.op_pause("arm-01", op_context(reason="构造中断任务"))
        task = runtime.submit_task(
            probe_payload([{"is_action": True, "outcome": "unknown"}]),
            source="console", context={"operator_id": "测试"})
        stored = runtime.store.get_task(task["task_id"])
        stored.update(status="executing", step_cursor=1,
                      plan=[{"step_no": 1, "action": "probe_1", "is_action": True}],
                      step_log=[{"step_no": 1, "action": "probe_1", "is_action": True,
                                 "phase": "done", "outcome": "unknown"}])
        runtime.store.save_task(stored)
        await runtime.shutdown()

        plugin = UnsafeRecoveryPlugin()
        restarted = make_runtime(tmp_path, plugin=plugin)
        await restarted.startup(device_configs=make_device_configs())
        try:
            done = restarted.get_task(task["task_id"])
            assert done["status"] == "unknown"
            assert restarted.devices.get("arm-01")["unknown_hold_task_id"] == task["task_id"]
            assert plugin.hooks == []
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupted", ["pending_stop", "unknown_step"])
def test_restart_uncertainty_never_replays_remaining_steps(tmp_path, interrupted):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        runtime.op_pause("arm-01", op_context(reason="构造中断任务"))
        task = runtime.submit_task(
            probe_payload([{"is_action": True}, {"is_action": True}]),
            source="console", context={"operator_id": "测试"})
        stored = runtime.store.get_task(task["task_id"])
        stored.update(status="executing", step_cursor=1,
                      plan=[{"step_no": 1, "action": "probe_1", "is_action": True},
                            {"step_no": 2, "action": "probe_2", "is_action": True}],
                      step_log=[{"step_no": 1, "action": "probe_1", "is_action": True,
                                 "phase": "done", "outcome": "success"}])
        if interrupted == "pending_stop":
            stored["stop_attempt_count"] = 1
            stored["stop_result"] = {"ok": False, "outcome_hint": "unknown"}
        else:
            stored["step_log"][0]["outcome"] = "unknown"
        runtime.store.save_task(stored)
        await runtime.shutdown()

        plugin = UnsafeRecoveryPlugin()
        restarted = make_runtime(tmp_path, plugin=plugin)
        await restarted.startup(device_configs=make_device_configs())
        try:
            assert restarted.get_task(task["task_id"])["status"] == "unknown"
            assert restarted.devices.get("arm-01")["status"] == "paused"
            assert plugin.step_executions == []
            assert plugin.hooks == []
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


def test_quarantine_survives_offline_restart_and_resume_failure_rolls_back(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        task = runtime.submit_task(
            probe_payload([{"is_action": True, "outcome": "unknown"}]),
            source="console", context={"operator_id": "测试"})
        await wait_terminal(runtime, task["task_id"])
        runtime.op_quarantine(
            "arm-01", op_context(reason="人工隔离", confirmation_token="确认执行"))
        device = runtime.devices.get("arm-01")
        runtime.devices._set_status("arm-01", "maintenance", "模拟离线前维护")
        runtime.devices._set_status("arm-01", "offline", "模拟离线")
        runtime.store.save_device(device)
        await runtime.shutdown()

        restarted = make_runtime(tmp_path)
        await restarted.startup(device_configs=make_device_configs())
        try:
            device = restarted.devices.get("arm-01")
            assert device["status"] == "quarantined"
            assert device["unknown_hold_task_id"] == task["task_id"]
            restarted.mark_task_reviewed(task["task_id"], op_context())
            restarted.op_reset_home("arm-01", op_context(confirmation_token="确认执行"))
            with pytest.raises(ApiError):
                restarted.op_resume("arm-01", op_context())
            assert device["unknown_hold_task_id"] == task["task_id"]
            assert device["pause_reason"]
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


def test_legacy_unknown_pause_remains_manual_without_guessing_history(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        task = runtime.submit_task(
            probe_payload([{"is_action": True, "outcome": "unknown"}]),
            source="console", context={"operator_id": "测试"})
        await wait_terminal(runtime, task["task_id"])
        device = runtime.devices.get("arm-01")
        device.pop("unknown_hold_task_id")
        device.pop("unknown_hold_task_ids")
        device.pop("unknown_hold_at")
        runtime.store.save_device(device)
        await runtime.shutdown()

        restarted = make_runtime(tmp_path)
        await restarted.startup(device_configs=make_device_configs())
        try:
            device = restarted.devices.get("arm-01")
            assert device["status"] == "paused"
            assert device["unknown_hold_task_id"] is None
            assert device["unknown_hold_task_ids"] == []
            with pytest.raises(ApiError):
                restarted.op_resume("arm-01", op_context())
            restarted.mark_task_reviewed(task["task_id"], op_context())
            restarted.op_reset_home("arm-01", op_context(confirmation_token="确认执行"))
            assert restarted.op_resume("arm-01", op_context())["status"] == "ready"
        finally:
            await restarted.shutdown()

        history = make_runtime(tmp_path)
        await history.startup(device_configs=make_device_configs())
        try:
            assert history.devices.get("arm-01")["status"] == "ready"
            assert history.get_task(task["task_id"])["status"] == "unknown"
        finally:
            await history.shutdown()

    asyncio.run(scenario())


def test_restart_preserves_every_unknown_task_before_resuming(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            first = runtime.submit_task(
                probe_payload([{"is_action": True, "outcome": "unknown"}]),
                source="console", context={"operator_id": "测试"})
            await wait_terminal(runtime, first["task_id"])
            second = runtime.submit_task(
                probe_payload([{"is_action": True, "outcome": "unknown"}]),
                source="console", context={"operator_id": "测试"})
            stored = runtime.get_task(second["task_id"])
            stored["status"] = "executing"
            stored["step_log"] = [{
                "step_no": 1, "action": "probe_1", "action_display": "中断动作",
                "is_action": True, "phase": "started",
            }]
            runtime.store.save_task(stored)
        finally:
            await runtime.shutdown()

        restarted = make_runtime(tmp_path)
        await restarted.startup(device_configs=make_device_configs())
        try:
            held = restarted.devices.get("arm-01")
            assert held["unknown_hold_task_ids"] == [first["task_id"], second["task_id"]]
            assert restarted.get_task(second["task_id"])["status"] == "unknown"
            restarted.mark_task_reviewed(first["task_id"], op_context())
            restarted.op_reset_home("arm-01", op_context(confirmation_token="确认执行"))
            with pytest.raises(ApiError, match="尚未人工核对"):
                restarted.op_resume("arm-01", op_context())
            restarted.mark_task_reviewed(second["task_id"], op_context())
            assert restarted.op_resume("arm-01", op_context())["status"] == "ready"
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


def test_unrelated_unknown_history_does_not_bind_an_old_pause(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{"is_action": True, "outcome": "unknown"}]),
                source="console", context={"operator_id": "测试"})
            await wait_terminal(runtime, task["task_id"])
            history = runtime.submit_task(
                probe_payload([{"is_action": True, "outcome": "unknown"}]),
                source="console", context={"operator_id": "测试"})
            stale = runtime.get_task(history["task_id"])
            stale["status"] = "unknown"
            runtime.store.save_task(stale)
            device = runtime.devices.get("arm-01")
            device.pop("unknown_hold_task_id")
            device.pop("unknown_hold_task_ids")
            device.pop("unknown_hold_at")
            runtime.store.save_device(device)
        finally:
            await runtime.shutdown()

        restarted = make_runtime(tmp_path)
        await restarted.startup(device_configs=make_device_configs())
        try:
            device = restarted.devices.get("arm-01")
            assert device["status"] == "paused"
            assert device["unknown_hold_task_ids"] == []
            restarted.mark_task_reviewed(task["task_id"], op_context())
            restarted.op_reset_home("arm-01", op_context(confirmation_token="确认执行"))
            assert restarted.op_resume("arm-01", op_context())["status"] == "ready"
            assert restarted.get_task(history["task_id"]).get("reviewed_at") is None
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


def test_real_binding_resume_requires_current_session_and_heartbeat(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = runtime.submit_task(
                probe_payload([{"is_action": True, "outcome": "unknown"}]),
                source="console", context={"operator_id": "测试"})
            await wait_terminal(runtime, task["task_id"])
            runtime.mark_task_reviewed(task["task_id"], op_context())
            runtime.op_reset_home("arm-01", op_context(confirmation_token="确认执行"))
            device = runtime.devices.get("arm-01")
            device["binding_mode"] = "real"
            with pytest.raises(ApiError):
                runtime.op_resume("arm-01", op_context())
            assert device["unknown_hold_task_id"] == task["task_id"]
            device["daemon_session"] = "current-session"
            device["last_heartbeat_at"] = iso(utc_now())
            assert runtime.op_resume("arm-01", op_context())["status"] == "ready"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
