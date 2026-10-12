"""Pre-enable HTTP policy is tested with temporary data and no hardware."""

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime


@pytest.fixture
def maintenance_app(tmp_path):
    config = PlatformConfig({
        "pre_enable_maintenance": True,
        "hardware_mode": "simulation",
        "db_path": "data/test.db",
        "plugin_dir": "plugins",
        "evidence_dir": "evidence",
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.device_configs = []
    with TestClient(app) as client:
        yield client, runtime, app


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/tasks"),
    ("POST", "/v1/devices/arm-01/resume"),
    ("POST", "/v1/devices/arm-01/connect"),
    ("PUT", "/v1/devices/arm-01/binding"),
    ("PATCH", "/v1/devices/arm-01/input-settings"),
    ("DELETE", "/v1/layout-templates/example"),
    ("POST", "/v1/emergency-stop/reset"),
    ("GET", "/api/v1/devices/phone/tasks/next"),
    ("GET", "/api/v1/tasks/old/control/next"),
    ("GET", "/v1/layout-templates"),
    ("GET", "/unreviewed-future-endpoint"),
])
def test_maintenance_rejects_before_endpoint_or_body_processing(maintenance_app, method, path):
    client, runtime, _ = maintenance_app
    before = runtime.store.all_tasks()
    response = client.request(method, path, content=b"not-json")
    assert response.status_code == 503, response.text
    assert response.json()["error"]["key"] == "pre_enable_maintenance"
    assert runtime.store.all_tasks() == before
    assert runtime._dispatch_task is None
    assert runtime._callback_flush_task is None


def test_status_build_and_schema_remain_available_without_ime_router(maintenance_app):
    client, _, app = maintenance_app
    status = client.get("/v1/system/status")
    assert status.status_code == 200
    assert status.json()["pre_enable_maintenance"] is True
    assert "启用前维护" in status.json()["operation_mode_display"]
    before = client.get("/v1/system/runtime-build").json()
    assert before == client.get("/v1/system/runtime-build").json()
    assert before["manifest_sha256"] == app.state.runtime_build["manifest_sha256"]
    schema = client.get("/openapi.json").json()
    assert "/v1/ime-phones" in schema["paths"]
    assert "/api/v1/devices/{device_id}/tasks/next" not in schema["paths"]
    assert not hasattr(app.state, "screen_ime")
    assert client.get("/v1/devices").status_code == 200
    assert client.get("/v1/tasks").status_code == 200


def test_emergency_stop_is_not_replaced_by_maintenance_rejection(maintenance_app, monkeypatch):
    client, runtime, _ = maintenance_app
    calls = []

    def stop(context):
        calls.append(context)
        return {"estop_active": True}

    monkeypatch.setattr(runtime, "op_estop", stop)
    response = client.post("/v1/emergency-stop", json={"reason": "test"})
    assert response.status_code == 200
    assert calls and calls[0]["reason"] == "test"
    assert client.post("/v1/emergency-stop/reset", json={}).status_code == 503


@pytest.mark.parametrize("encoded_delimiter", ["%3F", "%23"])
def test_guard_uses_the_same_decoded_path_as_the_router(maintenance_app, encoded_delimiter):
    client, _, app = maintenance_app
    reached = []

    @app.get("/v1/devices/{device_id}/unreviewed-probe")
    async def unreviewed_probe(device_id: str):
        reached.append(device_id)
        return {"unexpected": True}

    # URL parsing can treat a decoded ? or # as a delimiter and truncate the
    # path to an allowed device read; ASGI routing still sees the entire path.
    response = client.get(f"/v1/devices/probe{encoded_delimiter}/unreviewed-probe")
    assert response.status_code == 503, response.text
    assert reached == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", [
    "/v1/devices/arm-01%3F/input-settings",
    "/v1/devices/arm-01%23/input-settings",
    "/v1/devices/arm-01%3F/binding/options",
    "/v1/tasks/task-1%23/wx-navigation-review-receipts?request_id=review",
])
def test_independent_original_endpoint_reproducers_stay_frozen(maintenance_app, method, path):
    client, _, _ = maintenance_app
    assert client.request(method, path).status_code == 503
