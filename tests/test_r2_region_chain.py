"""R2-15 粗框从调试台实际请求走通真实证据链（FIX-12/R2-15）。

红测断言正确行为（修前必败）：
- 页面原始请求只有 {deviceId, contextId, rect{x,y,width,height},
  source_evidence_id}——不在测试添 installation_binding/source_size 等可信字段；
  rect 是 source_norm 的 {x,y,width,height}（旧 left/top/right/bottom 域拒绝）；
- 服务从 source_evidence_id 读原框选证据核对坐标域/真实尺寸/设备/安装/来源几何；
- 预览另取当前新鲜可信源帧（同设备/安装/来源几何）实际检测；
  source_binding.evidence_id 指向本次检测帧（可与框选证据不同）；
- 保存时再核验来源新鲜/预览期限/发布空闲；无校准可框选；检测失败可存
  needs_review 草稿但不能发布生效；两个 evidence_id 可以不同；
- 全部机械/输入为 0（测试无驱动通道）。
链路：页面生成的原始请求 → 真实调试台 HTTP 代理 → 真实 Fleet app（uvicorn
临时端口）→ 重启读取。相机帧桥以合成帧替身（归档进 EvidenceStore）。
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
import uuid

import numpy as np
import pytest

from tests.layout_d2_helpers import (AppEnv, FrameBridgeFake, phone_scene,
                                     write_v3_bundle)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def chain(tmp_path):
    """真实 Fleet app（uvicorn 临时端口）+ 真实调试台 HTTP 服务（临时端口）。

    调试台进程经 _auto_proxy（urllib）转发到 Fleet；身份按 R2-23 受信转发
    （同一令牌文件 + 控制台主体）。机械/输入通道不存在（零动作可静态成立）。
    """
    import uvicorn
    from fastapi.testclient import TestClient

    env = AppEnv(tmp_path)
    write_v3_bundle(tmp_path)  # v3 只影响四态抓页；粗框不要求（本链路验证不挡）
    bridge = FrameBridgeFake(env.runtime)
    env.bridge = bridge
    # 调试台合并设备配置来自 ARMFLEET_DB_PATH 的持久记录：统一指向死端口，
    # 保证测试全程不触碰任何真实 daemon/相机端口（require_context 只做
    # 字符串核对 + daemon.status() 失败兜底，不建立连接成功路径）。
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "fleet.db"))
    try:
        row = conn.execute(
            "SELECT json FROM devices WHERE device_id='arm-01'").fetchone()
        if row:
            record = json.loads(row[0])
            record["daemon_addr"] = "127.0.0.1:1"
            record["camera_status_url"] = "http://127.0.0.1:1/status"
            record["camera_frame_url"] = "http://127.0.0.1:1/latest.jpg"
            record["status"] = "online"
            conn.execute("UPDATE devices SET json=? WHERE device_id='arm-01'",
                         (json.dumps(record),))
            conn.commit()
    finally:
        conn.close()

    fleet_port = _free_port()
    config = uvicorn.Config(env.app, host="127.0.0.1", port=fleet_port,
                            log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "Fleet uvicorn 未启动"

    from scripts import motor_debug_server as debug
    console_port = _free_port()
    old_base = debug.IME_BASE_URL
    debug.IME_BASE_URL = f"http://127.0.0.1:{fleet_port}"
    old_configs = dict(debug.DEVICE_CONFIGS)
    debug.DEVICE_CONFIGS = {
        "arm-01": {"device_id": "arm-01", "camera_id": "cam-01",
                   "daemon_addr": "127.0.0.1:1",
                   "camera_status_url": "http://127.0.0.1:1/status",
                   "camera_frame_url": "http://127.0.0.1:1/latest.jpg",
                   "expected_frame_width": 640, "expected_frame_height": 480}}
    import os
    old_db = os.environ.get("ARMFLEET_DB_PATH")
    os.environ["ARMFLEET_DB_PATH"] = str(tmp_path / "fleet.db")
    from tests.layout_d2_helpers import TOKEN_ENV
    old_token = os.environ.get(TOKEN_ENV)
    from http.server import ThreadingHTTPServer as _THS
    httpd = _THS(("127.0.0.1", console_port), debug.Handler)
    console_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    console_thread.start()
    base = f"http://127.0.0.1:{console_port}"
    try:
        yield {"env": env, "console": base, "fleet_client": env.client,
               "bridge": bridge, "debug": debug}
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.should_exit = True
        thread.join(timeout=5)
        debug.IME_BASE_URL = old_base
        debug.DEVICE_CONFIGS = old_configs
        if old_db is None:
            os.environ.pop("ARMFLEET_DB_PATH", None)
        else:
            os.environ["ARMFLEET_DB_PATH"] = old_db
        if old_token is None:
            os.environ.pop(TOKEN_ENV, None)
        env.close()


def _console_post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    headers = _browser_headers(base)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


_DBG_SESSION = {"cookie": None, "csrf": None}


def _debug_bootstrap(base: str) -> None:
    """R3-14 浏览器对等自举（会话 Cookie + X-Debug-CSRF；幂等）。"""
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


def _browser_headers(base: str) -> dict:
    _debug_bootstrap(base)
    headers = {"Origin": base, "Sec-Fetch-Site": "same-origin"}
    if _DBG_SESSION["csrf"]:
        headers["X-Debug-CSRF"] = _DBG_SESSION["csrf"]
    if _DBG_SESSION["cookie"]:
        headers["Cookie"] = _DBG_SESSION["cookie"]
    return headers




def _console_get(base: str, path: str, context_id: str | None = None):
    if context_id:
        path += ("&" if "?" in path else "?") + "contextId=" + context_id
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _console_context(chain) -> str:
    """页面建立的调试上下文：向调试台进程注入本测试设备的会话上下文
    （上下文创建入口本身会连相机/daemon——外部 IO，替身为最小会话行；
    require_context 的校验逻辑不替换，真实执行）。"""
    import time as _time
    debug = chain["debug"]
    context_id = "ctx-" + uuid.uuid4().hex
    with debug._LOCK:
        debug.CONTEXTS[context_id] = {
            "context_id": context_id, "device_id": "arm-01",
            "camera_id": "cam-01", "camera_device_unique_id": None,
            "binding_revision": None, "frame_width": None,
            "frame_height": None, "daemon_session": None,
            "daemon_addr": "127.0.0.1:1",
            "expires_at": _time.time() + 600, "checks": [],
            "ready_for_motion": False, "ready_for_read": True}
    return context_id


def _page_preview_request(rect_evidence_id: str, rect: dict) -> dict:
    """页面生成的原始请求（regionPreview 当前发出的字段形状）。"""
    return {"deviceId": "arm-01", "contextId": None,
            "rect": rect, "source_evidence_id": rect_evidence_id}


def test_r2_15_page_request_preview_save_restart(chain):
    """正常取帧→画框（source_norm x/y/w/h）→预览→保存→重启读取：
    页面原始请求（不含安装/尺寸可信字段）经真实代理→三 Fleet 路由走通。"""
    env = chain["env"]
    bridge = chain["bridge"]
    context_id = _console_context(chain)
    # ① 旧帧（框选时的冻结帧）+ 新鲜帧（预览检测帧）：同设备/安装/几何
    old_frame = bridge.push(phone_scene(phone=(200, 120, 240, 240)))
    fresh_frame = bridge.push(phone_scene(phone=(200, 120, 240, 240)))
    assert old_frame["evidence_id"] != fresh_frame["evidence_id"]
    # 页面框住手机（source_norm {x,y,width,height}，右/下排他）
    rect = {"x": 0.25, "y": 0.15, "width": 0.5, "height": 0.66}
    request = _page_preview_request(old_frame["evidence_id"], rect)
    request["contextId"] = context_id
    status, preview = _console_post(
        chain["console"], "/api/phone-search-region/preview", request)
    assert status == 200, preview
    assert preview["status"] == "verified", preview
    assert preview["reason_codes"] == []
    assert preview["detected_quad"] and len(preview["detected_quad"]) == 4
    # 检测帧=新鲜帧（不是框选旧图）；两 evidence_id 可以不同
    assert preview["source_binding"]["evidence_id"] == fresh_frame["evidence_id"]
    assert preview["source_binding"]["evidence_id"] != old_frame["evidence_id"]
    # source_binding 来自服务端证据，不由客户端申报
    assert preview["source_binding"]["installation_binding"]["camera_binding_id"]
    assert preview["pixel_rect"] == {"left": 160, "top": 72,
                                     "right": 480, "bottom": 389}
    # ② 保存（publish=true；设备空闲、预览期限内）
    save_request = {"deviceId": "arm-01", "contextId": context_id,
                    "preview_id": preview["preview_id"],
                    "region_digest": preview["region_digest"],
                    "expected_revision": 0, "publish": True}
    status, saved = _console_post(
        chain["console"], "/api/phone-search-region/save", save_request)
    assert status == 200, saved
    assert saved["status"] == "verified"
    assert saved["selected_version"] == 1
    # ③ 重启读取：同一临时根新 app 实例（真实持久化，不借内存）
    #    （控制台→Fleet 同端口不重启 uvicorn；用同根新 AppEnv 验证持久）
    with AppEnv(env.tmp_path, token=True) as restarted:
        restarted.runtime.devices._devices["arm-01"].update({
            "status": "online", "calibration_status": "validated",
            "daemon_session": "sess-1", "camera_id": "cam-01",
            "daemon_binding_id": "arm-bind-01", "binding_revision": 7})
        read_headers = {**restarted.auth(),
                        "X-Correlation-Id": str(uuid.uuid4()),
                        "X-Layout-Contract-Version":
                            "phone-search-region-read.v1"}
        r = restarted.client.get(
            "/v1/devices/arm-01/phone-search-region", headers=read_headers)
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["status"] == "verified"
        assert view["selected"]["version"] == 1
        r = restarted.client.get(
            "/v1/devices/arm-01/phone-search-region/versions/1",
            headers=read_headers)
        assert r.status_code == 200, r.text
        assert r.json()["record"]["pixel_rect"]["left"] == 160


def test_r2_15_legacy_rect_domain_rejected(chain):
    """rect 旧 {left,top,right,bottom} 域（或四角）按冻结契约拒绝：422。"""
    bridge = chain["bridge"]
    frame = bridge.push(phone_scene())
    request = _page_preview_request(
        frame["evidence_id"],
        {"left": 0.2, "top": 0.2, "right": 0.8, "bottom": 0.7})
    request["contextId"] = _console_context(chain)
    status, body = _console_post(
        chain["console"], "/api/phone-search-region/preview", request)
    assert status == 422, (status, body)
    assert body["error"]["code"] == "layout_request_invalid"


def test_r2_15_foreign_or_missing_evidence_rejected(chain):
    """框选证据：不存在 404；他设备证据不读取（404，不泄漏存在性）。"""
    bridge = chain["bridge"]
    context_id = _console_context(chain)
    rect = {"x": 0.3, "y": 0.3, "width": 0.3, "height": 0.3}
    status, body = _console_post(
        chain["console"], "/api/phone-search-region/preview",
        {**_page_preview_request("no-such-evidence", rect),
         "contextId": context_id})
    assert status == 404, (status, body)
    # 他设备证据：为 arm-02 归档一帧后仍按设备 scope 拒绝
    other = FrameBridgeFake(chain["env"].runtime, device="arm-02")
    frame = other.push(phone_scene())
    status, body = _console_post(
        chain["console"], "/api/phone-search-region/preview",
        {**_page_preview_request(frame["evidence_id"], rect),
         "contextId": context_id})
    assert status == 404, (status, body)


def test_r2_15_binding_change_between_evidence_and_fresh_frame(chain):
    """新鲜帧与框选证据安装/几何不一致：拒绝（source_binding_changed 409），
    不能把新帧套到旧绑定。"""
    from tests.layout_d2_helpers import BINDING
    bridge = chain["bridge"]
    context_id = _console_context(chain)
    old_frame = bridge.push(phone_scene())
    changed = dict(bridge.binding)
    changed["camera_binding_id"] = "44444444-4444-4444-8444-444444444444"
    bridge.binding = changed
    bridge.push(phone_scene())
    bridge.binding = dict(BINDING)
    rect = {"x": 0.3, "y": 0.3, "width": 0.4, "height": 0.5}
    status, body = _console_post(
        chain["console"], "/api/phone-search-region/preview",
        {**_page_preview_request(old_frame["evidence_id"], rect),
         "contextId": context_id})
    assert status == 409, (status, body)
    assert body["error"]["code"] == "layout_binding_changed"


def test_r2_15_zero_detection_draft_cannot_publish(chain):
    """零手机检测：needs_review 预览（detected_quad=null）；publish=true 拒绝
    且指针不变；publish=false 存草稿（draft_saved，pending_draft 可见），
    草稿不改生效指针。"""
    from tests.layout_d2_helpers import encode_jpeg
    bridge = chain["bridge"]
    context_id = _console_context(chain)
    # 全暗帧（无手机）→ 检测 0 个候选
    dark = np.full((480, 640, 3), 18, dtype=np.uint8)
    old_frame = bridge.push(dark)
    bridge.push(dark)
    rect = {"x": 0.3, "y": 0.3, "width": 0.4, "height": 0.5}
    status, preview = _console_post(
        chain["console"], "/api/phone-search-region/preview",
        {**_page_preview_request(old_frame["evidence_id"], rect),
         "contextId": context_id})
    assert status == 200, preview
    assert preview["status"] == "needs_review"
    assert preview["detected_quad"] is None
    assert "phone_not_located" in preview["reason_codes"] or \
        "dark_screen" in preview["reason_codes"]
    # publish=true 拒绝：未验证的检测不能发布生效
    status, body = _console_post(
        chain["console"], "/api/phone-search-region/save",
        {"deviceId": "arm-01", "contextId": context_id,
         "preview_id": preview["preview_id"],
         "region_digest": preview["region_digest"],
         "expected_revision": 0, "publish": True})
    assert status == 409, (status, body)
    assert body["error"]["code"] == "layout_items_unverified"
    # publish=false：合规 needs_review 草稿，selected 不变
    status, preview2 = _console_post(
        chain["console"], "/api/phone-search-region/preview",
        {**_page_preview_request(old_frame["evidence_id"], rect),
         "contextId": context_id})
    assert status == 200
    status, saved = _console_post(
        chain["console"], "/api/phone-search-region/save",
        {"deviceId": "arm-01", "contextId": context_id,
         "preview_id": preview2["preview_id"],
         "region_digest": preview2["region_digest"],
         "expected_revision": 0, "publish": False})
    assert status == 200, saved
    assert saved["status"] == "draft_saved"
    assert saved["selected_version"] is None
    status, view = _console_get(chain["console"],
                                "/api/phone-search-region?device=arm-01",
                                context_id)
    assert status == 200, view
    assert view["pending_draft"] is not None
    assert view["selected"] is None


def test_r2_15_preview_expiry_blocks_save(chain):
    """预览期限（120 秒）过期后保存拒绝（409 layout_preview_expired）。"""
    bridge = chain["bridge"]
    env = chain["env"]
    context_id = _console_context(chain)
    frame = bridge.push(phone_scene())
    bridge.push(phone_scene())
    # 框需完整罩住手机（默认 200,120,240,240）才能 verified
    rect = {"x": 0.2, "y": 0.12, "width": 0.6, "height": 0.68}
    status, preview = _console_post(
        chain["console"], "/api/phone-search-region/preview",
        {**_page_preview_request(frame["evidence_id"], rect),
         "contextId": context_id})
    assert status == 200, preview
    assert preview["status"] == "verified", preview
    # 预览记录过期：直接推进服务端时钟（受控时钟注入）后保存
    svc = env.set_region_clock(AppEnv.RegionClock(start="2027-01-01T00:00:00Z"))
    status, body = _console_post(
        chain["console"], "/api/phone-search-region/save",
        {"deviceId": "arm-01", "contextId": context_id,
         "preview_id": preview["preview_id"],
         "region_digest": preview["region_digest"],
         "expected_revision": 0, "publish": True})
    assert status == 409, (status, body)
    assert body["error"]["code"] == "layout_preview_expired"


def test_r2_15_page_removed_phone_cannot_publish_old_detection(chain):
    """冻结后手机移出框/来源停止推进：旧检测不能发布——保存前重取新鲜帧
    实际再检测（不是复用预览时结论）。"""
    bridge = chain["bridge"]
    context_id = _console_context(chain)
    frame = bridge.push(phone_scene())
    bridge.push(phone_scene())
    rect = {"x": 0.25, "y": 0.15, "width": 0.5, "height": 0.66}
    status, preview = _console_post(
        chain["console"], "/api/phone-search-region/preview",
        {**_page_preview_request(frame["evidence_id"], rect),
         "contextId": context_id})
    assert status == 200 and preview["status"] == "verified", preview
    # 随后来源里的手机消失（新帧暗屏）
    from tests.layout_d2_helpers import phone_scene as _scene
    dark = np.full((480, 640, 3), 18, dtype=np.uint8)
    bridge.push(dark)
    status, body = _console_post(
        chain["console"], "/api/phone-search-region/save",
        {"deviceId": "arm-01", "contextId": context_id,
         "preview_id": preview["preview_id"],
         "region_digest": preview["region_digest"],
         "expected_revision": 0, "publish": True})
    # 保存时来源核验不得沿用旧检测结论：手机移出/暗屏 → 拒绝发布
    assert status == 409, (status, body)
    assert body["error"]["code"] in (
        "layout_items_unverified", "layout_source_unavailable",
        "layout_binding_changed")
    status, view = _console_get(chain["console"],
                                "/api/phone-search-region?device=arm-01",
                                context_id)
    assert status == 200, view
    assert view["status"] in ("unregistered", "needs_review")
    assert view["selected"] is None


def test_r2_15_zero_mechanical_input_actions(chain):
    """全程零机械/输入：测试装配未挂任何 daemon/输入通道；保存只写资料。
    （动作计数=0 由装配成立：无驱动 client、无 IME 写路径被调用。）"""
    env = chain["env"]
    submitted = [t for t in env.runtime.store.all_tasks()]
    assert submitted == [] or all(
        t.get("task_type") not in ("gesture_self_test", "wx_auto_navigate")
        for t in submitted)
