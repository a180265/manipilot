"""Automatic debugger plans and normalized submission use simulated hardware only."""
import asyncio
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from drivers import calibration_bundle as cb, screen_gestures as sg
from drivers.daemon_protocol import DOUBLE_TAP_FEATURE, SWIPE_FEATURE
from scripts import motor_debug_server as debug
from tests.test_motor_debug_basic_gestures import Camera, env, request
from tests.test_screen_gesture_driver import gesture_bundle


# 测试更正（§4.5，2026-10-03）：R3-14 调试台请求前置边界——真 HTTP 层的
# 写请求统一经 /api/debug-session 自举会话并携浏览器对等头（每 base 一份，
# 同 tests/test_r2_region_chain.py 的 _browser_headers 模式）。
_DBG_SESSIONS: dict[str, dict] = {}


def _dbg_bootstrap(base: str) -> None:
    session = _DBG_SESSIONS.setdefault(base, {"cookie": None, "csrf": None})
    if session["csrf"] is not None:
        return
    req = Request(base + "/api/debug-session")
    with urlopen(req, timeout=5) as resp:
        session["csrf"] = json.loads(resp.read() or b"{}").get("csrf_token")
        set_cookie = resp.headers.get("Set-Cookie") or ""
        if "=" in set_cookie:
            session["cookie"] = set_cookie.split(";")[0]


def _dbg_headers(base: str) -> dict:
    _dbg_bootstrap(base)
    session = _DBG_SESSIONS[base]
    headers = {"Origin": base, "Sec-Fetch-Site": "same-origin"}
    if session["csrf"]:
        headers["X-Debug-CSRF"] = session["csrf"]
    if session["cookie"]:
        headers["Cookie"] = session["cookie"]
    return headers


@pytest.fixture()
def plan_env(env, monkeypatch):
    daemon, context, root = env
    bundle = gesture_bundle()
    bundle["daemonSession"] = daemon.session
    cb.save_bundle(bundle, root)
    monkeypatch.setattr(debug, "WIZARDS", {})
    status = daemon.status
    monkeypatch.setattr(daemon, "status", lambda: {
        **status(), "motion_features": [DOUBLE_TAP_FEATURE, SWIPE_FEATURE]})
    return daemon, context, root, bundle


def normalized_request(action="double_tap", **changes):
    bundle = cb.load_bundle("arm-01", debug.DATA_DIR)
    points = ({"point": {"x": .28, "y": .63}} if action == "double_tap" else
              {"start": {"x": .5, "y": .75}, "end": {"x": .5, "y": .3}})
    planned = sg.plan_gesture(action, points, bundle)
    timing = "intervalMs" if action == "double_tap" else "durationMs"
    return request(action, coordinateSpace="screen_normalized", **points,
                   **{timing: planned[timing]}, **changes)


class SimulatedEntityVerifier:
    """Test evidence is explicit simulation, never a production authorization."""
    measurement_kind = "simulated_entity_motion"

    def __init__(self, camera, **kwargs):
        self.camera = camera

    async def before(self, label):
        await self.camera.capture()
        return {}

    async def after(self, label):
        await self.camera.capture()
        return {"verified": True}


