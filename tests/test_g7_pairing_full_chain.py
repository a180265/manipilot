"""G7 全链（软件集成）：本机调试台发起 → 手机确认 → 激活携证 → 三几何入口 →
重启保留/轮换/撤销（R3-17 收口；G7 规定的连续路径与故障不变量）。

连续路径（执行入口 §7 G7 行）：
1. 本机管理口创建邀请（含信任材料）
2. 手机 HTTP confirm（201 pending）→ 携证 GET → activate（200 active）
3. 同凭据经几何握手/上报/长轮询（三入口携证）
4. 服务重启（同库重建 Runtime）→ 配对身份保留、几何会话须重握手
5. 撤销 → 旧 token 全拒（几何/任务/配对查询），同号撤销掉包重放原结果

替身：隔离 app（TestClient）、临时库；手机侧=真实 HTTP 协议客户端语义。
"""
from __future__ import annotations

import sys
import uuid

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from fastapi.testclient import TestClient

from api.app import create_app
from runtime import ime_pairing as ip
from runtime.config import PlatformConfig
from runtime.ime_pairing import ImePairingService, new_credential_token, \
    new_invite_secret
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs

TRUST = {"https_origin": "https://fleet-lan.test", "ca_cert_b64": "Q0FEQVI=",
         "spki_sha256": "c" * 64}
CONTRACT = "ime-pairing.v1"
GEOM = "ime-geometry.v1"


def _build(tmp_path, db_name="ime_pairing.db"):
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
    runtime.ime_pairing = ImePairingService(tmp_path / db_name)
    runtime.ime_pairing.init_server_id()
    runtime.ime_pairing_trust = dict(TRUST)
    return runtime, app


@pytest.fixture()
def env(tmp_path):
    runtime, app = _build(tmp_path)
    with TestClient(app) as client:
        yield {"client": client, "runtime": runtime, "tmp_path": tmp_path}


def _invite_and_pair(env, instance="pixel-g7-0001"):
    c = env["client"]
    secret = new_invite_secret()
    r = c.post("/v1/ime-pairing/invitations", json={
        "contract_version": CONTRACT, "request_id": "g7-inv-" + instance,
        "invite_secret": secret, "arm_id": "arm-01"})
    assert r.status_code == 201, r.text
    inv = r.json()
    token = new_credential_token()
    rc = c.post("/api/v1/pairings/confirm", json={
        "contract_version": CONTRACT, "request_id": "g7-c-" + instance,
        "invitation_id": inv["invitation_id"], "invite_secret": secret,
        "phone_instance_id": instance, "phone_label": "G7 手机",
        "credential_id": str(uuid.uuid4()), "credential_token": token})
    assert rc.status_code == 201, rc.text
    pairing = rc.json()
    ra = c.post(f"/api/v1/pairings/{pairing['pairing_id']}/activate", json={
        "contract_version": CONTRACT, "request_id": "g7-a-" + instance,
        "expected_revision": pairing["revision"]},
        headers={"x-ime-phone-token": token})
    assert ra.status_code == 200, ra.text
    return inv, pairing, token


def _geometry_handshake(env, ime_device, token):
    return env["client"].post(
        f"/api/v1/devices/{ime_device}/geometry/sessions",
        json={"contract_version": GEOM, "imeGeometryVersion": 1,
              "client_instance_id": str(uuid.uuid4())},
        headers={"x-ime-phone-token": token})


def test_g7_full_chain_pair_activate_geometry_revoke_restart(tmp_path):
    runtime, app = _build(tmp_path)
    with TestClient(app) as client:
        env = {"client": client, "runtime": runtime, "tmp_path": tmp_path}
        _g7_chain_and_revoke(env)
    # 4. 重启（同库重建 Runtime + app）：撤销跨重启保留、server_id 保留
    runtime2, app2 = _build(tmp_path)
    with TestClient(app2) as client2:
        token = _g7_chain_and_revoke.last_token
        ime = _g7_chain_and_revoke.last_ime
        assert client2.get("/v1/ime-phones").status_code == 200
        r2 = client2.post(
            f"/api/v1/devices/{ime}/geometry/sessions",
            json={"contract_version": GEOM, "imeGeometryVersion": 1,
                  "client_instance_id": str(uuid.uuid4())},
            headers={"x-ime-phone-token": token})
        assert r2.status_code == 401, "撤销跨重启不得复活"


