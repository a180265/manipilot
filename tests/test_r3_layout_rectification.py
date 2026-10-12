"""R3-12：四态按本台摆正证据匹配（相机域原图 → 本台 v3 摆正 → 屏幕域模板区域）。

缺陷（修前）：`layout_adaptation.py::_match_page`（经 write_page）直接把相机域
原图交给以屏幕域为基准的模板区域算 check——无本台 v3 cameraToScreen 摆正。
标准画布直接被当 camera frame；屏幕域 search_region 对相机原图失去意义。

本文件自备全部夹具（不依赖 layout_r2_helpers 的校准提供器）：
- 真实非恒等透视：1080x2400 屏幕内容经单应逆变换合成 640x480 相机画面
  （偏移 + 缩放 + 透视），v3 档案携带 cameraToScreen 单应（相机→屏幕）；
- 全部走真实服务链：受控采集（两帧/采样推进/新鲜度）→ 服务端摆正 →
  matchTemplate 逐锚点 → 颜色采样 → 预览 → CAS 发布；不指定任何结果。

断言（缺陷卡 R3-12 A/B/C/D）：
- A：透视合成机在大相机画面中偏移/缩放/透视，走真实摆正/匹配，四态合法成功；
- B：错误本台 v3（错单应/错绑定/旧来源几何）、相似环境干扰（锚点歧义）、
  标准画布直接当 camera frame，均拒绝错误通过；
- C：失败一个锚点只阻断依赖项，不损坏不依赖的可用项（部分通过保留）；
- D：第二台不同透视独立校验（独立 Store/独立 v3/独立单应），同样成功。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import cv2
import numpy as np
import pytest

from runtime.layout_adaptation import PAGE_STATES, AdaptationError, LayoutAdaptationService
from runtime.layout_ops import LayoutOpsService
from runtime.layout_state import (
    LayoutStateStore,
    content_sha,
    state_identity,
    state_key,
)
from runtime.layout_template import LayoutTemplateService, LayoutTemplateStore

SCREEN_W, SCREEN_H = 1080, 2400
CAM_W, CAM_H = 640, 480
TILE = 72
BINDING = {"camera_id": "cam-persp",
           "camera_device_unique_id": "uvc-persp",
           "camera_binding_id": "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaa1",
           "arm_binding_id": "arm-bind-persp"}
GEOMETRY_ID = "b" * 64


# ---------------- 合成内容：粗棋盘锚点砖 + 条纹区域 ----------------
def make_tile(seed: int, size: int = TILE) -> np.ndarray:
    """粗棋盘纹理（大尺度结构可透过相机下采样+透视重采样+JPEG 保留）。"""
    cell = 24
    grid = max(1, size // cell)
    cell = size // grid
    phase = seed % 4
    tile = np.full((size, size), 60, dtype=np.uint8)
    for gy in range(grid):
        for gx in range(grid):
            on = (gx + gy + phase) % 2 == 0
            block = 215 if on else 55
            tile[gy * cell:(gy + 1) * cell, gx * cell:(gx + 1) * cell] = block
    rng = np.random.default_rng(seed)
    noise = rng.integers(-6, 7, size=(size, size), dtype=np.int16)
    tile = np.clip(tile.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)


PAGE_ANCHORS = {
    "wechat_list": ["wechat_chats_tab", "list_row_anchor"],
    "chat_ime_hidden": ["back_button", "input_box"],
    "chat_ime_empty": ["back_button", "input_box", "keyboard_boundary"],
    "chat_ime_text": ["back_button", "input_box", "keyboard_boundary",
                      "send_button", "send_button_feature"],
}

REGIONS = {
    "wechat_list": [("unread", "wechat_chats_tab", 0.0, 0.25, 0.2, 0.04),
                    ("content", "wechat_chats_tab", 0.25, 0.25, 0.5, 0.4)],
    "chat_ime_hidden": [("title", "back_button", 0.55, 0.0, 0.3, 0.03),
                        ("input", "input_box", -0.02, 0.30, 0.6, 0.05),
                        ("content", "back_button", 0.05, 0.45, 0.6, 0.4)],
    "chat_ime_empty": [("title", "back_button", 0.55, 0.0, 0.3, 0.03),
                       ("input", "input_box", -0.02, 0.30, 0.6, 0.05),
                       ("content", "back_button", 0.05, 0.45, 0.6, 0.4)],
    "chat_ime_text": [("title", "back_button", 0.55, 0.0, 0.3, 0.03),
                      ("input", "input_box", -0.02, 0.30, 0.6, 0.05),
                      ("content", "back_button", 0.05, 0.45, 0.6, 0.4)],
}


def build_assets() -> dict[str, np.ndarray]:
    names = sorted({name for names in PAGE_ANCHORS.values() for name in names})
    return {name: make_tile(11 + i) for i, name in enumerate(names)}


def build_pages(assets: dict[str, np.ndarray]) -> list[dict]:
    """屏幕域四页定义：锚点带**紧 search_region**（±4%），区域相对锚点。"""
    pages = []
    for state, names in PAGE_ANCHORS.items():
        anchors = []
        for i, name in enumerate(names):
            x = 0.03 + i * 0.10
            y = 0.04 if i < 3 else 0.20
            anchors.append({
                "id": name, "asset_id": f"asset-{name}",
                "search_region": {"x": max(0.0, x - 0.04),
                                  "y": max(0.0, y - 0.04),
                                  "width": 0.16, "height": 0.12},
                "reference_rect": {"x": x, "y": y,
                                   "width": TILE / SCREEN_W,
                                   "height": TILE / SCREEN_H},
                "required": True})
        regions = []
        for r_id, anchor_id, dx, dy, w, h in REGIONS[state]:
            regions.append({
                "id": r_id, "anchor_id": anchor_id,
                "relative_rect": {"dx": dx, "dy": dy,
                                  "width": w, "height": h},
                "purpose": r_id})
        pages.append({"state": state, "anchors": anchors,
                      "targets": [], "regions": regions})
    return pages


def page_by_state(pages: list[dict], state: str) -> dict:
    return next(p for p in pages if p["state"] == state)


def draw_screen_canvas(page: dict, assets: dict[str, np.ndarray], *,
                       duplicate: str | None = None,
                       blank_anchor: str | None = None) -> np.ndarray:
    """屏幕域画布：区域伪文本条纹 + 锚点砖按 reference_rect 摆放。"""
    canvas = np.full((SCREEN_H, SCREEN_W, 3), 200, dtype=np.uint8)
    rng = np.random.default_rng(5)
    anchors_by_id = {a["id"]: a for a in page["anchors"]}
    for region in page["regions"]:
        anchor = anchors_by_id[region["anchor_id"]]
        rel = region["relative_rect"]
        ox = int(round((anchor["reference_rect"]["x"]
                        + rel["dx"]) * SCREEN_W))
        oy = int(round((anchor["reference_rect"]["y"]
                        + rel["dy"]) * SCREEN_H))
        w = int(round(rel["width"] * SCREEN_W))
        h = int(round(rel["height"] * SCREEN_H))
        rows_needed = (h + 5) // 6
        stripes = (rng.integers(0, 2, size=(rows_needed, 1), dtype=np.uint8)
                   * 150 + 55).astype(np.uint8)
        block = np.repeat(stripes, 6, axis=0)[:h]
        block = np.repeat(block, w, axis=1)[:, :w]
        canvas[oy:oy + h, ox:ox + w, 0] = block
        canvas[oy:oy + h, ox:ox + w, 1] = block
        canvas[oy:oy + h, ox:ox + w, 2] = block
    for anchor in page["anchors"]:
        image = assets[anchor["id"]]
        if anchor["id"] == blank_anchor:
            image = np.full((TILE, TILE, 3), 200, dtype=np.uint8)
        x = int(round(anchor["reference_rect"]["x"] * SCREEN_W))
        y = int(round(anchor["reference_rect"]["y"] * SCREEN_H))
        canvas[y:y + TILE, x:x + TILE] = image
        if anchor["id"] == duplicate:
            # 相似环境干扰：同一锚点砖在 search_region 内的分离位置再放一份
            dx = int(0.052 * SCREEN_W)
            dy = int(0.04 * SCREEN_H)
            canvas[y + dy:y + dy + TILE, x + dx:x + dx + TILE] = image
    return canvas


# ---------------- 透视相机合成：屏幕 → 相机（偏移+缩放+透视） ----------------
def camera_quad(tag: int) -> np.ndarray:
    """不同设备不同透视：非平行四边形（真透视），偏移/缩放各机不同。"""
    quads = {
        1: np.array([[42, 34], [612, 58], [630, 452], [24, 428]],
                    dtype=np.float32),
        2: np.array([[28, 60], [600, 30], [626, 430], [46, 452]],
                    dtype=np.float32),
    }
    return quads[tag]


def homography(tag: int) -> np.ndarray:
    """cameraToScreen：相机像素 → 屏幕像素（真实非恒等透视）。"""
    screen_corners = np.array(
        [[0, 0], [SCREEN_W, 0], [SCREEN_W, SCREEN_H], [0, SCREEN_H]],
        dtype=np.float32)
    return cv2.getPerspectiveTransform(camera_quad(tag), screen_corners)


def homography_flat(tag: int) -> list[float]:
    return [float(v) for v in homography(tag).reshape(-1)]


def camera_jpeg(screen_canvas: np.ndarray, tag: int,
                quality: int = 96) -> bytes:
    """屏幕内容经单应逆变换投影为相机画面（偏移+缩放+透视）。

    投影前轻量光学模糊（模拟真实镜头/抗混叠），避免硬核下采样混叠。"""
    inverse = cv2.getPerspectiveTransform(
        np.array([[0, 0], [SCREEN_W, 0], [SCREEN_W, SCREEN_H],
                  [0, SCREEN_H]], dtype=np.float32),
        camera_quad(tag))
    blurred = cv2.GaussianBlur(screen_canvas, (5, 5), 1.1)
    # A visible margin survives anisotropic camera downsampling at every edge.
    blurred = cv2.copyMakeBorder(blurred, 24, 24, 8, 8, cv2.BORDER_CONSTANT,
                                 value=(200, 200, 200))
    offset = np.float64([[1, 0, -8], [0, 1, -24], [0, 0, 1]])
    frame = cv2.warpPerspective(blurred, inverse @ offset, (CAM_W, CAM_H),
                                flags=cv2.INTER_LINEAR,
                                borderValue=(18, 18, 18))
    ok, buf = cv2.imencode(".jpg", frame,
                           [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok
    return buf.tobytes()


# ---------------- 受控采集提供器（相机域两帧 + 真实采样推进） ----------------
class CameraProvider:
    def __init__(self, frames_by_state: dict[str, bytes], *,
                 geometry_id: str = GEOMETRY_ID):
        self.frames = frames_by_state
        self.geometry_id = geometry_id
        self.seq = 0
        self.capture_seq = 0
        self.mono_ns = 1_000_000_000_000
        self.generation = "cccccccc-1111-4ccc-8ccc-cccccccccccc"
        self.archive: dict[str, bytes] = {}
        self.calls: list[tuple] = []

    def _one(self, state: str) -> dict:
        self.seq += 1
        self.capture_seq += 1
        self.mono_ns += 250_000_000
        jpeg = self.frames[state]
        evidence_id = f"cam-{uuid.uuid4().hex[:12]}"
        self.archive[evidence_id] = jpeg
        return {
            "evidence_id": evidence_id, "jpeg": jpeg,
            "installation_binding": dict(BINDING),
            "source_size": {"width": CAM_W, "height": CAM_H},
            "source_geometry_id": self.geometry_id,
            "source_generation_id": self.generation,
            "source_frame_seq": self.seq,
            "source_capture_seq": self.capture_seq,
            "source_clock_id": "mach-absolute:" + "0" * 63 + "7",
            "clock_registration_id": "dddddddd-2222-4ddd-8ddd-dddddddddddd",
            "source_captured_monotonic_ns": self.mono_ns,
            "source_capture_uncertainty_ms": 2.0,
            "source_timing_provenance": "camera_pts_host_clock.v1",
            "received_monotonic_ms": self.mono_ns // 1_000_000 + 60,
        }

    def __call__(self, device_id, count=2, page_state=None):
        self.calls.append((device_id, page_state, count))
        key = page_state if page_state in self.frames else next(
            iter(self.frames))
        return [self._one(key) for _ in range(count)]

    def read(self, evidence_id: str) -> bytes:
        return self.archive[evidence_id]


class V3Calibration:
    """本台 v3 校准事实（含 cameraToScreen 摆正几何）；字段可注入错误。"""

    def __init__(self, tag: int = 1, *, camera_binding_id=None,
                 source_geometry_id: str = GEOMETRY_ID,
                 homography_override: list[float] | None = None,
                 include_homography: bool = True):
        self.tag = tag
        self.camera_binding_id = camera_binding_id or BINDING["camera_binding_id"]
        self.source_geometry_id = source_geometry_id
        self.homography_override = homography_override
        self.include_homography = include_homography
        from runtime.calibration_gate import _quad_of
        drawn_edge = np.float32([
            [-8, -24], [SCREEN_W + 7, -24],
            [SCREEN_W + 7, SCREEN_H + 23], [-8, SCREEN_H + 23]])
        expected = cv2.perspectiveTransform(
            drawn_edge[None], np.linalg.inv(homography(tag)))[0].tolist()
        self.corners = _quad_of(camera_jpeg(
            np.full((SCREEN_H, SCREEN_W, 3), 200, dtype=np.uint8), tag), expected)
        assert self.corners is not None

    def __call__(self, device_id: str) -> dict:
        camera: dict = {"validated": True, "matrix": [[600.0, 0, 320], [0, 600.0, 240],
                                   [0, 0, 1]]}
        if self.include_homography:
            camera["cameraToScreen"] = {
                "homography": (self.homography_override
                               if self.homography_override is not None
                               else homography_flat(self.tag))}
        return {"schemaVersion": 3, "status": "ready", "deviceId": device_id,
                "cameraId": BINDING["camera_id"],
                "cameraDeviceUniqueId": BINDING["camera_device_unique_id"],
                "cameraBindingId": self.camera_binding_id,
                "sourceGeometryId": self.source_geometry_id,
                "coordinateFrameHash": "e" * 64,
                "bindingFingerprint": "f" * 64,
                "frame": {"width": CAM_W, "height": CAM_H},
                "screen": {"screenWidth": SCREEN_W, "screenHeight": SCREEN_H,
                           "edgeModel": {"corners": self.corners,
                                         "tolerancePx": 8},
                           "orientation": "portrait"},
                "camera": camera}


class FakeClock:
    def __init__(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 10, 5, 12, 0, 0,
                            tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta
        self.now = self.now + timedelta(seconds=seconds)


class LegacyKV:
    def __init__(self, path: Path):
        import sqlite3
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("CREATE TABLE IF NOT EXISTS layout_kv "
                           "(k TEXT PRIMARY KEY, json TEXT)")
        self._conn.commit()

    def save(self, key, record):
        import json
        self._conn.execute("INSERT OR REPLACE INTO layout_kv VALUES (?, ?)",
                           (key, json.dumps(record, ensure_ascii=False)))
        self._conn.commit()

    def load(self, key):
        import json
        row = self._conn.execute("SELECT json FROM layout_kv WHERE k=?",
                                 (key,)).fetchone()
        return json.loads(row[0]) if row else None


# ---------------- 装配：真实服务 + 模板 catalog 提交（R3-13 兼容种子） ----------------
class Rig:
    def __init__(self, root: Path, tag: int = 1, *, provider=None,
                 calibration=None, device: str = "arm-p1"):
        self.root = root
        self.device = device
        self.clock = FakeClock()
        self.db = root / "layout.db"
        self.kv = LegacyKV(self.db)
        self.state = LayoutStateStore(self.db, data_root=root / "data",
                                      clock=self.clock)
        self.template_store = LayoutTemplateStore(self.db)
        self.region_store = LayoutRegionStoreLite(self.db)
        self.provider = provider
        self.calibration = calibration or V3Calibration(tag)
        self._region_seed(device)
        self.instance = f"inst-r312-{tag}-{uuid.uuid4().hex[:8]}"
        self.svc = LayoutAdaptationService(
            store=self.kv,
            template_store=self.template_store,
            state_store=self.state,
            clock=self.clock,
            server_instance_id=self.instance,
            capture_provider=provider,
            calibration_provider=self.calibration,
            frame_reader=provider.read if provider else None,
            region_ref_provider=self._region_ref,
            region_record_provider=self._region_record,
            binding_provider=lambda device: {
                "camera_id": BINDING["camera_id"],
                "camera_binding_id": BINDING["camera_binding_id"],
                "arm_binding_id": BINDING["arm_binding_id"],
                "camera_device_unique_id": BINDING["camera_device_unique_id"],
                "source_geometry_id": GEOMETRY_ID,
                "source_size": {"width": CAM_W, "height": CAM_H},
                "ime_device_id": "k40-p",
                "screen_descriptor": {
                    "width_px": SCREEN_W, "height_px": SCREEN_H,
                    "orientation": "portrait",
                    "system_insets": {"left": 0, "top": 0, "right": 0,
                                      "bottom": 0},
                    "calibration_geometry_hash": "e" * 64}})

    def _region_ref(self, device_id: str):
        record = self._region_record(device_id)
        if record is None:
            return None
        return {"id": device_id, "version": record["version"],
                "sha256": record["sha256"]}

    def _region_record(self, device_id: str):
        pointer = self.region_store.get_pointer(device_id)
        if pointer is None:
            return None
        record = self.region_store.get_version(
            device_id, int(pointer["selected_version"]))
        if record is None:
            return None
        assert record["sha256"] == pointer["selected_sha256"]
        return record

    def _region_seed(self, device: str) -> None:
        from runtime.layout_region import canonical_digest
        record = {"contract_version": "phone-search-region.v1",
                  "device_id": device, "version": 1,
                  "installation_binding": dict(BINDING),
                  "source_size": {"width": CAM_W, "height": CAM_H},
                  "source_geometry_id": GEOMETRY_ID,
                  # Leave support beyond both visible, perspective screen edges.
                  "rect": {"x": 0.01, "y": 0.02, "width": 0.99, "height": 0.96},
                  "reference_evidence_id": "seed-ev",
                  "created_at": "2026-10-05T00:00:00.000Z",
                  "created_by": "operator"}
        record["sha256"] = canonical_digest(record)
        self.region_store.insert_version(device, 1, record)
        if self.region_store.get_pointer(device) is None:
            self.region_store.cas_pointer(device, 0, 1, record["sha256"],
                                          "seed")

    def seed_template(self, template_id: str, pages: list[dict],
                      assets: dict[str, np.ndarray]) -> dict:
        """真实发布模板（原字节资产）+ catalog 提交（统一可见性兼容）。"""
        tpl = LayoutTemplateService(self.template_store)
        draft = tpl.create_draft({"template_id": template_id})
        for name, image in assets.items():
            ok, buf = cv2.imencode(".png", image)
            self.template_store.save_asset(draft["draft_id"],
                                           f"asset-{name}", buf.tobytes())
        pages_dict = {p["state"]: p for p in pages}
        tpl.publish({"template_id": template_id, "version": 1,
                     "pages": pages_dict, "metadata": {},
                     "draft_id": draft["draft_id"]})
        stored = self.template_store.get_template(template_id, 1)
        sha = stored["sha256"]
        row = {"resource": {"kind": "template",
                            "ref": {"id": template_id, "version": 1,
                                    "sha256": sha}},
               "operation_id": "seed-template",
               "created_at": "2026-10-05T00:00:00.000Z"}
        self.state.state_insert(
            "catalog", state_key("catalog", ["template", template_id, 1]),
            None, None, row)
        return {"id": template_id, "version": 1, "sha256": sha}

    def close(self):
        self.svc.close()
        self.state.close()
        self.template_store.close()
        self.region_store.close()
        self.kv._conn.close()


class LayoutRegionStoreLite:
    """轻量区域版本存储（同库文件独立连接；只为 installation_region_ref）。"""

    def __init__(self, db_path: Path):
        import sqlite3
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS layout_region_versions (
                device_id TEXT NOT NULL, version INTEGER NOT NULL,
                sha256 TEXT NOT NULL, json TEXT NOT NULL,
                PRIMARY KEY (device_id, version));
            CREATE TABLE IF NOT EXISTS layout_region_pointer (
                device_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                selected_version INTEGER NOT NULL,
                selected_sha256 TEXT NOT NULL, updated_at TEXT NOT NULL,
                operation_id TEXT NOT NULL);
        """)
        self._conn.commit()

    def insert_version(self, device_id, version, record):
        import json
        self._conn.execute(
            "INSERT OR IGNORE INTO layout_region_versions VALUES (?,?,?,?)",
            (device_id, version, record["sha256"],
             json.dumps(record, ensure_ascii=False)))
        self._conn.commit()

    def get_pointer(self, device_id):
        return self._conn.execute(
            "SELECT * FROM layout_region_pointer WHERE device_id=?",
            (device_id,)).fetchone()

    def get_version(self, device_id, version):
        import json
        row = self._conn.execute(
            "SELECT json FROM layout_region_versions "
            "WHERE device_id=? AND version=?", (device_id, version)).fetchone()
        return json.loads(row[0]) if row else None

    def cas_pointer(self, device_id, expected, version, sha, op_id):
        row = self.get_pointer(device_id)
        current = int(row["revision"]) if row else 0
        assert current == expected
        self._conn.execute(
            "INSERT INTO layout_region_pointer VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET revision=excluded.revision,"
            "selected_version=excluded.selected_version,"
            "selected_sha256=excluded.selected_sha256",
            (device_id, current + 1, version, sha,
             "2026-10-05T00:00:00.000Z", op_id))
        self._conn.commit()

    def close(self):
        self._conn.close()


