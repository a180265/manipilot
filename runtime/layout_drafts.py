"""共享模板草稿链（ui-layout-edit.v1，契约 §5；R2-17 HTTP 层冻结形状）。

流程（§5 固定顺序）：draft_create（固定设备/身份/绑定/兼容）→ 受控采集
（owner_kind=draft，经适配服务 capture）→ 资产上传（原字节/摘要/真实图像/
隐私确认）→ 单页完整替换 PUT（服务端真实匹配 page_check）→ 四页齐后发布
（CAS expected_draft_revision/expected_parent_version → 不可变模板）。

- 草稿持久在 layout_typed_state（kind=draft，业务 CAS revision）；
- 资产原字节存 LayoutTemplateStore（draft_id 命名空间），sha256=原字节；
- 发布只复制锚点实际引用的资产到模板所有者；模板记录为冻结 layout:template
  形状（pages 为四态数组）；
- 兼容字段：platform/orientation 固定 android/portrait，观察值草稿期可 null
  （sources 逐项 null）；metadata PUT 修订 revision+1，值变化清复核证据/结果；
- 真实匹配经 runtime/layout_matching（不信任客户端 passed/check）。
"""
from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime, timedelta, timezone

from runtime.layout_matching import collect_anchor_bytes, compute_page_check
from runtime.layout_geometry import merge_page_checks
from runtime.layout_state import (
    LayoutStateError,
    LayoutStateStore,
    content_sha,
    state_identity,
    state_key,
)
from runtime.layout_template import LayoutTemplateStore
from runtime.util import iso, parse_iso, utc_now

DRAFT_CONTRACT = "ui-layout-edit.v1"
PAGE_STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty", "chat_ime_text")

# §5 最低项（missing_items 闭集）
MINIMUM_ITEMS = {
    "wechat_list": [("target", "wechat_chats_tab"), ("region", "unread"),
                    ("region", "content")],
    "chat_ime_hidden": [("target", "back_button"), ("target", "input_box"),
                        ("region", "title"), ("region", "input"),
                        ("region", "content")],
    "chat_ime_empty": [("anchor", "keyboard_boundary")],
    "chat_ime_text": [("anchor", "keyboard_boundary"),
                      ("target", "send_button"),
                      ("anchor", "send_button_feature")],
}

_COMPAT_KEYS = ("platform", "orientation", "wechat_version", "layout_family",
                "font_setting", "display_scale", "theme",
                "system_navigation_mode", "ime_package", "ime_layout_version")


class LayoutDraftError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def _now(clock) -> datetime:
    value = (clock or utc_now)()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _missing_items(state: str) -> list[dict]:
    return [{"page_state": state, "item_kind": kind, "item_id": item}
            for kind, item in MINIMUM_ITEMS[state]]


