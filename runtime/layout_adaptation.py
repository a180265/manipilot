"""L03：设备布局适配（会话/采集/逐页服务端核验/预览/发布/版本/复核）。

R2-25（2026-10-04 第二轮）：适配服务由 Runtime.layout_bundle 持有多实例；
不再提供进程级 `shared` 单例（正式装配禁止进程级共享；相同 Runtime 复用自身
实例，不同 Runtime/测试根互相独立；close() 只释放本实例状态）。

R2-16（FIX-13）：四态通过必须由服务端证据推导——
- 通过注入的 capture 提供器取得受控两帧（真实采样推进/间隔/新鲜度/归属核验），
  采集按 §6 绑定一次消费、首次接纳时间不可变；
- 服务端读取真实图像 + 模板定义逐态逐项匹配（runtime/layout_matching.py），
  accepted/failed/blocking 全部由结果生成，客户端传入的 passed/check 一律不信任
  （session_page 请求按冻结字段封闭，多余字段拒绝）；
- 发布消费不可变预览（typed 一次消费行）并重验当前会话修订/身份/安装/v3/期限；
  预览后又写页（会话修订变化）不能消费旧结果。

R2-19（FIX-14）：发布按 §9 持久协议执行——operation/intent/原响应、不可变版本
文件、适配指针、最终 typed mutations/消费记录全部落 `layout_typed_state`/
`layout_review_records`（runtime/layout_state.py）；版本由持久预约分配；
by-request 幂等：同编号同内容返回原响应，异内容 409；启动对账在 StateStore。

session/preview 为实例态：服务重启（server_instance_id 变化）后未发布的
session/preview 失效；已发布适配版本/指针从持久资料恢复，不失效。
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from runtime.layout_matching import _pixel_rect, collect_anchor_bytes, compute_page_check
from runtime.layout_geometry import (
    LayoutGeometryError, merge_page_checks, rectify_capture,
)
from runtime.layout_state import (
    LayoutStateError,
    LayoutStateStore,
    canonical_json,
    content_sha,
    state_identity,
    state_key,
)
from runtime.util import iso, parse_iso, utc_now

# 四态闭集与固定顺序（schema page_state）
from runtime.layout_pages import LEGACY_STATES, page_states, valid_page_state

PAGE_STATES = LEGACY_STATES
SESSION_TTL_SECONDS = 30 * 60          # session 30 分钟
PAGE_EVIDENCE_TTL_SECONDS = 15 * 60    # 页证据自首次接纳 15 分钟
PREVIEW_TTL_SECONDS = 120              # preview 120 秒
CAPTURE_TTL_SECONDS = 120              # capture 120 秒内第一次交给页面 PUT
ITEM_STATUSES = ("passed", "failed", "unsupported")
SESSION_CONTRACT = "device-layout-edit.v1"

LEGACY_VERSION_KEY = "adaptation_version:{device_id}:{version}"
LEGACY_POINTER_KEY = "adaptation_pointer:{device_id}"


class AdaptationError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def canonical_digest(value) -> str:
    return content_sha(value)


def _require_uuid(value, field: str) -> str:
    text = str(value or "")
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError):
        raise AdaptationError("invalid_request",
                              f"{field} 必须是 UUID")


class LayoutAdaptationService:
    """适配会话/采集/逐页核验/预览/CAS 发布/版本读取（按 Runtime 实例持有）。

    会话与预览为实例态（重启失效）；已发布版本、指针与幂等台账在持久
    StateStore（同库多实例隔离）。客户端不提供任何通过事实。
    """

    def __init__(self, store=None, template_store=None, clock=None,
                 state_store: LayoutStateStore | None = None,
                 server_instance_id: str | None = None,
                 capture_provider=None, calibration_provider=None,
                 binding_provider=None, frame_reader=None,
                 region_ref_provider=None, ime_provider=None,
                 region_record_provider=None):
        self._sessions: dict[str, dict] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._registry_lock = threading.Lock()
        self._store = store                      # legacy KV：只读对账/迁移
        self._state = state_store
        # server_instance_id 冻结契约要求 UUID 形（layout:evidence 行）；
        # 非 UUID 形的装配值派生为稳定 UUID（同装配值 → 同 UUID，语义不变）
        raw_instance = str(server_instance_id or uuid.uuid4())
        try:
            uuid.UUID(raw_instance)
            self._server_instance_id = raw_instance
        except ValueError:
            self._server_instance_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"layout-instance:{raw_instance}"))
        self._template_store = template_store
        self._capture_provider = capture_provider
        self._calibration_provider = calibration_provider
        self._binding_provider = binding_provider
        self._frame_reader = frame_reader
        self._region_ref_provider = region_ref_provider
        self._region_record_provider = region_record_provider
        self._ime_provider = ime_provider
        self._previews: dict[str, dict] = {}
        self._clock = clock or utc_now
        self._closed = False

    # ---- 生命周期（R2-25：close 只释放本实例；不依赖/不影响其他实例） ----
    def close(self) -> None:
        """关机释放自己的会话/预览状态与设备锁登记（幂等）。"""
        self._closed = True
        with self._registry_lock:
            self._sessions.clear()
            self._locks.clear()

    def attach_providers(self, *, capture_provider=None,
                         calibration_provider=None, binding_provider=None,
                         frame_reader=None, region_ref_provider=None,
                         ime_provider=None, region_record_provider=None) -> None:
        """HTTP 层接入 Runtime 事实源（幂等；服务逻辑不依赖调用顺序）。

        R2-16/R2-17：正式装配（app 布局段）把 EvidenceStore 受控帧读取、
        v3 校准事实、当前安装/IME 绑定、区域指针接进服务；缺提供器时相关
        能力按既定错误拒绝（不回落自报字段）。
        """
        if capture_provider is not None:
            self._capture_provider = capture_provider
        if calibration_provider is not None:
            self._calibration_provider = calibration_provider
        if binding_provider is not None:
            self._binding_provider = binding_provider
        if frame_reader is not None:
            self._frame_reader = frame_reader
        if region_ref_provider is not None:
            self._region_ref_provider = region_ref_provider
        if region_record_provider is not None:
            self._region_record_provider = region_record_provider
        if ime_provider is not None:
            self._ime_provider = ime_provider

    def _require_open(self) -> None:
        if self._closed:
            raise AdaptationError("service_closed",
                                  "适配服务已关闭（本 Runtime 已关机）",
                                  status=503)

    # ---- 基础工具 ----
    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _device_lock(self, device_id: str) -> threading.RLock:
        with self._registry_lock:
            return self._locks.setdefault(device_id, threading.RLock())

    def _age_seconds(self, since: str | None) -> float:
        if not since:
            return float("inf")
        start = parse_iso(since)
        return (self._now() - start).total_seconds()

    def _server_id(self) -> str:
        return self._server_instance_id

    # ---- 模板/资源解析（start_session 与 GET 同一解析器，R2-20） ----
    def _resolve_template(self, template_id: str, version: int) -> dict | None:
        if self._template_store is None:
            return None
        if hasattr(self._template_store, "resolve_template"):
            return self._template_store.resolve_template(template_id, version)
        return self._template_store.get_template(template_id, version)

    def _template_asset_bytes(self, owner_id: str, version: int | None,
                              asset_id: str) -> bytes | None:
        """R3-13：模板资产读取走统一可见性解析器（版本感知；catalog 已提交
        才可读）。旧直接 store 兜底保留给非正式装配（仅只读历史）。"""
        if self._template_store is None:
            return None
        if hasattr(self._template_store, "resolve_template_asset"):
            return self._template_store.resolve_template_asset(
                owner_id, version, asset_id)
        return self._template_store.get_asset(owner_id, asset_id)

    def _read_frame_bytes(self, evidence_id: str) -> bytes:
        if self._frame_reader is not None:
            return self._frame_reader(evidence_id)
        raise AdaptationError("source_unavailable",
                              "未接入帧读取器，无法读取受控证据原图",
                              status=503)

    # ================= 会话 =================
    def start_session(self, device_id: str, request: dict) -> dict:
        self._require_open()
        # 冻结字段：template_ref 固定版本；旧内部字段 template_id/version 可转换
        template_ref = request.get("template_ref")
        if isinstance(template_ref, dict):
            template_id = template_ref.get("id")
            template_version = template_ref.get("version")
            template_sha = template_ref.get("sha256")
        else:
            template_id = request.get("template_id")
            template_version = request.get("template_version")
            template_sha = None
        if not template_id or not isinstance(template_version, int) \
                or template_version < 1:
            raise AdaptationError(
                "invalid_request",
                "session 必须固定 template_ref（含 id 与正整数 version）")
        principal = self._principal(request)
        keyboard_mode = request.get("keyboard_geometry_mode") or "live_ime_v1"
        if keyboard_mode not in ("legacy_registered", "live_ime_v1"):
            raise AdaptationError("invalid_request",
                                  "keyboard_geometry_mode 必须为 "
                                  "legacy_registered|live_ime_v1")
        now = self._now()
        session_id = str(uuid.uuid4())
        template = self._resolve_template(template_id, template_version)
        if template is None:
            raise AdaptationError(
                "template_not_found",
                f"模板 {template_id} v{template_version} 不存在", status=404)
        if isinstance(template_sha, str) and template.get("sha256") \
                and template_sha != template["sha256"]:
            raise AdaptationError("template_mismatch",
                                  "template_ref.sha256 与已发布模板不符",
                                  status=409)
        record = {
            "contract_version": "layout-session.v1",
            "capture_session_id": session_id,
            "device_id": device_id,
            "principal_id": principal,
            "revision": 1,
            "template_id": template_id,
            "template_version": template_version,
            "template_ref": {"id": template_id, "version": template_version,
                             "sha256": template.get("sha256") or template_sha},
            "installation_binding": (
                self._binding_provider(device_id) if self._binding_provider
                else self._binding_snapshot(request)),
            "keyboard_geometry_mode": keyboard_mode,
            "expected_pointer_revision": int(
                request.get("expected_pointer_revision")
                or request.get("expected_revision") or 0),
            "pages": {},
            "status": "active",
            "server_instance_id": self._server_id(),
            "created_monotonic_ms": 0,
            "created_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=SESSION_TTL_SECONDS)),
        }
        if self._state is not None:
            key = state_key("session", state_identity("session", record))
            self._state.state_insert("session", key, device_id, principal,
                                     record)
        self._sessions[session_id] = record
        return self._session_view(record)

    @staticmethod
    def _principal(request: dict) -> str:
        """作者身份：正式装配由服务端认证注入（请求自报仅作显式传递）。"""
        principal = request.get("principal_id")
        if not principal:
            raise AdaptationError("author_missing",
                                  "缺少已认证作者身份 principal_id",
                                  status=403)
        return str(principal)

    @staticmethod
    def _binding_snapshot(request: dict) -> dict | None:
        binding = request.get("installation_binding") \
            or request.get("binding_snapshot")
        return binding

    def _load_session(self, session_id: str) -> dict | None:
        record = self._sessions.get(session_id)
        if record is None and self._state is not None:
            key = state_key("session", [session_id])
            stored = self._state.state_get("session", key)
            if stored is not None:
                if stored.get("server_instance_id") != self._server_id():
                    return None  # 服务重启：未发布 session 失效
                self._sessions[session_id] = stored
                record = stored
        return record

    def _persist_session(self, record: dict, expected_sql_revision: int) -> None:
        if self._state is None:
            return
        key = state_key("session", state_identity("session", record))
        self._state.state_update("session", key, record["device_id"],
                                 record["principal_id"],
                                 expected_sql_revision, record)

    def _get_session_checked(self, device_id: str, session_id: str) -> dict:
        session = self._load_session(session_id)
        if session is None:
            raise AdaptationError("session_not_found", f"会话 {session_id} 不存在",
                                  status=404)
        if session["device_id"] != device_id:
            raise AdaptationError(
                "device_mismatch",
                f"会话固定设备 {session['device_id']}，不能跨设备操作",
                status=403)
        if self._age_seconds(session.get("expires_at")) >= 0:
            raise AdaptationError("session_expired",
                                  "会话已过 30 分钟有效期", status=409)
        return session

    def get_session(self, device_id: str, session_id: str) -> dict | None:
        """按设备 scope 读取；错设备等同不存在（不泄漏他人会话）。"""
        self._require_open()
        try:
            session = self._load_session(session_id)
        except LayoutStateError:
            return None
        if session is None or session["device_id"] != device_id:
            return None
        return self._session_view(session)

    @staticmethod
    def _session_view(session: dict) -> dict:
        view = dict(session)
        view["pages"] = dict(session["pages"])
        view["session_id"] = session["capture_session_id"]
        return view

    # ================= 受控采集（服务端两帧，R2-16 §6） =================
    def capture(self, device_id: str, request: dict) -> dict:
        self._require_open()
        for field in ("owner_kind", "expected_owner_revision",
                      "controlled_test_confirmed"):
            if field not in request:
                raise AdaptationError("invalid_request", f"缺少 {field}")
        if request.get("controlled_test_confirmed") is not True:
            raise AdaptationError("invalid_request",
                                  "必须确认 controlled_test_confirmed")
        owner_kind = request.get("owner_kind")
        if owner_kind not in ("search_region", "draft", "session"):
            raise AdaptationError("invalid_request",
                                  "owner_kind 必须为 search_region|draft|session")
        client_request_id = _require_uuid(
            request.get("client_request_id"), "client_request_id")
        page_state = request.get("page_state")
        owner_id = request.get("owner_id")
        expected_owner_revision = int(request.get("expected_owner_revision") or 0)
        session = None
        if owner_kind == "session":
            if not owner_id or not valid_page_state(page_state):
                raise AdaptationError(
                    "invalid_request",
                    "session 采集必须携带会话 UUID 与四态 page_state")
            if expected_owner_revision < 1:
                raise AdaptationError("invalid_request",
                                      "expected_owner_revision 必须为正整数")
            session = self._get_session_checked(device_id, str(owner_id))
            if page_state not in self._session_states(session):
                raise AdaptationError("invalid_request", "页面未在会话固定模板中声明")
            # 作者身份在会话创建时固定：采集沿用会话作者（服务端事实），
            # 请求中的 principal 只能与之相符或省略
            supplied = request.get("principal_id")
            if supplied is not None and str(supplied) != session["principal_id"]:
                raise AdaptationError("author_mismatch",
                                      "采集作者与会话固定作者不符", status=403)
            principal = session["principal_id"]
            if int(session.get("revision") or 1) != expected_owner_revision:
                raise AdaptationError(
                    "revision_conflict",
                    f"会话修订冲突（当前 {session.get('revision')}，期望 "
                    f"{expected_owner_revision}）", status=409)
        else:
            principal = self._principal(request)
            if owner_kind == "search_region":
                if page_state is not None or owner_id is not None:
                    raise AdaptationError(
                        "invalid_request",
                        "search_region 采集 owner_id/page_state 必须为 null")
            else:
                if not owner_id or not valid_page_state(page_state):
                    raise AdaptationError(
                        "invalid_request",
                        "draft 采集必须携带 owner UUID 与四态 page_state")
                if expected_owner_revision < 1:
                    raise AdaptationError("invalid_request",
                                          "expected_owner_revision 必须为正整数")
        replay = self._replay_operation(client_request_id)
        if replay is not None:
            capture = self._replay_payload(replay, device_id, request)
            if capture is not None:
                return capture
        if self._capture_provider is None:
            raise AdaptationError("source_unavailable",
                                  "未接入受控采集提供器，无法取帧", status=503)
        if self._state is None:
            raise AdaptationError("storage_failed",
                                  "未接入持久状态库，采集不可用", status=503)
        self._require_v3_calibration(device_id, session, owner_kind)
        # §6：search_region 采一帧；draft/session 自动采两帧（页面状态变化证明）
        frame_count = 1 if owner_kind == "search_region" else 2
        try:
            frames = self._capture_provider(device_id, frame_count, page_state)
        except AdaptationError:
            raise
        except Exception as exc:  # 外部 IO 失败 → 来源不可用
            raise AdaptationError("source_unavailable",
                                  f"受控采集失败：{exc}", status=503)
        frames = self._check_frames(device_id, frames, session,
                                    expected_count=frame_count)
        now = self._now()
        capture_id = str(uuid.uuid4())
        evidence_rows = []
        for frame in frames:
            evidence_rows.append({
                "evidence_id": frame["evidence_id"],
                "device_id": device_id,
                "principal_id": principal,
                "capture_id": capture_id,
                "page_state": page_state,
                "source_binding": {
                    "device_id": device_id,
                    "installation_binding": frame["installation_binding"],
                    "source_size": frame["source_size"],
                    "source_geometry_id": frame["source_geometry_id"],
                    "evidence_id": frame["evidence_id"],
                    "source_session_id": frame["source_generation_id"],
                    "frame_seq": frame["source_frame_seq"],
                    "source_captured_at": iso(now),
                    "server_received_at": iso(now)},
                "timing_proof": {
                    "source_clock_id": frame["source_clock_id"],
                    "clock_registration_id": frame["clock_registration_id"],
                    "source_generation_id": frame["source_generation_id"],
                    "source_frame_seq": frame["source_frame_seq"],
                    "source_capture_seq": frame["source_capture_seq"],
                    "source_captured_monotonic_ns":
                        frame["source_captured_monotonic_ns"],
                    "source_capture_uncertainty_ms":
                        frame["source_capture_uncertainty_ms"],
                    "source_timing_provenance":
                        frame["source_timing_provenance"]},
                "image_sha256": frame["image_sha256"],
                "media_type": "image/jpeg",
                "server_instance_id": self._server_id(),
                "captured_monotonic_ms":
                    frame["source_captured_monotonic_ns"] // 1_000_000,
                "received_monotonic_ms": frame["received_monotonic_ms"],
                "privacy_scope": "controlled_test"})
        capture_record = {
            "capture_id": capture_id,
            "device_id": device_id,
            "principal_id": principal,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "owner_revision": expected_owner_revision,
            "state": page_state,
            "evidence": evidence_rows,
            "created_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=CAPTURE_TTL_SECONDS)),
            "consumed_by": None,
            "server_instance_id": self._server_id(),
        }
        self._persist_capture(capture_record)
        response = {"capture": self._capture_view(capture_record),
                    "source_evidence_ids": [e["evidence_id"]
                                            for e in evidence_rows],
                    "capture_id": capture_id}
        operation_id = self._register_operation(
            resource_kind="capture", resource_id=capture_id,
            device_id=device_id, principal_id=principal,
            client_request_id=client_request_id, request=request,
            planned_response=response)
        response["operation_id"] = operation_id
        return response

    def _require_v3_calibration(self, device_id: str, session: dict | None,
                                owner_kind: str) -> None:
        if owner_kind == "search_region":
            return  # 粗框不要求校准
        if self._calibration_provider is None:
            raise AdaptationError(
                "items_unverified",
                "四态抓页要求本台有效 v3 校准；未接入校准事实提供器时拒绝",
                status=409)
        bundle = self._calibration_provider(device_id)
        if not isinstance(bundle, dict) \
                or int(bundle.get("schemaVersion") or 0) != 3 \
                or not bundle.get("cameraBindingId") \
                or not bundle.get("sourceGeometryId"):
            raise AdaptationError("items_unverified",
                                  "本台缺少有效 v3 校准，四态抓页拒绝",
                                  status=409)
        if session is not None:
            snapshot = session.get("installation_binding") or {}
            if snapshot.get("camera_binding_id") \
                    and snapshot["camera_binding_id"] \
                    != bundle.get("cameraBindingId"):
                raise AdaptationError("binding_changed",
                                      "当前安装与本会话快照不一致", status=409)

    @staticmethod
    def _check_frames(device_id: str, frames: list, session: dict | None,
                      *, expected_count: int = 2) -> list:
        """来源门禁：归属一致 + 多帧同绑定/几何/时钟、序号严格推进、
        间隔 Δms-u1-u2>=200、每帧年龄 <=1000ms（真实采样字段，不补 now）。
        search_region 单帧只核验字段/年龄（无配对语义）。"""
        if not isinstance(frames, list) or len(frames) != expected_count:
            raise AdaptationError(
                "source_unavailable",
                f"受控采集必须恰 {expected_count} 帧"
                f"（search_region 单帧/draft-session 两帧）", status=503)
        if expected_count == 1:
            frame = frames[0]
            required = ("evidence_id", "jpeg", "installation_binding",
                        "source_size", "source_geometry_id",
                        "source_generation_id", "source_frame_seq",
                        "source_capture_seq", "source_clock_id",
                        "clock_registration_id",
                        "source_captured_monotonic_ns",
                        "source_capture_uncertainty_ms",
                        "source_timing_provenance", "received_monotonic_ms")
            missing = [key for key in required if frame.get(key) is None]
            if missing:
                raise AdaptationError(
                    "source_unavailable",
                    f"受控帧缺少可信来源字段 {missing}", status=503)
            if frame["installation_binding"].get("camera_binding_id") is None:
                raise AdaptationError("source_unavailable",
                                      "受控帧缺 installation_binding",
                                      status=503)
            age = (frame["received_monotonic_ms"]
                   - frame["source_captured_monotonic_ns"] / 1e6
                   + frame["source_capture_uncertainty_ms"])
            if age > 1000:
                raise AdaptationError(
                    "evidence_expired",
                    f"受控帧最坏年龄 {age:.1f}ms > 1000ms", status=409)
            frame["image_sha256"] = hashlib.sha256(frame["jpeg"]).hexdigest()
            return frames
        for frame in frames:
            required = ("evidence_id", "jpeg", "installation_binding",
                        "source_size", "source_geometry_id",
                        "source_generation_id", "source_frame_seq",
                        "source_capture_seq", "source_clock_id",
                        "clock_registration_id", "source_captured_monotonic_ns",
                        "source_capture_uncertainty_ms",
                        "source_timing_provenance", "received_monotonic_ms")
            missing = [key for key in required if frame.get(key) is None]
            if missing:
                raise AdaptationError(
                    "source_unavailable",
                    f"受控帧缺少可信来源字段 {missing}，不可用于动作/适配",
                    status=503)
        first, second = frames
        for frame in frames:
            if frame["installation_binding"].get("camera_binding_id") is None:
                raise AdaptationError("source_unavailable",
                                      "受控帧缺 installation_binding",
                                      status=503)
        same = ["installation_binding", "source_geometry_id",
                "source_generation_id", "source_clock_id",
                "clock_registration_id"]
        for key in same:
            if canonical_json(first[key]) != canonical_json(second[key]):
                raise AdaptationError("source_binding_changed",
                                      f"两帧 {key} 不一致，须同源同链",
                                      status=409)
        if session is not None:
            snapshot = session.get("installation_binding") or {}
            current = first["installation_binding"]
            for key in ("camera_binding_id", "arm_binding_id",
                        "camera_device_unique_id"):
                if snapshot.get(key) and snapshot.get(key) != current.get(key):
                    raise AdaptationError(
                        "source_binding_changed",
                        f"来源绑定 {key} 与会话快照不一致", status=409)
        if second["source_frame_seq"] <= first["source_frame_seq"] \
                or second["source_capture_seq"] <= first["source_capture_seq"]:
            raise AdaptationError("source_unavailable",
                                  "两帧采样序号未严格推进，不能证明页面状态变化",
                                  status=503)
        delta_ms = (second["source_captured_monotonic_ns"]
                    - first["source_captured_monotonic_ns"]) / 1e6
        worst_gap = delta_ms - first["source_capture_uncertainty_ms"] \
            - second["source_capture_uncertainty_ms"]
        if worst_gap < 200:
            raise AdaptationError(
                "source_unavailable",
                f"最坏采样间隔 {worst_gap:.1f}ms < 200ms，拒绝接受", status=503)
        for frame in frames:
            age = (frame["received_monotonic_ms"]
                   - frame["source_captured_monotonic_ns"] / 1e6
                   + frame["source_capture_uncertainty_ms"])
            if age > 1000:
                raise AdaptationError(
                    "evidence_expired",
                    f"受控帧最坏年龄 {age:.1f}ms > 1000ms，须重新采集",
                    status=409)
        for frame in frames:
            frame["image_sha256"] = hashlib.sha256(frame["jpeg"]).hexdigest()
        return frames

    def _persist_capture(self, capture_record: dict) -> None:
        with self._state.transaction():
            capture_key = state_key("capture", state_identity(
                "capture", capture_record))
            self._state.state_insert("capture", capture_key,
                                     capture_record["device_id"],
                                     capture_record["principal_id"],
                                     capture_record)
            for evidence in capture_record["evidence"]:
                evidence_key = state_key("evidence", state_identity(
                    "evidence", evidence))
                self._state.state_insert("evidence", evidence_key,
                                         evidence["device_id"],
                                         evidence["principal_id"], evidence)

    def _find_evidence_rows(self, device_id: str,
                            evidence_ids: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for row in self._state.state_scan("evidence", device_id):
            value = row["value"]
            if value.get("evidence_id") in evidence_ids:
                if value.get("server_instance_id") != self._server_id():
                    continue  # 重启前的证据不接入未发布流程
                found[value["evidence_id"]] = value
        return found

    @staticmethod
    def _capture_view(capture_record: dict) -> dict:
        return {k: v for k, v in capture_record.items()
                if k != "server_instance_id"}

    # ================= 逐页写入（服务端核验，不信任客户端 passed） =================
    _PAGE_FIELDS = ("client_request_id", "expected_session_revision",
                    "source_evidence_ids", "local_overrides")

    def write_page(self, device_id: str, session_id: str, state: str,
                   page: dict) -> dict:
        self._require_open()
        if not valid_page_state(state):
            raise AdaptationError("invalid_request", "page_state 页面状态无效")
        if not isinstance(page, dict):
            raise AdaptationError("invalid_request", "page 必须为对象")
        unknown = [key for key in page if key not in self._PAGE_FIELDS]
        if unknown:
            # 客户端 check/passed/status 等事实字段一律不接受
            raise AdaptationError(
                "invalid_request",
                f"页面请求不接受字段 {unknown}；通过事实只能来自服务端核验")
        client_request_id = _require_uuid(
            page.get("client_request_id"), "client_request_id")
        expected_revision = page.get("expected_session_revision")
        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise AdaptationError("invalid_request",
                                  "expected_session_revision 必须为正整数")
        evidence_ids = page.get("source_evidence_ids")
        if not isinstance(evidence_ids, list) or len(evidence_ids) != 2 \
                or not all(isinstance(e, str) and e for e in evidence_ids) \
                or len(set(evidence_ids)) != 2:
            raise AdaptationError(
                "page_evidence_invalid",
                "每页必须携带本台服务端采集的两帧 source_evidence_ids")
        replay = self._replay_operation(client_request_id)
        if replay is not None:
            prior = self._replay_payload(replay, device_id, page)
            if prior is not None:
                return prior
        if self._state is None:
            raise AdaptationError("storage_failed", "未接入持久状态库",
                                  status=503)
        overrides = page.get("local_overrides") or []
        self._check_overrides(overrides)
        with self._device_lock(device_id):
            session = self._get_session_checked(device_id, session_id)
            if state not in self._session_states(session):
                raise AdaptationError("invalid_request", "页面未在会话固定模板中声明")
            if int(session.get("revision") or 1) != expected_revision:
                raise AdaptationError(
                    "revision_conflict",
                    f"会话修订冲突（当前 {session.get('revision')}，期望 "
                    f"{expected_revision}）", status=409)
            evidences = self._find_evidence_rows(device_id, evidence_ids)
            missing = [e for e in evidence_ids if e not in evidences]
            if missing:
                raise AdaptationError(
                    "page_evidence_invalid",
                    f"证据 {missing} 不存在或不属于本会话/本设备", status=404)
            capture_ids = {e["capture_id"] for e in evidences.values()}
            if len(capture_ids) != 1:
                raise AdaptationError("page_evidence_invalid",
                                      "两帧必须来自同一次受控采集", status=409)
            capture_id = capture_ids.pop()
            capture = self._state.state_get(
                "capture", state_key("capture", [capture_id]))
            if capture is None or capture["device_id"] != device_id \
                    or capture.get("owner_id") != session_id \
                    or capture.get("state") != state:
                raise AdaptationError("page_evidence_invalid",
                                      "采集不属于该会话/该页面状态", status=409)
            if capture.get("consumed_by") is not None:
                raise AdaptationError("request_conflict",
                                      "采集已绑定其他页面编辑请求；请重新采集",
                                      status=409)
            if self._age_seconds(capture.get("expires_at")) >= 0:
                raise AdaptationError("capture_expired",
                                      "采集已超过 120 秒未交给页面 PUT",
                                      status=409)
            # R3-12：会话固定原图一次摆正（相机域→本台 v3 屏幕域）；页检测、
            # 锚点匹配、颜色/空输入参考全部使用摆正图，不把相机原图对
            # 屏幕域区域算 check
            rectified = self._rectify_evidence_to_screen(device_id, session,
                                                         capture)
            check = self._match_page(session, state, capture,
                                     rectified=rectified, overrides=overrides)
            accepted_at = capture["created_at"]  # 首次接纳=采集归档时间，不可变
            # 颜色样本：摆正图逐通道中位 RGB（§6；区域缺失回退前两个已通过
            # 必需锚点矩形并集；不足/低纹理拒绝，不猜 RGB）
            color_sample = self._sample_colors(
                state, check, rectified["png"],
                [e["evidence_id"] for e in capture["evidence"]],
                page_failed=check.get("status") != "passed")
            check = {k: v for k, v in check.items()
                     if not k.startswith("_")}
            if state in ("chat_ime_hidden", "chat_ime_empty"):
                # §6：hidden/empty 页保存受控空白输入区域参考图（摆正图真实裁图）
                color_sample["empty_input_asset_ref"] = (
                    self._archive_empty_input_reference(
                        device_id, session_id, state, check,
                        rectified["png"]))
            page_entry = {
                "state": state,
                "capture": {"state": state, "capture_id": capture_id,
                            "accepted_at": accepted_at,
                            "evidence_ids": list(evidence_ids),
                            "check": check},
                "local_overrides": list(overrides or []),
                "color_sample": color_sample,
                "rectify": rectified["summary"],
            }
            updated = dict(session)
            pages = dict(session["pages"])
            pages[state] = page_entry
            updated["pages"] = pages
            updated["revision"] = int(session.get("revision") or 1) + 1
            self._persist_session(updated, int(session.get("revision") or 1))
            self._sessions[session_id] = updated
            capture_key = state_key("capture", [capture_id])
            self._state.state_consume("capture", capture_key,
                                      client_request_id)
            response = {"session_revision": updated["revision"],
                        "page": {"state": state, "capture": {
                            "state": state, "capture_id": capture_id,
                            "accepted_at": accepted_at,
                            "evidence_ids": list(evidence_ids)}},
                        "check": check}
            operation_id = self._register_operation(
                resource_kind="adaptation", resource_id=session_id,
                device_id=device_id,
                principal_id=session["principal_id"],
                client_request_id=client_request_id, request=page,
                planned_response=response)
            response["operation_id"] = operation_id
            return {**response, "session_id": session_id}

    def _principal_for_page(self, device_id: str, session: dict) -> str:
        return session["principal_id"]

    def _check_overrides(self, overrides) -> None:
        if not isinstance(overrides, list):
            raise AdaptationError("invalid_request", "local_overrides 必须为数组")
        for override in overrides:
            if not isinstance(override, dict) \
                    or not (override.get("kind") or override.get("item_kind")) \
                    or not override.get("item_id"):
                raise AdaptationError(
                    "invalid_request",
                    "local_overrides 项必须含 kind/item_id")
            kind = override.get("kind") or override.get("item_kind")
            if override.get("kind") and override.get("item_kind") \
                    and override["kind"] != override["item_kind"]:
                raise AdaptationError("invalid_request", "局部覆盖类型声明冲突")
            if "tapDepth" in override:
                from drivers.daemon_protocol import check_tap
                try:
                    if kind != "target":
                        raise ValueError("接触深度仅允许覆盖点击目标")
                    check_tap(override["tapDepth"])
                except (TypeError, ValueError) as exc:
                    raise AdaptationError("invalid_request", str(exc)) from exc
            if "tapDwellMs" in override:
                dwell = override["tapDwellMs"]
                if kind != "target" or type(dwell) is not int or not 0 <= dwell <= 2000:
                    raise AdaptationError(
                        "invalid_request", "接触停留仅允许点击目标，且须为 0-2000 毫秒整数")
            if kind == "target" \
                    and str(override.get("item_id", "")).startswith("ime_"):
                raise AdaptationError(
                    "invalid_request",
                    "live 模式 IME 目标不允许 local_overrides 修改")

    def _match_page(self, session: dict, state: str, capture: dict,
                    rectified: dict | None = None, overrides=()) -> dict:
        """服务端真实匹配：读模板定义 + 真实图像逐锚点匹配（不经客户端）。

        R3-12：模板区域是**屏幕域**定义；匹配只用会话固定原图经本台当前
        匹配 v3 cameraToScreen 摆正后的屏幕域图像，不把相机原图直接对
        屏幕域区域算 check（原相机字节不伪标屏幕图）。"""
        template = self._resolve_template(session["template_id"],
                                          session["template_version"])
        if template is None:
            raise AdaptationError("template_not_found",
                                  "会话固定模板不存在", status=404)
        pages = template.get("pages")
        page_def = None
        if isinstance(pages, list):
            for candidate in pages:
                if candidate.get("state") == state:
                    page_def = candidate
                    break
        elif isinstance(pages, dict):
            page_def = pages.get(state)
        if page_def is None:
            raise AdaptationError("template_invalid",
                                  f"模板缺少 {state} 页定义", status=503)
        page_def = copy.deepcopy(page_def)
        seen = set()
        for override in overrides:
            kind = override.get("kind") or override.get("item_kind")
            key = kind, override["item_id"]
            items = page_def.get(kind + "s", []) if kind in {"target", "region"} else []
            item = next((i for i in items if i["id"] == override["item_id"]), None)
            if (override.get("state", state) != state or key in seen or item is None
                    or not any(a["id"] == override.get("anchor_id") for a in page_def["anchors"])):
                raise AdaptationError("invalid_request", "局部覆盖页面、类型、目标或锚点无效或重复")
            seen.add(key)
            item.update(anchor_id=override["anchor_id"],
                        relative_rect=copy.deepcopy(override["relative_rect"]))
        evidence_ids = [e["evidence_id"] for e in capture["evidence"]]
        if rectified is None:
            rectified = self._rectify_evidence_to_screen(
                session["device_id"], session, capture)
        anchor_bytes = collect_anchor_bytes(
            page_def,
            lambda asset_id: self._template_asset_bytes(
                session["template_id"], session.get("template_version"),
                asset_id))
        error_bound = rectified.get("mapping_error_bound_px")
        checks = [compute_page_check(page_def, frame, anchor_bytes, evidence_ids,
                                     mapping_error_bound_px=error_bound)
                  for frame in rectified["frames"]]
        size = rectified["summary"]["rectified_size"]
        check = merge_page_checks(checks, page_def, (size["width"], size["height"]),
                                  mapping_error_bound_px=error_bound)
        # 摆正/坐标域/矩阵版本可追溯摘要（下划线键不进入持久 check；
        # write_page 另存为 page_entry["rectify"]）
        check["_rectify"] = rectified["summary"]
        # 用途与必需性来自会话固定模板，不从 item ID 或结果顺序猜测。
        check["_color_region_purposes"] = {
            region["id"]: region.get("purpose")
            for region in page_def.get("regions", [])}
        required_anchors = sorted(
            (anchor for anchor in page_def.get("anchors", [])
             if anchor.get("required") is True),
            key=lambda anchor: anchor["id"])
        check["_required_anchor_ids"] = [anchor["id"] for anchor in required_anchors]
        # 采样回退用：模板定义的前两个必需锚点 reference_rect（非契约字段，
        # 不进入 wire 校验路径；_sample_colors 内部消费后丢弃）
        check["_definition_anchor_rects"] = [
            anchor["reference_rect"] for anchor in required_anchors
            if anchor.get("reference_rect")][:2]
        return check

    def _rectify_evidence_to_screen(self, device_id: str, session: dict,
                                    capture: dict) -> dict:
        """Shared draft/adaptation rectification; missing providers fail closed."""
        try:
            return rectify_capture(
                device_id=device_id,
                snapshot=session.get("installation_binding"),
                current_binding=(self._binding_provider(device_id)
                                 if self._binding_provider else None),
                bundle=(self._calibration_provider(device_id)
                        if self._calibration_provider else None),
                region_record=(self._region_record_provider(device_id)
                               if self._region_record_provider else None),
                capture=capture, frame_reader=self._read_frame_bytes)
        except LayoutGeometryError as exc:
            raise AdaptationError(exc.code, exc.message, exc.status) from exc

    @staticmethod
    def _crop_screen_rect(frame, rect):
        height, width = frame.shape[:2]
        try:
            left, top, right, bottom = _pixel_rect(rect, width, height)
        except ValueError as exc:
            raise AdaptationError(
                "items_unverified", "采样区域非法、越界或过小：拒绝裁剪",
                status=409) from exc
        return frame[top:bottom, left:right]

    def _archive_empty_input_reference(self, device_id: str, session_id: str,
                                       state: str, check: dict,
                                       frame_bytes: bytes) -> dict | None:
        """裁本帧空白输入区域存为会话资产（PNG 原字节 + sha256）。"""
        import cv2
        import numpy as np

        input_rect = next((item.get("resolved_rect")
                           for item in check.get("item_results", [])
                           if item.get("id") == "input"
                           and item.get("resolved_rect") is not None), None)
        if input_rect is None:
            return None
        frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8),
                             cv2.IMREAD_COLOR)
        if frame is None:
            return None
        crop = self._crop_screen_rect(frame, input_rect)
        ok, buf = cv2.imencode(".png", crop)
        if not ok:
            return None
        data = buf.tobytes()
        digest = hashlib.sha256(data).hexdigest()
        directory = (self._state.data_root / "devices" / device_id /
                     "layout_adaptation" / "sessions" / session_id / "assets")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"empty-input-{state}.png"
        if not path.exists() or hashlib.sha256(
                path.read_bytes()).hexdigest() != digest:
            path.write_bytes(data)
        return {"id": f"empty-input-{state}", "sha256": digest}

    def _sample_colors(self, state: str, check: dict, frame_bytes: bytes,
                       evidence_ids: list, *, page_failed: bool = False) -> dict:
        """§6：明确用途优先；缺失时对已通过必需锚点并集做 Otsu 暗亮分组。"""
        import cv2
        import numpy as np

        frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8),
                             cv2.IMREAD_COLOR)
        if frame is None:
            raise AdaptationError("source_unavailable",
                                  "受控证据原图无法解码", status=503)

        def _median_rgb(pixels) -> list:
            median = np.median(pixels, axis=0)
            return [int(round(float(value))) for value in median[::-1]]

        purposes = check.get("_color_region_purposes", {})
        samples = {"foreground": [], "background": []}
        for item in check.get("item_results", []):
            purpose = purposes.get(item["id"])
            if (item.get("kind") == "region" and item.get("status") == "passed"
                    and purpose in samples and item.get("resolved_rect") is not None):
                samples[purpose].append(item["resolved_rect"])
        if any(len(rects) > 1 for rects in samples.values()):
            raise AdaptationError(
                "items_unverified", "颜色采样用途重复，不能确定唯一取样区域", status=409)
        if all(samples.values()):
            # 已明确指定的纯色块也是有效颜色证据，不套用锚点纹理门槛。
            foreground, background = [
                _median_rgb(self._crop_screen_rect(frame, samples[purpose][0]).reshape(-1, 3))
                for purpose in ("foreground", "background")]
        else:
            required_ids = set(check.get("_required_anchor_ids", []))
            anchors = sorted(
                (result for result in check.get("anchor_results", [])
                 if result.get("status") == "passed"
                 and result.get("id") in required_ids
                 and result.get("matched_rect") is not None),
                key=lambda result: result["id"])
            rects = [result["matched_rect"] for result in anchors[:2]]
            diagnostic_only = len(rects) < 2 and page_failed
            if diagnostic_only:
                # 保留已失败页的诊断测量，不赋予页面通过或颜色核验资格。
                rects = [rect for rect in check.get("_definition_anchor_rects", [])
                         if isinstance(rect, dict)][:2]
            if len(rects) < 2:
                raise AdaptationError(
                    "items_unverified",
                    "本页颜色采样区域不足（low_texture）：不能保存颜色样本",
                    status=409)
            if diagnostic_only:
                foreground, background = [
                    _median_rgb(self._crop_screen_rect(frame, rect).reshape(-1, 3))
                    for rect in rects]
            else:
                height, width = frame.shape[:2]
                try:
                    bounds = [_pixel_rect(rect, width, height) for rect in rects]
                except ValueError as exc:
                    raise AdaptationError(
                        "items_unverified", "采样区域非法、越界或过小：拒绝裁剪",
                        status=409) from exc
                mask = np.zeros((height, width), dtype=bool)
                for left, top, right, bottom in bounds:
                    mask[top:bottom, left:right] = True
                pixels = frame[mask]
                rgb = pixels[:, ::-1].reshape(-1, 1, 3)
                gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
                _, labels = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
                dark = labels.ravel() == 0
                light = ~dark
                gray = gray.ravel()
                if (int(dark.sum()) < 16 or int(light.sum()) < 16
                        or float(np.median(gray[light]) - np.median(gray[dark])) < 8):
                    raise AdaptationError(
                        "items_unverified",
                        "锚点颜色分组不足或对比度过低（low_texture）：拒绝颜色样本",
                        status=409)
                foreground = _median_rgb(pixels[dark])
                background = _median_rgb(pixels[light])
        return {"state": state,
                "foreground_rgb": foreground,
                "background_rgb": background,
                "empty_input_asset_ref": None,
                "evidence_ids": list(evidence_ids)}

    # ================= 幂等台账（by-request，R2-19） =================
    def _register_operation(self, *, resource_kind: str, resource_id: str,
                            device_id: str, principal_id: str,
                            client_request_id: str, request: dict,
                            planned_response: dict) -> str:
        operation = self._state.operation_prepare(
            principal_id=principal_id, device_id=device_id,
            resource_kind=resource_kind, resource_id=resource_id,
            client_request_id=client_request_id, request=request,
            planned_response=planned_response, files=[],
            pointer_before=None, pointer_after=None, mutations=[],
            version_ref=None)
        self._state.operation_commit(
            operation["operation_id"], response=planned_response)
        return operation["operation_id"]

    def _replay_operation(self, client_request_id: str) -> dict | None:
        return self._state.operation_get_by_request(client_request_id)

    def _replay_payload(self, operation: dict, device_id: str,
                        request: dict | None = None) -> dict | None:
        """同编号重放：同身份同内容返回原响应；异内容 409；跨设备 403。"""
        if operation.get("device_id") != device_id:
            raise AdaptationError("permission_denied",
                                  "client_request_id 已被其他设备/身份使用",
                                  status=403)
        if request is not None:
            intent = self._state.intent_get(operation["operation_id"])
            if intent is not None and intent["request_hash"] != \
                    content_sha(request):
                raise AdaptationError(
                    "request_conflict",
                    "同 client_request_id 携带了不同内容", status=409)
        response = operation.get("response")
        if response is None:
            raise AdaptationError(
                "request_conflict",
                "同编号操作未完成提交，不能重放；请按 operation 查询对账",
                status=409)
        return {**response, "replayed": True}

    def operation_by_request(self, device_id: str,
                             client_request_id: str) -> dict | None:
        self._require_open()
        operation = self._state.operation_get_by_request(client_request_id)
        if operation is None:
            return None
        if operation.get("device_id") != device_id:
            raise AdaptationError("permission_denied",
                                  "操作不属于该设备", status=403)
        return {"operation_id": operation["operation_id"],
                "phase": operation["phase"],
                "original_response": operation.get("response"),
                "error": operation.get("error")}

    # ================= 预览（不可变逐项结果，一次消费） =================
    def preview(self, device_id: str, request: dict) -> dict:
        self._require_open()
        if isinstance(request, str):  # 旧内部调用：preview(device, session_id)
            request = {"capture_session_id": request}
        session_id = request.get("capture_session_id") \
            or request.get("session_id")
        if not session_id:
            raise AdaptationError("invalid_request", "缺少 capture_session_id")
        expected_revision = request.get("expected_session_revision")
        with self._device_lock(device_id):
            session = self._get_session_checked(device_id, str(session_id))
            if expected_revision is not None \
                    and int(expected_revision) != int(session["revision"]):
                raise AdaptationError(
                    "revision_conflict",
                    f"会话修订冲突（当前 {session['revision']}，期望 "
                    f"{expected_revision}）", status=409)
            now = self._now()
            page_checks, accepted, failed, blocking = \
                self._compute_page_checks(session, now)
            # 取一张当前新鲜原图验证当前手机/布局（来源核验；不要求四态同屏）
            current_evidence_id = self._verify_current_source(device_id,
                                                              session)
            region_ref = self._checked_page_region_ref(session)
            preview_id = str(uuid.uuid4())  # 冻结契约：preview_id 为 UUID
            binding = session.get("installation_binding") or {}
            record = {
                "preview_id": preview_id,
                "device_id": device_id,
                "principal_id": session["principal_id"],
                "request_hash": content_sha(request),
                "capture_session_id": session["capture_session_id"],
                "session_revision": session["revision"],
                "template_id": session["template_id"],
                "template_version": session["template_version"],
                "binding_snapshot": binding,
                "configuration_sha256": self._configuration_sha(session, region_ref),
                "current_evidence_id": current_evidence_id,
                "page_checks": page_checks,
                "accepted_items": accepted,
                "failed_items": failed,
                "blocking_reasons": blocking,
                "status": "preview",
                "created_at": iso(now),
                "expires_at": iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
                "server_instance_id": self._server_id(),
                "consumed_by": None,
            }
            if self._state is not None:
                key = state_key("adaptation_preview",
                                state_identity("adaptation_preview", record))
                self._state.state_insert(
                    "adaptation_preview", key, device_id,
                    session["principal_id"], record)
            self._previews_cache(preview_id, record)
            return self._preview_view(record)

    def _previews_cache(self, preview_id: str, record: dict) -> None:
        self._previews[preview_id] = record

    def _load_preview(self, device_id: str, preview_id: str) -> dict | None:
        if hasattr(self, "_previews"):
            record = self._previews.get(preview_id)
            if record is not None:
                return record
        if self._state is None:
            return None
        stored = self._state.state_get(
            "adaptation_preview", state_key("adaptation_preview", [preview_id]))
        if stored is None:
            return None
        if stored.get("server_instance_id") != self._server_id():
            return None  # 重启后未发布 preview 失效
        self._previews_cache(preview_id, stored)
        return stored

    def _verify_current_source(self, device_id: str, session: dict) -> str | None:
        """预览当前帧：仅来源核验（新鲜 + 绑定一致），不做四态匹配。"""
        if self._capture_provider is None:
            return None
        try:
            frames = self._capture_provider(device_id, 1, None)
        except Exception as exc:
            raise AdaptationError("source_unavailable",
                                  f"预览取当前帧失败：{exc}", status=503)
        if not frames:
            raise AdaptationError("source_unavailable",
                                  "预览未取得当前帧", status=503)
        frame = frames[-1]
        snapshot = session.get("installation_binding") or {}
        current = (frame.get("installation_binding") or {})
        for key in ("camera_binding_id", "arm_binding_id"):
            if snapshot.get(key) and snapshot.get(key) != current.get(key):
                raise AdaptationError("source_binding_changed",
                                      f"当前来源 {key} 与会话快照不一致",
                                      status=409)
        age = frame.get("received_monotonic_ms", 0) \
            - frame.get("source_captured_monotonic_ns", 0) / 1e6
        if age > 1000:
            raise AdaptationError("evidence_expired",
                                  "当前帧不新鲜，不能生成预览", status=409)
        return frame.get("evidence_id")

    @staticmethod
    def _configuration_sha(session: dict, region_ref: dict) -> str:
        return content_sha({
            "template_ref": session.get("template_ref"),
            "binding": session.get("installation_binding") or {},
            "keyboard_geometry_mode": session.get("keyboard_geometry_mode"),
            "installation_region_ref": region_ref,
        })

    def _session_states(self, session: dict) -> tuple:
        template = self._resolve_template(session["template_id"], session["template_version"])
        if template is None:
            raise AdaptationError("template_not_found", "会话固定模板不存在", status=404)
        try:
            return page_states(template)
        except ValueError as exc:
            raise AdaptationError("invalid_request", str(exc)) from exc

    def _checked_page_region_ref(self, session: dict) -> dict:
        """Never relabel saved checks with a newer selected region."""
        provider = self._region_ref_provider
        current = provider(session["device_id"]) if callable(provider) else None
        if not isinstance(current, dict) \
                or current.get("id") != session["device_id"] \
                or type(current.get("version")) is not int or current["version"] < 1 \
                or not current.get("sha256"):
            raise AdaptationError(
                "items_unverified", "缺少有效的当前生效粗框，须重新核验", status=409)
        current = dict(current)
        for state in self._session_states(session):
            page = session["pages"].get(state)
            if page is None:
                continue  # The completeness gate reports uncaptured pages.
            matched_ref = (page.get("rectify") or {}).get("region_ref")
            if matched_ref != current:
                raise AdaptationError(
                    "items_unverified",
                    f"页面 {state} 的匹配粗框缺失或已变化，须按当前粗框重新采集",
                    status=409)
        return current

    def _compute_page_checks(self, session: dict,
                             now: datetime) -> tuple[dict, list, list, list]:
        """逐页结果只来自服务端已保存的 check（客户端不可提交）。"""
        self._checked_page_region_ref(session)
        page_checks: dict[str, dict] = {}
        accepted: list[dict] = []
        failed: list[dict] = []
        blocking: list[dict] = []
        for state in self._session_states(session):
            page = session["pages"].get(state)
            if page is None:
                raise AdaptationError(
                    "pages_incomplete",
                    f"四态 {list(PAGE_STATES)} 缺少 {state}，"
                    "不能发布空适配或缺页适配", status=409)
            accepted_at = page["capture"]["accepted_at"]
            age = (now - parse_iso(accepted_at)).total_seconds()
            if age > PAGE_EVIDENCE_TTL_SECONDS:
                raise AdaptationError(
                    "evidence_expired",
                    f"页面 {state} 证据已超过 {PAGE_EVIDENCE_TTL_SECONDS} 秒"
                    f"（自首次接纳 {accepted_at}），须重新采集",
                    status=409)
            check = page["capture"]["check"]
            page_checks[state] = {
                "state": state,
                "status": check["status"],
                "evidence_ids": list(page["capture"]["evidence_ids"]),
                "accepted_at": accepted_at,
                "anchor_results": check.get("anchor_results", []),
                "item_results": check.get("item_results", []),
                "reason_codes": check.get("reason_codes", []),
                # 兼容旧视图字段
                "items": [{"page_state": state,
                           "item_kind": r.get("kind", "anchor"),
                           "item_id": r["id"], "status": r["status"],
                           "reason_code": r.get("reason_code")}
                          for r in check.get("anchor_results", [])
                          + check.get("item_results", [])]}
            for result in check.get("anchor_results", []):
                keyed = {"page_state": state, "item_kind": "anchor",
                         "item_id": result["id"]}
                if result["status"] == "passed":
                    accepted.append(keyed)
                else:
                    failed.append({**keyed, "status": result["status"],
                                   "reason_code": result.get("reason_code")})
                    blocking.append(keyed)
            for result in check.get("item_results", []):
                keyed = {"page_state": state,
                         "item_kind": result.get("kind", "region"),
                         "item_id": result["id"]}
                if result["status"] == "passed":
                    accepted.append(keyed)
                else:
                    failed.append({**keyed, "status": result["status"],
                                   "reason_code": result.get("reason_code")})
                    blocking.append(keyed)
        state_order = {state: index for index, state
                       in enumerate(self._session_states(session))}
        accepted.sort(key=lambda k: (state_order[k["page_state"]],
                                     k["item_kind"], k["item_id"]))
        failed.sort(key=lambda k: (state_order[k["page_state"]],
                                   k["item_kind"], k["item_id"]))
        return page_checks, accepted, failed, blocking

    @staticmethod
    def _preview_view(record: dict) -> dict:
        return {k: v for k, v in record.items()
                if k not in ("consumed_by", "server_instance_id")}

    # ================= CAS 发布（R2-19 §9 持久协议） =================
    def publish(self, device_id: str, request: dict) -> dict:
        self._require_open()
        if self._state is None:
            raise AdaptationError("storage_failed", "未接入持久状态库",
                                  status=503)
        client_request_id = _require_uuid(
            request.get("client_request_id"), "client_request_id")
        preview_id = request.get("preview_id")
        session_id = request.get("session_id") \
            or request.get("capture_session_id")
        if not preview_id:
            raise AdaptationError(
                "invalid_request", "发布必须携带 preview_id（只接受预览结果）")
        # ① 幂等查重：同编号同内容返回原响应；异内容冲突
        prior = self._state.operation_get_by_request(client_request_id)
        if prior is not None:
            response = prior.get("response")
            if response is None:
                raise AdaptationError(
                    "request_conflict",
                    "同编号操作未完成提交；请按 operation 查询对账", status=409)
            request_hash = content_sha(self._publish_request_identity(request))
            # 同内容 = 同 preview/会话/期望修订（不含 correlation）
            intent = self._state.intent_get(prior["operation_id"])
            if intent is not None \
                    and intent["request_hash"] != request_hash:
                raise AdaptationError(
                    "request_conflict",
                    "同 client_request_id 携带了不同内容", status=409)
            return {**response, "replayed": True}
        if self._state.blocked_reasons:
            raise AdaptationError(
                "archive_invalid",
                f"存储完整性未恢复：{self._state.blocked_reasons}；"
                "拒绝新写", status=503)
        principal = self._principal(request)
        with self._device_lock(device_id):
            if session_id:
                session = self._get_session_checked(device_id, str(session_id))
            else:
                # 发布按 preview_id 反查唯一会话（冻结契约：不另补关联）
                preview = self._load_preview_checked(device_id, preview_id)
                session_id = preview["capture_session_id"]
                session = self._get_session_checked(device_id, session_id)
            if session["principal_id"] != principal:
                raise AdaptationError(
                    "author_mismatch",
                    f"会话固定作者 {session['principal_id']}，"
                    f"发布者 {principal} 不符", status=403)
            for key in ("template_id", "template_version"):
                if request.get(key) is not None \
                        and request.get(key) != session[key]:
                    raise AdaptationError(
                        "template_mismatch",
                        f"请求 {key} 与会话固定值不符", status=409)
            template = self._resolve_template(session["template_id"],
                                              session["template_version"])
            if template is None:
                raise AdaptationError("template_not_found", "会话模板不存在",
                                      status=404)
            self._require_v3_calibration(device_id, session, "session")
            self._verify_current_binding(device_id, session)
            preview = self._load_preview_checked(device_id, preview_id)
            if preview["capture_session_id"] != session["capture_session_id"]:
                raise AdaptationError("preview_not_found",
                                      "预览不属于该会话", status=404)
            if preview.get("consumed_by") is not None:
                raise AdaptationError(
                    "preview_consumed",
                    "同一 preview 只能消费一次；请重新生成预览", status=409)
            if self._age_seconds(preview.get("expires_at")) >= 0:
                raise AdaptationError("preview_expired",
                                      "预览已超过 120 秒有效期", status=409)
            # ② 预览后又写页不能消费旧结果：会话修订必须仍等于预览时修订
            if int(preview["session_revision"]) != int(session["revision"]):
                raise AdaptationError(
                    "revision_conflict",
                    f"预览后会话已变化（预览时 {preview['session_revision']}，"
                    f"当前 {session['revision']}）；请重新预览", status=409)
            # ③ 发布时重验四页齐与证据期限（结果只来自服务端 check）
            now = self._now()
            _page_checks, accepted_items, failed_items, blocking = \
                self._compute_page_checks(session, now)
            supplied = request.get("accepted_items")
            if supplied is not None:
                state_order = {state: index for index, state
                               in enumerate(self._session_states(session))}
                normalized = sorted(
                    [{"page_state": i.get("page_state"),
                      "item_kind": i.get("item_kind"),
                      "item_id": i.get("item_id")} for i in supplied],
                    key=lambda k: (state_order.get(k["page_state"] or "", 99),
                                   k["item_kind"] or "",
                                   k["item_id"] or ""))
                if normalized != accepted_items:
                    raise AdaptationError(
                        "accepted_items_mismatch",
                        "accepted_items 必须恰为预览中全部实际通过项",
                        status=409)
            # ④ 持久 CAS：expected_revision 对齐持久指针 revision
            pointer_before = self._state.adaptation_pointer_get(device_id) \
                or {"revision": 0, "version": None, "sha256": None}
            expected = request.get("expected_revision",
                                   request.get("expected_pointer_revision"))
            if expected is not None \
                    and int(expected) != int(pointer_before.get("revision") or 0):
                raise AdaptationError(
                    "revision_conflict",
                    f"指针修订冲突（当前 {pointer_before.get('revision')}，"
                    f"期望 {expected}）", status=409)
            # ⑤ 版本由持久预约分配（指针/磁盘/ prepared 预约最大值 +1）
            version = self._state.next_adaptation_version(device_id)
            # 冻结 layout:adaptation 记录（封闭字段集；sha 覆盖全部字段）
            facts = {}
            if callable(self._binding_provider):
                facts = self._binding_provider(device_id) or {}
            ime_device_id = facts.get("ime_device_id")
            screen_descriptor = facts.get("screen_descriptor")
            if not ime_device_id or not screen_descriptor:
                raise AdaptationError(
                    "source_unavailable",
                    "当前绑定/屏幕描述事实缺失：不能生成适配记录", status=503)
            region_ref = self._checked_page_region_ref(session)
            if preview.get("configuration_sha256") != self._configuration_sha(
                    session, region_ref):
                raise AdaptationError(
                    "items_unverified", "预览配置与匹配粗框不一致，须重新核验",
                    status=409)
            page_checks = [session["pages"][state]["capture"]["check"]
                           for state in self._session_states(session)]
            local_overrides = []
            color_samples = []
            for state in self._session_states(session):
                local_overrides.extend(
                    session["pages"][state].get("local_overrides") or [])
                if session["pages"][state].get("color_sample"):
                    color_samples.append(session["pages"][state]["color_sample"])
            record = {
                "contract_version": "device-layout-adaptation.v1",
                "device_id": device_id,
                "version": version,
                "template_ref": session.get("template_ref"),
                "ime_device_id": ime_device_id,
                "keyboard_geometry_mode": session["keyboard_geometry_mode"],
                "installation_region_ref": region_ref,
                "screen_descriptor": screen_descriptor,
                "compatibility_observed": template.get("compatibility"),
                "page_checks": page_checks,
                "local_overrides": local_overrides,
                "color_samples": color_samples,
                "reviewed_by": principal,
                "reviewed_at": iso(now),
            }

            record["sha256"] = content_sha(
                {k: v for k, v in record.items() if k != "sha256"})
            pointer_revision = int(pointer_before.get("revision") or 0) + 1
            pointer_after = {"revision": pointer_revision,
                             "version": version,
                             "sha256": record["sha256"]}
            per_item_status = [
                {"item": key, "status": "passed", "reason_code": None}
                for key in accepted_items] + [
                {"item": {k: item[k] for k in
                          ("page_state", "item_kind", "item_id")},
                 "status": item["status"],
                 "reason_code": item.get("reason_code")}
                for item in failed_items]
            planned_response = {
                "revision": pointer_revision,
                "adaptation_ref": {"id": device_id, "version": version,
                                   "sha256": record["sha256"]},
                "per_item_status": per_item_status,
                "replayed": False,
            }
            request_identity = self._publish_request_identity(request)
            operation_id = str(uuid.uuid4())
            planned_response["operation_id"] = operation_id
            catalog_row = {"resource": {
                "kind": "adaptation",
                "ref": {"id": device_id, "version": version,
                        "sha256": record["sha256"]}},
                "operation_id": operation_id,
                "created_at": record["reviewed_at"]}
            # §7：颜色资源复制到本台适配版本目录（发布时复制会话资产）
            for sample in color_samples:
                reference = sample.get("empty_input_asset_ref")
                if not reference:
                    continue
                source = (self._state.data_root / "devices" / device_id /
                          "layout_adaptation" / "sessions" /
                          session["capture_session_id"] / "assets" /
                          f"{reference['id']}.png")
                if source.exists():
                    target_dir = (self._state.data_root / "devices" /
                                  device_id / "layout_adaptation" /
                                  "versions" / str(version) / "assets")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / f"{reference['id']}.png"
                    if not target.exists():
                        target.write_bytes(source.read_bytes())
            file_plan = self._state.plan_adaptation_version(
                device_id, version, record)
            # §9 固定顺序：intent(prepared) → 文件 → 指针 → 最终 Store 提交
            operation = self._state.operation_prepare(
                principal_id=principal, device_id=device_id,
                resource_kind="adaptation", resource_id=device_id,
                client_request_id=client_request_id,
                request=request_identity,
                planned_response=planned_response,
                files=[file_plan],
                pointer_before=pointer_before,
                pointer_after=pointer_after,
                mutations=[{"table": "catalog", "expected_revision": 0,
                            "value": catalog_row}],
                consume=[{"kind": "adaptation_preview",
                          "state_key": state_key(
                              "adaptation_preview", [preview_id]),
                          "client_request_id": client_request_id}],
                version_ref={"id": device_id, "version": version,
                             "sha256": record["sha256"]},
                operation_id=operation_id)
            self._state.write_adaptation_version(device_id, version, record)
            self._state.adaptation_pointer_cas(
                device_id, int(pointer_before.get("revision") or 0),
                version, record["sha256"], operation_id)
            try:
                self._state.operation_commit(
                    operation_id, response=planned_response,
                    after_revision=pointer_revision,
                    mutations=[{"table": "catalog", "expected_revision": 0,
                                "value": catalog_row}],
                    consume=[("adaptation_preview",
                              state_key("adaptation_preview", [preview_id]),
                              client_request_id)])
            except LayoutStateError as exc:
                if exc.code == "layout_revision_conflict":
                    raise AdaptationError("revision_conflict", exc.message,
                                          status=exc.status)
                raise
            # 消费内存中的预览缓存（持久消费已在事务内）
            if self._previews.get(preview_id) is not None:
                self._previews[preview_id]["consumed_by"] = client_request_id
            self._persist_pointer_mirror(device_id, pointer_after, principal)
            # 返回体 = 冻结发布响应 + 记录派生投影（status/失败项等不入档：
            # 持久记录恒为封闭 layout:adaptation 形状）
            return {**planned_response,
                    "record": record,
                    "status": "verified" if not failed_items else "needs_review",
                    "accepted_items": accepted_items,
                    "failed_items": failed_items,
                    "blocking_reasons": blocking}

    @staticmethod
    def _publish_request_identity(request: dict) -> dict:
        """幂等 request_hash 的内容：去除关联字段后的完整请求。"""
        return {k: v for k, v in request.items()
                if k not in ("correlation_id",)}

    def _load_preview_checked(self, device_id: str, preview_id: str) -> dict:
        preview = self._load_preview(device_id, str(preview_id))
        if preview is None or preview["device_id"] != device_id:
            raise AdaptationError("preview_not_found",
                                  "预览不存在或不属于该设备", status=404)
        return preview

    def _verify_current_binding(self, device_id: str, session: dict) -> None:
        """发布重验当前安装（提供器接入时）。"""
        if self._binding_provider is None:
            return
        current = self._binding_provider(device_id)
        snapshot = session.get("installation_binding") or {}
        if not current:
            return
        for key in ("camera_binding_id", "arm_binding_id",
                    "camera_device_unique_id"):
            if snapshot.get(key) and snapshot.get(key) != current.get(key):
                raise AdaptationError("binding_changed",
                                      f"当前安装 {key} 已变化，须重新四态复核",
                                      status=409)

    def _persist_pointer_mirror(self, device_id: str, pointer: dict,
                                principal: str) -> None:
        """旧 KV 镜像（只读对账/迁移用）：新链路事实在 StateStore。"""
        if self._store is None:
            return
        try:
            self._store.save(LEGACY_POINTER_KEY.format(device_id=device_id),
                             {"revision": pointer["revision"],
                              "version": pointer["version"],
                              "sha256": pointer["sha256"],
                              "updated_by": principal})
        except Exception:
            pass

    # ================= 版本/当前读取（catalog 门控 + legacy 只读回退） =================
    def get_version(self, device_id: str, version: int) -> dict | None:
        self._require_open()
        if self._state is not None:
            # 只有 Store 最终提交（catalog 行存在）的版本可见；
            # aborted 孤档保留文件并占用版本号，但不可经读取路径使用
            catalog = self._state.state_get(
                "catalog", state_key("catalog",
                                     ["adaptation", device_id, version]))
            if catalog is not None:
                record = self._state.read_adaptation_version(device_id, version)
                if record is not None:
                    return record
        if self._store is not None:
            return self._store.load(
                LEGACY_VERSION_KEY.format(device_id=device_id, version=version))
        return None

    def get_current(self, device_id: str) -> dict | None:
        self._require_open()
        pointer = None
        if self._state is not None:
            pointer = self._state.adaptation_pointer_get(device_id)
        if pointer is None and self._store is not None:
            pointer = self._store.load(
                LEGACY_POINTER_KEY.format(device_id=device_id))
        version = (pointer or {}).get("version")
        return self.get_version(device_id, version) if version else None

    def list_versions(self, device_id: str) -> list[int]:
        versions: set[int] = set()
        if self._state is not None:
            versions |= set(self._state.list_adaptation_versions(device_id))
        if self._store is not None and hasattr(self._store, "scan_prefix"):
            for key, _rec in self._store.scan_prefix(
                    f"adaptation_version:{device_id}:"):
                try:
                    versions.add(int(key.rsplit(":", 1)[1]))
                except (ValueError, IndexError):
                    continue
        return sorted(versions)
