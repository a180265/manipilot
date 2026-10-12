"""NAV-02/NAV-04: temporary data, fake devices, no hardware access."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")
pytestmark = [needs_wx_example]


from api.app import create_app
from runtime import automatic_gestures as ag
from runtime.errors import ApiError
from tests.helpers import make_device_configs, make_runtime, wait_terminal, op_context
from tests.test_gesture_plugins import (
    WX_SOURCE, auto_nav_payload, load_manifest, load_module,
    seed_navigation, full_binding, list_observation, install_program_driver,
    navigation_program, calibration_fixture, stub_run,
    program_fixture, program_body, swipe_program, _seed_calibration, enable_program_switches,
    seed_navigation_template, unmatched_template,
)


def navigation_request(payload=None):
    return {
        "task_id": "nav-gate-task", "idempotency_key": "nav-gate-task",
        "task_type": "wx_auto_navigate", "device_id": "arm-01",
        "contract_version": "wx-auto-navigation.v1",
        "expires_at": "2099-01-01T00:00:00Z", "correlation_id": "nav-gate",
        "payload": payload or auto_nav_payload(),
    }


@needs_wx_example
@pytest.mark.parametrize("entrypoint", ["submit", "http"])
def test_direct_navigation_rejects_missing_certificate(tmp_path, entrypoint):
    """开关已删（2026-09-30 拍板）：拒绝只来自资格本身（此处=无证书档案）。"""
    async def scenario():
        module = load_module("wx_navigation_gate", WX_SOURCE)
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=load_manifest(WX_SOURCE))
        await runtime.startup(device_configs=make_device_configs(binding_revision=7))
        try:
            if entrypoint == "submit":
                with pytest.raises(ApiError) as error:
                    runtime.submit_task(navigation_request(), source="wx", context={})
                assert error.value.key == "device_not_eligible"
            else:
                app = create_app(runtime)
                app.state.runtime = runtime
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://test") as client:
                    response = await client.post("/v1/tasks", json=navigation_request())
                assert response.status_code == 409, response.text
                assert response.json()["error"]["key"] == "device_not_eligible"
            assert runtime.store.get_task("nav-gate-task") is None
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


async def navigation_runtime(tmp_path, monkeypatch):
    seed_navigation(tmp_path)
    module = load_module("wx_navigation_execution", WX_SOURCE)
    monkeypatch.setattr(module, "AUTO_NAV_SETTLE_SECONDS", 0)
    runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=load_manifest(WX_SOURCE))
    await runtime.startup(device_configs=make_device_configs(binding_revision=7))
    calls = install_program_driver(runtime)
    driver = runtime.devices.driver("arm-01")
    read_state = driver.read_state

    async def state():
        return {**await read_state(), "motion_features": ["path.v1", "swipe.v1"]}

    driver.read_state = state
    camera = runtime.devices.camera("arm-01")

    async def observe(**kwargs):
        return {**list_observation(), "_image_bytes": camera.capture_sync()["image"]}

    camera.observe_regions = observe
    return runtime, module, calls


def revoke(tmp_path, change):
    from drivers import calibration_bundle as cb
    root = tmp_path / "data"
    if change == "certificate":
        ag.publish_run_results("arm-01", stub_run("completed", valid=("scroll_up", "scroll_down")),
                               data_root=root)
    elif change == "calibration":
        bundle = cb.load_bundle("arm-01", root)
        bundle["z"]["tapDepth"] += 0.1
        cb.save_bundle(bundle, root)


@pytest.mark.parametrize("when", ["queue", "observe", "frame", "read_state"])
@pytest.mark.parametrize("change", ["certificate", "calibration"])
def test_revocation_before_driver_is_failed_without_business_motion(tmp_path, monkeypatch, when, change):
    async def scenario():
        runtime, module, calls = await navigation_runtime(tmp_path, monkeypatch)
        try:
            if when == "observe":
                camera = runtime.devices.camera("arm-01")
                original = camera.observe_regions

                async def observe(**kwargs):
                    result = await original(**kwargs)
                    await asyncio.sleep(0)
                    revoke(tmp_path, change)
                    return result

                camera.observe_regions = observe
            elif when == "frame":
                camera = runtime.devices.camera_broker("arm-01")
                original = camera.capture_sync
                revoked = False

                def capture():
                    nonlocal revoked
                    result = original()
                    if not revoked:
                        revoke(tmp_path, change)
                        revoked = True
                    return result

                camera.capture_sync = capture
            elif when == "read_state":
                driver = runtime.devices.driver("arm-01")
                original = driver.read_state

                async def read_state():
                    result = await original()
                    await asyncio.sleep(0)
                    revoke(tmp_path, change)
                    return result

                driver.read_state = read_state
            body = navigation_request()
            task = runtime.submit_task(body, source="console", context={})
            if when == "queue":
                revoke(tmp_path, change)
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "failed", done
            assert done["error_category"] == "device_not_eligible", done
            assert calls == [], "业务动作必须在真正 driver 调用之前被拦截"
            assert done["home_check"]["home_ok"], "业务资格失效不得阻止安全归位"
            assert runtime.submit_task(body, source="console", context={})["status"] == "failed"
            assert calls == []
            assert module.AUTO_NAV_DATA_ROOT != tmp_path / "data", "运行时不得改插件全局根"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("when", ["submit", "queue", "read_state"])
@pytest.mark.parametrize("change", ["calibration_due", "calibration_unknown", "paused", "binding_disabled", "home_lost"])
def test_runtime_safety_revocation_blocks_motion(tmp_path, monkeypatch, when, change):
    async def scenario():
        runtime, _, calls = await navigation_runtime(tmp_path, monkeypatch)
        driver = runtime.devices.driver("arm-01")
        original = driver.read_state

        def revoke_safety():
            if change.startswith("calibration_"):
                runtime.devices.set_calibration_status("arm-01", change.removeprefix("calibration_"))
            elif change == "paused":
                runtime.devices.pause("arm-01", "isolated revocation test")
            elif change == "binding_disabled":
                runtime.devices.get("arm-01")["binding_enabled"] = False
            else:
                runtime.devices.get("arm-01")["home_ok"] = False

        async def revoke_after_read():
            result = await original()
            await asyncio.sleep(0)
            revoke_safety()
            return result

        if when == "read_state":
            driver.read_state = revoke_after_read
        try:
            if when == "submit":
                revoke_safety()
                with pytest.raises(ApiError) as error:
                    runtime.submit_task(navigation_request(), source="console", context={})
                assert error.value.key in {"device_not_eligible", "device_unavailable"}
                assert runtime.store.get_task("nav-gate-task") is None
                assert calls == []
                return
            task = runtime.submit_task(navigation_request(), source="console", context={})
            if when == "queue":
                revoke_safety()
                if change in {"paused", "home_lost"}:
                    # Existing scheduling rules hold unsafe devices without taking a lease.
                    for _ in range(3):
                        await runtime._dispatch_once()
                    assert runtime.store.get_task(task["task_id"])["status"] == "queued"
                    assert runtime.leases.get("arm-01") is None
                    assert calls == []
                    cancelled = await runtime.cancel_task(task["task_id"], op_context())
                    assert cancelled["status"] == "cancelled"
                    assert calls == []
                    return
            done = await wait_terminal(runtime, task["task_id"])
            assert calls == [], "runtime safety revocation must block the driver call"
            assert done["status"] == "failed", done
            assert done["error_category"] == "device_not_eligible", done
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@needs_wx_example
def test_back_uses_verified_system_program_at_driver(tmp_path, monkeypatch):
    async def scenario():
        runtime, _module, calls = await navigation_runtime(tmp_path, monkeypatch)
        try:
            runtime.plugin_state_set("wx-wechat-adapter", "arm-01", "manual_session", {
                "session_id": "s1", "control_mode": "manual", "conversation_id": "c1"})
            payload = auto_nav_payload("back_to_list", session_ref={
                "session_id": "s1", "control_mode": "manual", "external_conversation_id": "c1"})
            task = runtime.submit_task(navigation_request(payload), source="console", context={})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed", done
            assert len(calls) == 1
            assert calls[0]["program"] == navigation_program("system_back_left")
            assert calls[0]["profile"]["tapDepth"] == 8
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("action,gesture_id", [("search_next", "scroll_up"),
                                              ("search_previous", "scroll_down"),
                                              ("return_top", "scroll_down")])
@needs_wx_example
def test_list_preserves_certified_contact_and_speed_at_driver(tmp_path, monkeypatch, action, gesture_id):
    async def scenario():
        runtime, _module, calls = await navigation_runtime(tmp_path, monkeypatch)
        try:
            if action == "return_top":
                seed_navigation_template(tmp_path)
                monkeypatch.setattr(runtime.plugins.instance("wx-wechat-adapter"),
                                    "_auto_nav_verify_template", unmatched_template)
            task = runtime.submit_task(navigation_request(auto_nav_payload(action)), source="console", context={})
            await wait_terminal(runtime, task["task_id"])
            assert len(calls) == 1
            actual = calls[0]["program"]["strokes"][0]
            expected = navigation_program(gesture_id)["strokes"][0]
            assert {k: v for k, v in actual.items() if k != "points"} == {
                k: v for k, v in expected.items() if k != "points"}
            p0, p1 = actual["points"]
            certified_speed = 0.45 / 2200
            assert abs(p1["y"] - p0["y"]) / p1["t_ms"] == pytest.approx(certified_speed, rel=0.001)
            assert p1["t_ms"] > 150
            assert calls[0]["profile"]["tapDepth"] == 8
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@needs_wx_example
def test_home_uses_certified_bottom_edge_program_at_driver(tmp_path, monkeypatch):
    async def scenario():
        runtime, _module, calls = await navigation_runtime(tmp_path, monkeypatch)
        try:
            seed_navigation_template(tmp_path, "launcher")

            async def confirmed_template(*args, **kwargs):
                return {"matched": True, "camera_failed": False,
                        "confirmation": {"kind": "launcher_template", "evidence_ids": ["test-launcher"]}}

            monkeypatch.setattr(runtime.plugins.instance("wx-wechat-adapter"),
                                "_auto_nav_verify_template", confirmed_template)
            task = runtime.submit_task(navigation_request(auto_nav_payload("home")), source="console", context={})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed", done
            assert len(calls) == 1
            assert calls[0]["program"] == navigation_program("system_home")
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


def test_post_motion_exception_is_unknown_and_never_retried(tmp_path, monkeypatch):
    async def scenario():
        runtime, _module, calls = await navigation_runtime(tmp_path, monkeypatch)
        driver = runtime.devices.driver("arm-01")

        async def execute(plan):
            calls.append(deepcopy(plan))
            raise RuntimeError("动作已下发，但回包丢失")

        driver.execute_gesture_program = execute
        try:
            body = navigation_request()
            task = runtime.submit_task(body, source="console", context={})
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "unknown", done
            assert len(calls) == 1
            again = runtime.submit_task(body, source="console", context={})
            assert again["status"] == "unknown"
            await asyncio.sleep(0.15)
            assert len(calls) == 1
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_compiled_swipe_forwards_normalized_points_not_screen_pixels(tmp_path, monkeypatch):
    module, manifest = program_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)
    enable_program_switches(tmp_path)

    async def scenario():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs())
        try:
            calls = install_program_driver(runtime)
            program = swipe_program()
            task = runtime.submit_task(program_body(program=program), source="console", context={})
            await wait_terminal(runtime, task["task_id"])
            assert len(calls) == 1
            for key, point in zip(("start", "end"), program["strokes"][0]["points"]):
                assert calls[0][key] == {"x": point["x"], "y": point["y"], "source": "screen_normalized"}
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