def build_provider(tag: int, *, duplicate: str | None = None,
                   blank_anchor: str | None = None) -> CameraProvider:
    assets = build_assets()
    frames = {}
    for state in PAGE_STATES:
        canvas = draw_screen_canvas(page_by_state(build_pages(assets), state),
                                    assets, duplicate=duplicate,
                                    blank_anchor=blank_anchor)
        frames[state] = camera_jpeg(canvas, tag)
    return CameraProvider(frames)


def run_four_state(rig: Rig, template_ref: dict, *,
                   principal: str = "operator",
                   installation_binding=None) -> dict:
    """真实四态：会话 → 四页（服务端摆正+匹配）→ 预览 → 发布。"""
    svc = rig.svc
    device = rig.device
    sid = svc.start_session(device, {
        "template_ref": template_ref, "principal_id": principal,
        "installation_binding": installation_binding or BINDING})["session_id"]
    checks = {}
    revision = 1
    for state in PAGE_STATES:
        capture = svc.capture(device, {
            "client_request_id": str(uuid.uuid4()),
            "owner_kind": "session", "owner_id": sid,
            "expected_owner_revision": revision, "page_state": state,
            "controlled_test_confirmed": True})
        view = svc.write_page(device, sid, state, {
            "client_request_id": str(uuid.uuid4()),
            "expected_session_revision": revision,
            "source_evidence_ids": capture["source_evidence_ids"],
            "local_overrides": []})
        checks[state] = view["check"]
        revision = view["session_revision"]
    return {"session_id": sid, "checks": checks, "revision": revision}


