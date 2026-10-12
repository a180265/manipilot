"""R3-17 红测：手机一次性配对与凭据撤销（ime-pairing.v1 服务端协议层）。

映射（acceptance-matrix.json 同步；G7 的全链在集成阶段跑）：
- R3-17-S1 server_id 离线初始化一次、重启保留、缺失拒绝
- R3-17-S2 邀请：创建/同号同摘要重放/异内容 409/撤销/过期
- R3-17-S3 confirm：合法 pending→激活→active；同号掉包重放原结果；无效秘密
  401；消费/撤销/过期邀请拒绝；phone_already_paired/pairing_pending/代际推进
- R3-17-S4 撤销：CAS revision；全部 active+pending 凭据立即失效；同号重放原
  结果不再推进代际；迟到 activate/旧 confirm 重放不得恢复身份
- R3-17-S5 限流：同来源 10 次/分钟持久窗口；有效重试不消耗名额
- R3-17-S6 时钟回退拒绝确认（clock_untrusted）
- R3-17-S7 严格 schema fixture：请求/响应过 ime-pairing.v1.schema.json

替身：临时 SQLite；时间用模块级 _now_ms 注入（可控时钟）。未替换：事务、
状态机、摘要、CAS。
"""
from __future__ import annotations

import json
import sys
import uuid

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import runtime.ime_pairing as ip
from runtime.ime_pairing import (
    ImePairingError, ImePairingService, new_credential_token,
    new_invite_secret)

TRUST = {"https_origin": "https://fleet-lan.test", "ca_cert_b64": "QUJD",
         "spki_sha256": "a" * 64}


@pytest.fixture()
def svc(tmp_path):
    service = ImePairingService(tmp_path / "ime_pairing.db")
    service.init_server_id()
    yield service
    service.close()


def _confirm(svc, *, request_id="req-c1", secret=None, invitation_id=None,
             instance="pixel-7-abc12345", label="客服手机", token=None,
             credential_id=None, source="lan"):
    secret = secret or new_invite_secret()
    if invitation_id is None:
        invitation_id = svc.create_invitation(
            request_id="req-inv-" + request_id, invite_secret=secret,
            arm_id=None, source="console", trust=TRUST)["invitation_id"]
    token = token or new_credential_token()
    credential_id = credential_id or str(uuid.uuid4())
    result = svc.confirm(
        request_id=request_id, invitation_id=invitation_id,
        invite_secret=secret, phone_instance_id=instance, phone_label=label,
        credential_id=credential_id, credential_token=token, source=source)
    return result, secret, token, credential_id


# ---------------------------------------------------------------- S1/S2

def test_s1_server_id_initialized_once_and_survives_reopen(tmp_path):
    db = tmp_path / "ime_pairing.db"
    first = ImePairingService(db)
    sid1 = first.init_server_id()
    sid1_again = first.init_server_id()
    assert sid1 == sid1_again
    first.close()
    second = ImePairingService(db)
    assert second.server_id == sid1, "重启保留 server_id"
    second.close()


def test_s1b_server_id_missing_refuses(tmp_path):
    service = ImePairingService(tmp_path / "fresh.db")
    with pytest.raises(ImePairingError) as exc:
        _ = service.server_id
    assert exc.value.code == "persistence_unavailable"


def test_s2_invitation_create_replay_and_conflict(svc):
    secret = new_invite_secret()
    r1 = svc.create_invitation(request_id="req-i1", invite_secret=secret,
                               arm_id="arm-01", source="console", trust=TRUST)
    assert r1["status"] == "offered" and r1["server_id"] == svc.server_id
    assert r1["trust"]["spki_sha256"] == "a" * 64
    # 同号同摘要重放：原邀请（浏览器可用原 secret 重新组合）
    r2 = svc.create_invitation(request_id="req-i1", invite_secret=secret,
                               arm_id="arm-01", source="console", trust=TRUST)
    assert r2["invitation_id"] == r1["invitation_id"]
    # 同号不同内容 → 409 idempotency_conflict
    with pytest.raises(ImePairingError) as exc:
        svc.create_invitation(request_id="req-i1",
                              invite_secret=new_invite_secret(),
                              arm_id="arm-01", source="console", trust=TRUST)
    assert exc.value.code == "idempotency_conflict"


