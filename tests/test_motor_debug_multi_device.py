from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest

from scripts import motor_debug_server as debug

REAL_CAMERA_STATUS = debug.camera_status



# R3-14（§4.5 订正 2026-10-03）：调试台边界要求浏览器对等头；测试请求统一
# 经 _dbg_headers 注入（会话 Cookie + X-Debug-CSRF + 精确 Origin）。
_DBG_SESSION = {"cookie": None, "csrf": None}


def _dbg_bootstrap(base):
    if _DBG_SESSION["csrf"] is not None:
        return
    try:
        req = urllib.request.Request(base + "/api/debug-session")
        with urllib.request.urlopen(req, timeout=5) as resp:
            _DBG_SESSION["csrf"] = json.loads(resp.read() or b"{}").get("csrf_token")
            set_cookie = resp.headers.get("Set-Cookie") or ""
            if "=" in set_cookie:
                _DBG_SESSION["cookie"] = set_cookie.split(";")[0]
    except Exception:
        pass


def _dbg_headers(base):
    _dbg_bootstrap(base)
    headers = {"Origin": base, "Sec-Fetch-Site": "same-origin"}
    if _DBG_SESSION["csrf"]:
        headers["X-Debug-CSRF"] = _DBG_SESSION["csrf"]
    if _DBG_SESSION["cookie"]:
        headers["Cookie"] = _DBG_SESSION["cookie"]
    return headers

class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body
        self.headers = FakeHeaders(headers or {})

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeDaemon:
    def __init__(self, device_id: str, port: int, session: str | None):
        self.device_id = device_id
        self.daemon_addr = ("127.0.0.1", port)
        self._lock = threading.RLock()
        self.session = session
        self.lease_id = None
        self.lease_expires = 0.0
        self.commands: list[str] = []
        self._mpos = {"x": 1.0, "y": 2.0, "z": 3.0, "state": "Idle"}
        self.motion_active = False
        self.motion_revision = 0

    def motion_scope(self):
        return nullcontext()

    def ensure_debug_lease(self):
        with self._lock:
            self.lease_id = "debug"

    def status(self):
        return {
            "connected": True,
            "session": self.session,
            "device_id": self.device_id,
            "daemon_addr": f"127.0.0.1:{self.daemon_addr[1]}",
            "device_present": True,
            "estop_locked": False,
            "lease": None,
            "last_error": None,
        }

    def mpos(self):
        return dict(self._mpos)

    def cmd(self, line: str, **_kwargs):
        with self._lock:
            self.commands.append(line)
            parts = line.split()
            if parts and parts[0] == "Z":
                self._mpos["z"] += float(parts[1])
            elif parts and parts[0] == "XY":
                self._mpos["x"] += float(parts[1])
                self._mpos["y"] += float(parts[2])
            if line == "RELEASE":
                return '{"released": true}'
            return json.dumps(self.mpos())


@pytest.fixture()
def two_devices(monkeypatch, tmp_path):
    configs = {
        "arm-01": {
            "device_id": "arm-01", "arm_slot": 1, "camera_id": "cam-01",
            "camera_mode": "multi_channel",
            "camera_frame_url": "http://camera/cameras/cam-01/latest.jpg",
            "camera_status_url": "http://camera/cameras/cam-01/status",
            "daemon_addr": "127.0.0.1:8795",
            "expected_frame_width": 1280, "expected_frame_height": 720,
            "expected_frame_fps": 10,
        },
        "arm-02": {
            "device_id": "arm-02", "arm_slot": 2, "camera_id": "cam-02",
            "camera_mode": "multi_channel",
            "camera_frame_url": "http://camera/cameras/cam-02/latest.jpg",
            "camera_status_url": "http://camera/cameras/cam-02/status",
            "daemon_addr": "127.0.0.1:8796",
            "expected_frame_width": 1280, "expected_frame_height": 720,
            "expected_frame_fps": 10,
        },
    }
    daemons = {
        "arm-01": FakeDaemon("arm-01", 8795, "session-01"),
        "arm-02": FakeDaemon("arm-02", 8796, "session-02"),
    }
    monkeypatch.setattr(debug, "DEVICE_CONFIGS", configs)
    monkeypatch.setattr(debug, "DAEMONS", daemons)
    monkeypatch.setattr(debug, "CONTEXTS", {})
    monkeypatch.setattr(debug, "WIZARDS", {})
    monkeypatch.setattr(debug, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "OPS_PATH", tmp_path / "ops.jsonl")
    monkeypatch.setattr(debug, "_STATUS_CACHES", {})
    monkeypatch.setattr(debug, "_STATUS_LOCKS", {})
    monkeypatch.setenv("ARMFLEET_DB_PATH", str(tmp_path / "armfleet.db"))

    def camera_status(device: str):
        cfg = configs[device]
        return {
            "camera_id": cfg["camera_id"],
            "device_id": device,
            "device_unique_id": "uvc-" + device[-2:],
            "binding_revision": 5,
            "status": "streaming",
            "has_frame": True,
            "frame_seq": 10,
            "seq": 10,
            "width": 1280,
            "height": 720,
        }

    monkeypatch.setattr(debug, "camera_status", camera_status)
    return configs, daemons


