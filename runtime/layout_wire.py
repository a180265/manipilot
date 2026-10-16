"""布局 38 路由的冻结 wire 外壳（R2-17/R2-23）。

依据 docs/specs/layout-reuse/{routes,wire,api,common}.schema.json 与
contract-completion.md §2/§3：
- 成功响应：JSON + `Cache-Control: no-store` + `X-Correlation-Id`（UUID）+
  `X-Layout-Contract-Version`（路由版本）；body 首层含 contract_version 与
  correlation_id；二进制为原字节（Content-Length=真实字节数、ETag=带双引号的
  字节 SHA-256、图片 inline、ZIP attachment）。
- 错误响应：`layout-error.v1` 外壳
  {contract_version, correlation_id, operation_id:null, code, message,
   motion_started:false, retryable:false}；message 必须是中文业务说明；
  无法解析关联 ID 时由服务器生成响应 ID。
- 请求：GET 的两个公共头必填；POST/PUT 的 contract_version/correlation_id 在
  JSON body，同值头可同时给，不同值 422；application/json 必须。
- 结构校验：调用冻结离线校验器（docs/specs/layout-reuse/validate.py 的
  Registry/StrictValidator，引用只在本地 Registry 解析），不复制第二套 schema。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

# ---- 冻结错误闭集（api.schema.json#/definitions/error.code）与状态映射 ----
ERROR_STATUS = {
    "layout_request_invalid": 422,
    "layout_permission_denied": 403,
    "layout_resource_missing": 404,
    "layout_revision_conflict": 409,
    "layout_request_conflict": 409,
    "layout_binding_changed": 409,
    "layout_device_busy": 409,
    "layout_preview_expired": 409,
    "layout_items_unverified": 409,
    "layout_source_unavailable": 503,
    "layout_archive_invalid": 503,
    "layout_storage_failed": 503,
}
ERROR_VERSION = "layout-error.v1"

# 服务层历史错误键 → 冻结闭集（改键不改语义；状态按冻结映射）
CODE_ALIASES = {
    # AdaptationError / RegionError / OpsError 历史键
    "invalid_request": "layout_request_invalid",
    "request_invalid": "layout_request_invalid",
    "camera_request_invalid": "layout_request_invalid",
    "missing_field": "layout_request_invalid",
    "unsupported_contract": "layout_request_invalid",
    "permission_denied": "layout_permission_denied",
    "author_missing": "layout_permission_denied",
    "author_mismatch": "layout_permission_denied",
    "device_mismatch": "layout_permission_denied",
    "identity_unavailable": "layout_permission_denied",
    "template_not_found": "layout_resource_missing",
    "preview_not_found": "layout_resource_missing",
    "session_not_found": "layout_resource_missing",
    "camera_not_found": "layout_resource_missing",
    "not_found": "layout_resource_missing",
    "revision_conflict": "layout_revision_conflict",
    "request_conflict": "layout_request_conflict",
    "preview_consumed": "layout_request_conflict",
    "accepted_items_mismatch": "layout_request_conflict",
    "template_mismatch": "layout_request_conflict",
    "binding_changed": "layout_binding_changed",
    "source_binding_changed": "layout_binding_changed",
    "device_busy": "layout_device_busy",
    "session_expired": "layout_preview_expired",
    "preview_expired": "layout_preview_expired",
    "capture_expired": "layout_items_unverified",
    "capture_expired": "layout_items_unverified",
    "evidence_expired": "layout_items_unverified",
    "pages_incomplete": "layout_items_unverified",
    "items_unverified": "layout_items_unverified",
    "source_unavailable": "layout_source_unavailable",
    "service_closed": "layout_storage_failed",
    "storage_failed": "layout_storage_failed",
    "archive_invalid": "layout_archive_invalid",
}

# 路由 contract_version（routes.json 权威值的只读镜像；结构以 routes.json 为准）
ROUTE_CONTRACT_VERSIONS = {
    "region_delete": "layout-region-delete.v1",
    "region_get": "phone-search-region-read.v1",
    "region_preview": "phone-search-region-preview.v1",
    "region_save": "phone-search-region-write.v1",
    "region_version": "phone-search-region-read.v1",
    "template_list": "ui-layout-list.v1",
    "template_version": "ui-layout-template-read.v1",
    "template_asset": "ui-layout-template-read.v1",
    "template_publish": "ui-layout-edit.v1",
    "draft_create": "ui-layout-edit.v1",
    "draft_get": "ui-layout-edit.v1",
    "draft_metadata": "ui-layout-edit.v1",
    "draft_page": "ui-layout-edit.v1",
    "draft_asset_upload": "ui-layout-edit.v1",
    "draft_asset_get": "ui-layout-edit.v1",
    "capture": "layout-capture.v1",
    "evidence_get": "layout-evidence.v1",
    "session_create": "device-layout-edit.v1",
    "session_get": "device-layout-edit.v1",
    "session_page": "device-layout-edit.v1",
    "adaptation_preview": "device-layout-edit.v1",
    "adaptation_publish": "device-layout-edit.v1",
    "adaptation_get": "device-layout-read.v1",
    "adaptation_version": "device-layout-read.v1",
    "adaptation_asset": "device-layout-read.v1",
    "issue_list": "layout-review.v1",
    "review": "layout-review.v1",
    "operation_request": "layout-operation.v1",
    "operation_get": "layout-operation.v1",
    "package_export": "layout-package.v1",
    "package_get": "layout-package.v1",
    "package_download": "layout-package.v1",
    "package_import_preview": "layout-package.v1",
    "package_import": "layout-package.v1",
    "rollback_preview": "layout-rollback.v1",
    "rollback": "layout-rollback.v1",
}


class LayoutHttpError(Exception):
    """布局路由的冻结错误（由 app 的异常处理器渲染为 layout-error.v1）。"""

    def __init__(self, code: str, message: str, status: int | None = None,
                 operation_id: str | None = None):
        frozen = CODE_ALIASES.get(code, code)
        if frozen not in ERROR_STATUS:
            frozen = "layout_request_invalid"
        self.code = frozen
        self.status = status or ERROR_STATUS[frozen]
        self.message = message
        self.operation_id = operation_id
        super().__init__(f"{frozen}: {message}")


def freeze_code(code: str) -> str:
    return CODE_ALIASES.get(code, code)


def freeze_status(code: str) -> int:
    return ERROR_STATUS[freeze_code(code)]


def error_body(correlation_id: str, code: str, message: str,
               operation_id: str | None = None) -> dict:
    return {
        "contract_version": ERROR_VERSION,
        "correlation_id": correlation_id,
        "operation_id": operation_id,
        "code": freeze_code(code),
        "message": message,
        "motion_started": False,
        "retryable": False,
    }


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def json_response(payload: dict, route_id: str, correlation_id: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=200,
        content=payload,
        headers={
            "Cache-Control": "no-store",
            "X-Correlation-Id": correlation_id,
            "X-Layout-Contract-Version": ROUTE_CONTRACT_VERSIONS[route_id],
        })


def error_response(correlation_id: str, code: str, message: str,
                   status: int | None = None):
    from fastapi.responses import JSONResponse
    frozen = freeze_code(code)
    if frozen not in ERROR_STATUS:
        frozen = "layout_request_invalid"
    if status is not None and frozen == "layout_request_invalid" \
            and code not in CODE_ALIASES:
        # 未登记的历史键：状态仍按冻结闭集（403/404/409/422/503 已知映射）
        status = None
    return JSONResponse(
        status_code=status or ERROR_STATUS[frozen],
        content=error_body(correlation_id, frozen, message),
        headers={
            "Cache-Control": "no-store",
            "X-Correlation-Id": correlation_id,
            "X-Layout-Contract-Version": ERROR_VERSION,
        })


def binary_response(data: bytes, media_type: str, *, zip_download: bool):
    from fastapi.responses import Response
    disposition = ("attachment; filename=layout-package.zip"
                   if zip_download else "inline")
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Length": str(len(data)),
            "ETag": f'"{hashlib.sha256(data).hexdigest()}"',
            "Cache-Control": "no-store",
            "Content-Disposition": disposition,
        })


# ---------------- 请求解析（公共头/body 关联字段 + 冻结结构校验） ----------------
_UUID_RE = uuid.UUID


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _UUID_RE(value)
        return True
    except (ValueError, AttributeError):
        return False


_FROZEN_DIR = None


def _frozen_dir():
    global _FROZEN_DIR
    if _FROZEN_DIR is None:
        from pathlib import Path
        _FROZEN_DIR = (Path(__file__).resolve().parents[1] / "docs" /
                       "specs" / "layout-reuse")
    return _FROZEN_DIR


_REGISTRY_CACHE: dict | None = None


def _frozen_registry():
    """冻结 schema 家族的本地 Registry（与离线校验器同源；引用只在本地解析）。

    与 docs/specs/layout-reuse/validate.py 相同的注册方式：每个 schema 文件
    按其自身 $id 注册进 referencing.Registry（不联网；retrieve 拒绝远程）。
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    import json
    from referencing import Registry, Resource
    from jsonschema import FormatChecker

    base = _frozen_dir()
    files = ["common.schema.json", "phone-search-region.schema.json",
             "layout.schema.json", "api.schema.json", "package.schema.json",
             "records.schema.json", "ime-geometry.schema.json",
             "ui-layout-template.v1.schema.json",
             "device-layout-adaptation.v1.schema.json", "wire.schema.json"]

    def _no_remote(uri: str):
        raise RuntimeError(f"拒绝远程 schema 引用：{uri}")

    registry: Registry = Registry(retrieve=_no_remote)
    wire = None
    for filename in files:
        schema = json.loads((base / filename).read_text(encoding="utf-8"))
        if filename == "wire.schema.json":
            wire = schema
        registry = registry.with_resources(
            [(schema["$id"], Resource.from_contents(schema))])
    if wire is None:
        raise RuntimeError("wire.schema.json 缺失（冻结契约不完整）")
    cache = {"registry": registry, "wire": wire, "checker": FormatChecker()}
    _REGISTRY_CACHE = cache
    return cache


