"""B01 验收（2026-10-03 规格 camera-binding 附件 §4/§4.1）。

- 绑定 v2：cameraBindingId/无绑定通道删除/重复逻辑相机、重复物理相机、
  重复实例 UUID 拒绝；坏档 503 不变空；
- prepared 意图原子写入：崩溃对账（=after 补 committed /=before aborted
  /其他 unknown 保持阻断）；同编号同摘要幂等、异摘要冲突；
- helper token：0600/拒绝符号链接/Bearer+loopback+常量时间；缺失不自动生成；
- installation_locks：动作共享、绑定写排他；文件不删除不替换；
- 换绑新 UUID 不复用历史；rename 不换 UUID；A→B→A 不同 UUID。
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading

import pytest

import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中")

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from scripts import camera_binding_v2 as v2


def _write_file(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _bindings(path: Path):
    return v2.load_bindings_v2(path)


def _req(action="bind", camera="cam-01", unique="uvc-01", request_id="r1", **kw):
    return {"request_id": request_id, "action": action, "camera_id": camera,
            "device_unique_id": unique, "bound_by": "测试", **kw}


def test_load_missing_defaults_empty_and_corrupt_raises(tmp_path):
    empty = _bindings(tmp_path / "b.json")
    assert empty == {"schemaVersion": 2, "revision": 0, "bindings": []}
    (tmp_path / "b.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(v2.CameraBindingError) as caught:
        _bindings(tmp_path / "b.json")
    assert caught.value.code == "camera_archive_invalid"


def test_duplicate_channel_physical_and_instance_rejected(tmp_path):
    path = tmp_path / "b.json"
    _write_file(path, {"schemaVersion": 2, "revision": 1, "bindings": [
        {"cameraId": "cam-01", "deviceUniqueId": "uvc-01", "cameraBindingId": "id-1"},
        {"cameraId": "cam-01", "deviceUniqueId": "uvc-02", "cameraBindingId": "id-2"},
    ]})
    with pytest.raises(v2.CameraBindingError, match="重复逻辑相机"):
        _bindings(path)
    _write_file(path, {"schemaVersion": 2, "revision": 1, "bindings": [
        {"cameraId": "cam-01", "deviceUniqueId": "uvc-01", "cameraBindingId": "id-1"},
        {"cameraId": "cam-02", "deviceUniqueId": "uvc-01", "cameraBindingId": "id-2"},
    ]})
    with pytest.raises(v2.CameraBindingError, match="重复物理相机"):
        _bindings(path)
    _write_file(path, {"schemaVersion": 2, "revision": 1, "bindings": [
        {"cameraId": "cam-01", "deviceUniqueId": "uvc-01", "cameraBindingId": "dup"},
        {"cameraId": "cam-02", "deviceUniqueId": "uvc-02", "cameraBindingId": "dup"},
    ]})
    with pytest.raises(v2.CameraBindingError, match="实例重复"):
        _bindings(path)


def test_empty_unique_id_channel_is_dropped_not_kept(tmp_path):
    path = tmp_path / "b.json"
    _write_file(path, {"schemaVersion": 2, "revision": 1, "bindings": [
        {"cameraId": "cam-01", "deviceUniqueId": "", "cameraBindingId": ""},
        {"cameraId": "cam-02", "deviceUniqueId": "uvc-02", "cameraBindingId": "id-2"},
    ]})
    loaded = _bindings(path)
    assert [b["cameraId"] for b in loaded["bindings"]] == ["cam-02"]


def test_save_bind_assigns_new_uuid_and_increments_revision(tmp_path):
    path = tmp_path / "b.json"
    intent = v2.save_binding_v2(path, _req())
    assert intent["state"] == "committed"
    assert intent["response"]["cameraBindingId"]
    stored = _bindings(path)
    assert stored["revision"] == 1
    assert stored["bindings"][0]["cameraBindingId"] == intent["response"]["cameraBindingId"]
    # 同编号同摘要幂等：原响应
    replay = v2.save_binding_v2(path, _req())
    assert replay["response"] == intent["response"]
    # 同编号异摘要冲突
    with pytest.raises(v2.CameraBindingError, match="request_conflict"):
        v2.save_binding_v2(path, _req(unique="uvc-XX"))


def test_rebind_assigns_fresh_uuid_never_reused(tmp_path):
    path = tmp_path / "b.json"
    first = v2.save_binding_v2(path, _req(request_id="r1"))
    id_a = first["response"]["cameraBindingId"]
    second = v2.save_binding_v2(path, _req(unique="uvc-02", request_id="r2",
                                           action="renew"))
    id_b = second["response"]["cameraBindingId"]
    assert id_a != id_b, "换绑分配新 UUID"
    # A→B→A：重新绑回原通道也拿新 UUID，绝不复用
    third = v2.save_binding_v2(path, _req(unique="uvc-01", request_id="r3",
                                          action="renew"))
    assert third["response"]["cameraBindingId"] not in {id_a, id_b}
    assert _bindings(path)["revision"] == 3


def test_unbind_removes_channel_and_rename_keeps_uuid(tmp_path):
    path = tmp_path / "b.json"
    first = v2.save_binding_v2(path, _req(request_id="r1"))
    id_1 = first["response"]["cameraBindingId"]
    renamed = v2.save_binding_v2(path, _req(action="rename", request_id="r2",
                                            display_name="新名字"))
    # 新显示名称：递增 revision 但不换实例 UUID（§4.1 元数据独立更新）
    assert renamed["response"]["cameraBindingId"] == id_1, "rename 不换 UUID"
    assert renamed["response"]["revision"] == 2
    removed = v2.save_binding_v2(path, _req(action="unbind", request_id="r3",
                                            camera="cam-01", unique="uvc-01"))
    assert removed["response"]["cameraBindingId"] is None
    assert _bindings(path)["bindings"] == []


def test_crash_reconciliation_by_disk_digest(tmp_path):
    path = tmp_path / "b.json"
    v2.save_binding_v2(path, _req(request_id="seed"))
    before_bytes = path.read_bytes()
    # 崩溃窗口 1：prepared 已插入、绑定文件已写（磁盘=after）→ committed
    partial = v2.save_binding_v2(path, _req(unique="uvc-02", request_id="crash-1",
                                            action="bind", camera="cam-02"))
    # 手动回滚意图状态模拟"committed 记录未写"
    op_path = v2.operation_path(path, "crash-1")
    record = json.loads(op_path.read_text(encoding="utf-8"))
    record["state"] = "prepared"
    op_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    results = v2.reconcile_operations(path)
    assert results[0]["state"] == "committed", "磁盘=after 补完成记录"

    # 崩溃窗口 2：磁盘=before（文件未写）→ aborted
    path.write_bytes(before_bytes)
    v2.save_binding_v2.__wrapped__ if False else None
    intent = v2.save_binding_v2(path, _req(unique="uvc-03", request_id="crash-2",
                                           action="bind", camera="cam-03"))
    record = json.loads(v2.operation_path(path, "crash-2").read_text(encoding="utf-8"))
    record["state"] = "prepared"
    op_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    path.write_bytes(before_bytes)
    results = v2.reconcile_operations(path)
    assert results[-1]["state"] == "aborted", "磁盘=before 标记中止不自动再写"

    # 崩溃窗口 3：磁盘既非 after 也非 before → unknown 保持阻断
    path.write_text('{"schemaVersion":2,"revision":99,"bindings":[]}',
                    encoding="utf-8")
    v2.save_binding_v2(path, _req(unique="uvc-04", request_id="crash-3",
                                  action="bind", camera="cam-04"))
    record = json.loads(v2.operation_path(path, "crash-3").read_text(encoding="utf-8"))
    record["state"] = "prepared"
    op_path.write_text(json.dumps(record, ensure_ascii=False))
    path.write_text('{"schemaVersion":2,"revision":50,"bindings":[]}',
                    encoding="utf-8")
    results = v2.reconcile_operations(path)
    assert results[-1]["state"] == "unknown"
    blocking = v2.blocking_intents(path)
    assert blocking and blocking[-1]["request_id"] == "crash-3"


def test_token_file_rules(tmp_path):
    token_file = tmp_path / "token"
    with pytest.raises(v2.CameraBindingError, match="缺失"):
        v2.load_control_token(token_file)
    token_file.write_text("x" * 64, encoding="utf-8")  # 权限错
    with pytest.raises(v2.CameraBindingError, match="0600"):
        v2.load_control_token(token_file)
    token_file.chmod(0o600)
    token_file.unlink()
    token_file.write_text("Z" * 64, encoding="utf-8")
    token_file.chmod(0o600)
    with pytest.raises(v2.CameraBindingError, match="hex"):
        v2.load_control_token(token_file)
    token_file.unlink()
    token = v2.generate_control_token(token_file)
    assert len(token) == 64 and v2.load_control_token(token_file) == token
    # 符号链接拒绝
    link = tmp_path / "link-token"
    link.symlink_to(token_file)
    with pytest.raises(v2.CameraBindingError, match="符号链接"):
        v2.load_control_token(link)


def test_installation_lock_shared_exclusive(tmp_path):
    with v2.installation_lock(tmp_path, "arm-01", exclusive=True):
        with pytest.raises(v2.CameraBindingError, match="维护占用"):
            with v2.installation_lock(tmp_path, "arm-01", exclusive=True):
                pass
        with pytest.raises(v2.CameraBindingError, match="维护占用"):
            with v2.installation_lock(tmp_path, "arm-01", exclusive=False):
                pass
    # 共享锁共存；排他等待者互斥
    with v2.installation_lock(tmp_path, "arm-01", exclusive=False):
        with v2.installation_lock(tmp_path, "arm-01", exclusive=False):
            pass
    # 锁文件保留（不删除不替换）
    assert (tmp_path / "installation_locks" / "arm-01.lock").exists()


# ---------------------------------------------------------------------------
# HTTP 层：binding-v2 路由 + helper 身份（真实服务器）
# ---------------------------------------------------------------------------

@needs_wx_example
def test_http_binding_v2_routes_with_helper_identity(tmp_path, monkeypatch):
    import urllib.request
    import urllib.error
    from http.server import ThreadingHTTPServer
    from scripts import camera_frame_server as server

    bindings = tmp_path / "bindings.json"
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({
        "devices": [{"device_id": "arm-01", "camera_id": "cam-01"}]}),
        encoding="utf-8")
    server.configure_state(bindings_path=bindings, devices_path=devices,
                           legacy_camera_id="cam-01")
    token_file = tmp_path / "token"
    token = v2.generate_control_token(token_file)
    monkeypatch.setattr(server, "CONTROL_TOKEN_FILE", token_file)
    monkeypatch.setattr(server, "V2_ENABLED", True)
    # R3-11/R4-11-B：换绑需 Fleet 安全查询（fail-closed）——受信替身，不关强校验
    from tests.close_c_safety import FleetSafetyStub
    safety = FleetSafetyStub.start(tmp_path, devices=("arm-01",))
    for _k, _v in safety.env_vars().items():
        monkeypatch.setenv(_k, _v)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def request(path, *, method="GET", body=None, headers=None):
        req = urllib.request.Request(base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    auth = {"Authorization": f"Bearer {token}"}
    try:
        # 无凭据 PUT → 403
        status, body = request("/cameras/cam-01/binding-v2", method="PUT",
                               body=json.dumps({"requestId": "x",
                                                "deviceUniqueId": "uvc-01"}).encode())
        assert status == 403 and body["error"]["code"] == "camera_permission_denied"
        # 带凭据 PUT bind → committed + 新 UUID
        status, body = request("/cameras/cam-01/binding-v2", method="PUT",
                               headers={**auth, "Content-Type": "application/json"},
                               body=json.dumps({
                                   "requestId": "http-1", "deviceUniqueId": "uvc-01",
                                   "displayName": "主摄像头"}).encode())
        assert status == 200, body
        binding_id = body["cameraBindingId"]
        assert binding_id
        # GET → 绑定视图
        status, body = request("/cameras/cam-01/binding-v2")
        assert status == 200 and body["binding"]["cameraBindingId"] == binding_id
        # 幂等重放同编号（摘要含完整原请求体：须逐字节重放原内容 §2）
        status, body2 = request("/cameras/cam-01/binding-v2", method="PUT",
                                headers={**auth, "Content-Type": "application/json"},
                                body=json.dumps({
                                    "requestId": "http-1",
                                    "deviceUniqueId": "uvc-01",
                                    "displayName": "主摄像头"}).encode())
        assert status == 200 and body2["cameraBindingId"] == binding_id
        # renew → 新 UUID
        status, body3 = request("/cameras/cam-01/binding-v2/renew", method="POST",
                                headers={**auth, "Content-Type": "application/json"},
                                body=json.dumps({"requestId": "http-2",
                                                 "deviceUniqueId": "uvc-01"}).encode())
        assert status == 200, (status, body3)
        assert body3["cameraBindingId"] != binding_id
        # 操作记录查询
        print("DEBUG files:", [p.name for p in bindings.parent.glob("binding_operations/*.json")])
        status, op = request("/camera-binding-operations/http-2")
        print("DEBUG op:", status, op)
        assert status == 200 and op["state"] == "committed"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        safety.stop()


@needs_wx_example
def test_v2_frame_bridge_receipts_and_rejections(tmp_path, monkeypatch):
    import urllib.request
    import urllib.error
    import cv2
    import numpy as np
    from http.server import ThreadingHTTPServer
    from scripts import camera_frame_server as server

    def jpeg_bytes(color=(10, 20, 30)):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        frame[:] = color
        ok, encoded = cv2.imencode(".jpg", frame)
        return encoded.tobytes()

    bindings = tmp_path / "bindings.json"
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({
        "devices": [{"device_id": "arm-01", "camera_id": "cam-01"}]}),
        encoding="utf-8")
    server.configure_state(bindings_path=bindings, devices_path=devices,
                           legacy_camera_id="cam-01")
    token_file = tmp_path / "token"
    token = v2.generate_control_token(token_file)
    monkeypatch.setattr(server, "CONTROL_TOKEN_FILE", token_file)
    monkeypatch.setattr(server, "V2_ENABLED", True)
    # R3-11/R4-11-B：换绑需 Fleet 安全查询（fail-closed）——受信替身，不关强校验
    from tests.close_c_safety import FleetSafetyStub
    safety = FleetSafetyStub.start(tmp_path, devices=("arm-01",))
    for _k, _v in safety.env_vars().items():
        monkeypatch.setenv(_k, _v)
    # R4-11-A：会话登记需受控证明基础设施——显式提供临时受控 helper
    #（合成 clock CLI）+ 真实宿主代际指纹，不靠关闭强校验恢复绿灯。
    from tests.test_r3_10_source_session_timing import (
        _write_fake_helper, _clock_tools)
    helper = _write_fake_helper(tmp_path / "fake_camera_helper")
    monkeypatch.setenv("CAMERA_HELPER_EXEC", str(helper))
    monkeypatch.setattr(server, "_PROOF_CACHE", {}, raising=False)
    import uuid as _uuid
    _install_id = str(_uuid.uuid4())
    _source_dir = tmp_path / "camera_source"
    _source_dir.mkdir(parents=True, exist_ok=True)
    import os as _os
    _install_file = _source_dir / "host_installation_id"
    _install_file.write_text(_install_id + "\n", encoding="utf-8")
    _os.chmod(_install_file, 0o600)
    monkeypatch.setattr(server, "INSTALLATION_DATA_ROOT", tmp_path)
    _clock = _clock_tools()
    real_clock_id = _clock.source_clock_id(
        **_clock.host_fingerprint(_install_id))
    real_format_id = "fourcc:2f323736;size:64x48;pixel-aspect:1/1"
    real_helper_sha = v2.file_sha256(helper)
    auth = {"Authorization": f"Bearer {token}",
            "Content-Type": "image/jpeg",
            "X-Camera-Binding-Contract": "camera-binding.v2",
            "X-Frame-Source": "camera_helper",
            "X-Camera-Id": "cam-01",
            "X-Camera-Unique-Id": "uvc-01",
            "X-Source-Geometry-Id": "geom-1",
            "X-Source-Generation-Id": "gen-1",
            "X-Source-Clock-Id": real_clock_id,
            "X-Source-Capture-Uncertainty-Ms": "1",
            "X-Source-Timing-Provenance": "camera_pts_host_clock.v1"}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def request(path, *, method="GET", body=None, headers=None):
        req = urllib.request.Request(base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def frame_post(seq, blob, geometry="geom-1", binding="bind-1",
                   generation=None):
        # R3-10：PTS/墙钟必须新鲜（修前固定假时间已被时效门拒绝）
        import time as _time
        from datetime import datetime, timezone as _tz
        now_ns = _time.monotonic_ns()
        now_iso = datetime.now(_tz.utc).isoformat().replace("+00:00", "Z")
        headers = {**auth, "X-Source-Frame-Seq": str(seq),
                   "X-Camera-Binding-Id": binding,
                   "X-Source-Captured-Monotonic-Ns": str(now_ns),
                   "X-Captured-At": now_iso}
        if generation is not None:
            headers["X-Source-Generation-Id"] = generation
        return request("/cameras/cam-01/frame", method="POST", body=blob,
                       headers=headers)

    # 先经 helper 身份绑定 v2 通道
    try:
        status, body = request("/cameras/cam-01/binding-v2", method="PUT",
                               headers={**auth, "Content-Type": "application/json"},
                               body=json.dumps({"requestId": "setup",
                                                "deviceUniqueId": "uvc-01"}).encode())
        assert status == 200, body
        binding_id = body["cameraBindingId"]
        # 测试更正（§4.5，2026-10-03）：R3-10.1 判定"无有效采集会话仍接纳 v2 帧"
        # 为缺陷——先登记会话再上传；原"无会话直传 200"断言被 R3-10-A1 取代。
        status, current = request("/cameras/cam-01/capture-sessions/current",
                                  headers=auth)
        assert status == 200, current
        # source-geometry.v1 载荷（与 R3-10 契约同形，摘要即 source_geometry_id）
        geometry = {
            "contract_version": "source-geometry.v1",
            "input_size": {"width": 64, "height": 48},
            "format_id": real_format_id,
            "crop": {"left": 0, "top": 0, "right": 64, "bottom": 48},
            "rotation_degrees": 0,
            "mirror_x": False, "mirror_y": False,
            "output_size": {"width": 64, "height": 48},
            "resampler": "none", "zoom_milli": 1000, "stabilization": "off",
        }
        geometry_id = hashlib.sha256(json.dumps(
            geometry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        status, sess = request("/cameras/cam-01/capture-sessions", method="POST",
                               headers={**auth, "Content-Type": "application/json"},
                               body=json.dumps({
                                   "serverInstanceId": current["server_instance_id"],
                                   "clientInstanceId": "legacy-receipts-test",
                                   "cameraBindingId": binding_id,
                                   "geometry": {**geometry,
                                                "source_geometry_id": geometry_id},
                                   "sourceClockId": real_clock_id,
                                   "timingProvenance": "camera_pts_host_clock.v1",
                                   "captureUncertaintyMs": 1,
                                   "formatId": real_format_id,
                                   "helperSha256": real_helper_sha,
                               }).encode())
        assert status in (200, 201), sess
        # 帧头与已登记会话同代际/几何（R3-10：旧代际来源已撤销）
        generation_id = sess.get("source_generation_id") \
            or sess.get("generation_id") or sess.get("sourceGenerationId")
        auth["X-Source-Generation-Id"] = generation_id
        auth["X-Source-Geometry-Id"] = geometry_id
        frame1 = jpeg_bytes()
        # 正常接纳
        status, r1 = frame_post(1, frame1, binding=binding_id)
        assert status == 200 and r1["accepted"] is True, r1
        assert r1["source_capture_seq"] == 1
        # 同代际+序号+同内容重传：原回执（不增桥序号）
        status, r2 = frame_post(1, frame1, binding=binding_id)
        assert status == 200 and r2 == r1 and r2["bridge_seq"] == r1["bridge_seq"]
        # 同号异内容 → 409
        status, _ = frame_post(1, jpeg_bytes((99, 99, 99)), binding=binding_id)
        assert status == 409
        # 低序号 → 409
        status, _ = frame_post(0, jpeg_bytes(), binding=binding_id)
        assert status == 409
        # 绑定实例不一致（换绑后旧上传）→ 409 source_binding_changed
        status, _ = frame_post(5, jpeg_bytes(), binding="old-binding")
        assert status == 409
        # 缺 v2 必需头 → 422
        headers = {k: v for k, v in auth.items() if k != "X-Source-Clock-Id"}
        headers["X-Source-Frame-Seq"] = "9"
        headers["X-Camera-Binding-Id"] = binding_id
        status, _ = request("/cameras/cam-01/frame", method="POST",
                            body=jpeg_bytes(), headers=headers)
        assert status == 422
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        safety.stop()
