"""L07/§11.13 ime-geometry.v1 协议验收：会话/序号/nonce/长轮询/新鲜快照。"""
import time
import uuid

import pytest

from runtime.ime_geometry import (CONTRACT_VERSION, ImeGeometryError,
                                  ImeGeometryService)

DEVICE = "k40-ime"


def handshake(svc, client=None):
    return svc.handshake(DEVICE, {
        "contract_version": CONTRACT_VERSION,
        "client_instance_id": client or str(uuid.uuid4()),
        "imeGeometryVersion": 1,
    })


def snapshot(session_id, view=None, seq=1, nonce=None, reason="layout_ready",
             geometry_valid=True, task_id="task-1"):
    def target(tid, visible=True):
        return {"id": tid, "bounds": ({"left": 10, "top": 2000, "right": 1070,
                                       "bottom": 2200} if visible else None),
                "visible": visible, "enabled": True,
                "clickable": visible}
    return {
        "contract_version": CONTRACT_VERSION,
        "ime_device_id": DEVICE,
        "runtime_session_id": session_id,
        "view_instance_id": view or str(uuid.uuid4()),
        "seq": seq,
        "request_nonce": nonce,
        "captured_uptime_ms": 123456,
        "display": {"display_id": 0, "width_px": 1080, "height_px": 2400,
                    "rotation": 0, "density": 2.75},
        "layout_version": "v7",
        "keyboard_active": True,
        "geometry_valid": geometry_valid,
        "keyboard_bounds": {"left": 0, "top": 1800, "right": 1080, "bottom": 2400},
        "current_task_id": task_id,
        "targets": [target("ime_candidate_bar"), target("ime_delete_key")],
        "reason": reason,
    }


def test_handshake_replay_same_client_returns_same_session():
    svc = ImeGeometryService()
    first = handshake(svc, client="11111111-1111-1111-1111-111111111111")
    again = handshake(svc, client="11111111-1111-1111-1111-111111111111")
    assert first["runtime_session_id"] == again["runtime_session_id"]
    assert first["contract_version"] == CONTRACT_VERSION


def test_new_client_revokes_old_session_and_replaced_client_gets_409():
    svc = ImeGeometryService()
    old = handshake(svc, client="22222222-2222-2222-2222-222222222222")
    new = handshake(svc, client="33333333-3333-3333-3333-333333333333")
    assert new["runtime_session_id"] != old["runtime_session_id"]
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, snapshot(old["runtime_session_id"]))
    assert (exc.value.status, exc.value.code) == (409, "ime_geometry_session_stale")
    with pytest.raises(ImeGeometryError) as exc:
        handshake(svc, client="22222222-2222-2222-2222-222222222222")
    assert (exc.value.status, exc.value.code) == (409, "ime_geometry_session_stale")


def test_report_accepts_and_returns_required_response_fields():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    response = svc.report(DEVICE, snapshot(session))
    assert response["accepted_seq"] == 1
    assert response["geometry_valid"] is True
    assert response["contract_version"] == CONTRACT_VERSION
    assert response["server_received_at"]


def test_same_seq_idempotent_keeps_first_receipt_different_digest_409():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    body = snapshot(session)
    first = svc.report(DEVICE, body)
    replay = svc.report(DEVICE, dict(body))  # 完全相同内容：幂等保留首收墙钟
    assert replay == first
    conflicting = snapshot(session, seq=1)
    conflicting["keyboard_active"] = False
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, conflicting)
    assert (exc.value.status, exc.value.code) == \
        (409, "ime_geometry_sequence_conflict")


def test_lower_seq_rejected():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    svc.report(DEVICE, snapshot(session, seq=5))
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, snapshot(session, seq=4, view=str(uuid.uuid4())))
    assert exc.value.code == "ime_geometry_sequence_conflict"


def test_retired_view_frame_cannot_reclaim():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    view_a, view_b = str(uuid.uuid4()), str(uuid.uuid4())
    svc.report(DEVICE, snapshot(session, view=view_a, seq=1))
    svc.report(DEVICE, snapshot(session, view=view_b, seq=2))
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, snapshot(session, view=view_a, seq=3))
    assert (exc.value.status, exc.value.code) == (409, "ime_geometry_session_stale")


def test_nonce_rules_snapshot_requires_nonce_others_forbid():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, snapshot(session, reason="snapshot", nonce=None))
    assert exc.value.code == "ime_geometry_invalid"
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, snapshot(session, reason="layout_ready",
                                    nonce=str(uuid.uuid4())))
    assert exc.value.code == "ime_geometry_invalid"
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, snapshot(session, reason="snapshot",
                                    nonce=str(uuid.uuid4())))
    assert exc.value.code == "ime_geometry_invalid"  # 非本服务器签发


