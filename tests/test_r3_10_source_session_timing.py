"""R3-10 红测：来源会话和时间证明不可缺省（第三轮缺陷卡 R3-10）。

映射（acceptance-matrix.json 同步）：
- R3-10-A1 v2 帧无有效会话立即拒绝（身份字段齐全≠时钟已证明）。
- R3-10-A2 合成 PTS 链经真实消费端（evidence 归档 → HttpFrameSource →
  broker 链身份 → Fleet 只读来源 HTTP 路由）：同会话身份、原 JPEG 与首次
  接收时间一致；重放原回执不续鲜。
- R3-10-B1 会话登记必须关联受控本机证明（helper 二进制摘要/宿主代际时钟
  指纹/实际格式/误差界限）；自报证明字符串不是可信登记。
- R3-10-B2 逐帧核对：缺会话/自报假证明/错误宿主代际/过大误差/旧序号/
  同号异字节/PTS 倒退逐项拒绝且不更新最新帧。
- R3-10-C 重启无会话不能上传成为可信证据；旧证据可审计但不可恢复执行资格；
  合法重握手恢复；时钟基准/几何变化按冻结规则失效。
- R3-10-D 实机时钟误差=待现场验证（单列 blocked，不在软件测试内闭合）。

替身清单：替换=相机采集（合成 JPEG+受控合成 PTS 链）、GUI、受控 helper
二进制（cc 原生替身）；保留=真实帧桥校验/会话/证据归档链、真实 Fleet
EvidenceStore 与只读来源路由、真实 HttpFrameSource/CameraFrameBroker 消费端。
隔离：临时 data_root/DB/令牌；不接触生产端口与真实设备。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import scripts.camera_binding_v2 as v2
from scripts import camera_frame_server as frame

ARM_ROOT = Path(__file__).resolve().parents[1]
FORMAT_ID = "fourcc:2f323736;size:640x480;pixel-aspect:1/1"
GEOMETRY_SIZE = (640, 480)


def jpeg_bytes(color: tuple[int, int, int] = (10, 20, 30), width: int = 640,
               height: int = 480) -> bytes:
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
    req = urllib.request.Request(base + path, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
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
        "verify_camera_clock_r310", ARM_ROOT / "tools" / "verify_camera_clock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_helper(source_path: Path) -> Path:
    compiled = source_path.with_suffix("")
    if not compiled.exists():
        source = source_path.with_suffix(".c")
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
        subprocess.run(["cc", "-O2", "-o", str(compiled), str(source)],
                       check=True, capture_output=True)
        compiled.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                       | stat.S_IRGRP | stat.S_IROTH)
        subprocess.run([str(compiled), "--clock-proof"], capture_output=True)
    return compiled


def geometry_payload(width: int = GEOMETRY_SIZE[0],
                     height: int = GEOMETRY_SIZE[1],
                     format_id: str = FORMAT_ID) -> tuple[dict, str]:
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


_SAFETY_FLEET: dict | None = None


def _start_fake_safety_fleet() -> dict:
    """最小假 Fleet：仅提供 confirmed 安全视图（只读投影），供换绑核对。"""
    global _SAFETY_FLEET
    if _SAFETY_FLEET is not None:
        return _SAFETY_FLEET
    import hashlib as _hl
    import hmac as _hm
    import threading as _th
    import uuid as _uu
    from datetime import datetime as _dt, timezone as _tz
    from http.server import BaseHTTPRequestHandler as _H, ThreadingHTTPServer as _S
    key_id, secret_hex = "camera-frame-server-test", "ef" * 32
    secret = bytes.fromhex(secret_hex)
    keys_file = None

    class Handler(_H):
        def do_GET(self):
            ts = self.headers.get("X-WX-Time", "")
            nonce = self.headers.get("X-WX-Nonce", "")
            sig = self.headers.get("X-WX-Signature", "")
            body_hash = _hl.sha256(b"").hexdigest()
            expected = _hm.new(secret, f"GET\n{self.path}\n{ts}\n{nonce}\n{body_hash}".encode(),
                               _hl.sha256).hexdigest()
            if not _hm.compare_digest(expected, sig):
                self.send_response(401); self.end_headers(); return
            device_id = self.path.split("/")[3].split("?")[0]  # /v1/devices/{id}/safety-view
            view = {"contract_version": "device-safety-view.v1",
                    "device_id": device_id, "binding": None,
                    "snapshot_revision": 1,
                    "observed_at": _dt.now(_tz.utc).isoformat(
                        timespec="milliseconds").replace("+00:00", "Z"),
                    "in_flight_task_ids": [], "unresolved_task_ids": [],
                    "held_session_id": None, "lease_active": False,
                    "mechanical_safe": "confirmed", "blocking_reasons": []}
            raw = json.dumps(view).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = _S(("127.0.0.1", 0), Handler)
    _th.Thread(target=httpd.serve_forever, daemon=True).start()
    import tempfile as _tf
    kf = Path(_tf.mkdtemp(prefix="r411-fakefleet-")) / "keys.json"
    kf.write_text(json.dumps({"format_version": 1, "active_key_id": key_id,
                              "keys": [{"key_id": key_id,
                                        "secret_hex": secret_hex,
                                        "enabled": True}]}))
    _SAFETY_FLEET = {"base": f"http://127.0.0.1:{httpd.server_port}",
                     "keys_file": str(kf), "key_id": key_id,
                     "server": httpd}
    return _SAFETY_FLEET


@pytest.fixture()
def timing_env(tmp_path, monkeypatch):
    """真实帧桥（受控证明严格模式）+ Fleet EvidenceStore 同根。"""
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({
        "devices": [{"device_id": "arm-01", "camera_id": "cam-01",
                     "expected_frame_width": GEOMETRY_SIZE[0],
                     "expected_frame_height": GEOMETRY_SIZE[1]}]}),
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
    helper = _write_fake_helper(tmp_path / "fake_camera_helper")
    monkeypatch.setenv("CAMERA_HELPER_EXEC", str(helper))
    monkeypatch.setattr(frame, "_PROOF_CACHE", {}, raising=False)
    # R4-11-B：换绑核对的 Fleet 安全查询为必需配置——本夹具提供模块内
    # 最小假 Fleet（仅 device-safety-view 只读投影，confirmed 视图）。
    fake = _start_fake_safety_fleet()
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_BASE_URL", fake["base"])
    monkeypatch.setenv("ARMFLEET_WX_NAV_KEYS_FILE", fake["keys_file"])
    monkeypatch.setenv("CAMERA_FLEET_SAFETY_KEY_ID", fake["key_id"])
    source_dir = tmp_path / "camera_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    install_id = str(uuid.uuid4())
    (source_dir / "host_installation_id").write_text(install_id + "\n",
                                                     encoding="utf-8")
    os.chmod(source_dir / "host_installation_id", 0o600)
    clock = _clock_tools()
    clock_id = clock.source_clock_id(**clock.host_fingerprint(install_id))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), frame.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state = {
        "base": f"http://127.0.0.1:{httpd.server_port}",
        "auth": {"Authorization": f"Bearer {token}"},
        "tmp_path": tmp_path,
        "helper": helper,
        "helper_sha256": v2.file_sha256(helper),
        "clock_id": clock_id,
        "clock_id_alt": "mach-absolute:" + "cd" * 32,
    }
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        frame.configure_state(record_restart=False)


def bind_channel(env: dict, request_id: str = "r310-bind") -> str:
    status, body = _http(env["base"], "/cameras/cam-01/binding-v2", method="PUT",
                         body={"requestId": request_id,
                               "deviceUniqueId": "uvc-01"},
                         headers=env["auth"])
    assert status == 200, body
    return body["cameraBindingId"]


def register_session(env: dict, binding_id: str, *, client: str = "helper-r310",
                     clock_id: str | None = None, format_id: str | None = FORMAT_ID,
                     helper_sha256: str | None = None, uncertainty: int = 1,
                     geometry_digest_override: str | None = None,
                     drop_fields: tuple = (), replaces: str | None = None):
    """按 R3-08 新协议登记会话；drop_fields 模拟缺失/自报证明。"""
    clock_id = clock_id if clock_id is not None else env["clock_id"]
    helper_sha256 = helper_sha256 if helper_sha256 is not None \
        else env["helper_sha256"]
    status, current = _http(env["base"],
                            "/cameras/cam-01/capture-sessions/current",
                            headers=env["auth"])
    assert status == 200, current
    geometry, geometry_id = geometry_payload(format_id=format_id)
    if geometry_digest_override is not None:
        geometry_id = geometry_digest_override
    body = {"serverInstanceId": current["server_instance_id"],
            "clientInstanceId": f"{client}-{uuid.uuid4().hex[:8]}",
            "cameraBindingId": binding_id,
            "geometry": {**geometry, "source_geometry_id": geometry_id},
            "sourceClockId": clock_id,
            "timingProvenance": "camera_pts_host_clock.v1",
            "captureUncertaintyMs": uncertainty}
    if replaces:
        body["replacesGenerationId"] = replaces
    if "formatId" not in drop_fields:
        body["formatId"] = format_id
    if "helperSha256" not in drop_fields:
        body["helperSha256"] = helper_sha256
    return _http(env["base"], "/cameras/cam-01/capture-sessions", method="POST",
                 body=body, headers=env["auth"])


def upload_frame(env: dict, *, seq: int, blob: bytes, binding_id: str,
                 generation: str, geometry_id: str | None = None,
                 clock_id: str | None = None, captured_ns: int | None = None,
                 captured_at: str | None = None, uncertainty: str = "1",
                 provenance: str | None = "camera_pts_host_clock.v1"):
    geometry_id = geometry_id if geometry_id is not None else geometry_payload()[1]
    clock_id = clock_id if clock_id is not None else env["clock_id"]
    headers = {
        **env["auth"],
        "Content-Type": "image/jpeg",
        "X-Camera-Binding-Contract": "camera-binding.v2",
        "X-Camera-Id": "cam-01",
        "X-Camera-Unique-Id": "uvc-01",
        "X-Camera-Binding-Id": binding_id,
        "X-Source-Geometry-Id": geometry_id,
        "X-Source-Generation-Id": generation,
        "X-Source-Frame-Seq": str(seq),
        "X-Source-Clock-Id": clock_id,
        "X-Source-Captured-Monotonic-Ns": str(captured_ns if captured_ns is not None
                                              else 1_000_000_000 + seq),
        "X-Source-Capture-Uncertainty-Ms": uncertainty,
        "X-Captured-At": captured_at or datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    if provenance is not None:
        headers["X-Source-Timing-Provenance"] = provenance
    return _http(env["base"], "/cameras/cam-01/frame", method="POST",
                 body=blob, headers=headers)


# ---------------------------------------------------------------------------
# R3-10-A1：无有效会话立即拒绝
# ---------------------------------------------------------------------------

def test_r3_10_a_v2_frame_without_valid_session_rejected(timing_env):
    env = timing_env
    binding_id = bind_channel(env)
    status, body = upload_frame(env, seq=1, blob=jpeg_bytes((80, 80, 80)),
                                binding_id=binding_id, generation=str(uuid.uuid4()))
    assert status == 503, (status, body)
    assert body["error"]["code"] == "source_unavailable"
    # 拒绝不得更新最新帧
    status, headers, _ = _http(env["base"], "/cameras/cam-01/latest.jpg", raw=True)
    assert status != 200, "无会话帧不得成为可消费来源"
    status, view = _http(env["base"], "/cameras/cam-01/status")
    assert view["has_frame"] is False


def test_r3_10_a_legacy_upload_cannot_overwrite_active_v2_session(timing_env):
    env = timing_env
    binding_id = bind_channel(env)
    status, session = register_session(env, binding_id)
    assert status == 200, session
    generation = session["source_generation_id"]
    geometry_id = session["source_geometry_id"]
    v2_blob = jpeg_bytes((200, 10, 10))
    status, receipt = upload_frame(env, seq=1, blob=v2_blob,
                                   binding_id=binding_id, generation=generation,
                                   geometry_id=geometry_id)
    assert status == 200, receipt
    # chrome_compat 兼容上传：不得覆盖活动 v2 会话
    status, body = _http(env["base"], "/cameras/cam-01/frame", method="POST",
                         body=jpeg_bytes((0, 255, 0)),
                         headers={"Content-Type": "image/jpeg",
                                  "X-Camera-Id": "cam-01",
                                  "X-Camera-Unique-Id": "uvc-01",
                                  "X-Frame-Source": "chrome_compat",
                                  "X-Source-Frame-Seq": "50",
                                  "X-Captured-At": datetime.now(timezone.utc)
                                  .isoformat()})
    assert status == 409, (status, body)
    assert body["error"]["code"] == "camera_source_conflict"
    # 自称正式来源但无 v2 身份头的旧协议上传：同样拒绝（不冒充 v2 可用）
    status, body = _http(env["base"], "/cameras/cam-01/frame", method="POST",
                         body=jpeg_bytes((0, 0, 255)),
                         headers={"Content-Type": "image/jpeg",
                                  "X-Camera-Id": "cam-01",
                                  "X-Camera-Unique-Id": "uvc-01",
                                  "X-Frame-Source": "camera_helper",
                                  "X-Source-Frame-Seq": "51",
                                  "X-Captured-At": datetime.now(timezone.utc)
                                  .isoformat()})
    assert status == 409, (status, body)
    # 最新帧保持 v2 原字节
    status, headers, served = _http(env["base"], "/cameras/cam-01/latest.jpg",
                                    raw=True)
    assert status == 200 and served == v2_blob
    assert headers["X-Source-Generation-Id"] == generation


# ---------------------------------------------------------------------------
# R3-10-B1：会话登记必须绑定受控本机证明
# ---------------------------------------------------------------------------

def test_r3_10_b_registration_requires_controlled_local_proof(timing_env):
    env = timing_env
    binding_id = bind_channel(env)
    # ① 缺 formatId/helperSha256（自报字符串不构成登记）
    status, body = register_session(env, binding_id,
                                    drop_fields=("formatId", "helperSha256"))
    assert status == 503 and body["error"]["code"] == "source_timing_unproven", body
    # ② 自报宿主代际时钟（不是本机 OS 计算）
    status, body = register_session(env, binding_id,
                                    clock_id="mach-absolute:" + "ab" * 32)
    assert status == 503 and body["error"]["code"] == "source_timing_unproven", body
    # ③ helper 摘要与受控安装二进制不一致
    status, body = register_session(env, binding_id,
                                    helper_sha256="ff" * 32)
    assert status == 503 and body["error"]["code"] == "source_timing_unproven", body
    # ④ 非实际管线格式（格式串形状非法）
    status, body = register_session(env, binding_id, format_id="1280x720")
    assert status == 422 and body["error"]["code"] == "camera_request_invalid", body
    # ⑤ 自报几何（摘要与几何事实不符）
    status, body = register_session(env, binding_id,
                                    geometry_digest_override="a" * 64)
    assert status == 422 and body["error"]["code"] == "camera_request_invalid", body
    # ⑥ 合法受控证明：登记成功且响应绑定证明事实；核验归档可审计
    status, session = register_session(env, binding_id)
    assert status == 200, session
    assert session["format_id"] == FORMAT_ID
    assert session["helper_sha256"] == env["helper_sha256"]
    assert session["source_clock_id"] == env["clock_id"]
    assert session["capture_uncertainty_ms"] == 1
    registration_file = env["tmp_path"] / "camera_source" / "clock_registration.json"
    assert registration_file.exists(), "受控时钟核验必须归档 clock_registration"
    registration = json.loads(registration_file.read_text(encoding="utf-8"))
    assert registration["ok"] is True
    assert registration["helper_sha256"] == env["helper_sha256"]
    assert registration["source_clock_id"] == env["clock_id"]
    assert registration["envelope_ok"] is True


# ---------------------------------------------------------------------------
# R3-10-A2：合成 PTS 链经真实消费端；重放不续鲜
# ---------------------------------------------------------------------------

def test_r3_10_a_synthetic_pts_chain_through_consumers(timing_env):
    env = timing_env
    binding_id = bind_channel(env)
    status, session = register_session(env, binding_id)
    assert status == 200, session
    generation = session["source_generation_id"]
    geometry_id = session["source_geometry_id"]
    # 合成采集：PTS 链单调推进（与 time.monotonic_ns 同域，供消费端新鲜门）
    blobs = [jpeg_bytes((n * 30, 60, 90)) for n in range(1, 4)]
    now_ns = time.monotonic_ns()
    now_dt = datetime.now(timezone.utc)

    def frame_clock(index: int) -> tuple[int, str]:
        ns = now_ns - 1_000_000_000 + (index - 1) * 250_000_000
        dt = now_dt - timedelta(seconds=(now_ns - ns) / 1e9)
        return ns, dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    receipts = {}
    for index, blob in enumerate(blobs, start=1):
        captured_ns, captured_at = frame_clock(index)
        status, receipt = upload_frame(
            env, seq=index, blob=blob, binding_id=binding_id,
            generation=generation, geometry_id=geometry_id,
            captured_ns=captured_ns, captured_at=captured_at)
        assert status == 200, receipt
        receipts[index] = receipt
    # ① 证据归档：同会话身份 + 原 JPEG + 首次接收时间
    from runtime.evidence import EvidenceStore
    from runtime.store import Store
    store = EvidenceStore(Store(env["tmp_path"] / "fleet.db"),
                          env["tmp_path"] / "evidence")
    rows = store.latest_source_frames_v2("arm-01", 8)
    by_seq = {row["source_capture_seq"]: row for row in rows}
    assert set(by_seq) == {1, 2, 3}
    for index, blob in enumerate(blobs, start=1):
        row = by_seq[index]
        assert row["jpeg_sha256"] == hashlib.sha256(blob).hexdigest()
        assert row["camera_binding_id"] == binding_id
        assert row["source_generation_id"] == generation
        assert row["source_geometry_id"] == geometry_id
        assert row["source_clock_id"] == env["clock_id"]
        assert row["source_captured_monotonic_ns"] == \
            frame_clock(index)[0]
        assert row["timing_provenance"] == "camera_pts_host_clock.v1"
        # 原字节保真（证据文件=上传 JPEG）
        assert store.read_source_frame_bytes(row["evidence_id"]) == blob
    mono = [by_seq[i]["source_captured_monotonic_ns"] for i in (1, 2, 3)]
    assert mono == sorted(mono), "采样时刻必须随帧序单调"
    # ② 重放原回执：原回执+原证据+首次时间，不增桥序号、不刷新时间
    status, replay = upload_frame(env, seq=2, blob=blobs[1],
                                  binding_id=binding_id, generation=generation,
                                  geometry_id=geometry_id,
                                  captured_ns=frame_clock(2)[0],
                                  captured_at=frame_clock(2)[1])
    assert status == 200 and replay == receipts[2]
    status, headers, _served = _http(env["base"], "/cameras/cam-01/latest.jpg",
                                     raw=True)
    assert headers["X-Frame-Seq"] == str(receipts[3]["bridge_seq"]), \
        "重放不得推进桥序号"
    assert headers["X-Received-At"] == receipts[3]["received_at"]
    # ③ 真实消费端：HttpFrameSource → RealCameraDriver → broker 链身份
    from drivers.camera_broker import CameraFrameBroker, frame_chain_identity
    from drivers.real_camera import HttpFrameSource, RealCameraDriver
    source = HttpFrameSource(env["base"] + "/cameras/cam-01/latest.jpg",
                             max_age_seconds=1.5)
    driver = RealCameraDriver("cam-01", frame_provider=source)
    brokered = CameraFrameBroker(driver, camera_id="cam-01",
                                 max_age_seconds=1.5).capture_sync()
    identity = frame_chain_identity(brokered)
    assert identity is not None, "合法 v2 帧必须通过同一链进入动作判定（滑动端同源）"
    assert identity["generation"] == generation
    assert identity["captured_monotonic_ns"] == frame_clock(3)[0]
    assert identity["provenance"] == "camera_pts_host_clock.v1"
    # ④ Fleet 只读来源 HTTP 路由：同一身份 + 首次时间
    from tests.layout_d2_helpers import AppEnv
    with AppEnv(env["tmp_path"]) as fleet:
        fleet.runtime.devices._devices["arm-01"].update({"status": "online"})
        r = fleet.client.get("/v1/devices/arm-01/camera-source-frame")
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["source_frame"]["source_generation_id"] == generation
        assert view["source_frame"]["camera_binding_id"] == binding_id
        assert view["source_frame"]["source_capture_seq"] == 3
        assert view["jpeg_sha256"] == \
            hashlib.sha256(blobs[2]).hexdigest()


# ---------------------------------------------------------------------------
# R3-10-B2：逐帧拒绝矩阵（不更新最新帧）
# ---------------------------------------------------------------------------

def test_r3_10_b_per_frame_reject_matrix(timing_env):
    env = timing_env
    binding_id = bind_channel(env)
    status, session = register_session(env, binding_id)
    assert status == 200, session
    generation = session["source_generation_id"]
    geometry_id = session["source_geometry_id"]
    base_blob = jpeg_bytes((5, 5, 5))
    status, first = upload_frame(env, seq=1, blob=base_blob,
                                 binding_id=binding_id, generation=generation,
                                 geometry_id=geometry_id, captured_ns=1_000_000_000)
    assert status == 200, first

    def expect_reject(**kwargs):
        codes = kwargs.pop("codes")
        kwargs.setdefault("geometry_id", geometry_id)
        status, body = upload_frame(env, seq=2,
                                    blob=kwargs.pop("blob", jpeg_bytes((7, 7, 7))),
                                    binding_id=binding_id, generation=generation,
                                    **kwargs)
        assert status in (409, 503), (status, body)
        assert body["error"]["code"] in codes, body

    # 自报假证明：缺 PTS 证明标记
    expect_reject(provenance=None,
                  codes={"source_timing_unproven"})
    # 错误宿主代际（未知 clock）
    expect_reject(clock_id="mach-absolute:" + "ff" * 32,
                  codes={"source_timing_unproven"})
    # 过大误差（超会话登记界限 1ms）
    expect_reject(uncertainty="5", codes={"source_timing_unproven"})
    # 超绝对上限
    expect_reject(uncertainty="99", codes={"source_timing_unproven"})
    # 旧序号
    status, body = upload_frame(env, seq=0, blob=jpeg_bytes((8, 8, 8)),
                                binding_id=binding_id, generation=generation,
                                geometry_id=geometry_id)
    assert status == 409 and body["error"]["code"] == "camera_source_conflict"
    # 同号异字节
    status, body = upload_frame(env, seq=1, blob=jpeg_bytes((9, 9, 9)),
                                binding_id=binding_id, generation=generation,
                                geometry_id=geometry_id, captured_ns=1_000_000_000)
    assert status == 409 and body["error"]["code"] == "camera_source_conflict"
    # 错误几何（自报）
    expect_reject(geometry_id="b" * 64, codes={"source_geometry_changed"})
    # 过期帧（原帧年龄 > 2s）
    stale_at = (datetime.now(timezone.utc) - timedelta(seconds=30)) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expect_reject(captured_at=stale_at, codes={"source_unavailable"})
    # 未来帧
    future_at = (datetime.now(timezone.utc) + timedelta(seconds=5)) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expect_reject(captured_at=future_at, codes={"source_unavailable"})
    # PTS 倒退（seq 前进、单调采样时间回落=时钟基准变化）
    status, body = upload_frame(env, seq=2, blob=jpeg_bytes((11, 11, 11)),
                                binding_id=binding_id, generation=generation,
                                geometry_id=geometry_id,
                                captured_ns=999_999_999)
    assert status == 503 and body["error"]["code"] == "source_timing_unproven", body
    # 逐项拒绝后最新帧保持第一帧
    status, headers, served = _http(env["base"], "/cameras/cam-01/latest.jpg",
                                    raw=True)
    assert status == 200 and served == base_blob
    assert headers["X-Frame-Seq"] == str(first["bridge_seq"])


# ---------------------------------------------------------------------------
# R3-10-C：重启/时钟基准/几何变化失效；旧证据可审计不可恢复资格
# ---------------------------------------------------------------------------

def test_r3_10_c_restart_and_clock_base_change_invalidate(timing_env, monkeypatch):
    env = timing_env
    tmp_path = env["tmp_path"]
    binding_id = bind_channel(env)
    status, session = register_session(env, binding_id)
    assert status == 200, session
    generation_old = session["source_generation_id"]
    status, _ = upload_frame(env, seq=1, blob=jpeg_bytes((33, 33, 33)),
                             binding_id=binding_id, generation=generation_old)
    assert status == 200
    # 服务重启：新实例、会话清空（证据归档保留）
    monkeypatch.setattr(frame, "_SERVER_INSTANCE_ID", str(uuid.uuid4()))
    frame.configure_state(
        bindings_path=tmp_path / "camera_helper" / "bindings.json",
        devices_path=tmp_path / "devices.json",
        legacy_camera_id="cam-01",
        data_root=tmp_path,
        fleet_db_path=tmp_path / "fleet.db",
        evidence_dir=tmp_path / "evidence",
        record_restart=True)
    # 重启无会话：旧代际帧不能上传成为可信证据
    status, body = upload_frame(env, seq=2, blob=jpeg_bytes((44, 44, 44)),
                                binding_id=binding_id, generation=generation_old)
    assert status == 503 and body["error"]["code"] == "source_unavailable", body
    # 旧证据仍可审计，但不恢复执行资格（通道无会话、无新鲜帧）
    from runtime.evidence import EvidenceStore
    from runtime.store import Store
    store = EvidenceStore(Store(tmp_path / "fleet.db"), tmp_path / "evidence")
    rows = store.latest_source_frames_v2("arm-01", 4)
    assert any(row["source_generation_id"] == generation_old for row in rows), \
        "旧证据必须保留可审计"
    status, _headers, _view = _http(env["base"],
                                    "/cameras/cam-01/latest.jpg", raw=True)
    assert status != 200, "审计保留≠执行资格：重启后不得有可动作新鲜帧"
    # 合法重握手恢复（同安装 UUID、新代际）
    status, session2 = register_session(env, binding_id, client="helper-r310-b")
    assert status == 200, session2
    generation_new = session2["source_generation_id"]
    assert generation_new != generation_old
    status, receipt = upload_frame(env, seq=1, blob=jpeg_bytes((55, 55, 55)),
                                   binding_id=binding_id,
                                   generation=generation_new)
    assert status == 200, receipt
    # 时钟基准变化（宿主代际更换）：本机宿主指纹事实改变（模拟安装代际更换；
    # 服务端始终从本机 OS 独立重算，伪造 clock id 无法登记）
    install_file = tmp_path / "camera_source" / "host_installation_id"
    new_install = str(uuid.uuid4())
    install_file.write_text(new_install + "\n", encoding="utf-8")
    os.chmod(install_file, 0o600)
    clock_tools = _clock_tools()
    clock_new = clock_tools.source_clock_id(
        **clock_tools.host_fingerprint(new_install))
    assert clock_new != env["clock_id"], "宿主代际更换必须产生新 clock id"
    status, body = register_session(env, binding_id, client="helper-forged",
                                    clock_id=env["clock_id_alt"])
    assert status == 503 and body["error"]["code"] == "source_timing_unproven", \
        "伪造宿主代际不得登记"
    env2 = dict(env)
    env2["clock_id"] = clock_new
    status, session3 = register_session(env2, binding_id, client="helper-r310-c",
                                        replaces=session2["source_generation_id"])
    assert status == 200 and session3["source_clock_id"] == clock_new
    status, body = upload_frame(env, seq=1, blob=jpeg_bytes((66, 66, 66)),
                                binding_id=binding_id,
                                generation=session3["source_generation_id"])
    assert status == 503 and body["error"]["code"] == "source_timing_unproven", body
    # 几何/格式变化：旧几何帧拒绝（同一会话内几何不变）
    status, body = upload_frame(env2, seq=1, blob=jpeg_bytes((77, 77, 77)),
                                binding_id=binding_id,
                                generation=session3["source_generation_id"],
                                geometry_id=geometry_payload()[1])
    # session3 登记几何=默认 geometry_payload()；这里 geometry 一致应成功
    assert status == 200, body
    other_geometry, other_digest = geometry_payload(format_id="fourcc:2f323939"
                                                    ";size:1280x720;"
                                                    "pixel-aspect:1/1")
    status, body = upload_frame(env2, seq=2, blob=jpeg_bytes((88, 88, 88)),
                                binding_id=binding_id,
                                generation=session3["source_generation_id"],
                                geometry_id=other_digest)
    assert status == 409 and body["error"]["code"] == "source_geometry_changed", body


def test_r3_10_c_without_proof_infrastructure_now_fail_closed(timing_env,
                                                               monkeypatch):
    """R4-11-A（替代旧 lax 断言，2026-10-05 执行与验收 §5 R4-11）：缺受控
    证明基础设施时登记必须 fail-closed（不产生动作可信会话）。旧"保持旧
    部署宽松"断言被本条替代；旧记录读取/展示兼容不受影响。
    """
    env = timing_env
    if not hasattr(frame, "_helper_exec_path"):
        pytest.skip("基线（修前）无受控证明门：兼容断言仅适用于修复后代码")
    monkeypatch.delenv("CAMERA_HELPER_EXEC")
    monkeypatch.setattr(frame, "_helper_exec_path", lambda: None)
    monkeypatch.setattr(frame, "_session_proof_strict", lambda: False)
    binding_id = bind_channel(env)
    # 手工构造无证明登记（旧协议形状）
    status, current = _http(env["base"],
                            "/cameras/cam-01/capture-sessions/current",
                            headers=env["auth"])
    assert status == 200
    body = {"serverInstanceId": current["server_instance_id"],
            "clientInstanceId": "legacy-client",
            "cameraBindingId": binding_id,
            "geometry": {"source_geometry_id": "a" * 64},
            "sourceClockId": "mach-absolute:" + "ab" * 32,
            "timingProvenance": "camera_pts_host_clock.v1",
            "captureUncertaintyMs": 1}
    status, response = _http(env["base"], "/cameras/cam-01/capture-sessions",
                             method="POST", body=body, headers=env["auth"])
    assert status == 503, response
    assert response["error"]["code"] == "source_timing_unproven"