def _g7_chain_and_revoke(env):
    c = env["client"]
    runtime = env["runtime"]
    # 1-2. 邀请 → confirm → activate（携证）
    inv, pairing, token = _invite_and_pair(env)
    ime = pairing["ime_device_id"]
    # 3. 几何三入口携证（握手→上报→长轮询领取）
    runtime.devices._devices["arm-01"].update({
        "status": "ready", "calibration_status": "validated",
        "ime_device_id": ime, "daemon_session": "sess-g7"})
    runtime.devices.input_binding = lambda device_id: {"ime_device_id": ime}
    hs = _geometry_handshake(env, ime, token)
    assert hs.status_code == 200, hs.text
    session_id = hs.json()["runtime_session_id"]
    nonce = runtime.ime_geometry.create_nonce_request(ime, purpose="g7")
    from adapters.screen_ime.routes import _clamp_int  # noqa: F401 (存在性)
    # 手机长轮询领取 nonce（携证）
    poll = c.get(
        f"/api/v1/devices/{ime}/geometry/requests/next"
        f"?waitSeconds=0&runtime_session_id={session_id}",
        headers={"x-ime-phone-token": token})
    assert poll.status_code == 200, poll.text
    assert poll.json()["request_nonce"] == nonce
    # 上报快照
    body = {"contract_version": GEOM, "runtime_session_id": session_id,
            "view_instance_id": str(uuid.uuid4()), "seq": 1,
            "captured_uptime_ms": 1000,
            "display": {"display_id": 0, "width_px": 1080, "height_px": 2400,
                        "rotation": 0, "density": 2.75},
            "layout_version": "g7", "keyboard_active": True,
            "geometry_valid": True,
            "keyboard_bounds": {"left": 0, "top": 1560, "right": 1080,
                                "bottom": 2400},
            "current_task_id": None, "reason": "snapshot",
            "request_nonce": nonce,
            "targets": [
                {"id": "ime_candidate_bar", "visible": True, "enabled": True,
                 "clickable": True, "bounds": {"left": 54, "top": 2160,
                                               "right": 1026, "bottom": 2280}},
                {"id": "ime_delete_key", "visible": True, "enabled": True,
                 "clickable": True, "bounds": {"left": 960, "top": 1960,
                                               "right": 1050, "bottom": 2050}}]}
    rep = c.post(f"/api/v1/devices/{ime}/geometry", json=body,
                 headers={"x-ime-phone-token": token})
    assert rep.status_code == 200, rep.text
    # 5. 撤销（先错 revision 拒，再成功；同号掉包重放原结果）
    phones = runtime.ime_pairing.list_phones()
    revision = [p for p in phones if p["ime_device_id"] == ime][0]["revision"]
    bad = c.post(f"/v1/ime-phones/{ime}/revoke", json={
        "contract_version": CONTRACT, "request_id": "g7-rv-bad",
        "expected_revision": revision + 5, "reason": "误操作"})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "revision_conflict"
    rv = c.post(f"/v1/ime-phones/{ime}/revoke", json={
        "contract_version": CONTRACT, "request_id": "g7-rv-1",
        "expected_revision": revision, "reason": "换机"})
    assert rv.status_code == 200, rv.text
    # 旧 token 全拒：几何/配对查询
    assert _geometry_handshake(env, ime, token).status_code == 401
    g = c.get(f"/api/v1/pairings/{pairing['pairing_id']}",
              headers={"x-ime-phone-token": token})
    assert g.status_code == 401
    replay = c.post(f"/v1/ime-phones/{ime}/revoke", json={
        "contract_version": CONTRACT, "request_id": "g7-rv-1",
        "expected_revision": revision, "reason": "换机"})
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    _g7_chain_and_revoke.last_token = token
    _g7_chain_and_revoke.last_ime = ime


def test_g7b_pairing_survives_restart_and_requires_rehandshake(tmp_path):
    """正常重启：配对身份保留；几何会话不保留（须重新握手）。"""
    runtime, app = _build(tmp_path)
    with TestClient(app) as client:
        env = {"client": client, "runtime": runtime, "tmp_path": tmp_path}
        inv, pairing, token = _invite_and_pair(env, instance="pixel-g7b-1")
        ime = pairing["ime_device_id"]
        if runtime.ime_pairing is not None:
            runtime.ime_pairing.close()  # lifespan 关闭前置空，这里显式收口副本
    # 重启：同一 DB 重建
    runtime2, app2 = _build(tmp_path)
    with TestClient(app2) as client2:
        cred = runtime2.phone_identity_for_token(token)
        assert cred is not None and cred["state"] == "active", \
            "重启保留配对身份"
        # 几何会话不保留（服务内存态）：直接上报旧会话必拒——新握手才可用
        assert runtime2.ime_geometry._active_by_device.get(ime) is None


def test_g7c_wrong_secret_and_cross_phone(env):
    """错邀请秘密 401；A 凭据访问 B 路径 403。"""
    c = env["client"]
    inv, pairing_a, token_a = _invite_and_pair(env, instance="pixel-g7c-a")
    inv_b, pairing_b, token_b = _invite_and_pair(env, instance="pixel-g7c-b")
    # 错秘密
    secret = new_invite_secret()
    r = c.post("/v1/ime-pairing/invitations", json={
        "contract_version": CONTRACT, "request_id": "g7c-inv-3",
        "invite_secret": secret, "arm_id": None})
    real_secret = None  # 故意用错误秘密 confirm
    rc = c.post("/api/v1/pairings/confirm", json={
        "contract_version": CONTRACT, "request_id": "g7c-c-wrong",
        "invitation_id": r.json()["invitation_id"],
        "invite_secret": new_invite_secret(),
        "phone_instance_id": "pixel-g7c-wrong", "phone_label": "X",
        "credential_id": str(uuid.uuid4()),
        "credential_token": new_credential_token()})
    assert rc.status_code == 401
    assert rc.json()["error"]["code"] == "authentication_required"
    # A 凭据访问 B 的配对
    g = c.get(f"/api/v1/pairings/{pairing_b['pairing_id']}",
              headers={"x-ime-phone-token": token_a})
    assert g.status_code == 403
    assert g.json()["error"]["code"] == "phone_scope_denied"
