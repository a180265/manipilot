"""D2：gesture-capture.v1 手机采样协议（规范第 5、22.2、22.3 节）。

HTTP 层验证：注册/心跳/命令轮询/Ready 握手/轨迹去重与冲突/页面事件/
与旧校准布防互斥/体积限制/旧协议端点不变。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from adapters.screen_ime.gesture_capture import GestureCaptureCoordinator
from adapters.screen_ime.service import ScreenImeService, set_default_service

UUIDS = {
    "client": "0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001",
    "session": "6b9f7c2e-1f4a-4d5e-9c01-2f2f2e08c002",
    "trial": "7c0f8d3f-2a5b-4e6f-8d12-2f2f2e08c003",
    "trace": "8d1f9e4a-3b6c-4f70-9e23-2f2f2e08c004",
    "reset": "9e2faf5b-4c7d-4a81-8f34-2f2f2e08c005",
}
from drivers.gesture_program import build_coordinate_frame

FRAME = build_coordinate_frame(
    binding={"device_id": "arm-01", "daemon_session": "sess-A",
             "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
             "binding_revision": 7, "calibration_fingerprint": "a" * 64,
             "frame_width": 1280, "frame_height": 720},
    calibration_run_id="run-abc",
    root_width_px=1080, root_height_px=2320,
    display_width_px=1080, display_height_px=2400,
    root_origin_display_px=[0, 80], density=3.0,
    source_client_instance_id=UUIDS["client"])


def _hello(seq: int = 1, page_id: str = "gesture-capture", **page_override):
    page = {
        "pageId": page_id, "pageGeneration": 3,
        "resumed": True, "hasFocus": True, "rotation": 0,
        "rootWidthPx": 1080, "rootHeightPx": 2320,
        "displayWidthPx": 1080, "displayHeightPx": 2400,
        "rootOriginDisplayPx": [0, 80], "density": 3.0,
        "systemGestureInsets": {"left": 20, "top": 40, "right": 20, "bottom": 60},
        "systemBarInsets": {"left": 0, "top": 80, "right": 0, "bottom": 60},
        "legacyCalibrationSessionId": None, "coordinateSpaceVersion": 2,
    }
    page.update(page_override)
    return {"contractVersion": "gesture-capture.v1", "deviceId": "ime-01",
            "clientInstanceId": UUIDS["client"], "heartbeatSeq": seq,
            "apkVersionCode": 7,
            "capabilities": ["gesture_trace.v1", "scroll_fixture.v1"],
            "page": page}


def _trace(command: dict, *, trace_id: str = UUIDS["trace"], end_reason="up",
           nonce=None, client=None, scroll=True):
    return {
        "contractVersion": "gesture-capture.v1",
        "sessionId": UUIDS["session"], "trialId": UUIDS["trial"],
        "clientInstanceId": client or UUIDS["client"],
        "nonce": nonce or command["nonce"], "traceId": trace_id,
        "pageGeneration": 3, "resetId": UUIDS["reset"],
        "coordinateSpace": "calibration_root_px.v1",
        "clockDomain": "android_uptime_ms",
        "events": [
            {"index": 0, "contactIndex": 0, "action": "down", "pointerId": 0,
             "tMs": 0, "xPx": 540, "yPx": 1710, "historical": False},
            {"index": 1, "contactIndex": 0, "action": "move", "pointerId": 0,
             "tMs": 400, "xPx": 540, "yPx": 900, "historical": False},
            {"index": 2, "contactIndex": 0, "action": "up", "pointerId": 0,
             "tMs": 900, "xPx": 540, "yPx": 696, "historical": False},
        ],
        "endReason": end_reason, "droppedSampleCount": 0,
        "scroll": {
            "beforeY": 1200, "afterY": 200, "minY": 0, "maxY": 4480,
            "contentHeightPx": 6400, "viewportHeightPx": 1920, "rowHeightPx": 192,
            "firstVisibleRowBefore": 6, "firstVisibleRowAfter": 1,
            "rowOffsetBeforePx": 48, "rowOffsetAfterPx": 8,
            "settled": True, "settleWindowMs": 300,
            "resetCounterBefore": 1, "resetCounterAfter": 1,
            "boundaryBefore": "neither", "boundaryAfter": "neither",
        } if scroll else None,
        "recognition": None,
    }


def _ready(command: dict, **override):
    body = {
        "sessionId": UUIDS["session"], "trialId": UUIDS["trial"],
        "commandId": command["commandId"], "commandSeq": command["commandSeq"],
        "clientInstanceId": UUIDS["client"], "nonce": command["nonce"],
        "page": _hello()["page"],
        "coordinateFrameHash": FRAME["coordinate_frame_hash"],
        "fixtureVersion": "fixture-list-v1",
        "fixtureRegion": {"leftPx": 0, "topPx": 200, "widthPx": 1080, "heightPx": 1900},
        "resetId": UUIDS["reset"], "scrollY": 2240,
        "contentHeightPx": 6400, "viewportHeightPx": 1920,
        "ready": True,
    }
    body.update(override)
    return body


@pytest.fixture()
def rig(tmp_path: Path):
    service = ScreenImeService(tmp_path / "ime.db", tmp_path / "ime_mappings.json")
    coordinator = GestureCaptureCoordinator(
        data_root=tmp_path, legacy_armed_provider=service.calibration_armed)
    service.gesture_capture = coordinator
    set_default_service(service)
    from adapters.screen_ime.gesture_routes import create_gesture_router
    from adapters.screen_ime.routes import create_screen_ime_router

    app = FastAPI()
    app.include_router(create_screen_ime_router(service))
    app.include_router(create_gesture_router(service))
    from adapters.screen_ime.routes import screen_ime_error_handler
    from adapters.screen_ime.service import ScreenImeApiError
    app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)
    with TestClient(app) as client:
        yield client, coordinator, service, tmp_path
    set_default_service(None)


def _register(client, hello=None):
    return client.post("/api/v1/devices/ime-01/gesture-client",
                       json=hello or _hello())


def _make_session(coordinator):
    return asyncio.run(coordinator.create_session(
        device_id="arm-01", ime_device_id="ime-01", binding=FRAME["binding"],
        actions=["scroll_up"], run_id="run-abc", session_id=UUIDS["session"]))


def _arm_trial(coordinator, kind="reset_list", payload=None):
    asyncio.run(coordinator.prepare_trial(
        UUIDS["session"], UUIDS["trial"],
        {"kind": kind, "payload": payload or {"offsetFraction": 0.5},
         "coordinateFrame": FRAME,
         "calibrationFingerprint": FRAME["binding"]["calibration_fingerprint"]}))


class TestProtocol:
    def test_gesture_protocol_endpoint(self, rig):
        client, *_ = rig
        response = client.get("/api/v1/gesture-protocol")
        assert response.status_code == 200
        assert response.json() == {"contractVersion": "gesture-capture.v1",
                                   "supported": True}

    def test_register_and_poll_empty(self, rig):
        client, *_ = rig
        assert _register(client).status_code == 200
        response = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}")
        assert response.status_code == 204

    def test_register_rejects_bad_capability(self, rig):
        client, *_ = rig
        response = _register(client, _hello(capabilities=["teleport.v1"]))
        assert response.status_code == 400
        assert response.json()["error"]

    def test_command_expires_at_is_issued_at_plus_10s(self, rig):
        """P1-2（2026-09-30 审查）：契约 gesture-capture.v1 规定
        issuedAt/expiresAt 固定相差 10000ms；旧实现两者相等，遵守契约的
        手机端每条命令到达即过期，页面模式采样链无法开始。"""
        from datetime import datetime
        client, coordinator, _service, _root = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}"
        ).json()

        def parse(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        delta_ms = (parse(command["expiresAt"]) - parse(command["issuedAt"])
                    ).total_seconds() * 1000
        # issuedAt/expiresAt 两次独立取时（毫秒截断），允许 ±50ms 采样抖动
        assert 9950 <= delta_ms <= 10050, (command["issuedAt"], command["expiresAt"])

    def test_command_flow_ready_and_trace(self, rig):
        client, coordinator, _service, root = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        response = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}")
        assert response.status_code == 200
        command = response.json()
        assert command["kind"] == "reset_list"
        assert command["trialId"] == UUIDS["trial"]
        # 重复轮询返回同一 commandId
        again = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        assert again["commandId"] == command["commandId"]
        # ready
        ready_response = client.post(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/ready", json=_ready(command))
        assert ready_response.status_code == 200
        # ready 后命令不再下发
        assert client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}"
        ).status_code == 204
        # 轨迹
        trace = _trace(command)
        trace_response = client.post(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/trace", json=trace)
        assert trace_response.status_code == 201
        receipt = trace_response.json()
        assert receipt["received"] is True
        assert receipt["acceptedForEvaluation"] is True
        # 证据先落盘再回执
        evidence = root / "devices" / "arm-01" / "gesture_validation_evidence" \
            / "run-abc" / UUIDS["trial"] / f"{UUIDS['trace']}.json"
        assert evidence.exists()
        assert json.loads(evidence.read_text(encoding="utf-8"))["traceId"] == UUIDS["trace"]

    def test_ready_wrong_nonce_rejected(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        bad = _ready(command, nonce="f" * 64)
        response = client.post(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/ready", json=bad)
        assert response.status_code == 409
        assert response.json()["error"] == "trial_not_ready"

    def test_ready_payload_conflict(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        assert client.post(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/ready",
            json=_ready(command)).status_code == 200
        conflicting = _ready(command, scrollY=999)
        response = client.post(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/ready", json=conflicting)
        assert response.status_code == 409
        assert response.json()["error"] == "ready_payload_conflict"

    def test_ready_duplicate_same_body_ok(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        first = client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/ready",
                            json=_ready(command))
        second = client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/ready",
                             json=_ready(command))
        assert first.status_code == second.status_code == 200

    def test_stale_heartbeat_blocks_ready(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        # 快进时钟超过心跳新鲜度（从模块常量取值：2026-09-30 起为 6.0s，
        # P2-#4 配置漂移修复后 gesture_defaults.json 同步为 6000ms）
        from adapters.screen_ime import gesture_capture as _gc
        _base = coordinator._clock()
        coordinator._clock = lambda: _base + _gc.HEARTBEAT_FRESHNESS_S + 1.0
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        response = client.post(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/ready", json=_ready(command))
        assert response.status_code == 409
        # B4 拆分：心跳超窗=client_stale（非前台才是 client_not_foreground）
        assert response.json()["error"] == "client_stale"

    def test_command_expiry_cancels_trial(self, rig):
        """R4 P2-8：命令到期不只返回 204，还必须把 trial 置 cancelled。"""
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        base = coordinator._clock()
        coordinator._clock = lambda: base + 36.0
        # 先刷新心跳再查命令（client TTL 与 command TTL 同为 35s）
        refreshed = client.post("/api/v1/devices/ime-01/gesture-client",
                                json=_hello(seq=99))
        assert refreshed.status_code == 200, refreshed.text  # 心跳必须真实刷新
        response = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}")
        assert response.status_code == 204
        state = coordinator.trial_view(UUIDS["session"], UUIDS["trial"])
        assert state.get("state") == "cancelled", state

    def test_trace_dedup_and_conflict(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/ready",
                    json=_ready(command))
        trace = _trace(command)
        first = client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/trace",
                            json=trace)
        assert first.status_code == 201
        duplicate = client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/trace",
                                json=trace)
        assert duplicate.status_code == 200
        assert duplicate.json()["traceId"] == first.json()["traceId"]
        conflicting = _trace(command)
        conflicting["droppedSampleCount"] = 3
        response = client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/trace",
                               json=conflicting)
        assert response.status_code == 409
        assert response.json()["error"] == "trace_payload_conflict"
        state = coordinator.trial_view(UUIDS["session"], UUIDS["trial"])
        assert state["state"] == "unknown"

    def test_trace_wrong_client_rejected(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/ready",
                    json=_ready(command))
        other = _trace(command, client="1c2b3a49-8d60-4b50-ae6f-2f2f2e08c009")
        response = client.post(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/trace", json=other)
        assert response.status_code == 409

    def test_receipt_lookup(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/ready",
                    json=_ready(command))
        client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/trace",
                    json=_trace(command))
        found = client.get(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/receipts/{UUIDS['trace']}")
        assert found.status_code == 200
        assert found.json()["received"] is True
        missing = client.get(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/receipts/"
            "00000000-0000-4000-8000-000000000000")
        assert missing.status_code == 404

    def test_page_event_stored(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator, kind="prepare_back",
                   payload={"parentId": "gesture-parent", "childId": "gesture-child"})
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/ready",
                    json=_ready(command))
        event = {
            "sessionId": UUIDS["session"], "trialId": UUIDS["trial"],
            "clientInstanceId": UUIDS["client"], "nonce": command["nonce"],
            "eventId": "b5c1d2e3-4f56-4a78-9c90-2f2f2e08c007",
            "eventSeq": 1, "pageGeneration": 3,
            "kind": "back_committed", "pageId": "gesture-child",
            "uptimeMs": 4567, "callbackSource": "on_back_invoked",
            "committed": True,
        }
        response = client.post(
            f"/api/v1/gesture-sessions/{UUIDS['session']}/page-event", json=event)
        assert response.status_code == 200
        state = coordinator.trial_view(UUIDS["session"], UUIDS["trial"])
        kinds = [item["kind"] for item in state["page_events"]]
        assert "back_committed" in kinds

    def test_body_too_large(self, rig):
        client, coordinator, *_ = rig
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        command = client.get(
            f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}").json()
        client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/ready",
                    json=_ready(command))
        huge = _trace(command)
        huge["events"] = [{"index": i, "contactIndex": 0, "action": "move",
                           "pointerId": 0, "tMs": i, "xPx": 1.0, "yPx": 1.0,
                           "historical": False} for i in range(600)]
        response = client.post(f"/api/v1/gesture-sessions/{UUIDS['session']}/trace",
                               json=huge)
        assert response.status_code == 413


class TestMutualExclusion:
    def test_legacy_arm_blocks_prepare_trial(self, rig):
        client, coordinator, service, _ = rig
        _register(client)
        asyncio.run(coordinator.create_session(
            device_id="arm-01", ime_device_id="ime-01",
            binding=FRAME["binding"], actions=["scroll_up"], run_id="r2",
            session_id=UUIDS["session"]))
        cal = asyncio.run(service.calibrations.create("ime-01"))
        asyncio.run(service.calibrations.client(cal["sessionId"], "ime-01", "open"))
        asyncio.run(service.calibrations.arm(cal["sessionId"], "s-1", 1,
                                             None, None, None, None))
        from adapters.screen_ime.service import ScreenImeApiError
        with pytest.raises(ScreenImeApiError) as exc:
            asyncio.run(coordinator.prepare_trial(
                UUIDS["session"], UUIDS["trial"],
                {"kind": "reset_list", "payload": {"offsetFraction": 0.5},
                 "coordinateFrame": FRAME, "calibrationFingerprint": "a" * 64}))
        assert exc.value.code == "calibration_active"
        # 旧校准流程不受影响
        assert asyncio.run(service.calibrations.current("ime-01")) is not None

    def test_active_trial_blocks_legacy_arm(self, rig):
        client, coordinator, _service, _ = rig
        # 无活动 trial：旧布防正常
        cal = client.post("/api/v1/calibrations", json={"deviceId": "ime-01"}).json()
        client.post(f"/api/v1/calibrations/{cal['sessionId']}/client",
                   json={"deviceId": "ime-01", "state": "open"})
        ok = client.post(f"/api/v1/calibrations/{cal['sessionId']}/arm",
                         json={"sampleId": "s-1", "sequence": 1})
        assert ok.status_code == 200
        client.post(f"/api/v1/calibrations/{cal['sessionId']}/disarm",
                    json={"sampleId": "s-1"})
        # 有活动 trial：旧布防被拒
        _register(client)
        _make_session(coordinator)
        _arm_trial(coordinator)
        blocked = client.post(f"/api/v1/calibrations/{cal['sessionId']}/arm",
                              json={"sampleId": "s-2", "sequence": 2})
        assert blocked.status_code == 409
        assert blocked.json()["error"] == "gesture_sampling_active"


class TestOldProtocolUnchanged:
    def test_legacy_calibration_flow_still_works(self, rig):
        client, *_ = rig
        cal = client.post("/api/v1/calibrations", json={"deviceId": "ime-01"})
        assert cal.status_code == 200
        body = cal.json()
        client.post(f"/api/v1/calibrations/{body['sessionId']}/client",
                   json={"deviceId": "ime-01", "state": "open"})
        armed = client.post(f"/api/v1/calibrations/{body['sessionId']}/arm",
                            json={"sampleId": "s-1", "sequence": 1})
        assert armed.status_code == 200
        touch = client.post(f"/api/v1/calibrations/{body['sessionId']}/touch",
                            json={"deviceId": "ime-01", "sampleId": "s-1",
                                  "sequence": 1, "screenX": 100, "screenY": 200,
                                  "screenWidth": 1080, "screenHeight": 2400})
        assert touch.status_code == 200
        assert len(touch.json()["samples"]) == 1

    def test_health_endpoint(self, rig):
        client, *_ = rig
        response = client.get("/api/v1/health")
        assert response.status_code == 200


class TestFinishedSessionBoundedRetention:
    def test_finished_sessions_do_not_accumulate_unboundedly(self, rig):
        """R1 P2：finished 会话（含 page_events）只增不删会无界占内存。"""
        client, coordinator, _service, _tmp = rig
        _register(client)
        for index in range(20):
            session = asyncio.run(coordinator.create_session(
                device_id="arm-01", ime_device_id="ime-01",
                binding=FRAME["binding"], actions=["scroll_up"],
                run_id=f"r-b{index}", session_id=f"sess-b-{index}"))
            asyncio.run(coordinator.finish_session(session["sessionId"], "passed"))
        with coordinator._lock:
            assert len(coordinator._sessions) <= 16


def test_accept_ready_rejects_coordinate_frame_hash_mismatch(rig):
    """B3（2026-10-01 backlog）：契约 Ready.coordinateFrameHash 必填却从未
    被比对——陈旧/旋转后的页面可通过握手，用错误 fixtureRegion 引导评估。
    失配必须 409 拒收。"""
    client, coordinator, _service, _root = rig
    _register(client)
    _make_session(coordinator)
    _arm_trial(coordinator)
    command = client.get(
        f"/api/v1/devices/ime-01/gesture-command?clientInstanceId={UUIDS['client']}"
    ).json()
    ready = _ready(command)
    ready["coordinateFrameHash"] = "0" * 64  # 陈旧/异页哈希
    response = client.post(
        f"/api/v1/gesture-sessions/{UUIDS['session']}/ready", json=ready)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "coordinate_frame_changed"


def test_close_clears_global_coordinator_reference(tmp_path, monkeypatch):
    """B8（2026-10-01 backlog）：首实例永占全局——close 后同进程重建 app
    时插件不得继续持已关闭协调器（get_global_coordinator 应回落 None，
    由新实例重新占位）。"""
    from adapters.screen_ime import gesture_capture as gc
    from adapters.screen_ime.service import ScreenImeService

    # 隔离全局态（套件内此前的协调器已占位，这里模拟干净进程）
    monkeypatch.setattr(gc, "_GLOBAL_COORDINATOR", None)
    service = ScreenImeService(tmp_path / "ime.db")
    first = service.gesture_capture
    assert gc.get_global_coordinator() is first
    service.close()
    assert gc.get_global_coordinator() is None, "close 后全局引用必须清空"
    assert first._sessions == {} and first._clients == {}
    # 新实例重新占位
    second_service = ScreenImeService(tmp_path / "ime2.db")
    assert gc.get_global_coordinator() is second_service.gesture_capture
    second_service.close()