class LayoutDraftService:
    """冻结草稿链（draft→assets→pages→publish）。"""

    def __init__(self, template_store: LayoutTemplateStore,
                 state: LayoutStateStore, *, clock=None, frame_reader=None,
                 binding_snapshot_provider=None, rectifier=None):
        self._templates = template_store
        self._state = state
        self._clock = clock
        self._frame_reader = frame_reader
        self._binding_snapshot_provider = binding_snapshot_provider
        self._rectifier = rectifier

    # ---- 幂等台账（复用 state store operation 协议） ----
    def _register(self, *, resource_kind: str, resource_id: str, device_id: str,
                  principal: str, client_request_id: str, request: dict,
                  response: dict) -> str:
        operation = self._state.operation_prepare(
            principal_id=principal, device_id=device_id,
            resource_kind=resource_kind, resource_id=resource_id,
            client_request_id=client_request_id,
            request={k: v for k, v in request.items()
                     if k not in ("correlation_id",)},
            planned_response=response, files=[], pointer_before=None,
            pointer_after=None, mutations=[], version_ref=None)
        self._state.operation_commit(operation["operation_id"],
                                     response=response)
        return operation["operation_id"]

    def _replay(self, client_request_id: str, device_id: str,
                request: dict) -> dict | None:
        operation = self._state.operation_get_by_request(client_request_id)
        if operation is None:
            return None
        if operation.get("device_id") != device_id:
            raise LayoutDraftError("layout_permission_denied",
                                   "client_request_id 已被其他设备使用", 403)
        intent = self._state.intent_get(operation["operation_id"])
        if intent is not None and intent["request_hash"] != content_sha(
                {k: v for k, v in request.items()
                 if k not in ("correlation_id",)}):
            raise LayoutDraftError("layout_request_conflict",
                                   "同 client_request_id 携带了不同内容", 409)
        response = operation.get("response")
        if response is None:
            raise LayoutDraftError(
                "layout_request_conflict",
                "同编号操作未完成提交；请按 operation 查询对账", 409)
        return {**response, "replayed": True}

    def _draft_device(self, draft_id: str) -> str:
        draft = self._state.state_get("draft", state_key("draft", [draft_id]))
        return (draft or {}).get("device_id") or draft_id

    # ---- 持久草稿 ----
    def _save(self, draft: dict) -> None:
        key = state_key("draft", state_identity("draft", draft))
        self._state.state_update("draft", key, draft["device_id"],
                                 draft["principal_id"], draft["revision"] - 1,
                                 draft)

    def _load(self, draft_id: str, principal: str) -> dict:
        draft = self._state.state_get("draft", state_key("draft", [draft_id]))
        if draft is None:
            raise LayoutDraftError("layout_resource_missing",
                                   f"草稿 {draft_id} 不存在", 404)
        if draft.get("principal_id") != principal:
            raise LayoutDraftError("layout_permission_denied",
                                   "草稿属于其他身份", 403)
        return draft

    # ================= 创建 =================
    def create(self, request: dict, principal: str) -> dict:
        device_id = request.get("device_id")
        if not device_id:
            raise LayoutDraftError("layout_request_invalid",
                                   "draft_create 必须固定 device_id")
        client_request_id = request["client_request_id"]
        replay = self._replay(client_request_id, device_id, request)
        if replay is not None:
            return replay
        snapshot = None
        provider = self._binding_snapshot_provider
        if callable(provider):
            snapshot = provider(device_id)
        if not snapshot:
            raise LayoutDraftError(
                "layout_source_unavailable",
                "本台当前绑定/来源身份缺失：不能创建草稿（先完成相机采集会话）",
                503)
        base_ref = request.get("base_template_ref")
        compatibility = {key: None for key in _COMPAT_KEYS}
        compatibility["platform"] = "android"
        compatibility["orientation"] = "portrait"
        # 值与来源的空缺必须一致（layout:unknown_source）；platform/方向是
        # 协议固定值（非未知）→ 来源标 reported，其余观察项草稿期未知
        compatibility["sources"] = {key: None for key in _COMPAT_KEYS}
        compatibility["sources"]["platform"] = "reported"
        compatibility["sources"]["orientation"] = "reported"
        # 参考屏幕描述 = 本台屏幕事实的正向投影（无机械/设备身份）
        screen = (snapshot or {}).get("screen_descriptor") or {}
        descriptor = None
        if screen.get("width_px") and screen.get("height_px"):
            descriptor = {
                "width_px": int(screen["width_px"]),
                "height_px": int(screen["height_px"]),
                "orientation": "portrait",
                "system_insets": dict(screen.get("system_insets")
                                      or {"left": 0, "top": 0,
                                          "right": 0, "bottom": 0})}
        pages = [{"state": state, "definition": None, "page_check": None,
                  "source_evidence_ids": [], "updated_at": None,
                  "missing_items": _missing_items(state)}
                 for state in PAGE_STATES]
        assets: list[dict] = []
        if base_ref is not None:
            base = self._templates.get_template(str(base_ref["id"]),
                                                int(base_ref["version"]))
            if base is None:
                raise LayoutDraftError("layout_resource_missing",
                                       "base_template_ref 指向的模板不存在",
                                       404)
            compatibility = dict(base.get("compatibility") or compatibility)
            descriptor = dict(base.get("reference_screen_descriptor") or {}) \
                or None
            base_pages = base.get("pages")
            base_list = base_pages if isinstance(base_pages, list) else []
            pages = [{"state": state, "definition": None, "page_check": None,
                      "source_evidence_ids": [], "updated_at": None,
                      "missing_items": _missing_items(state)}
                     for state in PAGE_STATES]
            assets = list(base.get("assets") or [])
        now = _now(self._clock)
        draft_id = str(uuid.uuid4())
        draft = {
            "draft_id": draft_id,
            "device_id": device_id,
            "principal_id": principal,
            "revision": 1,
            "base_template_ref": base_ref,
            "binding_snapshot": snapshot,
            "compatibility": compatibility,
            "reference_screen_descriptor": descriptor,
            "pages": pages,
            "assets": assets,
            "created_at": iso(now),
            "updated_at": iso(now),
            "published_refs": [],
        }
        # 复制基础模板资产原字节到草稿命名空间（独立草稿，旧模板不变）
        if base_ref is not None:
            base = self._templates.get_template(str(base_ref["id"]),
                                                int(base_ref["version"]))
            for asset in (base or {}).get("assets") or []:
                data = self._templates.get_asset(str(base_ref["id"]),
                                                 asset["id"])
                if data is not None:
                    self._templates.save_asset(draft_id, asset["id"], data)
        self._state.state_insert(
            "draft", state_key("draft", state_identity("draft", draft)),
            device_id, principal, draft)
        operation_id = self._register(
            resource_kind="draft", resource_id=draft_id, device_id=device_id,
            principal=principal, client_request_id=client_request_id,
            request=request,
            response={"draft_id": draft_id, "revision": 1})
        return {"operation_id": operation_id, "draft_id": draft_id,
                "revision": 1, "replayed": False}

    # ================= 读取 =================
    def get(self, draft_id: str, principal: str) -> dict:
        draft = self._load(draft_id, principal)
        # blocking_reasons 是冻结 reason 闭集（layout#/definitions/reason）
        blocking: list[str] = []
        for page in draft["pages"]:
            if page["definition"] is None:
                blocking.append("not_captured")
            elif page["page_check"] is None:
                blocking.append("not_captured")
            elif page["page_check"].get("status") != "passed":
                blocking.extend(page["page_check"].get("reason_codes")
                                or ["not_captured"])
        sources = draft["compatibility"].get("sources")
        if not sources or all(value is None for value in sources.values()):
            blocking.append("compatibility_unknown")
        return {"draft": draft, "blocking_reasons": sorted(set(blocking))}

    # ================= 兼容元数据 =================
    def update_metadata(self, draft_id: str, request: dict,
                        principal: str) -> dict:
        client_request_id = request["client_request_id"]
        replay = self._replay(client_request_id, self._draft_device(draft_id),
                              request)
        if replay is not None:
            return replay
        with self._state.transaction():
            draft = self._load(draft_id, principal)
            if int(draft["revision"]) != int(request["expected_revision"]):
                raise LayoutDraftError(
                    "layout_revision_conflict",
                    f"草稿修订冲突（当前 {draft['revision']}，期望 "
                    f"{request['expected_revision']}）", 409)
            incoming = request["compatibility"]
            changed = any(
                incoming.get(key) != draft["compatibility"].get(key)
                for key in _COMPAT_KEYS)
            draft["compatibility"] = incoming
            if changed:
                # 兼容值变化：清复核证据/结果与定义（页面回到未采集；
                # 原 evidence 作为审计保留在操作台账，不继承旧"通过"）。
                # 冻结 draft_page 语义：definition 非空必须伴随 updated_at，
                # 故未复核页不得保留半截定义。
                for page in draft["pages"]:
                    page["definition"] = None
                    page["page_check"] = None
                    page["source_evidence_ids"] = []
                    page["updated_at"] = None
                    page["missing_items"] = _missing_items(page["state"])
            draft["revision"] = int(draft["revision"]) + 1
            draft["updated_at"] = iso(_now(self._clock))
            self._save(draft)
        operation_id = self._register(
            resource_kind="draft", resource_id=draft_id,
            device_id=draft["device_id"], principal=principal,
            client_request_id=client_request_id, request=request,
            response={"draft_revision": draft["revision"]})
        return {"operation_id": operation_id, "draft": draft,
                "replayed": False}

    # ================= 资产上传 =================
    def upload_asset(self, draft_id: str, request: dict,
                     principal: str) -> dict:
        client_request_id = request["client_request_id"]
        replay = self._replay(client_request_id, self._draft_device(draft_id),
                              request)
        if replay is not None:
            return replay
        if request.get("privacy_confirmed") is not True:
            raise LayoutDraftError("layout_request_invalid",
                                   "上传必须确认 privacy_confirmed")
        asset = request.get("asset") or {}
        try:
            data = base64.b64decode(request.get("content_base64") or "",
                                    validate=True)
        except (binascii.Error, ValueError) as exc:
            raise LayoutDraftError("layout_request_invalid",
                                   f"content_base64 不是有效 base64：{exc}")
        if len(data) > 200 * 1024 * 1024:
            raise LayoutDraftError("layout_archive_invalid",
                                   "资源展开后超过 200MiB 上限", 503)
        from runtime.layout_ops import sha256_bytes
        digest = sha256_bytes(data)
        if asset.get("sha256") != digest:
            raise LayoutDraftError("layout_request_invalid",
                                   "asset.sha256 与原字节摘要不符")
        if asset.get("media_type") not in ("image/png", "image/jpeg"):
            raise LayoutDraftError("layout_request_invalid",
                                   "media_type 只允许 image/png|image/jpeg")
        width, height = self._image_size(data)
        if (width, height) != (asset.get("width"), asset.get("height")):
            raise LayoutDraftError(
                "layout_request_invalid",
                f"图像真实尺寸 {width}x{height} 与 asset 声明不符")
        if max(width, height) > 8192 or width * height > 20_000_000:
            raise LayoutDraftError("layout_request_invalid",
                                   "图像超出 8192 边/2000 万像素上限")
        # 资产原字节先落盘（幂等/冲突自检）；draft 行修订在 state 事务内推进
        # （模板 Store 与 StateStore 是同一库文件上的两条连接——同线程嵌套写
        # 两个连接会自锁，故不嵌套）
        self._templates.save_asset(draft_id, asset["id"], data)
        with self._state.transaction():
            draft = self._load(draft_id, principal)
            if int(draft["revision"]) != int(request["expected_revision"]):
                raise LayoutDraftError(
                    "layout_revision_conflict",
                    f"草稿修订冲突（当前 {draft['revision']}，期望 "
                    f"{request['expected_revision']}）", 409)
            reused = any(item["asset"]["id"] == asset["id"]
                         and item["asset"]["sha256"] == digest
                         for item in draft["assets"])
            if not reused:
                draft["assets"].append({
                    "asset": dict(asset), "byte_size": len(data),
                    "privacy_reviewed_by": principal,
                    "privacy_reviewed_at": iso(_now(self._clock)),
                    "source_evidence_ids": list(
                        request.get("source_evidence_ids") or [])})
            draft["revision"] = int(draft["revision"]) + 1
            draft["updated_at"] = iso(_now(self._clock))
            self._save(draft)
        record = next(item for item in draft["assets"]
                      if item["asset"]["id"] == asset["id"])
        operation_id = self._register(
            resource_kind="draft", resource_id=draft_id,
            device_id=draft["device_id"], principal=principal,
            client_request_id=client_request_id, request=request,
            response={"draft_revision": draft["revision"],
                      "asset_id": asset["id"]})
        return {"operation_id": operation_id, "revision": draft["revision"],
                "asset": record, "replayed": False}

    @staticmethod
    def _image_size(data: bytes) -> tuple[int, int]:
        import cv2
        import numpy as np
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8),
                             cv2.IMREAD_COLOR)
        if image is None:
            raise LayoutDraftError("layout_request_invalid",
                                   "资产不是可解码 PNG/JPEG 图像")
        height, width = image.shape[:2]
        return width, height

    # ================= 单页完整替换 =================
    def write_page(self, draft_id: str, state: str, request: dict,
                   principal: str) -> dict:
        if state not in PAGE_STATES:
            raise LayoutDraftError(
                "layout_request_invalid",
                f"page_state 必须属于 {list(PAGE_STATES)} 固定顺序四态")
        client_request_id = request["client_request_id"]
        replay = self._replay(client_request_id, self._draft_device(draft_id),
                              request)
        if replay is not None:
            return replay
        evidence_ids = request.get("source_evidence_ids") or []
        evidence_ids = sorted(evidence_ids)  # 与 page_check.evidence_ids 一致
        definition = request.get("page_definition")
        if definition is None or definition.get("state") != state:
            raise LayoutDraftError(
                "layout_request_invalid",
                "page_definition.state 必须等于路径 state（单页完整替换）")
        with self._state.transaction():
            draft = self._load(draft_id, principal)
            if int(draft["revision"]) != int(request["expected_revision"]):
                raise LayoutDraftError(
                    "layout_revision_conflict",
                    f"草稿修订冲突（当前 {draft['revision']}，期望 "
                    f"{request['expected_revision']}）", 409)
            check, accepted_at = self._verify_page_evidence(
                draft, state, evidence_ids, definition)
            for page in draft["pages"]:
                if page["state"] != state:
                    continue
                page["definition"] = definition
                page["page_check"] = check
                page["source_evidence_ids"] = list(evidence_ids)
                page["updated_at"] = accepted_at
                page["missing_items"] = [
                    key for key in _missing_items(state)
                    if key not in [
                        {"page_state": state,
                         "item_kind": r.get("kind", "anchor"),
                         "item_id": r["id"]}
                        for r in (check.get("anchor_results", [])
                                  + check.get("item_results", []))
                        if r.get("status") == "passed"]]
            draft["revision"] = int(draft["revision"]) + 1
            draft["updated_at"] = iso(_now(self._clock))
            self._save(draft)
        page_view = next(page for page in draft["pages"]
                         if page["state"] == state)
        operation_id = self._register(
            resource_kind="draft", resource_id=draft_id,
            device_id=draft["device_id"], principal=principal,
            client_request_id=client_request_id, request=request,
            response={"draft_revision": draft["revision"]})
        return {"operation_id": operation_id, "revision": draft["revision"],
                "page": page_view, "replayed": False}

    def _verify_page_evidence(self, draft: dict, state: str,
                              evidence_ids: list,
                              definition: dict) -> tuple[dict, str]:
        """草稿页证据核验：draft 采集（一次消费）+ 服务端真实匹配。"""
        if not isinstance(evidence_ids, list) or len(evidence_ids) != 2 \
                or len(set(evidence_ids)) != 2:
            raise LayoutDraftError("layout_request_invalid",
                                   "每页必须携带本台服务端采集的两帧证据")
        found = {}
        for row in self._state.state_scan("evidence", draft["device_id"]):
            value = row["value"]
            if value.get("evidence_id") in evidence_ids:
                found[value["evidence_id"]] = value
        missing = [e for e in evidence_ids if e not in found]
        if missing:
            raise LayoutDraftError(
                "layout_resource_missing",
                f"证据 {missing} 不存在或不属于本设备", 404)
        capture_ids = {value["capture_id"] for value in found.values()}
        if len(capture_ids) != 1:
            raise LayoutDraftError("layout_request_invalid",
                                   "两帧必须来自同一次受控采集", 409)
        capture = self._state.state_get(
            "capture", state_key("capture", [capture_ids.pop()]))
        if capture is None or capture.get("owner_kind") != "draft" \
                or capture.get("owner_id") != draft["draft_id"] \
                or capture.get("state") != state:
            raise LayoutDraftError("layout_request_invalid",
                                   "采集不属于该草稿/该页面状态", 409)
        if capture.get("consumed_by") is not None:
            raise LayoutDraftError(
                "layout_request_conflict",
                "采集已绑定其他页面编辑请求；请重新采集", 409)
        expires = parse_iso(capture["expires_at"])
        if expires <= _now(self._clock):
            raise LayoutDraftError("layout_items_unverified",
                                   "采集已超过 120 秒未交给页面 PUT", 409)
        accepted_at = capture["created_at"]
        if {e["evidence_id"] for e in capture.get("evidence", [])} != set(evidence_ids):
            raise LayoutDraftError("layout_request_invalid",
                                   "请求证据与受控采集的两帧不一致", 409)
        check = self._match_page(draft, state, definition, evidence_ids, capture)
        try:
            self._state.state_consume(
                "capture", state_key("capture", [capture["capture_id"]]),
                "draft:" + draft["draft_id"])
        except LayoutStateError as exc:
            if exc.code != "layout_request_conflict":
                raise
        return check, accepted_at

    def _match_page(self, draft: dict, state: str, definition: dict,
                    evidence_ids: list, capture: dict) -> dict:
        """Two independently rectified frames must confirm the same page."""
        if self._rectifier is None:
            raise LayoutDraftError("layout_items_unverified",
                                   "未接入本台 v3 摆正，草稿页核验拒绝", 409)
        rectified = self._rectifier(
            draft["device_id"], {"installation_binding": draft["binding_snapshot"]},
            capture)
        anchor_bytes = collect_anchor_bytes(
            definition, lambda asset_id: self._templates.get_asset(
                draft["draft_id"], asset_id))
        error_bound = rectified.get("mapping_error_bound_px")
        checks = [compute_page_check(definition, frame, anchor_bytes, list(evidence_ids),
                                     mapping_error_bound_px=error_bound)
                  for frame in rectified["frames"]]
        size = rectified["summary"]["rectified_size"]
        return merge_page_checks(checks, definition, (size["width"], size["height"]),
                                 mapping_error_bound_px=error_bound)

    # ================= 发布 =================
    def publish(self, request: dict, principal: str) -> dict:
        client_request_id = request["client_request_id"]
        template_id = request.get("template_id")
        draft_id = request.get("draft_id")
        replay = self._replay(client_request_id, self._draft_device(draft_id),
                              request)
        if replay is not None:
            return replay
        # ---- 阶段 A：读取与全部核验（state 事务；模板连接只读） ----
        with self._state.transaction():
            draft = self._load(draft_id, principal)
            if int(draft["revision"]) != int(request["expected_draft_revision"]):
                raise LayoutDraftError(
                    "layout_revision_conflict",
                    f"草稿修订冲突（当前 {draft['revision']}，期望 "
                    f"{request['expected_draft_revision']}）", 409)
            pages = []
            for page in draft["pages"]:
                check = page["page_check"]
                if page["definition"] is None:
                    raise LayoutDraftError(
                        "layout_items_unverified",
                        f"四态 {page['state']} 尚未定义，不能发布", 409)
                if check is None or check.get("status") != "passed":
                    reason = (check or {}).get("reason_codes") or [
                        "page_not_verified"]
                    raise LayoutDraftError(
                        "layout_items_unverified",
                        f"四态 {page['state']} 模板检查未通过"
                        f"（{'、'.join(map(str, reason))}）", 409)
                pages.append(page["definition"])
            versions = self._templates.list_template_versions(template_id)
            parent_version = max(versions) if versions else None
            expected_parent = request.get("expected_parent_version")
            if expected_parent != parent_version:
                raise LayoutDraftError(
                    "layout_revision_conflict",
                    f"expected_parent_version 冲突（当前 {parent_version}，"
                    f"期望 {expected_parent}）", 409)
            version = (parent_version or 0) + 1
            compatibility = draft["compatibility"]
            descriptor = draft["reference_screen_descriptor"]
            draft_revision = int(draft["revision"])
            device_id = draft["device_id"]
        # ---- 阶段 B：资产复制与不可变模板写入（state 事务外；同库双连接
        #      同线程嵌套写会自锁）。同内容幂等、异内容冲突由 save_asset/
        #      save_template 闭集保证 ----
        referenced = sorted({anchor["asset_id"] for page in pages
                             for anchor in page.get("anchors", [])})
        assets = []
        for asset_id in referenced:
            data = self._templates.get_asset(draft_id, asset_id)
            if data is None:
                raise LayoutDraftError(
                    "layout_archive_invalid",
                    f"锚点引用资产 {asset_id} 缺失原字节", 503)
            from runtime.layout_ops import sha256_bytes
            width, height = _image_dimensions(data)
            media_type = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" \
                else "image/jpeg"
            ext = "png" if media_type == "image/png" else "jpg"
            self._templates.save_asset(template_id, asset_id, data)
            assets.append({"id": asset_id,
                           "relative_path": f"assets/{asset_id}.{ext}",
                           "sha256": sha256_bytes(data),
                           "media_type": media_type,
                           "width": width, "height": height})
        record = {
            "contract_version": "ui-layout-template.v1",
            "template_id": template_id,
            "version": version,
            "parent_version": parent_version,
            "compatibility": compatibility,
            "reference_screen_descriptor": descriptor,
            "pages": pages,
            "assets": assets,
            "created_at": iso(_now(self._clock)),
            "created_by": principal,
        }
        record["sha256"] = content_sha(record)
        status = self._templates.save_template(
            template_id, version, record, content_sha=record["sha256"])
        # ---- 阶段 C：草稿簿记（state 事务；修订已被并发修改则拒绝） ----
        with self._state.transaction():
            draft = self._load(draft_id, principal)
            if int(draft["revision"]) != draft_revision:
                raise LayoutDraftError(
                    "layout_revision_conflict",
                    "草稿在发布过程中被并发修改；请重新发布", 409)
            draft["published_refs"].append(
                {"id": template_id, "version": version,
                 "sha256": record["sha256"]})
            draft["revision"] = int(draft["revision"]) + 1
            draft["updated_at"] = iso(_now(self._clock))
            self._save(draft)
        operation_id = self._register(
            resource_kind="template", resource_id=template_id,
            device_id=device_id, principal=principal,
            client_request_id=client_request_id, request=request,
            response={"template_ref": {"id": template_id,
                                       "version": version,
                                       "sha256": record["sha256"]}})
        return {"operation_id": operation_id,
                "template_ref": {"id": template_id, "version": version,
                                 "sha256": record["sha256"]},
                "replayed": status == "reused"}


def _image_dimensions(data: bytes) -> tuple[int, int]:
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise LayoutDraftError("layout_archive_invalid",
                               "资产不是可解码 PNG/JPEG 图像", 503)
    height, width = image.shape[:2]
    return width, height
