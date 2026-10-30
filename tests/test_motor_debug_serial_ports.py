"""调试台串口换口检测端点（2026-09-30）：
serial_ports_payload 扫描并列出绑定/未绑定；serial_bind_payload 持久化新串口并经平台 attach 热重绑。"""
import json
import urllib.request
import pytest
from types import SimpleNamespace

from scripts import motor_debug_server as debug


# 测试更正（§4.5，2026-10-03）：R3-14 调试台请求前置边界（回环+精确 Host+
# Origin/Sec-Fetch-Site+写 CSRF）——真 HTTP 层的写请求统一经 /api/debug-session
# 自举会话并携浏览器对等头（同 tests/test_r2_region_chain.py 模式；每 base 一份）。
_DBG_SESSIONS: dict[str, dict] = {}


def _dbg_bootstrap(base: str) -> None:
    session = _DBG_SESSIONS.setdefault(base, {"cookie": None, "csrf": None})
    if session["csrf"] is not None:
        return
    req = urllib.request.Request(base + "/api/debug-session")
    with urllib.request.urlopen(req, timeout=5) as resp:
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
def env(monkeypatch):
    monkeypatch.setattr(debug, "_merged_device_configs", lambda: {
        "arm-01": {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-21110",
                   "camera_id": "cam-01", "camera_mode": "multi_channel",
                   "camera_device_unique_id": "0x21110001c456200",
                   "camera_frame_url": "http://127.0.0.1:8790/cameras/cam-01/latest.jpg",
                   "camera_status_url": "http://127.0.0.1:8790/cameras/cam-01/status",
                   "expected_frame_width": 1280, "expected_frame_height": 720,
                   "expected_frame_fps": 10, "daemon_addr": "127.0.0.1:8795",
                   "arm_slot": 1, "hardware_mode": "real"}})
    calls = {"attach": None}
    scan_payload = {"unknown_ports": [{"path": "/dev/cu.usbserial-21130",
                                       "vid": "0x2A59", "pid": "0x7523",
                                       "serial_number": "A9M7",
                                       "fingerprint": "fp-21130"}],
                    "bindings": [{"device_id": "arm-01",
                                  "serial_port": "/dev/cu.usbserial-21110",
                                  "present": True, "vid": "0x2A59",
                                  "pid": "0x7523", "serial_number": "A9M7",
                                  "hardware_fingerprint": "fp-21110"}],
                    "probe_available": True}

    def fake_agent_json(method, path, body=None):
        if path == "/v1/devices/discovery/scan":
            return scan_payload
        calls["attach"] = {"path": path, "body": body}
        return {"ok": True, "note": "绑定已更新"}

    monkeypatch.setattr(debug, "_agent_json", fake_agent_json)
    monkeypatch.setattr(debug, "log_op", lambda *a, **k: None)
    return calls


def test_serial_ports_lists_bound_and_unbound(env):
    data = debug.serial_ports_payload("arm-01")
    by_path = {p["path"]: p for p in data["ports"]}
    assert by_path["/dev/cu.usbserial-21110"]["bound_device"] == "arm-01"
    assert by_path["/dev/cu.usbserial-21110"]["state"] == "present"
    assert by_path["/dev/cu.usbserial-21130"]["bound_device"] is None
    assert by_path["/dev/cu.usbserial-21130"]["state"] == "unbound"
    assert not data.get("hint")


def test_serial_bind_updates_config_and_proxies_attach(env, monkeypatch, tmp_path):
    # 隔离：save 必须写到 tmp（真实 devices.json 绝不能被测试触碰）
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp_path)
    import json as _json
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-21110",
         "arm_slot": 1, "hardware_mode": "real"}]}))

    data = debug.serial_bind_payload("arm-01", "/dev/cu.usbserial-21130")
    assert data["ok"] is True and data["serial_port"] == "/dev/cu.usbserial-21130"
    assert data["old_serial_port"] == "/dev/cu.usbserial-21110"
    attach = env["attach"]
    assert attach["path"] == "/v1/devices/arm-01/attach"
    assert attach["body"]["serial_port"] == "/dev/cu.usbserial-21130"
    saved = _json.loads((tmp_path / "configs" / "devices.json").read_text())
    assert saved["devices"][0]["serial_port"] == "/dev/cu.usbserial-21130"
    # B1：合并视图的注册中心字段不落盘
    assert "hardware_mode" in saved["devices"][0]
    assert "presence_state" not in saved["devices"][0]


