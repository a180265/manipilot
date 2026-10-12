"""device-safety-view.v1（核心通用安全视图）与帧桥换绑门的闭环测试。

- 服务签名矩阵：缺配置 503 / 缺头或坏签名 401 / 正常 200；
- 视图形状：空闲 runtime → mechanical_safe=confirmed、held_session_id=None；
- 帧桥门（scripts.camera_frame_server._require_fleet_device_safe）：
  对 fake fleet 的确认放行 / 阻断拒绝两向。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from fastapi.testclient import TestClient

from api.app import create_app
from runtime import service_signing
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs

KEY_ID = "safety-view-test"
SECRET_HEX = "ab" * 32


def _write_keys(tmp_path: Path) -> Path:
    keys_file = tmp_path / "safety_keys.json"
    keys_file.write_text(json.dumps({
        "format_version": 1,
        "keys": [{"key_id": KEY_ID, "secret_hex": SECRET_HEX, "enabled": True}],
    }), encoding="utf-8")
    return keys_file


def _signed_headers(method: str, path: str, *, secret_hex: str = SECRET_HEX,
                    timestamp: str | None = None, nonce: str | None = None,
                    key_id: str = KEY_ID, bad_signature: bool = False) -> dict:
    ts = timestamp or datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    nc = nonce or uuid.uuid4().hex
    base = service_signing.signature_base(method, path, ts, nc, b"")
    digest = hmac.new(bytes.fromhex(secret_hex), base, hashlib.sha256).hexdigest()
    if bad_signature:
        digest = "00" * 32
    return {"X-WX-Key-Id": key_id, "X-WX-Time": ts,
            "X-WX-Nonce": nc, "X-WX-Signature": digest}


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.device_configs = make_device_configs(1)
    keys_file = _write_keys(tmp_path)
    monkeypatch.setenv("FLEET_SAFETY_KEYS_FILE", str(keys_file))
    with TestClient(app) as client:
        yield client, runtime, keys_file


def _get(client: TestClient, keys_file: Path | None, **sign_kwargs) -> object:
    headers = {}
    if keys_file is not None:
        headers = _signed_headers("GET", "/v1/devices/arm-01/safety-view", **sign_kwargs)
    return client.get("/v1/devices/arm-01/safety-view", headers=headers)


def test_missing_keys_config_rejects_503(fleet, monkeypatch):
    client, _runtime, _keys = fleet
    monkeypatch.delenv("FLEET_SAFETY_KEYS_FILE")
    r = _get(client, None)
    assert r.status_code == 503 and r.json()["error"]["key"] == "authorization_unavailable"


def test_unsigned_request_rejected_403(fleet):
    client, _runtime, _keys = fleet
    r = client.get("/v1/devices/arm-01/safety-view")
    # 与源仓扩展端点一致：401 类核验失败统一映射 authorization_invalid→403
    assert r.status_code == 403
    assert r.json()["error"]["key"] == "authorization_invalid"


def test_bad_signature_rejected(fleet):
    client, _runtime, _keys = fleet
    r = _get(client, Path("/dev/null"), bad_signature=True)
    assert r.status_code == 403 and r.json()["error"]["key"] == "authorization_invalid"


def test_signed_view_confirmed_on_idle_runtime(fleet):
    client, _runtime, _keys = fleet
    r = _get(client, Path("/dev/null"))
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["contract_version"] == "device-safety-view.v1"
    assert view["device_id"] == "arm-01"
    assert view["mechanical_safe"] == "confirmed"
    assert view["in_flight_task_ids"] == []
    assert view["unresolved_task_ids"] == []
    assert view["held_session_id"] is None
    assert view["lease_active"] is False
    assert view["blocking_reasons"] == []
    assert isinstance(view["snapshot_revision"], int) and view["snapshot_revision"] >= 1


def test_in_flight_task_blocks_the_view(fleet):
    client, runtime, _keys = fleet
    runtime.store.save_task({
        "task_id": "t-inflight", "device_id": "arm-01", "task_type": "demo",
        "status": "queued", "payload": {}, "created_at": "2026-10-12T00:00:00Z",
    })
    r = _get(client, Path("/dev/null"))
    view = r.json()
    assert view["in_flight_task_ids"] == ["t-inflight"]
    assert view["mechanical_safe"] == "unknown"
    assert "device_busy" in view["blocking_reasons"]


# ---------------- 帧桥门（_require_fleet_device_safe） ----------------

def _fake_fleet(view: dict | None):
    """返回固定安全视图的 fake fleet（不验签，只验帧桥门的消费行为）。"""
    holder: dict = {"view": view}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(holder["view"] or {"error": "x"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, holder


@pytest.fixture()
def frame_gate_env(monkeypatch, tmp_path):
    keys_file = _write_keys(tmp_path)
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_BASE_URL", "http://127.0.0.1:0")
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_KEYS_FILE", str(keys_file))
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_KEY_ID", KEY_ID)
    yield


def test_frame_gate_passes_confirmed_view(frame_gate_env, monkeypatch):
    import importlib
    fr = importlib.import_module("scripts.camera_frame_server")
    confirmed = {
        "contract_version": "device-safety-view.v1", "device_id": "arm-01",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "mechanical_safe": "confirmed", "in_flight_task_ids": [],
        "unresolved_task_ids": [], "held_session_id": None,
        "lease_active": False, "blocking_reasons": [],
    }
    server, holder = _fake_fleet(confirmed)
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_BASE_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    try:
        fr._require_fleet_device_safe("arm-01")  # 不抛即通过
    finally:
        server.shutdown()


def test_frame_gate_blocks_busy_view(frame_gate_env, monkeypatch):
    import importlib
    fr = importlib.import_module("scripts.camera_frame_server")
    busy = {
        "contract_version": "device-safety-view.v1", "device_id": "arm-01",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "mechanical_safe": "unknown", "in_flight_task_ids": ["t-1"],
        "unresolved_task_ids": [], "held_session_id": None,
        "lease_active": False, "blocking_reasons": ["device_busy"],
    }
    server, holder = _fake_fleet(busy)
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_BASE_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    try:
        with pytest.raises(fr._FrameReject) as exc:
            fr._require_fleet_device_safe("arm-01")
        assert exc.value.code == "device_safety_unresolved"
    finally:
        server.shutdown()
