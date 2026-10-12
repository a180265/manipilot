from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeAsyncClient:
    response = FakeResponse(200, {"说明": "人工接管状态已更新", "事件": {"event_type": "human_takeover"}})
    requests: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def post(self, url, json):
        self.requests.append((url, json))
        return self.response

    async def aclose(self):
        return None


def make_client(tmp_path: Path, callback: str = "http://wx.test"):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "wx_callback_base_url": callback,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.device_configs = make_device_configs(1)
    return TestClient(app), runtime


@pytest.fixture(autouse=True)
def reset_fake_client(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response = FakeResponse(200, {"说明": "人工接管状态已更新", "事件": {"event_type": "human_takeover"}})
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


def body(**overrides):
    result = {
        "device_id": "arm-01",
        "conversation_id": "conv-01",
        "customer_id": "customer-01",
        "operator_id": "操作员甲",
        "role": "operator",
        "reason": "客户明确要求人工处理",
        "correlation_id": "takeover-corr-01",
        "client_request_id": "takeover-req-01",
        "contract_version": "human-takeover.v1",
        "confirmation_token": "确认执行",
        "context": {"view_session_id": "view-01"},
    }
    result.update(overrides)
    return result


def test_takeover_forwards_to_wx_and_audits(tmp_path):
    client, _runtime = make_client(tmp_path)
    with client:
        response = client.post("/v1/takeovers", json=body())
    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["status_display"] == "WX 已接管"
    assert len(FakeAsyncClient.requests) == 1
    url, forwarded = FakeAsyncClient.requests[0]
    assert url == "http://wx.test/internal/conversations/conv-01/takeover"
    assert forwarded["conversation_id"] == "conv-01"
    assert forwarded["customer_id"] == "customer-01"
    assert forwarded["context"]["device_id"] == "arm-01"


def test_takeover_requires_explicit_confirmation_and_context(tmp_path):
    client, _runtime = make_client(tmp_path)
    with client:
        missing = client.post("/v1/takeovers", json=body(confirmation_token=None))
        assert missing.status_code == 400
        assert missing.json()["error"]["key"] == "confirmation_required"
        missing_field = client.post("/v1/takeovers", json=body(conversation_id=""))
        assert missing_field.status_code == 400
        assert missing_field.json()["error"]["key"] == "missing_field"
        readonly = client.post("/v1/takeovers", json=body(role="readonly"))
        assert readonly.status_code == 403
        assert readonly.json()["error"]["key"] == "permission_denied"
        english_reason = client.post("/v1/takeovers", json=body(reason="manual takeover"))
        assert english_reason.status_code == 200
        assert english_reason.json()["accepted"] is True


def test_takeover_5xx_is_unknown_not_success(tmp_path):
    FakeAsyncClient.response = FakeResponse(503, {"message": "WX 暂时不可用"})
    client, _runtime = make_client(tmp_path)
    with client:
        response = client.post("/v1/takeovers", json=body())
    assert response.status_code == 503
    assert response.json()["error"]["key"] == "takeover_unknown"


def test_takeover_4xx_is_rejected(tmp_path):
    FakeAsyncClient.response = FakeResponse(409, {"message": "会话正在人工接管"})
    client, _runtime = make_client(tmp_path)
    with client:
        response = client.post("/v1/takeovers", json=body())
    assert response.status_code == 409
    assert response.json()["error"]["key"] == "takeover_rejected"


def test_takeover_http_200_explicit_rejection_is_not_success(tmp_path):
    FakeAsyncClient.response = FakeResponse(200, {
        "accepted": False,
        "说明": "会话已由其他销售人员接管",
    })
    client, _runtime = make_client(tmp_path)
    with client:
        response = client.post("/v1/takeovers", json=body())
    assert response.status_code == 409
    assert response.json()["error"]["key"] == "takeover_rejected"


def test_takeover_rejects_unknown_contract_fields(tmp_path):
    client, _runtime = make_client(tmp_path)
    with client:
        response = client.post("/v1/takeovers", json=body(unexpected="不要读取"))
    assert response.status_code == 400
    assert response.json()["error"]["key"] == "invalid_request"


def test_takeover_rejects_account_binding_mismatch(tmp_path):
    client, _runtime = make_client(tmp_path)
    with client:
        response = client.post(
            "/v1/takeovers",
            json=body(context={"wechat_account_id": "another-account"}),
        )
    assert response.status_code == 409
    assert response.json()["error"]["key"] == "account_mismatch"