def test_daemon_and_urls_are_resolved_per_device(two_devices):
    configs, daemons = two_devices
    assert debug.daemon_for("arm-01") is daemons["arm-01"]
    assert debug.daemon_for("arm-02") is daemons["arm-02"]
    assert debug.daemon_for("arm-01") is not debug.daemon_for("arm-02")
    assert debug.camera_frame_url_for("arm-01").endswith("/cam-01/latest.jpg")
    assert debug.camera_frame_url_for("arm-02").endswith("/cam-02/latest.jpg")
    assert debug._daemon_address(configs["arm-01"]) == ("127.0.0.1", 8795)
    assert debug._daemon_address(configs["arm-02"]) == ("127.0.0.1", 8796)


def test_calibration_binding_facts_are_complete_and_per_device(two_devices):
    first = debug.calibration_binding_facts("arm-01")
    second = debug.calibration_binding_facts("arm-02")
    assert first == {
        "cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-01",
        "bindingRevision": 5, "daemonAddr": "127.0.0.1:8795",
        "frameWidth": 1280, "frameHeight": 720,
    }
    assert second["cameraId"] == "cam-02"
    assert second["daemonAddr"] == "127.0.0.1:8796"


def test_config_validation_rejects_cross_wiring():
    base = {
        "device_id": "arm-01", "camera_id": "cam-01", "driver_mode": "daemon",
        "binding_mode": "real", "camera_mode": "multi_channel",
        "camera_frame_url": "http://127.0.0.1:8790/cameras/cam-01/latest.jpg",
        "camera_status_url": "http://127.0.0.1:8790/cameras/cam-01/status",
        "expected_frame_width": 1280, "expected_frame_height": 720,
        "expected_frame_fps": 10, "daemon_addr": "127.0.0.1:8795",
    }
    duplicate_camera = {**base, "device_id": "arm-02", "daemon_addr": "127.0.0.1:8796"}
    with pytest.raises(RuntimeError, match="camera_id"):
        debug._validate_device_configs([base, duplicate_camera])
    duplicate_daemon = {
        **base, "device_id": "arm-02", "camera_id": "cam-02",
        "camera_frame_url": "http://127.0.0.1:8790/cameras/cam-02/latest.jpg",
        "camera_status_url": "http://127.0.0.1:8790/cameras/cam-02/status",
    }
    with pytest.raises(RuntimeError, match="daemon_addr"):
        debug._validate_device_configs([base, duplicate_daemon])
    with pytest.raises(RuntimeError, match="仿真设备"):
        debug._validate_device_configs([{
            "device_id": "arm-02", "camera_id": "cam-02",
            "daemon_addr": "127.0.0.1:8796",
        }])


def test_device_contexts_are_isolated_and_reject_cross_device_use(two_devices):
    first = debug.create_device_context("arm-01")
    second = debug.create_device_context("arm-02")
    assert first["cameraId"] == "cam-01"
    assert first["daemonAddr"] == "127.0.0.1:8795"
    assert first["daemonSession"] == "session-01"
    assert second["cameraId"] == "cam-02"
    assert second["daemonAddr"] == "127.0.0.1:8796"
    assert first["contextId"] != second["contextId"]

    with pytest.raises(RuntimeError, match="device_context_mismatch"):
        debug.require_context("arm-02", first["contextId"], motion=True)


