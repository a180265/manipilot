"""R3-17-P4 红测：Fleet 挂载的手机类别路由修前无认证（安全缺口复现）。

修前事实：adapters/screen_ime/routes.py 全部路由无认证（"仅限可信局域网"）。
本红测断言正确行为（未携证必须 401）——修前在快照上失败（实际 200/204），
修复（phone_identity_provider 注入 + require_active_phone）后通过。
不依赖任何 R3-17 新模块，可在修前快照独立运行。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from fastapi.testclient import TestClient

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs


@pytest.fixture()
def client(tmp_path):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "real",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": True,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.device_configs = make_device_configs(1)
    with TestClient(app) as c:
        yield c


def test_p4_tasks_next_requires_token(client):
    r = client.get("/api/v1/devices/k40-test/tasks/next?waitSeconds=0")
    assert r.status_code == 401, \
        f"tasks/next 未携证必须 401，实际 {r.status_code}（修前无认证=缺口）"


def test_p4_heartbeat_requires_token(client):
    r = client.post("/api/v1/devices/k40-test/heartbeat", json={
        "appVersion": "old", "keyboardActive": True})
    assert r.status_code == 401, \
        f"heartbeat 未携证必须 401，实际 {r.status_code}（修前无认证=缺口）"