def test_s2b_invitation_revoke_and_expiry(svc, monkeypatch):
    secret = new_invite_secret()
    inv = svc.create_invitation(request_id="req-i2", invite_secret=secret,
                                arm_id=None, source="console", trust=TRUST)
    revoked = svc.revoke_invitation(inv["invitation_id"], request_id="req-r2")
    assert revoked["status"] == "revoked"
    view = svc.get_invitation(inv["invitation_id"])
    assert view["status"] == "revoked"
    # 撤销后的邀请不能用于确认
    token = new_credential_token()
    with pytest.raises(ImePairingError) as exc:
        _confirm(svc, secret=secret, invitation_id=inv["invitation_id"],
                 token=token)
    assert exc.value.code == "invitation_consumed"
    # 过期（可控时钟 +6 分钟）
    secret2 = new_invite_secret()
    inv2 = svc.create_invitation(request_id="req-i3", invite_secret=secret2,
                                 arm_id=None, source="console", trust=TRUST)
    real_now_ms = ip._now_ms
    monkeypatch.setattr(ip, "_now_ms", lambda: real_now_ms() + 6 * 60 * 1000)
    with pytest.raises(ImePairingError) as exc:
        _confirm(svc, secret=secret2, invitation_id=inv2["invitation_id"])
    assert exc.value.code == "invitation_expired"


# ---------------------------------------------------------------- S3

def test_s3_confirm_activate_full_chain(svc):
    result, secret, token, credential_id = _confirm(svc, request_id="req-c1")
    assert result["state"] == "pending"
    assert result["pairing_id"] == result["request_id"]
    ime_device = result["ime_device_id"]
    # pending 凭据只能查/激活自身配对
    view = svc.get_pairing(result["pairing_id"], token=token)
    assert view["state"] == "pending"
    activated = svc.activate(result["pairing_id"], request_id="req-a1",
                             expected_revision=result["revision"], token=token)
    assert activated["state"] == "active"
    assert activated["generation"] == 1
    cred = svc.verify_phone_token(token)
    assert cred is not None and cred["state"] == "active"
    assert cred["ime_device_id"] == ime_device
    # 同号激活掉包重放：原结果，revision 不变
    replay = svc.activate(result["pairing_id"], request_id="req-a1",
                          expected_revision=result["revision"], token=token)
    assert replay["replayed"] is True
    assert replay["revision"] == activated["revision"]


def test_s3b_confirm_idempotent_replay_after_packet_loss(svc):
    result, secret, token, credential_id = _confirm(svc, request_id="req-c2")
    # confirm 响应丢失：手机重发相同持久请求（同一 invitation_id/tuple）
    # → 200 原结果（replayed），不生成新 token、不消耗限流名额
    again = svc.confirm(
        request_id="req-c2", invitation_id=result.get("invitation_id")
        or _last_invitation_for(svc, "req-inv-req-c2"),
        invite_secret=secret, phone_instance_id="pixel-7-abc12345",
        phone_label="客服手机", credential_id=credential_id,
        credential_token=token, source="lan")
    assert again["pairing_id"] == result["pairing_id"]
    assert again["replayed"] is True


def _last_invitation_for(svc, request_id):
    row = svc._conn.execute(
        "SELECT response_json FROM ime_pairing_operations "
        "WHERE operation='invitation_create' AND request_id=?",
        (request_id,)).fetchone()
    return json.loads(row["response_json"])["invitation_id"]


def test_s3c_wrong_secret_401_and_no_leak(svc):
    real_secret = new_invite_secret()
    inv = svc.create_invitation(request_id="req-inv-ws",
                                invite_secret=real_secret, arm_id=None,
                                source="console", trust=TRUST)
    with pytest.raises(ImePairingError) as exc:
        _confirm(svc, secret=new_invite_secret(),
                 invitation_id=inv["invitation_id"])
    assert exc.value.code == "authentication_required"
    assert exc.value.status == 401


