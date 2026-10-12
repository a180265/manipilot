import asyncio

import pytest

from runtime.errors import ApiError
from tests.helpers import ScriptedProbePlugin, make_device_configs, make_runtime, probe_payload, wait_terminal


class ReviewablePlugin(ScriptedProbePlugin):
    def review_task(self, task, ctx, review_context):
        ctx.state_set("held", None)
        return {"context_released": True}


def test_manual_review_releases_only_plugin_context_not_physical_status(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path, 1, plugin=ReviewablePlugin())
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            task = runtime.submit_task(probe_payload([
                {"action_display": "动作结果不明", "is_action": True, "outcome": "unknown"},
            ]), source="console", context={"operator_id": "操作员"})
            await wait_terminal(runtime, task["task_id"])
            context = {"operator_id": "操作员", "role": "operator", "reason": "已现场核对",
                       "correlation_id": "review", "client_request_id": "review-request"}
            result = runtime.review_task_context(
                task["task_id"], context, expected_device_id="arm-01",
                expected_task_status="unknown", review_context={"observed_state": "idle"})
            assert result["context_released"] is True
            assert runtime.get_task(task["task_id"])["status"] == "unknown"
            assert runtime.devices.get("arm-01")["status"] == "paused"
            assert runtime.review_task_context(
                task["task_id"], context, expected_device_id="arm-01",
                expected_task_status="unknown", review_context={"observed_state": "idle"}) == result
            with pytest.raises(ApiError):
                runtime.review_task_context(
                    task["task_id"], context, expected_device_id="arm-02",
                    expected_task_status="unknown", review_context={"observed_state": "idle"})
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


def test_review_api_rejects_pending_task_and_readonly_operator(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_stage_e_api import build_app, PAUSE_BODY
    app, runtime = build_app(tmp_path, n_devices=1)
    with TestClient(app) as client:
        assert client.post("/v1/devices/arm-01/pause", json=PAUSE_BODY).status_code == 200
        task = client.post("/v1/tasks", json=probe_payload([
            {"action_display": "等待任务", "is_action": True}])).json()
        body = {**PAUSE_BODY, "client_request_id": "pending-review",
                "expected_device_id": "arm-01", "expected_task_status": "queued",
                "review_context": {"observed_state": "idle"}}
        response = client.post(f"/v1/tasks/{task['task_id']}/review-context", json=body)
        assert response.status_code == 409
        assert client.get(f"/v1/tasks/{task['task_id']}").json()["status"] == "queued"
        body["role"] = "readonly"
        assert client.post(f"/v1/tasks/{task['task_id']}/review-context", json=body).status_code == 403
