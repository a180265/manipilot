"""R2-D2 测试基础设施（R2-23 身份 / R2-15 粗框链 / R2-17 wire 契约）。

只替换外部 IO：相机帧桥（合成 JPEG + 真实采样身份字段归档进 EvidenceStore）、
机械/输入（不挂驱动，全程零动作）、时钟（RegionClock 受控）。真实保留：
FastAPI app、全部布局路由、布局服务与持久 Store、证据链、wire 契约校验
（docs/specs/layout-reuse/validate.py 同一离线校验器）。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import cv2

REPO = Path(__file__).resolve().parents[1]

# ---------------- 冻结契约（同一离线校验器加载 fixtures/schema） ----------------
_LV = None


def load_contract():
    """加载 docs/specs/layout-reuse/validate.py（模块名注册进 sys.modules）。"""
    global _LV
    if _LV is not None:
        return _LV
    spec = importlib.util.spec_from_file_location(
        "layout_reuse_validate",
        REPO / "docs" / "specs" / "layout-reuse" / "validate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["layout_reuse_validate"] = module
    spec.loader.exec_module(module)
    _LV = module
    return _LV


def wire_fixtures():
    lv = load_contract()
    examples, _cases = lv.all_fixtures()
    return examples


_HEADER_CANONICAL = {
    "content-type": "Content-Type",
    "content-length": "Content-Length",
    "content-disposition": "Content-Disposition",
    "cache-control": "Cache-Control",
    "etag": "ETag",
    "x-correlation-id": "X-Correlation-Id",
    "x-layout-contract-version": "X-Layout-Contract-Version",
}


def validate_wire_response(route_id: str, status: int, headers, body) -> None:
    """把真实 HTTP 响应按冻结 wire:<route_id>_response/error 校验（含公共头）。"""
    lv = load_contract()
    if status == 200:
        definition = route_id + "_response"
    else:
        definition = route_id + "_error"
    canonical = {}
    for key, value in headers.items():
        name = _HEADER_CANONICAL.get(key.lower(), key)
        if name == "Content-Length":
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
        canonical[name] = value
    # wire 视图是网关归一化后的契约测试容器：只保留冻结 schema 声明的
    # 应用头（真实 HTTP 的传输头如 Content-Length/Date 不属于契约字段）。
    wire_defs = lv.SCHEMAS["wire"]["definitions"]
    allowed = set(wire_defs[definition]["properties"]["headers"]
                  .get("properties", {}))
    canonical = {k: v for k, v in canonical.items() if k in allowed}
    wire = {
        "route_id": route_id,
        "status": status,
        "headers": canonical,
        "body": body,
    }
    try:
        lv.validate("wire", definition, wire)
    except lv.ContractError as exc:
        raise AssertionError(
            f"{route_id} 响应不符合冻结契约：{exc}; body="
            + json.dumps(body, ensure_ascii=False)[:600]) from exc


# ---------------- R2-23 身份：同机受信转发令牌 ----------------
TOKEN_ENV = "ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE"
PRINCIPAL_HEADER = "X-Layout-Console-Principal"


def make_token_file(root: Path) -> Path:
    path = root / "layout_forwarder.token"
    path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    return path


def identity_headers(token_file: Path, principal: str) -> dict:
    """受信转发身份头（令牌来自真实令牌文件，走 layout_auth 恒等比对）。"""
    token = token_file.read_text(encoding="utf-8").strip()
    return {"X-Layout-Forwarder-Token": token, PRINCIPAL_HEADER: principal}


# ---------------- 应用装配（真实 create_app + 临时根） ----------------
def make_app(tmp_path: Path, *, token: bool = True, devices: int = 1,
             frame_size: tuple[int, int] = (640, 480)):
    """真实 Fleet app（create_app+TestClient，临时根，无 daemon/IME）。"""
    from api.app import create_app
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime
    from tests.helpers import make_device_configs

    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    token_file = make_token_file(tmp_path) if token else None
    if token_file is not None:
        import os
        os.environ[TOKEN_ENV] = str(token_file)
    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.device_configs = make_device_configs(
        devices, camera_device_unique_id="uvc-01", binding_revision=7,
        daemon_addr="127.0.0.1:8795", expected_frame_width=frame_size[0],
        expected_frame_height=frame_size[1])
    app.state.layout_db = tmp_path / "layout.db"
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-1", "online": True, "keyboardActive": True}]
    return app, runtime, tmp_path, token_file


class AppEnv:
    """一个真实 app 实例的持有器（with 语义 + 常用装配）。"""

    def __init__(self, tmp_path: Path, *, token: bool = True, devices: int = 1,
                 frame_size: tuple[int, int] = (640, 480)):
        import os
        from fastapi.testclient import TestClient
        self.tmp_path = tmp_path
        self.token_file = None
        self._previous_token = os.environ.get(TOKEN_ENV)
        self.app, self.runtime, self.tmp_path, self.token_file = make_app(
            tmp_path, token=token, devices=devices, frame_size=frame_size)
        self.client = TestClient(self.app)
        with self.client:
            self.runtime.devices._devices["arm-01"].update({
                "status": "online", "calibration_status": "validated",
                "ime_online": True, "ime_device_id": "k40-1",
                "ime_keyboard_active": True, "daemon_session": "sess-1",
                "camera_id": "cam-01", "binding_revision": 7,
                "daemon_binding_id": "arm-bind-01"})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        import os
        try:
            self.client.close()
        except Exception:
            pass
        # __init__ enters and exits lifespan; close any later lazily opened stores.
        parts = getattr(self.runtime, "_layout_bundle_cache", None)
        if parts:
            for name in ("state_store", "template_store", "region_store"):
                parts[name].close()
        if self.token_file is not None:
            if self._previous_token is None:
                os.environ.pop(TOKEN_ENV, None)
            else:
                os.environ[TOKEN_ENV] = self._previous_token

    def auth(self, principal: str = "debug-console") -> dict:
        return identity_headers(self.token_file, principal)

    # ---- 布局服务时钟注入（预览期限等受控测试） ----
    class RegionClock:
        def __init__(self, start="2026-10-04T12:00:00Z"):
            self.now = datetime.fromisoformat(start.replace("Z", "+00:00"))

        def __call__(self):
            return self.now

        def advance(self, seconds: float):
            self.now = self.now + timedelta(seconds=seconds)

    def set_region_clock(self, clock):
        svc = self.runtime.layout_bundle(self.tmp_path / "layout.db")["region_svc"]
        svc._clock = clock
        return svc


# ---------------- 相机源帧替身（合成图 + 真实采样身份归档） ----------------
BINDING = {"camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
           "camera_binding_id": "33333333-3333-4333-8333-333333333333",
           "arm_binding_id": "arm-bind-01"}
GEOMETRY_ID = "a" * 64
CLOCK_ID = "mach-absolute:" + "b" * 64


def phone_scene(width: int = 640, height: int = 480,
                phone: tuple = (200, 120, 240, 240),
                tiles: dict | None = None) -> np.ndarray:
    """暗背景 + 亮色手机矩形（真实可检测）；tiles: {name: (image, x, y)}。"""
    canvas = np.full((height, width, 3), 18, dtype=np.uint8)
    x, y, w, h = phone
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), (235, 235, 235), -1)
    cv2.rectangle(canvas, (x + 6, y + 6), (x + w - 7, y + h - 7), (30, 30, 30), -1)
    for image, tx, ty in (tiles or {}).values():
        th, tw = image.shape[:2]
        canvas[ty:ty + th, tx:tx + tw] = image
    return canvas


def encode_jpeg(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    assert ok
    return buf.tobytes()


class FrameBridgeFake:
    """帧桥替身：把带完整 v2 采样身份的帧归档进 EvidenceStore（Fleet 只
    消费已归档受控帧）。seq/capture_seq/单调时刻真实推进。"""

    def __init__(self, runtime, device: str = "arm-01", *,
                 width: int = 640, height: int = 480,
                 binding: dict | None = None, geometry: str = GEOMETRY_ID):
        self.runtime = runtime
        self.device = device
        self.width = width
        self.height = height
        self.binding = dict(binding or BINDING)
        self.geometry = geometry
        self.seq = 0
        self.capture_seq = 0
        self.mono_ns = 1_000_000_000_000
        self.generation = "11111111-1111-4111-8111-111111111111"
        self.archived: list[dict] = []

    def push(self, image: np.ndarray, *, interval_ms: float = 250.0) -> dict:
        self.seq += 1
        self.capture_seq += 1
        self.mono_ns += int(interval_ms * 1e6)
        jpeg = encode_jpeg(image)
        record = {
            "device_id": self.device,
            "evidence_type": "camera_source_v2",
            "camera_id": self.binding["camera_id"],
            "device_unique_id": self.binding["camera_device_unique_id"],
            "camera_binding_id": self.binding["camera_binding_id"],
            "arm_binding_id": self.binding["arm_binding_id"],
            "source_geometry_id": self.geometry,
            "binding_revision": 7,
            "source_frame_seq": self.seq,
            "source_generation_id": self.generation,
            "source_capture_seq": self.capture_seq,
            "source_clock_id": CLOCK_ID,
            "source_captured_monotonic_ns": self.mono_ns,
            "capture_uncertainty_ms": 1.0,
            "source_capture_uncertainty_ms": 1.0,
            "timing_provenance": "camera_pts_host_clock.v1",
            "captured_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            "received_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            "frame_width": self.width,
            "frame_height": self.height,
            "jpeg_sha256": hashlib.sha256(jpeg).hexdigest(),
        }
        saved = self.runtime.evidence.save_source_frame_v2(
            device_id=self.device, generation=self.generation,
            seq=self.capture_seq, jpeg=jpeg, record=record)
        saved["jpeg_bytes"] = jpeg
        saved["received_monotonic_ms"] = self.mono_ns // 1_000_000 + 40
        self.archived.append(saved)
        return saved

    def provider(self):
        """Fleet 受控采集提供器同款逻辑（app 层接线一致）：取最近归档帧。"""

        def capture(device_id: str, count: int = 2, page_state=None) -> list:
            if device_id != self.device:
                return []
            rows = self.runtime.evidence.latest_source_frames_v2(
                self.device, count)
            frames = []
            for row in rows:
                data = self.runtime.evidence.read_source_frame_bytes(
                    row["evidence_id"])
                frames.append(self._frame(row, data))
            return frames

        return capture

    def _frame(self, row: dict, data: bytes) -> dict:
        return {
            "evidence_id": row["evidence_id"],
            "jpeg": data,
            "installation_binding": {
                "camera_id": row["camera_id"],
                "camera_device_unique_id": row["device_unique_id"],
                "camera_binding_id": row["camera_binding_id"],
                "arm_binding_id": row.get("arm_binding_id")
                or self.binding["arm_binding_id"]},
            "source_size": {"width": row["frame_width"],
                            "height": row["frame_height"]},
            "source_geometry_id": row["source_geometry_id"],
            "source_generation_id": row["source_generation_id"],
            "source_frame_seq": row["source_frame_seq"],
            "source_capture_seq": row["source_capture_seq"],
            "source_clock_id": row["source_clock_id"],
            "clock_registration_id": "22222222-2222-4222-8222-222222222222",
            "source_captured_monotonic_ns": row["source_captured_monotonic_ns"],
            "source_capture_uncertainty_ms":
                row.get("source_capture_uncertainty_ms", 1.0),
            "source_timing_provenance": row["timing_provenance"],
            "received_monotonic_ms": row.get("received_monotonic_ms",
                                             row["source_captured_monotonic_ns"] // 1_000_000 + 40),
        }


def write_v3_bundle(tmp_path: Path, *, device: str = "arm-01",
                    camera_binding_id: str = BINDING["camera_binding_id"],
                    geometry: str = GEOMETRY_ID,
                    screen_size: tuple[int, int] = (1000, 2000),
                    homography: list | None = None) -> Path:
    """真实 v3 校准档案（本台有效 v3；save_bundle 走正式落盘路径）。

    R3-12：四态匹配按本台 v3 cameraToScreen 单应摆正；默认恒等（相机域
    1000x2000 = 屏幕域）；透视场景由 R3-12/G5-D 传入非恒等单应。"""
    from drivers import calibration_bundle as cb
    if homography is not None:
        camera_to_screen = {"homography": list(homography)}
    else:
        camera_to_screen = {"homography": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0,
                                           0.0, 0.0, 1.0]}
    bundle = {
        "schemaVersion": 3, "status": "ready",
        "deviceId": device, "daemonSession": "sess-1",
        "cameraId": BINDING["camera_id"],
        "cameraDeviceUniqueId": BINDING["camera_device_unique_id"],
        "cameraBindingId": camera_binding_id, "sourceGeometryId": geometry,
        "coordinateFrameHash": "c" * 64,
        "bindingFingerprint": "d" * 64,
        "bindingRevision": 7,
        "frame": {"width": 1000, "height": 2000},
        "camera": {"validated": True,
                   "matrix": [[600.0, 0, 320], [0, 600.0, 240], [0, 0, 1]],
                   "cameraToScreen": camera_to_screen},
        "screen": {"screenWidth": screen_size[0],
                   "screenHeight": screen_size[1],
                   "widthPx": screen_size[0], "heightPx": screen_size[1]},
        "xy": {"a": 1.0, "b": 0.0, "c": 0.0, "d": 0.0, "e": 1.0, "f": 0.0},
        "z": {"liftMm": 5.0},
        "safePark": {"x": 10.0, "y": 10.0, "z": 20.0},
        "targets": {}, "acceptance": {"debug": False, "wechat": False},
    }
    data_root = tmp_path / "data"  # 与 app 解析（config.root/data）一致
    cb.save_bundle(bundle, data_root)
    return data_root


def seed_template_asset_png(width: int = 40, height: int = 40, seed: int = 1) -> bytes:
    rng = np.random.default_rng(seed)
    tile = ((rng.integers(0, 256, size=(height, width), dtype=np.uint16)
             // 4 * 4)).astype(np.uint8)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR))
    assert ok
    return buf.tobytes()


# ---------------- 冻结链构造器（auth/wire 测试共用；全部真实 HTTP） ----------------
import base64 as _b64
import copy as _copy
import hashlib as _hashlib
import json as _json
import urllib.parse as _urlparse

D2_STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty", "chat_ime_text")


def _cv2_imdecode(data: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def scene_frames_for_page(bridge, page_assets: dict, page: dict, *,
                          homography: list | None = None,
                          camera_size: tuple[int, int] | None = None) -> None:
    """按页定义把锚点资产原尺寸贴到 1000x2000 画布并归档两帧（真实匹配）。

    region 矩形内绘制确定性伪文本条纹（真实页面区域有内容纹理——颜色
    采样要求区域非低纹理；合成页同理，不把结果直接指定为通过）。
    R3-12：传 homography（相机→屏幕 3x3/9 元）+ camera_size 时，画布先轻
    模糊再经单应**逆变换**投影为相机域画面（真实透视相机帧）。
    """
    canvas = np.full((2000, 1000, 3), 24, dtype=np.uint8)
    rng = np.random.default_rng(7)
    for region in page.get("regions") or []:
        rect = region.get("relative_rect", {})
        anchor = next((a for a in page["anchors"]
                       if a["id"] == region.get("anchor_id")), None)
        if anchor is None:
            continue
        ox = int(round((anchor["reference_rect"]["x"]
                        + rect.get("dx", 0.0)) * 1000))
        oy = int(round((anchor["reference_rect"]["y"]
                        + rect.get("dy", 0.0)) * 2000))
        w = int(round(rect.get("width", 0.0) * 1000))
        h = int(round(rect.get("height", 0.0) * 2000))
        if w <= 4 or h <= 4:
            continue
        rows_needed = (h + 5) // 6
        stripes = (rng.integers(0, 2, size=(rows_needed, 1),
                                    dtype=np.uint8) * 170 + 40).astype(np.uint8)
        block = np.repeat(stripes, 6, axis=0)[:h]
        block = np.repeat(block, w, axis=1)[:, :w]
        canvas[oy:oy + h, ox:ox + w, 0] = block
        canvas[oy:oy + h, ox:ox + w, 1] = block
        canvas[oy:oy + h, ox:ox + w, 2] = block
    for anchor in page["anchors"]:
        tile = _cv2_imdecode(page_assets[anchor["asset_id"]])
        x = int(round(anchor["reference_rect"]["x"] * 1000))
        y = int(round(anchor["reference_rect"]["y"] * 2000))
        h, w = tile.shape[:2]
        canvas[y:y + h, x:x + w] = tile
    if homography is not None and camera_size is not None:
        import cv2 as _cv2
        matrix = np.asarray(homography, dtype=float).reshape(3, 3)
        inverse = np.linalg.inv(matrix)
        blurred = _cv2.GaussianBlur(canvas, (5, 5), 1.1)
        canvas = _cv2.warpPerspective(blurred, inverse,
                                      (int(camera_size[0]),
                                       int(camera_size[1])),
                                      flags=_cv2.INTER_LINEAR,
                                      borderValue=(18, 18, 18))
    bridge.push(canvas)
    bridge.push(canvas)


def fixture_page_definitions() -> dict:
    """四页定义（与冻结构造器 completion.py::page(state) 同构，asset 固定
    anchor-tile；每页两分离必需锚点 + 各态最低项 targets/regions）。"""

    def box(x, y, w, h):
        return {"x": x, "y": y, "width": w, "height": h}

    def anchor(name, x, y):
        return {"id": name, "asset_id": "anchor-tile",
                "search_region": box(max(0, x - .03), max(0, y - .03), .10, .08),
                "reference_rect": box(x, y, .04, .02), "required": True}

    def item(name, rect, field, value):
        origin = box(.03, .04, .04, .02)
        result = {"id": name, "anchor_id": "upper", "relative_rect": {
            "dx": round(rect["x"] - origin["x"], 6),
            "dy": round(rect["y"] - origin["y"], 6),
            "width": rect["width"], "height": rect["height"]}}
        result[field] = value
        return result

    def page(state):
        second = ("lower", .90) if state in D2_STATES[:2] \
            else ("keyboard_boundary", .72)
        anchors = [anchor("upper", .03, .04), anchor(second[0], .03, second[1])]
        if state == "chat_ime_text":
            anchors.append(anchor("send_button_feature", .85, .63))
        if state == "wechat_list":
            targets = [item("wechat_chats_tab", box(.05, .92, .1, .04),
                            "mode", "tap")]
            regions = [item("unread", box(.1, .89, .05, .03), "purpose", "unread"),
                       item("content", box(.05, .14, .9, .70), "purpose", "content")]
        else:
            targets = [item("back_button", box(.02, .05, .06, .04), "mode", "tap"),
                       item("input_box", box(.12, .62, .65, .06), "mode", "tap")]
            if state == "chat_ime_text":
                targets.append(item("send_button", box(.84, .63, .1, .04),
                                    "mode", "tap"))
            regions = [item("title", box(.12, .04, .7, .06), "purpose", "title"),
                       item("input", box(.12, .62, .65, .06), "purpose", "input"),
                       item("content", box(.05, .14, .9, .45), "purpose", "content")]
        return {"state": state, "anchors": anchors, "targets": targets,
                "regions": regions}

    return {state: page(state) for state in D2_STATES}


def fixture_page_assets() -> dict:
    examples, _cases = load_contract().all_fixtures()
    upload = examples["http-draft_asset_upload-request"]["body"]
    tile = _b64.b64decode(upload["content_base64"])
    return {"anchor-tile": tile,
            "empty-input": seed_template_asset_png(650, 120, seed=3)}


def publish_template_chain(env, principal: str, *,
                           template_id: str = "wechat-controlled") -> dict:
    """draft_create → 资产上传 → 四页（真实采集+匹配）→ template_publish。"""
    c = env.client
    headers = {**env.auth(principal), "Content-Type": "application/json"}
    bridge = env.bridge
    page_assets = fixture_page_assets()
    page_defs = fixture_page_definitions()
    created = c.post("/v1/layout-template-drafts", json={
        "client_request_id": str(uuid.uuid4()), "device_id": "arm-01",
        "base_template_ref": None,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert created.status_code == 200, created.text
    draft_id = created.json()["draft_id"]
    tile = page_assets["anchor-tile"]
    upload = c.post(f"/v1/layout-template-drafts/{draft_id}/assets", json={
        "client_request_id": str(uuid.uuid4()), "expected_revision": 1,
        "asset": {"id": "anchor-tile",
                  "relative_path": "assets/anchor-tile.png",
                  "sha256": _hashlib.sha256(tile).hexdigest(),
                  "media_type": "image/png", "width": 40, "height": 40},
        "content_base64": _b64.b64encode(tile).decode("ascii"),
        "source_evidence_ids": [], "privacy_confirmed": True,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert upload.status_code == 200, upload.text
    revision = upload.json()["revision"]
    for state in D2_STATES:
        scene_frames_for_page(bridge, page_assets, page_defs[state])
        capture = c.post("/v1/devices/arm-01/layout-captures", json={
            "client_request_id": str(uuid.uuid4()), "owner_kind": "draft",
            "owner_id": draft_id, "expected_owner_revision": 1,
            "page_state": state, "controlled_test_confirmed": True,
            "contract_version": "layout-capture.v1",
            "correlation_id": str(uuid.uuid4())}, headers=headers)
        assert capture.status_code == 200, capture.text
        evidence_ids = [e["evidence_id"]
                        for e in capture.json()["capture"]["evidence"]]
        written = c.put(
            f"/v1/layout-template-drafts/{draft_id}/pages/{state}",
            json={"client_request_id": str(uuid.uuid4()),
                  "expected_revision": revision,
                  "source_evidence_ids": evidence_ids,
                  "page_definition": page_defs[state],
                  "contract_version": "ui-layout-edit.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=headers)
        assert written.status_code == 200, written.text
        revision = written.json()["revision"]
    published = c.post("/v1/layout-templates", json={
        "client_request_id": str(uuid.uuid4()), "template_id": template_id,
        "expected_parent_version": None, "draft_id": draft_id,
        "expected_draft_revision": revision,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert published.status_code == 200, published.text
    ref = published.json()["template_ref"]
    return {"template_id": ref["id"], "sha256": ref["sha256"],
            "draft_id": draft_id, "version": ref["version"]}


def publish_adaptation_chain(env, principal: str, template: dict) -> dict:
    """session → capture×4 → page×4 → preview → publish（真实 HTTP + 匹配）。"""
    c = env.client
    headers = {**env.auth(principal), "Content-Type": "application/json"}
    bridge = env.bridge
    page_assets = fixture_page_assets()
    page_defs = fixture_page_definitions()
    session = c.post("/v1/devices/arm-01/layout-adaptation/sessions", json={
        "client_request_id": str(uuid.uuid4()),
        "template_ref": {"id": template["template_id"], "version": 1,
                         "sha256": template["sha256"]},
        "expected_revision": 0, "keyboard_geometry_mode": "live_ime_v1",
        "inherit_session_id": None, "inherit_states": [],
        "contract_version": "device-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert session.status_code == 200, session.text
    session_id = session.json()["session"]["capture_session_id"]
    revision = 1
    for state in D2_STATES:
        scene_frames_for_page(bridge, page_assets, page_defs[state])
        capture = c.post("/v1/devices/arm-01/layout-captures", json={
            "client_request_id": str(uuid.uuid4()), "owner_kind": "session",
            "owner_id": session_id, "expected_owner_revision": revision,
            "page_state": state, "controlled_test_confirmed": True,
            "contract_version": "layout-capture.v1",
            "correlation_id": str(uuid.uuid4())}, headers=headers)
        assert capture.status_code == 200, capture.text
        evidence_ids = [e["evidence_id"]
                        for e in capture.json()["capture"]["evidence"]]
        written = c.put(
            f"/v1/devices/arm-01/layout-adaptation/sessions/{session_id}"
            f"/pages/{state}",
            json={"client_request_id": str(uuid.uuid4()),
                  "expected_session_revision": revision,
                  "source_evidence_ids": evidence_ids,
                  "local_overrides": [],
                  "contract_version": "device-layout-edit.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=headers)
        assert written.status_code == 200, written.text
        revision = written.json()["session_revision"]
    preview = c.post("/v1/devices/arm-01/layout-adaptation/preview", json={
        "capture_session_id": session_id,
        "expected_session_revision": revision,
        "contract_version": "device-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert preview.status_code == 200, preview.text
    publish = c.put("/v1/devices/arm-01/layout-adaptation", json={
        "client_request_id": str(uuid.uuid4()), "expected_revision": 0,
        "preview_id": preview.json()["preview_id"],
        "accepted_items": preview.json()["accepted_items"],
        "contract_version": "device-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert publish.status_code == 200, publish.text
    return publish.json()


def export_template_package(env, principal: str, template: dict) -> dict:
    c = env.client
    export = c.post("/v1/layout-packages/export", json={
        "client_request_id": str(uuid.uuid4()),
        "roots": [{"kind": "template",
                   "ref": {"id": template["template_id"], "version": 1,
                           "sha256": template["sha256"]}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())},
        headers=env.auth(principal))
    assert export.status_code == 200, export.text
    return export.json()
