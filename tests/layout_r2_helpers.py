"""R2 布局包共享测试基础设施（R2-25/19/16/20）。

`build_layout_bundle` 镜像正式装配（runtime.layout_bundle 补丁后的形状）：
每个"Runtime/临时根"持有自己的 Store、持久状态库、适配服务、操作服务与锁，
互不共享进程级状态。测试只能替换外部 IO（时钟、采集帧、校准/绑定提供器），
保留真实服务/存储/匹配/协议实现。
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import cv2

from runtime.layout_adaptation import LayoutAdaptationService
from runtime.layout_ops import CatalogTemplateResolver, LayoutOpsService
from runtime.layout_state import LayoutStateStore
from runtime.layout_template import LayoutTemplateService, LayoutTemplateStore
from runtime.layout_region import LayoutRegionStore


# ---------------- 时钟 ----------------
class FakeClock:
    def __init__(self, start: str = "2026-10-04T12:00:00Z"):
        self.now = datetime.fromisoformat(start.replace("Z", "+00:00"))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


# ---------------- 与 runtime._KVStore 等价的旧 KV（仅 legacy 读取/迁移用） ----------------
class LegacyKV:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS layout_kv "
                               "(k TEXT PRIMARY KEY, json TEXT)")
            self._conn.commit()

    def save(self, key, record):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO layout_kv (k, json) VALUES (?, ?)",
                (key, json.dumps(record, ensure_ascii=False)))
            self._conn.commit()

    def load(self, key):
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM layout_kv WHERE k=?", (key,)).fetchone()
        return json.loads(row["json"]) if row else None

    def delete(self, key):
        with self._lock:
            self._conn.execute("DELETE FROM layout_kv WHERE k=?", (key,))
            self._conn.commit()

    def scan_prefix(self, prefix):
        with self._lock:
            rows = self._conn.execute(
                "SELECT k, json FROM layout_kv WHERE k LIKE ?",
                (prefix.replace("%", r"\%") + "%",)).fetchall()
        return [(r["k"], json.loads(r["json"])) for r in rows]

    def scan_all(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT k, json FROM layout_kv ORDER BY k").fetchall()
        return [(r["k"], json.loads(r["json"])) for r in rows]

    def raw_record_bytes(self, key: str) -> bytes:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM layout_kv WHERE k=?", (key,)).fetchone()
        return row["json"].encode("utf-8") if row else b""


# ---------------- 合成图（含可检测特征，实际运行匹配） ----------------
def make_anchor_tile(seed: int = 1, size: int = 24) -> np.ndarray:
    """固定纹理锚点块（棋盘+噪声），可被 matchTemplate 稳定定位。"""
    rng = np.random.default_rng(seed)
    tile = (rng.integers(0, 256, size=(size, size), dtype=np.uint16)
            // 4 * 4).astype(np.uint8)
    tile = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
    return np.ascontiguousarray(tile)


def make_frame(placement: dict[str, tuple[int, int]], tiles: dict[str, np.ndarray],
               width: int = 360, height: int = 640) -> np.ndarray:
    """把锚点块贴到画布指定左上角，其余填充浅灰背景。"""
    canvas = np.full((height, width, 3), 200, dtype=np.uint8)
    for name, (x, y) in placement.items():
        tile = tiles[name]
        h, w = tile.shape[:2]
        canvas[y:y + h, x:x + w] = tile
    return canvas


def encode_jpeg(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", image,
                           [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    assert ok
    return buf.tobytes()


def png_bytes(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------- 模板装配（真实模板记录 + 原字节资产） ----------------
# 四页最低项（契约 §5）：列表含 wechat_chats_tab、unread/content；
# 聊天含 back_button/input_box、title/input/content；展开页 keyboard_boundary；
# 有字页另 send_button/send_button_feature。
PAGE_TILES = {
    "wechat_list": ["wechat_chats_tab", "list_row_anchor"],
    "chat_ime_hidden": ["back_button", "input_box"],
    "chat_ime_empty": ["back_button", "input_box", "keyboard_boundary"],
    "chat_ime_text": ["back_button", "input_box", "keyboard_boundary",
                      "send_button", "send_button_feature"],
}


def build_template_assets(seed_base: int = 10) -> dict[str, np.ndarray]:
    return {name: make_anchor_tile(seed_base + i)
            for i, name in enumerate(
                sorted({t for tiles in PAGE_TILES.values() for t in tiles}))}


def build_template_pages(assets: dict[str, np.ndarray], tile: int = 24,
                         width: int = 360, height: int = 640) -> list[dict]:
    """冻结 page 形状：每态 anchors（引用资产）+ targets/regions。"""
    pages = []
    for state, names in PAGE_TILES.items():
        anchors = []
        for i, name in enumerate(names):
            x = 20 + i * (tile + 16)
            y = 40 + (0 if i < 3 else 60)
            anchors.append({
                "id": name, "asset_id": f"asset-{name}",
                "search_region": {"x": 0.0, "y": 0.0, "width": 1.0,
                                  "height": 1.0},
                "reference_rect": {"x": x / width, "y": y / height,
                                   "width": tile / width,
                                   "height": tile / height},
                "required": True})
        regions = []
        if state == "wechat_list":
            regions = [
                {"id": "unread", "anchor_id": "wechat_chats_tab",
                 "relative_rect": {"dx": 0.0, "dy": 0.05, "width": 0.2,
                                   "height": 0.05}, "purpose": "unread"},
                {"id": "content", "anchor_id": "wechat_chats_tab",
                 "relative_rect": {"dx": 0.25, "dy": 0.05, "width": 0.5,
                                   "height": 0.05}, "purpose": "content"},
            ]
        else:
            regions = [
                {"id": "title", "anchor_id": "back_button",
                 "relative_rect": {"dx": 0.1, "dy": 0.0, "width": 0.3,
                                   "height": 0.03}, "purpose": "title"},
                {"id": "input", "anchor_id": "input_box",
                 "relative_rect": {"dx": 0.1, "dy": 0.05, "width": 0.6,
                                   "height": 0.05}, "purpose": "input"},
                {"id": "content", "anchor_id": "back_button",
                 "relative_rect": {"dx": 0.1, "dy": 0.12, "width": 0.6,
                                   "height": 0.4}, "purpose": "content"},
            ]
        pages.append({"state": state, "anchors": anchors, "targets": [],
                      "regions": regions})
    return pages


def build_page_frames(assets: dict[str, np.ndarray], *,
                      correct: bool = True,
                      width: int = 360, height: int = 640) -> dict[str, np.ndarray]:
    """按模板 reference_rect 摆放锚点（correct=False 时贴错图案→真实匹配失败）。

    R2-16：region 矩形内绘制确定性伪文本条纹——真实页面区域有内容纹理，
    服务端颜色采样（低纹理拒绝）需要非平坦区域；合成页同理真实绘制。
    """
    frames = {}
    tile = 24
    pages_list = build_template_pages(assets, width=width, height=height)
    rng = np.random.default_rng(11)
    for state, names in PAGE_TILES.items():
        page = next(p for p in pages_list if p["state"] == state)
        canvas = np.full((height, width, 3), 200, dtype=np.uint8)
        anchors_by_id = {a["id"]: a for a in page["anchors"]}
        for region in page["regions"]:
            anchor = anchors_by_id.get(region.get("anchor_id"))
            rect = region.get("relative_rect") or {}
            if anchor is None:
                continue
            ox = int(round((anchor["reference_rect"]["x"]
                            + rect.get("dx", 0.0)) * width))
            oy = int(round((anchor["reference_rect"]["y"]
                            + rect.get("dy", 0.0)) * height))
            w = int(round(rect.get("width", 0.0) * width))
            h = int(round(rect.get("height", 0.0) * height))
            if w <= 4 or h <= 4:
                continue
            rows_needed = (h + 5) // 6
            stripes = (rng.integers(0, 2, size=(rows_needed, 1),
                                    dtype=np.uint8) * 150 + 55).astype(np.uint8)
            block = np.repeat(stripes, 6, axis=0)[:h]
            block = np.repeat(block, w, axis=1)[:, :w]
            canvas[oy:oy + h, ox:ox + w, 0] = block
            canvas[oy:oy + h, ox:ox + w, 1] = block
            canvas[oy:oy + h, ox:ox + w, 2] = block
        for i, name in enumerate(names):
            x = 20 + i * (tile + 16)
            y = 40 + (0 if i < 3 else 60)
            image = assets[name] if correct else make_anchor_tile(seed=900)
            th, tw = image.shape[:2]
            canvas[y:y + th, x:x + tw] = image
        frames[state] = canvas
    return frames


# ---------------- 受控采集提供器（合成两帧 + 真实采样字段） ----------------
class FakeCaptureProvider:
    """每次 capture 吐两帧：真实推进 frame_seq/capture_seq/采样时间；
    由测试控制是否满足间隔/新鲜度门禁。只替换外部相机 IO。
    原字节按 evidence_id 归档（read 回调供服务端匹配读取真实图像）。"""

    def __init__(self, frames_by_state: dict[str, np.ndarray],
                 *, binding: dict, source_geometry_id: str,
                 uncertainty_ms: float = 2.0):
        self.frames = frames_by_state
        self.binding = binding
        self.source_geometry_id = source_geometry_id
        self.uncertainty_ms = uncertainty_ms
        self.seq = 0
        self.capture_seq = 0
        self.mono_ns = 1_000_000_000_000  # 1000s
        self.generation = "11111111-1111-4111-8111-111111111111"
        self.interval_ms = 250.0          # 两帧采样间隔
        self.age_ms = 80.0                # 接收时帧龄
        self.calls: list[str] = []
        self.archive: dict[str, bytes] = {}

    def _one(self, state: str) -> dict:
        self.seq += 1
        self.capture_seq += 1
        self.mono_ns += int(self.interval_ms * 1e6)
        # Keep the template canvas intact inside a measurable camera background.
        image = cv2.copyMakeBorder(
            self.frames[state], 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=(20, 20, 20))
        jpeg = encode_jpeg(image)
        evidence_id = f"csf-{uuid.uuid4().hex[:12]}"
        self.archive[evidence_id] = jpeg
        return {
            "evidence_id": evidence_id,
            "jpeg": jpeg,
            "media_type": "image/jpeg",
            "installation_binding": dict(self.binding),
            "source_size": {"width": image.shape[1], "height": image.shape[0]},
            "source_geometry_id": self.source_geometry_id,
            "source_session_id": self.generation,
            "source_generation_id": self.generation,
            "source_frame_seq": self.seq,
            "source_capture_seq": self.capture_seq,
            "source_clock_id": "mach-absolute:" + "0" * 63 + "1",
            "clock_registration_id": "22222222-2222-4222-8222-222222222222",
            "source_captured_monotonic_ns": self.mono_ns,
            "source_capture_uncertainty_ms": self.uncertainty_ms,
            "source_timing_provenance": "camera_pts_host_clock.v1",
            "received_monotonic_ms": (self.mono_ns // 1_000_000
                                      + int(self.age_ms)),
        }

    def __call__(self, device_id: str, count: int = 2,
                 page_state: str | None = None) -> list[dict]:
        self.calls.append(f"{device_id}:{page_state}:{count}")
        key = page_state if page_state in self.frames else next(
            iter(self.frames))
        return [self._one(key) for _ in range(count)]

    def read(self, evidence_id: str) -> bytes:
        return self.archive[evidence_id]


DEFAULT_BINDING = {
    "camera_id": "cam-01",
    "camera_device_unique_id": "uvc-01",
    "camera_binding_id": "33333333-3333-4333-8333-333333333333",
    "arm_binding_id": "arm-bind-01",
}
DEFAULT_GEOMETRY_ID = "a" * 64


class FakeCalibrationProvider:
    """本台 v3 校准事实提供器（只替身校准产物来源，不替身校验逻辑）。

    默认 360x640 屏幕置于有 20px 背景的 400x680 相机帧中，边界真实可检测；
    模板仍在完整屏幕域，cameraToScreen 去掉背景偏移，不替换定位或姿态检查。
    透视场景由 R3-12 专项测试自带非恒等单应提供器。"""

    def __init__(self, ready: bool = True, schema_version: int = 3,
                 camera_binding_id: str = DEFAULT_BINDING["camera_binding_id"],
                 source_geometry_id: str = DEFAULT_GEOMETRY_ID,
                 screen_size: tuple[int, int] = (360, 640),
                 homography: list | None = None):
        self.ready = ready
        self.schema_version = schema_version
        self.camera_binding_id = camera_binding_id
        self.source_geometry_id = source_geometry_id
        self.screen_size = screen_size
        self.homography = homography  # None → 恒等（相机域=屏幕域）
        self.calls: list[str] = []

    def __call__(self, device_id: str) -> dict | None:
        self.calls.append(device_id)
        if not self.ready:
            return None
        width, height = self.screen_size
        original = (np.asarray(self.homography, dtype=float).reshape(3, 3)
                    if self.homography is not None else np.eye(3))
        matrix = original @ np.array([[1, 0, -20], [0, 1, -20], [0, 0, 1]])
        camera_to_screen = {"homography": matrix.reshape(-1).tolist()}
        quad = np.array([[0, 0, 1], [width - 1, 0, 1],
                         [width - 1, height - 1, 1], [0, height - 1, 1]]) @ np.linalg.inv(matrix).T
        corners = (quad[:, :2] / quad[:, 2:]).tolist()
        return {"schemaVersion": self.schema_version, "status": "ready",
                "deviceId": device_id,
                "cameraId": DEFAULT_BINDING["camera_id"],
                "cameraDeviceUniqueId": DEFAULT_BINDING["camera_device_unique_id"],
                "cameraBindingId": self.camera_binding_id,
                "sourceGeometryId": self.source_geometry_id,
                "coordinateFrameHash": DEFAULT_GEOMETRY_ID,
                "bindingFingerprint": DEFAULT_GEOMETRY_ID,
                "frame": {"width": width + 40, "height": height + 40},
                "screen": {"screenWidth": width, "screenHeight": height,
                           "orientation": "portrait",
                           "edgeModel": {"corners": corners, "tolerancePx": 8}},
                "camera": {"validated": True, "cameraToScreen": camera_to_screen},
                "ready": True}


# ---------------- 正式装配镜像（Runtime.layout_bundle 补丁形状） ----------------
class LayoutBundle:
    """一个"Runtime"的布局资料束：同根同库，持有多实例服务与锁。

    与 runtime.py 补丁一致：进程内不存在共享单例；关机 close() 只释放
    本实例的连接与状态。pre-fix 快照中本 helper 曾走
    LayoutAdaptationService.shared（缺陷装配），用于红测复现串库。
    """

    def __init__(self, root: Path, *, clock=None, capture_provider=None,
                 calibration_provider=None, binding_provider=None,
                 template_seed: bool = True):
        self.root = Path(root)
        self.db_path = self.root / "data" / "layout.db"
        self.clock = clock or FakeClock()
        # 本台屏幕事实（portrait；与合成页 360x640 同域）
        width_screen = (360, 640)
        self.capture_provider = capture_provider
        self.calibration_provider = calibration_provider
        self.kv_store = LegacyKV(self.db_path)
        self.region_store = LayoutRegionStore(self.db_path)
        self.template_store = LayoutTemplateStore(self.db_path)
        self.state = LayoutStateStore(self.db_path)
        self.persistence = self.state
        self.ops_svc = LayoutOpsService(
            self.kv_store, blobs_dir=self.root / "data" / "layout_packages",
            region_store=self.region_store,
            template_store=self.template_store,
            state_store=self.state, data_root=self.root / "data")
        frame_reader = (capture_provider.read
                        if hasattr(capture_provider, "read") else None)
        # R2-20 统一解析器：适配服务与正式 GET/导入同路径（catalog 门控）
        template_resolver = CatalogTemplateResolver(self.ops_svc,
                                                    self.template_store)
        # server_instance_id = 本次启动 UUID（每次装配唯一，模拟真实重启）
        self.instance_id = f"inst-{uuid.uuid4().hex[:12]}"

        def _region_record_provider(device_id: str):
            pointer = self.region_store.get_pointer(device_id)
            if pointer is None:
                return None
            record = self.region_store.get_version(device_id, pointer["selected_version"])
            assert record is not None and record["sha256"] == pointer["selected_sha256"]
            return record

        def _region_ref_provider(device_id: str):
            region = _region_record_provider(device_id)
            if region is None:
                return None
            return {"id": device_id, "version": region["version"],
                    "sha256": region.get("sha256")}

        self.binding_provider = binding_provider or (lambda device: {
            "camera_id": DEFAULT_BINDING["camera_id"],
            "camera_binding_id": DEFAULT_BINDING["camera_binding_id"],
            "arm_binding_id": DEFAULT_BINDING["arm_binding_id"],
            "camera_device_unique_id": DEFAULT_BINDING["camera_device_unique_id"],
            "source_geometry_id": DEFAULT_GEOMETRY_ID,
            "source_size": {"width": 400, "height": 680},
            "ime_device_id": "k40-1",
            "screen_descriptor": {
                "width_px": width_screen[0], "height_px": width_screen[1],
                "orientation": "portrait",
                "system_insets": {"left": 0, "top": 0, "right": 0,
                                  "bottom": 0},
                "calibration_geometry_hash": DEFAULT_GEOMETRY_ID}})
        self.adaptation_svc = LayoutAdaptationService(
            store=self.kv_store,
            template_store=template_resolver,
            state_store=self.state,
            clock=self.clock,
            server_instance_id=self.instance_id,
            capture_provider=capture_provider,
            calibration_provider=calibration_provider,
            binding_provider=self.binding_provider,
            frame_reader=frame_reader,
            region_ref_provider=_region_ref_provider,
            region_record_provider=_region_record_provider)
        if template_seed:
            self._seed_template()
            self._seed_region()
        self._closed = False

    def _seed_region(self, device_id: str = "arm-01"):
        """本台粗框 v1（适配发布需要 installation_region_ref）。"""
        from runtime.layout_region import canonical_digest
        record = {"contract_version": "phone-search-region.v1",
                  "device_id": device_id, "version": 1,
                  "installation_binding": dict(DEFAULT_BINDING),
                  "source_size": {"width": 400, "height": 680},
                  "source_geometry_id": DEFAULT_GEOMETRY_ID,
                  # The coarse frame includes both the phone and its camera background.
                  "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                  "pixel_rect": {"left": 0, "top": 0, "right": 400,
                                 "bottom": 680},
                  "reference_evidence_id": "seed-evidence",
                  "created_at": "2026-10-04T00:00:00.000Z",
                  "created_by": "operator"}
        record["sha256"] = canonical_digest(record)
        self.region_store.insert_version(device_id, 1, record)
        pointer = self.region_store.get_pointer(device_id)
        if pointer is None:
            self.region_store.cas_pointer(device_id, 0, 1,
                                          record["sha256"], "seed-region")

    def _seed_template(self):
        """真实发布一个含原字节资产的模板（pages 按态键控，锚点引用资产）。

        R3-13：统一可见性按 catalog/已提交发布事务门控；种子模板补 catalog
        行（模拟已提交资源），半包可见性由 R3-13 专项测试覆盖。"""
        from runtime.layout_state import state_key
        assets = build_template_assets()
        self.assets = assets
        pages_list = build_template_pages(assets)
        pages = {page["state"]: page for page in pages_list}
        tpl_svc = LayoutTemplateService(self.template_store)
        draft = tpl_svc.create_draft({"template_id": "tpl-1"})
        for name, image in assets.items():
            self.template_store.save_asset(draft["draft_id"],
                                           f"asset-{name}", png_bytes(image))
        tpl_svc.publish({"template_id": "tpl-1", "version": 1,
                         "pages": pages, "metadata": {},
                         "draft_id": draft["draft_id"]})
        self.template_pages = pages_list
        record = self.template_store.get_template("tpl-1", 1)
        row = {"resource": {"kind": "template",
                            "ref": {"id": "tpl-1", "version": 1,
                                    "sha256": record["sha256"]}},
               "operation_id": "seed-template",
               "created_at": record.get("created_at")
               or "2026-10-04T00:00:00.000Z"}
        self.state.state_insert(
            "catalog", state_key("catalog", ["template", "tpl-1", 1]),
            None, None, row)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.adaptation_svc.close()
        self.state.close()
        self.template_store.close()
        self.region_store.close()
        self.kv_store._conn.close()


def open_bundle(root: Path, **kwargs) -> LayoutBundle:
    """重启场景：同一临时根重新装配（不共享内存实例）。"""
    return LayoutBundle(root, template_seed=False, **kwargs)