def test_context_rejects_binding_revision_and_daemon_session_changes(two_devices, monkeypatch):
    _configs, daemons = two_devices
    context = debug.create_device_context("arm-01")

    original = debug.camera_status
    monkeypatch.setattr(debug, "camera_status", lambda device: {
        **original(device), "binding_revision": 6,
    })
    with pytest.raises(RuntimeError, match="camera_binding_revision_changed"):
        debug.require_context("arm-01", context["contextId"])

    monkeypatch.setattr(debug, "camera_status", original)
    daemons["arm-01"].session = "session-new"
    with pytest.raises(RuntimeError, match="daemon_session_changed"):
        debug.require_context("arm-01", context["contextId"])


def test_context_expiry_and_close_release_only_bound_device(two_devices):
    _configs, daemons = two_devices
    first = debug.create_device_context("arm-01")
    second = debug.create_device_context("arm-02")
    daemons["arm-01"].lease_id = "lease-01"
    daemons["arm-02"].lease_id = "lease-02"

    debug.close_device_context(first["contextId"])
    assert daemons["arm-01"].commands == ["RELEASE"]
    assert daemons["arm-02"].commands == []
    assert second["contextId"] in debug.CONTEXTS

    debug.CONTEXTS[second["contextId"]]["expires_at"] = time.time() - 1
    with pytest.raises(RuntimeError, match="device_context_expired"):
        debug.require_context("arm-02", second["contextId"])
    assert daemons["arm-02"].commands == ["RELEASE"]


def test_context_close_does_not_release_lease_while_same_device_context_is_live(two_devices):
    _configs, daemons = two_devices
    first = debug.create_device_context("arm-01")
    second = debug.create_device_context("arm-01")
    daemons["arm-01"].lease_id = "lease-01"

    debug.close_device_context(first["contextId"])
    assert daemons["arm-01"].commands == []
    assert second["contextId"] in debug.CONTEXTS

    debug.close_device_context(second["contextId"])
    assert daemons["arm-01"].commands == ["RELEASE"]


def test_context_registration_is_serialized_with_lease_release(two_devices):
    _configs, daemons = two_devices
    first = debug.create_device_context("arm-01")
    daemon = daemons["arm-01"]
    daemon.lease_id = "lease-01"
    release_started = threading.Event()
    allow_release = threading.Event()
    original_cmd = daemon.cmd

    def blocked_cmd(line: str, **kwargs):
        if line == "RELEASE":
            release_started.set()
            assert allow_release.wait(3)
        return original_cmd(line, **kwargs)

    daemon.cmd = blocked_cmd
    closer = threading.Thread(
        target=debug.close_device_context, args=(first["contextId"],), daemon=True)
    closer.start()
    assert release_started.wait(1)

    created = {}

    def create_and_claim():
        created.update(debug.create_device_context("arm-01"))
        daemon.ensure_debug_lease()

    creator = threading.Thread(target=create_and_claim, daemon=True)
    creator.start()
    time.sleep(0.05)
    assert creator.is_alive()

    allow_release.set()
    closer.join(timeout=2)
    creator.join(timeout=2)
    assert not closer.is_alive() and not creator.is_alive()
    assert created["contextId"] in debug.CONTEXTS
    assert daemon.lease_id == "debug"


@pytest.mark.parametrize("runtime_status", ["paused", "quarantined", "maintenance"])
def test_new_runtime_restrictions_block_old_context_but_not_emergency(
        two_devices, monkeypatch, runtime_status):
    _configs, daemons = two_devices
    monkeypatch.setattr(debug, "runtime_device_record", lambda _device: {
        "device_id": "arm-01", "presence_state": "present",
        "status": None,
    })
    context = debug.create_device_context("arm-01")
    monkeypatch.setattr(debug, "runtime_device_record", lambda _device: {
        "device_id": "arm-01", "presence_state": "present",
        "status": runtime_status,
    })

    with pytest.raises(RuntimeError, match=f"device_{runtime_status}"):
        debug.require_context("arm-01", context["contextId"], motion=True)
    assert debug.require_context(
        "arm-01", context["contextId"], emergency=True)["device_id"] == "arm-01"