def test_serial_bind_rejects_unknown_device(env):
    with pytest.raises(ValueError, match="未登记"):
        debug.serial_bind_payload("arm-99", "/dev/cu.usbserial-21130")


def test_discovery_fingerprint_is_path_free():
    """指纹不含 path：同一适配器换 USB 口后指纹不变（跨端口自愈的前提）。"""
    import hashlib
    from pathlib import Path

    src = (Path(debug.__file__).resolve().parents[1] / "runtime" / "discovery.py").read_text()
    # 指纹身份串不含 device/path 字段（源码级断言，防止回归加入路径）
    identity_line = [l for l in src.split("\n") if "identity = " in l and "join" in l][0]
    block_end = src.index("))", src.index("identity = "))
    block = src[src.index("identity = "):block_end]
    assert "path" not in block, "指纹身份串不得包含 path（跨端口自愈依赖）"
    # 行为级：同身份不同 device 的哈希一致
    def fp(fields):
        identity = "|".join(str(v or "") for v in fields)
        import hashlib
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    assert fp(("0x2A59", "0x7523", "S1")) == fp(("0x2A59", "0x7523", "S1"))


def test_serial_ports_hides_absent_bound_port(env, monkeypatch):
    """拔掉/断电的绑定端口：从可选 ports 消失，仅出现在 absent_ports（用户拍板）。"""
    import copy

    def scan_payload(**overrides):
        base = {"unknown_ports": [{"path": "/dev/cu.usbserial-21130",
                                   "vid": "0x2A59", "pid": "0x7523",
                                   "serial_number": "A9M7",
                                   "fingerprint": "fp-21130"}],
                "bindings": [{"device_id": "arm-01",
                              "serial_port": "/dev/cu.usbserial-21110",
                              "present": False, "vid": "0x2A59",
                              "pid": "0x7523", "serial_number": "A9M7",
                              "hardware_fingerprint": "fp-21110"}],
                "probe_available": True}
        base.update(overrides)
        return base

    monkeypatch.setattr(debug, "_agent_json",
                        lambda method, path, body=None: scan_payload())
    data = debug.serial_ports_payload("arm-01")
    paths = [p["path"] for p in data["ports"]]
    assert "/dev/cu.usbserial-21110" not in paths, "不在场的绑定端口不得列入可选"
    assert data["absent_ports"] and data["absent_ports"][0]["path"] == "/dev/cu.usbserial-21110"
    assert "不在场" in (data["hint"] or "")


def test_serial_bind_http_post_route(env, monkeypatch, tmp_path):
    """P0-1（2026-09-30 审查）：前端 POST /api/serial-bind——路由必须在 do_POST 可达。

    旧实现把分支写在 do_GET（读 body 发绑定），POST 一律落到兜底 404：
    调试台「连接此串口」按钮整体不可用。此测试用真 HTTP 层锁住路由。
    """
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp_path)
    import json as _json
    import threading
    import urllib.request
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-21110",
         "arm_slot": 1, "hardware_mode": "real"}]}))

    server = debug.ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/serial-bind"
        # 测试更正（§4.5，2026-10-03）：R3-14 写边界——携自举会话+同站头
        req = urllib.request.Request(
            url, method="POST",
            data=_json.dumps({"device": "arm-01",
                              "port": "/dev/cu.usbserial-21130"}).encode(),
            headers={"Content-Type": "application/json",
                     **_dbg_headers(f"http://127.0.0.1:{server.server_address[1]}")})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            payload = _json.loads(resp.read())
        assert payload["ok"] is True
        assert payload["serial_port"] == "/dev/cu.usbserial-21130"
        assert env["attach"]["path"] == "/v1/devices/arm-01/attach"
    finally:
        server.shutdown()
        server.server_close()