def test_nonce_lifecycle_create_poll_report_and_expiry():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    nonce = svc.create_nonce_request(DEVICE)
    claimed = svc.poll_next(DEVICE, session, wait_seconds=0)
    assert claimed["request_nonce"] == nonce
    assert claimed["runtime_session_id"] == session
    # 已领取后无更多请求
    assert svc.poll_next(DEVICE, session, wait_seconds=0) is None
    response = svc.report(DEVICE, snapshot(session, seq=1, reason="snapshot",
                                           nonce=nonce))
    assert response["accepted_seq"] == 1
    # 同 nonce 二次上报拒绝
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, snapshot(session, seq=2, reason="snapshot", nonce=nonce))
    assert exc.value.code == "ime_geometry_nonce_expired"


def test_expired_nonce_rejected_on_report_and_not_dispatched():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    nonce = svc.create_nonce_request(DEVICE)
    rec_issued = time.monotonic()
    # 模拟超过 2 秒：直接改内部签发时刻（仅测试注入）
    svc._nonces[nonce]["issued_mono"] -= 2.5
    assert time.monotonic() - rec_issued < 1
    assert svc.poll_next(DEVICE, session, wait_seconds=0) is None
    with pytest.raises(ImeGeometryError) as exc:
        svc.report(DEVICE, snapshot(session, reason="snapshot", nonce=nonce))
    assert (exc.value.status, exc.value.code) == (409, "ime_geometry_nonce_expired")


def test_poll_wait_seconds_bounds_and_session_check():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    with pytest.raises(ImeGeometryError) as exc:
        svc.poll_next(DEVICE, session, wait_seconds=21)
    assert exc.value.code == "ime_geometry_invalid"
    with pytest.raises(ImeGeometryError) as exc:
        svc.poll_next(DEVICE, "not-a-session", wait_seconds=0)
    assert exc.value.code == "ime_geometry_session_stale"


def test_snapshot_validation_rejects_bad_bounds_and_targets():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    bad = snapshot(session)
    bad["keyboard_bounds"] = {"left": 100, "top": 0, "right": 50, "bottom": 10}
    with pytest.raises(ImeGeometryError):
        svc.report(DEVICE, bad)
    bad = snapshot(session)
    bad["display"]["rotation"] = 45
    with pytest.raises(ImeGeometryError):
        svc.report(DEVICE, bad)
    bad = snapshot(session)
    bad["targets"] = bad["targets"][:1]  # 缺删除目标
    with pytest.raises(ImeGeometryError):
        svc.report(DEVICE, bad)
    bad = snapshot(session)
    bad["targets"][0]["visible"] = False
    bad["targets"][0]["clickable"] = True  # 不可见仍可点击 → 拒
    with pytest.raises(ImeGeometryError):
        svc.report(DEVICE, bad)
    bad = snapshot(session)
    bad["geometry_valid"] = False
    bad["targets"][0]["bounds"] = {"left": 1, "top": 1, "right": 2, "bottom": 2}
    with pytest.raises(ImeGeometryError):
        svc.report(DEVICE, bad)


def test_fresh_snapshot_window_rules():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    nonce = svc.create_nonce_request(DEVICE)
    svc.poll_next(DEVICE, session, wait_seconds=0)
    svc.report(DEVICE, snapshot(session, reason="snapshot", nonce=nonce))
    ok, why = svc.fresh_snapshot(DEVICE, nonce)
    assert (ok, why) == (True, "ok")
    # 快照接收超过 1 秒 → 不新鲜
    svc._nonces[nonce]["answered_mono"] -= 1.5
    ok, why = svc.fresh_snapshot(DEVICE, nonce)
    assert (ok, why) == (False, "snapshot_stale")
    # nonce 发出超过 2 秒 → 过期（先于 1 秒快照窗判定）
    svc._nonces[nonce]["issued_mono"] -= 3.0
    ok, why = svc.fresh_snapshot(DEVICE, nonce)
    assert (ok, why) == (False, "nonce_expired")


def test_geometry_invalid_snapshot_fails_freshness():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    nonce = svc.create_nonce_request(DEVICE)
    svc.poll_next(DEVICE, session, wait_seconds=0)
    body = snapshot(session, reason="snapshot", nonce=nonce, geometry_valid=False)
    for t in body["targets"]:  # 失效几何不得携带可执行目标框
        t["bounds"] = None
        t["clickable"] = False
    svc.report(DEVICE, body)
    ok, why = svc.fresh_snapshot(DEVICE, nonce)
    assert (ok, why) == (False, "geometry_invalid")


def test_service_restart_invalidates_all_sessions():
    svc = ImeGeometryService()
    session = handshake(svc)["runtime_session_id"]
    restarted = ImeGeometryService()  # 进程重启 = 新实例
    with pytest.raises(ImeGeometryError) as exc:
        restarted.report(DEVICE, snapshot(session))
    assert exc.value.code == "ime_geometry_session_stale"
