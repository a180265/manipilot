from types import SimpleNamespace

import pytest

from drivers import calibration_bundle as cb, screen_proof
from scripts import motor_debug_server as debug
from tests.test_screen_proof import box, png, scene
from tests.test_ui_targets import ready_bundle



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

@pytest.fixture
def environment(monkeypatch, tmp_path):
    cb.save_bundle(ready_bundle(), tmp_path)
    commands = []
    daemon = SimpleNamespace(status=lambda: {"session": "sess-A", "lease": None}, commands=commands)
    context = {"camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
               "frame_width": 1280, "frame_height": 720}
    monkeypatch.setattr(debug, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "daemon_for", lambda device: daemon)
    monkeypatch.setattr(debug, "require_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(debug, "fetch_camera_frame", lambda device: (png(scene()), 10))
    monkeypatch.setattr(debug, "camera_status", lambda device: {
        "status": "streaming", "has_frame": True, "frame_age_ms": 10})
    monkeypatch.setattr(debug, "log_op", lambda *args, **kwargs: None)
    return tmp_path, daemon


def request(method, path, body=None):
    handler = debug.Handler.__new__(debug.Handler)
    # 测试更正（§4.5）：R3-14 边界读取 peer 地址——裸构造的 Handler 补回环语义
    handler.client_address = ("127.0.0.1", 0)
    import email.message as _em
    headers = _em.Message()
    # 测试更正（§4.5）：精确 Host（裸 Handler 无 server→边界按 LISTEN_PORT 判定）
    # + 同站浏览器对等头（R3-14 写通道需会话 CSRF）
    headers["Host"] = f"127.0.0.1:{debug.LISTEN_PORT}"
    headers["Origin"] = f"http://127.0.0.1:{debug.LISTEN_PORT}"
    headers["Sec-Fetch-Site"] = "same-origin"
    # 裸 Handler 无真实服务可自举：直接向模块会话表签发（同一会话机制）
    if not _DBG_SESSION.get("csrf"):
        _token, _csrf = debug._DEBUG_SESSIONS.issue()
        _DBG_SESSION["cookie"] = debug._la.DebugConsoleSessions.cookie_value(_token)
        _DBG_SESSION["csrf"] = _csrf
    headers["Cookie"] = _DBG_SESSION.get("cookie") or ""
    headers["X-Debug-CSRF"] = _DBG_SESSION.get("csrf") or ""
    handler.headers = headers
    handler.path = path
    output = {}
    handler._body = lambda: body or {}
    handler._json = lambda value, code=200: output.update(status=code, body=value)
    handler._send = lambda code, data, mime, *args, **kwargs: output.update(status=code, raw=data)
    getattr(handler, "do_" + method)()
    return output


def test_debug_reference_endpoint_persists_and_has_readback_without_motion(environment):
    root, daemon = environment
    result = request("POST", "/api/screen-proof/reference", {
        "device": "arm-01", "contextId": "ctx", "operator": "tester", "confirmed": True,
        "state": "conversation", "label": "会话界面", "region": box(100, 50, 220, 110)})
    assert result["status"] == 200, result
    loaded = request("GET", "/api/screen-proof?device=arm-01&contextId=ctx")
    assert loaded["body"]["profile"]["references"]["conversation"]["label"] == "会话界面"
    assert screen_proof.load_profile("arm-01", root)["revision"] == 1
    assert not daemon.commands


def test_debug_registration_requires_confirmation_and_current_daemon(environment):
    root, daemon = environment
    body = {"device": "arm-01", "contextId": "ctx", "operator": "tester",
            "state": "conversation", "label": "会话界面", "region": box(100, 50, 220, 110)}
    result = request("POST", "/api/screen-proof/reference", body)
    assert result["status"] >= 400
    daemon.status = lambda: {"session": "restarted", "lease": None}
    result = request("POST", "/api/screen-proof/reference", {**body, "confirmed": True})
    assert result["status"] >= 400
    assert not screen_proof.profile_path("arm-01", root).exists()


def test_debug_page_exposes_reference_regions_and_read_only_check():
    from html.parser import HTMLParser

    class Form(HTMLParser):
        ids = set()

        def handle_starttag(self, tag, attrs):
            self.ids.update(value for key, value in attrs if key == "id")

    form = Form()
    form.feed(debug.PAGE_PATH.read_text())
    assert {"proofReference", "proofRegions", "proofCheck", "proofRegion",
            "proofCornerA", "proofCornerB", "proofState"} <= form.ids


def test_debug_page_describes_registered_send_target_without_ocr_fallback():
    response = request("GET", "/")
    assert response["status"] == 200, str(response.get("body"))[:200]
    page = response["raw"].decode()
    assert "OCR 优先、固定兜底" not in page
    assert "OCR 优先不变" not in page
    assert "发送按钮必须登记、测试并启用" in page


def test_fleet_readiness_endpoint_exposes_metadata_not_image_or_coordinates(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_stage_e_api import build_app
    from tests.test_screen_proof import registered_scene

    registered_scene(tmp_path / "data")
    app, runtime = build_app(tmp_path, n_devices=1)
    with TestClient(app) as client:
        runtime.devices._devices["arm-01"]["daemon_session"] = "sess-A"
        response = client.get("/v1/devices/arm-01/screen-proof")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["regions_registered"] is True
        assert result["reference_states"] == ["conversation"]
        assert result["registered"] is True
        assert "image" not in result and "regions" not in result
