"""The public task path is exercised with installed plugins and fake hardware."""
import asyncio
import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")


from api.app import create_app
from drivers import calibration_bundle as cb
from tests.helpers import make_device_configs, make_runtime, wait_terminal
from tests.test_daemon_arm import CALIB_SESSION
from tests.test_screen_gesture_driver import gesture_bundle


ROOT = Path(__file__).parents[1]
SCHEMA = json.loads((ROOT / "api/contracts/screen-gesture.v1.schema.json").read_text())


async def setup(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.plugins.install(ROOT / "examples/plugins/screen-gesture-adapter",
                            operator_id="tester", reason="isolated integration")
    runtime.plugins.activate("screen-gesture-adapter", operator_id="tester",
                             reason="isolated integration")
    await runtime.startup(device_configs=make_device_configs())
    runtime.devices._devices["arm-01"]["daemon_session"] = CALIB_SESSION
    driver = runtime.devices.driver("arm-01")
    original_read = driver.read_state

    async def read_state():
        return {**await original_read(), "session": CALIB_SESSION}

    driver.read_state = read_state
    cb.save_bundle(gesture_bundle(), tmp_path / "data")
    app = create_app(runtime)
    app.state.runtime = runtime
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated")
    return runtime, client


async def request_body(client, action):
    response = await client.get("/v1/devices/arm-01/screen-gestures")
    assert response.status_code == 200, response.text
    metadata = response.json()
    jsonschema.validate(metadata, {"$defs": SCHEMA["$defs"], "$ref": "#/$defs/metadata"})
    assert metadata["usable"] and metadata["actions"][action]["usable"], metadata
    payload = {"action": action, "binding": metadata["binding"]}
    if action == "double_tap":
        payload.update(point={"x": .31, "y": .67}, interval_ms=120)
    else:
        payload.update(start={"x": .5, "y": .99}, end={"x": .5, "y": .8})
    body = {"task_type": "screen_gesture", "contract_version": "screen-gesture.v1",
            "task_id": f"gesture-{action}", "idempotency_key": f"gesture-{action}",
            "device_id": "arm-01", "expires_at": None,
            "correlation_id": f"gesture-{action}", "payload": payload}
    jsonschema.validate(body, SCHEMA)
    return body


def gestures(runtime):
    return [item for item in runtime.devices.driver("arm-01").action_log
            if item["action"] in {"double_tap", "swipe"}]


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_public_metadata_task_result_and_duplicate_are_one_motion(tmp_path, action):
    async def run():
        runtime, client = await setup(tmp_path)
        before = cb.bundle_path("arm-01", tmp_path / "data").read_bytes()
        try:
            body = await request_body(client, action)
            response = await client.post("/v1/tasks", json=body)
            assert response.status_code == 200, response.text
            task = await wait_terminal(runtime, body["task_id"])
            assert task["status"] == "confirmed", (
                task.get("error_category"), task.get("failure_reason"), task.get("result_message"),
                task.get("step_log"))
            view = (await client.get(f"/v1/tasks/{body['task_id']}")).json()
            detail = next(entry["detail"] for entry in view["step_log"] if entry["phase"] == "done")
            jsonschema.validate(detail, {"$defs": SCHEMA["$defs"], "$ref": "#/$defs/resultDetail"})
            assert detail["mechanical_status"] == "confirmed"
            assert detail["effect_status"] == "unverified"
            assert detail["before_evidence"]["evidence_id"] != detail["after_evidence"]["evidence_id"]
            assert not view.get("provider_message_id") and not view.get("sent_at")
            repeat = await client.post("/v1/tasks", json=body)
            assert repeat.status_code == 200
            assert repeat.json()["task_id"] == body["task_id"]
            assert len(gestures(runtime)) == 1
            assert cb.bundle_path("arm-01", tmp_path / "data").read_bytes() == before
        finally:
            await client.aclose()
            await runtime.shutdown()
    asyncio.run(run())


@needs_wx_example
@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_guarded_context_after_metadata_rejects_submission(tmp_path, action):
    async def run():
        runtime, client = await setup(tmp_path)
        try:
            body = await request_body(client, action)
            runtime.plugins.state_set("wx-wechat-adapter", "arm-01", "manual_session",
                                      {"session_id": "held"})
            metadata = (await client.get("/v1/devices/arm-01/screen-gestures")).json()
            assert not metadata["usable"]
            response = await client.post("/v1/tasks", json=body)
            assert 400 <= response.status_code < 500, response.text
            assert response.json()["error"]["key"] == "device_not_eligible"
            assert not gestures(runtime)
        finally:
            await client.aclose()
            await runtime.shutdown()
    asyncio.run(run())


@needs_wx_example
@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_context_held_after_task_submission_blocks_capability(tmp_path, action):
    async def run():
        runtime, client = await setup(tmp_path)
        try:
            body = await request_body(client, action)
            original = runtime.plugin_capture_evidence

            async def hold_context(*args, **kwargs):
                evidence = await original(*args, **kwargs)
                runtime.plugins.state_set("wx-wechat-adapter", "arm-01", "manual_session",
                                          {"session_id": "new-held"})
                return evidence

            runtime.plugin_capture_evidence = hold_context
            response = await client.post("/v1/tasks", json=body)
            assert response.status_code == 200, response.text
            task = await wait_terminal(runtime, body["task_id"])
            assert task["status"] == "failed", task
            assert task["error_category"] == "capability_not_allowed"
            assert not gestures(runtime)
            assert runtime.plugins.state_get("wx-wechat-adapter", "arm-01", "manual_session")
        finally:
            await client.aclose()
            await runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_unknown_task_is_never_replayed(tmp_path, action):
    async def run():
        runtime, client = await setup(tmp_path)
        try:
            body = await request_body(client, action)
            runtime.devices.driver("arm-01").faults["disconnect_after"] = True
            response = await client.post("/v1/tasks", json=body)
            assert response.status_code == 200, response.text
            task = await wait_terminal(runtime, body["task_id"])
            assert task["status"] == "unknown", task
            assert len(gestures(runtime)) == 1
            repeat = await client.post("/v1/tasks", json=body)
            assert repeat.status_code == 200 and repeat.json()["status"] == "unknown"
            assert len(gestures(runtime)) == 1
        finally:
            await client.aclose()
            await runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_public_request_rejects_mechanical_override(tmp_path, action):
    async def run():
        runtime, client = await setup(tmp_path)
        try:
            body = await request_body(client, action)
            body["payload"]["tapDepth"] = 9
            response = await client.post("/v1/tasks", json=body)
            assert 400 <= response.status_code < 500, response.text
            assert not gestures(runtime)
        finally:
            await client.aclose()
            await runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("field", [
    "task_id", "contract_version", "correlation_id", "expires_at",
])
def test_public_request_requires_stable_envelope(tmp_path, field):
    async def run():
        runtime, client = await setup(tmp_path)
        try:
            body = await request_body(client, "double_tap")
            body.pop(field)
            response = await client.post("/v1/tasks", json=body)
            assert 400 <= response.status_code < 500, response.text
            assert runtime.store.all_tasks() == []
            assert not gestures(runtime)
        finally:
            await client.aclose()
            await runtime.shutdown()
    asyncio.run(run())


def test_public_draft_metadata_is_valid_but_never_usable(tmp_path):
    async def run():
        runtime, client = await setup(tmp_path)
        try:
            bundle = gesture_bundle()
            bundle["status"] = "draft"
            cb.save_bundle(bundle, tmp_path / "data")
            response = await client.get("/v1/devices/arm-01/screen-gestures")
            assert response.status_code == 200
            metadata = response.json()
            jsonschema.validate(metadata, {"$defs": SCHEMA["$defs"], "$ref": "#/$defs/metadata"})
            assert metadata["binding"] is None and not metadata["usable"]
            assert not any(item["usable"] for item in metadata["actions"].values())
            assert not gestures(runtime)
        finally:
            await client.aclose()
            await runtime.shutdown()
    asyncio.run(run())

@pytest.mark.parametrize("expiry", ["invalid", "2026-09-14T12:00:00", "2026-02-30T12:00:00Z"])
def test_public_invalid_expiry_never_creates_a_task(tmp_path, expiry):
    async def run():
        runtime, client = await setup(tmp_path)
        try:
            body = await request_body(client, "swipe")
            body["expires_at"] = expiry
            response = await client.post("/v1/tasks", json=body)
            assert 400 <= response.status_code < 500, response.text
            assert runtime.store.all_tasks() == []
        finally:
            await client.aclose()
            await runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("key,value", [
    ("request_schema", {"type": "invalid-type"}),
    ("request_schema", False),
    ("screen_context_guards", [{"plugin_id": "wx-wechat-adapter"}]),
])
def test_manifest_rejects_invalid_request_or_context_declaration(tmp_path, key, value):
    from runtime.errors import ApiError

    runtime = make_runtime(tmp_path)
    manifest = json.loads((ROOT / "examples/plugins/screen-gesture-adapter/manifest.json").read_text())
    target = manifest["task_types"][0] if key == "request_schema" else manifest
    target[key] = value
    with pytest.raises(ApiError):
        runtime.plugins.validate_manifest(manifest)
