"""L04/§8/§9：布局操作记录、资料包导出导入（冻结 package:manifest.v1）、回滚。

R2-20（2026-10-04 第二轮）：导入完整闭包进入同一不可变资源目录 + 已提交
catalog；正式 template/asset 读取与 start_session 使用同一解析器
（resolve_template/resolve_asset/resolve_adaptation/resolve_region）。
- 导出/导入 manifest 为冻结 `package:manifest.v1`（roots/entries/file_count/
  total_bytes/privacy_scope/sha256；entry 区分 record/asset，sha256 为文件
  原字节摘要；manifest.sha256 去自身字段计算；total_bytes 不含 manifest）。
- 导入可见性由 catalog 门控：写入先落各 Store/文件，catalog 最后一次事务
  统一提交；提交前解析器不可见（无半包可用）。同 ID+version 同内容复用、
  异内容整包冲突；不设置 current、不复制许可/校准/实时证据。
- 幂等：client_request_id 经持久台账（layout_state operation）同号同内容
  返回原响应、异内容 409；中断由启动对账按 §9 矩阵落 aborted/committed。
- 旧 `layout_imported:*`/`adaptation_version:*` KV 仅作只读 legacy 回退
  （历史读取/对账），新链路不再写入自由 KV。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from runtime.input_control import canonical_digest as _legacy_canonical_digest
from runtime.layout_state import (
    LayoutStateError,
    LayoutStateStore,
    content_sha,
    canonical_json,
    state_identity,
    state_key,
)
from runtime.util import iso, parse_iso, utc_now


def canonical_digest(value) -> str:
    """布局资料摘要：附件 canonical（紧凑分隔 JSON）的 SHA-256。

    注意与 input_control 的带空格 canonical 不同——布局附件规定资料摘要
    使用紧凑规范 JSON（README「严格性」节），输入摘要不改变既有字节。
    """
    return content_sha(value)

RESOURCE_KINDS = ("region", "template", "adaptation", "review", "draft",
                  "capture", "package")
MAX_IMPORT_BYTES = 200 * 1024 * 1024
MAX_IMPORT_FILES = 5000
PACKAGE_CONTRACT = "layout-package-manifest.v1"
ENTRY_PATH_RE = re.compile(
    r"^(?:templates|regions|adaptations)/[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    r"/[1-9][0-9]*/(?:record\.json|assets/(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/)*"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.(?:png|jpg|jpeg))$")
PREVIEW_TTL_SECONDS = 120


class LayoutOpsError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CatalogTemplateResolver:
    """R2-20 统一模板/资产解析器：适配服务 start_session、锚点资产读取与
    正式 GET 共用（导入资源经 catalog 门控可见；未命中回退本机模板 Store）。

    R3-13：全部读取走 LayoutOpsService 统一可见性判断（catalog 已提交或
    该资源自身已提交发布事务才可见）；回退 store 同样经过门控，不再旁路。"""

    def __init__(self, ops: "LayoutOpsService", fallback):
        self._ops = ops
        self._fallback = fallback

    def get_template(self, template_id: str, version: int) -> dict | None:
        return self._ops.resolve_template(template_id, version)

    def resolve_template_asset(self, owner_id: str, version: int | None,
                               asset_id: str) -> bytes | None:
        return self._ops.resolve_template_asset(owner_id, version, asset_id)

    def get_asset(self, owner_id: str, asset_id: str) -> bytes | None:
        return self._ops.resolve_template_asset(owner_id, None, asset_id)


class LayoutOpsService:
    def __init__(self, store, blobs_dir: str | Path = "data/layout_packages",
                 region_store=None, template_store=None,
                 state_store: LayoutStateStore | None = None,
                 data_root: str | Path | None = None, clock=None):
        self._store = store
        self._region_store = region_store
        self._template_store = template_store
        self._state = state_store
        self._data_root = Path(data_root) if data_root else (
            Path(blobs_dir).parent if state_store is None
            else state_store.data_root)
        self._blobs = Path(blobs_dir)
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._clock = clock or utc_now
        # R3-13 测试故障注入点（磁盘故障语义；不影响正常逻辑）
        self._fault_points: set[str] = set()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def arm_faults(self, points) -> None:
        self._fault_points = set(points)

    def _fault(self, point: str) -> None:
        if point in self._fault_points:
            raise LayoutOpsError(503, "layout_storage_failed",
                                 f"故障注入点 {point}")

    # ================ R3-13 统一可见性（GET/列表/资产/选模板同源） ================
    def _catalog_row(self, kind: str, res_id: str, version: int) -> dict | None:
        if self._state is None:
            return None
        return self._state.state_get(
            "catalog", state_key("catalog", [kind, res_id, version]))

    def _legacy_committed(self, kind: str, res_id: str, version: int) -> bool:
        """typed-state 之前已提交的历史资料（只读回退，不再新增）。"""
        if kind == "template":
            keys = (f"template_version:{res_id}:{version}",
                    f"layout_imported:template:-:{res_id}:v{version}")
        elif kind == "adaptation":
            keys = (f"adaptation_version:{res_id}:{version}",
                    f"layout_imported:adaptation:{res_id}:v{version}")
        elif kind == "region":
            keys = (f"region_version:{res_id}:{version}",)
        else:
            return False
        for key in keys:
            if self._store.load(key) is not None:
                return True
        return False

    def is_committed_visible(self, kind: str, res_id: str,
                             version: int) -> bool:
        """R3-13 唯一可见性判断（正式 GET/列表/资产下载/适配选模板全走它）：

        - catalog 已提交（导入/发布的最终 Store 事务），或
        - 该资源自身的已提交发布事务（本机草稿链发布：台账 committed），或
        - legacy 已提交历史（typed-state 之前的只读资料）。

        其余一切状态（写入中、部分落盘、catalog 未提交、aborted）不可见；
        半包资源不可读、不可选、不可下载。无 typed store 的旧装配没有
        catalog 机制（导入不可用），本机 store 即提交事实（legacy 语义）。"""
        if self._state is None:
            return True
        version = int(version)
        if self._catalog_row(kind, res_id, version) is not None:
            return True
        if self._state.operation_find_committed_resource(
                kind, res_id, version) is not None:
            return True
        return self._legacy_committed(kind, res_id, version)

    # ================ 统一资源解析器（GET/会话/导出导入同用） ================
    def resolve_template(self, template_id: str, version: int) -> dict | None:
        if self._template_store is not None \
                and self.is_committed_visible("template", template_id,
                                              version):
            record = self._template_store.get_template(template_id, version)
            if record is not None:
                return record
        # legacy 只读回退（历史导入副本/旧键，不再新增）
        for key in (f"template_version:{template_id}:{version}",
                    f"layout_imported:template:-:{template_id}:v{version}"):
            record = self._store.load(key)
            if record is not None:
                return record
        return None

    def resolve_template_asset(self, template_id: str,
                               version: int | None,
                               asset_id: str) -> bytes | None:
        """模板资产读取（R3-13 版本感知门控）：模板该版本已提交才可读。"""
        if version is not None:
            visible = self.is_committed_visible("template", template_id,
                                                version)
        else:
            # 无版本上下文（旧形状调用）：仅当该模板存在任一已提交版本时放行
            visible = self._template_has_committed_version(template_id)
        if not visible:
            return None
        if self._template_store is not None:
            data = self._template_store.get_asset(template_id, asset_id)
            if data is not None:
                return data
        record = self._store.load(f"template_asset:{template_id}:{asset_id}")
        if isinstance(record, dict) and record.get("data_b64"):
            return base64.b64decode(record["data_b64"])
        return None

    def _template_has_committed_version(self, template_id: str) -> bool:
        if self._state is not None:
            for row in self._state.state_scan("catalog"):
                resource = row["value"].get("resource") or {}
                if resource.get("kind") == "template" \
                        and (resource.get("ref") or {}).get("id") \
                        == template_id:
                    return True
            return self._state.operation_find_committed_resource(
                "template", template_id) is not None
        return False

    def list_visible_templates(self) -> list[dict]:
        """正式模板列表（R3-13）：只含已提交版本（半包不出现在列表）。"""
        if self._template_store is None:
            return []
        result = []
        for record in self._template_store.list_templates():
            template_id = record.get("template_id")
            version = record.get("version")
            if not template_id or not isinstance(version, int) or version < 1:
                continue
            if self.is_committed_visible("template", template_id, version):
                result.append(record)
        return result

    def resolve_adaptation(self, device_id: str, version: int) -> dict | None:
        if self._state is not None:
            row = self._state.state_get("catalog", state_key(
                "catalog", ["adaptation", device_id, version]))
            if row is not None:
                return self._state.read_adaptation_version(device_id, version)
        record = self._store.load(
            f"adaptation_version:{device_id}:{version}")
        if record is not None:
            return record
        return self._store.load(
            f"layout_imported:adaptation:{device_id}:v{version}")

    def resolve_region(self, device_id: str, version: int) -> dict | None:
        if self._region_store is not None:
            record = self._region_store.get_version(device_id, version)
            if record is not None:
                return record
        return self._store.load(f"region_version:{device_id}:{version}")

    def resolve_asset(self, owner_kind: str, owner_id: str,
                      asset_id: str) -> bytes | None:
        if owner_kind == "template" and self._template_store is not None:
            data = self._template_store.get_asset(owner_id, asset_id)
            if data is not None:
                return data
        if owner_kind == "adaptation" and self._state is not None:
            versions_dir = (self._data_root / "devices" / owner_id
                            / "layout_adaptation" / "versions")
            for version_dir in sorted(
                    versions_dir.glob("*")) if versions_dir.exists() else []:
                try:
                    version = int(version_dir.name)
                except ValueError:
                    continue
                data = self._state.read_adaptation_asset(
                    owner_id, version, asset_id)
                if data is not None:
                    return data
        for key in (f"template_asset:{owner_id}:{asset_id}",
                    f"layout_imported:asset:{owner_id}:{asset_id}"):
            record = self._store.load(key)
            if isinstance(record, dict) and record.get("data_b64"):
                return base64.b64decode(record["data_b64"])
        return None

    # ---- 兼容旧调用形状（api 路由/旧测试在用） ----
    def _load_resource(self, kind: str, device_id, res_id, version) -> dict | None:
        if kind == "region":
            return self.resolve_region(device_id, version)
        if kind == "adaptation":
            return self.resolve_adaptation(device_id, version)
        return self.resolve_template(res_id, version)

    def _template_asset(self, owner_id: str, asset_id: str) -> bytes | None:
        data = self.resolve_asset("template", owner_id, asset_id)
        if data is not None:
            return data
        return self.resolve_asset("adaptation", owner_id, asset_id)

    def _list_template_assets(self, owner_id: str) -> list[dict]:
        if self._template_store is not None:
            return self._template_store.list_assets(owner_id)
        result = []
        for key, record in self._store.scan_prefix(
                f"template_asset:{owner_id}:") if hasattr(
                self._store, "scan_prefix") else []:
            result.append({"asset_id": key.rsplit(":", 1)[1],
                           "sha256": record.get("sha256"),
                           "size_bytes": record.get("size_bytes")})
        return result

    # ================ 操作记录（旧 free-KV 台账：legacy 兼容保留） ================
    def _key(self, kind: str, op_id: str) -> str:
        return f"layout_op:{kind}:{op_id}"

    def prepare_operation(self, principal_id: str, device_id: str | None,
                          resource_kind: str, client_request_id: str,
                          request: dict) -> dict:
        if resource_kind not in RESOURCE_KINDS:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 f"resource_kind 必须属于 {RESOURCE_KINDS}")
        request_hash = _legacy_canonical_digest(request)
        operation_id = uuid.uuid4().hex
        record = {
            "operation_id": operation_id,
            "resource_kind": resource_kind,
            "resource_id": request.get("resource_id"),
            "device_id": device_id,
            "principal_id": principal_id,
            "request_id": client_request_id,
            "request_hash": request_hash,
            "before_revision": request.get("expected_revision"),
            "after_revision": None,
            "phase": "prepared",
            "version_ref": None,
            "response": None,
            "error": None,
            "created_at": iso(utc_now()),
            "updated_at": iso(utc_now()),
        }
        self._store.save(self._key("op", operation_id), record)
        self._store.save(self._key("op_req", client_request_id),
                         {"operation_id": operation_id})
        return record

    def commit_operation(self, operation_id: str, response: dict,
                         after_revision: int | None = None,
                         version_ref: dict | None = None) -> dict:
        record = self.get_operation(operation_id)
        if record is None:
            raise LayoutOpsError(404, "layout_resource_missing",
                                 f"操作 {operation_id} 不存在")
        if record["phase"] == "committed":
            return record  # 幂等提交保留首收响应
        record.update(phase="committed", response=response,
                      after_revision=after_revision, version_ref=version_ref,
                      updated_at=iso(utc_now()))
        self._store.save(self._key("op", operation_id), record)
        return record

    def fail_operation(self, operation_id: str, error: dict,
                       phase: str = "aborted") -> dict | None:
        record = self.get_operation(operation_id)
        if record is None:
            return None
        record.update(phase=phase, error=error, updated_at=iso(utc_now()))
        self._store.save(self._key("op", operation_id), record)
        return record

    def get_operation(self, operation_id: str) -> dict | None:
        if self._state is not None:
            try:
                record = self._state.operation_get(operation_id)
                if record is not None:
                    return record
            except LayoutStateError:
                pass
        return self._store.load(self._key("op", operation_id))

    def get_operation_by_request(self, client_request_id: str) -> dict | None:
        if self._state is not None:
            record = self._state.operation_get_by_request(client_request_id)
            if record is not None:
                return record
        link = self._store.load(self._key("op_req", client_request_id))
        if link is None:
            return None
        return self.get_operation(link["operation_id"])

    @staticmethod
    def projection(record: dict | None) -> dict | None:
        if record is None:
            return None
        return {"operation_id": record["operation_id"],
                "phase": record["phase"],
                "original_response": record.get("response"),
                "error": record.get("error")}

    def find_replay(self, principal_id: str, device_id: str | None,
                    client_request_id: str, request: dict) -> dict | None:
        """同身份同设备同编号同摘要 → 原记录（replayed 语义由调用方回放响应）。"""
        record = self.get_operation_by_request(client_request_id)
        if record is None:
            return None
        if record["principal_id"] != principal_id \
                or record["device_id"] != device_id:
            raise LayoutOpsError(403, "layout_permission_denied",
                                 "client_request_id 已被其他身份使用")
        if record["request_hash"] != _legacy_canonical_digest(request):
            raise LayoutOpsError(409, "layout_request_conflict",
                                 "同 client_request_id 携带了不同内容")
        return record

    # ================ 资料包导出（冻结 manifest 闭包 + 原字节资产） ================
    def _closure_refs(self, records: dict) -> None:
        """records: {(kind, id, version): record}；补齐传递引用。"""
        visited = set()
        pending = list(records.keys())
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            kind, res_id, version = key
            record = records[key]
            refs = []
            if kind == "adaptation":
                template_ref = record.get("template_ref")
                if isinstance(template_ref, dict) and template_ref.get("id"):
                    refs.append(("template", template_ref["id"],
                                 template_ref.get("version")))
                if not template_ref and record.get("template_id"):
                    refs.append(("template", record["template_id"],
                                 record.get("template_version")))
                region_ref = record.get("installation_region_ref")
                if isinstance(region_ref, dict) and region_ref.get("id"):
                    refs.append(("region", region_ref["id"],
                                 region_ref.get("version")))
            elif kind == "template":
                parent = record.get("parent_version")
                if isinstance(parent, int) and parent != version:
                    refs.append(("template", res_id, parent))
                base = record.get("base_template_ref")
                if isinstance(base, dict) and base.get("id"):
                    refs.append(("template", base["id"], base.get("version")))
            for ref_kind, ref_id, ref_version in refs:
                if not isinstance(ref_version, int) or ref_version < 1:
                    raise LayoutOpsError(
                        503, "layout_archive_invalid",
                        f"{kind} {res_id} v{version} 引用了不完整的目标")
                ref_key = (ref_kind, ref_id, ref_version)
                if ref_key in records:
                    pending.append(ref_key)
                    continue
                resolved = self._load_resource(ref_kind, ref_id
                                               if ref_kind != "template"
                                               else None, ref_id, ref_version)
                if resolved is None:
                    raise LayoutOpsError(
                        503, "layout_archive_invalid",
                        f"闭包父档缺失：{ref_kind} {ref_id} v{ref_version}")
                records[ref_key] = resolved
                pending.append(ref_key)

    def export_package(self, principal_id: str, payload: dict) -> dict:
        items = payload.get("items")
        if items is None:
            items = [{"kind": root.get("kind"),
                      "device_id": (root.get("ref") or {}).get("id"),
                      "id": (root.get("ref") or {}).get("id"),
                      "version": (root.get("ref") or {}).get("version")}
                     for root in (payload.get("roots") or [])]
        if not isinstance(items, list) or not items:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "items/roots 必须是非空数组")
        records: dict = {}
        roots = []
        for item in items:
            kind = item.get("kind")
            version = item.get("version")
            if kind not in ("region", "template", "adaptation"):
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"kind={kind} 不可导出（只允许不可变资料）")
            if not isinstance(version, int) or version < 1:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     "version 必须是正整数")
            res_id = item.get("id")
            device_id = item.get("device_id")
            if kind == "template":
                res_id = res_id or device_id
                device_id = None
            if kind != "template" and not device_id:
                device_id = res_id
            record = self._load_resource(kind, device_id, res_id, version)
            if record is None:
                raise LayoutOpsError(404, "layout_resource_missing",
                                     f"{kind} {res_id or device_id} "
                                     f"v{version} 不存在")
            key = (kind, res_id if kind == "template" else device_id, version)
            records[key] = record
            roots.append({"kind": kind, "ref": {
                "id": key[1], "version": version,
                "sha256": record.get("sha256")
                or canonical_digest(record)}})
        self._closure_refs(records)

        # manifest：冻结 package:manifest.v1（记录 + 资产原字节，闭包授权在路由层）
        entries: list[dict] = []
        listed: set[str] = set()

        def record_entry(kind, owner_id, version, record):
            relative = f"{kind}s/{owner_id}/{version}/record.json"
            payload = canonical_json(record).encode("utf-8")
            entry = {"path": relative, "kind": "record",
                     "resource": {"kind": kind,
                                  "ref": {"id": owner_id,
                                          "version": version,
                                          "sha256": record.get("sha256")
                                          or canonical_digest(record)}},
                     "owner": None, "asset": None,
                     "byte_size": len(payload),
                     "sha256": sha256_bytes(payload)}
            entries.append(entry)
            listed.add(relative)
            return entry

        for (kind, owner_id, version), record in sorted(
                records.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
            record_entry(kind, owner_id, version, record)

        for (kind, owner_id, version), record in sorted(
                records.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
            if kind == "adaptation" and self._state is not None:
                # 本台颜色资源（空白输入参考图）：随适配版本目录原字节导出
                for sample in record.get("color_samples") or []:
                    reference = sample.get("empty_input_asset_ref")
                    if not reference:
                        continue
                    data_path = (self._state.data_root / "devices" /
                                 owner_id / "layout_adaptation" /
                                 "versions" / str(version) / "assets" /
                                 f"{reference['id']}.png")
                    if not data_path.exists():
                        raise LayoutOpsError(
                            503, "layout_archive_invalid",
                            f"适配 {owner_id} v{version} 颜色资源 "
                            f"{reference['id']} 缺失原字节")
                    data = data_path.read_bytes()
                    image = _image_size(data)
                    relative = (f"adaptations/{owner_id}/{version}/"
                                f"assets/{reference['id']}.png")
                    entries.append({
                        "path": relative, "kind": "asset", "resource": None,
                        "owner": {"kind": "adaptation",
                                  "ref": {"id": owner_id, "version": version,
                                          "sha256": record.get("sha256")
                                          or canonical_digest(record)}},
                        "asset": {"id": reference["id"],
                                  "relative_path":
                                      f"assets/{reference['id']}.png",
                                  "sha256": sha256_bytes(data),
                                  "media_type": "image/png",
                                  "width": image[0], "height": image[1]},
                        "byte_size": len(data), "sha256": sha256_bytes(data)})
                    listed.add(relative)
            if kind != "template":
                continue
            for meta in sorted(self._list_template_assets(owner_id),
                               key=lambda a: a["asset_id"]):
                data = self._template_asset(owner_id, meta["asset_id"])
                if data is None:
                    raise LayoutOpsError(
                        503, "layout_archive_invalid",
                        f"模板 {owner_id} 资产 {meta['asset_id']} 缺失原字节")
                digest = sha256_bytes(data)
                if meta.get("sha256") not in (None, digest):
                    raise LayoutOpsError(
                        503, "layout_archive_invalid",
                        f"模板 {owner_id} 资产 {meta['asset_id']} 字节与摘要不符")
                ext = "png"
                if data[:3] == b"\xff\xd8\xff":
                    ext = "jpg"
                relative = (f"templates/{owner_id}/{version}/"
                            f"assets/{meta['asset_id']}.{ext}")
                media_type = "image/png" if ext == "png" else "image/jpeg"
                image = _image_size(data)
                entries.append({
                    "path": relative, "kind": "asset",
                    "resource": None,
                    "owner": {"kind": "template",
                              "ref": {"id": owner_id, "version": version,
                                      "sha256": records[
                                          ("template", owner_id, version)]
                                      .get("sha256")
                                      or canonical_digest(records[
                                          ("template", owner_id, version)])}},
                    "asset": {"id": meta["asset_id"],
                              "relative_path": f"assets/{meta['asset_id']}.{ext}",
                              "sha256": digest,
                              "media_type": media_type,
                              "width": image[0], "height": image[1]},
                    "byte_size": len(data), "sha256": digest})
                listed.add(relative)

        entries.sort(key=lambda entry: entry["path"])  # 冻结语义：按 path 排序
        total_bytes = sum(entry["byte_size"] for entry in entries)
        manifest = {
            "contract_version": PACKAGE_CONTRACT,
            "package_id": str(uuid.uuid4()),
            "roots": roots,
            "entries": entries,
            "file_count": 1 + len(entries),
            "total_bytes": total_bytes,
            "created_at": iso(self._now()),
            "created_by": principal_id,
            "privacy_scope": "sanitized_layout_only",
        }
        manifest["sha256"] = canonical_digest(
            {k: v for k, v in manifest.items() if k != "sha256"})
        blob = self._zip_manifest(manifest, records)
        archive_sha = sha256_bytes(blob)
        package_id = manifest["package_id"]
        (self._blobs / f"{package_id}.zip").write_bytes(blob)
        record = {"package_id": package_id, "manifest": manifest,
                  "archive_sha256": archive_sha,
                  "size_bytes": len(blob), "created_at": manifest["created_at"],
                  "created_by": principal_id}
        self._store.save(f"layout_package:{package_id}", record)
        if self._state is not None:
            export_record = {
                "package_id": package_id,
                "principal_id": principal_id,
                "operation_id": "",  # 由调用方经 ledger 关联（导出无独立 op 时留空）
                "manifest": manifest,
                "archive_sha256": archive_sha,
                "archive_byte_size": len(blob),
                "created_at": manifest["created_at"],
            }
            key = state_key("package_export", state_identity(
                "package_export", export_record))
            self._state.state_insert("package_export", key, None,
                                     principal_id, export_record)
        return record

    def _zip_manifest(self, manifest: dict, records: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", canonical_json(manifest))
            for entry in manifest["entries"]:
                if entry["kind"] == "record":
                    resource = entry["resource"]
                    key = (resource["kind"], resource["ref"]["id"],
                           resource["ref"]["version"])
                    zf.writestr(entry["path"],
                                canonical_json(records[key]).encode("utf-8"))
                else:
                    owner = entry["owner"]
                    if owner["kind"] == "adaptation" and self._state is not None:
                        # 本台颜色资源：从适配版本目录读原字节
                        asset_path = (self._state.data_root / "devices" /
                                      owner["ref"]["id"] /
                                      "layout_adaptation" / "versions" /
                                      str(owner["ref"]["version"]) /
                                      "assets" /
                                      f"{entry['asset']['id']}.png")
                        if not asset_path.exists():
                            raise LayoutOpsError(
                                503, "layout_archive_invalid",
                                f"适配颜色资源 {entry['asset']['id']} 缺失")
                        data = asset_path.read_bytes()
                    else:
                        data = self._template_asset(owner["ref"]["id"],
                                                    entry["asset"]["id"])
                    if data is None:
                        raise LayoutOpsError(
                            503, "layout_archive_invalid",
                            f"包资产 {entry['asset']['id']} 缺失原字节")
                    zf.writestr(entry["path"], data)
        return buf.getvalue()

    def get_package(self, package_id: str) -> dict | None:
        return self._store.load(f"layout_package:{package_id}")

    def read_package_bytes(self, package_id: str) -> bytes | None:
        path = self._blobs / f"{Path(package_id).name}.zip"
        return path.read_bytes() if path.exists() else None

    # ================ 资料包导入（冻结 manifest；catalog 门控可见性） ================
    @staticmethod
    def _validate_entry_name(name: str) -> None:
        if name.startswith("/") or "\\" in name or ":" in name \
                or "%" in name or "\x00" in name:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 f"条目路径不合法：{name}")
        parts = PurePosixPath(name).parts
        if not parts or any(p in ("", ".", "..") for p in parts) \
                or name.endswith("/"):
            raise LayoutOpsError(422, "layout_request_invalid",
                                 f"条目路径不合法：{name}")

    def _parse_zip(self, blob: bytes) -> dict:
        """冻结 package:manifest.v1 严格校验；返回 manifest。"""
        if len(blob) > MAX_IMPORT_BYTES:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "包超过 200MiB 上限")
        names: set[str] = set()
        lowered: set[str] = set()
        total = 0
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            raise LayoutOpsError(422, "layout_request_invalid", "不是合法 zip 包")
        for zi in zf.infolist():
            name = zi.filename
            if name in names:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"重复条目 {name}")
            names.add(name)
            low = name.lower()
            if low in lowered:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"条目路径大小写碰撞：{name}")
            lowered.add(low)
            self._validate_entry_name(name)
            if zi.is_dir():
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"不允许目录条目：{name}")
            if (zi.external_attr >> 16) & 0o170000 == 0o120000:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"条目是符号链接：{name}")
            total += zi.file_size
            if total > MAX_IMPORT_BYTES:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     "解包总量超过 200MiB 上限")
            if len(names) > MAX_IMPORT_FILES:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     "条目数超过 5000 上限")
        if "manifest.json" not in names:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "缺少 manifest.json")
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "manifest.json 不是合法 JSON")
        self._validate_manifest(manifest, zf, names)
        return manifest

    def _validate_manifest(self, manifest: dict, zf, names: set[str]) -> None:
        required = ("contract_version", "package_id", "roots", "entries",
                    "file_count", "total_bytes", "created_at", "created_by",
                    "privacy_scope", "sha256")
        missing = [key for key in required if key not in manifest]
        if missing:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 f"manifest 缺少 {missing}")
        if manifest["contract_version"] != PACKAGE_CONTRACT:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 f"contract_version 必须为 {PACKAGE_CONTRACT}")
        try:
            uuid.UUID(str(manifest["package_id"]))
        except (ValueError, AttributeError):
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "package_id 必须是 UUID")
        if manifest.get("privacy_scope") != "sanitized_layout_only":
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "privacy_scope 必须为 sanitized_layout_only")
        embedded = manifest.get("sha256")
        if embedded != canonical_digest(
                {k: v for k, v in manifest.items() if k != "sha256"}):
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "manifest sha256 校验失败")
        roots = manifest.get("roots")
        if not isinstance(roots, list) or not roots:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "manifest roots 必须是非空数组")
        entries = manifest.get("entries")
        if not isinstance(entries, list) or not entries \
                or len(entries) > MAX_IMPORT_FILES:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "manifest entries 必须为 1..4999")
        manifest_bytes = len(canonical_json(manifest).encode("utf-8"))
        if manifest.get("file_count") != 1 + len(entries):
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "file_count 必须 = 1 + entries（含 manifest）")
        if manifest.get("total_bytes") != sum(
                e.get("byte_size", 0) for e in entries):
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "total_bytes 必须 = entries 字节总数（不含 manifest）")
        if manifest_bytes + manifest["total_bytes"] > MAX_IMPORT_BYTES:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "包总量超过 200MiB 上限")
        listed = {"manifest.json"}
        seen_paths: set[str] = set()
        record_resources: dict[tuple, dict] = {}
        for entry in entries:
            path = entry.get("path")
            if not isinstance(path, str) or not ENTRY_PATH_RE.match(path):
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"entry 路径不合法：{path}")
            if path in seen_paths:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"entry 路径重复：{path}")
            seen_paths.add(path)
            if path not in names:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"entry 文件缺失：{path}")
            listed.add(path)
            kind = entry.get("kind")
            data = zf.read(path)
            if entry.get("byte_size") != len(data) \
                    or entry.get("sha256") != sha256_bytes(data):
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"entry 字节/摘要不符：{path}")
            if kind == "record":
                resource = entry.get("resource")
                if not isinstance(resource, dict) \
                        or resource.get("kind") not in (
                            "template", "region", "adaptation") \
                        or not isinstance(resource.get("ref"), dict) \
                        or entry.get("asset") is not None \
                        or entry.get("owner") is not None:
                    raise LayoutOpsError(422, "layout_request_invalid",
                                         f"record entry 结构不合法：{path}")
                try:
                    record = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise LayoutOpsError(422, "layout_request_invalid",
                                         f"记录不是合法 JSON：{path}")
                if not isinstance(record, dict):
                    raise LayoutOpsError(422, "layout_request_invalid",
                                         f"记录必须是对象：{path}")
                ref = resource["ref"]
                record_resources[(resource["kind"], ref.get("id"),
                                  ref.get("version"))] = record
            elif kind == "asset":
                asset = entry.get("asset")
                owner = entry.get("owner")
                if not isinstance(asset, dict) or not isinstance(owner, dict) \
                        or entry.get("resource") is not None \
                        or owner.get("kind") not in (
                            "template", "region", "adaptation"):
                    raise LayoutOpsError(422, "layout_request_invalid",
                                         f"asset entry 结构不合法：{path}")
            else:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"entry kind 必须为 record|asset：{path}")
        for name in names:
            if name not in listed:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"包内含未列文件：{name}")
        # 闭包：记录引用的父档必须在包内或本机已存在；缺父档拒绝
        for (kind, res_id, version), record in record_resources.items():
            refs = []
            if kind == "adaptation":
                template_ref = record.get("template_ref")
                if isinstance(template_ref, dict) and template_ref.get("id"):
                    refs.append(("template", template_ref.get("id"),
                                 template_ref.get("version")))
                if not template_ref and record.get("template_id"):
                    refs.append(("template", record.get("template_id"),
                                 record.get("template_version")))
                region_ref = record.get("installation_region_ref")
                if isinstance(region_ref, dict) and region_ref.get("id"):
                    refs.append(("region", region_ref.get("id"),
                                 region_ref.get("version")))
            elif kind == "template":
                parent = record.get("parent_version")
                if isinstance(parent, int) and parent != version:
                    refs.append(("template", res_id, parent))
            for ref_kind, ref_id, ref_version in refs:
                if not isinstance(ref_version, int):
                    continue
                if (ref_kind, ref_id, ref_version) in record_resources:
                    continue
                if self._load_resource(
                        ref_kind,
                        ref_id if ref_kind != "template" else None,
                        ref_id, ref_version) is None:
                    raise LayoutOpsError(
                        503, "layout_archive_invalid",
                        f"闭包父档缺失：{ref_kind} {ref_id} v{ref_version}")
        # 资产 owner 必须是包内记录
        for entry in entries:
            if entry["kind"] != "asset":
                continue
            owner = entry["owner"]["ref"]
            if (entry["owner"]["kind"], owner.get("id"),
                    owner.get("version")) not in record_resources:
                raise LayoutOpsError(422, "layout_request_invalid",
                                     f"资产 owner 不在包内：{entry['path']}")

    def _entry_status(self, entry: dict, record: dict | None = None) -> str:
        """对照本机 catalog/资源：new / reuse / conflict。

        R3-13：可见性判断用已提交门控解析器；另对**未提交但物理已占用**
        的同号模板行（写入中断残留）按同内容复用、异内容冲突——占用不
        等于可见，但绝不允许异内容整包洗掉占用号。"""
        if entry["kind"] == "record":
            resource = entry["resource"]
            ref = resource["ref"]
            local = self._load_resource(resource["kind"],
                                        ref["id"] if resource["kind"] != "template"
                                        else None, ref["id"], ref["version"])
            if local is None and resource["kind"] == "template" \
                    and self._template_store is not None:
                raw = self._template_store.get_template(ref["id"],
                                                        ref["version"])
                if raw is not None:
                    local = raw
            if local is None and resource["kind"] == "adaptation" \
                    and self._state is not None:
                # catalog 未提交但磁盘已有同号版本文件（aborted 孤档/占用号）：
                # 摘要一致按复用，异内容按冲突（不覆盖不可变文件）
                raw = self._state.read_adaptation_version(ref["id"],
                                                          ref["version"])
                if raw is not None:
                    local = raw
            if local is None:
                return "new"
            return "reuse" if canonical_digest(local) \
                == canonical_digest(record) else "conflict"
        asset = entry["asset"]
        owner = entry["owner"]["ref"]
        owner_kind = entry["owner"]["kind"]
        local = self.resolve_asset(owner_kind, owner["id"], asset["id"])
        if local is None:
            return "new"
        return "reuse" if sha256_bytes(local) == asset["sha256"] else "conflict"

    def import_preview(self, principal_id: str, blob: bytes,
                       privacy_confirmed: bool,
                       client_request_id: str | None = None) -> dict:
        if privacy_confirmed is not True:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "导入预览必须确认 privacy_confirmed")
        manifest = self._parse_zip(blob)
        archive_sha = sha256_bytes(blob)
        zf = zipfile.ZipFile(io.BytesIO(blob))
        items = []
        conflicts = []
        reused = []
        to_import = []
        for entry in manifest["entries"]:
            record = None
            if entry["kind"] == "record":
                record = json.loads(
                    zf.read(entry["path"]).decode("utf-8"))
            status = self._entry_status(entry, record)
            if entry["kind"] != "record":
                continue  # 冻结 import_preview.items 只列记录资源（按 manifest 序）
            resource = entry["resource"]
            item_resource = {"kind": resource["kind"],
                             "ref": dict(resource["ref"])}
            items.append({"resource": item_resource, "status": status})
            if status == "conflict":
                conflicts.append(item_resource)
            elif status == "reuse":
                reused.append(item_resource)
            else:
                to_import.append(item_resource)
        preview = {
            "manifest_sha256": manifest["sha256"],
            "items_total": len(items),
            "to_import": to_import, "reused": reused,
            "conflicts": conflicts, "blocked": bool(conflicts),
            "items": items, "can_import": not conflicts,
        }
        now = self._now()
        preview_id = str(uuid.uuid4())
        # 暂存原字节供提交使用（进入 catalog 前不可经 API 读取）
        staging = self._data_root / "layout_staging" / preview_id
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "package.zip").write_bytes(blob)
        if self._state is not None:
            record_row = {
                "preview_id": preview_id,
                "principal_id": principal_id,
                "request_hash": canonical_digest({"archive_sha256": archive_sha}),
                "archive_sha256": archive_sha,
                "manifest": manifest,
                "items": items,
                "created_at": iso(now),
                "expires_at": iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
                "server_instance_id": "ops",
                "created_monotonic_ms": 0,
                "consumed_by": None,
            }
            key = state_key("package_import_preview", state_identity(
                "package_import_preview", record_row))
            self._state.state_insert("package_import_preview", key, None,
                                     principal_id, record_row)
        # legacy KV 预览记录（提交路径兼容；由 catalog 门控可见性）
        self._store.save(f"layout_import_preview:{preview_id}",
                         {"preview": preview, "principal_id": principal_id,
                          "manifest": manifest,
                          "archive_sha256": archive_sha,
                          "staging_dir": str(staging),
                          "created_at": iso(now)})
        return {"preview_id": preview_id, "manifest": manifest,
                "archive_sha256": archive_sha,
                "items": items, "can_import": not conflicts,
                "created_at": iso(now),
                "expires_at": iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
                **{k: preview[k] for k in
                   ("to_import", "reused", "conflicts", "blocked")}}

    def _apply_import(self, manifest: dict, zf) -> list[dict]:
        """写入各 Store/文件（catalog 之前，解析器不可见）；返回待提交
        catalog 行列表（只为本机尚不存在的资源建行，重放复用不重建）。
        任何失败向上抛出，由调用方标记操作 aborted。"""
        catalog_rows = []
        for entry in manifest["entries"]:
            if entry["kind"] != "record":
                continue
            resource = entry["resource"]
            ref = resource["ref"]
            record = json.loads(zf.read(entry["path"]).decode("utf-8"))
            local = self._load_resource(resource["kind"],
                                        ref["id"] if resource["kind"] != "template"
                                        else None, ref["id"], ref["version"])
            if local is not None:
                if canonical_digest(local) != canonical_digest(record):
                    raise LayoutOpsError(
                        409, "layout_request_conflict",
                        f"{resource['kind']} {ref['id']} v{ref['version']} "
                        "本机已有异内容版本，整包冲突")
                continue  # 同内容复用：不重建资源，不重建 catalog 行
            if resource["kind"] == "template":
                if self._template_store is not None:
                    try:
                        self._template_store.save_template(
                            ref["id"], ref["version"], record,
                            content_sha=record.get("sha256")
                            or canonical_digest(record))
                    except Exception as exc:
                        code = getattr(exc, "code", "")
                        if code in ("version_conflict", "asset_conflict"):
                            raise LayoutOpsError(
                                409, "layout_request_conflict",
                                f"模板 {ref['id']} v{ref['version']} "
                                "本机已有异内容版本，整包冲突")
                        raise
            elif resource["kind"] == "region":
                if self._region_store is not None:
                    self._region_store.insert_version(
                        ref["id"], ref["version"], record)
            else:  # adaptation：不可变版本文件（catalog 提交前不可见）
                if self._state is not None:
                    try:
                        self._state.write_adaptation_version(
                            ref["id"], ref["version"], record)
                    except LayoutStateError as exc:
                        if exc.code == "layout_archive_invalid":
                            raise LayoutOpsError(
                                409, "layout_request_conflict", exc.message)
                        raise
            catalog_rows.append({"table": "catalog", "expected_revision": 0,
                                 "value": {
                                     "resource": {"kind": resource["kind"],
                                                  "ref": dict(ref)},
                                     "operation_id": "",
                                     "created_at": iso(self._now())}})
        for entry in manifest["entries"]:
            if entry["kind"] != "asset":
                continue
            data = zf.read(entry["path"])
            asset = entry["asset"]
            owner = entry["owner"]
            if owner["kind"] == "template":
                if self._template_store is not None:
                    try:
                        self._template_store.save_asset(
                            owner["ref"]["id"], asset["id"], data)
                    except Exception as exc:
                        code = getattr(exc, "code", "")
                        if code == "asset_conflict":
                            raise LayoutOpsError(
                                409, "layout_request_conflict",
                                f"资产 {asset['id']} 本机已有异字节版本")
                        raise
            elif owner["kind"] == "adaptation" and self._state is not None:
                # 相对路径保持包内文件名（…/assets/{id}.png）
                self._state.write_adaptation_asset(
                    owner["ref"]["id"], owner["ref"]["version"],
                    asset["relative_path"].split("/")[-1], data)
        return catalog_rows

    def _commit_catalog(self, catalog_rows: list[dict],
                        operation_id: str) -> None:
        for row in catalog_rows:
            row["value"]["operation_id"] = operation_id
        with self._state.transaction():
            for row in catalog_rows:
                self._state.apply_mutation(row)

    def _plan_import_catalog_rows(self, manifest: dict, zf) -> list[dict]:
        """提交前规划待提交 catalog 行（只为本机尚不存在的资源建行）。"""
        catalog_rows = []
        for entry in manifest["entries"]:
            if entry["kind"] != "record":
                continue
            resource = entry["resource"]
            ref = resource["ref"]
            record = json.loads(zf.read(entry["path"]).decode("utf-8"))
            local = self._load_resource(
                resource["kind"],
                ref["id"] if resource["kind"] != "template" else None,
                ref["id"], ref["version"])
            if local is not None:
                if canonical_digest(local) != canonical_digest(record):
                    raise LayoutOpsError(
                        409, "layout_request_conflict",
                        f"{resource['kind']} {ref['id']} v{ref['version']} "
                        "本机已有异内容版本，整包冲突")
                continue  # 同内容复用：不重建资源，不重建 catalog 行
            catalog_rows.append({"table": "catalog", "expected_revision": 0,
                                 "value": {
                                     "resource": {"kind": resource["kind"],
                                                  "ref": dict(ref)},
                                     "operation_id": None,
                                     "created_at": iso(self._now())}})
        return catalog_rows

    def import_commit(self, principal_id: str, preview_id: str,
                      client_request_id: str | None = None,
                      archive_sha256: str | None = None) -> dict:
        # ① 幂等查重先行：丢回包/重启后同编号重放不依赖预览记录仍存在
        if client_request_id and self._state is not None:
            prior = self._state.operation_get_by_request(client_request_id)
            if prior is not None:
                if prior.get("principal_id") != principal_id:
                    raise LayoutOpsError(403, "layout_permission_denied",
                                         "client_request_id 已被其他身份使用")
                intent = self._state.intent_get(prior["operation_id"])
                if prior.get("response") is not None:
                    original = intent["request"] if intent else {}
                    presented = {"preview_id": preview_id,
                                 "archive_sha256":
                                     archive_sha256
                                     or original.get("archive_sha256")}
                    if intent is not None and content_sha(presented) != \
                            intent["request_hash"]:
                        raise LayoutOpsError(409, "layout_request_conflict",
                                             "同编号携带不同内容")
                    return {**prior["response"], "replayed": True}
                raise LayoutOpsError(409, "layout_request_conflict",
                                     "同编号操作未完成提交；请按 operation 查询")
        rec = self._store.load(f"layout_import_preview:{preview_id}")
        if rec is None:
            raise LayoutOpsError(404, "layout_resource_missing",
                                 "导入预览不存在或已失效")
        if rec["principal_id"] != principal_id:
            raise LayoutOpsError(403, "layout_permission_denied",
                                 "预览属于其他身份")
        preview = rec["preview"]
        if preview["blocked"]:
            raise LayoutOpsError(409, "layout_request_conflict",
                                 "存在同 ID/version 异内容冲突，禁止部分导入")
        manifest = rec["manifest"]
        archive_sha = rec.get("archive_sha256")
        if archive_sha256 is not None and archive_sha \
                and archive_sha256 != archive_sha:
            raise LayoutOpsError(409, "layout_request_conflict",
                                 "archive_sha256 与预览不一致")
        # 提交时锁内复核：任一冲突整包拒绝，不留半包
        staging_dir = rec.get("staging_dir")
        archive_path = (Path(staging_dir) / "package.zip") \
            if staging_dir else None
        if archive_path is None or not archive_path.exists():
            raise LayoutOpsError(503, "layout_archive_invalid",
                                 "预览暂存原字节缺失；请重新预览")
        archive_bytes = archive_path.read_bytes()
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
        for entry in manifest["entries"]:
            record = json.loads(zf.read(entry["path"]).decode("utf-8")) \
                if entry["kind"] == "record" else None
            status = self._entry_status(entry, record)
            if status == "conflict":
                raise LayoutOpsError(409, "layout_request_conflict",
                                     "提交复核发现同 ID/version 异内容冲突，"
                                     "整包拒绝")
        operation_id = str(uuid.uuid4())
        planned_response = self._import_response(manifest, preview,
                                                 operation_id)
        # ② R3-13：正式资源写入前先持久操作意图（完整请求摘要、planned
        #    响应、暂存包文件与 catalog mutation 计划；恢复不重新生成）。
        catalog_rows = self._plan_import_catalog_rows(manifest, zf)
        if self._state is not None and client_request_id:
            self._state.operation_prepare(
                principal_id=principal_id, device_id=None,
                resource_kind="package",
                resource_id=manifest["package_id"],
                client_request_id=client_request_id,
                request={"preview_id": preview_id,
                         "archive_sha256": archive_sha},
                planned_response=planned_response,
                files=[{"relative_path":
                        str(Path(staging_dir).relative_to(self._data_root)
                        / "package.zip") if staging_dir else
                        f"layout_staging/{preview_id}/package.zip",
                        "sha256": sha256_bytes(archive_bytes),
                        "byte_size": len(archive_bytes)}],
                pointer_before=None, pointer_after=None,
                mutations=catalog_rows, version_ref=None,
                operation_id=operation_id)
        # ③ 资源写入（未提交 catalog 前经统一可见性不可见）→ catalog 一次
        #    事务提交 → 台账 committed。任一步失败：aborted，未提交资源不可见。
        try:
            self._fault("promote_records")
            self._apply_import(manifest, zf)
            self._fault("promote_assets")
            if self._state is not None:
                self._fault("catalog")
                self._commit_catalog(catalog_rows, operation_id)
            self._fault("before_response")
            if self._state is not None and client_request_id:
                self._state.operation_commit(
                    operation_id, response=planned_response,
                    after_revision=None)
        except (LayoutOpsError, LayoutStateError) as exc:
            if self._state is not None and client_request_id:
                self._state.operation_abort(operation_id, {
                    "code": "layout_request_conflict",
                    "message": "导入整包冲突或写入失败，已中止；"
                               "未提交 catalog 前资源不可见"})
            if isinstance(exc, LayoutStateError):
                # 存储/CAS 冲突 → 冻结 layout_* 错误外壳（不泄漏内部细节）
                raise LayoutOpsError(getattr(exc, "status", 409),
                                     getattr(exc, "code",
                                             "layout_request_conflict"),
                                     getattr(exc, "message", str(exc))) from exc
            raise
        result = self._import_response(manifest, preview, operation_id)
        if hasattr(self._store, "delete"):
            self._store.delete(f"layout_import_preview:{preview_id}")
        return result

    @staticmethod
    def _import_response(manifest: dict, preview: dict,
                         operation_id: str) -> dict:
        return {"status": "imported",
                "operation_id": operation_id,
                "package_id": manifest["package_id"],
                "items": preview["items"],
                "reused": preview["reused"],
                "manifest_sha256": manifest["sha256"],
                "pointers_changed": False,
                "replayed": False,
                "note": "导入资料未接入本台 current 指针，"
                        "须重新完成本台适配并人工选用"}

    # ================ 回滚（R3-16：kind 显式路由） ================
    # 四态闭集（冻结顺序；与 layout_adaptation.PAGE_STATES 一致）
    _FOUR_STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty",
                    "chat_ime_text")
    ROLLBACK_TTL_SECONDS = 120

    def attach_adaptation_rollback_env(self, *, session_loader=None,
                                       capture_provider=None,
                                       calibration_provider=None,
                                       binding_provider=None,
                                       device_idle=None) -> None:
        """R3-16 适配回滚事实源（app 装配注入；缺提供器按既定错误拒绝）。"""
        self._rollback_env = {
            "session_loader": session_loader,
            "capture_provider": capture_provider,
            "calibration_provider": calibration_provider,
            "binding_provider": binding_provider,
            "device_idle": device_idle,
        }

    def _rollback_env_get(self, key):
        env = getattr(self, "_rollback_env", None) or {}
        return env.get(key)

    def rollback_preview(self, device_id: str, payload: dict,
                         principal_id: str = "operator") -> dict:
        kind = payload.get("kind") or payload.get("resource") or "adaptation"
        if kind in ("layout_adaptation", "adaptation"):
            if self._state is not None:
                # R3-16：typed 世界走新四态核验回滚（正式路由）
                return self._adaptation_rollback_preview(device_id, payload,
                                                         principal_id)
            # 无 typed store 的旧装配：无会话/catalog 基础设施 → legacy KV 链
            return self._legacy_rollback_preview(device_id, payload)
        if kind not in ("region", "phone_search_region"):
            raise LayoutOpsError(422, "layout_request_invalid",
                                 f"kind={kind} 不可回滚"
                                 "（只允许 layout_adaptation|phone_search_region）")
        return self._legacy_rollback_preview(device_id, payload)

    def _legacy_rollback_preview(self, device_id: str, payload: dict) -> dict:
        resource = payload.get("resource") or "adaptation"
        if resource not in ("adaptation", "region"):
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "resource 必须为 adaptation|region")
        target_version = payload.get("target_version")
        if not isinstance(target_version, int) or target_version < 1:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "target_version 必须是正整数")
        target = self._load_resource(
            resource, device_id,
            device_id if resource == "region" else None, target_version) \
            if resource == "region" else self.resolve_adaptation(
                device_id, target_version)
        if target is None:
            raise LayoutOpsError(404, "layout_resource_missing",
                                 f"{resource} 版本 {target_version} 不存在")
        current = {"revision": 0, "version": None}
        if resource == "adaptation" and self._state is not None:
            pointer = self._state.adaptation_pointer_get(device_id)
            if pointer is not None:
                current = {"revision": pointer["revision"],
                           "version": pointer["version"]}
        else:
            current = self._store.load(
                f"layout_pointer:{resource}:{device_id}") or current
        return {"device_id": device_id, "resource": resource,
                "current_version": current.get("version"),
                "current_revision": current.get("revision"),
                "target_version": target_version,
                "target_sha256": target.get("sha256"),
                "status": "preview",
                "note": "回滚只前进指针修订号，不复活旧动作；提交后须人工复核当前适用性"}

    def _adaptation_rollback_preview(self, device_id: str, payload: dict,
                                     principal_id: str) -> dict:
        """适配回滚预览（§8）：用新四态会话核验目标历史版本；取当前新鲜帧；
        目标模板/安装范围/手机/校准一致；120 秒一次消费。"""
        if self._state is None:
            raise LayoutOpsError(503, "layout_storage_failed",
                                 "适配回滚需要持久状态库")
        if self._state.blocked_reasons:
            raise LayoutOpsError(503, "layout_archive_invalid",
                                 f"存储完整性未恢复："
                                 f"{self._state.blocked_reasons}；拒绝回滚")
        target_version = payload.get("target_version")
        if not isinstance(target_version, int) or target_version < 1:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "target_version 必须是正整数")
        target = self.resolve_adaptation(device_id, target_version)
        if target is None:
            raise LayoutOpsError(404, "layout_resource_missing",
                                 f"适配版本 {target_version} 不存在")
        pointer = self._state.adaptation_pointer_get(device_id) \
            or {"revision": 0, "version": None}
        expected = payload.get("expected_revision")
        if expected is None or \
                int(expected) != int(pointer.get("revision") or 0):
            raise LayoutOpsError(
                409, "layout_revision_conflict",
                f"指针修订冲突（当前 {pointer.get('revision')}，期望 "
                f"{expected}）")
        if pointer.get("version") == target_version:
            raise LayoutOpsError(409, "layout_request_conflict",
                                 "目标版本即当前生效版本，无需回滚")
        # ① 新四态核验会话（必需）：无新会话拒绝（不以历史证据冒充核验）
        session_id = payload.get("rollback_session_id")
        loader = self._rollback_env_get("session_loader")
        if not session_id or not callable(loader):
            raise LayoutOpsError(
                409, "layout_items_unverified",
                "适配回滚必须使用新四态会话核验目标历史版本"
                "（缺少 rollback_session_id 或会话事实源未接入）")
        session = loader(str(session_id))
        if session is None or session.get("device_id") != device_id:
            raise LayoutOpsError(404, "layout_resource_missing",
                                 "回滚核验会话不存在或不属于该设备")
        if session.get("principal_id") != principal_id:
            raise LayoutOpsError(403, "layout_permission_denied",
                                 "回滚核验会话属于其他身份")
        expires_at = session.get("expires_at")
        if expires_at and self._now() >= parse_iso(expires_at):
            raise LayoutOpsError(409, "layout_request_conflict",
                                 "回滚核验会话已过期；请重新四态采集")
        # ② 会话与目标一致：模板
        template_ref = target.get("template_ref") or {}
        if session.get("template_id") != template_ref.get("id") \
                or session.get("template_version") \
                != template_ref.get("version"):
            raise LayoutOpsError(409, "layout_request_conflict",
                                 "核验会话模板与目标版本不一致")
        # ③ 安装范围一致（区域已变化须重新框选，不能用回滚绕过）
        region_ref = target.get("installation_region_ref") or {}
        if self._region_store is not None and region_ref.get("version"):
            pointer_row = self._region_store.get_pointer(device_id)
            current_region_version = int(pointer_row["selected_version"]) \
                if pointer_row else None
            if current_region_version != region_ref.get("version"):
                raise LayoutOpsError(
                    409, "layout_binding_changed",
                    "本台安装范围已变化；须重新框选/复核，不能用回滚绕过")
        # ④ 手机/校准一致（校准变化拒绝）
        binding_provider = self._rollback_env_get("binding_provider")
        if callable(binding_provider):
            facts = binding_provider(device_id) or {}
            target_screen = target.get("screen_descriptor") or {}
            current_screen = facts.get("screen_descriptor") or {}
            target_hash = target_screen.get("calibration_geometry_hash")
            current_hash = current_screen.get("calibration_geometry_hash")
            if target_hash and current_hash and target_hash != current_hash:
                raise LayoutOpsError(409, "layout_binding_changed",
                                     "本台校准几何已变化；须重新四态适配")
            if facts.get("ime_device_id") \
                    and target.get("ime_device_id") \
                    and facts["ime_device_id"] != target["ime_device_id"]:
                raise LayoutOpsError(409, "layout_binding_changed",
                                     "本台手机绑定已变化；不能回滚到旧手机适配")
        calibration_provider = self._rollback_env_get("calibration_provider")
        if callable(calibration_provider):
            bundle = calibration_provider(device_id)
            if not isinstance(bundle, dict) \
                    or int(bundle.get("schemaVersion") or 0) != 3:
                raise LayoutOpsError(409, "layout_items_unverified",
                                     "适配回滚要求本台有效 v3 校准")
        # ⑤ 会话四页全部通过且证据新鲜（≤900 秒）
        pages = session.get("pages") or {}
        session_summary = {}
        now = self._now()
        for state in self._FOUR_STATES:
            page = pages.get(state)
            capture = (page or {}).get("capture") or {}
            check = capture.get("check") or {}
            if page is None or check.get("status") != "passed":
                raise LayoutOpsError(
                    409, "layout_items_unverified",
                    f"回滚核验会话 {state} 页未通过；不能核验目标版本")
            accepted_at = capture.get("accepted_at")
            age = (now - parse_iso(accepted_at)).total_seconds() \
                if accepted_at else float("inf")
            if age > 900:
                raise LayoutOpsError(
                    409, "layout_items_unverified",
                    f"回滚核验会话 {state} 页证据已过期（>900 秒）；"
                    "须重新采集")
            session_summary[state] = {
                "evidence_ids": list(capture.get("evidence_ids") or []),
                "accepted_at": accepted_at}
        # ⑥ 当前新鲜帧核验（来源一致 + 年龄 ≤1000ms）
        capture_provider = self._rollback_env_get("capture_provider")
        if not callable(capture_provider):
            raise LayoutOpsError(503, "layout_source_unavailable",
                                 "适配回滚预览需要当前新鲜帧核验"
                                 "（未接入采集事实源）")
        frames = capture_provider(device_id, 1, None)
        if not frames:
            raise LayoutOpsError(503, "layout_source_unavailable",
                                 "预览未取得当前帧")
        frame = frames[-1]
        age_ms = frame.get("received_monotonic_ms", 0) \
            - frame.get("source_captured_monotonic_ns", 0) / 1e6
        if age_ms > 1000:
            raise LayoutOpsError(409, "layout_items_unverified",
                                 "当前帧不新鲜，不能生成回滚预览")
        snapshot = session.get("installation_binding") or {}
        current = frame.get("installation_binding") or {}
        for key in ("camera_binding_id", "arm_binding_id"):
            if snapshot.get(key) and current.get(key) \
                    and snapshot[key] != current[key]:
                raise LayoutOpsError(409, "layout_source_binding_changed",
                                     f"当前来源 {key} 与核验会话不一致")
        current_evidence_id = frame.get("evidence_id")
        # ⑦ 一次消费预览行（typed rollback_preview）
        from runtime.layout_region import canonical_digest
        preview_id = str(uuid.uuid4())
        target_ref = {"id": device_id, "version": target_version,
                      "sha256": target.get("sha256")
                      or canonical_digest(target)}
        record = {
            "preview_id": preview_id,
            "device_id": device_id,
            "principal_id": principal_id,
            "kind": "layout_adaptation",
            "target_ref": target_ref,
            "expected_revision": int(pointer.get("revision") or 0),
            "rollback_session_id": str(session_id),
            "rollback_session": session_summary,
            "current_evidence_id": current_evidence_id,
            "status": "verified",
            "reason_codes": [],
            "request_hash": content_sha(
                {k: v for k, v in payload.items()
                 if k not in ("correlation_id",)}),
            "created_at": iso(now),
            "expires_at": iso(now + timedelta(
                seconds=self.ROLLBACK_TTL_SECONDS)),
            "consumed_by": None,
            "server_instance_id": "ops",
        }
        self._state.state_insert(
            "rollback_preview",
            state_key("rollback_preview", state_identity("rollback_preview",
                                                          record)),
            device_id, principal_id, record)
        return {"preview_id": preview_id, "kind": "layout_adaptation",
                "target_ref": target_ref,
                "expected_revision": record["expected_revision"],
                "status": "verified", "reason_codes": [],
                "rollback_session": session_summary,
                "current_evidence_id": current_evidence_id,
                "created_at": record["created_at"],
                "expires_at": record["expires_at"]}

    def rollback_commit(self, device_id: str, payload: dict,
                        principal_id: str = "operator",
                        client_request_id: str | None = None) -> dict:
        kind = payload.get("kind") or payload.get("resource") or "adaptation"
        if kind in ("layout_adaptation", "adaptation") \
                and self._state is not None:
            return self._adaptation_rollback_commit(
                device_id, payload, principal_id, client_request_id)
        return self._legacy_rollback_commit(device_id, payload,
                                            principal_id, client_request_id)

    def _legacy_rollback_commit(self, device_id: str, payload: dict,
                                principal_id: str,
                                client_request_id: str | None) -> dict:
        preview = self._legacy_rollback_preview(device_id, payload)
        expected = payload.get("expected_revision")
        if expected is not None and expected != preview["current_revision"]:
            raise LayoutOpsError(409, "layout_revision_conflict",
                                 f"指针修订冲突（当前 {preview['current_revision']}，"
                                 f"期望 {expected}）")
        resource = preview["resource"]
        if resource == "adaptation" and self._state is not None:
            pointer = self._state.adaptation_pointer_cas(
                device_id, int(preview["current_revision"] or 0),
                preview["target_version"], preview["target_sha256"],
                client_request_id or uuid.uuid4().hex)
            revision = pointer["revision"]
        else:
            pointer = {"revision": preview["current_revision"] + 1,
                       "version": preview["target_version"],
                       "sha256": preview["target_sha256"],
                       "rolled_back_at": iso(utc_now()),
                       "rolled_back_by": principal_id}
            self._store.save(f"layout_pointer:{resource}:{device_id}", pointer)
            revision = pointer["revision"]
        return {"device_id": device_id, "resource": resource,
                "rolled_back_to": preview["target_version"],
                "revision": revision, "status": "committed",
                "pointers_changed": True}

    def _adaptation_rollback_commit(self, device_id: str, payload: dict,
                                    principal_id: str,
                                    client_request_id: str | None) -> dict:
        """适配回滚提交（§8）：同号幂等 → 一次消费预览 → 复核事实+台账 →
        仅推进 current revision（旧版本字节不改，不重新发布）。"""
        if self._state is None:
            raise LayoutOpsError(503, "layout_storage_failed",
                                 "适配回滚需要持久状态库")
        if not client_request_id:
            raise LayoutOpsError(422, "layout_request_invalid",
                                 "适配回滚提交必须携带 client_request_id")
        # ① 幂等重放：同号同身份返回原响应；未完成提交引导对账
        prior = self._state.operation_get_by_request(client_request_id)
        if prior is not None:
            if prior.get("device_id") != device_id \
                    or prior.get("principal_id") != principal_id:
                raise LayoutOpsError(403, "layout_permission_denied",
                                     "client_request_id 已被其他身份/设备使用")
            if prior.get("response") is None:
                raise LayoutOpsError(409, "layout_request_conflict",
                                     "同编号操作未完成提交；请按 operation 查询")
            return {**prior["response"], "replayed": True}
        if self._state.blocked_reasons:
            raise LayoutOpsError(503, "layout_archive_invalid",
                                 f"存储完整性未恢复："
                                 f"{self._state.blocked_reasons}；拒绝回滚")
        # ② 一次消费预览核验
        preview_id = str(payload.get("preview_id") or "")
        record = self._state.state_get(
            "rollback_preview", state_key("rollback_preview", [preview_id]))
        if record is None or record.get("device_id") != device_id \
                or record.get("principal_id") != principal_id:
            raise LayoutOpsError(404, "layout_resource_missing",
                                 "回滚预览不存在或不属于该身份/设备")
        if record.get("consumed_by") is not None:
            raise LayoutOpsError(409, "layout_request_conflict",
                                 "回滚预览已消费")
        if self._now() >= parse_iso(record["expires_at"]):
            raise LayoutOpsError(409, "layout_items_unverified",
                                 "回滚预览已超过 120 秒有效期；请重新预览")
        # ③ 设备空闲（忙拒绝；无空闲事实源按未知拒绝）
        idle = self._rollback_env_get("device_idle")
        if not callable(idle) or not idle(device_id):
            raise LayoutOpsError(409, "layout_device_busy",
                                 "适配回滚要求设备空闲（空闲事实不可用或设备忙）")
        # ④ 指针仍等于预览核验时的 revision
        pointer = self._state.adaptation_pointer_get(device_id) \
            or {"revision": 0, "version": None}
        if int(pointer.get("revision") or 0) != int(record["expected_revision"]):
            raise LayoutOpsError(
                409, "layout_revision_conflict",
                f"指针修订冲突（当前 {pointer.get('revision')}，期望 "
                f"{record['expected_revision']}）")
        operation_id = str(uuid.uuid4())
        review_id = str(uuid.uuid4())
        target_ref = record["target_ref"]
        consume_key = state_key("rollback_preview", [preview_id])
        # ⑤ 台账 prepared（指针 before/after + 预览消费计划）
        self._state.operation_prepare(
            principal_id=principal_id, device_id=device_id,
            resource_kind="adaptation", resource_id=device_id,
            client_request_id=client_request_id,
            request={k: v for k, v in payload.items()
                     if k not in ("correlation_id",)},
            planned_response={"rolled_back_to": target_ref["version"],
                              "review_id": review_id,
                              "operation_id": operation_id},
            files=[],
            pointer_before={"revision": int(pointer.get("revision") or 0),
                            "version": pointer.get("version"),
                            "sha256": pointer.get("sha256")},
            pointer_after={"revision": int(pointer.get("revision") or 0) + 1,
                           "version": target_ref["version"],
                           "sha256": target_ref["sha256"]},
            mutations=[],
            version_ref=None,
            consume=[{"kind": "rollback_preview", "state_key": consume_key,
                      "client_request_id": client_request_id}],
            operation_id=operation_id)
        # ⑥ 新复核事实（review 记录；不覆写旧记录/历史版本）
        self._state.review_record_save(review_id, device_id, {
            "review_id": review_id, "kind": "adaptation_rollback",
            "device_id": device_id, "operation_id": operation_id,
            "target_ref": target_ref,
            "from_version": pointer.get("version"),
            "from_revision": record["expected_revision"],
            "rollback_session_id": record.get("rollback_session_id"),
            "rollback_session": record.get("rollback_session"),
            "current_evidence_id": record.get("current_evidence_id"),
            "verified_at": iso(self._now()),
            "note": "适配回滚：新四态会话核验目标历史版本后仅推进 "
                    "current revision；旧版本字节/摘要/时间不改，"
                    "不以重新发布冒充回滚"})
        # ⑦ 指针 CAS（只前进修订；selected 指回目标历史版本）
        try:
            pointer_after = self._state.adaptation_pointer_cas(
                device_id, int(record["expected_revision"]),
                int(target_ref["version"]), target_ref["sha256"],
                operation_id)
        except LayoutStateError as exc:
            self._state.operation_abort(operation_id, {
                "code": "layout_revision_conflict",
                "message": getattr(exc, "message", "指针切换失败")})
            raise LayoutOpsError(getattr(exc, "status", 409),
                                 getattr(exc, "code",
                                         "layout_revision_conflict"),
                                 getattr(exc, "message",
                                         str(exc))) from exc
        response = {"device_id": device_id, "kind": "layout_adaptation",
                    "operation_id": operation_id, "review_id": review_id,
                    "rolled_back_to": int(target_ref["version"]),
                    "pointer": pointer_after,
                    "revision": pointer_after["revision"],
                    "status": "committed", "pointers_changed": True,
                    "replayed": False}
        # ⑧ 台账 committed + 预览一次消费（同一 Store 事务）
        self._state.operation_commit(
            operation_id, response=response,
            after_revision=pointer_after["revision"],
            consume=[("rollback_preview", consume_key, client_request_id)])
        return response


def _image_size(data: bytes) -> tuple[int, int]:
    """真实解码读取宽高（非扩展名推断）；失败即拒绝。"""
    import cv2
    import numpy as np
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise LayoutOpsError(422, "layout_request_invalid",
                             "资产不是可解码 PNG/JPEG 图像")
    height, width = image.shape[:2]
    return width, height