def test_ready_plan_is_readonly_and_separates_geometry_from_verification(plan_env):
    daemon, _, root, bundle = plan_env
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = debug.calculated_gesture_plan("arm-01", "ctx")
    assert result["calibrationReady"]
    assert result["binding"] == sg.binding_snapshot(bundle)
    assert result["profile"] == {"hoverZ": -8, "tapDepth": 8, "tapFeed": 1500}
    assert result["frameSeq"] == 100
    assert all(p["calculated"] and p["effectStatus"] == "unverified" for p in result["presets"])
    assert result["presets"][0]["intervalMs"] == 50
    assert result["presets"][0]["repeatLiftMm"] == 3
    assert all(a["usable"] for a in result["actions"].values())
    assert not result["blockingReasons"]
    assert not debug._BASIC_TICKETS and not daemon.commands
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("change", ["draft", "session", "context", "camera", "z", "auto", "busy"])
def test_plan_reports_blockers_without_erasing_calibration(plan_env, monkeypatch, change):
    daemon, context, root, bundle = plan_env
    if change == "draft":
        bundle["status"] = "draft"
    elif change == "session":
        daemon.session = "replacement"
    elif change == "context":
        context["binding_revision"] += 1
    elif change == "camera":
        monkeypatch.setattr(debug, "camera_status", lambda _: {"has_frame": False})
    elif change == "z":
        bundle["z"]["tapDepth"] = 0
    elif change == "auto":
        debug.WIZARDS["arm-01"] = SimpleNamespace(auto_run_active=lambda: True)
    else:
        daemon.motion_active = True
    cb.save_bundle(bundle, root)
    monkeypatch.setattr(debug, "CameraMotionVerifier", SimulatedEntityVerifier)
    result = debug.calculated_gesture_plan("arm-01", "ctx")
    assert all(not a["usable"] and a["blockingReasons"] for a in result["actions"].values())
    assert not daemon.commands and not debug._BASIC_TICKETS


def test_missing_features_are_reported_per_action(plan_env, monkeypatch):
    daemon = plan_env[0]
    monkeypatch.setattr(debug, "CameraMotionVerifier", SimulatedEntityVerifier)
    monkeypatch.setattr(daemon, "status", lambda: {
        "session": daemon.session, "connected": True, "motion_features": [SWIPE_FEATURE]})
    result = debug.calculated_gesture_plan("arm-01", "ctx")
    assert result["actions"]["swipe"]["usable"]
    assert not result["actions"]["double_tap"]["usable"]
    assert result["presets"][0]["calculated"]


def test_plan_never_initializes_or_recovers_wizard(plan_env, monkeypatch):
    def forbidden(*args):
        pytest.fail("Read-only planning must not initialize the wizard")
    monkeypatch.setattr(debug, "get_wizard", forbidden)
    monkeypatch.setattr(debug, "_reject_motion_during_auto_run", forbidden)
    path = plan_env[2] / "devices" / "arm-01" / "calibration_wizard.json"
    path.write_text('{"stage":"precheck","imeSessionId":"old"}')
    before = path.read_bytes()
    result = debug.calculated_gesture_plan("arm-01", "ctx")
    assert result["calibrationReady"]
    assert path.read_bytes() == before and not debug.WIZARDS


@pytest.mark.parametrize("session,blocked", [("sess-A", True), ("old-session", False)])
def test_only_current_session_uncertainty_blocks_new_plan(plan_env, monkeypatch, session, blocked):
    monkeypatch.setattr(debug, "CameraMotionVerifier", SimulatedEntityVerifier)
    debug._basic_save("arm-01", {
        "deviceId": "arm-01", "requestId": "old-request", "outcome": "unknown",
        "binding": {"daemonSession": session}})
    result = debug.calculated_gesture_plan("arm-01", "ctx")
    assert all(action["usable"] is not blocked for action in result["actions"].values())
    assert debug._basic_load("arm-01")["outcome"] == "unknown"


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_custom_camera_preview_matches_production_geometry_and_writes_nothing(plan_env, action):
    daemon, _, root, bundle = plan_env
    points = ({"point": {"x": .23, "y": .57}} if action == "double_tap" else
              {"start": {"x": .35, "y": .71}, "end": {"x": .62, "y": .27}})
    expected = sg.plan_gesture(action, points, bundle)
    result = debug.preview_calculated_gesture("arm-01", {
        "deviceId": "arm-01", "contextId": "ctx", "action": action, **expected["points"]})
    for key, point in points.items():
        assert result[key] == pytest.approx(point)
    assert result["binding"] == expected["binding"]
    assert result["effectStatus"] == "unverified"
    assert not daemon.commands and not debug._BASIC_TICKETS
    assert not debug._basic_record_path("arm-01").exists()