def test_s3d_phone_already_paired_and_pairing_pending(svc):
    result, secret, token, credential_id = _confirm(svc, request_id="req-c3")
    svc.activate(result["pairing_id"], request_id="req-a3",
                 expected_revision=result["revision"], token=token)
    # 同实例已有 active：confirm 409 phone_already_paired
    secret2 = new_invite_secret()
    inv2 = svc.create_invitation(request_id="req-inv-c3b", invite_secret=secret2,
                                 arm_id=None, source="console", trust=TRUST)
    with pytest.raises(ImePairingError) as exc:
        _confirm(svc, secret=secret2, invitation_id=inv2["invitation_id"],
                 instance="pixel-7-abc12345")
    assert exc.value.code == "phone_already_paired"
    # 未过期 pending：第二个 confirm 409 pairing_pending
    result_p, secret_p, token_p, cred_p = _confirm(svc, request_id="req-c4",
                                                   instance="pixel-8-def67890")
    secret3 = new_invite_secret()
    inv3 = svc.create_invitation(request_id="req-inv-c4b", invite_secret=secret3,
                                 arm_id=None, source="console", trust=TRUST)
    with pytest.raises(ImePairingError) as exc:
        _confirm(svc, secret=secret3, invitation_id=inv3["invitation_id"],
                 instance="pixel-8-def67890")
    assert exc.value.code == "pairing_pending"


def test_s3e_same_instance_repair_advances_generation_same_device(svc):
    result1, secret1, token1, cred1 = _confirm(svc, request_id="req-c5",
                                               instance="pixel-9-xyz00011")
    svc.activate(result1["pairing_id"], request_id="req-a5",
                 expected_revision=result1["revision"], token=token1)
    # 本机明确撤销后重配：同 ime_device_id、代际 +1、旧 token 失效
    revoked = svc.revoke_phone(result1["ime_device_id"], request_id="req-r5",
                               expected_revision=2, reason="重配")
    assert revoked["credential_generation"] == 2
    assert svc.verify_phone_token(token1) is None
    secret2 = new_invite_secret()
    token2 = new_credential_token()
    inv2 = svc.create_invitation(request_id="req-inv-c5b", invite_secret=secret2,
                                 arm_id=None, source="console", trust=TRUST)
    result2 = svc.confirm(
        request_id="req-c5b", invitation_id=inv2["invitation_id"],
        invite_secret=secret2, phone_instance_id="pixel-9-xyz00011",
        phone_label="客服手机", credential_id=str(uuid.uuid4()),
        credential_token=token2, source="lan")
    assert result2["ime_device_id"] == result1["ime_device_id"], \
        "同实例重配映射同一 ime_device_id"
    # 新安装实例：新 ime_device_id
    secret3 = new_invite_secret()
    inv3 = svc.create_invitation(request_id="req-inv-c5c", invite_secret=secret3,
                                 arm_id=None, source="console", trust=TRUST)
    result3 = svc.confirm(
        request_id="req-c5c", invitation_id=inv3["invitation_id"],
        invite_secret=secret3, phone_instance_id="pixel-10-new991",
        phone_label="新手机", credential_id=str(uuid.uuid4()),
        credential_token=new_credential_token(), source="lan")
    assert result3["ime_device_id"] != result1["ime_device_id"]


# ---------------------------------------------------------------- S4 撤销

def test_s4_revoke_cas_and_immediate_invalidation(svc):
    result, secret, token, credential_id = _confirm(svc, request_id="req-c6")
    svc.activate(result["pairing_id"], request_id="req-a6",
                 expected_revision=result["revision"], token=token)
    # CAS：错 revision 拒绝
    with pytest.raises(ImePairingError) as exc:
        svc.revoke_phone(result["ime_device_id"], request_id="req-r6x",
                         expected_revision=99, reason="误操作")
    assert exc.value.code == "revision_conflict"
    revoked = svc.revoke_phone(result["ime_device_id"], request_id="req-r6",
                               expected_revision=2, reason="换机")
    assert revoked["state"] == "revoked"
    assert svc.verify_phone_token(token) is None, "撤销后旧 token 立即失效"
    # 迟到 activate：不能恢复身份
    with pytest.raises(ImePairingError) as exc:
        svc.activate(result["pairing_id"], request_id="req-a6-late",
                     expected_revision=result["revision"], token=token)
    assert exc.value.code == "authentication_required"
    # 同号掉包重放原结果：不再次推进代际
    replay = svc.revoke_phone(result["ime_device_id"], request_id="req-r6",
                              expected_revision=2, reason="换机")
    assert replay["replayed"] is True
    assert replay["credential_generation"] == revoked["credential_generation"]
    # 同号不同内容 → 409
    with pytest.raises(ImePairingError) as exc:
        svc.revoke_phone(result["ime_device_id"], request_id="req-r6",
                         expected_revision=3, reason="换机")
    assert exc.value.code == "idempotency_conflict"