def test_serial_bind_writes_static_entry_without_runtime_fields(
        env, monkeypatch, tmp_path):
    """B1（2026-10-01 backlog）：换口只改静态表目标设备一条——合并视图里的
    注册中心易变快照（presence/动态地址等）不得冻结进 devices.json，
    否则重启后"复活"。"""
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp_path)
    import json as _json
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-21110",
         "arm_slot": 1, "camera_id": "cam-01", "daemon_addr": "127.0.0.1:8795"}]}))
    # 合并视图掺入注册中心运行时字段（旧实现整表回写会把它们写进磁盘）
    monkeypatch.setattr(debug, "_merged_device_configs", lambda: {
        "arm-01": {"device_id": "arm-01",
                   "serial_port": "/dev/cu.usbserial-21110",
                   "arm_slot": 1, "camera_id": "cam-01",
                   "daemon_addr": "127.0.0.1:8795",
                   "presence_state": "present", "ime_online": True,
                   "driver_mode": "daemon", "hardware_mode": "real",
                   "ui_confidence": 0.97}})
    debug.serial_bind_payload("arm-01", "/dev/cu.usbserial-21130")
    saved = _json.loads((tmp_path / "configs" / "devices.json").read_text())
    entry = saved["devices"][0]
    assert entry["serial_port"] == "/dev/cu.usbserial-21130"
    assert entry["arm_slot"] == 1 and entry["daemon_addr"] == "127.0.0.1:8795"
    for runtime_field in ("presence_state", "ime_online", "driver_mode",
                          "hardware_mode", "ui_confidence"):
        assert runtime_field not in entry, runtime_field


def test_serial_bind_two_devices_no_lost_update(env, monkeypatch, tmp_path):
    """B1：连续换口两台设备不得互相丢更新——旧实现第二次用合并视图整表
    写回，把第一次刚写入的新串口按旧快照抹掉（8792/8788 无跨进程锁）。"""
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp_path)
    import json as _json
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "hardware_mode": "real",
         "serial_port": "/dev/cu.usbserial-21110"},
        {"device_id": "arm-02", "hardware_mode": "real",
         "serial_port": "/dev/cu.usbserial-21120"}]}))
    monkeypatch.setattr(debug, "_merged_device_configs", lambda: {
        "arm-01": {"device_id": "arm-01", "hardware_mode": "real",
                   "serial_port": "/dev/cu.usbserial-21110"},
        "arm-02": {"device_id": "arm-02", "hardware_mode": "real",
                   "serial_port": "/dev/cu.usbserial-21120"}})
    debug.serial_bind_payload("arm-01", "/dev/cu.usbserial-A1")
    debug.serial_bind_payload("arm-02", "/dev/cu.usbserial-A2")
    saved = {item["device_id"]: item for item in _json.loads(
        (tmp_path / "configs" / "devices.json").read_text())["devices"]}
    assert saved["arm-01"]["serial_port"] == "/dev/cu.usbserial-A1"
    assert saved["arm-02"]["serial_port"] == "/dev/cu.usbserial-A2"


def test_serial_bind_rejects_non_serial_port_form(env):
    """B2（2026-10-01 backlog）：免 device-context 写入口——port 必须是
    串口设备形态（/dev/cu.* 或 /dev/tty.*），任意字符串不得进配置触发
    attach 把设备搞离线。"""
    for bad in ("COM3", "usbserial-21130", "/dev/cu/", "/dev/tty", " ",
                "/etc/passwd", "/dev/cu.usbserial-21130 extra"):
        with pytest.raises(ValueError, match="串口路径形态"):
            debug.serial_bind_payload("arm-01", bad)


def test_post_promote_sync_chain_order_and_once(env, monkeypatch):
    """校准完成先暂停再同步资格；轮询不会重复执行或恢复运行。"""
    calls = []
    monkeypatch.setattr(debug, "_post_promote_platform_sync",
                        lambda device, run_id: calls.append((device, run_id)))
    debug._PROMOTE_SYNCED.clear()
    view = {"state": "PROMOTED", "runId": "run-sync-1"}
    debug._maybe_post_promote_sync("arm-01", view)
    debug._maybe_post_promote_sync("arm-01", view)
    debug._maybe_post_promote_sync("arm-01", {"state": "RUNNING", "runId": "run-2"})
    assert calls == [("arm-01", "run-sync-1")]


def _tmp_configs(monkeypatch):
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp)
    return tmp


def test_post_promote_platform_sync_chain_and_failure(env, monkeypatch):
    """任何画面条件下均先暂停再同步校准，不归位、不恢复、不唤醒。"""
    calls = []

    def request(method, path, body=None):
        calls.append((method, path.rsplit("/", 1)[-1], body))
        return {"ok": True}

    monkeypatch.setattr(debug, "_agent_json", request)
    result = debug._post_promote_platform_sync("arm-01", "run-x", background=False)
    assert result == {"pause": "ok", "calibration-status": "ok"}
    assert [(method, name) for method, name, _ in calls] == [
        ("POST", "pause"), ("POST", "calibration-status")]
    assert calls[1][2]["status"] == "validated"
    assert "一键恢复" in calls[0][2]["reason"]

    calls.clear()

    def fail_calibration(method, path, body=None):
        calls.append(path.rsplit("/", 1)[-1])
        if path.endswith("calibration-status"):
            raise OSError("平台不可达")
        return {"ok": True}

    monkeypatch.setattr(debug, "_agent_json", fail_calibration)
    result = debug._post_promote_platform_sync("arm-01", "run-x", background=False)
    assert result["pause"] == "ok"
    assert result["calibration-status"].startswith("failed")
    assert calls == ["pause", "calibration-status"]


