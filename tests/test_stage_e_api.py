"""阶段 E 验收测试（后端）：设备/任务/运维/审计/能力接口、中文状态、权限、
危险操作二次确认、SSE 状态推送、实时画面查看会话全流程。

退出条件：前后端可独立启动；中文状态、权限、断线和危险操作验收通过。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import validate as schema_validate

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tests.helpers import ScriptedProbePlugin, make_device_configs

CONTRACTS = Path(__file__).parents[1] / "api" / "contracts"

PAUSE_BODY = {
    "operator_id": "操作员甲", "role": "operator", "reason": "现场检查，暂停设备",
    "correlation_id": "corr-e2e-1", "client_request_id": "req-e2e-1",
}


def build_app(tmp_path: Path, *, ttl_seconds: int = 60, n_devices: int = 10):
    """构建未启动的 FastAPI 应用（测试用 async transport 手动管理生命周期）。"""
    config = PlatformConfig(
        {"db_path": str(tmp_path / "api.db"), "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation",
         "view_session_ttl_seconds": ttl_seconds, "view_session_renew_advice_seconds": max(ttl_seconds // 3, 1),
         "camera_frame_interval_seconds": 0.05},
        root=tmp_path,
    )
    runtime = ArmFleetRuntime(config)
    runtime.plugins.register_instance_for_tests(ScriptedProbePlugin(), ScriptedProbePlugin.manifest)
    app = create_app(runtime)
    app.state.runtime_factory = lambda: runtime
    app.state.device_configs = make_device_configs(n_devices)
    return app, runtime


def make_client(tmp_path: Path, *, ttl_seconds: int = 60, n_devices: int = 10) -> TestClient:
    app, _runtime = build_app(tmp_path, ttl_seconds=ttl_seconds, n_devices=n_devices)
    return TestClient(app)


def test_devices_list_chinese_and_contract(tmp_path):
    client = make_client(tmp_path)
    with client:
        response = client.get("/v1/devices")
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["devices"]) == 10
        for device in payload["devices"]:
            assert device["status"] == "ready" and device["status_display"] == "可用"
            assert device["current_ui_state_display"]
            assert device["camera_quality_display"] == "清晰"
            assert device["wechat_account_id"].startswith("sim-acc-")
            assert device["binding_note"], "仿真绑定必须注明待项目方确认"

        detail = client.get("/v1/devices/arm-03").json()
        assert detail["device_id"] == "arm-03"
        assert "driver_versions" in detail and "lease_id" in detail

        missing = client.get("/v1/devices/arm-99")
        assert missing.status_code == 404
        error = missing.json()["error"]
        assert error["title"] == "设备不存在" and error["suggestion"], "错误必须返回中文标题与处理建议"


def test_ops_permission_and_confirmation(tmp_path):
    client = make_client(tmp_path, n_devices=2)
    with client:
        # 空白原因仍然拒绝
        response = client.post("/v1/devices/arm-01/pause", json={
            "operator_id": "操作员甲", "role": "operator", "reason": " \t\n",
            "correlation_id": "c", "client_request_id": "r"})
        assert response.status_code == 400
        assert response.json()["error"]["key"] == "missing_field"

        # 只读角色被拒绝（后端必须再次拒绝）
        readonly = dict(PAUSE_BODY, role="readonly", client_request_id="req-readonly")
        response = client.post("/v1/devices/arm-01/pause", json=readonly)
        assert response.status_code == 403
        assert response.json()["error"]["key"] == "permission_denied"

        # 正常暂停
        response = client.post("/v1/devices/arm-01/pause", json=PAUSE_BODY)
        assert response.status_code == 200
        assert response.json()["status_display"] == "暂停"

        # 隔离是危险操作：缺二次确认拒绝
        quarantine_body = dict(PAUSE_BODY, reason="疑似异常需要隔离", client_request_id="req-quar")
        response = client.post("/v1/devices/arm-01/quarantine", json=quarantine_body)
        assert response.status_code == 400
        assert response.json()["error"]["key"] == "confirmation_required"
        quarantine_body["confirmation_token"] = "确认执行"
        response = client.post("/v1/devices/arm-01/quarantine", json=quarantine_body)
        assert response.status_code == 200
        assert response.json()["status_display"] == "隔离"

        # 急停需要应急角色
        estop_body = dict(PAUSE_BODY, reason="现场紧急情况，立即急停", client_request_id="req-estop")
        response = client.post("/v1/emergency-stop", json=estop_body)
        assert response.status_code == 403
        estop_body["role"] = "responder"
        estop_body["confirmation_token"] = "确认执行"
        response = client.post("/v1/emergency-stop", json=estop_body)
        assert response.status_code == 200
        assert response.json()["estop_active"] is True
        status = client.get("/v1/system/status").json()
        assert status["estop_active_display"] == "急停中"

        # 复位
        reset_body = dict(estop_body, reason="现场已排除风险，复位", client_request_id="req-reset")
        response = client.post("/v1/emergency-stop/reset", json=reset_body)
        assert response.status_code == 200
        assert response.json()["estop_active"] is False

        # 审计记录了全部操作（中文）
        events = client.get("/v1/audit-events", params={"limit": 50}).json()["events"]
        actions = {e["action_display"] for e in events}
        assert {"暂停设备", "隔离设备", "全局急停", "急停复位"} <= actions
        assert all(e["reason"] for e in events), "审计必须带原因"


@pytest.mark.parametrize("reason", ["ok", "123", "!@#$", "確認済み", "确认正常"])
def test_operation_reason_is_language_neutral_and_audited(tmp_path, reason):
    with make_client(tmp_path, n_devices=1) as client:
        response = client.post("/v1/devices/arm-01/pause", json=dict(PAUSE_BODY, reason=reason))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "paused"
        events = client.get("/v1/audit-events", params={"limit": 50}).json()["events"]
        pause_events = [event for event in events if event["action"] == "device_pause"]
        assert len(pause_events) == 1
        assert pause_events[0]["reason"] == reason


def test_task_flow_over_api(tmp_path):
    client = make_client(tmp_path, n_devices=2)
    with client:
        payload = {
            "task_type": "script_probe", "idempotency_key": "api-probe-001", "device_id": "arm-01",
            "expires_at": None, "payload": {"steps": [{"action_display": "接口冒烟步骤"}]},
            "correlation_id": "corr-api-1", "source": "console", "operator_id": "操作员甲",
        }
        response = client.post("/v1/tasks", json=payload)
        assert response.status_code == 200
        created = response.json()
        assert created["task_type_display"] == "脚本探针任务"
        assert created["status_display"] in ("已排队", "等待设备", "校验中", "执行中")

        # 幂等：同键重提交返回同一任务
        response2 = client.post("/v1/tasks", json=payload)
        assert response2.json()["task_id"] == created["task_id"]

        # 等待完成
        deadline = time.time() + 8
        status = None
        while time.time() < deadline:
            status = client.get(f"/v1/tasks/{created['task_id']}").json()
            if status["status"] in ("confirmed", "failed", "unknown", "cancelled", "expired"):
                break
            time.sleep(0.05)
        assert status["status"] == "confirmed" and status["status_display"] == "已完成"

        # 任务筛选
        listing = client.get("/v1/tasks", params={"device_id": "arm-01", "status": "confirmed"}).json()
        assert any(t["task_id"] == created["task_id"] for t in listing["tasks"])

        # 取消排队任务
        payload2 = dict(payload, idempotency_key="api-probe-002", payload={"steps": [{"action_display": "长步骤", "sleep": 0.5}]})
        client.post("/v1/devices/arm-01/pause", json=PAUSE_BODY)
        created2 = client.post("/v1/tasks", json=payload2).json()
        cancel = client.post(f"/v1/tasks/{created2['task_id']}/cancel", json=dict(PAUSE_BODY, reason="业务方撤回任务", client_request_id="req-cancel"))
        assert cancel.status_code == 200
        assert cancel.json()["status_display"] == "已取消"
        client.post("/v1/devices/arm-01/resume", json=dict(PAUSE_BODY, reason="恢复设备", client_request_id="req-resume"))


def test_view_session_full_flow(tmp_path):
    """实时画面：创建/续期/单帧/关闭/过期/上限，响应符合 camera-view.v1。"""
    schema = json.loads((CONTRACTS / "camera-view.v1.schema.json").read_text(encoding="utf-8"))
    client = make_client(tmp_path, ttl_seconds=3, n_devices=1)
    with client:
        create_body = {
            "device_id": "arm-01", "camera_id": "cam-01", "purpose": "主动查看",
            "requested_by": "操作员甲", "correlation_id": "corr-view-1", "client_request_id": "req-view-1",
        }
        response = client.post("/v1/camera/view-sessions", json=create_body)
        assert response.status_code == 200
        session = response.json()
        schema_validate(session, schema)
        assert session["status"] == "valid" and session["status_display"] == "有效"
        assert session["purpose"] == "主动查看"
        assert session["stream_protocol"] == "mjpeg"
        assert session["last_frame_at"] is None and session["closed_at"] is None and session["close_reason"] is None

        # 设备/摄像头不匹配
        bad = dict(create_body, camera_id="cam-other", client_request_id="req-view-bad")
        response = client.post("/v1/camera/view-sessions", json=bad)
        assert response.status_code == 400
        assert response.json()["error"]["key"] == "view_session_device_mismatch"

        # 并发上限 2
        client.post("/v1/camera/view-sessions", json=dict(create_body, client_request_id="req-view-2"))
        third = client.post("/v1/camera/view-sessions", json=dict(create_body, client_request_id="req-view-3"))
        assert third.status_code == 429
        assert third.json()["error"]["key"] == "view_session_limit"
        assert third.json()["error"]["title"], "超限错误必须中文标题"

        # 单帧
        frame = client.get(f"/v1/camera/view-sessions/{session['view_session_id']}/frame")
        assert frame.status_code == 200
        assert frame.content.startswith(b"\x89PNG")

        # 续期
        renewed = client.post(f"/v1/camera/view-sessions/{session['view_session_id']}/renew",
                              json={"requested_by": "操作员甲"}).json()
        schema_validate(renewed, schema)
        assert renewed["status"] == "valid"

        # 关闭
        closed = client.request("DELETE", f"/v1/camera/view-sessions/{session['view_session_id']}",
                                json={"requested_by": "操作员甲", "reason": "页面关闭，结束查看"}).json()
        assert closed["status"] == "closed" and closed["status_display"] == "已关闭"
        assert closed["close_reason"] == "页面关闭，结束查看"
        assert closed["closed_at"]

        # 查看会话不改变设备状态、不获取租约
        device = client.get("/v1/devices/arm-01").json()
        assert device["status"] == "ready" and device["lease_id"] is None

        # 过期（TTL 3 秒）
        expiring = client.post("/v1/camera/view-sessions", json=dict(create_body, client_request_id="req-view-exp")).json()
        time.sleep(3.4)
        expired = client.get(f"/v1/camera/view-sessions/{expiring['view_session_id']}").json()
        assert expired["status"] == "expired" and expired["status_display"] == "已过期"
        # 过期后取帧 -> 查看会话过期错误
        frame = client.get(f"/v1/camera/view-sessions/{expiring['view_session_id']}/frame")
        assert frame.status_code == 410

        # 审计记录查看会话生命周期
        events = client.get("/v1/audit-events", params={"limit": 50}).json()["events"]
        displays = {e["action_display"] for e in events}
        assert {"创建实时画面查看会话", "续期查看会话", "关闭查看会话", "查看会话过期"} <= displays


def test_view_session_unavailable_on_camera_fault(tmp_path):
    """摄像头遮挡时：查看会话状态为画面不可用。"""
    client = make_client(tmp_path, n_devices=1)
    with client:
        # r7 起接管查看需可信代理身份（identity_unverified）；本用例目标
        # 是相机故障→画面不可达路径，改走主动查看（直连合法）。
        session = client.post("/v1/camera/view-sessions", json={
            "device_id": "arm-01", "purpose": "主动查看", "requested_by": "操作员乙",
            "correlation_id": "corr-view-2", "client_request_id": "req-view-ua",
        }).json()
        assert session["status"] == "valid"
        runtime_client = client
        # 注入摄像头故障
        rt = runtime_client.app.state.runtime
        rt.devices.camera("arm-01").set_quality("blocked")
        frame = runtime_client.get(f"/v1/camera/view-sessions/{session['view_session_id']}/frame")
        assert frame.status_code == 409
        after = runtime_client.get(f"/v1/camera/view-sessions/{session['view_session_id']}").json()
        assert after["status"] == "unavailable" and after["status_display"] == "画面不可用"


def test_sse_stream_connected_event(tmp_path):
    """SSE（真实 HTTP 服务器）：连接后立即收到连接确认事件（中文），断开可正常清理。

    httpx 的 ASGITransport 会缓冲完整响应、无法测无限流，因此本测试启动真实
    uvicorn 服务器（临时端口）做端到端验证。
    """
    import threading
    import time

    import httpx
    import uvicorn

    app, runtime = build_app(tmp_path, n_devices=1)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 服务器未能启动"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        with httpx.Client(timeout=10) as client:
            with client.stream("GET", f"http://127.0.0.1:{port}/v1/events/stream") as response:
                lines: list[str] = []
                for line in response.iter_lines():
                    lines.append(line)
                    if line.startswith("data:"):
                        break
                response.close()
        assert any("connected" in line for line in lines), f"必须先发送连接确认事件，实际：{lines}"
        assert any("已连接" in line for line in lines), "连接确认必须使用中文"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_event_bus_pushes_device_updates(tmp_path):
    """事件总线：设备状态变化必须发布 device.updated（含内部键与中文显示）。"""

    async def scenario():
        client = make_client(tmp_path, n_devices=1)
        with client:
            runtime = client.app.state.runtime
            queue = runtime.events.subscribe()

            def trigger():
                runtime.op_pause("arm-01", {"operator_id": "操作员甲", "role": "operator",
                                            "reason": "现场检查，暂停设备", "correlation_id": "c",
                                            "client_request_id": "r"})

            thread = threading.Thread(target=trigger)
            thread.start()
            received: list[dict] = []

            async def collect():
                deadline = asyncio.get_event_loop().time() + 5
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if event["event_type"] == "device.updated":
                        received.append(event)
                        return

            await collect()
            thread.join(timeout=5)
            runtime.events.unsubscribe(queue)
            assert received, "暂停设备必须发布 device.updated 事件"
            data = received[0]["data"]
            assert data["status"] == "paused" and data["status_display"] == "暂停"

    asyncio.run(scenario())


def test_capabilities_and_takeovers_api(tmp_path):
    client = make_client(tmp_path, n_devices=1)
    with client:
        caps = client.get("/v1/capabilities").json()["capabilities"]
        ids = {c["capability_id"] for c in caps}
        assert {"arm.home", "camera.capture", "camera.live_view"} <= ids
        for capability in caps:
            assert capability["name_display"] and capability["risk_level"] in ("低", "中", "高")

        # 制造结果未知 -> 人工处理接口
        rt = client.app.state.runtime
        task = rt.submit_task(
            {"task_type": "script_probe", "idempotency_key": "tk-001", "device_id": "arm-01",
             "expires_at": None,
             "payload": {"steps": [{"action_display": "断连步骤", "outcome": "unknown", "message": "点击后断连"}]},
             "correlation_id": "c"},
            source="console", context={"operator_id": "操作员甲"},
        )
        deadline = time.time() + 8
        while time.time() < deadline:
            current = rt.get_task(task["task_id"])
            if current["status"] == "unknown":
                break
            time.sleep(0.05)
        takeovers = client.get("/v1/takeovers").json()["items"]
        assert any(i["kind"] == "task_unknown" for i in takeovers)

        reviewed = client.post(f"/v1/tasks/{task['task_id']}/mark-reviewed", json=dict(
            PAUSE_BODY, reason="已人工核对界面无异常", client_request_id="req-review"))
        assert reviewed.status_code == 200
        assert reviewed.json()["reviewed_by"] == "操作员甲"