@pytest.mark.parametrize("change", ["size", "edge", "extra", "wrong_device", "missing"])
def test_invalid_preview_does_not_create_ticket_or_move(plan_env, change):
    p = sg.plan_gesture("double_tap", {"point": {"x": .5, "y": .5}}, plan_env[3])["points"]
    body = {"deviceId": "arm-01", "contextId": "ctx", "action": "double_tap", **p}
    if change == "size":
        body["point"]["frameW"] = 640
    elif change == "edge":
        body["point"]["cameraX"] = 0
    elif change == "extra":
        body["tapDepth"] = 8
    elif change == "wrong_device":
        body["deviceId"] = "arm-02"
    else:
        body.pop("point")
    with pytest.raises(ValueError):
        debug.preview_calculated_gesture("arm-01", body)
    assert not plan_env[0].commands and not debug._BASIC_TICKETS


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_normalized_submission_is_recomputed_and_uses_full_binding(plan_env, action):
    body = normalized_request(action)
    binding, bundle = debug._basic_binding("arm-01", plan_env[1], "sess-A")
    result = debug._basic_validate("arm-01", body, binding, bundle)
    assert result["binding"] == sg.binding_snapshot(bundle)
    keys = ("point",) if action == "double_tap" else ("start", "end")
    assert all(result[k] == {"source": "screen_normalized", **body[k]} for k in keys)
    timing = "intervalMs" if action == "double_tap" else "durationMs"
    body[timing] += 1
    if action == "swipe":
        with pytest.raises(ValueError, match="重新计算"):
            debug._basic_validate("arm-01", body, binding, bundle)
    else:
        assert debug._basic_validate("arm-01", body, binding, bundle)[timing] == body[timing]


@pytest.mark.parametrize("interval", [50, 80, 120, 200, 500])
def test_normalized_double_tap_allows_manual_release_pause_only(plan_env, interval):
    body = normalized_request()
    body["intervalMs"] = interval
    binding, bundle = debug._basic_binding("arm-01", plan_env[1], "sess-A")
    result = debug._basic_validate("arm-01", body, binding, bundle)
    assert result["intervalMs"] == interval
    assert result["binding"] == sg.binding_snapshot(bundle)
    body["tapDepth"] = 1
    with pytest.raises(ValueError, match="禁止覆盖"):
        debug._basic_validate("arm-01", body, binding, bundle)


@pytest.mark.parametrize("interval", [49, 501, True, 120.5, "120"])
def test_normalized_double_tap_rejects_invalid_release_pause(plan_env, interval):
    body = normalized_request()
    body["intervalMs"] = interval
    binding, bundle = debug._basic_binding("arm-01", plan_env[1], "sess-A")
    with pytest.raises(ValueError, match="毫秒"):
        debug._basic_validate("arm-01", body, binding, bundle)


@pytest.mark.parametrize("change", ["safePark", "z", "xy", "camera"])
def test_new_binding_invalidates_old_ticket_after_calibration_change(plan_env, change):
    body = normalized_request()
    bundle = plan_env[3]
    if change == "safePark":
        bundle["safePark"]["robotX"] = 999
    elif change == "z":
        bundle["z"]["tapDepth"] = 7
    elif change == "xy":
        bundle["xy"]["robotToScreen"]["c"] += 1
    else:
        bundle["camera"]["cameraToScreen"]["maxErrorPx"] += .1
    cb.save_bundle(bundle, plan_env[2])
    with pytest.raises(ValueError, match="绑定、校准或会话"):
        debug.test_basic_gesture("arm-01", body)
    assert not plan_env[0].commands and debug._basic_load("arm-01") is None


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_missing_feature_never_releases_lease_writes_pending_or_dispatches(plan_env, monkeypatch, action):
    daemon = plan_env[0]
    daemon.lease_id = "own"
    daemon.lease = {"lease_id": "own", "owner_type": "debug"}
    body = normalized_request(action)
    monkeypatch.setattr(daemon, "status", lambda: {
        "session": "sess-A", "connected": True, "lease": daemon.lease, "motion_features": []})
    run = AsyncMock()
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", body)
    run.assert_not_called()
    assert not daemon.commands and debug._basic_load("arm-01") is None