# ================= A：透视合成机走真实摆正/匹配，四态合法成功 =================
def test_r3_12_a_perspective_phone_rectified_four_state_verified(tmp_path):
    rig = Rig(tmp_path / "rt-a", tag=1,
              provider=build_provider(1))
    try:
        template = rig.seed_template("tpl-persp", build_pages(build_assets()),
                                     build_assets())
        result = run_four_state(rig, template)
        for state in PAGE_STATES:
            check = result["checks"][state]
            assert check["status"] == "passed", (
                state, check.get("reason_codes"),
                [(a["id"], a["status"], a.get("reason_code"),
                  a.get("best_score")) for a in check["anchor_results"]])
        preview = rig.svc.preview(rig.device,
                                  {"capture_session_id": result["session_id"]})
        assert preview["failed_items"] == []
        record = rig.svc.publish(rig.device, {
            "client_request_id": str(uuid.uuid4()),
            "session_id": result["session_id"],
            "preview_id": preview["preview_id"],
            "principal_id": "operator", "expected_revision": 0,
            "accepted_items": preview["accepted_items"]})
        assert record["status"] == "verified", record.get("failed_items")
        # 可追溯摘要：source→rectified 域与矩阵版本保留在会话记录
        session = rig.svc.get_session(rig.device, result["session_id"])
        page = session["pages"][PAGE_STATES[0]]
        summary = page.get("rectify") or {}
        assert summary.get("source_evidence_id"), "必须保留原图证据编号"
        assert summary.get("coordinate_domain") == "camera->screen"
        assert summary.get("matrix_kind") == "cameraToScreen"
        assert summary.get("matrix_version"), "必须保留矩阵版本指纹"
        assert summary.get("rectified_size") == {"width": SCREEN_W,
                                                 "height": SCREEN_H}
        # 原相机字节不被伪标屏幕图：摘要记录原图尺寸（相机域）
        assert summary.get("source_size") == {"width": CAM_W,
                                              "height": CAM_H}
    finally:
        rig.close()


