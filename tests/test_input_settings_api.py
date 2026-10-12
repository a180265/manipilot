"""A-开关 API 测试（规格 §4.2/§4.2.3）：完整快照、权限（wx 拒绝）、幂等原快照、绑定门。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from fastapi.testclient import TestClient

from api.app import create_app
from runtime import input_settings as iset
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
        "screen_ime_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.device_configs = make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=7,
        daemon_addr="127.0.0.1:8795", expected_frame_width=64,
        expected_frame_height=48)
    settings_root = tmp_path / "data"
    runtime.devices.settings_data_root = lambda device_id: settings_root
    # 手机在线/键盘展开是实时观察：测试注入 provider，不依赖真实输入法服务
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-1", "online": True, "keyboardActive": True}]
    with TestClient(app) as c:
        # 设备注册由 app 启动路径完成（app.state.device_configs + lifespan）
        runtime.devices._devices["arm-01"].update({
            "status": "online", "calibration_status": "validated",
            "ime_online": True, "ime_device_id": "k40-1",
            "ime_keyboard_active": True, "daemon_session": "sess-1"})
        yield c, runtime, settings_root


def op(**extra):
    base = {"contract_version": "device-input-settings.v1",
            "operator_id": "操作员甲", "role": "operator", "reason": "规格测试",
            "correlation_id": "corr", "client_request_id": "req-1"}
    base.update(extra)
    return base


def test_get_defaults_off(tmp_path, client):
    c, runtime, root = client
    body = c.get("/v1/devices/arm-01/input-settings").json()
    assert body["enabled"] is False and body["revision"] == 0
    assert body["revoked_through_revision"] == 0 and body["updated_at"] is None
    assert body["calibration_ready"] is True and body["ime_online"] is True
    assert body["current_ime_device_id"] == "k40-1"
    assert body["effective"] is False
    # 未知查询参数明确拒绝（§4.2.2：不允许 require_input 弱化检查）
    r = c.get("/v1/devices/arm-01/input-settings?require_input=false")
    assert r.status_code == 400
    assert r.json()["error"]["key"] == "invalid_request"


def test_put_enable_then_get_effective(tmp_path, client):
    c, runtime, root = client
    r = c.put("/v1/devices/arm-01/input-settings", json={**op(), "enabled": True,
              "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    # §4.2.2：PUT 返回 {"settings": 状态对象, "replayed"}
    assert body["replayed"] is False
    assert body["settings"]["enabled"] is True
    assert body["settings"]["effective"] is True
    got = c.get("/v1/devices/arm-01/input-settings").json()
    assert got["enabled"] is True and got["revision"] == 1
    # 未知字段拒绝（§4.2.1：客户端不得提交 effective/撤销边界等）
    r2 = c.put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id="extra"), "enabled": False,
        "expected_revision": 1, "effective": True})
    assert r2.status_code == 400
    assert r2.json()["error"]["key"] == "invalid_request"


def test_put_requires_calibration(tmp_path, client):
    c, runtime, root = client
    runtime.devices._devices["arm-01"]["calibration_status"] = "due"
    r = c.put("/v1/devices/arm-01/input-settings", json={**op(), "enabled": True,
              "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r.status_code == 409
    assert r.json()["error"]["key"] == "calibration_not_ready"
    # 关闭不受校准限制
    r2 = c.put("/v1/devices/arm-01/input-settings", json={**op(client_request_id="close"),
               "enabled": False, "expected_revision": 0, "ime_device_id": None})
    assert r2.status_code == 200 and r2.json()["settings"]["enabled"] is False


def test_put_enable_requires_matching_binding(tmp_path, client):
    c, runtime, root = client
    # 请求手机与唯一绑定不一致 → 409 input_binding_mismatch（§4.2.1 第 4 步）
    r = c.put("/v1/devices/arm-01/input-settings", json={**op(), "enabled": True,
              "expected_revision": 0, "ime_device_id": "k40-other"})
    assert r.status_code == 409
    assert r.json()["error"]["key"] == "input_binding_mismatch"
    # 无绑定 → 409 input_binding_unavailable
    runtime.devices._devices["arm-01"]["ime_device_id"] = None
    r2 = c.put("/v1/devices/arm-01/input-settings", json={**op(client_request_id="nb"),
               "enabled": True, "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r2.status_code == 409
    assert r2.json()["error"]["key"] == "input_binding_unavailable"


def test_wx_role_rejected_i21(tmp_path, client):
    c, runtime, root = client
    r = c.put("/v1/devices/arm-01/input-settings", json={**op(role="wx"), "enabled": True,
              "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r.status_code == 403
    assert r.json()["error"]["key"] == "permission_denied"
    # readonly 同拒
    r2 = c.put("/v1/devices/arm-01/input-settings", json={**op(role="readonly"),
               "enabled": False, "expected_revision": 0})
    assert r2.status_code == 403


def test_revision_conflict_409_i16(tmp_path, client):
    c, runtime, root = client
    r1 = c.put("/v1/devices/arm-01/input-settings", json={**op(), "enabled": True,
               "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r1.status_code == 200
    # 同 client_request_id 不同内容 → request_conflict（§4.2.3）
    r2 = c.put("/v1/devices/arm-01/input-settings", json={
        **op(), "enabled": False, "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r2.status_code == 409
    assert r2.json()["error"]["key"] == "request_conflict"
    # 换新编号但旧 revision → revision_conflict
    r3 = c.put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id="stale"), "enabled": False,
        "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r3.status_code == 409
    assert r3.json()["error"]["key"] == "revision_conflict"


def test_idempotent_replay_returns_original_snapshot(tmp_path, client):
    c, runtime, root = client
    payload = {**op(client_request_id="same"), "enabled": True,
               "expected_revision": 0, "ime_device_id": "k40-1"}
    r1 = c.put("/v1/devices/arm-01/input-settings", json=payload)
    assert r1.status_code == 200 and r1.json()["replayed"] is False
    # 手机下线后重放原请求：返回原快照（原观察 effective=true），不重算
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-1", "online": False, "keyboardActive": False}]
    r2 = c.put("/v1/devices/arm-01/input-settings", json=payload)
    assert r2.status_code == 200 and r2.json()["replayed"] is True
    snapshot = r2.json()["settings"]
    assert snapshot["revision"] == 1 and snapshot["effective"] is True
    # 当前 GET 才反映当前观察
    got = c.get("/v1/devices/arm-01/input-settings").json()
    assert got["revision"] == 1 and got["effective"] is False
    # 摘要含 expected_revision：同编号改版本=冲突（§4.1 完整规范化摘要）
    r3 = c.put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id="same"), "enabled": True,
        "expected_revision": 1, "ime_device_id": "k40-1"})
    assert r3.status_code == 409
    assert r3.json()["error"]["key"] == "request_conflict"


def test_ime_offline_saves_but_not_effective(tmp_path, client):
    c, runtime, root = client
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-1", "online": False, "keyboardActive": False}]
    r = c.put("/v1/devices/arm-01/input-settings", json={**op(), "enabled": True,
              "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r.status_code == 200
    body = r.json()["settings"]
    assert body["enabled"] is True and body["effective"] is False
    assert any(b["code"] == "ime_offline" for b in body["blocking_reasons"])
    # 保存成功但当前不可输入：不是数据不一致（§4.2.2）
    assert body["ime_online"] is False and body["keyboard_active"] is False


def test_corrupt_settings_file_returns_503(tmp_path, client):
    c, runtime, root = client
    path = iset.settings_path("arm-01", root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt", encoding="utf-8")
    r = c.get("/v1/devices/arm-01/input-settings")
    # §4.2.3：持久资料不可安全解释 → 503，不伪装成正常 200
    assert r.status_code == 503
    assert r.json()["error"]["key"] == "input_settings_invalid"