@pytest.mark.parametrize("action,interval", [
    ("double_tap", None), ("double_tap", 120), ("swipe", None),
])
def test_normalized_real_driver_fake_daemon_one_macro_no_reconnect(
        plan_env, monkeypatch, action, interval):
    from drivers import real_stubs
    from tests.test_daemon_arm import FakeDaemon

    monkeypatch.setattr(real_stubs, "require_confirmed_hardware", lambda: None)
    monkeypatch.setattr(debug, "CameraMotionVerifier", SimulatedEntityVerifier)
    camera = Camera()
    monkeypatch.setattr(debug, "_gesture_camera", lambda *args: camera)
    monkeypatch.setattr(debug, "check_live_calibration", AsyncMock(return_value=([], {}, None)))
    daemon = FakeDaemon(mpos=(-52, 49, -8)).start()
    daemon.session = "sess-A"
    daemon.motion_features = [DOUBLE_TAP_FEATURE, SWIPE_FEATURE]
    monkeypatch.setattr(debug, "device_config", lambda _: {"daemon_addr": f"127.0.0.1:{daemon.port}"})
    before = cb.bundle_path("arm-01", plan_env[2]).read_bytes()
    try:
        body = normalized_request(action)
        if interval is not None:
            body["intervalMs"] = interval
        result = debug.test_basic_gesture("arm-01", body)
        assert result["outcome"] == "confirmed"
        assert result["evidence"]["effectStatus"] == "manual_confirmation_required"
        motion = result["evidence"]["motion"]
        assert motion["tap_count"] == 2 if action == "double_tap" else motion["swipe_executed"]
        command = "TAP2 " if action == "double_tap" else "SWIPE "
        assert sum(c.startswith(command) for c in daemon.commands) == 1
        if action == "double_tap":
            assert len(daemon.commands) == 4
            assert daemon.commands[0].startswith("XY ")
            expected_interval = 50 if interval is None else interval
            assert daemon.commands[-3:] == [
                "Z 5.000 F1500", f"TAP2 3.000 F1500 I{expected_interval / 1000:g}",
                "Z -5.000 F1500"]
            assert motion["interval_ms"] == expected_interval
            assert motion["controller_completed"] is True
            assert motion["phases"][-1]["phase"] == "double_tap_retract"
        commands = list(daemon.commands)
        assert debug.test_basic_gesture("arm-01", deepcopy(body)) == result
        assert daemon.commands == commands
        assert sum(c.startswith(command) for c in daemon.commands) == 1
        assert not any(c.startswith(("CONNECT", "DISCONNECT")) for _, c in daemon.requests)
        assert not daemon.lease and not daemon.transactions and camera.closed
        assert daemon.mpos[2] == -8
        assert cb.bundle_path("arm-01", plan_env[2]).read_bytes() == before
    finally:
        daemon.stop()


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("fault", [
    "ack_only", "stopped", "cancelled", "unknown", "partial",
    "aborted", "unknown_flag", "failed_status", "failed_outcome", "malformed_stopped",
])
def test_normalized_incomplete_result_is_never_confirmed(plan_env, monkeypatch, action, fault):
    body = normalized_request(action)
    camera = Camera()
    monkeypatch.setattr(debug, "CameraMotionVerifier", SimulatedEntityVerifier)
    monkeypatch.setattr(debug, "_gesture_camera", lambda *args: camera)

    class Arm:
        def __init__(self, *args, **kwargs):
            self.daemon_addr = kwargs["daemon_addr"]

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def read_state(self):
            return {"session": "sess-A"}

        async def move(self, target):
            assert target["binding"] == sg.binding_snapshot(plan_env[3])
            await self.motion_verifier.before(action)
            await self.motion_verifier.after(action)
            result = {"ok": True, "motion_verified": True, "tap_count": 2, "swipe_executed": True}
            if fault == "ack_only":
                result["motion_verified"] = False
            elif fault == "partial":
                result.update(tap_count=1, swipe_executed=False)
            elif fault == "unknown":
                result["outcome_hint"] = "unknown"
            elif fault == "unknown_flag":
                result["unknown"] = True
            elif fault == "failed_status":
                result["status"] = "failed"
            elif fault == "failed_outcome":
                result["outcome"] = "failed"
            elif fault == "malformed_stopped":
                result["stopped"] = 0
            else:
                result[fault] = True
            return result

        double_tap = swipe = move

    monkeypatch.setattr(debug, "DaemonArmDriver", Arm)
    binding, bundle = debug._basic_binding("arm-01", plan_env[1], "sess-A")
    gesture = debug._basic_validate("arm-01", body, binding, bundle)
    outcome, evidence = asyncio.run(debug._run_basic_gesture("arm-01", body, gesture))
    assert outcome == "unknown" and not evidence["motionConfirmed"]
    assert camera.closed and not plan_env[0].commands


