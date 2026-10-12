"""R3-08 红测：CameraHelper 正式 v2 握手（第三轮缺陷卡 R3-08）。

映射（acceptance-matrix.json 同步）：
- R3-08-A  生产客户端（main.swift）的序列化与握手逻辑接真实帧桥：测试按
  修复后 main.swift 的真实 HTTP 序列模拟（机器凭据载入→binding-v2→
  capture-sessions/current→capture-sessions→同身份 PTS 帧上传），只替换
  采集/网络传输 IO；建立可消费来源（latest.jpg 原字节+冻结 v2 头）。
- R3-08-B1 token 缺失/错 → 真实拒绝（403），通道不出帧，不退回 legacy 冒充 v2。
- R3-08-B2 服务重启 → source_session_expired→重查询+新 client_instance_id
  重握手；旧代际帧拒绝；安装 UUID 不换。
- R3-08-B3 会话创建回包丢失 → 同号重放返回原响应（不新建第二会话）。
- R3-08-B4 换绑后旧绑定实例/旧代际帧拒绝。
- R3-08-会话证明 会话登记必须携带真实采集格式/时钟证明/host 代际/二进制摘要，
  并与服务端受控核验事实绑定（§4/§6：不接受自报字符串）。

替身清单：替换=相机采集（合成 JPEG）、GUI、helper 二进制（受控 fake 脚本供
包络核验）；保留=真实帧桥 HTTP 路由/握手状态机/绑定文件/证据归档链。
隔离：临时 data_root/绑定/令牌；不接触生产端口与真实相机。
Swift 客户端可构建性证据：tools/camera_helper swift build（C 断言，见交付报告）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import scripts.camera_binding_v2 as v2
from scripts import camera_frame_server as frame

ARM_ROOT = Path(__file__).resolve().parents[1]
FORMAT_ID = "fourcc:2f323736;size:64x48;pixel-aspect:1/1"


def jpeg_bytes(color: tuple[int, int, int] = (90, 10, 10), width: int = 64,
               height: int = 48) -> bytes:
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = color
    ok, encoded = cv2.imencode(".jpg", img)
    assert ok
    return encoded.tobytes()


def _http(base: str, path: str, *, method: str = "GET",
          body: bytes | dict | None = None, headers: dict | None = None,
          raw: bool = False):
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **(headers or {})}
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + path, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = resp.read()
            if raw:
                return resp.status, dict(resp.headers), payload
            return resp.status, json.loads(payload or b"{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, dict(exc.headers), payload
        try:
            return exc.code, json.loads(payload or b"{}")
        except ValueError:
            return exc.code, {}


def _clock_tools():
    spec = importlib.util.spec_from_file_location(
        "verify_camera_clock_r308", ARM_ROOT / "tools" / "verify_camera_clock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_helper(path: Path) -> None:
    """受控 helper 替身：--clock-proof 输出本机单调时钟 tick（§6 包络核验用）。

    用 cc 编译的原生小程序（进程启动 ~2ms，满足往返≤20ms 包络；解释器脚本
    启动 ~200ms 会超出任何真实 helper 的启动预算）。§6 语义是受控 helper 由
    Fleet 在本机启动——测试中该角色由同机原生替身承担。
    """
    import subprocess as sp
    source = path.with_suffix(".c")
    source.write_text(
        "#include <stdio.h>\n"
        "#include <mach/mach_time.h>\n"
        "int main(void) {\n"
        "    uint64_t t = mach_absolute_time();\n"
        "    mach_timebase_info_data_t tb;\n"
        "    mach_timebase_info(&tb);\n"
        "    unsigned long long ns = (unsigned long long)(t * tb.numer / tb.denom);\n"
        "    printf(\"{\\\"host_tick_ns\\\": %llu}\\n\", ns);\n"
        "    return 0;\n"
        "}\n", encoding="utf-8")
    compiled = path.with_suffix("")
    sp.run(["cc", "-O2", "-o", str(compiled), str(source)], check=True,
           capture_output=True)
    compiled.chmod(0o755)
    # 冷启动预热（不在核验 20 次内挑子集；只避免首次 dyld 开销干扰）
    sp.run([str(compiled), "--clock-proof"], capture_output=True)
    return compiled


@pytest.fixture()
def helper_env(tmp_path, monkeypatch):
    """真实帧桥 + 受控来源证明设施（fake helper 脚本 + host_installation_id）。"""
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({
        "devices": [{"device_id": "arm-01", "camera_id": "cam-01",
                     "expected_frame_width": 64, "expected_frame_height": 48}]}),
        encoding="utf-8")
    frame.configure_state(
        bindings_path=tmp_path / "camera_helper" / "bindings.json",
        devices_path=devices,
        legacy_camera_id="cam-01",
        data_root=tmp_path,
        fleet_db_path=tmp_path / "fleet.db",
        evidence_dir=tmp_path / "evidence",
        record_restart=False)
    token_file = tmp_path / "control-token"
    token = v2.generate_control_token(token_file)
    monkeypatch.setattr(frame, "CONTROL_TOKEN_FILE", token_file)
    monkeypatch.setattr(frame, "V2_ENABLED", True)
    helper_script = tmp_path / "fake_camera_helper"
    compiled = _write_fake_helper(helper_script)
    monkeypatch.setenv("CAMERA_HELPER_EXEC", str(compiled))
    # 证明缓存为新代码内部状态；基线（修前）无此符号时保持缺省（red 落在目标断言）
    monkeypatch.setattr(frame, "_PROOF_CACHE", {}, raising=False)
    source_dir = tmp_path / "camera_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    install_id = str(uuid.uuid4())
    (source_dir / "host_installation_id").write_text(install_id + "\n",
                                                     encoding="utf-8")
    os.chmod(source_dir / "host_installation_id", 0o600)
    clock = _clock_tools()
    fingerprint = clock.host_fingerprint(install_id)
    clock_id = clock.source_clock_id(**fingerprint)
    # R4-11-B：换绑核对的 Fleet 安全查询为必需配置——夹具提供模块内假
    # Fleet（仅 device-safety-view 只读 confirmed 投影）。
    from tests.test_r3_10_source_session_timing import _start_fake_safety_fleet
    fake = _start_fake_safety_fleet()
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_BASE_URL", fake["base"])
    monkeypatch.setenv("ARMFLEET_WX_NAV_KEYS_FILE", fake["keys_file"])
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_KEY_ID", fake["key_id"])
    from http.server import ThreadingHTTPServer
    import threading
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), frame.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "base": f"http://127.0.0.1:{httpd.server_port}",
            "auth": {"Authorization": f"Bearer {token}"},
            "token": token,
            "tmp_path": tmp_path,
            "helper_path": compiled,
            "helper_sha256": v2.file_sha256(compiled),
            "clock_id": clock_id,
            "format_id": FORMAT_ID,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        frame.configure_state(record_restart=False)


# ---- main.swift 正式客户端序列的 Python 协议仿真（只替换采集/传输 IO） ----

def geometry_payload(width: int = 64, height: int = 48,
                     format_id: str = FORMAT_ID) -> tuple[dict, str]:
    """实际管线几何（§3 固定字段）；摘要=规范化 JSON SHA-256。"""
    geometry = {
        "contract_version": "source-geometry.v1",
        "input_size": {"width": width, "height": height},
        "format_id": format_id,
        "crop": {"left": 0, "top": 0, "right": width, "bottom": height},
        "rotation_degrees": 0,
        "mirror_x": False, "mirror_y": False,
        "output_size": {"width": width, "height": height},
        "resampler": "none", "zoom_milli": 1000, "stabilization": "off",
    }
    digest = hashlib.sha256(json.dumps(geometry, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return geometry, digest


def helper_handshake(env: dict, *, client: str = "helper-r3-08",
                     clock_id: str | None = None, format_id: str | None = FORMAT_ID,
                     helper_sha256: str | None = None, extra: dict | None = None,
                     binding_id: str | None = None) -> dict:
    """按修复后 main.swift 的序列：current → POST capture-sessions（Bearer）。"""
    clock_id = clock_id if clock_id is not None else env["clock_id"]
    helper_sha256 = helper_sha256 if helper_sha256 is not None \
        else env["helper_sha256"]
    status, current = _http(env["base"], "/cameras/cam-01/capture-sessions/current",
                            headers=env["auth"])
    assert status == 200, current
    geometry, geometry_id = geometry_payload(format_id=format_id or FORMAT_ID)
    body: dict = {
        "serverInstanceId": current["server_instance_id"],
        "clientInstanceId": client,
        "cameraBindingId": binding_id or current_binding_id(env),
        "geometry": {**geometry, "source_geometry_id": geometry_id},
        "sourceClockId": clock_id,
        "timingProvenance": "camera_pts_host_clock.v1",
        "captureUncertaintyMs": 1,
    }
    if format_id is not None:
        body["formatId"] = format_id
    if helper_sha256 is not None:
        body["helperSha256"] = helper_sha256
    if extra:
        body.update(extra)
    status, response = _http(env["base"], "/cameras/cam-01/capture-sessions",
                             method="POST", body=body, headers=env["auth"])
    return {"status": status, "response": response, "request": body}


def current_binding_id(env: dict) -> str | None:
    status, payload = _http(env["base"], "/cameras/cam-01/binding-v2")
    if status != 200:
        return None
    return payload["binding"]["cameraBindingId"]


def upload_v2_frame(env: dict, *, seq: int, blob: bytes, binding_id: str,
                    generation: str, geometry: str | None = None,
                    clock: str | None = None, captured_ns: int | None = None,
                    uncertainty: str = "1"):
    clock = clock or env["clock_id"]
    if geometry is None:
        geometry = geometry_payload()[1]
    captured_ns = captured_ns if captured_ns is not None else 1_000_000 + seq
    headers = {
        **env["auth"],
        "Content-Type": "image/jpeg",
        "X-Camera-Binding-Contract": "camera-binding.v2",
        "X-Camera-Id": "cam-01",
        "X-Camera-Unique-Id": "uvc-01",
        "X-Camera-Binding-Id": binding_id,
        "X-Source-Geometry-Id": geometry,
        "X-Source-Generation-Id": generation,
        "X-Source-Frame-Seq": str(seq),
        "X-Source-Clock-Id": clock,
        "X-Source-Captured-Monotonic-Ns": str(captured_ns),
        "X-Source-Capture-Uncertainty-Ms": uncertainty,
        "X-Source-Timing-Provenance": "camera_pts_host_clock.v1",
        "X-Captured-At": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
    }
    return _http(env["base"], "/cameras/cam-01/frame", method="POST",
                 body=blob, headers=headers)


# ---------------------------------------------------------------------------
# R3-08-A：生产客户端序列接真实帧桥，建立可消费来源
# ---------------------------------------------------------------------------

def test_r3_08_a_formal_handshake_builds_consumable_source(helper_env):
    env = helper_env
    # ① 机器凭据（受限本机配置文件 0600 载入；不入 URL/日志）
    token_file = env["tmp_path"] / "control-token"
    assert (token_file.stat().st_mode & 0o777) == 0o600
    token = token_file.read_text(encoding="utf-8").strip()
    assert token == env["token"]
    # ② PUT binding-v2（Bearer；不再 PUT 旧 /binding）
    status, saved = _http(env["base"], "/cameras/cam-01/binding-v2", method="PUT",
                          body={"requestId": "r3-08-bind",
                                "deviceUniqueId": "uvc-01",
                                "displayName": "主摄像头"},
                          headers=env["auth"])
    assert status == 200, saved
    binding_id = saved["cameraBindingId"]
    # ③/④ 采集会话握手：真实格式/时钟证明/host 代际/二进制摘要绑定进会话
    handshake = helper_handshake(env, binding_id=binding_id)
    assert handshake["status"] == 200, handshake["response"]
    session = handshake["response"]
    assert session["source_generation_id"]
    # 服务端受控核验事实写进会话响应（不接受自报字符串的直接采信）
    assert session["format_id"] == FORMAT_ID, \
        "会话必须登记当前真实采集格式"
    assert session["helper_sha256"] == env["helper_sha256"], \
        "会话必须绑定受控 helper 二进制摘要"
    assert session["source_clock_id"] == env["clock_id"], \
        "时钟 id 必须等于服务端本机核验的宿主代际指纹"
    # ⑤ 同身份 PTS 帧上传
    blob = jpeg_bytes((90, 10, 10))
    status, receipt = upload_v2_frame(env, seq=1, blob=blob,
                                      binding_id=binding_id,
                                      generation=session["source_generation_id"])
    assert status == 200, receipt
    assert receipt["jpeg_sha256"] == hashlib.sha256(blob).hexdigest()
    # ⑥ 可消费来源：latest.jpg 原字节 + 冻结 v2 头
    status, headers, served = _http(env["base"], "/cameras/cam-01/latest.jpg",
                                    raw=True)
    assert status == 200
    assert served == blob, "取帧必须返回原 JPEG 字节"
    assert headers["X-Camera-Binding-Id"] == binding_id
    assert headers["X-Source-Generation-Id"] == session["source_generation_id"]
    assert headers["X-Source-Clock-Id"] == env["clock_id"]
    assert headers["X-Source-Timing-Provenance"] == "camera_pts_host_clock.v1"
    # ⑦ 会话创建回包丢失：同号同内容重放 → 原响应（不新建第二会话）
    replay = helper_handshake(env, client="helper-r3-08", binding_id=binding_id)
    assert replay["status"] == 200
    assert replay["response"]["source_generation_id"] == \
        session["source_generation_id"], "回包丢失重放必须复用原会话响应"


# ---------------------------------------------------------------------------
# R3-08-B1：token 缺失/错 → 真实拒绝，不退回 legacy 冒充 v2
# ---------------------------------------------------------------------------

def test_r3_08_b_machine_credential_missing_or_wrong_refused(helper_env, tmp_path):
    env = helper_env
    status, saved = _http(env["base"], "/cameras/cam-01/binding-v2", method="PUT",
                          body={"requestId": "cred-bind",
                                "deviceUniqueId": "uvc-01"},
                          headers=env["auth"])
    assert status == 200, saved
    binding_id = saved["cameraBindingId"]

    # token 缺失（无 Authorization 头）
    status, body = _http(env["base"], "/cameras/cam-01/capture-sessions",
                         method="POST",
                         body={"serverInstanceId": "x", "clientInstanceId": "c",
                               "cameraBindingId": binding_id,
                               "geometry": {"source_geometry_id": "a" * 64},
                               "sourceClockId": env["clock_id"],
                               "timingProvenance": "camera_pts_host_clock.v1",
                               "captureUncertaintyMs": 1})
    assert status == 403, (status, body)
    assert body["error"]["code"] == "camera_permission_denied"
    # token 错
    status, body = _http(env["base"], "/cameras/cam-01/capture-sessions",
                         method="POST",
                         body={"serverInstanceId": "x"},
                         headers={"Authorization": "Bearer " + "ab" * 32})
    assert status == 403 and body["error"]["code"] == "camera_permission_denied"
    # token 文件缺失：能力拒绝，不自动生成替换
    (tmp_path / "control-token").rename(tmp_path / "control-token.bak")
    status, body = _http(env["base"], "/cameras/cam-01/capture-sessions",
                         method="POST", body={"serverInstanceId": "x"},
                         headers=env["auth"])
    assert status == 403 and body["error"]["code"] == "camera_permission_denied"
    (tmp_path / "control-token.bak").rename(tmp_path / "control-token")
    # 认证失败后不得退回 legacy 冒充 v2：通道必须保持无帧（不可用）
    status, headers, _served = _http(env["base"], "/cameras/cam-01/latest.jpg",
                                     raw=True)
    assert status != 200, "认证失败后通道不得出现可消费帧（明确不可用）"
    # legacy 兼容上传也不能覆盖 v2 会话通道冒充 v2 可用
    legacy = _http(env["base"], "/cameras/cam-01/frame", method="POST",
                   body=jpeg_bytes((0, 255, 0)),
                   headers={"Content-Type": "image/jpeg",
                            "X-Camera-Id": "cam-01",
                            "X-Camera-Unique-Id": "uvc-01",
                            "X-Frame-Source": "camera_helper",
                            "X-Source-Frame-Seq": "1",
                            "X-Captured-At": datetime.now(timezone.utc)
                            .isoformat()})
    assert legacy[0] in (403, 409), \
        "v2 通道存在时 legacy 上传必须被拒绝（不得冒充 v2 可用）"


# ---------------------------------------------------------------------------
# R3-08-B2：服务重启 → source_session_expired→重握手；旧代际拒绝；UUID 不换
# ---------------------------------------------------------------------------

def test_r3_08_b_restart_requires_rehandshake_keeps_binding(helper_env, monkeypatch):
    env = helper_env
    status, saved = _http(env["base"], "/cameras/cam-01/binding-v2", method="PUT",
                          body={"requestId": "restart-bind",
                                "deviceUniqueId": "uvc-01"},
                          headers=env["auth"])
    assert status == 200, saved
    binding_id = saved["cameraBindingId"]
    handshake = helper_handshake(env, client="helper-restart",
                                 binding_id=binding_id)
    assert handshake["status"] == 200, handshake["response"]
    generation_old = handshake["response"]["source_generation_id"]

    # 服务重启：新 server_instance_id、会话清空（绑定文件保留）
    monkeypatch.setattr(frame, "_SERVER_INSTANCE_ID", str(uuid.uuid4()))
    frame.configure_state(
        bindings_path=env["tmp_path"] / "camera_helper" / "bindings.json",
        devices_path=env["tmp_path"] / "devices.json",
        legacy_camera_id="cam-01",
        data_root=env["tmp_path"],
        record_restart=True)
    # 旧代际帧上传：重启后无有效会话 → 拒绝（不得成为可信来源）
    status, body = upload_v2_frame(env, seq=2, blob=jpeg_bytes((0, 0, 255)),
                                   binding_id=binding_id,
                                   generation=generation_old)
    assert status in (409, 503), (status, body)
    assert body["error"]["code"] in ("source_unavailable",
                                     "source_generation_rejected",
                                     "source_session_expired")
    # 重握手：GET current 拿新实例 → 新 client_instance_id → 新代际
    status, current = _http(env["base"],
                            "/cameras/cam-01/capture-sessions/current",
                            headers=env["auth"])
    assert status == 200 and current["session"] is None, current
    handshake2 = helper_handshake(env, client="helper-restart-2",
                                  binding_id=binding_id)
    assert handshake2["status"] == 200, handshake2["response"]
    generation_new = handshake2["response"]["source_generation_id"]
    assert generation_new != generation_old, "重启重握手必须生成新来源代际"
    assert current_binding_id(env) == binding_id, "普通重启不得更换安装 UUID"
    status, receipt = upload_v2_frame(env, seq=1, blob=jpeg_bytes((255, 0, 0)),
                                      binding_id=binding_id,
                                      generation=generation_new)
    assert status == 200, receipt


# ---------------------------------------------------------------------------
# R3-08-B3/B4：会话冲突语义与换绑后旧身份拒绝
# ---------------------------------------------------------------------------

def test_r3_08_b_session_conflict_and_stale_binding_rejected(helper_env):
    env = helper_env
    status, saved = _http(env["base"], "/cameras/cam-01/binding-v2", method="PUT",
                          body={"requestId": "conf-bind",
                                "deviceUniqueId": "uvc-01"},
                          headers=env["auth"])
    assert status == 200, saved
    binding_id = saved["cameraBindingId"]
    handshake = helper_handshake(env, client="helper-conf", binding_id=binding_id)
    assert handshake["status"] == 200
    generation = handshake["response"]["source_generation_id"]
    # 实例代际不匹配 → source_session_expired（重新查询后握手）
    body = dict(handshake["request"])
    body["serverInstanceId"] = "stale-instance"
    status, resp = _http(env["base"], "/cameras/cam-01/capture-sessions",
                         method="POST", body=body, headers=env["auth"])
    assert status == 409 and resp["error"]["code"] == "source_session_expired"
    # 其他 client 携带错误 replaces → 409
    body2 = dict(handshake["request"])
    body2["clientInstanceId"] = "helper-conf-2"
    body2["replacesGenerationId"] = "not-the-current-generation"
    status, resp = _http(env["base"], "/cameras/cam-01/capture-sessions",
                         method="POST", body=body2, headers=env["auth"])
    assert status == 409, (status, resp)
    # 换绑：新安装实例；旧 binding/代际帧全部拒绝
    status, saved2 = _http(env["base"], "/cameras/cam-01/binding-v2", method="PUT",
                           body={"requestId": "conf-rebind",
                                 "deviceUniqueId": "uvc-01"},
                           headers=env["auth"])
    assert status == 200, saved2
    binding_new = saved2["cameraBindingId"]
    assert binding_new != binding_id
    status, body3 = upload_v2_frame(env, seq=9, blob=jpeg_bytes((9, 9, 9)),
                                    binding_id=binding_id, generation=generation)
    assert status == 409 and body3["error"]["code"] == "source_binding_changed"