def test_motion_requires_physical_connection_and_device_presence(two_devices, monkeypatch):
    _configs, daemons = two_devices
    context = debug.create_device_context("arm-01")
    monkeypatch.setattr(daemons["arm-01"], "status", lambda: {
        "connected": True, "session": "session-01", "device_id": "arm-01",
        "daemon_addr": "127.0.0.1:8795", "device_present": False,
    })

    with pytest.raises(RuntimeError, match="daemon_unreachable"):
        debug.require_context("arm-01", context["contextId"], motion=True)


def test_http_status_cache_path_uses_only_local_context_validation(two_devices):
    _configs, daemons = two_devices
    context = {
        "context_id": "ctx-status", "device_id": "arm-01",
        "daemon_addr": "127.0.0.1:8795",
        "expires_at": time.time() + 60,
    }
    debug.CONTEXTS[context["context_id"]] = context
    cached = {
        "daemon": {"connected": True, "session": "session-01"},
        "mpos": {"x": 1, "y": 2, "z": 3}, "usable": True,
        "runtimeCalibration": {"usable": True},
    }
    debug._STATUS_CACHES["arm-01"] = cached
    status_lock = debug._STATUS_LOCKS.setdefault("arm-01", threading.Lock())
    status_lock.acquire()
    server = debug.ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(daemons["arm-01"], "status",
                          lambda: pytest.fail("slow daemon validation"))
            patch.setattr(debug, "camera_status",
                          lambda _device: pytest.fail("slow camera validation"))
            with urllib.request.urlopen(
                    f"{base}/api/status?device=arm-01&contextId=ctx-status",
                    timeout=2) as response:
                payload = json.loads(response.read())
        assert payload["status_stale"] is True
        assert payload["mpos"] is None
    finally:
        status_lock.release()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_rejects_cross_device_context(two_devices):
    _configs, daemons = two_devices
    server = debug.ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: dict):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **_dbg_headers(base)},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    try:
        status, first = post("/api/device-context", {"device": "arm-01"})
        assert status == 200
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            post("/api/jog", {
                "device": "arm-02", "contextId": first["contextId"],
                "axis": "x", "delta": 1, "feed": 500,
            })
        error = json.loads(excinfo.value.read())
        assert "device_context_mismatch" in str(error)
        assert daemons["arm-02"].commands == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_fetch_camera_frame_checks_camera_and_device_headers(two_devices, monkeypatch):
    # Auto_CS#6：fetch_camera_frame 经 HttpFrameSource 单实现——正式链路要求
    # v2 全套响应头（身份/绑定代际/序号/分辨率/接收时刻），与观测链同一契约。
    image = b"jpeg-bytes"
    now_iso = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    good_headers = {
        "X-Camera-Id": "cam-01",
        "X-Device-Id": "arm-01",
        "X-Camera-Unique-Id": "uvc-01",
        "X-Binding-Revision": "7",
        "X-Frame-Seq": "12",
        "X-Frame-Width": "64",
        "X-Frame-Height": "48",
        "X-Received-At": now_iso,
        "X-Captured-At": now_iso,
    }
    monkeypatch.setattr(
        debug.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(image, good_headers),
    )
    assert debug.fetch_camera_frame("arm-01") == (image, 12)

    bad_headers = {**good_headers, "X-Device-Id": "arm-02"}
    monkeypatch.setattr(
        debug.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(image, bad_headers),
    )
    with pytest.raises(RuntimeError, match="设备 arm-01 取流失败.*身份不匹配"):
        debug.fetch_camera_frame("arm-01")


def test_camera_status_checks_returned_identity(two_devices, monkeypatch):
    payload = {
        "camera_id": "cam-01", "device_id": "arm-01", "frame_seq": 8,
        "status": "streaming", "has_frame": True,
    }
    monkeypatch.setattr(
        debug.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(json.dumps(payload).encode()),
    )
    result = REAL_CAMERA_STATUS("arm-01")
    assert result["seq"] == 8
    assert result["camera_id"] == "cam-01"

    payload["camera_id"] = "cam-02"
    result = REAL_CAMERA_STATUS("arm-01")
    assert result["has_frame"] is False
    assert "身份不匹配" in result["error"]