# ================= B：错误 v3/旧来源/干扰/标准画布均拒绝错误通过 =================
def test_r3_12_b_wrong_homography_rejects_wrong_pass(tmp_path):
    """错单应把屏幕投出原图：在采样边界填充值之前拒绝，不发布通过。"""
    rotated = [0.0, 1.0, 0.0,
               -1.0, 0.0, float(SCREEN_H),
               0.0, 0.0, 1.0]
    rig = Rig(tmp_path / "rt-b1", tag=1, provider=build_provider(1),
              calibration=V3Calibration(1, homography_override=rotated))
    try:
        template = rig.seed_template("tpl-persp", build_pages(build_assets()),
                                     build_assets())
        with pytest.raises(AdaptationError, match="超出原图") as exc:
            run_four_state(rig, template)
        assert exc.value.code == "items_unverified"
        assert rig.svc.get_current(rig.device) is None
    finally:
        rig.close()


def test_r3_12_b_wrong_device_calibration_and_stale_source_rejected(tmp_path):
    """错设备 v3（binding 不一致）/旧来源几何（sourceGeometryId 不一致）拒绝。"""
    provider = build_provider(1)
    rig = Rig(tmp_path / "rt-b2", tag=1, provider=provider)
    try:
        template = rig.seed_template("tpl-persp", build_pages(build_assets()),
                                     build_assets())
        sid = rig.svc.start_session(rig.device, {
            "template_ref": template, "principal_id": "operator",
            "installation_binding": BINDING})["session_id"]
        capture = rig.svc.capture(rig.device, {
            "client_request_id": str(uuid.uuid4()),
            "owner_kind": "session", "owner_id": sid,
            "expected_owner_revision": 1, "page_state": "wechat_list",
            "controlled_test_confirmed": True})
        # 写页前换上错设备 v3（cameraBindingId 与会话快照/帧不符）
        rig.svc._calibration_provider = V3Calibration(
            1, camera_binding_id="bbbbbbbb-2222-4bbb-8bbb-bbbbbbbbbbbb")
        from runtime.layout_adaptation import AdaptationError
        with pytest.raises(AdaptationError) as exc:
            rig.svc.write_page(rig.device, sid, "wechat_list", {
                "client_request_id": str(uuid.uuid4()),
                "expected_session_revision": 1,
                "source_evidence_ids": capture["source_evidence_ids"],
                "local_overrides": []})
        assert exc.value.code in ("binding_changed", "source_binding_changed",
                                  "items_unverified")
        # 旧来源几何：v3 的 sourceGeometryId 与证据来源不一致 → 拒绝
        rig.svc._calibration_provider = V3Calibration(
            1, source_geometry_id="9" * 64)
        with pytest.raises(AdaptationError) as exc:
            rig.svc.write_page(rig.device, sid, "wechat_list", {
                "client_request_id": str(uuid.uuid4()),
                "expected_session_revision": 1,
                "source_evidence_ids": capture["source_evidence_ids"],
                "local_overrides": []})
        assert exc.value.code in ("source_binding_changed", "items_unverified",
                                  "binding_changed")
    finally:
        rig.close()


