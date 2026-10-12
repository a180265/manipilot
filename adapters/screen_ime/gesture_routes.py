"""手势采样协议路由（规范第 5.2 节路由表）。

挂在 8788 的 /api/v1/* 下，与旧 screen_ime 路由并存；错误外壳
沿用 {"error","message"}。不向手机暴露任何可请求机械动作的端点。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from adapters.screen_ime.gesture_capture import GestureCaptureCoordinator
from adapters.screen_ime.routes import _json_body
from adapters.screen_ime.service import (
    MAX_BODY_BYTES, ScreenImeApiError, ScreenImeService, get_default_service,
)


def _coordinator(service: ScreenImeService | None) -> GestureCaptureCoordinator:
    svc = service or get_default_service()
    coordinator = getattr(svc, "gesture_capture", None)
    if not isinstance(coordinator, GestureCaptureCoordinator):
        raise ScreenImeApiError(503, "gesture_capture_unavailable",
                                "手势采样协调器未挂载")
    return coordinator


_active_coordinator = None  # 模块级：HTTP 挂载时设置，进程内所有调用方共用


def get_active_coordinator():
    """进程内唯一协调器引用（由 create_gesture_router 挂载时设置）。"""
    return _active_coordinator


def create_gesture_router(service: ScreenImeService | None = None,
                          phone_identity_provider=None) -> APIRouter:
    """gesture-capture.v1 手机采样路由。

    phone_identity_provider（W4-001/D 接口请求①）：与 screen_ime 路由同款
    token 门——手机侧 5 端点（gesture-client/gesture-command/ready/trace/
    page-event）必须携有效 x-ime-phone-token；设备路径端点同时核凭据与
    路径设备一致。None=独立部署保持原合同（与 screen_ime 路由语义相同）。
    """
    global _active_coordinator
    router = APIRouter()
    svc = service or get_default_service()
    _active_coordinator = getattr(svc, "gesture_capture", None)
    if isinstance(_active_coordinator, GestureCaptureCoordinator):
        # 使用已打开的服务数据库路径，保证临时测试/多实例不会写进现场目录。
        database = svc.tasks._query_one("PRAGMA database_list")
        if database and database["file"]:
            _active_coordinator.configure_metadata_storage(Path(database["file"]).parent)

    def require_phone(request: Request, path_device: str | None = None) -> dict:
        if phone_identity_provider is None:
            return {}
        token = request.headers.get("x-ime-phone-token")
        identity = phone_identity_provider(token) if token else None
        if identity is None:
            raise ScreenImeApiError(
                401, "authentication_required",
                "缺少或无效手机凭据（x-ime-phone-token）：旧输入法需升级配对")
        if identity.get("state") not in ("active", "pending"):
            raise ScreenImeApiError(401, "authentication_required",
                                    "手机凭据已失效（需重新配对）")
        if path_device is not None \
                and identity.get("ime_device_id") != path_device:
            raise ScreenImeApiError(403, "phone_scope_denied",
                                    "凭据与路径设备不一致（A 凭据不能用 B 路径）")
        return identity

    @router.get("/api/v1/gesture-protocol")
    async def gesture_protocol():
        return {"contractVersion": "gesture-capture.v1", "supported": True}

    @router.get("/api/v1/gesture-protocol/legacy-metadata")
    async def legacy_metadata(request: Request):
        """旧校准页元数据查询（规范第 4.2/22.4 节，只读）。

        向导完成最后独立验证时按 session/device 核对同一次原校准的页面
        元数据；缺失返回 404 legacy_metadata_not_found，不影响原校准。
        """
        device_id = request.query_params.get("deviceId") or ""
        session_id = request.query_params.get("sessionId") or ""
        found = _coordinator(service).legacy_metadata(device_id, session_id)
        if found is None:
            raise ScreenImeApiError(404, "legacy_metadata_not_found",
                                    "旧校准页元数据不存在或会话不匹配")
        return {"deviceId": device_id, "sessionId": found["sessionId"],
                "clientInstanceId": found["clientInstanceId"],
                "page": found["page"]}

    @router.post("/api/v1/devices/{ime_device_id}/gesture-client")
    async def register_client(ime_device_id: str, request: Request):
        require_phone(request, path_device=ime_device_id)
        body = await _json_body(request)
        return await _coordinator(service).register_client(ime_device_id, body)

    @router.get("/api/v1/devices/{ime_device_id}/gesture-command")
    async def next_command(ime_device_id: str, request: Request):
        require_phone(request, path_device=ime_device_id)
        client_instance_id = request.query_params.get("clientInstanceId")
        coordinator = _coordinator(service)
        command = await coordinator.next_command(
            ime_device_id, client_instance_id or "")
        if command is None:
            return Response(status_code=204)
        return command

    @router.post("/api/v1/gesture-sessions/{session_id}/ready")
    async def accept_ready(session_id: str, request: Request):
        require_phone(request)
        body = await _json_body(request)
        return await _coordinator(service).accept_ready(session_id, body)

    @router.post("/api/v1/gesture-sessions/{session_id}/trace")
    async def accept_trace(session_id: str, request: Request):
        require_phone(request)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise ScreenImeApiError(413, "body_too_large",
                                    "Request body is too large")
        body = await _json_body(request)
        receipt, duplicate = await _coordinator(service).accept_trace(session_id, body)
        return JSONResponse(status_code=200 if duplicate else 201, content=receipt)

    @router.post("/api/v1/gesture-sessions/{session_id}/page-event")
    async def accept_page_event(session_id: str, request: Request):
        require_phone(request)
        body = await _json_body(request)
        return await _coordinator(service).accept_page_event(session_id, body)

    @router.get("/api/v1/gesture-sessions/{session_id}/receipts/{trace_id}")
    async def lookup_receipt(session_id: str, trace_id: str):
        found = await _coordinator(service).receipt(session_id, trace_id)
        if found is None:
            raise ScreenImeApiError(404, "receipt_not_found", "轨迹回执不存在")
        return found

    return router