def test_debug_list_merges_dynamic_registry_and_removed_device_is_motion_blocked(two_devices, monkeypatch, tmp_path):
    """调试台必须即时看到注册中心新增设备，移除后旧上下文不能继续运动。"""
    import sqlite3

    db = tmp_path / "armfleet.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE devices (device_id TEXT PRIMARY KEY, json TEXT NOT NULL)")
    conn.execute("INSERT INTO devices(device_id, json) VALUES (?, ?)", (
        "arm-hot-01", json.dumps({
            "device_id": "arm-hot-01", "camera_id": "cam-hot-01", "daemon_addr": "127.0.0.1:8895",
            "driver_mode": "daemon", "presence_state": "removed", "status": "offline",
            "device_mode": "unmanaged", "calibration_status": "due", "enforce_input_gate": True,
        })))
    conn.commit(); conn.close()
    monkeypatch.setenv("ARMFLEET_DB_PATH", str(db))
    items = debug.list_arm_devices()
    hot = next(item for item in items if item["deviceId"] == "arm-hot-01")
    assert hot["dynamic"] is True
    assert hot["presenceState"] == "removed"
    assert hot["calibrationStatus"] == "due"

    context = {"context_id": "ctx-removed", "device_id": "arm-hot-01",
               "checks": [{"code": "device_removed", "message": "已移除"}],
               "expires_at": time.time() + 60}
    debug.CONTEXTS["ctx-removed"] = context
    with pytest.raises(RuntimeError, match="device_removed"):
        debug.require_context("arm-hot-01", "ctx-removed", motion=True)


def test_require_context_ignores_stale_removed_gate_when_daemon_is_back(two_devices, monkeypatch):
    """daemon 已确认在位/连接时，允许跨过一个心跳周期的旧 removed 镜像。"""
    monkeypatch.setattr(debug, "runtime_device_record", lambda _device: {
        "device_id": "arm-01",
        "presence_state": "removed",
        "status": "offline",
    })
    context = debug.create_device_context("arm-01")
    assert any(item["code"] == "device_removed" for item in context["checks"])

    result = debug.require_context("arm-01", context["contextId"], motion=True)
    assert result["device_id"] == "arm-01"
    assert result["runtime_status"] == "recovering"
    assert result["presence_state"] == "present"


def test_wizard_http_auto_actions_dispatch_without_scope_error(two_devices, tmp_path):
    """auto-state/auto-start 拒绝路径：action 分发不得引用越界变量（连带审查修复）。"""
    configs, daemons = two_devices
    wizard = debug.get_wizard("arm-01")
    # auto-state：直接返回 runner 视图（此前误用未定义的 device 变量 → NameError）
    view = debug.wizard_http(wizard, "auto-state", {})
    assert view["state"] == "IDLE" and view["activeRunId"] is None
    # auto-start：前置未满足时给出中文 WizardError（不是内部错误）
    import pytest as _pytest
    from drivers.calibration_wizard import WizardError as _WizardError
    with _pytest.raises(_WizardError, match="zSafe|SAFE PARK|A 阶段"):
        debug.wizard_http(wizard, "auto-start", {})