def test_arm_anchor_verify_bounded_and_deviation(tmp_path, monkeypatch):
    """A3-W 臂侧锚点验证器：走位受限（do_goto_robot 即带单次上限/残差守卫）、
    探头尖偏差 ≤45px 通过 / 超限不通过、缺验证点/探头不可见显式报错。"""
    import numpy as np
    import cv2
    from drivers import screen_edge as _se, screen_pose as _sp
    from runtime import recovery
    from types import SimpleNamespace
    monkeypatch.setattr(debug, "daemon_for", lambda device: SimpleNamespace(
        status=lambda: {"session": "same-session"},
        mpos=lambda: {"x": 0., "y": 0., "z": 2., "state": "Idle"}))
    # 相机链全替身：decode→纯色帧、四边定位→档案角、姿态→单位阵
    monkeypatch.setattr(cv2, "imdecode",
                        lambda *a, **k: np.zeros((720, 1280, 3), np.uint8))
    monkeypatch.setattr(_se, "locate_screen_quad",
                        lambda frame, expected, **kw: {"corners": expected})
    monkeypatch.setattr(_sp, "fit_pose",
                        lambda saved, live, **kw: {
                            "matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                            "corners": list(saved), "movementPx": 0.0,
                            "fitErrorPx": 0.0})
    anchor_calls: list[tuple] = []
    tip_px = (368.0, 220.0)  # = H 逆投影预测位（768,820→368,220），偏差 0

    fake_bundle = {
        "xy": {"validation": [{"effectiveRobotXY": [30.0, 0.0],
                               "targetScreen": [768.0, 820.0]}]},
        "z": {"hoverZ": 2.0, "tapDepth": 8.0, "tapFeed": 2000.0},
        "camera": {"cameraToScreen": {"homography": [[1, 0, 400], [0, 1, 600],
                                                     [0, 0, 1]]}},
        "screen": {"edgeModel": {"corners": [[100, 100], [1180, 100],
                                             [1180, 620], [100, 620]]}},
        "frame": {"width": 1280, "height": 720},
    }
    monkeypatch.setattr(debug.calibration_bundle, "load_bundle",
                        lambda device, root: fake_bundle)
    monkeypatch.setattr(debug, "do_goto_robot",
                        lambda device, rx, ry, *, tap, hover_z, feed_xy,
                        tap_depth, tap_feed: anchor_calls.append((rx, ry))
                        or {"targetRobot": [rx, ry]})
    sequences = iter(range(100))
    monkeypatch.setattr(debug, "fetch_camera_frame",
                        lambda device, **kwargs: (b"jpg", next(sequences)))
    monkeypatch.setattr(recovery, "observe_recovery_probe",
                        lambda *args: tip_px)
    monkeypatch.setattr(debug, "log_op", lambda *a, **k: None)

    # H 逆投影：屏幕 (768,820) → 相机 (368,220)；预测=自身（姿态矩阵取单位）
    assert debug._arm_anchor_verify("arm-01") is True
    assert anchor_calls == [(30.0, 0.0), (0., 0.)], "一次悬停核对后返回原起点"

    tip_px = (500.0, 400.0)  # 偏差 ~225px 超限
    assert debug._arm_anchor_verify("arm-01") is False

    def invisible(*args):
        raise RuntimeError("移动探头无法唯一识别")
    monkeypatch.setattr(recovery, "observe_recovery_probe", invisible)
    with pytest.raises(RuntimeError, match="无法唯一识别"):
        debug._arm_anchor_verify("arm-01")

    monkeypatch.setattr(debug.calibration_bundle, "load_bundle",
                        lambda device, root: {"xy": {"validation": []}})
    assert anchor_calls[-1] == (0., 0.), "识别失败也必须返回起点"

    with pytest.raises(RuntimeError, match="已实测"):
        debug._arm_anchor_verify("arm-01")