def validate_request_view(route_id: str, view: dict) -> None:
    """把实际 HTTP 请求装配成 wire:<route_id>_request 视图并按冻结 schema 校验。

    view = {route_id, method, path, headers, query, body}（与冻结 wire 视图
    同构）。失败抛 LayoutHttpError(422, 中文说明)。校验器是冻结 wire.schema
    （Draft07、封闭字段集），本模块不复制第二套判定。
    """
    from jsonschema import Draft7Validator
    cache = _frozen_registry()
    definition = cache["wire"]["definitions"].get(route_id + "_request")
    if definition is None:
        raise LayoutHttpError("layout_request_invalid",
                              f"路由 {route_id} 不在冻结契约内")
    validator = Draft7Validator(definition, registry=cache["registry"],
                                format_checker=cache["checker"])
    errors = sorted(validator.iter_errors(view), key=lambda e: str(
        list(e.absolute_path)))
    if errors:
        error = errors[0]
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        raise LayoutHttpError("layout_request_invalid",
                              f"请求不符合冻结契约（{where}）：{error.message}")


def validate_response_view(route_id: str, status: int, view: dict) -> None:
    """成功/错误响应外壳按冻结 wire schema 自检（契约安装点，失败即缺陷）。

    view = {route_id, status, headers, body}。
    """
    from jsonschema import Draft7Validator
    cache = _frozen_registry()
    suffix = "_response" if status == 200 else "_error"
    definition = cache["wire"]["definitions"].get(route_id + suffix)
    if definition is None:
        raise LayoutHttpError("layout_request_invalid",
                              f"路由 {route_id} 不在冻结契约内")
    validator = Draft7Validator(definition, registry=cache["registry"],
                                format_checker=cache["checker"])
    errors = sorted(validator.iter_errors(view), key=lambda e: str(
        list(e.absolute_path)))
    if errors:
        error = errors[0]
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        raise LayoutHttpError("layout_archive_invalid",
                              f"响应不符合冻结契约（{where}）：{error.message}",
                              status=503)
