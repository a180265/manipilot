"""R7-S2 余项（R4-12-D/E）：相机会话身份与流撤销。

- 自报 requested_by/role 不作为操作者归属：直连（无可信头）时身份恒
  direct-unverified/readonly；伪造 role=admin 不提升任何权限；
  接管查看（privileged purpose）无可信身份直接 403 零副作用。
- 可信反代头（X-WX-Operator/X-WX-Operator-Role，在边界重建）被采信。
- 已建立 MJPEG 流在服务端撤销后 ≤5 秒停止供给（真实 uvicorn 回环服务；
  回退帧循环每帧查会话状态）。

替身边界：FakeArmDriver/合成帧（外部 IO 替身）；真实 Runtime/views/
路由/流循环/uvicorn 服务保留。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from fastapi.testclient import TestClient

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs


@pytest.fixture()
def env(tmp_path):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
        "camera_frame_interval_seconds": 0.2,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime = runtime
    asyncio.run(runtime.devices.register_all(make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=1,
        daemon_addr="127.0.0.1:8795", expected_frame_width=640,
        expected_frame_height=480)))
    app.state.device_configs = []
    with TestClient(app) as client:
        runtime.devices._devices["arm-01"].update({
            "status": "ready", "calibration_status": "validated"})
        yield {"client": client, "runtime": runtime}


def _create_body(purpose="主动查看", **over):
    body = {
        "device_id": "arm-01",
        "purpose": purpose,
        "requested_by": "attacker-claimed-admin",
        "correlation_id": str(uuid.uuid4()),
        "client_request_id": str(uuid.uuid4()),
        "role": "admin",
    }
    body.update(over)
    return body


def test_forged_identity_not_trusted(env):
    """直连自报 requested_by=admin/role=admin：会话身份记 direct-unverified。"""
    r = env["client"].post("/v1/camera/view-sessions", json=_create_body())
    assert r.status_code == 200, r.text
    sid = r.json()["view_session_id"]
    stored = env["runtime"].views.get(sid)
    assert stored["requested_by"] == "direct-unverified", stored


def test_takeover_requires_trusted_identity(env):
    """接管查看（privileged）：无可信头 403 零副作用；可信头放行且身份如实。"""
    r = env["client"].post("/v1/camera/view-sessions",
                           json=_create_body(purpose="接管查看"))
    assert r.status_code == 403, r.text
    assert "identity_unverified" in r.text or "自报" in r.text
    # 零副作用：会话未创建（列表为空）
    lst = env["client"].get("/v1/camera/view-sessions")
    items = lst.json().get("items", lst.json()) if lst.status_code == 200 else []
    assert not [s for s in (items or [])
                if s.get("purpose") == "接管查看"], items

    # 模拟可信反代在边界重建的身份头（LAN 模板 auth_request 后注入）
    r2 = env["client"].post(
        "/v1/camera/view-sessions", json=_create_body(purpose="接管查看"),
        headers={"X-WX-Operator": "op.admin", "X-WX-Operator-Role": "admin"})
    assert r2.status_code == 200, r2.text
    stored2 = env["runtime"].views.get(r2.json()["view_session_id"])
    assert stored2["requested_by"] == "op.admin", stored2


def test_renew_close_use_derived_identity(env):
    """续期/关闭同样不采信自报 requested_by（审计归属=推导身份）。"""
    r = env["client"].post("/v1/camera/view-sessions", json=_create_body())
    sid = r.json()["view_session_id"]
    r2 = env["client"].post(
        f"/v1/camera/view-sessions/{sid}/renew",
        json={"requested_by": "someone-forged", "correlation_id": "c1"})
    assert r2.status_code == 200, r2.text
    r3 = env["client"].request(
        "DELETE", f"/v1/camera/view-sessions/{sid}",
        json={"requested_by": "someone-forged", "reason": "测试关闭"})
    assert r3.status_code == 200, r3.text
    audit = env["runtime"].store.audit_events(limit=50)
    related = [a for a in audit if sid in str(a.get("detail") or {})
               or a.get("task_id") == sid]
    ops = {str(a.get("detail", {}).get("operator_id", "")) for a in related}
    assert not any("forged" in o or "attacker" in o for o in ops), ops


def test_stream_stops_within_5s_after_revocation(env, tmp_path):
    """已建立流：服务端撤销后 ≤5s 停止供给。

    测量口径修正（R8-F1）：先收完整图像帧（PNG）；DELETE 经同一真实
    uvicorn 服务；EOF 为唯一合格终止。完整实现与 20 次负载验收见
    tests/test_r8_f1_stream_revocation.py（本用例保留为身份套件内回归）。
    """
    from tests.test_r8_f1_stream_revocation import _one_revocation_run
    import threading
    import uvicorn

    r = env["client"].post("/v1/camera/view-sessions", json=_create_body())
    sid = r.json()["view_session_id"]
    app = env["client"].app
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        timeline = _one_revocation_run(f"http://127.0.0.1:{port}",
                                       env["runtime"])
        assert timeline["delete_status"] == 200
        assert timeline["png_frames_before_revoke"] >= 1
        assert timeline["eof_latency_s"] <= 5.0, timeline
    finally:
        server.should_exit = True
        thread.join(timeout=5)
