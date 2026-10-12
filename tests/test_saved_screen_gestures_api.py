"""Public archive endpoints via in-process ASGI; no runtime workers or live IO."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import jsonschema
import pytest

from api.app import create_app
from drivers import calibration_bundle as cb, saved_screen_gestures as saved
from tests.helpers import make_device_configs, make_runtime
from tests.test_saved_screen_gestures import SCHEMA, TASK_SCHEMA, archive_path, definition
from tests.test_screen_gesture_driver import gesture_bundle


def api_fixture(tmp_path, *, driver_root=False):
    runtime = make_runtime(tmp_path)
    for cfg in make_device_configs(2):
        runtime.devices.register_device(cfg)
    bundle = gesture_bundle()
    runtime.devices._devices["arm-01"]["daemon_session"] = bundle["daemonSession"]
    driver = runtime.devices.driver("arm-01")
    root = tmp_path / ("driver-data" if driver_root else "data")
    if driver_root:
        driver.data_root = root
    driver.read_state = AsyncMock(side_effect=AssertionError("Archive GET must not read hardware"))
    driver.read_gesture_state = AsyncMock(side_effect=AssertionError("Archive GET must not read hardware"))
    cb.save_bundle(bundle, root)
    app = create_app(runtime)
    app.state.runtime = runtime
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated")
    return runtime, client, bundle, root


def get(client, path):
    return asyncio.run(client.get(path))


@pytest.mark.parametrize("driver_root", [False, True])
def test_list_and_detail_use_device_archive_and_keep_payload_task_compatible(tmp_path, driver_root):
    runtime, client, bundle, root = api_fixture(tmp_path, driver_root=driver_root)
    entry = saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    try:
        response = get(client, "/v1/devices/arm-01/saved-screen-gestures")
        assert response.status_code == 200, response.text
        listing = response.json()
        assert listing == {"contract_version": saved.CONTRACT_VERSION, "device_id": "arm-01",
                           "gestures": [entry]}
        jsonschema.validate(listing, SCHEMA)
        detail = get(client, "/v1/devices/arm-01/saved-screen-gestures/home")
        assert detail.status_code == 200 and detail.json() == entry
        task = {
            "task_id": "example-only", "task_type": "screen_gesture", "contract_version": "screen-gesture.v1",
            "device_id": "arm-01", "idempotency_key": "example-only", "expires_at": None,
            "correlation_id": "example-only", "payload": deepcopy(detail.json()["payload"]),
        }
        jsonschema.validate(task, TASK_SCHEMA)
        saved.save_gesture("arm-01", definition(
            "double_tap", gesture_id="home", expected_version=1, label="Back",
        ), bundle, root)
        assert task["payload"] == entry["payload"]
        assert task["payload"]["action"] == "swipe"
        # Only the explicit save above changes the archive; GETs do not touch it.
        before.pop(archive_path(root))
        for path, contents in before.items():
            assert path.read_bytes() == contents
        assert runtime.store.all_tasks() == []
        assert runtime.devices.driver("arm-01").action_log == []
        runtime.devices.driver("arm-01").read_state.assert_not_called()
        runtime.devices.driver("arm-01").read_gesture_state.assert_not_called()
        assert runtime.leases.get("arm-01") is None
    finally:
        asyncio.run(client.aclose())


def test_get_does_not_create_archive_or_lock_files(tmp_path):
    runtime, client, _, root = api_fixture(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    try:
        listing = get(client, "/v1/devices/arm-01/saved-screen-gestures")
        assert listing.status_code == 200 and listing.json()["gestures"] == []
        assert get(client, "/v1/devices/arm-01/saved-screen-gestures/missing").status_code == 404
        assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        assert runtime.devices.driver("arm-01").action_log == []
    finally:
        asyncio.run(client.aclose())


def test_public_archive_has_no_write_or_execution_routes(tmp_path):
    runtime, client, bundle, root = api_fixture(tmp_path)
    saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    before = archive_path(root).read_bytes()
    async def run():
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for route in ("/v1/devices/arm-01/saved-screen-gestures",
                          "/v1/devices/arm-01/saved-screen-gestures/home"):
                response = await client.request(method, route, json=definition())
                assert response.status_code == 405
    try:
        asyncio.run(run())
        assert archive_path(root).read_bytes() == before
        assert runtime.store.all_tasks() == []
    finally:
        asyncio.run(client.aclose())


def test_device_and_gesture_not_found_are_distinct(tmp_path):
    _, client, bundle, root = api_fixture(tmp_path)
    saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    try:
        missing_device = get(client, "/v1/devices/arm-99/saved-screen-gestures")
        assert missing_device.status_code == 404
        assert missing_device.json()["error"]["key"] == "device_not_found"
        wrong_device = get(client, "/v1/devices/arm-02/saved-screen-gestures/home")
        assert wrong_device.status_code == 404
        assert wrong_device.json()["error"]["key"] == "saved_gesture_not_found"
        assert get(client, "/v1/devices/arm-02/saved-screen-gestures").json()["gestures"] == []
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize("change", ["session", "unknown_session", "calibration"])
def test_public_stale_entries_stay_immutable_and_unusable(tmp_path, change):
    runtime, client, bundle, root = api_fixture(tmp_path)
    original = saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    if change == "session":
        runtime.devices._devices["arm-01"]["daemon_session"] = "new-session"
    elif change == "unknown_session":
        runtime.devices._devices["arm-01"]["daemon_session"] = None
    else:
        bundle["z"]["tapDepth"] = 7
        cb.save_bundle(bundle, root)
    before = archive_path(root).read_bytes()
    try:
        response = get(client, "/v1/devices/arm-01/saved-screen-gestures/home")
        assert response.status_code == 200
        entry = response.json()
        assert not entry["usable"] and entry["blocking_reasons"]
        assert entry["payload"] == original["payload"]
        assert entry["binding"] == original["binding"]
        assert entry["effect_status"] == "unverified"
        assert archive_path(root).read_bytes() == before
        jsonschema.validate(entry, SCHEMA)
    finally:
        asyncio.run(client.aclose())


def test_saved_usable_does_not_claim_live_readiness(tmp_path):
    runtime, client, bundle, root = api_fixture(tmp_path)
    saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    runtime.devices._devices["arm-01"]["status"] = "paused"
    try:
        entry = get(client, "/v1/devices/arm-01/saved-screen-gestures/home").json()
        assert entry["usable"]
        assert entry["effect_status_display"] == "手机效果未核验"
        assert runtime.devices._devices["arm-01"]["status"] == "paused"
        assert runtime.store.all_tasks() == []
    finally:
        asyncio.run(client.aclose())


def test_corrupt_archive_returns_structured_error_not_empty_list(tmp_path):
    _, client, _, root = api_fixture(tmp_path)
    archive_path(root).write_text("{broken")
    try:
        for route in ("/v1/devices/arm-01/saved-screen-gestures",
                      "/v1/devices/arm-01/saved-screen-gestures/home"):
            response = get(client, route)
            assert response.status_code == 409
            error = response.json()["error"]
            assert error["key"] == "saved_gesture_archive_invalid"
            assert all(error[key] for key in ("title", "detail", "suggestion", "trace_id"))
        assert archive_path(root).read_text() == "{broken"
    finally:
        asyncio.run(client.aclose())
