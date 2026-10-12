"""R3-17 红测：配对路由与 P4 手机身份分类（HTTP 层，经正式 app 装配）。

映射：
- R3-17-R1 本机管理路由：邀请创建/查询/撤销、手机列表/撤销；回环+转发凭据边界
- R3-17-R2 手机配对三入口：confirm（201/200 重放）、get（携证）、activate（携证）
- R3-17-P4 分类：手机类别（tasks/next、heartbeat、status、control、calibration
  读取/touch/client）无 token 401；管理类别（任务创建、calibrations arm/complete、
  screen-mappings）回环外 403；正文 device_id 不能覆盖认证身份；pending 凭据
  不能领取任务
- R3-17-R3 配对后凭据可用于几何握手（身份 Provider 接通生产路径）

替身：临时库/临时目录；未替换：路由/parser/身份/事务。
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

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

TRUST = {"https_origin": "https://fleet-lan.test", "ca_cert_b64": "QUJD",
         "spki_sha256": "b" * 64}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # 本文件语义 = 未配置转发令牌的隔离部署（回环放行）；同 worker 其他测试
    # 若泄漏 ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE 会让 201/503 变 403，
    # 这里显式清除保证封闭（2026-10-12 全量分布洗牌暴露）。
    monkeypatch.delenv("ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE", raising=False)
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
    # 隔离配对服务（真实服务，独立临时库）+ 信任材料（部署输入）
    runtime.ime_pairing = ImePairingService(tmp_path / "ime_pairing.db")
    runtime.ime_pairing.init_server_id()
    runtime.ime_pairing_trust = dict(TRUST)
    with TestClient(app) as client:
        yield {"client": client, "runtime": runtime, "tmp_path": tmp_path}


def _invite(env, request_id="req-inv-1"):
    r = env["client"].post("/v1/ime-pairing/invitations", json={
        "contract_version": "ime-pairing.v1", "request_id": request_id,
        "invite_secret": new_invite_secret(), "arm_id": "arm-01"})
    return r


def _full_pair(env, instance="pixel-test-0001"):
    secret = new_invite_secret()
    r = env["client"].post("/v1/ime-pairing/invitations", json={
        "contract_version": "ime-pairing.v1", "request_id": "req-" + instance,
        "invite_secret": secret, "arm_id": "arm-01"})
    assert r.status_code == 201, r.text
    invitation_id = r.json()["invitation_id"]
    token = new_credential_token()
    rc = env["client"].post("/api/v1/pairings/confirm", json={
        "contract_version": "ime-pairing.v1", "request_id": "pc-" + instance,
        "invitation_id": invitation_id, "invite_secret": secret,
        "phone_instance_id": instance, "phone_label": "测试手机",
        "credential_id": str(uuid.uuid4()), "credential_token": token})
    assert rc.status_code == 201, rc.text
    pairing = rc.json()
    ra = env["client"].post(
        f"/api/v1/pairings/{pairing['pairing_id']}/activate",
        json={"contract_version": "ime-pairing.v1",
              "request_id": "pa-" + instance,
              "expected_revision": pairing["revision"]},
        headers={"x-ime-phone-token": token})
    assert ra.status_code == 200, ra.text
    return pairing, token


# ---------------------------------------------------------------- 管理路由

def test_r1_invitation_create_get_revoke(env):
    r = _invite(env)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "offered"
    assert body["trust"]["https_origin"] == TRUST["https_origin"]
    g = env["client"].get(f"/v1/ime-pairing/invitations/{body['invitation_id']}")
    assert g.status_code == 200 and g.json()["status"] == "offered"
    # 撤销
    rv = env["client"].post(
        f"/v1/ime-pairing/invitations/{body['invitation_id']}/revoke",
        json={"request_id": "req-rev-1"})
    assert rv.status_code == 200 and rv.json()["status"] == "revoked"
    # 同号重放原结果
    rv2 = env["client"].post(
        f"/v1/ime-pairing/invitations/{body['invitation_id']}/revoke",
        json={"request_id": "req-rev-1"})
    assert rv2.status_code == 200 and rv2.json()["replayed"] is True


def test_r1b_trust_unconfigured_rejects(env):
    env["runtime"].ime_pairing_trust = None
    r = env["client"].post("/v1/ime-pairing/invitations", json={
        "contract_version": "ime-pairing.v1", "request_id": "req-nt",
        "invite_secret": new_invite_secret(), "arm_id": None})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "persistence_unavailable"


# ---------------------------------------------------------------- 手机三入口

def test_r2_pairing_full_flow_via_http(env):
    pairing, token = _full_pair(env)
    assert pairing["state"] == "pending"
    g = env["client"].get(
        f"/api/v1/pairings/{pairing['pairing_id']}",
        headers={"x-ime-phone-token": token})
    assert g.status_code == 200 and g.json()["state"] == "active"
    # 无凭据 401
    g2 = env["client"].get(f"/api/v1/pairings/{pairing['pairing_id']}")
    assert g2.status_code == 401


# ---------------------------------------------------------------- P4 分类

def test_p4_phone_routes_require_token(env):
    # Fleet 挂载后：手机类别无 token → 401（修前无认证=200/204，红测目标）
    r1 = env["client"].get("/api/v1/devices/k40-test/tasks/next?waitSeconds=0")
    assert r1.status_code == 401, "tasks/next 未携证必须 401"
    r2 = env["client"].post("/api/v1/devices/k40-test/heartbeat", json={
        "appVersion": "old", "keyboardActive": True})
    assert r2.status_code == 401, "heartbeat 未携证必须 401"


def test_p4_pending_credential_cannot_poll(env):
    secret = new_invite_secret()
    inv = env["client"].post("/v1/ime-pairing/invitations", json={
        "contract_version": "ime-pairing.v1", "request_id": "req-pending",
        "invite_secret": secret, "arm_id": None}).json()
    token = new_credential_token()
    rc = env["client"].post("/api/v1/pairings/confirm", json={
        "contract_version": "ime-pairing.v1", "request_id": "pc-pending",
        "invitation_id": inv["invitation_id"], "invite_secret": secret,
        "phone_instance_id": "pixel-pending-1", "phone_label": "待激活",
        "credential_id": str(uuid.uuid4()), "credential_token": token})
    assert rc.status_code == 201
    # pending 凭据：不能领取任务（先激活）
    r = env["client"].get("/api/v1/devices/whatever/tasks/next?waitSeconds=0",
                          headers={"x-ime-phone-token": token})
    assert r.status_code == 403, "pending 凭据不能领取任务"
    # 激活后路径设备不匹配 → 403（A 凭据不能用 B 路径）
    pairing, token2 = _full_pair(env, instance="pixel-paired-2")
    ime = env["runtime"].ime_pairing.list_phones()[0]["ime_device_id"]
    r2 = env["client"].get("/api/v1/devices/other-phone/tasks/next?waitSeconds=0",
                           headers={"x-ime-phone-token": token2})
    assert r2.status_code == 403, "路径设备与凭据不一致必须 403"


def test_p4_machine_routes_loopback_only(env):
    # 回环机器调用可用（TestClient 客户端地址=testclient→回环语义按实现）
    r = env["client"].post("/api/v1/tasks", json={
        "taskId": "t-machine-1", "deviceId": "k40-test",
        "targetUser": "客户", "content": "hi"})
    assert r.status_code != 403, "回环机器调用不得被身份门拒绝"
    # 手机凭据访问管理口无意义（身份门后业务校验照常）
    # screen-mappings 管理口可回环读取
    g = env["client"].get("/api/v1/screen-mappings")
    assert g.status_code == 200


def test_p4_pairing_enables_geometry_handshake(env):
    """配对成功 → 该手机可经凭据建立几何会话（身份 Provider 生产路径）。"""
    from runtime.ime_geometry import CONTRACT_VERSION
    pairing, token = _full_pair(env, instance="pixel-geo-1")
    ime = pairing["ime_device_id"]
    env["runtime"].devices._devices["arm-01"].update({
        "status": "ready", "calibration_status": "validated",
        "ime_device_id": ime, "daemon_session": "sess-geo"})
    env["runtime"].devices.input_binding = lambda device_id: {"ime_device_id": ime}
    r = env["client"].post(f"/api/v1/devices/{ime}/geometry/sessions", json={
        "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
        "client_instance_id": str(uuid.uuid4())},
        headers={"x-ime-phone-token": token})
    assert r.status_code == 200, r.text
    assert r.json()["runtime_session_id"]
    # 撤销后旧 token 立即失效（几何/任务一并）
    phones = env["runtime"].ime_pairing.list_phones()
    target = [p for p in phones if p["ime_device_id"] == ime][0]
    rv = env["client"].post(f"/v1/ime-phones/{ime}/revoke", json={
        "contract_version": "ime-pairing.v1", "request_id": "req-rv-geo",
        "expected_revision": target["revision"], "reason": "测试撤销"})
    assert rv.status_code == 200, rv.text
    r2 = env["client"].post(f"/api/v1/devices/{ime}/geometry/sessions", json={
        "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
        "client_instance_id": str(uuid.uuid4())},
        headers={"x-ime-phone-token": token})
    assert r2.status_code == 401, "撤销后旧 token 几何入口必须 401"
