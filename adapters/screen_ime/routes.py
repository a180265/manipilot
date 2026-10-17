"""屏幕输入法协议路由：与 K40TaskServer /api/v1/* 路径和字段完全一致。

设计边界：
- 这里是"手机输入法 ↔ 服务端"的设备侧协议，与平台 /v1/* 契约互相独立；
  错误格式 {"error","message"} 与 K40TaskServer 一致，客户端零改动迁移。
- 长轮询端点在事件循环内异步等待，不阻塞其他请求。
- R3-17-P4：Fleet 挂载（phone_identity_provider 注入）时按身份附件 P4 分类
  执行——手机类别路由必须携有效 x-ime-phone-token 且与路径设备一致；
  管理类别路由（任务创建/校准 arm/disarm/complete、screen-mappings、相机
  管理口）只接受本机回环机器调用，不作为 LAN 匿名后门。未注入 provider=
  独立部署（非 LAN 拓扑）保持原合同。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRouter

from adapters.screen_ime.service import (
    MAX_BODY_BYTES,
    ScreenImeApiError,
    ScreenImeService,
    get_default_service,
)

_PHONE_PAGE = Path(__file__).resolve().parent / "phone.html"


def _clamp_int(value: str | None, default: int, minimum: int, maximum: int, field: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ScreenImeApiError(400, f"invalid_{field}", f"{field} is out of range")
    if parsed < minimum or parsed > maximum:
        raise ScreenImeApiError(400, f"invalid_{field}", f"{field} is out of range")
    return parsed


def _iso_or_none(value: str | None, field: str) -> str | None:
    if value is None or not value.strip():
        return None
    from adapters.screen_ime.service import parse_iso

    try:
        parse_iso(value.strip())
    except ValueError:
        raise ScreenImeApiError(400, f"invalid_{field}", f"{field} must be an ISO-8601 instant")
    return value.strip()


async def _json_body(request: Request) -> dict:
    """读取 JSON 请求体；Content-Type 与大小限制与 Java 版一致。"""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        raise ScreenImeApiError(415, "unsupported_media_type",
                                "Content-Type must be application/json")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ScreenImeApiError(413, "body_too_large", "Request body is too large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ScreenImeApiError(400, "invalid_json", "Request body is not valid JSON")


def create_screen_ime_router(service: ScreenImeService | None = None,
                             frame_source=None,
                             geometry_provider=None,
                             phone_identity_provider=None,
                             pairing_provider=None) -> APIRouter:
    """geometry_provider(request)→ImeGeometryService：Fleet 挂载时注入当前
    Runtime 持有的几何实例；缺省回退进程级共享单例（独立部署原合同）。

    phone_identity_provider(token)→dict|None：R3-17-P4 手机身份 Provider
    （配对/登记凭据 → {ime_device_id, state,...}）。注入后手机类别路由强制
    携证；None=独立部署保持原合同。pairing_provider：ime-pairing.v1 服务。"""
    router = APIRouter()
    get_service = lambda: service or get_default_service()

    # ---- R3-17-P4：手机/机器两类入口身份边界 ----
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

    def require_active_phone(request: Request, path_device: str | None = None) -> dict:
        identity = require_phone(request, path_device)
        if identity and identity.get("state") != "active":
            raise ScreenImeApiError(
                403, "phone_scope_denied",
                "pending 凭据不能领取任务或回报；请先完成配对激活")
        return identity

    def require_machine(request: Request) -> None:
        """管理类别路由：Fleet 挂载时只接受本机回环机器调用（N1：管理口不
        映射 LAN）；手机/局域网请求一律拒绝。"""
        if phone_identity_provider is None:
            return
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "testclient"):  # testclient=TestClient 回环语义
            raise ScreenImeApiError(
                403, "phone_scope_denied",
                "管理入口仅限本机回环调用（手机 LAN 入口不开放管理操作）")

    def _pairing_error_response(exc) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={
            "contract_version": "ime-pairing.v1",
            "request_id": None,
            "error": {"code": exc.code, "message": exc.message}})

    def _pairing_svc():
        provider = pairing_provider() if callable(pairing_provider) \
            else pairing_provider
        if provider is None:
            return JSONResponse(status_code=503, content={
                "contract_version": "ime-pairing.v1", "request_id": None,
                "error": {"code": "persistence_unavailable",
                          "message": "配对服务未挂载"}})
        return provider

    @router.post("/api/v1/pairings/confirm")
    async def pairing_confirm(request: Request):
        try:
            body = await _json_body(request)
            provider = _pairing_svc()
            if isinstance(provider, JSONResponse):
                return provider
            result = provider.confirm(
                request_id=str(body.get("request_id") or ""),
                invitation_id=str(body.get("invitation_id") or ""),
                invite_secret=str(body.get("invite_secret") or ""),
                phone_instance_id=str(body.get("phone_instance_id") or ""),
                phone_label=str(body.get("phone_label") or ""),
                credential_id=str(body.get("credential_id") or ""),
                credential_token=str(body.get("credential_token") or ""),
                source=request.client.host if request.client else "lan")
            return JSONResponse(
                status_code=200 if result.get("replayed") else 201,
                content=result)
        except Exception as exc:
            from runtime.ime_pairing import ImePairingError
            if isinstance(exc, ImePairingError):
                return _pairing_error_response(exc)
            raise

    @router.get("/api/v1/pairings/{pairing_id}")
    async def pairing_get(pairing_id: str, request: Request):
        try:
            provider = _pairing_svc()
            if isinstance(provider, JSONResponse):
                return provider
            return provider.get_pairing(
                pairing_id, token=request.headers.get("x-ime-phone-token") or "")
        except Exception as exc:
            from runtime.ime_pairing import ImePairingError
            if isinstance(exc, ImePairingError):
                return _pairing_error_response(exc)
            raise

    @router.post("/api/v1/pairings/{pairing_id}/activate")
    async def pairing_activate(pairing_id: str, request: Request):
        try:
            body = await _json_body(request)
            provider = _pairing_svc()
            if isinstance(provider, JSONResponse):
                return provider
            return provider.activate(
                pairing_id, request_id=str(body.get("request_id") or ""),
                expected_revision=int(body.get("expected_revision") or 0),
                token=request.headers.get("x-ime-phone-token") or "")
        except Exception as exc:
            from runtime.ime_pairing import ImePairingError
            if isinstance(exc, ImePairingError):
                return _pairing_error_response(exc)
            raise
    if frame_source is None:
        from adapters.screen_ime.camera_source import OpenCVFrameSource

        frame_source = OpenCVFrameSource()
    def get_frame_source(device_id: str | None = None):
        """Resolve a per-device source without changing legacy object injection."""
        if callable(frame_source) and not hasattr(frame_source, "capture"):
            return frame_source(device_id)
        return frame_source

    @router.get("/api/v1/health")
    async def health():
        healthy = get_service().tasks.database_healthy()
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded",
                     "database": "ok" if healthy else "error"},
        )

    # ---------- 任务 ----------
    @router.post("/api/v1/tasks")
    async def create_task(request: Request):
        require_machine(request)  # R3-17-P4：任务创建归本机/服务管理
        body = await _json_body(request)
        task, created = await get_service().tasks.create(body, direct=True)
        return JSONResponse(status_code=201 if created else 200, content=task)

    @router.get("/api/v1/tasks")
    async def list_tasks(request: Request):
        require_machine(request)  # 全局任务列表不对手机开放（P4）
        query = request.query_params
        svc = get_service()
        page = svc.tasks.list(
            device_id=query.get("deviceId") or None,
            status=query.get("status") or None,
            target_user=query.get("targetUser") or None,
            text_query=query.get("query") or None,
            limit=_clamp_int(query.get("limit"), 50, 1, 100, "limit"),
            cursor=svc.tasks.decode_cursor(query.get("cursor")),
            created_from=_iso_or_none(query.get("createdFrom"), "createdFrom"),
            created_to=_iso_or_none(query.get("createdTo"), "createdTo"),
        )
        return page

    @router.get("/api/v1/devices")
    async def list_devices(request: Request):
        require_machine(request)
        return {"items": get_service().tasks.list_devices()}

    @router.get("/api/v1/devices/{device_id}/tasks/next")
    async def next_task(device_id: str, request: Request):
        require_active_phone(request, device_id)  # R3-17-P4：领取在校验之后
        get_service().tasks.touch(device_id)  # 设备轮询=隐式心跳（保持在线状态真实）
        wait = _clamp_int(request.query_params.get("waitSeconds"), 20, 0, 20, "waitSeconds")
        task = await get_service().tasks.next_task(device_id, wait)
        if task is None:
            return Response(status_code=204)
        return task

    @router.post("/api/v1/devices/{device_id}/heartbeat")
    async def heartbeat(device_id: str, request: Request):
        require_active_phone(request, device_id)
        body = await _json_body(request)
        return await get_service().tasks.heartbeat(device_id, body)

    @router.get("/api/v1/tasks/{task_id}")
    async def task_detail(task_id: str, request: Request):
        require_machine(request)  # W4-001：任务正文读取归本机/服务管理（P4）
        return get_service().tasks.detail(task_id)

    @router.get("/api/v1/tasks/{task_id}/wait")
    async def wait_task(task_id: str, request: Request):
        require_machine(request)  # W4-001：状态等待归本机/服务管理（P4）
        query = request.query_params
        wait = _clamp_int(query.get("waitSeconds"), 20, 0, 20, "waitSeconds")
        raw = query.get("expected")
        if not raw or not raw.strip():
            raise ScreenImeApiError(400, "invalid_request", "expected is required")
        expected: set[str] = set()
        for item in raw.split(","):
            normalized = item.strip()
            if not normalized:
                continue
            if normalized not in ("pending", "displayed", "typing", "inserted",
                                  "typing_failed", "cancel_requested", "cancelled"):
                raise ScreenImeApiError(400, "invalid_status", "Unsupported task status")
            expected.add(normalized)
        if not expected:
            raise ScreenImeApiError(400, "invalid_status",
                                    "At least one task status is required")
        return await get_service().tasks.wait_for_status(task_id, expected, wait)

    @router.post("/api/v1/tasks/{task_id}/status")
    async def update_status(task_id: str, request: Request):
        identity = require_active_phone(request)
        body = await _json_body(request)
        # 正文 device_id 不能覆盖认证身份（P4）
        if identity and identity.get("ime_device_id") \
                and str(body.get("deviceId") or "") != identity["ime_device_id"]:
            raise ScreenImeApiError(403, "phone_scope_denied",
                                    "正文 deviceId 与认证身份不一致")
        return await get_service().tasks.update_status(task_id, body)

    @router.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, request: Request):
        require_machine(request)  # R3-17-P4：任务取消归本机/服务管理
        return await get_service().tasks.cancel(task_id)

    @router.get("/api/v1/tasks/{task_id}/control/next")
    async def next_control(task_id: str, request: Request):
        identity = require_active_phone(request)
        query = request.query_params
        device_id = query.get("deviceId")
        if not device_id or not device_id.strip():
            raise ScreenImeApiError(400, "invalid_request", "deviceId is required")
        if identity and identity.get("ime_device_id") \
                and device_id.strip() != identity["ime_device_id"]:
            raise ScreenImeApiError(403, "phone_scope_denied",
                                    "查询 deviceId 与认证身份不一致")
        wait = _clamp_int(query.get("waitSeconds"), 20, 0, 20, "waitSeconds")
        get_service().tasks.touch(device_id.strip())
        command = await get_service().tasks.next_control(task_id, device_id.strip(), wait)
        if command is None:
            return Response(status_code=204)
        return command

    @router.post("/api/v1/tasks/{task_id}/control/ack")
    async def ack_control(task_id: str, request: Request):
        identity = require_active_phone(request)
        body = await _json_body(request)
        if identity and identity.get("ime_device_id") \
                and str(body.get("deviceId") or "") != identity["ime_device_id"]:
            raise ScreenImeApiError(403, "phone_scope_denied",
                                    "正文 deviceId 与认证身份不一致")
        return await get_service().tasks.acknowledge_control(task_id, body)

    # ---------- 屏幕校准会话 ----------
    @router.post("/api/v1/calibrations")
    async def create_calibration(request: Request):
        require_machine(request)  # R3-17-P4：校准创建归本机/服务管理
        body = await _json_body(request)
        return await get_service().calibrations.create(body.get("deviceId"))

    @router.get("/api/v1/devices/{device_id}/calibration")
    async def current_calibration(device_id: str, request: Request):
        require_active_phone(request, device_id)  # 本手机校准读取（P4）
        # 校准页打开期间手机持续轮询本端点（~750ms 一次）——视为隐式心跳：
        # 调试台刷新设备列表即可看到该设备在线，无需手机端点「测试连接」
        get_service().tasks.touch(device_id)
        session = await get_service().calibrations.current(device_id)
        if session is None:
            return Response(status_code=204)
        return session

    @router.get("/api/v1/calibrations/{session_id}")
    async def get_calibration(session_id: str, request: Request):
        require_machine(request)
        return await get_service().calibrations.get(session_id)

    @router.post("/api/v1/calibrations/{session_id}/arm")
    async def arm_calibration(session_id: str, request: Request):
        require_machine(request)  # 布防归本机/服务管理（P4）
        body = await _json_body(request)
        svc = get_service()
        # 设备级互斥：新手势采样活动存在时拒绝旧点布防（规范 22.2 节）。
        # B5（2026-10-01 backlog）：检查与布防动作置于同一 cross_mutex 临界区，
        # 消除与 prepare_trial 双侧 check-then-act 竞窗。
        gesture = getattr(svc, "gesture_capture", None)
        if gesture is None:
            return await svc.calibrations.arm(
                session_id, body.get("sampleId"), body.get("sequence"),
                body.get("targetU"), body.get("targetV"),
                body.get("targetWidthU"), body.get("targetHeightV"))
        with gesture.cross_mutex:
            # 不变量：临界区内不得引入真实让出点（DB await/sleep）——
            # prepare_trial 在 loop 线程上取同一锁，让出即死锁整个循环。
            existing = await svc.calibrations.get(session_id)
            if gesture.has_active_sampling(existing["deviceId"]):
                raise ScreenImeApiError(
                    409, "gesture_sampling_active",
                    "手势采样进行中，旧点布防被拒绝；请先结束手势试验")
            return await svc.calibrations.arm(
                session_id, body.get("sampleId"), body.get("sequence"),
                body.get("targetU"), body.get("targetV"),
                body.get("targetWidthU"), body.get("targetHeightV"))

    @router.post("/api/v1/calibrations/{session_id}/disarm")
    async def disarm_calibration(session_id: str, request: Request):
        require_machine(request)
        body = await _json_body(request)
        return await get_service().calibrations.disarm(
            session_id, body.get("sampleId"), body.get("sequence"))

    @router.post("/api/v1/calibrations/{session_id}/touch")
    async def touch_calibration(session_id: str, request: Request):
        identity = require_active_phone(request)
        if identity and identity.get("ime_device_id"):
            session = await get_service().calibrations.get(session_id)
            if session and session.get("deviceId") != identity["ime_device_id"]:
                raise ScreenImeApiError(403, "phone_scope_denied",
                                        "校准会话不属于该手机")
        body = await _json_body(request)
        return await get_service().calibrations.touch(session_id, body)

    @router.post("/api/v1/calibrations/{session_id}/client")
    async def calibration_client(session_id: str, request: Request):
        identity = require_active_phone(request)
        if identity and identity.get("ime_device_id"):
            session = await get_service().calibrations.get(session_id)
            if session and session.get("deviceId") != identity["ime_device_id"]:
                raise ScreenImeApiError(403, "phone_scope_denied",
                                        "校准会话不属于该手机")
        body = await _json_body(request)
        return await get_service().calibrations.client(
            session_id, body.get("deviceId"), body.get("state"),
            body.get("screenWidth"), body.get("screenHeight"),
            body.get("orientation"))

    @router.post("/api/v1/calibrations/{session_id}/complete")
    async def complete_calibration(session_id: str, request: Request):
        require_machine(request)  # 完成归本机/服务管理（P4）
        return await get_service().calibrations.finish(session_id, cancel=False)

    @router.post("/api/v1/calibrations/{session_id}/cancel")
    async def cancel_calibration(session_id: str, request: Request):
        require_machine(request)
        return await get_service().calibrations.finish(session_id, cancel=True)

    # ---------- 校准映射（Agent/插件使用；平台侧扩展，不在手机协议内） ----------
    @router.get("/api/v1/screen-mappings")
    async def list_mappings(request: Request):
        require_machine(request)  # R3-17-P4：screen-mappings 管理不对手机开放
        return {"items": get_service().mappings.all()}

    @router.get("/api/v1/screen-mappings/{device_id}")
    async def get_mapping(device_id: str):
        mapping = get_service().mappings.get(device_id)
        if mapping is None:
            raise ScreenImeApiError(404, "mapping_not_found", "校准映射不存在，请先完成屏幕校准")
        return mapping

    # ---------- 零安装手机演示页（iPhone Safari 直接打开即可试） ----------
    @router.get("/screen-ime/phone")
    async def phone_page():
        if not _PHONE_PAGE.exists():
            raise ScreenImeApiError(404, "not_found", "演示页缺失")
        return FileResponse(_PHONE_PAGE, media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    # ---------- 固定机位摄像头定位（标定页适配摄像头） ----------
    def camera_parts(device_id: str | None = None):
        svc = get_service()
        if svc.camera_store is None:
            raise ScreenImeApiError(503, "opencv_missing",
                                    "OpenCV/numpy 未安装：.venv/bin/pip install opencv-python-headless numpy")
        from adapters.screen_ime.camera_source import CameraUnavailableError

        return svc, get_frame_source(device_id), CameraUnavailableError

    @router.get("/api/v1/screen-ime/camera/status")
    async def camera_status(request: Request):
        require_machine(request)  # W4-001：相机管理口归本机（P4 分类）
        svc, source, _ = camera_parts()
        items = sorted(svc.camera_store.all(), key=lambda item: item.get("deviceId", ""))
        return {"camera": source.name if source is not None else "按设备分路",
                "calibrations": items}

    @router.get("/screen-ime/aruco/{marker_id}.png")
    async def aruco_png(marker_id: int):
        """ArUco 基准码图片（校准页面 <img> 引用；白边留白已内置）。"""
        from adapters.screen_ime.camera_calibration import generate_aruco_png
        from fastapi.responses import Response

        png = generate_aruco_png(marker_id)
        if png is None:
            raise ScreenImeApiError(503, "opencv_missing",
                                    "OpenCV 未安装，无法生成 ArUco 基准码")
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})

    @router.post("/api/v1/screen-ime/camera/markers")
    async def camera_calibrate_screen(request: Request):
        """校准页"摄像头标定"：页面回报 4 个基准点屏幕坐标（ArUco id 或颜色），服务端拍帧配对拟合摄像头→屏幕。"""
        require_machine(request)  # W4-001：标定写路径归本机（P4 分类）
        body = await _json_body(request)
        device_id = body.get("deviceId")
        if not device_id or not str(device_id).strip():
            raise ScreenImeApiError(400, "invalid_request", "deviceId is required")
        markers = body.get("markers")
        if not isinstance(markers, list) or len(markers) < 4:
            raise ScreenImeApiError(400, "invalid_request",
                                    "需要 4 个标定点（markers: id 或 color + screenX + screenY）")
        for marker in markers:
            if not isinstance(marker, dict) or not isinstance(marker.get("screenX"), (int, float)) \
                    or not isinstance(marker.get("screenY"), (int, float)):
                raise ScreenImeApiError(400, "invalid_request",
                                        "标定点格式：{id 或 color, screenX, screenY}")
        from adapters.screen_ime.camera_calibration import (
            CameraCalibrationError,
            CameraScreenTransform,
            detect_aruco_markers,
            detect_markers,
            fit_camera_to_screen,
        )
        from adapters.screen_ime.service import now_iso

        svc, source, unavailable_error = camera_parts(str(device_id))
        try:
            frame = source.capture()
        except unavailable_error as exc:
            raise ScreenImeApiError(503, "camera_unavailable", str(exc))

        # 优先 ArUco（按 ID 配对，不会误检画面里其他物体）；页面报 color 时退回色点方案
        by_id = {int(m["id"]): (float(m["screenX"]), float(m["screenY"]))
                 for m in markers if isinstance(m.get("id"), int)}
        detected: dict = {}
        correspondences: list[dict] = []
        missing: list = []
        mode = "aruco"
        if by_id:
            found = detect_aruco_markers(frame)
            for marker_id, screen_pt in by_id.items():
                point = found.get(marker_id)
                if point is None:
                    missing.append(f"aruco:{marker_id}")
                    continue
                correspondences.append({"cameraX": point[0], "cameraY": point[1],
                                        "screenX": screen_pt[0], "screenY": screen_pt[1]})
            detected = {str(k): list(v) for k, v in found.items()}
        if len(correspondences) < 4 and all(m.get("color") for m in markers):
            mode = "color"
            detected = detect_markers(frame)
            correspondences, missing = [], []
            for marker in markers:
                point = detected.get(marker["color"])
                if point is None:
                    missing.append(marker["color"])
                    continue
                correspondences.append({"cameraX": point[0], "cameraY": point[1],
                                        "screenX": float(marker["screenX"]),
                                        "screenY": float(marker["screenY"])})
        if len(correspondences) < 4:
            raise ScreenImeApiError(
                409, "camera_markers_not_detected",
                f"画面中未检出到 4 个基准点（缺：{missing or '对比度不足/曝光异常'}）；"
                "请检查摄像头画面覆盖全部基准码、光线与曝光")
        try:
            fit = fit_camera_to_screen(correspondences)
        except CameraCalibrationError as exc:
            raise ScreenImeApiError(409, exc.code, exc.message)
        transform = CameraScreenTransform.from_dict(
            {"homography": fit["homography"], "reprojectionRms": fit["reprojectionRms"],
             "deviceId": device_id, "createdAt": now_iso()})
        saved = svc.camera_store.save(transform)
        return {"deviceId": device_id, "mode": mode,
                "reprojectionRms": saved["reprojectionRms"],
                "version": saved["version"], "detected": detected, "missing": missing,
                "note": "摄像头→屏幕单应已存档；摄像头或手机移动后重拍一次即可"}

    @router.post("/api/v1/screen-ime/camera/manual-touch")
    async def camera_manual_touch(request: Request):
        """机械臂物理点屏幕时，校准页面自动回报触点像素（按顺序积累，供 robot-complete 配对）。"""
        require_machine(request)  # W4-001：标定辅助写路径归本机（P4）
        body = await _json_body(request)
        device_id = body.get("deviceId")
        screen_x, screen_y = body.get("screenX"), body.get("screenY")
        if not device_id or not isinstance(screen_x, (int, float)) \
                or not isinstance(screen_y, (int, float)):
            raise ScreenImeApiError(400, "invalid_request", "需要 deviceId、screenX、screenY")
        touches = get_service().manual_touches.setdefault(str(device_id), [])
        touches.append({"screenX": float(screen_x), "screenY": float(screen_y),
                        "at": body.get("at") or ""})
        return {"deviceId": device_id, "touchCount": len(touches)}

    @router.delete("/api/v1/screen-ime/camera/manual-touch")
    async def camera_manual_touch_clear(request: Request, device_id: str):
        require_machine(request)  # W4-001：标定辅助写路径归本机（P4）
        get_service().manual_touches.pop(str(device_id), None)
        return {"deviceId": device_id, "touchCount": 0}

    @router.post("/api/v1/screen-ime/camera/robot-point")
    async def camera_robot_point(request: Request):
        """机械臂段：探头放到某位置后调用，拍帧找探头尖，经单应换算回屏幕像素。"""
        require_machine(request)  # W4-001：标定辅助写路径归本机（P4）
        body = await _json_body(request)
        device_id = body.get("deviceId")
        robot_x = body.get("robotX")
        robot_y = body.get("robotY")
        if not device_id or not isinstance(robot_x, (int, float)) \
                or not isinstance(robot_y, (int, float)):
            raise ScreenImeApiError(400, "invalid_request",
                                    "需要 deviceId、robotX、robotY")
        stored = get_service().camera_store.get(str(device_id)) if get_service().camera_store else None
        if stored is None:
            raise ScreenImeApiError(404, "mapping_not_found",
                                    "该设备还没有摄像头→屏幕标定，请先在校准页完成摄像头标定")
        from adapters.screen_ime.camera_calibration import (
            CameraScreenTransform,
            detect_pointer_tip,
        )

        svc, source, unavailable_error = camera_parts(str(device_id))
        try:
            frame = source.capture()
        except unavailable_error as exc:
            raise ScreenImeApiError(503, "camera_unavailable", str(exc))
        tip = detect_pointer_tip(frame)
        if tip is None:
            raise ScreenImeApiError(
                409, "camera_tip_not_detected",
                "画面中未找到探头尖（默认品红标记）；请确认标记颜色、光线与画面覆盖")
        transform = CameraScreenTransform.from_dict(stored)
        screen_x, screen_y = transform.screen_from_camera(tip[0], tip[1])
        return {"deviceId": device_id, "robotX": float(robot_x), "robotY": float(robot_y),
                "cameraX": tip[0], "cameraY": tip[1],
                "screenX": screen_x, "screenY": screen_y}

    @router.post("/api/v1/screen-ime/camera/robot-complete")
    async def camera_robot_complete(request: Request):
        """≥3 组 (机械坐标, 屏幕像素) 配对 → 仿射拟合 → 存入正式映射（与触点回报法同格式）。"""
        require_machine(request)  # W4-001：正式映射写路径归本机（P4）
        body = await _json_body(request)
        device_id = body.get("deviceId")
        pairs = body.get("pairs")
        threshold = body.get("residual_threshold_px", 3.0)
        if not device_id:
            raise ScreenImeApiError(400, "invalid_request", "需要 deviceId")
        if body.get("useManualTouches"):
            # 校准页积累的触点（机械臂逐次点击时页面自动回报）与机械坐标按顺序配对
            touches = get_service().manual_touches.get(str(device_id)) or []
            robot_coords = body.get("robotCoords")
            if not isinstance(robot_coords, list) or len(robot_coords) < 3 \
                    or len(robot_coords) != len(touches):
                raise ScreenImeApiError(
                    400, "invalid_request",
                    f"useManualTouches 需要 robotCoords 与页面触点一一对应"
                    f"（当前触点 {len(touches)} 个，机械坐标 {len(robot_coords or [])} 个）")
            pairs = [{"robotX": rc[0], "robotY": rc[1],
                      "screenX": touch["screenX"], "screenY": touch["screenY"]}
                     for rc, touch in zip(robot_coords, touches)]
        if not isinstance(pairs, list) or len(pairs) < 3:
            raise ScreenImeApiError(400, "invalid_request",
                                    "需要至少 3 组配对（pairs 或 useManualTouches+robotCoords）")
        from adapters.screen_ime.geometry import (
            CalibrationMathError,
            ScreenRobotTransform,
            fit_affine,
        )
        from adapters.screen_ime.service import now_iso

        svc = get_service()
        samples = []
        for pair in pairs:
            try:
                samples.append({"robotX": float(pair["robotX"]), "robotY": float(pair["robotY"]),
                                "screenX": float(pair["screenX"]), "screenY": float(pair["screenY"])})
            except (KeyError, TypeError, ValueError):
                raise ScreenImeApiError(400, "invalid_request",
                                        "pairs 元素需要 robotX/robotY/screenX/screenY 数字")
        try:
            fit = fit_affine(samples)
        except CalibrationMathError as exc:
            raise ScreenImeApiError(409, exc.code, exc.message)
        if fit["residualRms"] > float(threshold):
            raise ScreenImeApiError(
                409, "calibration_residual_exceeded",
                f"仿射拟合残差 {fit['residualRms']:.2f}px 超过阈值 {threshold}px，映射未保存")
        first = svc.camera_store.get(str(device_id)) or {}
        transform = ScreenRobotTransform.from_fit(
            fit, device_id=str(device_id),
            screen_width=int(body.get("screenWidth") or 0) or 1080,
            screen_height=int(body.get("screenHeight") or 0) or 2400,
            orientation=body.get("orientation", "portrait"),
            version=1, created_at=now_iso())
        saved = svc.mappings.save(transform)
        return {"mapping": saved, "reprojectionRms": fit["residualRms"],
                "cameraVersion": (first or {}).get("version"),
                "note": "机械↔屏幕映射已存档，screen_type/图标点击可直接使用"}

    # ---- L07/§11.13 ime-geometry.v1：键盘几何握手/上报/只读长轮询（零机械动作） ----
    # FIX-15/R2-21：服务实例由调用方注入（Fleet=当前 Runtime 持有的实例）；
    # 身份验证在服务内部于任何会话/nonce 处理前执行；凭据来自请求头
    # x-ime-phone-token（每机独立可撤销令牌；协议细节待用户确认）。
    def _geometry_service(request: Request):
        if geometry_provider is not None:
            return geometry_provider(request)
        from runtime.ime_geometry import shared_service
        return shared_service()

    def _geometry_credentials(request: Request):
        token = request.headers.get("x-ime-phone-token")
        return {"token": token} if token else None

    @router.post("/api/v1/devices/{ime_device_id}/geometry/sessions")
    async def ime_geometry_session(ime_device_id: str, request: Request):
        from runtime.ime_geometry import ImeGeometryError
        body = await request.json()
        try:
            return _geometry_service(request).handshake(
                ime_device_id, body, credentials=_geometry_credentials(request))
        except ImeGeometryError as exc:
            raise ScreenImeApiError(exc.status, exc.code, exc.message)

    @router.post("/api/v1/devices/{ime_device_id}/geometry")
    async def ime_geometry_report(ime_device_id: str, request: Request):
        from runtime.ime_geometry import ImeGeometryError
        body = await request.json()
        try:
            return _geometry_service(request).report(
                ime_device_id, body, credentials=_geometry_credentials(request))
        except ImeGeometryError as exc:
            raise ScreenImeApiError(exc.status, exc.code, exc.message)

    @router.get("/api/v1/devices/{ime_device_id}/geometry/requests/next")
    async def ime_geometry_poll(ime_device_id: str, request: Request):
        from runtime.ime_geometry import ImeGeometryError
        try:
            wait_raw = request.query_params.get("waitSeconds")
            wait_seconds = int(wait_raw) if wait_raw not in (None, "") else 0
        except ValueError:
            raise ScreenImeApiError(422, "ime_geometry_invalid", "waitSeconds 必须是整数")
        session_id = request.query_params.get("runtime_session_id") or ""
        try:
            # R2-03：正式路由走可取消异步长轮询——等待不占事件循环，
            # 取消/服务关闭立即退出且无遗留等待者。
            claimed = await _geometry_service(request).poll_next_async(
                ime_device_id, session_id, wait_seconds,
                credentials=_geometry_credentials(request))
        except ImeGeometryError as exc:
            raise ScreenImeApiError(exc.status, exc.code, exc.message)
        if claimed is None:
            # R3-02-E：204 不得携带正文——JSONResponse(content=None) 会渲染
            # b"null"，经真实 HTTP（h11）在每次空轮询上触发协议错误并断连
            #（TestClient 不经 h11 故此前未暴露）。空响应用无正文 Response。
            return Response(status_code=204)
        return claimed

    return router


def screen_ime_error_handler(request: Request, exc: ScreenImeApiError) -> JSONResponse:
    """与 K40TaskServer 一致的错误响应格式。"""
    return JSONResponse(status_code=exc.status,
                        content={"error": exc.code, "message": exc.message})
