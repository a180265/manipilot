"""Named gestures share production geometry; archive operations never move hardware."""
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from drivers import calibration_bundle as cb, screen_gestures as sg
from scripts import motor_debug_server as debug
from tests.test_motor_debug_basic_gestures import env
from tests.test_motor_debug_gesture_plans import (
    SimulatedEntityVerifier, normalized_request, plan_env, _dbg_headers,
)


# 测试更正（§4.5，2026-10-03）：R3-14 写边界——HTTP 写请求统一携
# _dbg_headers（自举 /api/debug-session 会话 + 同站浏览器对等头）。


def body(bundle, action="swipe"):
    return {
        "deviceId": "arm-01", "contextId": "ctx", "label": "返回上一级",
        "operator": "测试员", "binding": sg.binding_snapshot(bundle), "action": action,
        **({"start": {"x": .04, "y": .5}, "end": {"x": .32, "y": .5}}
           if action == "swipe" else {"point": {"x": .27, "y": .67}, "interval_ms": 80}),
    }


@pytest.mark.parametrize("action", ["swipe", "double_tap"])
def test_save_list_and_reload_preserve_calibration_and_never_issue_motion(plan_env, action):
    daemon, _, root, bundle = plan_env
    before = cb.bundle_path("arm-01", root).read_bytes()
    saved = debug.save_basic_gesture("arm-01", body(bundle, action))
    assert saved["version"] == 1 and saved["usable"]
    assert saved["effect_status"] == "unverified"
    assert saved["payload"]["binding"] == sg.binding_snapshot(bundle)
    assert saved["plan"]["id"] == "custom" and saved["plan"]["calculated"]
    timing = "duration_ms" if action == "swipe" else "interval_ms"
    plan_timing = "durationMs" if action == "swipe" else "intervalMs"
    assert saved["payload"][timing] == saved["plan"][plan_timing]
    listing = debug.saved_basic_gestures("arm-01", "ctx")
    assert listing["deviceId"] == "arm-01" and listing["contextId"] == "ctx"
    assert listing["gestures"] == [saved]
    assert not daemon.commands and not debug._BASIC_TICKETS
    assert debug._basic_load("arm-01") is None
    assert cb.bundle_path("arm-01", root).read_bytes() == before


def test_unknown_action_does_not_prevent_drawing_or_saving_but_still_blocks_execution(plan_env):
    daemon, _, _, bundle = plan_env
    debug._basic_save("arm-01", {"deviceId": "arm-01", "requestId": "old",
        "outcome": "unknown", "binding": {"daemonSession": daemon.session}})
    saved = debug.save_basic_gesture("arm-01", body(bundle))
    assert saved["usable"] and saved["effect_status"] == "unverified"
    assert debug.basic_gesture_state("arm-01", "ctx")["blocked"]
    assert debug._basic_load("arm-01")["outcome"] == "unknown"
    assert not daemon.commands


def test_save_defaults_audit_source_without_an_operator_field(plan_env):
    request = body(plan_env[3])
    request.pop("operator")
    saved = debug.save_basic_gesture("arm-01", request)
    assert saved["usable"] and saved["created_by"] == "调试台"
    assert not plan_env[0].commands


@pytest.mark.parametrize("code", ["device_paused", "device_quarantined", "device_maintenance"])
def test_device_execution_blockers_are_visible_without_blocking_path_save(plan_env, monkeypatch, code):
    plan_env[1]["checks"] = [{"code": code, "message": code}]
    monkeypatch.setattr(debug, "CameraMotionVerifier", SimulatedEntityVerifier)
    planned = debug.calculated_gesture_plan("arm-01", "ctx")
    assert planned["calibrationReady"] and planned["blockingReasons"]
    assert all(not action["usable"] for action in planned["actions"].values())
    assert debug.save_basic_gesture("arm-01", body(plan_env[3]))["usable"]
    assert not plan_env[0].commands


@pytest.mark.parametrize("change", ["binding", "extra", "device", "interval", "edge", "operator"])
def test_save_rejects_stale_or_invalid_data_before_writing(plan_env, change):
    daemon, _, root, bundle = plan_env
    request = body(bundle)
    if change == "binding":
        request["binding"]["calibration_fingerprint"] = "0" * 64
    elif change == "extra":
        request["tapDepth"] = 9
    elif change == "device":
        request["deviceId"] = "arm-02"
    elif change == "interval":
        request["interval_ms"] = 80
    elif change == "edge":
        request["start"]["x"] = 0
    else:
        request["operator"] = ""
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        debug.save_basic_gesture("arm-01", request)
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert not daemon.commands and not debug._BASIC_TICKETS


def test_saved_geometry_turns_stale_after_calibration_changes_without_rebinding(plan_env):
    _, _, root, bundle = plan_env
    saved = debug.save_basic_gesture("arm-01", body(bundle))
    old_binding = deepcopy(saved["binding"])
    bundle["safePark"]["robotX"] = 1
    cb.save_bundle(bundle, root)
    listed = debug.saved_basic_gestures("arm-01", "ctx")["gestures"][0]
    assert not listed["usable"] and listed["blocking_reasons"]
    assert listed["binding"] == listed["payload"]["binding"] == old_binding
    assert listed["plan"] is None


def test_named_save_http_routes_enforce_explicit_device_and_context(plan_env, monkeypatch):
    context = plan_env[1]

    def require(device, context_id, **kwargs):
        if device != "arm-01" or context_id != "ctx":
            raise ValueError("设备上下文不一致")
        assert kwargs.get("motion") is False
        return context

    monkeypatch.setattr(debug, "require_context", require)
    server = ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, data=None):
        # 测试更正（§4.5，2026-10-03）：R3-14 写边界——携自举会话+同站头
        req = Request(f"http://127.0.0.1:{server.server_port}{path}",
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={"Content-Type": "application/json",
                               **_dbg_headers(f"http://127.0.0.1:{server.server_port}")})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        request = body(plan_env[3])
        request.pop("operator")
        code, saved = call("/api/gestures/saved", request)
        assert code == 200 and saved["version"] == 1
        code, listed = call("/api/gestures/saved?deviceId=arm-01&contextId=ctx")
        assert code == 200 and listed["gestures"] == [saved]
        for path in ("/api/gestures/saved?deviceId=../arm-01&contextId=ctx",
                     "/api/gestures/saved?deviceId=arm-01&contextId=wrong",
                     "/api/gestures/saved?deviceId=arm-01&device=arm-02&contextId=ctx"):
            assert call(path)[0] == 400
        invalid = body(plan_env[3])
        invalid["deviceId"] = "../arm-01"
        assert call("/api/gestures/saved", invalid)[0] == 400
        assert not plan_env[0].commands and not debug._BASIC_TICKETS
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_http_single_gesture_needs_no_operator_form(plan_env, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(debug, "CameraMotionVerifier", SimulatedEntityVerifier)
    run = AsyncMock(return_value=("confirmed", {"motionConfirmed": True}))
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    server = ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = normalized_request()
        request.pop("operator")
        req = Request(f"http://127.0.0.1:{server.server_port}/api/gestures/test",
                      data=json.dumps(request).encode(),
                      headers={"Content-Type": "application/json",
                               # 测试更正（§4.5，2026-10-03）：R3-14 写边界
                               **_dbg_headers(f"http://127.0.0.1:{server.server_port}")})
        with urlopen(req, timeout=3) as response:
            result = json.load(response)
        assert result["outcome"] == "confirmed" and result["operator"] == "调试台"
        run.assert_awaited_once()
        assert run.await_args.args[1]["operator"] == "调试台"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