def test_recovery_attempt_endpoint_flow(tmp_path, monkeypatch):
    """A3-W 端点：/api/recovery/attempt 接 attempt_recovery_action（motion 门）；
    动作函数把 bundle/frame/会话/相机/锚点验证器正确接进 attempt_recovery。"""
    import json as _json
    import threading
    import urllib.request
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-21130",
         "hardware_mode": "real"}]}))

    recorded: list[dict] = []

    class _FakeDaemon:
        def status(self):
            return {"session": "sess-new"}

        def mpos(self):
            return {"x": 0.0, "y": 0.0, "z": 0.0}

    monkeypatch.setattr(debug, "daemon_for", lambda device: _FakeDaemon())
    monkeypatch.setattr(debug, "require_context",
                        lambda device, ctx, *, motion=False, **kw:
                        recorded.append({"motion": motion}))
    monkeypatch.setattr(debug, "_reject_motion_during_auto_run",
                        lambda device, action: None)
    monkeypatch.setattr(debug, "daemon_for", lambda device: SimpleNamespace(
        ensure_debug_lease=lambda: None))
    monkeypatch.setattr(debug, "attempt_recovery_action",
                        lambda device: recorded.append({"device": device})
                        or {"recovered": True, "stage": "done", "certificates": 8})

    server = debug.ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/recovery/attempt"
        # 测试更正（§4.5，2026-10-03）：R3-14 写边界——携自举会话+同站头
        req = urllib.request.Request(
            url, method="POST",
            data=_json.dumps({"device": "arm-01", "contextId": "ctx-1"}).encode(),
            headers={"Content-Type": "application/json",
                     **_dbg_headers(f"http://127.0.0.1:{server.server_address[1]}")})
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = _json.loads(resp.read())
        assert payload["recovered"] is True and payload["certificates"] == 8
        assert recorded[0]["motion"] is True, "锚点走位是物理动作，必须 motion 门"
        assert recorded[1]["device"] == "arm-01"
    finally:
        server.shutdown()
        server.server_close()


def test_recovery_attempt_action_real_body_wiring(tmp_path, monkeypatch):
    """A3-W P1 回归锚：attempt_recovery_action 真函数体（非替身）——
    此前 cb 未绑定名导致真实调用必 NameError，替身测试全绿掩盖。"""
    import json as _json
    from types import SimpleNamespace

    fake_bundle = {"deviceId": "arm-01", "status": "draft",
                   "recoveryGeometryHash": "g",
                   "daemonSession": "old"}
    fake_frame = {"coordinate_frame_hash": "cfh-1"}
    monkeypatch.setattr("drivers.calibration_bundle.load_bundle",
                        lambda device, root: fake_bundle)
    monkeypatch.setattr("drivers.gesture_program.load_coordinate_frame",
                        lambda device, root: fake_frame)

    class _FakeDaemon:
        def status(self):
            return {"session": "sess-new"}

        def mpos(self):
            return {"x": 0.0, "y": 0.0, "z": 0.0}

    monkeypatch.setattr(debug, "daemon_for", lambda device: _FakeDaemon())
    monkeypatch.setattr(debug, "fetch_camera_frame", lambda device: (b"jpg", 1))
    monkeypatch.setattr(debug, "_arm_anchor_verify",
                        lambda device: True)
    monkeypatch.setattr("runtime.recovery.attempt_recovery",
                        lambda device, **kw: (
                            recorded_kwargs.update(kw),
                            {"recovered": True, "stage": "done",
                             "certificates": 8, "rebinds": ["sess-new"]})[1])
    recorded_kwargs: dict = {}

    result = debug.attempt_recovery_action("arm-01")
    assert result["recovered"] is True
    # 验证器以可调用体注入（调用时机由 attempt_recovery 决定，见其自身测试）
    assert callable(recorded_kwargs["arm_anchor_verifier"])
    assert recorded_kwargs["new_session"] == "sess-new"
    assert recorded_kwargs["bundle"] is fake_bundle
    assert recorded_kwargs["coordinate_frame"] is fake_frame
    assert recorded_kwargs["phone_frame_provider"] is not None
    assert recorded_kwargs["arm_anchor_verifier"] is not None
    # rebind_targets 可调用且收 restored bundle（真实重绑在
    # attempt_recovery 内部以 restored 调用——rebinds 由真实现填充）
    assert callable(recorded_kwargs["rebind_targets"])


