"""NAV-08: actual registration, isolated evidence roots, and fake driver gates."""
import asyncio
import hashlib

import cv2
import httpx
import numpy as np
import pytest

import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")
pytestmark = [needs_wx_example]


from adapters.uinav.navigation_templates import NavigationTemplates
from api.app import create_app
from runtime.config import PlatformConfig
from runtime.errors import ApiError
from runtime.runtime import ArmFleetRuntime
from runtime.util import iso, utc_now
from tests.helpers import make_device_configs, wait_terminal
from tests.test_gesture_plugins import (
    WX_SOURCE, auto_nav_payload, calibration_fixture, install_program_driver,
    list_observation, load_manifest, load_module, seed_navigation,
)
from tests.test_navigation_execution_gates import navigation_request


async def registered_runtime(tmp_path, monkeypatch, kind):
    seed_navigation(tmp_path)
    module = load_module("wx_integrity_runtime", WX_SOURCE)
    monkeypatch.setattr(module, "AUTO_NAV_SETTLE_SECONDS", 0)
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "nondefault" / "captures"),
        "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation",
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    runtime.plugins.register_instance_for_tests(module.Plugin(), load_manifest(WX_SOURCE))
    await runtime.startup(device_configs=make_device_configs(binding_revision=7))
    try:
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
        image = np.random.default_rng(123).integers(0, 256, (720, 1280), dtype=np.uint8)
        data = cv2.imencode(".png", image)[1].tobytes()

        class PreviewCamera:
            camera_id = device_id = "cam-01"
            calls = 0

            def capture_sync(self):
                self.calls += 1
                return {"image": data, "sha256": hashlib.sha256(data).hexdigest(),
                        "captured_at": iso(utc_now()), "width": 1280, "height": 720,
                        "frame_seq": self.calls}

        bundle = calibration_fixture()[0]
        with monkeypatch.context() as registration_patch:
            source = PreviewCamera()
            registration_patch.setattr(runtime.devices, "camera_broker", lambda device: source)
            registration_patch.setattr(runtime.devices, "binding_view", lambda device: {
                "binding": {"camera_id": "cam-01", "camera_device_unique_id": "uvc-01"},
                "binding_revision": 7, "daemon_session": bundle["daemonSession"],
                "binding_enabled": True})
            service = NavigationTemplates(runtime, "arm-01")
            preview = await service.preview({"kind": kind}, "test")
            template = service.register(kind, {
                "preview_id": preview["preview_id"], "expected_version": 0,
                "confirmed": True, "restore_target": None,
                "anchor_regions": [{"left_px": 100 + 200 * i, "top_px": 400,
                                    "width_px": 100, "height_px": 100}
                                   for i in range(1 if kind == "top" else 3)],
            })
        return runtime, template, calls
    except BaseException:
        await runtime.shutdown()
        raise


def corrupt(runtime, template, fault):
    entry = template["anchor_evidence"][0]
    record = runtime.evidence.get(entry["frame_evidence"][1]["evidence_id"])
    root = runtime.config.evidence_dir
    if fault == "source_replaced":
        data = cv2.imencode(".png", np.zeros((720, 1280), np.uint8))[1].tobytes()
        (root / record["file_path"]).write_bytes(data)
    elif fault == "all_deleted":
        for path in (root / f'navtpl-{template["kind"]}').glob("*.png"):
            path.unlink()
    elif fault == "source_foreign":
        record["device_id"] = "arm-02"
        runtime.store.save_evidence(record)
    elif fault == "crop_replaced":
        record = runtime.evidence.get(entry["evidence_id"])
        (root / record["file_path"]).write_bytes(b"corrupt crop")
    else:
        raise AssertionError(fault)


@pytest.mark.parametrize("kind,action", [("top", "return_top"), ("launcher", "home")])
@pytest.mark.parametrize("when", ["submit", "http", "queue", "read_state"])
@pytest.mark.parametrize("fault", ["source_replaced", "all_deleted", "source_foreign", "crop_replaced"])
def test_integrity_revocation_never_reaches_business_driver(tmp_path, monkeypatch, kind, action, when, fault):
    async def scenario():
        runtime, template, calls = await registered_runtime(tmp_path, monkeypatch, kind)
        try:
            body = navigation_request(auto_nav_payload(action))
            if when in {"submit", "http"}:
                corrupt(runtime, template, fault)
                if when == "submit":
                    with pytest.raises(ApiError) as caught:
                        runtime.submit_task(body, source="console", context={})
                    assert caught.value.key == "device_not_eligible"
                else:
                    app = create_app(runtime)
                    app.state.runtime = runtime
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="http://test") as client:
                        response = await client.post("/v1/tasks", json=body)
                    assert response.status_code == 409, response.text
                    assert response.json()["error"]["key"] == "device_not_eligible"
                assert runtime.store.get_task(body["task_id"]) is None
            else:
                if when == "read_state":
                    driver = runtime.devices.driver("arm-01")
                    original = driver.read_state

                    async def read_and_revoke():
                        result = await original()
                        await asyncio.sleep(0)
                        corrupt(runtime, template, fault)
                        return result

                    driver.read_state = read_and_revoke
                task = runtime.submit_task(body, source="console", context={})
                if when == "queue":
                    corrupt(runtime, template, fault)
                done = await wait_terminal(runtime, task["task_id"])
                assert done["status"] == "failed", done
                assert done["error_category"] == "device_not_eligible", done
                assert done["home_check"]["home_ok"], done
            assert calls == [], "No business motion may survive evidence revocation"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind,action", [("top", "return_top"), ("launcher", "home")])
def test_nondefault_evidence_root_remains_eligible_and_reaches_driver(tmp_path, monkeypatch, kind, action):
    async def scenario():
        runtime, template, calls = await registered_runtime(tmp_path, monkeypatch, kind)
        try:
            app = create_app(runtime)
            app.state.runtime = runtime
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.get("/v1/devices/arm-01/automatic-navigation/wx_chat_list_auto")
            assert response.status_code == 200
            assert response.json()["actions"][action]["usable"], response.text
            from tasks.plugin_api import StepContext
            body = navigation_request(auto_nav_payload(action))
            ctx = StepContext(runtime, body, load_manifest(WX_SOURCE))
            assert ctx.evidence_root == runtime.config.evidence_dir
            module = load_module("wx_nondefault_crop_consumer", WX_SOURCE)
            anchors = module._auto_nav_anchors(template, "arm-01", data_root=tmp_path / "data",
                                                evidence_root=ctx.evidence_root,
                                                evidence_get=runtime.evidence.get)
            assert len(anchors) == (1 if kind == "top" else 3)
            task = runtime.submit_task(body, source="console", context={})
            done = await wait_terminal(runtime, task["task_id"])
            assert len(calls) == 1, done
            assert done["status"] in {"confirmed", "unknown"}, done
            assert not (tmp_path / "evidence").exists(), "No inferred default evidence root"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