def test_http_ui_targets_lifecycle_and_isolation(two_devices):
    """/api/ui-targets 全链路：登记门禁（bundle ready）→ 草稿 → 确认 → 启用，
    目标操作不修改 calibration_bundle.json；跨设备上下文拒绝。"""
    from drivers import calibration_bundle as cb
    from tests.test_ui_targets import ready_bundle

    _configs, daemons = two_devices
    server = debug.ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: dict):
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **_dbg_headers(base)}, method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def get(path: str):
        with urllib.request.urlopen(base + path, timeout=3) as response:
            return response.status, json.loads(response.read())

    try:
        _status, ctx = post("/api/device-context", {"device": "arm-01"})
        context_id = ctx["contextId"]

        # 1) 无 ready 档案：登记被拒（calibration_not_ready → 400）
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            post("/api/ui-targets/draft", {"device": "arm-01", "contextId": context_id,
                                           "targetId": "back_button", "nx": 0.06, "ny": 0.08})
        error_message = str(json.loads(excinfo.value.read()).get("error", {}).get("message", ""))
        assert "校准" in error_message and "登记" in error_message

        # 2) 写入 ready 档案后：草稿 → 确认 → 启用 全链路；bundle 字节不变
        cb.save_bundle(ready_bundle(session=daemons["arm-01"].session), debug.DATA_DIR)
        bundle_path = cb.bundle_path("arm-01", debug.DATA_DIR)
        before = bundle_path.read_bytes()

        status, resp = post("/api/ui-targets/draft", {
            "device": "arm-01", "contextId": context_id,
            "targetId": "back_button", "nx": 0.06, "ny": 0.08})
        assert status == 200 and resp["entry"]["state"] == "draft"

        status, resp = post("/api/ui-targets/confirm", {
            "device": "arm-01", "contextId": context_id, "targetId": "back_button",
            "passed": True, "evidence": {"kind": "tap_test"}})
        assert resp["entry"]["state"] == "tested"

        status, resp = post("/api/ui-targets/enable", {
            "device": "arm-01", "contextId": context_id, "targetId": "back_button"})
        assert resp["entry"]["state"] == "enabled"
        assert bundle_path.read_bytes() == before, "目标登记/确认/启用不得修改基础校准档案"

        # 3) 列表视图：登记门禁打开、状态已启用
        status, view = get(f"/api/ui-targets?device=arm-01&contextId={context_id}")
        assert status == 200 and view["registrationOpen"] is True
        assert view["targets"]["back_button"]["derivedStatus"] == "已启用"
        assert view["targets"]["back_button"]["businessUsable"] is True

        # 4) 跨设备上下文拒绝（arm-02 用 arm-01 的 context）
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            post("/api/ui-targets/draft", {
                "device": "arm-02", "contextId": context_id,
                "targetId": "back_button", "nx": 0.06, "ny": 0.08})
        assert "device_context_mismatch" in str(json.loads(excinfo.value.read()))

        # 5) 停用后业务视图不可用
        _status, resp = post("/api/ui-targets/disable", {
            "device": "arm-01", "contextId": context_id, "targetId": "back_button"})
        assert resp["entry"]["state"] == "disabled"
        assert ui_targets_view_disabled(base, context_id)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def ui_targets_view_disabled(base: str, context_id: str) -> bool:
    with urllib.request.urlopen(
            f"{base}/api/ui-targets?device=arm-01&contextId={context_id}", timeout=3) as r:
        view = json.loads(r.read())
    entry = view["targets"]["back_button"]
    return entry["derivedStatus"] == "已停用" and entry["businessUsable"] is False


