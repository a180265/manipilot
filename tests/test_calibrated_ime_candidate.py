"""Calibrated candidate clicks reuse registered geometry and existing IME facts."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.screen_ime import input_task
from drivers.arm import DriverError
from runtime.layout_geometry import mapping_error_bound
from runtime.layout_matching import compute_page_check
from tasks.plugin_api import StepContext
from tests.helpers import PROBE_MANIFEST, make_runtime, make_device_configs, op_context, probe_payload
from tests.test_layout_observation import layout_rig, offline_only, png  # noqa: F401
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401


def _enable_input(data_root):
    from runtime.input_settings import update_settings
    update_settings("arm-01", data_root=data_root, enabled=True, ime_device_id="k40-test",
                    expected_revision=0, operator_id="操作员", reason="测试配对候选点击",
                    client_request_id="candidate-input-enable", calibration_ready=True)


async def _runtime(tmp_path):
    manifest = copy.deepcopy(PROBE_MANIFEST)
    manifest["task_types"][0].update(task_type="declared_input", requires_input=True,
                                     requires_keyboard_active=False)
    manifest["capabilities"] += ["arm.click", "arm.park"]
    manifest["resources"]["input"] = True
    runtime = make_runtime(tmp_path, manifest=manifest)
    await runtime.devices.register_all(make_device_configs())
    _enable_input(tmp_path / "data")
    payload = probe_payload([{"is_action": True}])
    payload["task_type"] = "declared_input"
    task = runtime.submit_task(payload, source="console", context=op_context())
    lease = runtime.leases.acquire(task["device_id"], task["task_id"])
    task.update(lease_id=lease["lease_id"], status="executing")
    runtime.store.save_task(task)
    return runtime, task


@pytest.mark.parametrize("fault", [None, "task", "phone", "text", "status", "state", "layout"])
@pytest.mark.parametrize("payload_mode", ["task_text", "input_context"])
def test_calibrated_candidate_reuses_displayed_bound_task(tmp_path, monkeypatch, fault, payload_mode):
    async def scenario():
        runtime, task = await _runtime(tmp_path)
        try:
            task["payload"] = {"text": "ok"} if payload_mode == "task_text" else {"query": "ok"}
            runtime.store.save_task(task)
            monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
            monkeypatch.setattr(runtime.devices, "input_binding", lambda _: {"ime_device_id": "bound-phone"})
            runtime.devices._ime_status_provider = lambda: [
                {"deviceId": "bound-phone", "online": True,
                 "keyboardActive": True, "inputControlCompatible": True}]
            monkeypatch.setattr(runtime.layout_observation, "execution_binding",
                                lambda _: None if fault == "layout" else {"selected_version": 7})
            geometry = AsyncMock(side_effect=AssertionError("calibrated candidate must not request nonce"))
            monkeypatch.setattr(runtime, "_ime_live_geometry_target", geometry)
            plugin, _ = runtime.plugins.instance_for_task_type(task["task_type"])
            ctx = StepContext(runtime, task, plugin.manifest)

            class InputContext:
                # A search-like input body belongs to this operation; capability
                # calls still use the original task, whose payload has no text.
                task = {**ctx.task, "payload": {**ctx.task["payload"], "text": "ok"}}

                def __getattr__(self, name):
                    return getattr(ctx, name)

            input_ctx = ctx if payload_mode == "task_text" else InputContext()
            if fault != "state":
                ctx.state_set("observed_layout_state", "chat_ime_empty")
            rows, cancelled, clicks = {}, [], []

            class Tasks:
                def list_devices(self):
                    return [{"deviceId": "bound-phone", "online": True, "keyboardActive": True}]

                async def create(self, payload):
                    rows[payload["taskId"]] = {**payload, "status": "displayed"}
                    return payload, True

                def find(self, key):
                    row = copy.deepcopy(rows.get(key))
                    if fault == "task":
                        return None
                    if fault == "phone":
                        row["deviceId"] = "other-phone"
                    if fault == "text":
                        row["content"] = "different text"
                    if fault == "status":
                        row["status"] = "cancelled"
                    return row

                async def wait_for_status(self, key, statuses, timeout, probe=None):
                    return ({"status": "displayed"} if "displayed" in statuses else
                            {"status": "inserted", "typedCharacterCount": 2})

                async def cancel(self, key):
                    cancelled.append(key)

            service = SimpleNamespace(tasks=Tasks())
            monkeypatch.setattr(input_task, "get_default_service", lambda: service)
            from adapters.screen_ime import service as service_module
            monkeypatch.setattr(service_module, "get_default_service", lambda: service)
            arm = runtime.devices.driver("arm-01")
            original_click = arm.click

            async def click(params):
                clicks.append(params)
                assert params["target_ref"] == {"layout_state": "chat_ime_empty"}
                assert "geometry_gate" not in params["input_claim"]
                return await original_click(params)

            monkeypatch.setattr(arm, "click", click)
            if fault:
                with pytest.raises(DriverError):
                    await input_task.insert_bound_text(input_ctx)
                assert clicks == []
                assert cancelled == [task["task_id"] + "-ime"]
                assert not runtime.store.get_task(task["task_id"]).get("input_effect_seq")
            else:
                result = await input_task.insert_bound_text(input_ctx)
                assert result["outcome"] == "success"
                assert len(clicks) == 1
                assert cancelled == []
                assert runtime.store.get_task(task["task_id"])["input_effect_seq"] == 1
            geometry.assert_not_called()
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,target", [("live", "ime_candidate_bar"),
                                        ("live", "ime_delete_key"),
                                        ("calibrated", "ime_delete_key")])
def test_other_ime_geometry_paths_keep_live_nonce(tmp_path, monkeypatch, mode, target):
    async def scenario():
        runtime, task = await _runtime(tmp_path)
        try:
            monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", mode)
            geometry = AsyncMock(side_effect=DriverError("driver_policy_rejected", "nonce probe"))
            monkeypatch.setattr(runtime, "_ime_live_geometry_target", geometry)
            plugin, _ = runtime.plugins.instance_for_task_type(task["task_type"])
            ctx = StepContext(runtime, task, plugin.manifest)
            with pytest.raises(DriverError, match="nonce probe"):
                await ctx.call_capability("arm.click", {"target_id": target})
            geometry.assert_awaited_once_with("arm-01", target)
            assert not any(row["action"] == "click" for row in runtime.devices.driver("arm-01").action_log)
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("registered", [False, True])
def test_actual_driver_candidate_uses_selected_calibrated_rect(target_rig, monkeypatch, registered):
    rig = target_rig
    if registered:
        template = copy.deepcopy(rig.parts["template_store"].get_template("offline-template", 2))
        template["version"] = 3
        page = next(p for p in template["pages"] if p["state"] == "chat_ime_empty")
        page["targets"].append({
            "id": "ime_candidate_bar", "anchor_id": page["anchors"][0]["id"], "mode": "tap",
            "relative_rect": {"dx": .2, "dy": .2, "width": .5, "height": .1}})
        rig.adaptation["page_checks"] = [
            compute_page_check(p, png(rig.screens[p["state"]]),
                {a["id"]: rig.assets[a["asset_id"]] for a in p["anchors"]},
                ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle))
            for p in template["pages"]]
        rig.template_version(template)
    _enable_input(rig.data)
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    rig.source.image = rig.frame("chat_ime_empty")
    params = {"target_id": "ime_candidate_bar", "target_ref": {"layout_state": "chat_ime_empty"}}
    if not registered:
        with pytest.raises(DriverError):
            asyncio.run(rig.arm.click(params))
        assert motion(rig) == []
    else:
        result = asyncio.run(rig.arm.click(params))
        assert result["target_source"] == "shared_layout"
        assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1