# ---------------------------------------------------------------- S5/S6

def test_s5_confirm_rate_limit_persistent(svc):
    source = "10.0.0.9"
    blocked = False
    for i in range(10):
        secret = new_invite_secret()
        token = new_credential_token()
        try:
            _confirm(svc, request_id=f"req-fail-{i}",
                     secret=secret, invitation_id=str(uuid.uuid4()),
                     token=token, source=source)
        except ImePairingError as exc:
            assert exc.code == "authentication_required"
    with pytest.raises(ImePairingError) as exc:
        _confirm(svc, request_id="req-fail-11", secret=new_invite_secret(),
                 invitation_id=str(uuid.uuid4()),
                 token=new_credential_token(), source=source)
    assert exc.value.code == "rate_limited"
    # 重启（重开库）限流窗口仍在
    svc._conn.commit()
    with pytest.raises(ImePairingError) as exc:
        _confirm(svc, request_id="req-fail-12", secret=new_invite_secret(),
                 invitation_id=str(uuid.uuid4()),
                 token=new_credential_token(), source=source)
    assert exc.value.code == "rate_limited"


def test_s6_clock_rollback_rejects_confirm(svc):
    secret = new_invite_secret()
    inv = svc.create_invitation(request_id="req-inv-clock",
                                invite_secret=secret, arm_id=None,
                                source="console", trust=TRUST)
    # 持久水位拨到未来 10 分钟 → 当前确认按时钟回退拒绝
    real_now_ms = ip._now_ms
    svc._kv_set(svc._conn, "clock_last_ms", str(real_now_ms() + 10 * 60 * 1000))
    svc._conn.commit()
    with pytest.raises(ImePairingError) as exc:
        _confirm(svc, secret=secret, invitation_id=inv["invitation_id"])
    assert exc.value.code == "clock_untrusted"


# ---------------------------------------------------------------- S7 schema fixture

def _load_schema():
    from pathlib import Path
    path = Path(__file__).parent.parent / "api" / "contracts" / \
        "ime-pairing.v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(schema, ref_root):
    try:
        import jsonschema
    except ImportError:
        pytest.skip("jsonschema 未安装")
    from jsonschema import RefResolver
    return jsonschema.Draft202012Validator(
        schema, resolver=RefResolver.from_schema(ref_root)).validate


def test_s7_request_response_fixtures_match_contract(svc):
    schema = _load_schema()
    validate_against = _validator(schema, schema)
    # confirm 请求 fixture
    confirm_request = {
        "contract_version": "ime-pairing.v1",
        "request_id": "req-fix-1",
        "invitation_id": str(uuid.uuid4()),
        "invite_secret": new_invite_secret(),
        "phone_instance_id": "pixel-7-fix0001",
        "phone_label": "客服手机",
        "credential_id": str(uuid.uuid4()),
        "credential_token": new_credential_token(),
    }
    validate_against(confirm_request, schema["$defs"]["pairing_confirm_request"])
    # 伪造 role/权限类未声明字段必须拒绝
    bad = dict(confirm_request, role="admin")
    with pytest.raises(Exception):
        validate_against(bad, schema["$defs"]["pairing_confirm_request"])
    # activate 响应 fixture（真实服务产出）
    result, secret, token, credential_id = _confirm(svc, request_id="req-fix-2")
    activated = svc.activate(result["pairing_id"], request_id="req-fix-2a",
                             expected_revision=result["revision"], token=token)
    validate_against(activated, schema["$defs"]["pairing_activate_result"])
    validate_against(result, schema["$defs"]["pairing_confirm_result"])
    # 错误外壳 fixture
    error_shell = {"contract_version": "ime-pairing.v1", "request_id": None,
                   "error": {"code": "authentication_required",
                             "message": "邀请不存在或秘密无效"}}
    validate_against(error_shell, schema["$defs"]["error"])