def test_real_http_routes_allow_supervised_gesture_without_probe_template(plan_env, monkeypatch):
    context = plan_env[1]

    def require(device, context_id, **kwargs):
        if device != "arm-01" or context_id != "ctx":
            raise ValueError("Invalid fixture context")
        return context

    monkeypatch.setattr(debug, "require_context", require)
    run = AsyncMock(return_value=("executed", {"controllerCompleted": True, "motionConfirmed": False}))
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    server = ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def call(path, body=None):
        # 测试更正（§4.5，2026-10-03）：R3-14 写边界——携自举会话+同站头
        req = Request(origin + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", **_dbg_headers(origin)})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        code, planned = call("/api/gestures/plan?deviceId=arm-01&contextId=ctx")
        assert code == 200 and planned["calibrationReady"]
        assert planned["actions"]["double_tap"]["usable"]
        p = planned["presets"][0]["points"]["point"]
        code, preview = call("/api/gestures/preview", {
            "deviceId": "arm-01", "contextId": "ctx", "action": "double_tap", "point": p})
        assert code == 200 and preview["point"] == pytest.approx({"x": .5, "y": .5})
        for path in ("/api/gestures/plan?deviceId=arm-01&contextId=wrong",
                     "/api/gestures/plan?deviceId=../arm-01&contextId=ctx",
                     "/api/gestures/plan?deviceId=arm-01&device=arm-02&contextId=ctx"):
            code, error = call(path)
            assert code == 400 and error["error"]
        code, result = call("/api/gestures/test", normalized_request())
        assert code == 200 and result["outcome"] == "executed"
        assert not result["evidence"]["motionConfirmed"]
        assert debug._basic_record_path("arm-01").exists()
        run.assert_awaited_once()
        assert not plan_env[0].commands
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_normalized_swipe_accepts_stroke_mm_real_frontend_payload(plan_env):
    """P0-2（2026-09-30 审查）：前端 swipe 载荷无条件带 strokeMm——三层
    白名单必须放行，否则调试台所有滑动测试 400。服务端校验 5~150（与前端
    IIFE 同区间）；不带 strokeMm 的旧载荷保持兼容。"""
    binding, bundle = debug._basic_binding("arm-01", plan_env[1], "sess-A")
    result = debug._basic_validate("arm-01", normalized_request("swipe", strokeMm=30),
                                   binding, bundle)
    assert result["strokeMm"] == 30
    # 旧载荷（不带 strokeMm）保持兼容
    legacy = normalized_request("swipe")
    legacy_result = debug._basic_validate("arm-01", legacy, binding, bundle)
    assert "strokeMm" not in legacy_result
    for bad in (4, 151, "30", None, True, 0, -30):
        with pytest.raises(ValueError):
            debug._basic_validate("arm-01", normalized_request("swipe", strokeMm=bad),
                                  binding, bundle)