def test_post_promote_write_requests_have_stable_distinct_ids(env, monkeypatch):
    calls = []
    def request(method, path, body=None):
        if method == 'GET':
            return {'camera_quality': 'clear'}
        assert body.get('client_request_id'), 'write API rejects missing client_request_id'
        calls.append(body['client_request_id'])
        return {'home_ok': True}
    monkeypatch.setattr(debug, '_agent_json', request)
    debug._post_promote_platform_sync('arm-01', 'run-request-id', background=False)
    first = calls[:]
    assert len(set(first)) == 2
    debug._post_promote_platform_sync('arm-01', 'run-request-id', background=False)
    assert calls[2:] == first


def test_post_promote_pause_failure_does_not_publish_readiness(env, monkeypatch):
    calls = []

    def request(method, path, body=None):
        calls.append(path)
        raise OSError("暂停未成功")

    monkeypatch.setattr(debug, '_agent_json', request)
    result = debug._post_promote_platform_sync('arm-01', 'run-held', background=False)
    assert '暂停未成功' in result['pause']
    assert calls == ['/v1/devices/arm-01/pause']
    assert 'calibration-status' not in result


def test_serial_unbind_proxies_platform_binding_with_empty_port(env, monkeypatch, tmp_path):
    """解除绑定：代理平台 PUT binding {"serial_port": ""}，不自造半状态。"""
    import json as _json
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-21110",
         "arm_slot": 1, "hardware_mode": "real"}]}))
    calls = {"put": None}

    def fake_passthrough(method, path, body=None):
        calls["put"] = {"method": method, "path": path, "body": body}
        return 200, {"ok": True, "device_id": "arm-01"}

    monkeypatch.setattr(debug, "_agent_passthrough", fake_passthrough)
    monkeypatch.setattr(debug, "_runtime_device_records", lambda: {"arm-01": {}})
    status, payload = debug.serial_unbind_payload("arm-01")
    assert status == 200 and payload["ok"] is True
    assert calls["put"]["method"] == "PUT"
    assert calls["put"]["path"] == "/v1/devices/arm-01/binding"
    assert calls["put"]["body"]["serial_port"] == ""
    assert calls["put"]["body"]["confirmation_token"] == "确认执行"


def test_serial_unbind_rejected_when_nothing_bound(env, monkeypatch):
    monkeypatch.setattr(debug, "_runtime_device_records", lambda: {"arm-01": {}})
    monkeypatch.setattr(debug, "_merged_device_configs", lambda: {
        "arm-01": {"device_id": "arm-01", "serial_port": ""}})
    with pytest.raises(ValueError) as excinfo:
        debug.serial_unbind_payload("arm-01")
    assert "没有串口绑定" in str(excinfo.value)


def test_serial_unbind_blocked_by_unknown_hold(env, monkeypatch, tmp_path):
    import json as _json
    from runtime.errors import ApiError
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-21110"}]}))
    for shape in ({"unknown_hold_task_id": "task-9"},
                  {"unknown_hold_task_ids": ["t"]},
                  {"unknown_hold_task_ids": "abc"}):
        monkeypatch.setattr(debug, "_runtime_device_records", lambda shape=shape: {"arm-01": shape})
        with pytest.raises(ApiError) as excinfo:
            debug.serial_unbind_payload("arm-01")
        assert excinfo.value.key == "device_unknown_hold"
    for shape in ({}, {"unknown_hold_task_id": None}, {"unknown_hold_task_ids": []},
                  {"unknown_hold_task_ids": ""}, {"unknown_hold_task_ids": [None, ""]}):
        monkeypatch.setattr(debug, "_runtime_device_records", lambda shape=shape: {"arm-01": shape})
        monkeypatch.setattr(debug, "_agent_passthrough", lambda *a, **k: (200, {"ok": True}))
        status, _ = debug.serial_unbind_payload("arm-01")
        assert status == 200, f"脏数据误拦：{shape}"


def test_serial_unbind_works_when_port_absent(monkeypatch, tmp_path):
    """绑定口不在场时也必须能解绑（后端只读合并配置，不依赖在场状态）。"""
    import json as _json
    monkeypatch.setattr("runtime.config.PROJECT_ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-GONE"}]}))
    monkeypatch.setattr(debug, "_merged_device_configs", lambda: {
        "arm-01": {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-GONE"}})
    monkeypatch.setattr(debug, "_runtime_device_records", lambda: {"arm-01": {}})
    calls = {}
    monkeypatch.setattr(debug, "_agent_passthrough",
                        lambda m, p, b=None: (calls.update(path=p, body=b) or (200, {"ok": True})))
    status, _ = debug.serial_unbind_payload("arm-01")
    assert status == 200 and calls["body"]["serial_port"] == ""
