"""Isolated Fleet wake lifecycle; no live daemon, services, or calibration files."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys
import types

import httpx
import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")


from api.app import create_app
from drivers.arm import DriverError
from drivers import calibration_bundle as cb, screen_gestures as sg
from runtime.errors import ApiError
from runtime.util import iso, parse_iso, utc_now
from tests.helpers import make_device_configs, make_runtime, wait_terminal, probe_payload
from tests.test_screen_gesture_driver import gesture_bundle
from tests.test_screen_wake_contract import wake_request


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "examples/plugins/screen-wake-adapter"


async def wake_runtime(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    runtime.capabilities.bootstrap()
    await runtime.devices.register_all(make_device_configs())
    bundle = gesture_bundle()
    cb.save_bundle(bundle, tmp_path / "data")
    binding = sg.binding_snapshot(bundle)
    # The separately owned driver boundary resolves a verified calibration point.
    module = types.ModuleType("drivers.wake_screen")
    module.resolve_wake_target = lambda value: {
        "binding": sg.binding_snapshot(value),
        "validation_point_id": "validation-1", "point": {"x": .5, "y": .5},
    }
    monkeypatch.setitem(sys.modules, "drivers.wake_screen", module)
    runtime.plugins.install(PLUGIN_DIR, "tester", "isolated wake test")
    runtime.plugins.activate("screen-wake-adapter", "tester", "isolated wake test")
    device = runtime.devices.get("arm-01")
    device.update(camera_quality="waiting", daemon_session=binding["daemon_session"],
                  camera_device_unique_id=binding["camera_device_unique_id"],
                  camera_binding_revision=binding["binding_revision"],
                  camera_frame_age_ms=0, ime_device_id=None,
                  enforce_input_gate=True, input_acceptance_deferred=True)
    runtime.devices.camera("arm-01").set_quality("waiting")
    driver = runtime.devices.driver("arm-01")
    calls = []

    async def wake(params, *, execution_guard):
        execution_guard()
        calls.append(deepcopy(params))
        return {"controller_completed": True, "tap_count": 2, "effect_status": "unverified"}

    monkeypatch.setattr(driver, "wake_screen", wake, raising=False)
    request = wake_request(payload={"binding": binding, "confirmed": True})
    return runtime, request, calls


def test_waiting_wake_executes_once_without_ime_home_or_calibration_changes(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        device = runtime.devices.get("arm-01")
        protected = {key: deepcopy(device.get(key)) for key in
                     ("calibration_status", "home_ok", "daemon_session", "device_mode",
                      "current_ui_state", "input_acceptance_deferred")}
        bundle_path = cb.bundle_path("arm-01", tmp_path / "data")
        before = bundle_path.read_bytes()
        driver = runtime.devices.driver("arm-01")
        previous_actions = deepcopy(driver.action_log)
        task = runtime.submit_task(request, source="console", context={"operator_id": "tester"})
        assert task["read_only_task"] is False
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "confirmed", result
        assert result["effect_status"] == "unverified"
        assert result["controller_completed"] is True
        assert result["tap_count"] == 2
        assert result["home_verified"] is False
        assert runtime.leases.get("arm-01") is None
        assert calls == [request["payload"]]
        assert driver.action_log == previous_actions, "no extra home, STOP, or cleanup motion"
        assert device["status"] == "ready"
        assert device["camera_quality"] == "waiting"
        assert {key: device.get(key) for key in protected} == protected
        assert bundle_path.read_bytes() == before
        assert runtime.devices.is_taskable("arm-01") == (False, "camera_waiting")
        assert runtime.submit_task(request, source="console", context={})["status"] == "confirmed"
        await runtime._dispatch_once()
        assert calls == [request["payload"]]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"status": "paused"}, {"status": "quarantined"}, {"status": "offline"},
    {"status": "recovering"}, {"status": "unknown"}, {"status": "degraded"},
    {"estop_latched": True}, {"home_ok": False}, {"binding_enabled": False},
    {"presence_state": "removed"}, {"daemon_session": "new-session"},
    {"calibration_status": "due"}, {"camera_quality": "frozen"},
    {"camera_quality": "overexposed"}, {"camera_quality": "unavailable"},
    {"camera_quality": "unknown"}, {"camera_frame_age_ms": 3001},
    {"camera_frame_age_ms": float("nan")}, {"camera_frozen": True},
    {"motion_result_unknown": True}, {"stop_result_unknown": True},
    {"capabilities": []},
])
def test_other_device_blockers_still_prevent_claim(tmp_path, monkeypatch, change):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        device = runtime.devices.get("arm-01")
        device.update(change)
        before = deepcopy(device)
        await runtime._dispatch_once()
        assert calls == []
        assert runtime.get_task(task["task_id"])["status"] == "queued"
        assert runtime.leases.get("arm-01") is None
        for key in ("home_ok", "calibration_status", "status", "camera_quality"):
            assert device.get(key) == before.get(key)

    asyncio.run(scenario())


@needs_wx_example
@pytest.mark.parametrize("blocker", ["global_estop", "lease", "declared_guard"])
def test_current_context_guards_are_not_waived(tmp_path, monkeypatch, blocker):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        if blocker == "global_estop":
            monkeypatch.setattr(type(runtime.estop), "active", property(lambda self: True))
        elif blocker == "lease":
            runtime.leases.acquire("arm-01", "other-task")
        elif blocker == "declared_guard":
            record = runtime.plugins.get("screen-wake-adapter")
            record["manifest"]["screen_context_guards"] = [
                {"plugin_id": "wx-wechat-adapter", "state_key": "manual_session"}]
            runtime.store.save_plugin(record)
            runtime.plugin_state_set("wx-wechat-adapter", "arm-01", "manual_session", {"held": True})
        await runtime._dispatch_once()
        assert calls == []
        assert runtime.get_task(task["task_id"])["status"] == "queued"

    asyncio.run(scenario())


@pytest.mark.parametrize("previous_action_count", [0, 1, None])
def test_terminal_unknown_history_does_not_block_new_wake_or_change_audit(
        tmp_path, monkeypatch, previous_action_count):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        history = []
        for index in range(23):
            previous = deepcopy(request)
            previous.update(
                task_id=f"historical-self-test-{index}",
                idempotency_key=f"historical-key-{index}",
                task_type="gesture_self_test", plugin_id="gesture-validation-adapter",
                status="unknown", uncertainty_reason="historical test interrupted",
                created_at=iso(utc_now() - timedelta(days=6)),
                finished_at=iso(utc_now() - timedelta(days=6, seconds=-30)),
                action_call_count=previous_action_count,
            )
            previous["payload"]["binding"]["daemon_session"] = "previous-session"
            runtime.store.save_task(previous)
            history.append(deepcopy(runtime.get_task(previous["task_id"])))
        bundle_path = cb.bundle_path("arm-01", tmp_path / "data")
        before_bundle = bundle_path.read_bytes()
        before_device = deepcopy(runtime.devices.get("arm-01"))
        from runtime.screen_wake import wake_readiness
        assert wake_readiness(runtime, "arm-01")["usable"] is True
        assert runtime.devices.get("arm-01") == before_device
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "confirmed", result
        assert calls == [request["payload"]]
        assert runtime.leases.get("arm-01") is None
        for previous in history:
            assert runtime.get_task(previous["task_id"]) == previous
        assert bundle_path.read_bytes() == before_bundle
        assert runtime.submit_task(request, source="console", context={})["status"] == "confirmed"
        await runtime._dispatch_once()
        assert calls == [request["payload"]]

    asyncio.run(scenario())


@needs_wx_example
def test_idle_chat_session_is_preserved_and_does_not_prevent_manual_wake(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        session = {"session_id": "idle-chat", "state": "active", "control_mode": "manual",
                   "external_conversation_id": "conversation-1"}
        runtime.plugin_state_set("wx-wechat-adapter", "arm-01", "manual_session", session)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "confirmed"
        assert runtime.plugin_state_get("wx-wechat-adapter", "arm-01", "manual_session") == session
        assert len(calls) == 1
        assert runtime.leases.get("arm-01") is None

    asyncio.run(scenario())


def test_other_executing_task_blocks_even_if_lease_record_is_missing(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        active = runtime.submit_task(probe_payload([{}]), source="console", context={})
        active["status"] = "executing"
        runtime.store.save_task(active)
        await runtime._dispatch_once()
        assert not calls and runtime.leases.get("arm-01") is None
        assert runtime.get_task(task["task_id"])["status"] == "queued"

    asyncio.run(scenario())


def test_expired_submission_records_no_motion_without_lease(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        request["expires_at"] = iso(utc_now() - timedelta(seconds=10))
        task = runtime.submit_task(request, source="console", context={})
        assert task["status"] == "expired"
        assert task["motion_not_started"] is True
        assert task["step_log"][-1]["detail"]["binding"] == request["payload"]["binding"]
        await runtime._dispatch_once()
        assert not calls and runtime.leases.get("arm-01") is None

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [
    ("plugin_id", "other-plugin"), ("contract_version", "other.v1"),
    ("capability", "arm.double_tap"), ("read_only_task", True),
    ("payload", {"binding": {}, "confirmed": True}),
    ("policy_context", {"automatic": True}),
])
def test_task_type_name_alone_never_grants_waiting_exception(tmp_path, monkeypatch, field, value):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(request, source="console", context={})
        task[field] = value
        runtime.store.save_task(task)
        await runtime._dispatch_once()
        assert calls == []
        assert runtime.leases.get("arm-01") is None

    asyncio.run(scenario())


def test_ordinary_task_cannot_claim_waiting_device(tmp_path, monkeypatch):
    async def scenario():
        runtime, _, calls = await wake_runtime(tmp_path, monkeypatch)
        task = runtime.submit_task(probe_payload([{}]), source="console", context={})
        await runtime._dispatch_once()
        assert runtime.get_task(task["task_id"])["status"] == "queued"
        assert not calls and runtime.leases.get("arm-01") is None

    asyncio.run(scenario())


def test_wake_expiry_is_capped_and_original_request_remains_idempotent(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        request["expires_at"] = iso(utc_now() + timedelta(days=1))
        task = runtime.submit_task(request, source="console", context={})
        assert 0 < (parse_iso(task["expires_at"]) - parse_iso(task["created_at"])).total_seconds() <= 60
        assert runtime.submit_task(request, source="console", context={})["task_id"] == task["task_id"]
        task["expires_at"] = iso(utc_now() - timedelta(seconds=1))
        runtime.store.save_task(task)
        await runtime._dispatch_once()
        assert runtime.get_task(task["task_id"])["status"] == "expired"
        assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"point": {"x": .5, "y": .5}}, {"confirmed": False}, {"confirmed": 1},
    {"binding": {}}, {"tapDepth": 8}, {"unlock": True}, {"repeat": 2},
])
def test_runtime_also_enforces_closed_payload(tmp_path, monkeypatch, changes):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        request["payload"].update(changes)
        with pytest.raises(ApiError):
            runtime.submit_task(request, source="console", context={})
        assert not calls and not runtime.store.all_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("reject_before_motion", [True, False])
def test_driver_exception_classification_never_retries_or_homes(tmp_path, monkeypatch, reject_before_motion):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        driver = runtime.devices.driver("arm-01")
        actions = deepcopy(driver.action_log)

        async def wake(params, *, execution_guard):
            execution_guard()
            calls.append(params)
            error = DriverError("driver_disconnected", "injected")
            if reject_before_motion:
                error.motion_not_started = True
            raise error

        monkeypatch.setattr(driver, "wake_screen", wake)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == ("failed" if reject_before_motion else "unknown")
        assert result["motion_not_started"] is reject_before_motion
        assert runtime.devices.get("arm-01")["status"] == ("ready" if reject_before_motion else "paused")
        assert runtime.devices.get("arm-01")["home_ok"] is reject_before_motion
        assert runtime.devices.get("arm-01")["calibration_status"] == "validated"
        assert driver.action_log == actions
        assert runtime.leases.get("arm-01") is None
        await runtime._dispatch_once()
        runtime.recover_tasks()
        assert len(calls) == 1

    asyncio.run(scenario())


def test_capability_removed_after_plan_is_explicit_zero_motion_failure(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)
        plugin, _ = runtime.plugins.instance_for_task_type("screen_wake")
        original = plugin.plan

        async def plan(task, device):
            result = await original(task, device)
            runtime.devices.get("arm-01")["capabilities"].remove("arm.wake_screen")
            return result

        monkeypatch.setattr(plugin, "plan", plan)
        task = runtime.submit_task(request, source="console", context={})
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "failed"
        assert result["motion_not_started"] is True
        assert calls == []
        assert runtime.devices.get("arm-01")["calibration_status"] == "validated"
        assert runtime.leases.get("arm-01") is None

    asyncio.run(scenario())


def test_get_metadata_is_read_only_and_does_not_predict_wake(tmp_path, monkeypatch):
    async def scenario():
        runtime, request, calls = await wake_runtime(tmp_path, monkeypatch)

        async def state():
            return {"connected": True, "session": request["payload"]["binding"]["daemon_session"],
                    "motion_features": ["tap2.v1"]}

        monkeypatch.setattr(runtime.devices.driver("arm-01"), "read_gesture_state", state, raising=False)
        before = deepcopy(runtime.devices.get("arm-01"))
        app = create_app(runtime)
        app.state.runtime = runtime
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/devices/arm-01/screen-wake")
        assert response.status_code == 200
        result = response.json()
        assert result["usable"] is True, result
        assert result["effect_status"] == "unverified"
        assert result["binding"] == request["payload"]["binding"]
        assert result["target"] == {"validation_point_id": "validation-1", "point": {"x": .5, "y": .5}}
        assert runtime.devices.get("arm-01") == before
        assert not calls and runtime.leases.get("arm-01") is None
        assert not runtime.store.all_tasks()

    asyncio.run(scenario())