def test_r3_12_b_anchor_ambiguity_not_passed(tmp_path):
    """相似环境干扰：search_region 内同砖重复 → 锚点歧义 → 该页不通过。"""
    rig = Rig(tmp_path / "rt-b3", tag=1,
              provider=build_provider(1, duplicate="back_button"))
    try:
        template = rig.seed_template("tpl-persp", build_pages(build_assets()),
                                     build_assets())
        result = run_four_state(rig, template)
        for state in ("chat_ime_hidden", "chat_ime_empty", "chat_ime_text"):
            check = result["checks"][state]
            back = next(a for a in check["anchor_results"]
                        if a["id"] == "back_button")
            assert back["status"] != "passed", (
                "重复砖必须产生歧义/失败", back)
            assert check["status"] == "failed"
        # 不依赖 back_button 的列表页不受影响（C 侧：只阻断依赖）
        assert result["checks"]["wechat_list"]["status"] == "passed"
    finally:
        rig.close()


def test_r3_12_b_standard_canvas_not_camera_frame(tmp_path):
    """屏幕画布尺寸不是可信相机尺寸，须在匹配前拒绝，不能发布。"""
    assets = build_assets()
    frames = {}
    for state in PAGE_STATES:
        canvas = draw_screen_canvas(page_by_state(build_pages(assets), state),
                                    assets)
        ok, buf = cv2.imencode(".jpg", canvas,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        frames[state] = buf.tobytes()
    provider = CameraProvider(frames)
    rig = Rig(tmp_path / "rt-b4", tag=1, provider=provider)
    try:
        template = rig.seed_template("tpl-persp", build_pages(assets), assets)
        with pytest.raises(AdaptationError, match="实际原图尺寸") as exc:
            run_four_state(rig, template)
        assert exc.value.code == "source_binding_changed"
        assert rig.svc.get_current(rig.device) is None
    finally:
        rig.close()


# ============ C：失败一个目标只阻断依赖项，可用项完整保留 ============
def test_r3_12_c_single_failed_target_blocks_only_dependents(tmp_path):
    """input_box 锚点资产换成空白 → 该锚点+依赖项失败；其余锚点/页保持通过。"""
    rig = Rig(tmp_path / "rt-c", tag=1,
              provider=build_provider(1, blank_anchor="input_box"))
    try:
        template = rig.seed_template("tpl-persp", build_pages(build_assets()),
                                     build_assets())
        result = run_four_state(rig, template)
        for state in ("chat_ime_hidden", "chat_ime_empty", "chat_ime_text"):
            check = result["checks"][state]
            input_box = next(a for a in check["anchor_results"]
                             if a["id"] == "input_box")
            assert input_box["status"] == "failed"
            deps = {r["id"] for r in check["item_results"]
                    if r["status"] == "failed"}
            assert "input" in deps, "依赖 input_box 的 input 区域必须被阻断"
            others = {a["id"] for a in check["anchor_results"]
                      if a["status"] == "passed"}
            assert "back_button" in others, "不依赖项不得被牵连"
        preview = rig.svc.preview(rig.device,
                                  {"capture_session_id": result["session_id"]})
        failed_states = {i["page_state"] for i in preview["failed_items"]}
        assert failed_states == {"chat_ime_hidden", "chat_ime_empty",
                                 "chat_ime_text"}
        record = rig.svc.publish(rig.device, {
            "client_request_id": str(uuid.uuid4()),
            "session_id": result["session_id"],
            "preview_id": preview["preview_id"],
            "principal_id": "operator", "expected_revision": 0,
            "accepted_items": preview["accepted_items"]})
        assert record["status"] == "needs_review"
        # 列表页通过项完整保留（不整包清空）
        assert {i["page_state"] for i in record["accepted_items"]} \
            >= {"wechat_list"}
    finally:
        rig.close()


# ============ D：第二台不同透视独立校验（独立 Store/单应/设备） ============
def test_r3_12_d_second_device_independent_perspective(tmp_path):
    provider = build_provider(2)
    rig = Rig(tmp_path / "rt-d2", tag=2, provider=provider,
              device="arm-p2")
    try:
        assert not np.allclose(homography(1), homography(2)), \
            "两台单应必须不同（各自独立校准）"
        template = rig.seed_template("tpl-p2", build_pages(build_assets()),
                                     build_assets())
        result = run_four_state(rig, template)
        for state in PAGE_STATES:
            assert result["checks"][state]["status"] == "passed", (
                state, result["checks"][state].get("reason_codes"))
        preview = rig.svc.preview("arm-p2",
                                  {"capture_session_id": result["session_id"]})
        record = rig.svc.publish("arm-p2", {
            "client_request_id": str(uuid.uuid4()),
            "session_id": result["session_id"],
            "preview_id": preview["preview_id"],
            "principal_id": "operator", "expected_revision": 0,
            "accepted_items": preview["accepted_items"]})
        assert record["status"] == "verified", record.get("failed_items")
        assert record["record"]["device_id"] == "arm-p2"
    finally:
        rig.close()
