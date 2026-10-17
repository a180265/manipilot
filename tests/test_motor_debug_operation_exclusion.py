"""真实调试 HTTP 入口的并发互斥；机械、手机和摄像头均为隔离 fake。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from http.server import ThreadingHTTPServer
import json
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts import motor_debug_server as debug
from scripts.camera_binding_v2 import installation_lock
from tests.test_calibration_wizard import Env, FakeDaemon, auto_report, drive_to_z_ready


@pytest.fixture
def console(tmp_path, monkeypatch):
    env = Env(tmp_path)
    drive_to_z_ready(env)
    env.ime.phone_lag = 0
    env.wizard.phone_arm_lead_s = 0
    env.jog_to_screen(540, 1200)
    other = FakeDaemon()
    for daemon in (env.daemon, other):
        raw_cmd = daemon.cmd
        daemon.cmd = lambda command, execute=raw_cmd: json.dumps(execute(command))
        daemon.motion_scope = nullcontext
        daemon.motion_revision = 0
        daemon.motion_active = False
        position = daemon.mpos
        daemon.mpos = lambda read_position=position, **kwargs: read_position()
    env.daemon.stop = lambda: {"stopped": True}
    env.daemon.daemon_estop = lambda reason: {"estop_locked": True}
    monkeypatch.setattr(debug, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "OPS_PATH", tmp_path / "ops.jsonl")
    monkeypatch.setattr(debug, "INSTALLATION_LOCK_ROOT", tmp_path)
    monkeypatch.setattr(debug, "WIZARDS", {"arm-01": env.wizard})
    daemons = {"arm-01": env.daemon, "arm-02": other}
    monkeypatch.setattr(debug, "DAEMONS", daemons)
    monkeypatch.setattr(debug, "CONTEXTS", {
        "ctx": {"device_id": "arm-01", "expires_at": time.time() + 60}})
    monkeypatch.setattr(debug, "DEVICE_CONFIGS", {
        device: {"daemon_addr": "127.0.0.1:1"} for device in daemons})
    monkeypatch.setattr(debug, "_STATUS_LOCKS", {})
    monkeypatch.setattr(debug, "_STATUS_CACHES", {})
    monkeypatch.setattr(debug, "daemon_for", lambda device: daemons[device])
    monkeypatch.setattr(debug, "require_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(debug, "_runtime_device_records", lambda: {})
    monkeypatch.setattr(debug, "camera_status", lambda device: {"has_frame": True, "seq": 1})
    server = ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connect = socket.socket.connect

    def isolated_connect(sock, address):
        assert address == server.server_address, f"禁止测试连接真设备：{address}"
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", isolated_connect)
    with urlopen(origin + "/api/debug-session", timeout=2) as response:
        csrf = json.load(response)["csrf_token"]
        cookie = response.headers["Set-Cookie"].split(";")[0]
    headers = {"Content-Type": "application/json", "Origin": origin,
               "Sec-Fetch-Site": "same-origin", "X-Debug-CSRF": csrf, "Cookie": cookie}

    def call(path, body=None, timeout=2):
        if body is not None:
            body = {"deviceId": "arm-01", "contextId": "ctx", **body}
        request = Request(origin + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    yield env, call
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()
    env.close()


@pytest.mark.parametrize("dwell", [0, 50, 100])
def test_http_confirmation_forwards_dwell_to_one_contact_and_saved_parameters(console, dwell):
    env, call = console
    auto_report(env.ime, env.daemon)
    start = len(env.daemon.cmds)
    status, result = call("/api/wizard", {
        "action": "z-confirm-safe", "tapDepth": 3, "tapFeed": 1500, "tapDwellMs": dwell})
    assert status == 200, result
    assert result["tapDwellMs"] == dwell
    taps = [line for line in env.daemon.cmds[start:] if line.startswith("TAP")]
    assert taps == ["TAP 3.000 F1500" + (f" P{dwell / 1000:g}" if dwell else "")]
    assert env.wizard.state["z"]["tap"]["dwellMs"] == dwell


def test_diagnostic_tap_preserves_ready_calibration_and_uses_requested_dwell_once(console):
    from drivers import calibration_bundle as cb

    env, call = console
    bundle = cb.load_bundle("arm-01", env.wizard.data_root)
    bundle["status"] = "ready"
    bundle["z"].update(tapDepth=10, tapFeed=2000, tapDwellMs=0)
    cb.save_bundle(bundle, env.wizard.data_root)
    original = cb.bundle_path("arm-01", env.wizard.data_root).read_bytes()
    start = len(env.daemon.cmds)
    status, result = call("/api/tap", {"depth": 8, "feed": 2000, "dwell": .05})
    assert status == 200, result
    assert [line for line in env.daemon.cmds[start:] if line.startswith("TAP")] == [
        "TAP 8.000 F2000 P0.05"]
    assert cb.bundle_path("arm-01", env.wizard.data_root).read_bytes() == original


@pytest.mark.parametrize("action", ["z-confirm-safe", "z-test"])
@pytest.mark.parametrize("wait_phase", ["arming", "report"])
def test_parameter_test_rejects_conflicting_requests_without_queueing(console, action, wait_phase):
    env, call = console
    entered, release = threading.Event(), threading.Event()
    original = env.ime.arm if wait_phase == "arming" else env.ime.get_session

    def delayed_phone(*args, **kwargs):
        if wait_phase == "arming" or env.daemon.tap_count:
            entered.set()
            assert release.wait(8), "测试未释放手机等待"
        return original(*args, **kwargs)

    if wait_phase == "arming":
        env.ime.arm = delayed_phone
    else:
        env.ime.get_session = delayed_phone
    auto_report(env.ime, env.daemon)
    with ThreadPoolExecutor(max_workers=2) as pool:
        auto_state = None
        active = pool.submit(call, "/api/wizard", {
            "action": action, "tapDepth": 3, "tapFeed": 1500}, 12)
        try:
            assert entered.wait(3)
            for path in (
                "/api/jog", "/api/tap", "/api/hover", "/api/park", "/api/goto",
                "/api/resume", "/api/connect", "/api/estop_reset", "/api/serial-bind",
                "/api/gestures/test", "/api/gestures/review", "/api/ui-targets/test",
                "/api/ui-targets/delete-test", "/api/gesture-profiles/test",
                "/api/auto-gestures/validate", "/api/auto-navigate", "/api/recovery/attempt",
            ):
                status, result = call(path, {"axis": "x", "delta": 1, "feed": 150})
                assert status == 409, (path, result)
                assert result["error"]["code"] == "debug_operation_busy", (path, result)
            for conflict in ("z-confirm-safe", "z-test", "park-confirm", "park-clear", "start",
                             "auto-start", "z-save", "xy-preview"):
                status, result = call("/api/wizard", {"action": conflict})
                assert status == 409, (conflict, result)
                assert result["error"]["code"] == "debug_operation_busy"
            status, result = call("/api/calibrated-scroll?deviceId=arm-01&contextId=ctx")
            assert status == 409 and result["error"]["code"] == "debug_operation_busy"
            # 只读和紧急停止不等确认操作；不同设备可以独立执行。
            status, snapshot = call("/api/status?deviceId=arm-01&contextId=ctx")
            assert status == 200 and snapshot["device"] == "arm-01", snapshot
            assert call("/api/log?deviceId=arm-01&contextId=ctx")[0] == 200
            assert call("/api/stop", {}) == (200, {"stopped": True})
            assert call("/api/estop", {}) == (200, {"estop_locked": True})
            assert call("/api/jog", {"deviceId": "arm-02", "axis": "x", "delta": 1,
                                     "feed": 150})[0] == 200
            assert not any(command.startswith("XY") for command in env.daemon.cmds)
            # 向导快照保留原内部锁语义，但不被新增请求锁拒绝。
            auto_state = pool.submit(call, "/api/wizard", {"action": "auto-state"}, 5)
            with pytest.raises(TimeoutError):
                auto_state.result(timeout=0.2)
        finally:
            release.set()
            status, result = active.result(timeout=5)
            assert status == 200, result
            if auto_state is not None:
                assert auto_state.result(timeout=3)[0] == 200
        assert env.daemon.tap_count == 1
        assert not any(command.startswith("XY") for command in env.daemon.cmds)
        # 结束后仅新提交的操作执行，之前的冲突请求没有延迟补发。
        assert call("/api/jog", {"axis": "x", "delta": 1, "feed": 150})[0] == 200
        assert len([command for command in env.daemon.cmds if command.startswith("XY")]) == 1


def test_failed_parameter_test_releases_device_operation(console):
    env, call = console
    original = env.ime.arm

    def fail_arm(*args, **kwargs):
        raise RuntimeError("模拟手机布防失败")

    env.ime.arm = fail_arm
    status, result = call("/api/wizard", {"action": "z-confirm-safe"})
    assert status == 400 and "模拟手机布防失败" in result["error"]["message"]
    assert env.daemon.tap_count == 0
    env.ime.arm = original
    auto_report(env.ime, env.daemon)
    assert call("/api/wizard", {"action": "z-confirm-safe"})[0] == 200
    assert env.daemon.tap_count == 1


def test_parameter_confirmation_honors_installation_lock_and_releases_operation(console, tmp_path):
    env, call = console
    with installation_lock(tmp_path, "arm-01", exclusive=True):
        status, result = call("/api/wizard", {"action": "z-confirm-safe"})
        assert status == 409 and result["error"]["code"] == "device_maintenance_busy"
    assert env.daemon.tap_count == 0
    auto_report(env.ime, env.daemon)
    assert call("/api/wizard", {"action": "z-confirm-safe"})[0] == 200


def test_parameter_confirmation_rejects_jog_during_phone_arming(console):
    env, call = console
    entered, release = threading.Event(), threading.Event()
    arm = env.ime.arm

    def delayed_arm(*args, **kwargs):
        entered.set()
        assert release.wait(5), "测试未释放手机布防"
        return arm(*args, **kwargs)

    env.ime.arm = delayed_arm
    auto_report(env.ime, env.daemon)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(call, "/api/wizard", {
            "action": "z-confirm-safe", "tapDepth": 3, "tapFeed": 1500}, 8)
        try:
            assert entered.wait(3)
            status, result = call("/api/jog", {"axis": "x", "delta": 1, "feed": 150})
            assert status == 409, result
            assert result["error"]["code"] == "debug_operation_busy"
            assert not any(command.startswith("XY") for command in env.daemon.cmds)
        finally:
            release.set()
            status, result = active.result(timeout=5)
            assert status == 200, result


def test_input_settings_read_proxy_uses_get_without_request_body(console, monkeypatch):
    """真实调试 GET 入口曾因缺少 body 参数始终返回 400。"""
    _env, call = console
    requests = []

    class Upstream:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"enabled":false,"revision":0}'

    def upstream(request, timeout):
        requests.append(request)
        return Upstream()

    monkeypatch.setattr(debug.urllib.request, "urlopen", upstream)
    status, body = call("/api/input-settings?deviceId=arm-01&contextId=ctx")
    assert status == 200
    assert body == {"enabled": False, "revision": 0}
    assert len(requests) == 1
    assert requests[0].get_method() == "GET"
    assert requests[0].data is None
    assert requests[0].full_url.endswith("/v1/devices/arm-01/input-settings")
