"""屏幕输入法适配（screen_ime）验收测试。

覆盖：
- K40TaskServer 协议兼容：心跳门禁、幂等重放、状态机迁移、长轮询、取消控制、
  持久化（服务重启任务不丢）
- HTTP 路由：/api/v1/* 与平台 /v1/* 共存；错误格式与 K40TaskServer 一致
- 屏幕校准：会话门禁、布防/触点配对、仿射拟合、残差、映射注册表
- 插件端到端：屏幕文字输入任务（仿真输入法全流程）、屏幕校准采样任务
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adapters.screen_ime.geometry import (
    CalibrationMathError,
    MappingRegistry,
    ScreenRobotTransform,
    fit_affine,
)
from adapters.screen_ime.service import (
    ScreenImeApiError,
    ScreenImeService,
    get_default_service,
    set_default_service,
)
from api.app import create_app
from runtime.config import PlatformConfig
from runtime.errors import ApiError
from runtime.runtime import ArmFleetRuntime
from runtime.util import new_id
from tests.helpers import enable_input_switch, make_device_configs, op_context

PLUGIN_SOURCE = Path(__file__).parents[1] / "examples" / "plugins" / "screen-ime-adapter"


async def _wait_terminal(runtime: ArmFleetRuntime, task_id: str, timeout: float = 20.0) -> dict:
    terminal = {"confirmed", "failed", "unknown", "cancelled", "expired"}
    waited = 0.0
    while waited < timeout:
        task = runtime.get_task(task_id)
        if task["status"] in terminal:
            return task
        await asyncio.sleep(0.05)
        waited += 0.05
    return runtime.get_task(task_id)


# ---------------------------------------------------------------------------
# 服务核心（协议语义）
# ---------------------------------------------------------------------------

def _service(tmp_path: Path) -> ScreenImeService:
    return ScreenImeService(tmp_path / "ime.db", tmp_path / "ime_mappings.json")


def test_create_requires_recent_heartbeat_and_active_keyboard(tmp_path):
    """创建任务前设备必须有 45 秒内心跳且 keyboardActive=true。"""

    async def scenario():
        service = _service(tmp_path)
        try:
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.create({"taskId": "t-1", "deviceId": "k40-01",
                                            "targetUser": "张三", "content": "你好"})
            assert exc.value.code == "device_unavailable"
            await service.tasks.heartbeat("k40-01", {"keyboardActive": False})
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.create({"taskId": "t-1", "deviceId": "k40-01",
                                            "targetUser": "张三", "content": "你好"})
            assert exc.value.code == "device_unavailable"
            await service.tasks.heartbeat("k40-01",
                                          {"appVersion": "test", "keyboardActive": True})
            task, created = await service.tasks.create(
                {"taskId": "t-1", "deviceId": "k40-01", "targetUser": "张三",
                 "content": "你好", "typingSpeed": "normal"})
            assert created and task["status"] == "pending"
            assert task["resolvedTypingSpeed"] == "normal"
        finally:
            service.close()

    asyncio.run(scenario())


def test_create_idempotent_replay_and_conflict(tmp_path):
    """同 taskId 同内容重放返回原任务；不同内容 409 冲突；字段与长度校验。"""

    async def scenario():
        service = _service(tmp_path)
        try:
            await service.tasks.heartbeat("k40-01", {"keyboardActive": True})
            request = {"taskId": "t-1", "deviceId": "k40-01", "targetUser": "张三",
                       "content": "你好"}
            first, created1 = await service.tasks.create(request)
            replay, created2 = await service.tasks.create(dict(request))
            assert created1 and not created2
            assert first["taskId"] == replay["taskId"]
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.create({**request, "content": "再见"})
            assert exc.value.code == "task_conflict"
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.create({**request, "taskId": "t-2",
                                            "typingSpeed": "faster"})
            assert exc.value.code == "invalid_typing_speed"
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.create({**request, "taskId": "t-3",
                                            "content": "x" * 10001})
            assert exc.value.code == "content_too_long"
        finally:
            service.close()

    asyncio.run(scenario())


def test_status_machine_transitions_and_displayed_uniqueness(tmp_path):
    """状态机：非法迁移拒绝；同一设备同时只允许一个 displayed 任务。"""

    async def scenario():
        service = _service(tmp_path)
        try:
            await service.tasks.heartbeat("k40-01", {"keyboardActive": True})
            for task_id in ("t-1", "t-2"):
                await service.tasks.create({"taskId": task_id, "deviceId": "k40-01",
                                            "targetUser": "张三", "content": task_id})
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.update_status(
                    "t-1", {"deviceId": "k40-01", "status": "inserted"})
            assert exc.value.code == "invalid_transition"
            await service.tasks.update_status("t-1",
                                              {"deviceId": "k40-01", "status": "displayed"})
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.update_status("t-2",
                                                  {"deviceId": "k40-01", "status": "displayed"})
            assert exc.value.code == "device_has_displayed_task"
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.update_status(
                    "t-1", {"deviceId": "other", "status": "typing"})
            assert exc.value.code == "device_mismatch"
        finally:
            service.close()

    asyncio.run(scenario())


def test_long_poll_dispatch_and_cancel_control(tmp_path):
    """长轮询：设备领取任务、Agent 等待状态；取消经控制命令确认后进入 cancelled。"""

    async def scenario():
        service = _service(tmp_path)
        try:
            await service.tasks.heartbeat("k40-01", {"keyboardActive": True})
            await service.tasks.create({"taskId": "t-1", "deviceId": "k40-01",
                                        "targetUser": "张三", "content": "你好"})

            async def phone():
                task = await service.tasks.next_task("k40-01", 5)
                assert task is not None and task["status"] == "pending"
                await service.tasks.update_status(
                    task["taskId"], {"deviceId": "k40-01", "status": "displayed"})
                await asyncio.sleep(0.05)
                await service.tasks.update_status(
                    task["taskId"],
                    {"deviceId": "k40-01", "status": "typing", "typedCharacterCount": 1})
                await service.tasks.update_status(
                    task["taskId"],
                    {"deviceId": "k40-01", "status": "inserted", "typedCharacterCount": 2})

            phone_task = asyncio.create_task(phone())
            final = await service.tasks.wait_for_status("t-1", {"inserted"}, 5)
            await phone_task
            assert final["status"] == "inserted" and final["typedCharacterCount"] == 2
            assert await service.tasks.next_task("k40-01", 0) is None

            # 取消链路：displayed → cancel_requested → ack → cancelled
            await service.tasks.heartbeat("k40-01", {"keyboardActive": True})
            await service.tasks.create({"taskId": "t-2", "deviceId": "k40-01",
                                        "targetUser": "李四", "content": "测试"})
            await service.tasks.update_status("t-2",
                                              {"deviceId": "k40-01", "status": "displayed"})

            async def phone_cancel():
                command = await service.tasks.next_control("t-2", "k40-01", 5)
                assert command is not None and command["command"] == "cancel"
                await service.tasks.acknowledge_control(
                    "t-2", {"deviceId": "k40-01",
                            "commandId": command["commandId"], "typedCharacterCount": 0})

            cancel_task = asyncio.create_task(phone_cancel())
            cancelled = await service.tasks.cancel("t-2")
            assert cancelled["status"] == "cancel_requested"
            await cancel_task
            assert service.tasks.find("t-2")["status"] == "cancelled"
            # 幂等取消与终态取消
            assert (await service.tasks.cancel("t-2"))["status"] == "cancelled"
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.cancel("t-1")
            assert exc.value.code == "task_terminal"
        finally:
            service.close()

    asyncio.run(scenario())


def test_typing_failed_recorded_and_history_kept(tmp_path):
    """typing_failed 回报失败原因与已输入字符数；历史完整保留。"""

    async def scenario():
        service = _service(tmp_path)
        try:
            await service.tasks.heartbeat("k40-01", {"keyboardActive": True})
            await service.tasks.create({"taskId": "t-1", "deviceId": "k40-01",
                                        "targetUser": "张三", "content": "你好世界"})
            await service.tasks.update_status("t-1",
                                              {"deviceId": "k40-01", "status": "displayed"})
            await service.tasks.update_status("t-1",
                                              {"deviceId": "k40-01", "status": "typing",
                                               "typedCharacterCount": 2})
            await service.tasks.update_status(
                "t-1", {"deviceId": "k40-01", "status": "typing_failed",
                        "typedCharacterCount": 2, "failureReason": "焦点丢失"})
            detail = service.tasks.detail("t-1")
            assert detail["task"]["status"] == "typing_failed"
            assert [h["status"] for h in detail["history"]] == [
                "pending", "displayed", "typing", "typing_failed"]
            with pytest.raises(ScreenImeApiError) as exc:
                await service.tasks.update_status(
                    "t-1", {"deviceId": "k40-01", "status": "inserted"})
            assert exc.value.code == "invalid_transition"
        finally:
            service.close()

    asyncio.run(scenario())


def test_persistence_survives_restart(tmp_path):
    """任务与历史落库：服务实例重建后数据仍在（原 Java 版同语义）。"""
    service = _service(tmp_path)
    asyncio.run(service.tasks.heartbeat("k40-01", {"keyboardActive": True}))
    asyncio.run(service.tasks.create({"taskId": "t-1", "deviceId": "k40-01",
                                      "targetUser": "张三", "content": "重启保留"}))
    service.close()

    service2 = _service(tmp_path)
    try:
        task = service2.tasks.find("t-1")
        assert task is not None and task["content"] == "重启保留"
        assert len(service2.tasks.detail("t-1")["history"]) == 1
    finally:
        service2.close()


# ---------------------------------------------------------------------------
# HTTP 路由（/api/v1/* 设备侧协议 + 平台 /v1/* 共存）
# ---------------------------------------------------------------------------

def _app(tmp_path: Path):
    config = PlatformConfig(
        {"db_path": str(tmp_path / "fleet.db"), "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation",
         "screen_ime_db_path": str(tmp_path / "ime.db"),
         "screen_ime_mappings_path": str(tmp_path / "ime_mappings.json")},
        root=tmp_path,
    )
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime_factory = lambda: runtime
    app.state.device_configs = make_device_configs(1)
    # 测试更正（§4.5，2026-10-03）：R3-17-P4 手机类别路由要求携证——
    # _app 为用到的测试手机登记独立凭据，测试经 H_* 头携证。
    for dev, token in _TEST_TOKENS.items():
        runtime.register_phone_credential(dev, token)
    return app, runtime


_TEST_TOKENS = {
    "k40-01": "k40-01-test-token-000000000001",
    "k40-poll": "k40-poll-test-token-0000000001",
    "k40-poll2": "k40-poll2-test-token-00000000001",
}
_H01 = {"x-ime-phone-token": _TEST_TOKENS["k40-01"]}
_HP1 = {"x-ime-phone-token": _TEST_TOKENS["k40-poll"]}
_HP2 = {"x-ime-phone-token": _TEST_TOKENS["k40-poll2"]}


def test_http_protocol_compatible_with_k40_server(tmp_path):
    """路由与错误格式：health、心跳、创建、长轮询 204、错误 {"error","message"}。"""
    app, _runtime = _app(tmp_path)
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200 and health.json()["status"] == "ok"
        # Content-Type 必须是 application/json（与 Java 版一致）
        bad_type = client.post("/api/v1/tasks", data="x", headers={"Content-Type": "text/plain"})
        assert bad_type.status_code == 415
        assert set(bad_type.json()) == {"error", "message"}
        offline = client.post("/api/v1/tasks", json={
            "taskId": "t-1", "deviceId": "k40-01", "targetUser": "张三",
            "content": "你好"})
        assert offline.status_code == 409
        assert offline.json() == {"error": "device_unavailable",
                                  "message": "Device is offline or its keyboard is closed"}
        # 无任务长轮询立即 204（携证；P4 下只能轮询自己的设备）
        empty_first = client.get(
            "/api/v1/devices/k40-01/tasks/next?waitSeconds=0", headers=_H01)
        assert empty_first.status_code == 204
        client.post("/api/v1/devices/k40-01/heartbeat", headers=_H01,
                    json={"appVersion": "sim", "keyboardActive": True,
                          "inputTaskControlVersion": 1})
        # Fleet 直发遵守正式输入许可；仿真夹具补齐绑定/校准与输入开关。
        _runtime.devices._devices["arm-01"].update({
            "ime_device_id": "k40-01", "status": "ready", "calibration_status": "validated"})
        enabled = client.put("/v1/devices/arm-01/input-settings", json={
            "contract_version": "device-input-settings.v1", "operator_id": "测试",
            "role": "operator", "reason": "协议兼容测试", "correlation_id": "ime-http",
            "client_request_id": "ime-http-enable", "enabled": True,
            "expected_revision": 0, "ime_device_id": "k40-01"})
        assert enabled.status_code == 200, enabled.text
        created = client.post("/api/v1/tasks", json={
            "taskId": "t-1", "deviceId": "k40-01", "targetUser": "张三", "content": "你好"})
        assert created.status_code == 201 and created.json()["status"] == "pending"
        # 字段缺失与非法状态
        missing = client.post("/api/v1/tasks", json={"taskId": "t-9"})
        assert missing.status_code == 400
        bad_status = client.post("/api/v1/tasks/t-1/status", headers=_H01,
                                 json={"deviceId": "k40-01", "status": "shown"})
        assert bad_status.status_code == 400 and bad_status.json()["error"] == "invalid_status"
        # 设备列表与任务查询
        assert client.get("/api/v1/devices").json()["items"][0]["online"] is True
        listed = client.get("/api/v1/tasks?deviceId=k40-01&status=pending").json()
        assert listed["tasks"][0]["taskId"] == "t-1"
        # 平台 API 不受影响（中文映射共存）
        assert client.get("/v1/system/status").status_code == 200
        # 零安装手机演示页可访问
        page = client.get("/screen-ime/phone")
        assert page.status_code == 200
        assert "任务输入法" in page.text
        assert "text/html" in page.headers["content-type"]


def test_http_calibration_endpoints(tmp_path):
    """校准协议：创建/布防门禁/触点配对/结束；映射注册表查询。"""
    app, _runtime = _app(tmp_path)
    with TestClient(app) as client:
        session = client.post("/api/v1/calibrations", json={"deviceId": "k40-01"}).json()
        session_id = session["sessionId"]
        dup = client.post("/api/v1/calibrations", json={"deviceId": "k40-01"})
        assert dup.status_code == 409 and dup.json()["error"] == "calibration_active"
        # 页面未打开时布防被拒
        gated = client.post(f"/api/v1/calibrations/{session_id}/arm",
                            json={"sampleId": "s1", "sequence": 1})
        assert gated.status_code == 409 and gated.json()["error"] == "phone_page_closed"
        client.post(f"/api/v1/calibrations/{session_id}/client", headers=_H01,
                    json={"deviceId": "k40-01", "state": "open"})
        armed = client.post(f"/api/v1/calibrations/{session_id}/arm",
                            json={"sampleId": "s1", "sequence": 1})
        assert armed.status_code == 200 and armed.json()["armed"]["sampleId"] == "s1"
        # 无匹配布防的触点被拒绝
        mismatch = client.post(f"/api/v1/calibrations/{session_id}/touch", headers=_H01,
                               json={"deviceId": "k40-01", "sampleId": "s9",
                                     "sequence": 9, "screenX": 1, "screenY": 1,
                                     "screenWidth": 100, "screenHeight": 100,
                                     "orientation": "portrait"})
        assert mismatch.status_code == 409 and mismatch.json()["error"] == "sample_not_armed"
        # 越界触点被拒绝
        oob = client.post(f"/api/v1/calibrations/{session_id}/touch", headers=_H01,
                          json={"deviceId": "k40-01", "sampleId": "s1", "sequence": 1,
                                "screenX": -5, "screenY": 10, "screenWidth": 100,
                                "screenHeight": 100, "orientation": "portrait"})
        assert oob.status_code == 400 and oob.json()["error"] == "screen_point_out_of_bounds"
        touch = client.post(f"/api/v1/calibrations/{session_id}/touch", headers=_H01,
                            json={"deviceId": "k40-01", "sampleId": "s1", "sequence": 1,
                                  "screenX": 50, "screenY": 60, "screenWidth": 100,
                                  "screenHeight": 100, "orientation": "portrait"})
        assert touch.status_code == 200 and len(touch.json()["samples"]) == 1
        assert touch.json()["armed"] is None
        assert client.post(f"/api/v1/calibrations/{session_id}/complete").json()["status"] == "completed"
        # 手机轮询当前会话：无会话 204
        assert client.get("/api/v1/devices/k40-01/calibration", headers=_H01).status_code == 204
        # 映射注册表：未校准 404
        missing = client.get("/api/v1/screen-mappings/k40-01")
        assert missing.status_code == 404


# ---------------------------------------------------------------------------
# 几何：仿射拟合与映射注册表
# ---------------------------------------------------------------------------

def test_calibration_client_reports_readonly_screen_metadata(tmp_path):
    """F2 前置（规范 §6.2.2）：open 回报只读屏幕元数据；持久在会话、_view 透出；非法 400。"""
    app, _runtime = _app(tmp_path)
    with TestClient(app) as client:
        session = client.post("/api/v1/calibrations", json={"deviceId": "k40-01"}).json()
        session_id = session["sessionId"]
        base = f"/api/v1/calibrations/{session_id}/client"
        # 非法值：宽高非正整数 400
        bad = client.post(base, headers=_H01, json={"deviceId": "k40-01", "state": "open",
                                      "screenWidth": 0, "screenHeight": 2400,
                                      "orientation": "portrait"})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_screen_metadata"
        # 半套元数据（只有宽没有高）400，不留下可被误读成"已回报"的资料
        half = client.post(base, headers=_H01, json={"deviceId": "k40-01", "state": "open",
                                       "screenWidth": 1080})
        assert half.status_code == 400 and half.json()["error"] == "invalid_screen_metadata"
        # 非法方向 400
        bad_orientation = client.post(base, headers=_H01, json={"deviceId": "k40-01", "state": "open",
                                                  "screenWidth": 1080, "screenHeight": 2400,
                                                  "orientation": "  "})
        assert bad_orientation.status_code == 400
        # 合法回报：open 时持久保存并透出
        ok = client.post(base, headers=_H01, json={"deviceId": "k40-01", "state": "open",
                                     "screenWidth": 1080, "screenHeight": 2400,
                                     "orientation": "portrait"})
        assert ok.status_code == 200
        assert ok.json()["screen"] == {"screenWidth": 1080, "screenHeight": 2400,
                                       "orientation": "portrait"}
        view = client.get(f"/api/v1/calibrations/{session_id}").json()
        assert view["clientState"] == "open"
        assert view["screen"] == {"screenWidth": 1080, "screenHeight": 2400,
                                  "orientation": "portrait"}
        # 后续不带元数据的 open 心跳不清空已持久元数据
        again = client.post(base, headers=_H01, json={"deviceId": "k40-01", "state": "open"})
        assert again.status_code == 200
        assert again.json()["screen"]["screenWidth"] == 1080
        # closed 后元数据保留供审计，但布防仍被拒
        closed = client.post(base, headers=_H01, json={"deviceId": "k40-01", "state": "closed"})
        assert closed.status_code == 200 and closed.json()["screen"]["screenHeight"] == 2400


def test_affine_fit_roundtrip_and_collinear_rejection():
    fit = fit_affine([
        {"robotX": 150.0, "robotY": 60.0, "screenX": 540.0, "screenY": 1200.0},
        {"robotX": 165.0, "robotY": 60.0, "screenX": 570.0, "screenY": 1200.0},
        {"robotX": 150.0, "robotY": 75.0, "screenX": 540.0, "screenY": 1230.0},
    ])
    assert fit["residualRms"] < 1e-6
    transform = ScreenRobotTransform.from_fit(
        fit, device_id="k40-01", screen_width=1080, screen_height=2400,
        orientation="portrait", version=1, created_at="t")
    sx, sy = transform.screen_from_robot(157.5, 67.5)
    assert sx == pytest.approx(555.0, abs=1e-6)
    assert sy == pytest.approx(1215.0, abs=1e-6)
    rx, ry = transform.robot_from_screen(sx, sy)
    assert abs(rx - 157.5) < 1e-9 and abs(ry - 67.5) < 1e-9
    with pytest.raises(CalibrationMathError) as exc:
        fit_affine([
            {"robotX": 150.0, "robotY": 60.0, "screenX": 540.0, "screenY": 1200.0},
            {"robotX": 160.0, "robotY": 65.0, "screenX": 560.0, "screenY": 1210.0},
            {"robotX": 170.0, "robotY": 70.0, "screenX": 580.0, "screenY": 1220.0},
        ])
    assert exc.value.code == "calibration_collinear"
    with pytest.raises(CalibrationMathError):
        fit_affine([{"robotX": 1, "robotY": 1, "screenX": 2, "screenY": 2}])


def test_mapping_registry_versions_and_persistence(tmp_path):
    registry = MappingRegistry(tmp_path / "m.json")
    fit = fit_affine([
        {"robotX": 0.0, "robotY": 0.0, "screenX": 100.0, "screenY": 200.0},
        {"robotX": 10.0, "robotY": 0.0, "screenX": 120.0, "screenY": 200.0},
        {"robotX": 0.0, "robotY": 10.0, "screenX": 100.0, "screenY": 230.0},
    ])
    transform = ScreenRobotTransform.from_fit(
        fit, device_id="k40-01", screen_width=1080, screen_height=2400,
        orientation="portrait", version=1, created_at="t")
    assert registry.save(transform)["version"] == 1
    assert registry.save(transform)["version"] == 2
    reloaded = MappingRegistry(tmp_path / "m.json")
    mapping = reloaded.get("k40-01")
    assert mapping["version"] == 2
    assert mapping["formula"] == "screenX=a*robotX+b*robotY+c; screenY=d*robotX+e*robotY+f"
    assert reloaded.delete("k40-01") is True


# ---------------------------------------------------------------------------
# 插件端到端（仿真输入法：心跳 + 长轮询 + 逐字输入回报）
# ---------------------------------------------------------------------------

def _runtime_with_plugin(tmp_path: Path) -> ArmFleetRuntime:
    """构建运行时并安装屏幕输入适配插件、注入独立服务实例。"""
    config = PlatformConfig(
        {"db_path": str(tmp_path / "fleet.db"), "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation"},
        root=tmp_path,
    )
    runtime = ArmFleetRuntime(config)
    source = tmp_path / "screen-ime-src"
    source.mkdir(exist_ok=True)
    (source / "manifest.json").write_text(
        (PLUGIN_SOURCE / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    (source / "plugin.py").write_text(
        (PLUGIN_SOURCE / "plugin.py").read_text(encoding="utf-8"), encoding="utf-8")
    runtime.plugins.install(source, operator_id="操作员甲", reason="安装屏幕输入适配插件")
    runtime.plugins.activate("screen-ime-adapter")
    set_default_service(_service(tmp_path))
    return runtime


def _screen_input_payload(ime_device_id: str = "k40-01", **extra) -> dict:
    return {
        "task_type": "screen_input",
        "idempotency_key": f"ime-{new_id('k')}",
        "device_id": "arm-01",
        "payload": {"text": "你好，机械臂", "ime_device_id": ime_device_id,
                    "wait_seconds": 3, **extra},
        "correlation_id": "corr-ime-1",
    }



def _hermetic_ime_service(monkeypatch, tmp_path):
    """封闭服务：进程级默认服务会写仓库 sqlite（非封闭）；测试统一指向 tmp。"""
    from adapters.screen_ime import service as ime_service

    svc = ime_service.ScreenImeService(tmp_path / "ime.db",
                                       tmp_path / "mappings.json")
    monkeypatch.setattr(ime_service, "_default_service", svc)
    return svc


def test_plugin_screen_input_end_to_end(tmp_path, monkeypatch):
    """全链路：提交屏幕输入任务 → 仿真输入法领取/显示/逐字输入 → 平台任务已完成。

    FIX-15：候选栏点击规划前必须取得实时几何快照——仿真手机同时充当几何
    客户端（握手 → 长轮询领取 nonce → 按 nonce 上报候选/删除实时框），
    走与真机相同的 runtime.ime_geometry 协议，不伪造 geometry_valid。
    """

    async def scenario():
        runtime = _runtime_with_plugin(tmp_path)
        await runtime.startup(device_configs=make_device_configs(1))
        enable_input_switch(runtime, tmp_path)
        service = _hermetic_ime_service(monkeypatch, tmp_path)
        # FIX-15：唯一绑定登记（几何身份反查以此为源）
        runtime.devices._devices["arm-01"]["ime_device_id"] = "k40-01"
        # R2-21：几何入口要求每机独立凭据（测试登记，经真实验证器）
        if hasattr(runtime, "register_phone_credential"):
            runtime.register_phone_credential(
                "k40-01", "k40-01-geometry-test-token-0001")
        try:
            # 输入法先上线（插件第一步校验心跳与键盘状态；FIX-15：模拟手机
            # 按 F15-1 语义携带控制版本 1，缺版本=能力不兼容）
            await service.tasks.heartbeat(
                "k40-01", {"appVersion": "sim-ime", "keyboardActive": True,
                           "inputTaskControlVersion": 1})
            # 几何会话：与真机同一契约（握手 → nonce 快照）
            import uuid as _uuid
            from runtime.ime_geometry import CONTRACT_VERSION
            session = runtime.ime_geometry.handshake("k40-01", {
                "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
                "client_instance_id": str(_uuid.uuid4())},
                credentials="k40-01-geometry-test-token-0001")

            def _snapshot_body(nonce: str, seq: int) -> dict:
                display = {"display_id": 0, "width_px": 1080, "height_px": 2400,
                           "rotation": 0, "density": 2.75}
                return {
                    "contract_version": CONTRACT_VERSION,
                    "runtime_session_id": session["runtime_session_id"],
                    "view_instance_id": str(_uuid.uuid4()),
                    "seq": seq,
                    "captured_uptime_ms": 123456,
                    "display": display,
                    "layout_version": "k40-candidate-1",
                    "keyboard_active": True,
                    "geometry_valid": True,
                    "keyboard_bounds": {"left": 0, "top": 1560, "right": 1080,
                                        "bottom": 2400},
                    # 测试更正（§4.5，2026-10-03）：R3-02-1 候选规划必须绑定
                    # 非空原任务引用（同快照在 TAP 门一致性核对）。
                    "current_task_id": "t-sim-ime",
                    "reason": "snapshot",
                    "request_nonce": nonce,
                    "targets": [
                        {"id": "ime_candidate_bar", "visible": True,
                         "enabled": True, "clickable": True,
                         "bounds": {"left": 54, "top": 2160, "right": 1026,
                                    "bottom": 2280}},
                        {"id": "ime_delete_key", "visible": True,
                         "enabled": True, "clickable": True,
                         "bounds": {"left": 960, "top": 1960, "right": 1050,
                                    "bottom": 2050}},
                    ],
                }

            async def geometry_client():
                # 长轮询领取 nonce 并按 nonce 上报快照（≤2s/≤1s 协议）
                seq = 0
                while True:
                    claimed = await asyncio.to_thread(
                        runtime.ime_geometry.poll_next,
                        "k40-01", session["runtime_session_id"], 1,
                        "k40-01-geometry-test-token-0001")
                    if claimed is None:
                        await asyncio.sleep(0.01)
                        continue
                    seq += 1
                    await asyncio.to_thread(
                        runtime.ime_geometry.report, "k40-01",
                        _snapshot_body(claimed["request_nonce"], seq),
                        "k40-01-geometry-test-token-0001")

            async def phone():
                task = await service.tasks.next_task("k40-01", 15)
                assert task is not None, "输入法未收到任务"
                # 协议不改写：输入法把 content 原样放入候选栏
                assert task["content"] == "你好，机械臂"
                await service.tasks.update_status(
                    task["taskId"], {"deviceId": "k40-01", "status": "displayed"})
                # 候选栏被机械臂点击后开始按人工节奏逐字输入
                await asyncio.sleep(0.1)
                total = len(task["content"])
                await service.tasks.update_status(
                    task["taskId"],
                    {"deviceId": "k40-01", "status": "typing", "typedCharacterCount": 0})
                for count in range(1, total + 1):
                    await asyncio.sleep(0.01)
                    status = "inserted" if count == total else "typing"
                    await service.tasks.update_status(
                        task["taskId"],
                        {"deviceId": "k40-01", "status": status,
                         "typedCharacterCount": count})

            geo_task = asyncio.create_task(geometry_client())
            phone_task = asyncio.create_task(phone())
            submitted = runtime.submit_task(_screen_input_payload(), source="console",
                                            context=op_context())
            task = await _wait_terminal(runtime, submitted["task_id"])
            await phone_task
            geo_task.cancel()
            assert task["status"] == "confirmed", task.get("result_message")
            assert task["status_display"] == "已输入"
            last_step = task["step_log"][-1]
            assert last_step["action"] == "wait_typed"
            assert "已按原文逐字输入完成" in last_step["message"]
            # 机械臂确实点击过候选栏（fake 驱动动作记录）
            driver = runtime.devices.driver("arm-01")
            clicks = [a for a in driver.action_log
                      if a["action"] == "click" and a["target"] == "ime_candidate_bar"]
            assert clicks
            # FIX-15：点击坐标来自实时几何（live_ime_geometry target_ref），非旧固定坐标
            assert any(
                ((a.get("params") or {}).get("target_ref") or {}).get("source")
                == "live_ime_geometry" for a in clicks)
            # 内嵌输入法任务终态为 inserted
            assert service.tasks.find(f"{task['task_id']}-ime")["status"] == "inserted"
        finally:
            await runtime.shutdown()
            set_default_service(None)

    asyncio.run(scenario())


def test_plugin_screen_input_rejects_offline_ime_device(tmp_path):
    """输入法设备离线时明确失败（device_offline），不下发任何文本。"""

    async def scenario():
        runtime = _runtime_with_plugin(tmp_path)
        await runtime.startup(device_configs=make_device_configs(1))
        enable_input_switch(runtime, tmp_path)
        try:
            submitted = runtime.submit_task(_screen_input_payload(), source="console",
                                            context=op_context())
            task = await _wait_terminal(runtime, submitted["task_id"])
            assert task["status"] == "failed"
            assert task["error_category"] == "device_offline"
        finally:
            await runtime.shutdown()
            set_default_service(None)

    asyncio.run(scenario())


def test_plugin_screen_calibration_end_to_end(tmp_path, monkeypatch):
    """校准全链路：手机校准页回报像素（由真实仿射变换生成）→ 插件拟合还原变换并保存。"""

    async def scenario():
        runtime = _runtime_with_plugin(tmp_path)
        await runtime.startup(device_configs=make_device_configs(1))
        enable_input_switch(runtime, tmp_path)
        service = _hermetic_ime_service(monkeypatch, tmp_path)
        try:
            # 手机屏幕背后的"真实"变换：screen = 2*robot + (540, 1140)
            def true_screen(rx: float, ry: float) -> tuple[float, float]:
                return (2.0 * rx + 540.0, 2.0 * ry + 1140.0)

            def robot_point(sequence: int) -> tuple[float, float]:
                return (150.0 + (15.0 if sequence == 2 else 0.0),
                        60.0 + (15.0 if sequence == 3 else 0.0))

            async def phone():
                await service.tasks.heartbeat(
                    "k40-01", {"appVersion": "sim-ime", "keyboardActive": True})
                deadline = asyncio.get_running_loop().time() + 20
                while asyncio.get_running_loop().time() < deadline:
                    session = await service.calibrations.current("k40-01")
                    if session is not None:
                        if session["clientState"] != "open":
                            await service.calibrations.client(
                                session["sessionId"], "k40-01", "open")
                        armed = session["armed"]
                        if armed is not None:
                            # 校准页不知道机械坐标，只回报 ACTION_UP 的屏幕像素
                            rx, ry = robot_point(armed["sequence"])
                            sx, sy = true_screen(rx, ry)
                            await service.calibrations.touch(
                                session["sessionId"],
                                {"deviceId": "k40-01", "sampleId": armed["sampleId"],
                                 "sequence": armed["sequence"], "screenX": sx,
                                 "screenY": sy, "screenWidth": 1080,
                                 "screenHeight": 2400, "orientation": "portrait",
                                 "eventTime": 1})
                    await asyncio.sleep(0.02)

            phone_task = asyncio.create_task(phone())
            submitted = runtime.submit_task(
                {"task_type": "screen_calibration",
                 "idempotency_key": f"cal-{new_id('k')}",
                 "device_id": "arm-01",
                 "payload": {"ime_device_id": "k40-01", "wait_seconds": 10},
                 "correlation_id": "corr-cal-1"},
                source="console", context=op_context())
            task = await _wait_terminal(runtime, submitted["task_id"], timeout=15)
            await phone_task
            assert task["status"] == "confirmed", task.get("result_message")
            assert task["status_display"] == "已完成"
            mapping = service.mappings.get("k40-01")
            assert mapping is not None and mapping["residualRms"] < 1e-6
            # 还原的变换必须能反推屏幕目标对应的机械坐标（机械臂到屏幕操作使用该方向）
            transform = ScreenRobotTransform.from_dict(mapping)
            rx, ry = transform.robot_from_screen(600.0, 1200.0)
            assert abs(rx - 30.0) < 1e-6 and abs(ry - 30.0) < 1e-6
            # 校准会话已结束
            assert await service.calibrations.current("k40-01") is None
        finally:
            await runtime.shutdown()
            set_default_service(None)

    asyncio.run(scenario())


def test_plugin_submit_rejects_invalid_payload(tmp_path):
    """参数校验：缺 ime_device_id、非法速度档位在提交时被 schema 拒绝。"""
    runtime = _runtime_with_plugin(tmp_path)
    try:
        with pytest.raises(ApiError):
            runtime.submit_task(
                {"task_type": "screen_input", "idempotency_key": "k1",
                 "device_id": "arm-01", "payload": {"ime_device_id": "k40-01"}},
                source="console", context=op_context())
        with pytest.raises(ApiError):
            runtime.submit_task(
                {"task_type": "screen_input", "idempotency_key": "k2",
                 "device_id": "arm-01",
                 "payload": {"text": "x", "ime_device_id": "k40-01",
                             "typing_speed": "turbo"}},
                source="console", context=op_context())
    finally:
        set_default_service(None)


def test_device_polls_keep_device_online_without_heartbeat(tmp_path):
    """校准页轮询=隐式心跳：手机停在校准页不再发心跳，设备列表仍显示在线。

    用户拍板 2026-09-03：调试台刷新页面应自动看到在线设备，不需要手机端
    点「测试连接」。设备级端点（校准轮询/任务长轮询/控制轮询）都会 touch。
    """
    app, _runtime = _app(tmp_path)
    with TestClient(app) as client:
        # 从未心跳过的设备：校准页轮询一次（204 无会话也计入在线证据）
        first = client.get("/api/v1/devices/k40-poll/calibration", headers=_HP1)
        assert first.status_code == 204
        devices = client.get("/api/v1/devices").json()["items"]
        entry = next(d for d in devices if d["deviceId"] == "k40-poll")
        assert entry["online"] is True

        # 任务长轮询也计入：另一台设备
        assert client.get("/api/v1/devices/k40-poll2/tasks/next?waitSeconds=0",
                          headers=_HP2).status_code == 204
        devices = client.get("/api/v1/devices").json()["items"]
        assert next(d for d in devices if d["deviceId"] == "k40-poll2")["online"] is True

        # 心跳携带的元数据不被 touch 覆盖：先心跳（keyboard_active=True），再轮询
        client.post("/api/v1/devices/k40-poll/heartbeat", headers=_HP1,
                    json={"appVersion": "1.2.3", "keyboardActive": True})
        client.get("/api/v1/devices/k40-poll/calibration", headers=_HP1)
        entry = next(d for d in client.get("/api/v1/devices").json()["items"]
                     if d["deviceId"] == "k40-poll")
        assert entry["online"] is True and entry["keyboardActive"] is True
        assert entry["appVersion"] == "1.2.3"


def test_gesture_evidence_root_follows_db_parent(tmp_path):
    """P1-4（2026-09-30 审查）：手势轨迹证据根必须与 db 同根（data/）。

    旧启发式只在 db 父目录名为 devices 时才传 data_root；生产 db 是
    data/screen_ime_tasks.db → data_root=None → 兜底到 adapters/data/，
    与 run 状态（data/devices/）撕裂。修复后 data_root 直取 db 父目录。"""
    from adapters.screen_ime.service import ScreenImeService
    from types import SimpleNamespace

    data_dir = tmp_path / "data"
    service = ScreenImeService(data_dir / "screen_ime_tasks.db")
    coordinator = service.gesture_capture
    assert coordinator._data_root == data_dir
    # 行为级：证据目录落在同一数据根下，不再漂到 adapters/data
    session = SimpleNamespace(device_id="arm-01", run_id="run-ev")
    trial_dir = coordinator._evidence_dir(session, "trial-ev")
    assert trial_dir == data_dir / "devices" / "arm-01" / \
        "gesture_validation_evidence" / "run-ev" / "trial-ev"
    service.close()
    service.close()