def test_http_park_moves_to_confirmed_safe_park(two_devices):
    """一键停靠：SAFE PARK 已确认且会话一致 → 真实走位（无 TAP）；
    会话变化 → 拒绝（旧绝对坐标不可信）。"""
    from drivers import calibration_bundle as cb

    _configs, daemons = two_devices
    server = debug.ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: dict):
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **_dbg_headers(base)}, method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    try:
        _status, ctx = post("/api/device-context", {"device": "arm-01"})
        context_id = ctx["contextId"]

        # 1) 停止位未保存 → 拒绝
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            post("/api/park", {"device": "arm-01", "contextId": context_id})
        assert "SAFE PARK" in str(json.loads(excinfo.value.read()))

        # 2) 档案内确认的停止位 + 会话一致 → 走位（先 Z 后 XY，无 TAP）
        bundle = cb.new_bundle("arm-01")
        bundle.update({"status": "ready", "daemonSession": daemons["arm-01"].session,
                       "safePark": {"parkX": 120.0, "parkY": -60.0, "parkZ": 5.0,
                                    "confirmed": True, "confirmedAt": "t",
                                    "source": "test"}})
        cb.save_bundle(bundle, debug.DATA_DIR)
        daemon = daemons["arm-01"]
        daemon.commands.clear()
        status, result = post("/api/park", {"device": "arm-01", "contextId": context_id})
        assert status == 200
        assert result["targetRobot"] == [120.0, -60.0]
        assert any(c.startswith("Z ") for c in daemon.commands)
        assert any(c.startswith("XY ") for c in daemon.commands)
        assert not any(c.startswith("TAP") for c in daemon.commands)
        assert daemon._mpos["x"] == 120.0 and daemon._mpos["y"] == -60.0

        # 3) 机械会话变化后，绝对停车坐标仍须重新确认。
        daemon.session = "session-changed"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            post("/api/park", {"device": "arm-01", "contextId": context_id})
        assert "会话已变化" in str(json.loads(excinfo.value.read()))
        daemon.session = "session-01"

        # 4) P09：停车位带自身确认会话（旧）而顶层/当前会话一致 → 仍拒绝。
        #    顶层 daemonSession 更新不能替旧停车位重新背书。
        wizard = debug.get_wizard("arm-01")
        with wizard._lock:
            wizard.state["safePark"] = {
                "confirmed": True, "parkX": 130.0, "parkY": -50.0, "parkZ": 5.0,
                "source": "test", "daemonSession": "session-old",
                "mposAtConfirm": {"x": 0.0, "y": 0.0, "z": 0.0}}
            wizard.state["daemonSession"] = daemon.session  # 顶层与当前一致
            wizard._save_state()
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            post("/api/park", {"device": "arm-01", "contextId": context_id})
        assert "重新确认 SAFE PARK" in str(json.loads(excinfo.value.read()))
        assert daemon._mpos["x"] == 120.0  # 未发生任何停车走位
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_motion_rejected_while_auto_run_active(two_devices, monkeypatch):
    """运动链审计 R6：校准 run 活动期间，调试台运动端点一律拒绝
    （runner 持有运动权；STOP/ESTOP 不经此门禁）。"""
    _configs, daemons = two_devices
    server = debug.ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    class _FakeWizard:
        def auto_run_active(self):
            return True

    monkeypatch.setattr(debug, "get_wizard", lambda device: _FakeWizard())

    def post(path: str, payload: dict):
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **_dbg_headers(base)}, method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    try:
        _status, ctx = post("/api/device-context", {"device": "arm-01"})
        context_id = ctx["contextId"]
        for path, payload, label in [
            ("/api/jog", {"axis": "x", "delta": 1, "feed": 500}, "点动"),
            ("/api/tap", {"depth": 3.0, "feed": 1500}, "点击测试"),
            ("/api/hover", {"hoverZ": 2.0, "feed": 1500}, "回悬停"),
            ("/api/goto", {"nx": 0.5, "ny": 0.5}, "画面走位"),
            ("/api/park", {}, "驶向停止位"),
        ]:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                post(path, {**payload, "device": "arm-01", "contextId": context_id})
            message = str(json.loads(excinfo.value.read()).get("error", {}).get("message", ""))
            assert "校准 run 进行中" in message and label in message
        # run 不活动时放行（回归）
        monkeypatch.setattr(debug, "get_wizard",
                            lambda device: type("W", (), {"auto_run_active": lambda self: False})())
        _status, ctx2 = post("/api/device-context", {"device": "arm-01"})
        # jog 会真实执行到 fake daemon——只验证不再被门禁拒绝
        status, _ = post("/api/jog", {"axis": "x", "delta": 1, "feed": 500,
                                      "device": "arm-01", "contextId": ctx2["contextId"]})
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("runtime_status", ["paused", "quarantined", "maintenance", "offline"])
@pytest.mark.parametrize("route,payload,expected", [
    ("jog", {"axis": "z", "delta": 1, "feed": 1000}, "Z 1.000 F1000"),
    ("tap", {"depth": 1, "feed": 1000}, "TAP 1.000 F1000"),
    ("hover", {"hoverZ": 4, "feed": 1000}, "Z 1.000 F1000"),
    ("resume", {}, "RESUME"),
])
def test_manual_controls_ignore_task_and_camera_gates(two_devices, monkeypatch, runtime_status,
                                                route, payload, expected):
    _configs, daemons = two_devices
    context = debug.create_device_context("arm-01")
    monkeypatch.setattr(debug, "runtime_device_record", lambda _d: {
        "status": runtime_status, "presence_state": "removed"})
    def unavailable_camera(_device):
        raise RuntimeError("手动操作不读取相机资格")

    monkeypatch.setattr(debug, "camera_status", unavailable_camera)
    daemon = daemons["arm-01"]
    daemon.session = "session-after-usb-reconnect"
    monkeypatch.setattr(daemon, "resume", lambda: json.loads(daemon.cmd("RESUME")), raising=False)
    server = debug.ThreadingHTTPServer(("127.0.0.1", 0), debug.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        req = urllib.request.Request(base + "/api/" + route, method="POST",
            headers={"Content-Type": "application/json", **_dbg_headers(base)},
            data=json.dumps({"device": "arm-01", "contextId": context["contextId"],
                             **payload}).encode())
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                status, result = response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status, result = exc.code, json.loads(exc.read())
        assert status == 200, result
        assert daemons["arm-01"].commands == [expected]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("condition,expected", [
    ({"estop_locked": True}, "急停锁定"),
    ({"connected": False, "session": None}, "串口未连接"),
    ({"device_id": "arm-02"}, "daemon_device_mismatch"),
])
def test_manual_context_reports_actual_hardware_restrictions(two_devices, monkeypatch, condition, expected):
    _configs, daemons = two_devices
    context = debug.create_device_context("arm-01")
    daemon = daemons["arm-01"]
    status = {**daemon.status(), **condition}
    monkeypatch.setattr(daemon, "status", lambda: status)
    with pytest.raises(RuntimeError, match=expected):
        debug.require_context("arm-01", context["contextId"], motion=True, manual=True)
    assert daemon.commands == []


def test_manual_context_does_not_relax_automatic_context(two_devices, monkeypatch):
    _configs, daemons = two_devices
    context = debug.create_device_context("arm-01")
    monkeypatch.setattr(debug, "runtime_device_record", lambda _d: {"status": "paused"})
    with pytest.raises(RuntimeError, match="device_paused"):
        debug.require_context("arm-01", context["contextId"], motion=True)
    # 同一旧上下文：仅手动入口放行，自动暂停状态不会被改为 ready。
    assert debug.require_context("arm-01", context["contextId"], motion=True, manual=True)
    with pytest.raises(RuntimeError, match="device_paused"):
        debug.require_context("arm-01", context["contextId"], motion=True)
    with pytest.raises(RuntimeError, match="device_context_mismatch"):
        debug.require_context("arm-02", context["contextId"], motion=True, manual=True)
    assert daemons["arm-01"].commands == []


def test_status_after_camera_rebinding_keeps_manual_connection_visible(two_devices, monkeypatch):
    _configs, daemons = two_devices
    context = debug.create_device_context("arm-01")
    camera = {**debug.camera_status("arm-01"), "binding_revision": 6}
    monkeypatch.setattr(debug, "camera_status", lambda _d: camera)
    daemon = daemons["arm-01"]
    monkeypatch.setattr(daemon, "mpos", lambda **_kw: dict(daemon._mpos))
    result = debug.status_snapshot("arm-01", context["contextId"])
    assert result["daemon"]["connected"] is True
    assert result["mpos"]["x"] == 1.0
    assert result["camera"]["binding_revision"] == 6
    with pytest.raises(RuntimeError, match="camera_binding_revision_changed"):
        debug.require_context("arm-01", context["contextId"], motion=True)


@pytest.mark.parametrize("condition,code,http_status,message", [
    ({"connected": False, "session": None, "disconnect_reason": "控制器启动/复位，会话已撤销"},
     "serial_not_connected", 409, "控制器启动/复位"),
    ({"connected": False, "session": None, "status_stale": True,
      "error_code": "daemon_command_timeout", "last_error": "守护进程状态查询超时"},
     "daemon_command_timeout", 504, "查询超时"),
    ({"connected": False, "session": None, "status_stale": True,
      "error_code": "daemon_unreachable", "last_error": "守护进程连接被拒绝"},
     "daemon_unreachable", 503, "连接被拒绝"),
])
def test_manual_guard_preserves_connection_failure_reason(two_devices, monkeypatch,
                                                         condition, code, http_status, message):
    _configs, daemons = two_devices
    context = debug.create_device_context("arm-01")
    daemon = daemons["arm-01"]
    status = {**daemon.status(), **condition}
    monkeypatch.setattr(daemon, "status", lambda: status)
    with pytest.raises(debug.DaemonCommandError) as failure:
        debug.require_context("arm-01", context["contextId"], motion=True, manual=True)
    actual_http, body = debug.api_error(failure.value)
    assert actual_http == http_status
    assert body["error"]["code"] == code
    assert message in body["error"]["message"]
    assert daemon.commands == []
