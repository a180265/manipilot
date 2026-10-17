"""粗框搜索范围（phone-search-region.v1，规格 §11.11.2 / L01 / R2-15）。

R2-15（2026-10-04 第二轮）：
- 预览请求按冻结 wire 契约：rect 是 source_norm 的 {x,y,width,height}
  （右/下排他语义换算为 pixel_rect），客户端只声明 rect 与 source_evidence_id；
  不接受自报 installation_binding/source_size/source_geometry_id。
- 服务从 source_evidence_id 读取原框选证据（EvidenceStore camera_source_v2），
  核对坐标域、真实尺寸、设备归属、安装与来源几何（camera_broker 冻结构建器，
  缺可信字段拒绝）。
- 预览另取当前新鲜可信源帧（同设备/安装/来源几何），在粗框内实际检测手机；
  source_binding.evidence_id 指向本次检测帧（可与框选证据不同）。
- 保存时再次核验当前来源新鲜度、预览期限与发布空闲；检测状态仅作预览参考
  信息，不拦发布（业主 2026-10-16 拍板：人工画框直接采信）；publish=false 存
  needs_review 草稿不改生效指针。真正的门在使用时（下游 detect_screen_quad
  实际定位，框里没手机会在使用时大声失败）。
- 预览/草稿/操作台账落 layout_typed_state（region_preview 一次消费、
  region_draft 不可变、operation/intent 幂等）。

全部零机械/输入动作：本服务不持有驱动通道。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from runtime.layout_state import (
    LayoutStateError,
    LayoutStateStore,
    content_sha,
    state_identity,
    state_key,
)
from runtime.util import iso, parse_iso, utc_now

CONTRACT_VERSION = "phone-search-region.v1"
POINTER_VERSION = "layout-pointer.v1"
WRITE_VERSION = "phone-search-region-write.v1"
PREVIEW_VERSION = "phone-search-region-preview.v1"
READ_VERSION = "phone-search-region-read.v1"
PREVIEW_TTL_SECONDS = 120

REASON_CODES = (
    "phone_not_located", "multiple_screen_candidates", "screen_outside_region",
    "dark_screen", "occluded", "source_unavailable", "source_binding_changed",
    "geometry_changed", "archive_invalid",
)

# §7.3 质量阈值（与 phone_localization 同源）
_DARK_BRIGHTNESS_MAX = 18.0
_DARK_TEXTURE_MAX = 8.0


class RegionError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def canonical_digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _region_now(clock) -> datetime:
    value = (clock or utc_now)()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _parse_norm_rect(rect: dict) -> dict:
    """冻结 rect 域：source_norm {x,y,width,height}（旧 left/top 域拒绝）。"""
    if not isinstance(rect, dict):
        raise RegionError("camera_request_invalid", "rect 必须为对象")
    if set(rect) != {"x", "y", "width", "height"}:
        raise RegionError(
            "camera_request_invalid",
            "rect 必须为 source_norm 的 {x,y,width,height}（不接受 "
            "left/top/right/bottom 或其他字段）")
    values = {}
    for key, value in rect.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            raise RegionError("camera_request_invalid",
                              f"rect.{key} 必须为有限数值")
        values[key] = float(value)
    if not (0.0 <= values["x"] < 1.0 and 0.0 <= values["y"] < 1.0):
        raise RegionError("camera_request_invalid", "rect.x/y 超出归一化范围")
    if not (0.0 < values["width"] <= 1.0 and 0.0 < values["height"] <= 1.0):
        raise RegionError("camera_request_invalid",
                          "rect.width/height 必须在 (0,1]")
    if values["x"] + values["width"] > 1.0 + 1e-9 \
            or values["y"] + values["height"] > 1.0 + 1e-9:
        raise RegionError("camera_request_invalid", "rect 超出来源图右/下边界")
    return values


def _pixel_rect_from_norm(rect: dict, width: int, height: int) -> dict:
    """左上 floor、右下 ceil，右/下排他，最小 4 像素（§11.11.2）。"""
    left = max(0, math.floor(rect["x"] * width))
    top = max(0, math.floor(rect["y"] * height))
    right = min(width, math.ceil((rect["x"] + rect["width"]) * width))
    bottom = min(height, math.ceil((rect["y"] + rect["height"]) * height))
    if right - left < 4 or bottom - top < 4:
        raise RegionError("camera_request_invalid", "像素框宽高至少 4 像素")
    return {"left": left, "top": top, "right": right, "bottom": bottom}


class LayoutRegionStore:
    """phone-search-region 不可变版本 + CAS 指针（per-device）。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS layout_region_versions (
                    device_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    json TEXT NOT NULL,
                    PRIMARY KEY (device_id, version)
                );
                CREATE TABLE IF NOT EXISTS layout_region_pointer (
                    device_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    selected_version INTEGER NOT NULL,
                    selected_sha256 TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    operation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS layout_region_drafts (
                    device_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (device_id, version)
                );
                CREATE TABLE IF NOT EXISTS layout_previews (
                    preview_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            self._conn.commit()

    def _save(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        """R2-25：释放本 Store 自己的连接（幂等）。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---- 版本读写 ----
    def get_version(self, device_id: str, version: int) -> dict | None:
        row = self._query_one(
            "SELECT json FROM layout_region_versions WHERE device_id=? AND version=?",
            (device_id, version))
        return json.loads(row["json"]) if row else None

    def latest_version(self, device_id: str) -> tuple[int, dict | None]:
        row = self._query_one(
            "SELECT version, json FROM layout_region_versions "
            "WHERE device_id=? ORDER BY version DESC LIMIT 1", (device_id,))
        if row is None:
            return 0, None
        return int(row["version"]), json.loads(row["json"])

    def insert_version(self, device_id: str, version: int, record: dict) -> None:
        sha = canonical_digest(
            {k: v for k, v in record.items() if k != "sha256"})
        with self._lock:
            row = self._query_one(
                "SELECT sha256 FROM layout_region_versions "
                "WHERE device_id=? AND version=?", (device_id, version))
            if row is not None:
                if row["sha256"] == sha:
                    return  # 同内容幂等复用（导入重放不造新版本）
                raise RegionError(
                    "revision_conflict",
                    f"区域版本 {device_id} v{version} 已存在且内容不同")
            try:
                self._save(
                    "INSERT INTO layout_region_versions "
                    "(device_id, version, sha256, json) VALUES (?, ?, ?, ?)",
                    (device_id, version, sha,
                     json.dumps(record, ensure_ascii=False)))
            except sqlite3.IntegrityError:
                # 并发竞态：唯一约束兜底（回滚隐式事务，避免悬挂写锁）
                with self._lock:
                    self._conn.rollback()
                row = self._query_one(
                    "SELECT sha256 FROM layout_region_versions "
                    "WHERE device_id=? AND version=?", (device_id, version))
                if row is not None and row["sha256"] == sha:
                    return
                raise RegionError(
                    "revision_conflict",
                    f"区域版本 {device_id} v{version} 并发写入冲突")

    def save_draft(self, device_id: str, version: int, record: dict) -> str:
        """needs_review 草稿（R2-15）：不改生效指针；同内容幂等。"""
        sha = canonical_digest(
            {k: v for k, v in record.items() if k != "sha256"})
        record = {**record, "sha256": sha}
        with self._lock:
            row = self._query_one(
                "SELECT sha256 FROM layout_region_drafts "
                "WHERE device_id=? AND version=?", (device_id, version))
            if row is not None:
                if row["sha256"] == sha:
                    return sha
                raise RegionError(
                    "revision_conflict",
                    f"草稿 {device_id} v{version} 已存在且内容不同")
            self._save(
                "INSERT INTO layout_region_drafts "
                "(device_id, version, sha256, json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (device_id, version, sha,
                 json.dumps(record, ensure_ascii=False), iso(utc_now())))
        # typed region_draft 行同步（不可变；身份 [device_id, version]）
        state = self._state_ref()
        if state is not None:
            try:
                state.state_insert(
                    "region_draft",
                    state_key("region_draft", [device_id, version]),
                    device_id, record.get("created_by"), record)
            except LayoutStateError:
                pass
        return sha

    def latest_draft(self, device_id: str) -> tuple[int, dict | None]:
        row = self._query_one(
            "SELECT version, json FROM layout_region_drafts "
            "WHERE device_id=? ORDER BY version DESC LIMIT 1", (device_id,))
        if row is None:
            return 0, None
        return int(row["version"]), json.loads(row["json"])

    def get_draft(self, device_id: str, version: int) -> dict | None:
        row = self._query_one(
            "SELECT json FROM layout_region_drafts "
            "WHERE device_id=? AND version=?", (device_id, version))
        return json.loads(row["json"]) if row else None

    def _state_ref(self):
        return getattr(self, "_typed_state", None)

    def attach_state(self, state: LayoutStateStore) -> None:
        self._typed_state = state

    def delete_pointer(self, device_id: str) -> dict:
        """撤销生效范围（业主 2026-10-16：删错框重来的正规入口）。版本历史保留作审计。"""
        with self._lock:
            row = self._query_one(
                "SELECT revision FROM layout_region_pointer WHERE device_id=?",
                (device_id,))
            if row is None:
                return {"deleted": False, "revision": None}
            self._save("DELETE FROM layout_region_pointer WHERE device_id=?",
                       (device_id,))
            return {"deleted": True, "revision": int(row["revision"])}

    def get_pointer(self, device_id: str) -> dict | None:
        row = self._query_one(
            "SELECT * FROM layout_region_pointer WHERE device_id=?", (device_id,))
        if row is None:
            return None
        return {"contract_version": POINTER_VERSION,
                "device_id": device_id,
                "kind": "phone_search_region",
                "revision": row["revision"],
                "selected_version": row["selected_version"],
                "selected_sha256": row["selected_sha256"],
                "updated_at": row["updated_at"],
                "operation_id": row["operation_id"]}

    def cas_pointer(self, device_id: str, expected_revision: int,
                    selected_version: int, selected_sha256: str,
                    operation_id: str) -> dict:
        now = iso(utc_now())
        with self._lock:
            row = self._query_one(
                "SELECT revision FROM layout_region_pointer WHERE device_id=?",
                (device_id,))
            current = int(row["revision"]) if row else 0
            if current != expected_revision:
                raise RegionError(
                    "revision_conflict",
                    f"指针版本冲突（当前 {current}，期望 {expected_revision}）")
            self._save(
                "INSERT INTO layout_region_pointer "
                "(device_id, revision, selected_version, selected_sha256, updated_at, operation_id) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET "
                "revision=excluded.revision, selected_version=excluded.selected_version, "
                "selected_sha256=excluded.selected_sha256, updated_at=excluded.updated_at, "
                "operation_id=excluded.operation_id",
                (device_id, current + 1, selected_version, selected_sha256,
                 now, operation_id))
            return self.get_pointer(device_id)

    # ---- 预览缓存（兼容保留；权威在 typed region_preview 行） ----
    def save_preview(self, device_id: str, preview_id: str, record: dict) -> None:
        self._save(
            "INSERT OR REPLACE INTO layout_previews (preview_id, device_id, json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (preview_id, device_id, json.dumps(record, ensure_ascii=False),
             iso(utc_now())))

    def get_preview(self, preview_id: str) -> dict | None:
        row = self._query_one(
            "SELECT json FROM layout_previews WHERE preview_id=?", (preview_id,))
        return json.loads(row["json"]) if row else None


# ---------------- 检测（真实 OpenCV 运行，不指定结果） ----------------

def _screen_candidates(crop_bgr: np.ndarray) -> list[np.ndarray]:
    """粗框内屏幕候选（独立外轮廓、可测尺寸、可近似四边形）。

    同时检查亮/暗极性，按空间重合去重同一屏幕的内外环。
    不按主导极性丢弃另一侧，否则亮屏旁的暗屏会被忽略。
    """
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    unique: list[np.ndarray] = []
    for image in (gray, cv2.bitwise_not(gray)):
        _, binary = cv2.threshold(image, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        # Otsu and its inverse can put a small dark screen in the background.
        # A border-relative contrast mask preserves that independent candidate.
        border = np.concatenate((image[0], image[-1], image[:, 0], image[:, -1]))
        contrast = cv2.threshold(
            image, min(255, float(np.median(border)) + 8), 255,
            cv2.THRESH_BINARY)[1]
        contrast_contours, _ = cv2.findContours(
            contrast, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = list(contours) + list(contrast_contours)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True):
            area = float(cv2.contourArea(contour))
            # Enlarging a manual ROI must not hide a second measurable screen.
            if not 24 * 24 <= area <= width * height * 0.98:
                continue
            short, long = sorted(cv2.minAreaRect(contour)[1])
            if short < 24 or long <= 0 or short / long < .25:
                continue
            hull = cv2.convexHull(contour)
            approx = cv2.approxPolyDP(hull, 0.02 *
                                      cv2.arcLength(hull, True), True)
            if len(approx) < 4:
                continue
            quad = approx.reshape(-1, 2).astype(np.float32)
            # The inverse-polarity outer background is not another screen.
            if np.any(quad < 1) or np.any(quad[:, 0] >= width - 1) \
                    or np.any(quad[:, 1] >= height - 1):
                continue
            if any(cv2.intersectConvexConvex(quad, other)[0]
                   >= .8 * min(cv2.contourArea(quad), cv2.contourArea(other))
                   for other in unique):
                continue
            # 过曝屏的反色候选与正向候选中心重合、面积相近=同一物理屏幕：
            # 反向 Otsu 阈值不同导致轮廓内缩/外扩，交集比阈值不一定过——
            # 按中心+面积判同物去重（2026-10-16 过曝校准页实测二次保存失败根因）。
            if any(_same_screen_quad(quad, other) for other in unique):
                continue
            unique.append(quad)
            if len(unique) >= 2:
                return unique
    return unique


def _same_screen_quad(a: np.ndarray, b: np.ndarray) -> bool:
    """两四边形中心重合且面积相近=同一物理屏幕（跨极性轮廓内缩容忍）。"""
    ax, ay = float(a[:, 0].mean()), float(a[:, 1].mean())
    bx, by = float(b[:, 0].mean()), float(b[:, 1].mean())
    span = max(float(a[:, 0].max() - a[:, 0].min()),
               float(a[:, 1].max() - a[:, 1].min()),
               float(b[:, 0].max() - b[:, 0].min()),
               float(b[:, 1].max() - b[:, 1].min()), 1.0)
    area_a = float((a[:, 0].max() - a[:, 0].min()) * (a[:, 1].max() - a[:, 1].min()))
    area_b = float((b[:, 0].max() - b[:, 0].min()) * (b[:, 1].max() - b[:, 1].min()))
    center_offset = float(np.hypot(ax - bx, ay - by))
    size_ratio = min(area_a, area_b) / max(area_a, area_b, 1.0)
    return center_offset <= 0.25 * span and size_ratio >= 0.36


def _detect_in_rect(frame_bgr: np.ndarray, pixel_rect: dict) -> dict:
    """在当前帧实际检测手机并核对粗框：返回 {status, reason_codes,
    detected_quad}。

    - 检测只在粗框裁图运行，仍用 drivers.screen_edge 严格四边拟合；
      粗框不是四角，检测结果加回裁剪偏移后才是原图坐标；
    - verified 要求：唯一屏幕四边形且完整落在粗框内（未裁断）；
    - 手机在框外 → screen_outside_region；多候选不再单独否决，与单候选同路
      全裁图严格拟合裁决（详见函数体内注释）；全帧暗屏 → dark_screen；
      其余未检出 → phone_not_located。
    """
    failure = {"status": "needs_review",
               "reason_codes": ["screen_outside_region"],
               "detected_quad": None}
    height, width = frame_bgr.shape[:2]
    if not isinstance(pixel_rect, dict):
        return failure
    bounds = [pixel_rect.get(key) for key in ("left", "top", "right", "bottom")]
    if any(type(value) is not int for value in bounds):
        return failure
    left, top, right, bottom = bounds
    if not (0 <= left < right <= width and 0 <= top < bottom <= height
            and right - left >= 4 and bottom - top >= 4):
        return failure
    crop = frame_bgr[top:bottom, left:right]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    frame_dark = (float(gray.mean()) <= _DARK_BRIGHTNESS_MAX
                  and float(gray.std()) < _DARK_TEXTURE_MAX)
    # 多候选不再一票否决（业主 2026-10-16：手画框被杂物候选卡成仅可存草稿）。
    # _screen_candidates 只是低阶轮廓启发式（Otsu 双极性，24×24 矩形亮块即
    # 入围；屏幕+机身边框/桌面矩形杂物轻松凑满两个，且矩形杂物同样能过严
    # 格拟合），而下游消费方（校准向导、姿态门、屏幕观察）一律以
    # detect_screen_quad 全裁图结果为准——门槛应当预演下游行为，不是做更严
    # 的第二意见。多候选与单候选同路：全裁图严格拟合裁决，命中严格入框即
    # verified，框太紧/框外照旧给对应复核原因。
    from drivers.screen_edge import ScreenEdgeError, detect_screen_quad
    # 冻结语义：verified 要求检测四角严格落在粗框内（不得贴边/裁断）。
    margin = 1.0

    def _strictly_inside(corners) -> bool:
        return all(left + margin < point[0] < right - margin
                   and top + margin < point[1] < bottom - margin
                   for point in corners)

    try:
        found = detect_screen_quad(crop)
    except ScreenEdgeError:
        found = None
    if found is not None:
        corners = np.asarray(found["corners"], np.float64) + [left, top]
        # 手机贴满/超出框 = 框太紧 → screen_outside_region（提示稍大于手机重画）。
        if _strictly_inside(corners):
            return {"status": "verified", "reason_codes": [],
                    "detected_quad": [{"x": round(float(px), 2),
                                       "y": round(float(py), 2)}
                                      for px, py in corners]}
        return {"status": "needs_review",
                "reason_codes": ["screen_outside_region"],
                "detected_quad": None}
    if frame_dark:
        return {"status": "needs_review", "reason_codes": ["dark_screen"],
                "detected_quad": None}
    # 全图只诊断框过紧/框错位置，绝不从框外结果生成 verified。
    try:
        outside = detect_screen_quad(frame_bgr)
    except ScreenEdgeError:
        outside = None
    if outside is not None and any(
            not (left + 8 <= x <= right - 8 and top + 8 <= y <= bottom - 8)
            for x, y in outside["corners"]):
        return failure
    return {"status": "needs_review", "reason_codes": ["phone_not_located"],
            "detected_quad": None}


def _decode_jpeg(data: bytes) -> np.ndarray:
    try:
        array = np.frombuffer(data, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR) if array.size else None
    except (ValueError, TypeError, cv2.error):
        image = None
    if image is None:
        raise RegionError("archive_invalid", "来源帧原图无法解码（archive_invalid）")
    return image


class LayoutRegionService:
    """粗框：预览（真实证据链+检测）/保存（复验+草稿/发布）/读取/版本。"""

    def __init__(self, store: LayoutRegionStore, state_store: LayoutStateStore | None = None):
        self._store = store
        self._state = state_store
        store.attach_state(state_store) if state_store is not None else None
        self._clock = None
        self._env: dict = {}

    # ---- 环境注入（app 层接 Runtime 事实源；幂等） ----
    def configure(self, *, evidence_get=None, frame_reader=None,
                  latest_frames=None, arm_binding=None, device_idle=None,
                  clock=None, binding_snapshot=None) -> None:
        self._env.update({
            "evidence_get": evidence_get, "frame_reader": frame_reader,
            "latest_frames": latest_frames, "arm_binding": arm_binding,
            "device_idle": device_idle, "binding_snapshot": binding_snapshot})
        if clock is not None:
            self._clock = clock

    def _now(self) -> datetime:
        return _region_now(self._clock)

    def _env_get(self, key: str):
        return self._env.get(key)

    # ================= 预览 =================
    def preview(self, device_id: str, request: dict, principal: str) -> dict:
        rect = _parse_norm_rect(request.get("rect"))
        evidence_id = request.get("source_evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise RegionError("camera_request_invalid",
                              "source_evidence_id 必填（原框选证据）")
        # ① 原框选证据：设备归属 + 冻结来源身份（缺可信字段拒绝）
        evidence_get = self._env_get("evidence_get")
        if evidence_get is None:
            raise RegionError("source_unavailable", "证据读取器未接入")
        record = evidence_get(evidence_id)
        if record is None or record.get("device_id") != device_id \
                or record.get("evidence_type") != "camera_source_v2":
            raise RegionError("camera_not_found",
                              "框选证据不存在或不属于该设备")
        identity = self._source_identity(record)
        width = int(identity["width"])
        height = int(identity["height"])
        pixel_rect = _pixel_rect_from_norm(rect, width, height)
        # ② 当前新鲜可信源帧（同设备/安装/来源几何）
        frame_record, frame_bytes = self._fresh_frame(device_id, identity)
        image = _decode_jpeg(frame_bytes)
        if image.shape[1] != width or image.shape[0] != height:
            raise RegionError("source_binding_changed",
                              "当前帧尺寸与框选证据不一致（来源几何已变化）")
        identity = self._source_identity(frame_record)
        # ③ 粗框内实际检测（真实 OpenCV；粗框不是四角）
        outcome = _detect_in_rect(image, pixel_rect)
        preview_id = str(uuid.uuid4())
        now = self._now()
        arm_getter = self._env_get("arm_binding")
        arm_id = arm_getter(device_id) if callable(arm_getter) else ""
        if not isinstance(arm_id, str) or not arm_id.strip():
            raise RegionError("source_binding_changed", "缺少已登记的机械臂绑定身份，请先核对设备绑定")
        installation_binding = {
            "camera_id": identity["camera_id"],
            "camera_device_unique_id": identity["camera_device_unique_id"],
            "camera_binding_id": identity["camera_binding_id"],
            "arm_binding_id": arm_id,
        }
        source_binding = {
            "device_id": device_id,
            "installation_binding": installation_binding,
            "source_size": {"width": width, "height": height},
            "source_geometry_id": identity["source_geometry_id"],
            "evidence_id": frame_record["evidence_id"],  # 检测帧（可与框选证据不同）
            "source_session_id": identity["source_generation_id"],
            "frame_seq": identity["source_frame_seq"],
            "source_captured_at": identity["source_captured_at"],
            "server_received_at": identity["source_received_at"],
        }
        # 契约 region_digest：覆盖来源身份+框（不只 rect）——保存/回滚核对
        # 绑定与几何不被偷换
        region_digest = canonical_digest({
            "device_id": device_id,
            "installation_binding": installation_binding,
            "source_size": {"width": width, "height": height},
            "source_geometry_id": identity["source_geometry_id"],
            "rect": rect,
            "pixel_rect": pixel_rect,
        })
        record = {
            "preview_id": preview_id,
            "device_id": device_id,
            "principal_id": principal,
            "request_hash": content_sha({
                "rect": rect, "source_evidence_id": evidence_id}),
            "rect": rect,
            "pixel_rect": pixel_rect,
            "region_digest": region_digest,
            "source_binding": source_binding,
            "rect_evidence_id": evidence_id,
            "detected_quad": outcome["detected_quad"],
            "status": outcome["status"],
            "reason_codes": outcome["reason_codes"],
            "created_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
            "consumed_by": None,
        }
        self._persist_preview(record)
        self._store.save_preview(device_id, preview_id, record)
        return self._preview_view(record)

    def _source_identity(self, record: dict) -> dict:
        """经冻结构建器提取来源身份（缺可信字段 503，不补默认值）。"""
        from drivers.camera_broker import (SourceFrameContractError,
                                           build_source_frame)
        try:
            frame = build_source_frame(record)
        except SourceFrameContractError as exc:
            raise RegionError("source_unavailable", str(exc)) from exc
        return {
            "camera_id": frame["camera_id"],
            "camera_device_unique_id": frame["device_unique_id"],
            "camera_binding_id": frame["camera_binding_id"],
            "source_geometry_id": frame["source_geometry_id"],
            "source_generation_id": frame["source_generation_id"],
            "source_frame_seq": frame["source_frame_seq"],
            "source_captured_at": frame["source_captured_at"],
            "source_received_at": frame["source_received_at"],
            "width": frame["width"], "height": frame["height"],
        }

    def _fresh_frame(self, device_id: str, identity: dict,
                     *, max_age: float = 2.0) -> tuple[dict, bytes]:
        latest = self._env_get("latest_frames")
        if latest is None:
            raise RegionError("source_unavailable", "来源读取器未接入")
        rows = latest(device_id, 1)
        if not rows:
            raise RegionError(
                "source_unavailable",
                "当前没有可用的 v2 来源帧：请确认采集进行中")
        record = rows[0]
        if record.get("device_id") != device_id:
            raise RegionError("source_unavailable", "来源帧设备不匹配")
        self._check_freshness(record, max_age=max_age)
        current = self._source_identity(record)
        if current["camera_binding_id"] != identity["camera_binding_id"] \
                or current["source_geometry_id"] != identity["source_geometry_id"] \
                or current["camera_id"] != identity["camera_id"] \
                or current["camera_device_unique_id"] != identity["camera_device_unique_id"] \
                or (current["width"], current["height"]) != (
                    identity["width"], identity["height"]):
            raise RegionError(
                "source_binding_changed",
                "当前来源与框选证据的安装/来源几何不一致：请重新框选")
        reader = self._env_get("frame_reader")
        if reader is None:
            raise RegionError("source_unavailable", "原图读取器未接入")
        try:
            data = reader(record["evidence_id"])
        except (FileNotFoundError, OSError) as exc:
            raise RegionError("archive_invalid",
                              f"来源帧原图读取失败：{exc}") from exc
        if hashlib.sha256(data).hexdigest() != record.get("jpeg_sha256"):
            raise RegionError("archive_invalid", "来源帧原图摘要与证据不一致")
        return record, data

    @staticmethod
    def _check_freshness(record: dict, *, max_age: float = 2.0) -> None:
        captured = record.get("captured_at")
        if not captured:
            raise RegionError("source_unavailable",
                              "归档证据缺少真实采样时刻")
        try:
            captured_dt = parse_iso(str(captured))
        except (ValueError, TypeError):
            raise RegionError("source_unavailable", "来源帧时间无效")
        age = (datetime.now(timezone.utc) - captured_dt).total_seconds()
        if age > max_age:
            raise RegionError(
                "source_unavailable",
                f"来源帧不新鲜（{age:.1f}s > {max_age:.0f}s）：请确认采集进行中")
        if age < -0.5:
            raise RegionError("source_unavailable",
                              f"来源帧时间来自未来（{-age:.1f}s）：拒绝")

    def _persist_preview(self, record: dict) -> None:
        if self._state is None:
            return
        self._state.state_insert(
            "region_preview",
            state_key("region_preview", state_identity("region_preview", record)),
            record["device_id"], record["principal_id"], record)

    def _load_preview(self, preview_id: str) -> dict | None:
        stored = self._store.get_preview(preview_id)
        if stored is not None:
            return stored
        if self._state is None:
            return None
        return self._state.state_get(
            "region_preview", state_key("region_preview", [preview_id]))

    @staticmethod
    def _preview_view(record: dict) -> dict:
        return {key: record[key] for key in (
            "contract_version", "correlation_id", "preview_id",
            "region_digest", "rect", "pixel_rect", "source_binding",
            "detected_quad", "status", "reason_codes", "created_at",
            "expires_at") if key in record} | {
            "contract_version": PREVIEW_VERSION,
        }

    # ================= 保存 =================
    def delete_pointer(self, device_id: str) -> dict:
        """撤销生效范围（业主 2026-10-16：删错框重来的正规入口）。委托存储层。"""
        return self._store.delete_pointer(device_id)

    def save(self, device_id: str, request: dict, principal: str) -> dict:
        correlation_id = request.get("correlation_id")
        client_request_id = request.get("client_request_id")
        expected_revision = request.get("expected_revision")
        publish = request.get("publish")
        if type(publish) is not bool:
            raise RegionError("camera_request_invalid", "publish 必须为布尔")
        if not isinstance(expected_revision, int) \
                or isinstance(expected_revision, bool) or expected_revision < 0:
            raise RegionError("camera_request_invalid",
                              "expected_revision 必须为非负整数")
        # ① 幂等：同编号同内容返回原响应（不重放副作用）
        replay = self._replay(client_request_id, device_id, request)
        if replay is not None:
            return replay
        # ② 预览：存在/归属/期限/未消费
        preview = self._load_preview(str(request.get("preview_id") or ""))
        if preview is None or preview.get("device_id") != device_id:
            raise RegionError("camera_not_found", "预览不存在或不属于该设备")
        if preview.get("consumed_by") is not None:
            raise RegionError("request_conflict",
                              "同一预览只能保存一次；请重新预览")
        if self._now() >= parse_iso(preview["expires_at"]):
            raise RegionError("preview_expired", "预览已超过 120 秒有效期")
        if preview.get("region_digest") != request.get("region_digest"):
            raise RegionError("camera_request_invalid", "region_digest 不匹配")
        # ③ 来源复验：当前新鲜帧与预览来源一致
        identity = self._source_identity(self._env_get("evidence_get")(
            preview["source_binding"]["evidence_id"]))
        _frame_record, _frame_bytes = self._fresh_frame(device_id, identity)
        arm_getter = self._env_get("arm_binding")
        if callable(arm_getter) and arm_getter(device_id) != \
                preview["source_binding"]["installation_binding"]["arm_binding_id"]:
            raise RegionError("source_binding_changed", "机械臂绑定已变化，请重新预览粗框")
        operation_id = str(uuid.uuid4())
        if publish:
            # 检测门已撤（业主 2026-10-16 拍板：人工画框直接采信——画框人必
            # 把手机画入范围）。检测状态仅作预览参考信息，不再拦截发布；下
            # 游校准向导/姿态门在使用时仍以 detect_screen_quad 实际定位，框
            # 画得不好会在使用时大声失败，不会静默错。
            idle_getter = self._env_get("device_idle")
            if idle_getter is not None and not idle_getter(device_id):
                raise RegionError("device_busy",
                                  "设备忙（有任务租约）：发布要求设备空闲")
            return self._publish(device_id, preview, request, principal,
                                 expected_revision, operation_id)
        return self._save_draft(device_id, preview, request, principal,
                                operation_id)

    def _publish(self, device_id: str, preview: dict, request: dict,
                 principal: str, expected_revision: int,
                 operation_id: str) -> dict:
        pointer = self._store.get_pointer(device_id)
        current_revision = int(pointer["revision"]) if pointer else 0
        if current_revision != int(expected_revision):
            raise RegionError(
                "revision_conflict",
                f"指针版本冲突（当前 {current_revision}，期望 {expected_revision}）")
        current_version, _current = self._store.latest_version(device_id)
        new_version = current_version + 1
        binding = preview["source_binding"]["installation_binding"]
        record = {
            "contract_version": CONTRACT_VERSION,
            "device_id": device_id,
            "version": new_version,
            "installation_binding": binding,
            "source_size": preview["source_binding"]["source_size"],
            "source_geometry_id": preview["source_binding"]["source_geometry_id"],
            "rect": preview["rect"],
            "pixel_rect": preview["pixel_rect"],
            "reference_evidence_id": preview["source_binding"]["evidence_id"],
            "created_at": iso(self._now()),
            "created_by": principal,
        }
        record["sha256"] = canonical_digest(record)
        # §9 固定顺序：intent(prepared) → 版本文件（版本表） → 指针 CAS → 提交
        if self._state is not None:
            self._state.operation_prepare(
                principal_id=principal, device_id=device_id,
                resource_kind="region", resource_id=device_id,
                client_request_id=request["client_request_id"],
                request={k: v for k, v in request.items()
                         if k not in ("correlation_id",)},
                planned_response={"saved_version": new_version,
                                  "status": "verified"},
                files=[], pointer_before={
                    "revision": current_revision,
                    "version": pointer["selected_version"] if pointer else None,
                    "sha256": pointer["selected_sha256"] if pointer else None},
                pointer_after={"revision": current_revision + 1,
                               "version": new_version,
                               "sha256": record["sha256"]},
                mutations=[],
                version_ref={"id": device_id, "version": new_version,
                             "sha256": record["sha256"]},
                consume=[{"kind": "region_preview",
                          "state_key": state_key("region_preview",
                                                 [preview["preview_id"]]),
                          "client_request_id": request["client_request_id"]}],
                operation_id=operation_id)
        self._store.insert_version(device_id, new_version, record)
        pointer = self._store.cas_pointer(
            device_id, current_revision, new_version, record["sha256"],
            operation_id)
        if self._state is not None:
            self._state.operation_commit(operation_id, response={
                "saved_version": new_version, "status": "verified",
                "revision": pointer["revision"],
                "selected_version": new_version})
            self._consume_preview(preview["preview_id"],
                                  request["client_request_id"])
        response = {
            "contract_version": WRITE_VERSION,
            "correlation_id": request.get("correlation_id"),
            "operation_id": operation_id,
            "revision": pointer["revision"],
            "saved_version": new_version,
            "selected_version": new_version,
            "status": "verified",
            "replayed": False,
        }
        return response

    def _save_draft(self, device_id: str, preview: dict, request: dict,
                    principal: str, operation_id: str) -> dict:
        draft_version, _ = self._store.latest_draft(device_id)
        draft_version = max(draft_version,
                            self._store.latest_version(device_id)[0]) + 1
        record = {
            "contract_version": CONTRACT_VERSION,
            "device_id": device_id,
            "version": draft_version,
            "installation_binding":
                preview["source_binding"]["installation_binding"],
            "source_size": preview["source_binding"]["source_size"],
            "source_geometry_id": preview["source_binding"]["source_geometry_id"],
            "rect": preview["rect"],
            "pixel_rect": preview["pixel_rect"],
            "reference_evidence_id": preview["source_binding"]["evidence_id"],
            "created_at": iso(self._now()),
            "created_by": principal,
            "status": "draft",
        }
        sha = self._store.save_draft(device_id, draft_version, record)
        if self._state is not None:
            self._state.operation_prepare(
                principal_id=principal, device_id=device_id,
                resource_kind="region", resource_id=device_id,
                client_request_id=request["client_request_id"],
                request={k: v for k, v in request.items()
                         if k not in ("correlation_id",)},
                planned_response={"saved_version": draft_version,
                                  "status": "draft_saved"},
                files=[], pointer_before=None, pointer_after=None,
                mutations=[], version_ref=None,
                consume=[{"kind": "region_preview",
                          "state_key": state_key("region_preview",
                                                 [preview["preview_id"]]),
                          "client_request_id": request["client_request_id"]}],
                operation_id=operation_id)
            self._state.operation_commit(operation_id, response={
                "saved_version": draft_version, "status": "draft_saved"})
            self._consume_preview(preview["preview_id"],
                                  request["client_request_id"])
        pointer = self._store.get_pointer(device_id)
        return {
            "contract_version": WRITE_VERSION,
            "correlation_id": request.get("correlation_id"),
            "operation_id": operation_id,
            "revision": int(pointer["revision"]) if pointer else 0,
            "saved_version": draft_version,
            "selected_version": (pointer or {}).get("selected_version"),
            "status": "draft_saved",
            "replayed": False,
        }

    def _consume_preview(self, preview_id: str, client_request_id: str) -> None:
        if self._state is None:
            return
        try:
            self._state.state_consume(
                "region_preview", state_key("region_preview", [preview_id]),
                client_request_id)
        except LayoutStateError as exc:
            if exc.code == "layout_request_conflict":
                return  # 已由同事务消费（typed 行 revision=2）
            raise
        stored = self._store.get_preview(preview_id)
        if stored is not None:
            stored["consumed_by"] = client_request_id
            self._store.save_preview(stored["device_id"], preview_id, stored)

    def _replay(self, client_request_id: str, device_id: str,
                request: dict) -> dict | None:
        if self._state is None or not client_request_id:
            return None
        operation = self._state.operation_get_by_request(client_request_id)
        if operation is None:
            return None
        if operation.get("device_id") != device_id:
            raise RegionError("request_conflict",
                              "client_request_id 已被其他设备使用")
        intent = self._state.intent_get(operation["operation_id"])
        if intent is not None:
            identity = {k: v for k, v in request.items()
                        if k != "correlation_id"}
            if intent.get("request_hash") != content_sha(
                    _strip_wrapper(request)):
                raise RegionError("request_conflict",
                                  "同 client_request_id 携带了不同内容")
        response = (operation.get("response") or {})
        saved_version = response.get("saved_version")
        if saved_version is None:
            raise RegionError(
                "request_conflict",
                "同编号操作未完成提交；请按 operation 查询对账")
        pointer = self._store.get_pointer(device_id)
        return {
            "contract_version": WRITE_VERSION,
            "correlation_id": request.get("correlation_id"),
            "operation_id": operation["operation_id"],
            "revision": int(pointer["revision"]) if pointer else 0,
            "saved_version": int(saved_version),
            "selected_version": response.get("selected_version")
            or (pointer or {}).get("selected_version"),
            "status": response.get("status", "verified"),
            "replayed": True,
        }

    # ================= 读取/版本 =================
    def get(self, device_id: str) -> dict:
        # 指针已删（撤销生效范围后重画路径，业主 2026-10-16）≠ 数据损坏：
        # 版本历史保留作审计，读取按「未登记」返回（revision 0），面板照常
        # 允许重新框选。指针存在时才走 load_region_context 的完整校验
        # （摘要/字段损坏仍终态拒绝）。
        pointer = self._store.get_pointer(device_id)
        record = None
        if pointer is not None:
            from runtime.phone_region_context import RegionContextError, load_region_context

            try:
                context = load_region_context(self._store, device_id)
            except RegionContextError as exc:
                raise RegionError("items_unverified", exc.message) from exc
            pointer = context["pointer"]
            record = context["region"]
        draft_version, _draft = self._store.latest_draft(device_id)
        reasons: list[str] = []
        selected_ref = None
        if record is not None:
            selected_ref = {"id": device_id, "version": record["version"],
                            "sha256": record.get("sha256")}
        pending_ref = None
        if _draft is not None:
            pending_ref = {"id": device_id, "version": draft_version,
                           "sha256": _draft.get("sha256")}
        if record is None and _draft is None:
            status = "unregistered"
        else:
            status = "verified"
            binding_getter = self._env_get("binding_snapshot")
            if record is not None and callable(binding_getter):
                # binding_snapshot 提供器返回当前受信 camera_binding_id
                current_binding_id = binding_getter(device_id)
                bound = (record.get("installation_binding") or {}).get(
                    "camera_binding_id")
                if current_binding_id and bound \
                        and current_binding_id != bound:
                    status = "needs_review"
                    reasons.append("source_binding_changed")
            arm_getter = self._env_get("arm_binding")
            if record is not None and callable(arm_getter) and arm_getter(device_id) != \
                    (record.get("installation_binding") or {}).get("arm_binding_id"):
                status = "needs_review"
                if "source_binding_changed" not in reasons:
                    reasons.append("source_binding_changed")
            if pending_ref is not None and pointer is None:
                status = "needs_review"
                reasons.append("phone_not_located")
        return {
            "contract_version": READ_VERSION,
            "correlation_id": str(uuid.uuid4()),
            "revision": int(pointer["revision"]) if pointer else 0,
            "selected": selected_ref,
            "pending_draft": pending_ref,
            "status": status,
            "reasons": reasons,
        }

    def get_version_record(self, device_id: str, version: int) -> dict | None:
        record = self._store.get_version(device_id, version)
        if record is not None:
            return record
        return self._store.get_draft(device_id, version)

    # ================= 范围回滚（契约 §8；kind=phone_search_region） ========
    ROLLBACK_TTL_SECONDS = 120

    def rollback_preview(self, device_id: str, request: dict,
                         principal: str) -> dict:
        kind = request.get("kind")
        if kind != "phone_search_region":
            raise RegionError("request_conflict",
                              "区域回滚入口只接受 kind=phone_search_region")
        target_ref = request.get("target_ref") or {}
        if target_ref.get("id") != device_id:
            raise RegionError("camera_not_found", "回滚目标不属于该设备")
        target = self._store.get_version(device_id,
                                         int(target_ref.get("version") or 0))
        if target is None or (target_ref.get("sha256")
                              and target.get("sha256")
                              != target_ref["sha256"]):
            raise RegionError("camera_not_found",
                              "回滚目标版本不存在或摘要不符")
        pointer = self._store.get_pointer(device_id)
        current_revision = int(pointer["revision"]) if pointer else 0
        if int(request.get("expected_revision") or -1) != current_revision:
            raise RegionError(
                "revision_conflict",
                f"指针修订冲突（当前 {current_revision}，期望 "
                f"{request.get('expected_revision')}）")
        status, reasons = "verified", []
        source_evidence_id = request.get("source_evidence_id")
        try:
            rows = (self._env_get("latest_frames") or (lambda *a: []))(
                device_id, 1)
            if not rows:
                raise RegionError("source_unavailable", "当前没有来源帧")
            current = rows[0]
            self._check_freshness(current)
            if current.get("evidence_id") != source_evidence_id:
                status = "needs_review"
                reasons.append("source_unavailable")
            current_binding = self._source_identity(current)
            if current_binding["camera_binding_id"] \
                    != (target.get("installation_binding") or {}).get(
                        "camera_binding_id"):
                status = "needs_review"
                reasons.append("source_binding_changed")
            if current_binding["source_geometry_id"] \
                    != target.get("source_geometry_id"):
                status = "needs_review"
                reasons.append("geometry_changed")
        except RegionError as exc:
            if exc.code in ("source_unavailable", "archive_invalid"):
                status, reasons = "needs_review", [exc.code.replace(
                    "source_unavailable", "source_unavailable")]
            else:
                raise
        preview_id = str(uuid.uuid4())
        now = self._now()
        record = {
            "preview_id": preview_id,
            "device_id": device_id,
            "principal_id": principal,
            "kind": kind,
            "target_ref": {"id": target_ref.get("id"),
                           "version": int(target_ref["version"]),
                           "sha256": target.get("sha256")},
            "expected_revision": current_revision,
            "status": status,
            "reason_codes": reasons,
            "request_hash": content_sha(
                {k: v for k, v in request.items()
                 if k not in ("correlation_id",)}),
            "created_at": iso(now),
            "expires_at": iso(now + timedelta(
                seconds=self.ROLLBACK_TTL_SECONDS)),
            "consumed_by": None,
        }
        if self._state is not None:
            self._state.state_insert(
                "rollback_preview",
                state_key("rollback_preview",
                          state_identity("rollback_preview", record)),
                device_id, principal, record)
        return {"preview_id": preview_id, "kind": kind,
                "target_ref": record["target_ref"],
                "expected_revision": current_revision,
                "status": status, "reason_codes": reasons,
                "created_at": record["created_at"],
                "expires_at": record["expires_at"]}

    def rollback_commit(self, device_id: str, request: dict,
                        principal: str) -> dict:
        client_request_id = request["client_request_id"]
        replay = self._rollback_replay(client_request_id, device_id, request)
        if replay is not None:
            return replay
        preview_id = str(request.get("preview_id") or "")
        record = None
        if self._state is not None:
            record = self._state.state_get(
                "rollback_preview",
                state_key("rollback_preview", [preview_id]))
        if record is None or record.get("device_id") != device_id \
                or record.get("principal_id") != principal:
            raise RegionError("camera_not_found", "回滚预览不存在")
        if record.get("consumed_by") is not None:
            raise RegionError("request_conflict", "回滚预览已消费")
        if self._now() >= parse_iso(record["expires_at"]):
            raise RegionError("preview_expired", "回滚预览已过期")
        pointer = self._store.get_pointer(device_id)
        current_revision = int(pointer["revision"]) if pointer else 0
        if current_revision != int(record["expected_revision"]):
            raise RegionError(
                "revision_conflict",
                f"指针修订冲突（当前 {current_revision}，期望 "
                f"{record['expected_revision']}）")
        operation_id = str(uuid.uuid4())
        review_id = str(uuid.uuid4())
        pointer = self._store.cas_pointer(
            device_id, current_revision, int(record["target_ref"]["version"]),
            record["target_ref"]["sha256"], operation_id)
        if self._state is not None:
            self._state.operation_prepare(
                principal_id=principal, device_id=device_id,
                resource_kind="region", resource_id=device_id,
                client_request_id=client_request_id,
                request={k: v for k, v in request.items()
                         if k not in ("correlation_id",)},
                planned_response={"pointer": pointer,
                                  "review_id": review_id}, files=[],
                version_ref=None,
                pointer_before={"revision": current_revision,
                                "version": pointer["selected_version"],
                                "sha256": pointer["selected_sha256"]},
                pointer_after={"revision": pointer["revision"],
                               "version": pointer["selected_version"],
                               "sha256": pointer["selected_sha256"]},
                mutations=[],
                consume=[{"kind": "rollback_preview",
                          "state_key": state_key("rollback_preview",
                                                 [preview_id]),
                          "client_request_id": client_request_id}],
                operation_id=operation_id)
            self._state.operation_commit(operation_id,
                                         response={"pointer": pointer,
                                                   "review_id": review_id})
            try:
                self._state.state_consume(
                    "rollback_preview",
                    state_key("rollback_preview", [preview_id]),
                    client_request_id)
            except LayoutStateError as exc:
                if exc.code != "layout_request_conflict":
                    raise
            self._state.review_record_save(
                review_id, device_id,
                {"review_id": review_id, "kind": "rollback",
                 "device_id": device_id, "operation_id": operation_id,
                 "target_ref": record["target_ref"],
                 "verified_at": iso(self._now()),
                 "note": "范围回滚：当前来源与目标绑定/几何一致后指针前进；"
                         "旧版本内容不变"})
        return {"operation_id": operation_id, "pointer": pointer,
                "review_id": review_id, "replayed": False}

    def _rollback_replay(self, client_request_id: str, device_id: str,
                         request: dict) -> dict | None:
        if self._state is None or not client_request_id:
            return None
        operation = self._state.operation_get_by_request(client_request_id)
        if operation is None:
            return None
        if operation.get("device_id") != device_id:
            raise RegionError("request_conflict",
                              "client_request_id 已被其他设备使用")
        response = operation.get("response") or {}
        if not response.get("pointer"):
            raise RegionError("request_conflict",
                              "同编号操作未完成提交；请按 operation 查询")
        return {"operation_id": operation["operation_id"],
                "pointer": response["pointer"],
                "review_id": response.get("review_id"),
                "replayed": True}


def _strip_wrapper(request: dict) -> dict:
    """幂等 request_hash 的内容（去除关联字段）。"""
    return {k: v for k, v in request.items()
            if k not in ("correlation_id",)}
