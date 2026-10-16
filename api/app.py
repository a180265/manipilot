"""Arm Fleet HTTP API（FastAPI）。

- 所有业务显示返回内部键 + 中文显示（status_display 等）。
- 所有写操作必须带 operator_id、非空文本 reason（不限语言）、correlation_id、client_request_id；
  危险操作额外需要 confirmation_token。
- 错误统一返回：HTTP 状态、内部错误键、中文标题、中文处理建议、链路编号。
- 实时画面查看为只读会话，不获取租约、不暂停任务、不默认录制。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import jsonschema

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from runtime.audit import AuditLog  # noqa: F401
from runtime.errors import ApiError
from runtime.runtime import ArmFleetRuntime
from runtime.views import ViewSessionManager  # noqa: F401
from runtime.util import iso, new_id, parse_iso, utc_now

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "api" / "contracts"


def _ime_pending_cancellations(rt_) -> int:
    """FIX-02：从挂载的 IME 仓储投影待取消数量；仓储缺失返回 0。"""
    try:
        screen_ime = getattr(rt_, "screen_ime", None)
        repo = getattr(screen_ime, "tasks", None)
        if repo is not None and hasattr(repo, "pending_cancellation_count"):
            return repo.pending_cancellation_count()
    except Exception:
        pass
    return 0


def create_app(runtime: ArmFleetRuntime | None = None) -> FastAPI:
    app = FastAPI(title="Arm Fleet 通用硬件执行平台 API", version="0.1.0",
                  description="设备、任务、能力、插件、审计、急停与只读实时画面查看接口")
    from runtime.build_info import capture_runtime_build

    app.state.runtime_build = capture_runtime_build()

    if runtime is not None:
        app.state.runtime_factory = lambda: runtime

    # ---------- 屏幕输入法适配（设备侧 /api/v1/* 协议，与 /v1/* 平台契约互相独立） ----------
    from runtime.config import load_platform_config

    ime_config = runtime.config if runtime is not None else load_platform_config()
    maintenance = bool(ime_config.get("pre_enable_maintenance", False))
    app.state.pre_enable_maintenance = maintenance
    if maintenance:
        # GET is not universally side-effect free (e.g. phone task polling).
        # Only audited status, camera-source and screen-observation reads are
        # exposed in this mode (Auto_CS#20: screen-observation 是纯读证据——
        # GET 无副作用、不领任务、不动电机，与 camera-source-frame 同类豁免).
        read_paths = re.compile(
            r"/(?:openapi\.json|docs(?:/oauth2-redirect)?|redoc|v1/(?:"
            r"system/(?:status|runtime-build)|daemons|tasks(?:/[^/]+)?|"
            r"alerts|audit-events|devices(?:/[^/]+(?:/"
            r"(?:supervision|screen-observation|"
            r"camera-source-frame(?:/[^/]+/image)?))?)?))/?"
        )

        @app.middleware("http")
        async def pre_enable_guard(request: Request, call_next):
            # Match the router's decoded path, not URL parsing that truncates
            # literal ?/# characters originating from percent-encoded segments.
            route_path = request.scope["path"]
            allowed_read = (request.method in {"GET", "HEAD"}
                            and read_paths.fullmatch(route_path) is not None)
            emergency_stop = (request.method == "POST"
                              and route_path == "/v1/emergency-stop")
            if not allowed_read and not emergency_stop:
                error = ApiError("pre_enable_maintenance")
                return JSONResponse(status_code=error.http, content=error.to_payload())
            return await call_next(request)

    if ime_config.get("screen_ime_enabled", True):
        from adapters.screen_ime.routes import (
            create_screen_ime_router,
            screen_ime_error_handler,
        )
        from adapters.screen_ime.service import (
            ScreenImeApiError,
            ScreenImeService,
            set_default_service,
        )

        ime_service = ScreenImeService(ime_config.screen_ime_db_path,
                                       ime_config.screen_ime_mappings_path)
        set_default_service(ime_service)
        app.state.screen_ime = ime_service
        from adapters.screen_ime.camera_source import auto_frame_source
        from adapters.screen_ime.camera_source import UnconfiguredFrameSource
        from runtime.config import load_device_configs

        # 屏幕校准/输入法路由按 device_id 惰性解析摄像头；绝不在多设备
        # 启动时猜测全局 8790/latest.jpg。单路旧脚本需显式声明 legacy。
        try:
            configured_devices = load_device_configs(ime_config.root)
        except Exception:
            configured_devices = []
        source_cache: dict[str, tuple[tuple[object, ...], object]] = {}

        def camera_source_for(device_id: str | None):
            if not device_id:
                return auto_frame_source(camera_id=None)[0]
            key = str(device_id)
            # 优先读取当前运行时设备镜像，使设备菜单换绑后下一次校准
            # 不会继续使用旧 camera_id；进程尚未启动时再用配置白名单。
            active_runtime = getattr(app.state, "runtime", None)
            config = {}
            if active_runtime is not None:
                try:
                    config = active_runtime.devices.get(key)
                except Exception:
                    config = {}
            if not config:
                config = next((item for item in configured_devices
                               if str(item.get("device_id")) == key), {})
            camera_id = config.get("camera_id")
            frame_url = config.get("camera_frame_url")
            status_url = config.get("camera_status_url")
            camera_mode = str(config.get("camera_mode") or "")
            camera_unique_id = config.get("camera_device_unique_id")
            expected_width = config.get("expected_frame_width")
            expected_height = config.get("expected_frame_height")
            binding_revision = config.get("binding_revision")
            allow_legacy = camera_mode == "legacy_single"
            # 绑定变更不一定更换 camera_id（例如换 URL、物理 UVC 或分辨率），
            # 因此缓存键必须覆盖所有会改变帧身份/来源的字段。
            cache_key = (
                str(camera_id or ""), str(frame_url or ""), str(status_url or ""),
                camera_mode, str(camera_unique_id or ""),
                str(expected_width or ""), str(expected_height or ""),
                str(binding_revision or ""),
            )
            cached = source_cache.get(key)
            if cached is not None and cached[0] == cache_key:
                return cached[1]
            if cached is not None:
                close = getattr(cached[1], "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass
            source = auto_frame_source(
                bridge_url=str(frame_url) if frame_url else None,
                status_url=str(status_url) if status_url else None,
                camera_id=str(camera_id) if camera_id else None,
                expected_device_id=str(key),
                expected_device_unique_id=(str(camera_unique_id)
                                           if camera_unique_id else None),
                expected_width=(int(expected_width) if expected_width else None),
                expected_height=(int(expected_height) if expected_height else None),
                allow_legacy=allow_legacy,
            )[0]
            # 不缓存“桥尚未可用”的占位源；否则服务稍后恢复时会永久卡在
            # UnconfiguredFrameSource，直到绑定再次变化或进程重启。
            if not isinstance(source, UnconfiguredFrameSource):
                source_cache[key] = (cache_key, source)
            return source

        # FIX-15：几何三端点使用当前 Runtime 持有的几何服务实例（禁止跨
        # Runtime 全局单例），身份反查按 Runtime 设备表唯一绑定执行。
        def geometry_provider(request: Request):
            rt_ = getattr(app.state, "runtime", None)
            if rt_ is not None and getattr(rt_, "ime_geometry", None) is not None:
                return rt_.ime_geometry
            from runtime.ime_geometry import shared_service
            return shared_service()

        # R3-17-P4：手机身份 Provider——配对凭据（生产）优先，登记凭据
        # （R2-21 测试/内部装配）兜底；无凭据统一 401（旧输入法需升级配对）。
        # 业主产品决策 2026-10-16：未配置配对信任材料（ime_pairing_trust 空）=
        # 开放部署模式，不注入手机身份门（填 IP+端口直连即用；LAN 明文前提下
        # 身份/撤销能力不启用）；配置了信任材料的部署仍强制配对身份。
        _ime_pairing_enforced = (runtime is not None and getattr(
            runtime, "ime_pairing_trust", None) is not None)
        def phone_identity_provider(token):
            rt_ = getattr(app.state, "runtime", None)
            if rt_ is None or not token:
                return None
            resolver = getattr(rt_, "phone_identity_for_token", None)
            if resolver is None:
                return None
            return resolver(token)

        def pairing_provider():
            rt_ = getattr(app.state, "runtime", None)
            return getattr(rt_, "ime_pairing", None)

        app.include_router(create_screen_ime_router(
            ime_service, camera_source_for, geometry_provider=geometry_provider,
            phone_identity_provider=phone_identity_provider if _ime_pairing_enforced else None,
            pairing_provider=pairing_provider))
        # gesture-capture.v1 手机采样协议路由（规范第 5.2 节，同挂 8788）。
        # D 接口请求①：手机侧 5 端点同款 token 门（W4-001：与 screen_ime
        # 路由共用同一 phone_identity_provider，未配对零采样入口）。
        from adapters.screen_ime.gesture_routes import create_gesture_router

        app.include_router(create_gesture_router(
            ime_service, phone_identity_provider=phone_identity_provider))
        app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not hasattr(app.state, "runtime_factory"):
            app.state.runtime_factory = ArmFleetRuntime
        rt = app.state.runtime_factory()
        ime_service = getattr(app.state, "screen_ime", None)
        if ime_service is not None:
            rt.devices.set_input_status_provider(ime_service.tasks.list_devices)
        if getattr(app.state, "device_configs", None) is None and runtime is None:
            # 生产引导：从 configs/devices.json 加载设备白名单（e2e/测试经
            # app.state 显式注入；旧 agent 架构迁移后的等价装配）。
            try:
                from runtime.config import load_device_configs
                app.state.device_configs = load_device_configs(ime_config.root)
            except Exception:
                app.state.device_configs = []
        if not rt._running:
            await rt.startup(device_configs=getattr(app.state, "device_configs", None)
                             or [])
        # §4.8.5：Fleet 挂载输入法仓储注入输入控制钩子（P07）
        screen_ime = getattr(app.state, "screen_ime", None)
        if screen_ime is not None:
            screen_ime.set_control_hooks(rt.ime_control_hooks_fleet())
        app.state.runtime = rt
        yield
        # D 接口请求③：先停 Runtime——在途插件的取消清理可能仍需写 IME
        # 库；先关服务会把这类写入打到已关连接（关停期噪声，非状态损坏）。
        await rt.shutdown(
            stop_daemons=bool(rt.config.get("daemon_supervisor_stop_on_shutdown", False))
        )
        ime_service = getattr(app.state, "screen_ime", None)
        if ime_service is not None:
            ime_service.close()
            from adapters.screen_ime.service import set_default_service

            set_default_service(None)

    app.router.lifespan_context = lifespan

    # ---------- 通用 ----------
    def rt(request: Request) -> ArmFleetRuntime:
        return request.app.state.runtime

    @app.get("/v1/devices/{device_id}/screen-observation")
    async def screen_observation(device_id: str, request: Request):
        from runtime.screen_observation import ScreenObservationService
        runtime = rt(request)
        if not hasattr(runtime, "screen_observations"):
            runtime.screen_observations = ScreenObservationService(runtime)
        return await runtime.screen_observations.capture(device_id)

    def op_context(body: dict, default_role: str = "operator") -> dict:
        return {
            "operator_id": body.get("operator_id", ""),
            "role": body.get("role", default_role),
            "reason": body.get("reason", ""),
            "correlation_id": body.get("correlation_id", ""),
            "client_request_id": body.get("client_request_id", ""),
            "confirmation_token": body.get("confirmation_token"),
        }

    def preview_service_base(rt_: ArmFleetRuntime) -> str:
        host = str(rt_.config.get("camera_preview_host", "127.0.0.1"))
        port = int(rt_.config.get("camera_preview_port", 8793))
        return f"http://{host}:{port}"

    async def preview_service_available(rt_: ArmFleetRuntime) -> bool:
        """Check only the optional media plane; never make it a platform dependency."""
        if not rt_.config.get("camera_preview_enabled", False):
            return False
        try:
            import httpx

            timeout = httpx.Timeout(0.35, connect=0.15)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{preview_service_base(rt_)}/health")
            return response.status_code == 200
        except Exception:
            return False

    def device_view(rt_: ArmFleetRuntime, device: dict) -> dict:
        view = dict(device)
        view.update(rt_.devices.input_status_view(device["device_id"]))
        view["queue_length"] = len([t for t in rt_.store.all_tasks()
                                    if t["device_id"] == device["device_id"] and t["status"] in ("queued", "awaiting_device")])
        lease = rt_.leases.get(device["device_id"])
        view["active_lease"] = lease
        view["alerts"] = [a for a in rt_.store.all_alerts(include_resolved=False)
                          if a.get("device_id") == device["device_id"]][:5]
        view["supervision"] = rt_.supervision.view(device["device_id"])
        view["eligibility"] = rt_.device_eligibility(device["device_id"], require_input=True)
        return view

    def task_view(rt_: ArmFleetRuntime, task: dict) -> dict:
        view = dict(task)
        view["evidence"] = rt_.evidence.for_task(task["task_id"])
        return view

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.http, content=exc.to_payload())

    # 域异常（带 reason_code）的统一转换点：reason_code 已登记时按登记状态码
    # 送达（如 gesture_settings_invalid 409 / navigation_run_not_found 404），
    # 不再落入兜底 500；未登记的 reason_code 仍走 500 兜底并可见键名。
    def _register_domain_error_handler(exception_type) -> None:
        async def domain_error_handler(request: Request, exc: Exception):
            error = ApiError(exc.reason_code, exc.message)
            return JSONResponse(status_code=error.http, content=error.to_payload())
        app.add_exception_handler(exception_type, domain_error_handler)

    from runtime.automatic_gestures import AutomaticGestureError
    from drivers.gesture_program import GestureProgramError
    _register_domain_error_handler(AutomaticGestureError)
    _register_domain_error_handler(GestureProgramError)

    # FIX-12/13：布局域错误按异常自带状态码送达（AdaptationError.status；
    # RegionError 代码固定映射），不再落入 500 兜底。
    async def adaptation_error_handler(request: Request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {
            "key": exc.code, "title": exc.message,
            "suggestion": "请按错误键核对会话/预览/指针修订后重试。",
            "trace_id": new_id("trace"), "detail": exc.message,
        }})

    _REGION_ERROR_STATUS = {
        "camera_request_invalid": 422,
        "camera_not_found": 404,
        "revision_conflict": 409,
    }

    async def region_error_handler(request: Request, exc):
        status = _REGION_ERROR_STATUS.get(getattr(exc, "code", ""), 422)
        return JSONResponse(status_code=status, content={"error": {
            "key": exc.code, "title": exc.message,
            "suggestion": "请核对粗框请求、来源证据与指针修订后重试。",
            "trace_id": new_id("trace"), "detail": exc.message,
        }})

    try:
        from runtime.layout_adaptation import AdaptationError
        app.add_exception_handler(AdaptationError, adaptation_error_handler)
    except ImportError:  # pragma: no cover
        pass
    try:
        from runtime.layout_region import RegionError
        app.add_exception_handler(RegionError, region_error_handler)
    except ImportError:  # pragma: no cover
        pass
    try:  # 导航轮次模块按需加载（与其他使用点同守卫）
        from adapters.uinav.automatic_navigation import NavigationRunError
        _register_domain_error_handler(NavigationRunError)
    except ImportError:  # pragma: no cover - 模块缺失属集成错误
        pass

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        error = ApiError("invalid_request", f"服务器内部错误：{exc}")
        return JSONResponse(status_code=500, content={
            "error": {"key": "internal_error", "title": "服务器内部错误",
                      "suggestion": "请查看服务日志和链路编号后重试。", "trace_id": error.trace_id,
                      "detail": str(exc)},
        })

    @app.get("/v1/system/runtime-build")
    async def runtime_build(request: Request):
        return request.app.state.runtime_build

    @app.get("/v1/system/status")
    async def system_status(request: Request):
        rt_ = rt(request)
        return {
            "platform_version": rt_.config.get("platform_version"),
            "agent_version": rt_.config.get("agent_version"),
            "hardware_mode": rt_.config.hardware_mode,
            "hardware_mode_display": "仿真模式" if rt_.config.hardware_mode == "simulation" else "真机模式",
            "contract_version": rt_.config.get("contract_version"),
            "pre_enable_maintenance": bool(rt_.config.get("pre_enable_maintenance", False)),
            "operation_mode_display": ("启用前维护，仅开放状态和原始画面"
                                       if rt_.config.get("pre_enable_maintenance", False)
                                       else "常规运行"),
            "estop_active": rt_.estop.active,
            "estop_active_display": "急停中" if rt_.estop.active else "正常",
            "device_count": len(rt_.devices.all()),
            "daemon_supervisor": {
                "enabled": bool(rt_.daemon_supervisor.enabled),
                "states": rt_.daemon_status(),
            },
            "task_type_registry": [
                {"task_type": k, **{kk: vv for kk, vv in v.items()}}
                for k, v in rt_.plugins.task_type_registry().items()
            ],
        }

    @app.get("/v1/daemons")
    async def list_daemons(request: Request):
        """返回每台 daemon 进程的监督状态；不触发串口 CONNECT。"""
        rt_ = rt(request)
        return {
            "enabled": bool(rt_.daemon_supervisor.enabled),
            "items": rt_.daemon_status(),
            "说明": "监督器只管理 daemon 进程；串口 CONNECT 仍由人工调试操作触发",
        }

    # ---------- 设备 ----------
    @app.get("/v1/devices/{device_id}/screen-wake")
    async def screen_wake_metadata(device_id: str, request: Request):
        from runtime.screen_wake import wake_metadata

        return await wake_metadata(rt(request), device_id)

    @app.post("/v1/devices/{device_id}/screen-wake/manual-confirmations")
    async def screen_wake_manual_confirm(device_id: str, request: Request):
        """W13/§9.3：本地操作员单次现场确认；只签发确认记录，不下发运动。"""
        from runtime.screen_wake import confirmations

        runtime = rt(request)
        # 当前部署不能隔离操作员控制通道时，该例外默认禁用（规范 §9.3(3)）。
        if not bool(runtime.config.get("screen_wake_manual_confirm_enabled", False)):
            raise ApiError("screen_wake_manual_confirm_disabled",
                           "人工无姿态唤醒确认未启用：当前部署未隔离本地操作员通道")
        try:
            body = await request.json()
        except Exception as exc:
            raise ApiError("invalid_request", f"请求体不是有效 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise ApiError("invalid_request", "请求体必须是 JSON 对象")
        if isinstance(body.get("task_id"), str):
            task = runtime.store.get_task(body["task_id"])
            if task is None:
                raise ApiError("task_not_found", "确认引用的唤醒任务不存在")
            if task.get("device_id") != device_id:
                raise ApiError("screen_wake_manual_confirm_conflict", "确认任务与设备不一致")
            if (task.get("status") == "expired"
                    or (task.get("expires_at")
                        and parse_iso(task["expires_at"]) <= utc_now())):
                raise ApiError("screen_wake_manual_confirm_expired",
                               "唤醒任务已过期，确认引用失效")
        status, payload = confirmations(runtime).create(runtime, device_id, body)
        return JSONResponse(status_code=status, content=payload)

    @app.get("/v1/devices/{device_id}/screen-gestures")
    async def screen_gesture_metadata(device_id: str, request: Request):
        from runtime.screen_gestures import gesture_metadata

        return await gesture_metadata(rt(request), device_id)

    def saved_gesture_view(device_id: str, request: Request, gesture_id: str | None = None):
        from drivers import calibration_bundle, saved_screen_gestures

        runtime = rt(request)
        device = runtime.devices.get(device_id)
        driver = runtime.devices.driver(device_id)
        root = getattr(driver, "data_root", None) or runtime.config.root / "data"
        try:
            bundle = calibration_bundle.load_bundle(device_id, root)
            session = device.get("daemon_session")
            if gesture_id is not None:
                return saved_screen_gestures.get_gesture(device_id, gesture_id, bundle, session, root)
            return {
                "contract_version": saved_screen_gestures.CONTRACT_VERSION, "device_id": device_id,
                "gestures": saved_screen_gestures.list_gestures(device_id, bundle, session, root),
            }
        except saved_screen_gestures.SavedGestureError as exc:
            return JSONResponse(status_code=exc.http_status, content={"error": {
                "key": exc.code, "title": "已保存屏幕手势不可用",
                "suggestion": "请核对设备、手势编号和档案；损坏档案不可覆盖或调用。",
                "trace_id": new_id("trace"), "detail": str(exc),
            }})

    @app.get("/v1/devices/{device_id}/saved-screen-gestures")
    async def saved_screen_gesture_list(device_id: str, request: Request):
        return await asyncio.to_thread(saved_gesture_view, device_id, request)

    @app.get("/v1/devices/{device_id}/saved-screen-gestures/{gesture_id}")
    async def saved_screen_gesture_detail(device_id: str, gesture_id: str, request: Request):
        return await asyncio.to_thread(saved_gesture_view, device_id, request, gesture_id)

    @app.get("/v1/devices/{device_id}/screen-proof")
    async def screen_proof_metadata(device_id: str, request: Request):
        from drivers import calibration_bundle, screen_proof

        runtime = rt(request)
        device = runtime.devices.get(device_id)
        root = getattr(runtime.devices.driver(device_id), "data_root", None) or runtime.config.root / "data"
        result = {"contract_version": "screen-proof.v1", "device_id": device_id,
                  "registered": False, "usable": False, "reference_states": [],
                  "regions_registered": False, "revision": None, "blocking_reasons": []}
        try:
            selected = await asyncio.to_thread(runtime.layout_observation.resolve, device_id)
            if selected is not None:
                _, bundle = screen_proof.binding(device_id, root, allow_motion_ready=True)
                result.update(await asyncio.to_thread(selected.metadata, bundle))
            else:
                profile = screen_proof.load_profile(device_id, root)
                if not profile:
                    result["blocking_reasons"].append("页面识别档案尚未登记")
                    return result
                bundle = calibration_bundle.load_bundle(device_id, root)
                result.update(registered=True, reference_states=sorted(profile.get("references", {})),
                              regions_registered=bool(profile.get("regions")), revision=profile["revision"])
            if bundle.get("daemonSession") != device.get("daemon_session"):
                result["blocking_reasons"].append("页面档案与当前机械臂会话不一致")
            if "camera.observe_regions" not in device.get("capabilities", []):
                result["blocking_reasons"].append("设备未启用登记区域视觉证据能力")
            if not result["regions_registered"] or not result["reference_states"]:
                result["blocking_reasons"].append("页面参考或识别区域尚未登记完整")
            result["usable"] = not result["blocking_reasons"]
            if selected is not None:
                selected.ensure_current()
        except (ValueError, OSError, KeyError) as exc:
            result["usable"] = False
            result["blocking_reasons"].append(str(exc))
        return result

    @app.get("/v1/devices/{device_id}/gesture-profiles/{profile_id}")
    async def gesture_profile(device_id: str, profile_id: str, request: Request):
        return rt(request).gesture_profile_view(device_id, profile_id)

    @app.get("/v1/devices")
    async def list_devices(request: Request):
        rt_ = rt(request)
        return {"devices": [device_view(rt_, d) for d in rt_.devices.all()]}

    @app.post("/v1/devices/discovery/scan")
    async def scan_device_discovery(request: Request):
        body = await request.json()
        result = await rt(request).devices.scan_discovery(ports=body.get("ports") if isinstance(body, dict) else None)
        return result

    @app.post("/v1/devices/register")
    async def register_device(request: Request):
        body = await request.json()
        body.setdefault("hotplug", True)
        result = await rt(request).register_device(body)
        return device_view(rt(request), result)

    @app.post("/v1/devices/{device_id}/attach")
    async def attach_device(device_id: str, request: Request):
        body = await request.json()
        body["device_id"] = device_id
        result = await rt(request).attach_device(body)
        return device_view(rt(request), result)

    @app.post("/v1/devices/{device_id}/remove")
    async def remove_device(device_id: str, request: Request):
        body = await request.json()
        result = await rt(request).remove_device(device_id, body.get("reason") or "设备拔出")
        return device_view(rt(request), result)

    # ---------- 设备菜单：绑定事实与绑定变更（C 组） ----------
    @app.get("/v1/devices/{device_id}/binding")
    async def get_device_binding(device_id: str, request: Request):
        return rt(request).device_binding_view(device_id)

    @app.get("/v1/devices/{device_id}/binding/options")
    async def get_device_binding_options(device_id: str, request: Request):
        return rt(request).devices.binding_options(device_id)

    @app.put("/v1/devices/{device_id}/binding")
    async def update_device_binding(device_id: str, request: Request):
        body = await request.json()
        context = op_context(body)
        updates = {key: body[key] for key in (
            "arm_slot", "camera_id", "camera_device_unique_id", "ime_device_id",
            "wechat_account_id", "daemon_addr", "serial_port", "camera_frame_url",
            "camera_status_url", "camera_binding_id", "daemon_binding_id", "serial_binding_id",
        ) if key in body}
        result = await rt(request).op_device_binding_update(device_id, updates, context)
        return {"device_id": device_id, **result,
                "说明": "绑定变更已保存；设备已暂停并进入待校准门禁，重新校准前不得托管"}

    @app.post("/v1/devices/{device_id}/binding/disable")
    async def disable_device_binding(device_id: str, request: Request):
        body = await request.json()
        context = op_context(body)
        result = rt(request).op_device_binding_disable(device_id, context)
        return {"device_id": device_id, **result,
                "说明": "绑定已禁用：设备暂停且不能接收任务；恢复需重新校准"}

    @app.post("/v1/devices/{device_id}/binding/enable")
    async def enable_device_binding(device_id: str, request: Request):
        body = await request.json()
        context = op_context(body)
        result = rt(request).op_device_binding_enable(device_id, context)
        return {"device_id": device_id, **result,
                "说明": "绑定已恢复：设备进入待校准状态，完成校准和主界面检查后方可托管"}

    # ---------- §4.3 设备侧允许回复名单（业务系统保存名单时同步） ----------
    @app.get("/v1/devices/{device_id}/reply-allowlist")
    async def get_reply_allowlist(device_id: str, request: Request):
        rt_ = rt(request)
        rt_.devices.get(device_id)
        if not bool(rt_.config.get("wx_compat_enabled", False)):
            raise ApiError("wx_compat_disabled", "回复白名单由 WX 8787 管理，Arm Fleet 不保存业务名单")
        entries = json.loads(rt_.store.kv_get(f"reply_allowlist:{device_id}") or "[]")
        return {"device_id": device_id, "entries": entries,
                "说明": "设备侧允许回复名单；由 WX 保存名单时同步维护"}

    @app.post("/v1/devices/{device_id}/reply-allowlist")
    async def update_reply_allowlist(device_id: str, request: Request):
        rt_ = rt(request)
        rt_.devices.get(device_id)
        if not bool(rt_.config.get("wx_compat_enabled", False)):
            raise ApiError("wx_compat_disabled", "回复白名单由 WX 8787 管理，Arm Fleet 不保存业务名单")
        body = await request.json()
        context = op_context(body)
        entry = {
            "wechat_account_id": str(body.get("wechat_account_id") or ""),
            "external_conversation_id": str(body.get("external_conversation_id") or ""),
            "wechat_name": str(body.get("wechat_name") or ""),
            "enabled": bool(body.get("enabled", True)),
            "operator": str(body.get("operator") or context.get("operator_id") or ""),
            "config_version": str(body.get("config_version") or ""),
        }
        if not entry["wechat_account_id"]:
            raise ApiError("missing_field", "名单项必须绑定微信账号编号")
        entries = json.loads(rt_.store.kv_get(f"reply_allowlist:{device_id}") or "[]")
        key = (entry["wechat_account_id"], entry["external_conversation_id"])
        kept = [item for item in entries
                if (item.get("wechat_account_id"), item.get("external_conversation_id")) != key]
        kept.append({**entry, "updated_at": iso(utc_now())})
        rt_.store.kv_set(f"reply_allowlist:{device_id}", json.dumps(kept, ensure_ascii=False))
        rt_.audit.record("supervision_configure", device_id=device_id,
                         outcome="success",
                         reason=f"更新允许回复名单：{entry['wechat_name'] or entry['wechat_account_id']}"
                                f"{' 启用' if entry['enabled'] else ' 停用'}",
                         detail=entry)
        return {"device_id": device_id, "entry": entry, "entries": kept,
                "说明": "设备侧允许回复名单已更新并写入审计"}

    # ---------- §10 回调持久队列查询 ----------
    @app.get("/v1/callbacks")
    async def list_callbacks(request: Request, status: str | None = None):
        outbox = rt(request).callback_outbox
        return {"callbacks": outbox.list_callbacks(status=status), **outbox.summary(),
                "说明": "WX 回调持久队列：待投递/人工处理/已投递/最终放弃可查询"}

    @app.post("/v1/callbacks/flush")
    async def flush_callbacks(request: Request):
        rt_ = rt(request)
        from runtime.config import task_callback_base_url
        base = task_callback_base_url(rt_.config)
        summary = await rt_.callback_outbox.flush(base)
        return {"flushed": True, **summary,
                "说明": "已触发一轮回调重投；未配置回调地址时仅返回待投递数量"}

    @app.post("/v1/devices/menu/register")
    async def menu_register_device(request: Request):
        """设备菜单新增设备：带操作人/原因/链路编号/幂等与审计。"""
        body = await request.json()
        context = op_context(body)
        result = await rt(request).op_device_register(body, context)
        return device_view(rt(request), result)

    @app.get("/v1/devices/{device_id}/eligibility")
    async def device_eligibility(device_id: str, request: Request, require_input: bool = True):
        return {"device_id": device_id, **rt(request).device_eligibility(device_id, require_input=require_input)}

    # ---------- 自动手势与业务导航（规范第 7、12.3、22.12、22.14、22.15 节） ----------

    def _gesture_bundle_and_frame(rt_, device_id: str):
        from drivers import calibration_bundle as cb
        from drivers import gesture_program as gprog
        data_root = rt_._gesture_data_root(device_id)
        return (cb.load_bundle(device_id, data_root),
                gprog.load_coordinate_frame(device_id, data_root), data_root)

    @app.get("/v1/devices/{device_id}/automatic-gestures")
    async def automatic_gestures(device_id: str, request: Request):
        from runtime import automatic_gestures as ag
        rt_ = rt(request)
        bundle, frame, data_root = _gesture_bundle_and_frame(rt_, device_id)
        return ag.gestures_view(device_id, bundle=bundle, frame=frame,
                                data_root=data_root)

    @app.get("/v1/devices/{device_id}/automatic-gesture-settings")
    async def gesture_settings(device_id: str, request: Request):
        from runtime import automatic_gestures as ag
        from runtime.calibration_gate import check_registered_calibration
        from drivers import calibration_bundle as cb
        rt_ = rt(request)
        data_root = rt_._gesture_data_root(device_id)
        bundle = cb.load_bundle(device_id, data_root)
        device = rt_.devices.get(device_id)
        calibration_ready = (cb.automatic_calibration_verified(bundle)
                             and device.get("calibration_status") == "validated")
        if calibration_ready:
            calibration_ready = not check_registered_calibration(
                device, bundle)
        return {"device_id": device_id,
                **ag.load_settings(device_id, data_root),
                "calibrationReady": calibration_ready}

    @app.put("/v1/devices/{device_id}/automatic-gesture-settings")
    async def update_gesture_settings(device_id: str, request: Request):
        """开关已全部删除（2026-09-30 拍板）：保留端点仅作拒绝语义，
        任何 updates 一律 400——可用性由校准门禁自动决定，无需人工开启。"""
        from runtime import automatic_gestures as ag
        from runtime.permissions import check_operation
        body = await request.json()
        context = op_context(body)
        check_operation("gesture_settings_update", context)
        updates = body.get("updates")
        if not isinstance(updates, dict) or not updates:
            raise ApiError("invalid_request", "updates 必须为非空对象")
        rt_ = rt(request)
        data_root = rt_._gesture_data_root(device_id)
        # save_settings 对任何 updates 都抛 invalid_request（开关已删除）
        ag.save_settings(device_id, updates, data_root=data_root)
        return {"device_id": device_id, **ag.load_settings(device_id, data_root)}

    def _submit_self_test(rt_, device_id: str, actions, context, *,
                          vision_only=None):
        from runtime import automatic_gestures as ag
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone
        bundle, frame, data_root = _gesture_bundle_and_frame(rt_, device_id)
        settings = ag.load_settings(device_id, data_root)
        rules_version = settings.get("gesture_rules_version",
                                     ag.DEFAULT_SETTINGS["gesture_rules_version"])
        binding, blocking = ag._current_binding(bundle, frame, rules_version)
        if binding is None:
            raise ApiError(blocking[0]["code"], blocking[0]["message"])
        profile = ag.load_profile(device_id, data_root)
        binding["gesture_profile_version"] = int(profile.get("version") or 0) \
            if profile else 0
        # 视觉优先（拍板 2026-09-30）：纯滑动自检默认走相机光流判定发证，
        # 不依赖手势页会话（生产 业务无软件回报）；显式传 vision_only 可覆盖。
        scroll_only = bool(actions) and all(
            a in ("scroll_up", "scroll_down") for a in actions)
        if vision_only is None:
            vision_only = scroll_only
        task_id = f"gval-{_uuid.uuid4().hex[:12]}"
        payload = {
            "task_type": "gesture_self_test",
            "contract_version": "gesture-validation.v1",
            "device_id": device_id, "task_id": task_id,
            "idempotency_key": task_id,
            "payload": {"binding": binding, "actions": list(actions),
                        "rules_version": rules_version,
                        "vision_only": bool(vision_only)},
            "expires_at": (datetime.now(timezone.utc)
                           + timedelta(minutes=15)).isoformat(timespec="seconds"),
            "correlation_id": task_id,
        }
        return rt_.submit_task(payload, source="console", context=context)

    @app.post("/v1/devices/{device_id}/automatic-gestures/validate")
    async def validate_gestures(device_id: str, request: Request):
        from runtime.permissions import check_operation
        body = await request.json()
        context = op_context(body)
        check_operation("task_submit", context)
        actions = body.get("actions") or ["scroll_up", "scroll_down",
                                          "system_back_left", "system_back_right",
                                          "double_tap", "system_home"]
        vision_only = body.get("vision_only")
        if vision_only is not None and not isinstance(vision_only, bool):
            raise ApiError("invalid_request", "vision_only 必须为布尔")
        return _submit_self_test(rt(request), device_id, actions, context,
                                 vision_only=vision_only)



    @app.get("/v1/devices/{device_id}/automatic-navigation/{profile_id}")
    async def automatic_navigation(device_id: str, profile_id: str,
                                   request: Request):
        from runtime import automatic_gestures as ag
        rt_ = rt(request)
        bundle, frame, data_root = _gesture_bundle_and_frame(rt_, device_id)
        view = ag.navigation_view(device_id, profile_id, bundle=bundle,
                                  frame=frame, data_root=data_root,
                                  evidence_root=rt_.config.evidence_dir,
                                  evidence_get=rt_.evidence.get)
        if view is None:
            raise ApiError("navigation_profile_not_found",
                           f"导航档案 {profile_id} 不存在")
        return view

    def _navigation_manager(rt_, device_id: str):
        try:
            from adapters.uinav.automatic_navigation import NavigationRunManager
        except ImportError as exc:  # pragma: no cover - 模块缺失属集成错误
            raise ApiError("capability_unavailable",
                           f"导航轮次模块未加载：{exc}") from exc
        return NavigationRunManager(device_id,
                                    data_root=rt_._gesture_data_root(device_id))

    @app.get("/v1/devices/{device_id}/navigation-runs/{navigation_id}")
    async def navigation_run_view(device_id: str, navigation_id: str,
                                  request: Request):
        view = _navigation_manager(rt(request), device_id).run_view(navigation_id)
        if view is None:
            raise ApiError("navigation_run_not_found", "导航轮次不存在")
        return view

    @app.post("/v1/devices/{device_id}/navigation-runs/{navigation_id}/close")
    async def navigation_run_close(device_id: str, navigation_id: str,
                                   request: Request):
        from runtime.permissions import check_operation
        body = await request.json()
        context = op_context(body)
        check_operation("task_submit", context)
        reason = body.get("reason")
        if reason not in ("caller_finished", "caller_cancelled"):
            raise ApiError("invalid_request",
                           "reason 必须为 caller_finished 或 caller_cancelled")
        manager = _navigation_manager(rt(request), device_id)
        try:
            import time as _time
            return manager.close_run(navigation_id, reason,
                                     now_ms=int(_time.time() * 1000))
        except Exception as exc:
            if getattr(exc, "reason_code", None) == "navigation_step_conflict":
                raise ApiError("navigation_step_conflict", str(exc)) from exc
            raise

    @app.post("/v1/devices/{device_id}/navigation-template-previews")
    async def navigation_template_preview(device_id: str, request: Request):
        from adapters.uinav.navigation_templates import NavigationTemplates, request_payload
        from runtime.permissions import check_operation
        body = await request.json()
        request_payload(body, "previewRequest")
        context = op_context(body)
        check_operation("task_submit", context)
        return await NavigationTemplates(rt(request), device_id).preview(
            body, context["operator_id"])

    @app.put("/v1/devices/{device_id}/navigation-templates/{template_id}")
    async def register_navigation_template(device_id: str, template_id: str,
                                           request: Request):
        from adapters.uinav.navigation_templates import NavigationTemplates, request_payload
        from runtime.permissions import check_operation
        body = await request.json()
        request_payload(body, "registrationRequest")
        context = op_context(body)
        check_operation("navigation_template_register", context)
        rt_ = rt(request)
        record = NavigationTemplates(rt_, device_id).register(template_id, body)
        rt_.audit.record("navigation_template_register",
                         operator_id=context["operator_id"], device_id=device_id,
                         reason=context["reason"],
                         detail=f"导航模板登记：{template_id}")
        return record

    @app.get("/v1/devices/{device_id}/navigation-templates/{template_id}")
    async def get_navigation_template(device_id: str, template_id: str,
                                      request: Request):
        from adapters.uinav.navigation_templates import NavigationTemplates
        return NavigationTemplates(rt(request), device_id).get(template_id)

    def _guard_record(rt_: ArmFleetRuntime, device_id: str) -> dict | None:
        try:
            from runtime.input_control import record_id_for_guard
            return rt_.store.input_record_get(record_id_for_guard(device_id))
        except Exception:
            return None

    def _guard_revision(rt_: ArmFleetRuntime, device_id: str) -> int:
        guard = _guard_record(rt_, device_id)
        return int(guard["revision"]) if guard else 0

    def _has_open_recovery_issues(rt_: ArmFleetRuntime, device_id: str) -> bool:
        guard = _guard_record(rt_, device_id)
        return bool(guard and any(i["state"] == "open" for i in guard.get("issues", [])))

    


    def _input_settings_status(rt_: ArmFleetRuntime, device_id: str,
                               settings: dict | None) -> dict:
        """§4.2.2 完整资格快照：GET 与 PUT 原响应共用同一组合公式。"""
        device = rt_.devices.get(device_id)
        calibration_ready = device.get("calibration_status") == "validated"
        binding = rt_.devices.input_binding(device_id) or {}
        current_ime = binding.get("ime_device_id")
        # §4.2.2：ime_online/keyboard_active=当前唯一绑定手机的实时状态（与资格
        # 计算同源）；未知按 false 并给阻断原因，不用设备字典上的缓存旗标。
        try:
            ime = rt_.devices._ime_status(device) or {}
        except Exception:
            ime = {}
        ime_online = bool(ime.get("online"))
        keyboard_active = bool(ime.get("keyboardActive", ime.get("keyboard_active", False)))
        blocking: list[dict] = []
        if settings is not None:
            eligibility = rt_.devices.eligibility(
                device_id, require_input=True,
                ime_device_id=settings.get("ime_device_id"))
            blocking.extend({"code": code, "message": code}
                            for code in eligibility["blocking_checks"])
            if settings.get("enabled") and settings.get("ime_device_id"):
                if not current_ime:
                    blocking.append({"code": "input_binding_unavailable",
                                     "message": "尚未确定唯一输入法手机"})
                elif settings.get("ime_device_id") != current_ime:
                    blocking.append({"code": "input_binding_mismatch",
                                     "message": "已保存手机与当前唯一绑定不一致"})
        seen: set[str] = set()
        deduped = []
        for item in blocking:
            if item["code"] not in seen:
                seen.add(item["code"])
                deduped.append(item)
        effective = bool(settings and settings.get("enabled") is True
                         and not deduped and ime_online and keyboard_active)
        from runtime import input_settings as iset
        from runtime.util import utc_now, iso
        return {"contract_version": iset.CONTRACT_VERSION,
                "device_id": device_id,
                "ime_device_id": (settings or {}).get("ime_device_id"),
                "current_ime_device_id": current_ime,
                "enabled": bool((settings or {}).get("enabled") is True),
                "revision": (settings or {}).get("revision") or 0,
                "revoked_through_revision": (settings or {}).get("revoked_through_revision") or 0,
                "updated_at": (settings or {}).get("updated_at"),
                "observed_at": iso(utc_now()),
                "effective": effective,
                "blocking_reasons": deduped,
                "calibration_ready": calibration_ready,
                "ime_online": ime_online,
                "keyboard_active": keyboard_active,
                # FIX-02：待取消数从 IME 实际记录投影（不再硬编码 0）
                "pending_cancellation_count": _ime_pending_cancellations(rt_),
                "recovery_revision": _guard_revision(rt_, device_id),
                "input_recovery_required": _has_open_recovery_issues(rt_, device_id)}

    @app.get("/v1/devices/{device_id}/input-settings")
    async def get_input_settings(device_id: str, request: Request):
        """A-开关（规格 §4.2.2）：完整资格快照。GET 零动作；未知查询参数明确拒绝。"""
        from runtime import input_settings as iset
        if len(request.query_params):
            raise ApiError("invalid_request",
                           f"未知查询参数：{', '.join(request.query_params.keys())}；本端点不接受查询参数")
        rt_ = rt(request)
        rt_.devices.get(device_id)
        try:
            # R2-02：GET 走统一历史核对入口（已知设备文件丢失/回退=503，
            # 不按空白 r0 重建，不返回回退内容）
            settings = rt_.input_control.load_settings_verified(
                device_id, data_root=rt_.devices.settings_data_root(device_id))
        except iset.InputSettingsError as exc:
            # §4.2.3：持久资料不可安全解释 → 503，保留原文件、阻断输入。
            raise ApiError("input_settings_invalid", exc.message)
        return _input_settings_status(rt_, device_id, settings)

    @app.put("/v1/devices/{device_id}/input-settings")
    async def put_input_settings(device_id: str, request: Request):
        """A-开关写契约（规格 §4.2）：权限/幂等/并发/绑定/审计；保存零动作。"""
        from runtime import input_settings as iset
        from runtime.input_control import InputControlError
        from runtime.permissions import check_operation
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("invalid_request", "请求体必须为 JSON 对象")
        allowed_fields = {"contract_version", "enabled", "expected_revision",
                          "ime_device_id", "operator_id", "role", "reason",
                          "correlation_id", "client_request_id"}
        unknown = sorted(set(body) - allowed_fields)
        if unknown:
            raise ApiError("invalid_request", f"请求包含未知字段：{', '.join(unknown)}")
        if body.get("contract_version") != iset.CONTRACT_VERSION:
            raise ApiError("unsupported_contract",
                           f"contract_version 必须为 {iset.CONTRACT_VERSION}")
        limits = {"operator_id": 100, "role": 32, "reason": 500,
                  "correlation_id": 160, "client_request_id": 160}
        for field, max_len in limits.items():
            value = body.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_len:
                raise ApiError("invalid_request",
                               f"{field} 必须为非空字符串（≤{max_len} 字符）")
        context = op_context(body)
        check_operation("input_settings_update", context)
        # 规格 §4.2.1：wx 与 operator 数值同级，不能只靠等级比较——显式拒绝 wx/readonly
        if context.get("role") in ("wx", "readonly"):
            raise ApiError("permission_denied", "wx/readonly 身份不能修改输入开关")
        rt_ = rt(request)
        rt_.devices.get(device_id)
        calibration_ready = rt_.devices.get(device_id).get("calibration_status") == "validated"
        binding = rt_.devices.input_binding(device_id) or {}
        current_ime = binding.get("ime_device_id")
        enabled = body.get("enabled")
        if type(enabled) is not bool:
            raise ApiError("invalid_request", "enabled 必须为布尔值")
        # 仅对新请求生效；重放在模块内先返回原快照，不重新评估开启条件。
        binding_state = None
        if enabled:
            if not current_ime:
                binding_state = "unavailable"
            elif body.get("ime_device_id") != current_ime:
                binding_state = "mismatch"
        try:
            # §4.8.3/§4.8.8 P08：公网写入口统一走 InputControl 编排
            # （prepared 意图/崩溃对账/撤销边界全链生效）。
            outcome = rt_.input_control.write_settings(
                device_id,
                data_root=rt_.devices.settings_data_root(device_id),
                request={"enabled": enabled,
                         "ime_device_id": body.get("ime_device_id"),
                         "expected_revision": body.get("expected_revision"),
                         "operator_id": context.get("operator_id") or "",
                         "reason": context.get("reason") or "",
                         "client_request_id": context.get("client_request_id") or "",
                         "correlation_id": context.get("correlation_id")},
                observe=lambda _device_id, _stored: _input_settings_status(
                    rt_, device_id, _stored),
                calibration_ready=calibration_ready,
                binding_state=binding_state,
                call_context={"origin": "human_put",
                              "operator_id": context.get("operator_id"),
                              "role": context.get("role"),
                              "operation_id": "input_settings_update"})
        except iset.InputSettingsError as exc:
            raise ApiError(exc.reason_code, exc.message)
        except InputControlError as exc:
            raise ApiError(exc.reason_code, exc.message)
        rt_.audit.record(
            "input_settings_update", operator_id=context.get("operator_id") or "",
            device_id=device_id, reason=context.get("reason") or "",
            detail={"enabled": enabled,
                    "revision": (outcome["settings"] or {}).get("revision"),
                    "ime_device_id": body.get("ime_device_id"),
                    "replayed": outcome.get("replayed", False),
                    "client_request_id": context.get("client_request_id")})
        return {"settings": outcome["settings"], "replayed": outcome.get("replayed", False)}

    @app.post("/v1/devices/{device_id}/input-recovery/review")
    async def post_input_recovery_review(device_id: str, request: Request):
        """§4.8.7 人工核对出口：零机械动作，仅解除列明的可核对问题。"""
        from runtime.input_control import InputControlError
        from runtime.permissions import check_operation
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("invalid_request", "请求体必须为 JSON 对象")
        allowed = {"contract_version", "expected_recovery_revision", "ime_device_id",
                   "issue_ids", "candidate_removed", "editor_checked",
                   "operator_id", "role", "reason", "correlation_id",
                   "client_request_id"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise ApiError("invalid_request", f"请求包含未知字段：{', '.join(unknown)}")
        if body.get("contract_version") != "input-recovery-review.v1":
            raise ApiError("unsupported_contract",
                           "contract_version 必须为 input-recovery-review.v1")
        for field in ("candidate_removed", "editor_checked"):
            if body.get(field) is not True:
                raise ApiError("invalid_request", f"{field} 必须为 true（人工已核对）")
        issue_ids = body.get("issue_ids")
        if not isinstance(issue_ids, list) or not issue_ids \
                or len(set(issue_ids)) != len(issue_ids):
            raise ApiError("invalid_request", "issue_ids 必须为非空且不重复的列表")
        limits = {"operator_id": 100, "role": 32, "reason": 500,
                  "correlation_id": 160, "client_request_id": 160}
        for field, max_len in limits.items():
            value = body.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_len:
                raise ApiError("invalid_request",
                               f"{field} 必须为非空字符串（≤{max_len} 字符）")
        context = op_context(body)
        check_operation("input_settings_update", context)
        if context.get("role") in ("wx", "readonly"):
            raise ApiError("permission_denied", "wx/readonly 身份不能执行人工核对")
        rt_ = rt(request)
        rt_.devices.get(device_id)
        # 原下发协程退出核对：设备不得有持有租约的执行中任务
        lease = rt_.leases.get(device_id)
        if lease is not None:
            raise ApiError("device_busy", "设备仍有任务租约：先完成/取消原任务再核对")
        try:
            result = rt_.input_control.review_input_recovery(
                device_id,
                expected_revision=int(body.get("expected_recovery_revision") or 0),
                issue_ids=[str(i) for i in issue_ids],
                resolution={"candidate_removed": True, "editor_checked": True,
                            "operator_id": context.get("operator_id"),
                            "reason": context.get("reason")},
                client_request_id=context.get("client_request_id") or "",
                operator_id=context.get("operator_id") or "",
                call_context={"origin": "human_review",
                              "operator_id": context.get("operator_id"),
                              "role": context.get("role"),
                              "operation_id": "input_recovery_review"})
        except InputControlError as exc:
            raise ApiError(exc.reason_code, exc.message)
        except Exception as exc:
            from runtime.input_control import InputRecordConflict
            if isinstance(exc, InputRecordConflict):
                raise ApiError("input_recovery_conflict",
                               "人工核对与并发变更冲突：请刷新恢复版本后重试")
            raise
        rt_.audit.record(
            "input_recovery_review", operator_id=context.get("operator_id") or "",
            device_id=device_id, reason=context.get("reason") or "",
            detail={"issue_ids": result["issue_ids"],
                    "recovery_revision": result["recovery_revision"],
                    "client_request_id": context.get("client_request_id")})
        return {"device_id": device_id,
                "issue_ids": result["issue_ids"],
                "recovery_revision": result["recovery_revision"],
                "input_recovery_required": _has_open_recovery_issues(rt_, device_id)}

    # ---- §5.1（camera-binding B03）：Fleet 只读来源接口（零动作） ----
    def _camera_source_meta(rt_: ArmFleetRuntime, device_id: str) -> dict | None:
        # 帧桥（camera_frame_server）与 Fleet 是两个进程：来源事实以
        # EvidenceStore 最新 camera_source_v2 归档为准（§5.1 证据接口）。
        try:
            return rt_.evidence.latest_source_frame_v2(device_id)
        except Exception:
            return None

    # R2-18（camera-binding 附件 §5.1）：来源 API 输出冻结 v2 对象——字段转换
    # 集中在 camera_broker 构建器边界；缺可信字段拒绝（503），不补默认值。
    from drivers.camera_broker import (SourceFrameContractError,
                                       build_screen_source_response,
                                       build_source_frame_view)

    def _source_freshness_gate(record: dict) -> None:
        """§5.1：沿用采样距现在≤2 秒、未来≤0.5 秒门槛（按真实采样时刻核对）。"""
        from datetime import datetime, timezone
        captured = record.get("captured_at")
        if not captured:
            raise ApiError("source_unavailable",
                           "归档证据缺少真实采样时刻：不可作为当前来源")
        try:
            captured_dt = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
            if captured_dt.tzinfo is None:
                raise ValueError("naive")
        except ValueError:
            raise ApiError("source_unavailable", "来源帧时间无效")
        age = (datetime.now(timezone.utc) - captured_dt).total_seconds()
        if age > 2:
            raise ApiError("source_unavailable",
                           f"来源帧不新鲜（{age:.1f}s）：请确认采集进行中")
        if age < -0.5:
            raise ApiError("source_unavailable",
                           f"来源帧时间来自未来（{-age:.1f}s）：拒绝")

    @app.get("/v1/devices/{device_id}/camera-source-frame")
    async def get_camera_source_frame(device_id: str, request: Request):
        rt_ = rt(request)
        rt_.devices.get(device_id)
        record = _camera_source_meta(rt_, device_id)
        if record is None or not record.get("jpeg_sha256"):
            raise ApiError("source_unavailable",
                           "设备没有可用的 v2 来源帧：请检查 CameraHelper 会话")
        _source_freshness_gate(record)
        try:
            return build_source_frame_view(record)
        except SourceFrameContractError as exc:
            raise ApiError(getattr(exc, "code", "source_unavailable"), str(exc))

    @app.get("/v1/devices/{device_id}/camera-source-frame/{evidence_id}/image")
    async def get_camera_source_image(device_id: str, evidence_id: str,
                                      request: Request):
        import hashlib as _hashlib
        rt_ = rt(request)
        record = rt_.evidence.get(evidence_id)
        if record is None or record.get("device_id") != device_id \
                or record.get("evidence_type") != "camera_source_v2":
            raise ApiError("camera_not_found", "证据不存在或不属于该设备")
        # 即使证据已过实时期限也可仅供查看（不续期）；但原字节摘要必须与归档
        # 记录一致（读原字节，不取另一帧、不重编码）。
        data = rt_.evidence.read_source_frame_bytes(evidence_id)
        digest = _hashlib.sha256(data).hexdigest()
        expected = record.get("jpeg_sha256")
        if expected and digest != expected:
            raise ApiError("source_unavailable",
                           "证据原字节摘要与归档记录不一致（已损坏）")
        return Response(content=data, media_type="image/jpeg",
                        headers={"ETag": f'"{digest}"',
                                 "Cache-Control": "no-store"})

    @app.get("/v1/devices/{device_id}/screen-source-observation")
    async def get_screen_source_observation(device_id: str, request: Request,
                                            source_evidence_id: str):
        rt_ = rt(request)
        rt_.devices.get(device_id)
        if not source_evidence_id:
            raise ApiError("camera_request_invalid",
                           "source_evidence_id 必填（只从指定原图摆正/识别）")
        record = rt_.evidence.get(source_evidence_id)
        if record is None or record.get("device_id") != device_id \
                or record.get("evidence_type") != "camera_source_v2":
            raise ApiError("camera_not_found", "来源证据不存在")
        bundle = None
        from drivers import calibration_bundle as cb
        try:
            arm = rt_.devices.driver(device_id)
            bundle = cb.load_bundle(device_id, getattr(arm, "data_root", None))
        except Exception:
            bundle = None
        if (not bundle or int(bundle.get("schemaVersion") or 0) != 3
                or bundle.get("cameraBindingId") != (record.get("camera_binding_id"))
                or bundle.get("sourceGeometryId") != record.get("source_geometry_id")):
            raise ApiError("source_unavailable",
                           "本台 v3 机械校准与指定原帧的绑定/几何不匹配")
        from runtime.screen_observation import observe_from_source_frame
        observation = await observe_from_source_frame(
            rt_, device_id=device_id, bundle=bundle,
            evidence_id=source_evidence_id)
        # §5.1：screen-source-observation.v2 = 外层 source 身份 + 内层
        # screen-observation.v1 已声明字段的严格投影 + 同帧坐标域摘要；
        # 只由指定原帧派生，不另拍新图。
        # §5.1/R2-18：coordinate_frame_hash 是外层字段（screen_observation
        # schema 封闭，内层不得携带）；observation 与外层 source 的同帧一致性
        # 由 build_screen_source_response 严格核对，不符拒绝。
        coordinate_frame_hash = observation.pop("coordinate_frame_hash", None)
        observation.pop("source", None)
        try:
            return build_screen_source_response(
                record, observation=observation,
                coordinate_frame_hash=coordinate_frame_hash)
        except SourceFrameContractError as exc:
            raise ApiError(getattr(exc, "code", "source_unavailable"), str(exc))

    # ==== 布局 35 路由（R2-17 冻结 wire 契约 + R2-23 可信身份 + R2-15 粗框） ====
    # FIX-12/R2-25：布局资料服务由当前 Runtime 持有（runtime.layout_bundle）。
    # R2-23：全部布局路由经唯一可信身份解析点（runtime/layout_auth.py）；
    # 服务端 principal 不由正文 operator_id/principal_id/role 默认或覆盖；
    # 未配置身份提供器时写/取证/导出/导入/设备资料读 403（明确前置），共享模板
    # 只读放行（契约 §2）。R2-17：请求/响应按冻结 wire schema 安装 parser，
    # 错误外壳 layout-error.v1；公共头 Cache-Control/X-Correlation-Id/
    # X-Layout-Contract-Version。
    from runtime import layout_auth
    from runtime.layout_capture import (
        archived_camera_identity, make_evidence_capture_provider,
    )
    from runtime.layout_drafts import LayoutDraftService
    from runtime.layout_review import LayoutReviewService
    from runtime.layout_state import content_sha
    from runtime.layout_wire import (
        ROUTE_CONTRACT_VERSIONS,
        LayoutHttpError,
        binary_response,
        error_response,
        json_response,
        new_correlation_id,
        validate_request_view,
        validate_response_view,
    )

    def _layout_db_path(request: Request) -> Path:
        if hasattr(request.app.state, "layout_db"):
            return Path(str(request.app.state.layout_db))
        return rt(request).layout_db_path

    def _layout_bundle(request: Request) -> dict:
        return rt(request).layout_bundle(
            db_path=_layout_db_path(request))

    def _layout_store(request: Request):
        """L04/L06 共享 KV（legacy 只读对账/包记录）。"""
        return _layout_bundle(request)["kv_store"]

    def _layout_ops_svc(request: Request):
        svc = _layout_bundle(request)["ops_svc"]
        if not getattr(svc, "_d2_rollback_env_attached", False):
            # R3-16：适配回滚事实源（新四态会话/当前帧/校准/绑定/空闲）。
            rt_ = rt(request)
            bundle = _layout_bundle(request)
            svc.attach_adaptation_rollback_env(
                session_loader=bundle["adaptation_svc"]._load_session,
                capture_provider=_capture_provider(rt_),
                calibration_provider=lambda device: _calibration_facts(rt_, device),
                binding_provider=lambda device: _binding_facts(rt_, device),
                device_idle=lambda device: rt_.leases.get(device) is None)
            svc._d2_rollback_env_attached = True
        return svc

    def _layout_state(request: Request):
        return _layout_bundle(request)["state_store"]

    # ---- Runtime 事实源（R2-16/R2-15 提供器；缺可信字段拒绝） ----
    def _device_arm_binding(rt_, device_id: str) -> str:
        device = rt_.devices.get(device_id) or {}
        return str(device.get("daemon_binding_id")
                   or device.get("arm_binding_id") or "")

    def _calibration_facts(rt_, device_id: str) -> dict | None:
        from drivers import calibration_bundle as cb
        try:
            arm = rt_.devices.driver(device_id)
            # 与 Runtime._gesture_data_root 同一解析（driver 档案根优先，
            # 否则 config.root/data）；不经 default_data_root 触碰仓库目录
            data_root = getattr(arm, "data_root", None)                 or (rt_.config.root / "data")
            bundle = cb.load_bundle(device_id, data_root)
        except Exception:
            bundle = None
        if not bundle or int(bundle.get("schemaVersion") or 0) != 3:
            return None
        if bundle.get("status") not in ("ready", "motion_ready") \
                or (bundle.get("camera") or {}).get("validated") is not True:
            return None
        return bundle

    def _binding_facts(rt_, device_id: str) -> dict:
        """当前安装事实（相机侧来自最近归档 v2 帧；屏幕侧来自本台 v3）。"""
        facts: dict = {}
        latest = None
        try:
            latest = rt_.evidence.latest_source_frame_v2(device_id)
        except Exception:
            latest = None
        if latest:
            try:
                camera_identity = archived_camera_identity(latest)
            except ValueError as exc:
                raise LayoutHttpError(
                    "layout_binding_changed",
                    "归档来源的物理相机身份缺失或冲突，请重新采集", 409,
                ) from exc
            facts.update({
                "camera_id": latest.get("camera_id"),
                "camera_device_unique_id": camera_identity,
                "camera_binding_id": latest.get("camera_binding_id"),
                "source_geometry_id": latest.get("source_geometry_id"),
                "arm_binding_id": _device_arm_binding(rt_, device_id),
            })
            registered = rt_.devices.get(device_id) or {}
            for key in ("camera_id", "camera_device_unique_id",
                        "camera_binding_id", "source_geometry_id"):
                if registered.get(key) and registered[key] != facts.get(key):
                    raise LayoutHttpError(
                        "layout_binding_changed",
                        "当前设备绑定与已归档来源不一致，请重新采集", 409)
            for configured, observed in (
                ("expected_frame_width", "frame_width"),
                ("expected_frame_height", "frame_height"),
            ):
                if registered.get(configured) and latest.get(observed) \
                        and registered[configured] != latest.get(observed):
                    raise LayoutHttpError(
                        "layout_binding_changed",
                        "当前设备来源尺寸与已归档来源不一致，请重新采集", 409)
        bundle = _calibration_facts(rt_, device_id)
        if bundle:
            screen = bundle.get("screen") or {}
            facts["screen_descriptor"] = {
                "width_px": int(screen.get("screenWidth") or screen.get("widthPx")
                                or 0) or None,
                "height_px": int(screen.get("screenHeight") or screen.get("heightPx")
                                 or 0) or None,
                "orientation": "portrait",
                "system_insets": {"left": 0, "top": 0, "right": 0,
                                  "bottom": 0},
                "calibration_geometry_hash":
                    bundle.get("coordinateFrameHash")
                    or bundle.get("bindingFingerprint"),
            }
            facts.setdefault("camera_binding_id",
                             bundle.get("cameraBindingId"))
        binding = rt_.devices.input_binding(device_id) or {}
        facts["ime_device_id"] = binding.get("ime_device_id")
        return facts

    def _identity(request: Request, route_id: str):
        """R2-23 单点：解析 + 授权分类（共享模板只读放行）。"""
        identity = layout_auth.resolve_identity(request)
        try:
            layout_auth.authorize(
                identity, route_id=route_id,
                shared_read=route_id in layout_auth.SHARED_READ_ROUTES)
        except PermissionError as exc:
            raise LayoutHttpError("layout_permission_denied", str(exc),
                                  403) from exc
        return identity

    def _require_principal(request: Request, route_id: str) -> str:
        identity = _identity(request, route_id)
        if identity is None:
            raise LayoutHttpError("layout_permission_denied",
                                  layout_auth.PREREQ_MESSAGE, 403)
        return identity.principal

    def _wire_headers(request: Request) -> dict:
        canonical = {"content-type": "Content-Type",
                     "x-correlation-id": "X-Correlation-Id",
                     "x-layout-contract-version": "X-Layout-Contract-Version"}
        view = {}
        for key, value in request.headers.items():
            name = canonical.get(key.lower())
            if name:
                view[name] = value
        return view

    def _query_view(request: Request) -> dict:
        query: dict = {}
        for key in request.query_params.keys():
            values = request.query_params.getlist(key)
            if len(values) > 1:
                raise LayoutHttpError(
                    "layout_request_invalid",
                    f"查询参数 {key} 重复（不接受重复参数）")
            raw = values[0]
            if raw == "":
                raise LayoutHttpError(
                    "layout_request_invalid",
                    f"查询参数 {key} 为空串（省略或合法 UUID）")
            if raw.isdigit():
                query[key] = int(raw)
            else:
                query[key] = raw
        return query

    async def _wire_request(request: Request, route_id: str,
                            path_params: dict):
        """装配 wire 请求视图并按冻结 schema 校验；返回 (view, body)。"""
        if request.method == "GET" or (
                request.method == "DELETE"
                and not request.headers.get("content-length")):
            # GET 无体；DELETE 允许空体直呼（无 content-length 时跳过
            # content-type 检查——api:empty 路由的合法形态）。
            body = None
        else:
            content_type = (request.headers.get("content-type") or "").split(
                ";")[0].strip().lower()
            if content_type != "application/json":
                raise LayoutHttpError(
                    "layout_request_invalid",
                    "POST/PUT 必须 application/json（契约 §2）")
            try:
                body = await request.json()
            except Exception as exc:
                raise LayoutHttpError("layout_request_invalid",
                                      f"请求体不是有效 JSON：{exc}") from exc
        query = _query_view(request)
        if route_id in ("template_list", "issue_list") \
                and "cursor" not in query:
            query["cursor"] = None  # 契约 §4：首页 cursor 省略 → 归一化 null
        view = {"route_id": route_id, "method": request.method,
                "path": path_params, "headers": _wire_headers(request),
                "query": query, "body": body}
        validate_request_view(route_id, view)
        # 可选关联头与正文同值核对（不同值 422）
        header_corr = request.headers.get("x-correlation-id")
        body_corr = (body or {}).get("correlation_id") \
            if isinstance(body, dict) else None
        if header_corr and body_corr and header_corr != body_corr:
            raise LayoutHttpError("layout_request_invalid",
                                  "X-Correlation-Id 与正文 correlation_id 不同值")
        header_version = request.headers.get("x-layout-contract-version")
        if header_version and isinstance(body, dict) \
                and body.get("contract_version") \
                and header_version != body["contract_version"]:
            raise LayoutHttpError("layout_request_invalid",
                                  "X-Layout-Contract-Version 与正文版本不同值")
        return view, body

    def _correlation_of(view: dict, body: dict | None) -> str:
        if isinstance(body, dict) and body.get("correlation_id"):
            return str(body["correlation_id"])
        header = (view["headers"] or {}).get("X-Correlation-Id") or \
            (view["headers"] or {}).get("x-correlation-id")
        return header or new_correlation_id()

    def _layout_ok(request: Request, route_id: str, correlation_id: str,
                   payload: dict):
        body = {**payload, "contract_version":
                ROUTE_CONTRACT_VERSIONS[route_id],
                "correlation_id": correlation_id}
        validate_response_view(route_id, 200, {
            "route_id": route_id, "status": 200,
            "headers": {"Content-Type": "application/json",
                        "Cache-Control": "no-store",
                        "X-Correlation-Id": correlation_id,
                        "X-Layout-Contract-Version":
                            ROUTE_CONTRACT_VERSIONS[route_id]},
            "body": body})
        return json_response(body, route_id, correlation_id)

    def _layout_fail(request: Request, exc: Exception):
        """域异常 → 冻结 layout-error.v1（code 闭集映射；不泄漏内部细节）。"""
        import os as _os
        if _os.environ.get("ARM_FLEET_LAYOUT_DEBUG"):
            import traceback as _tb
            _tb.print_exc()
        if isinstance(exc, LayoutHttpError):
            return error_response(new_correlation_id(), exc.code,
                                  exc.message, exc.status)
        code = getattr(exc, "code", "") or "layout_request_invalid"
        status = getattr(exc, "status", None)
        message = str(getattr(exc, "message", exc) or "请求被拒绝")
        return error_response(new_correlation_id(), code, message, status)

    @app.exception_handler(LayoutHttpError)
    async def layout_http_error_handler(request: Request,
                                        exc: LayoutHttpError):
        return _layout_fail(request, exc)

    # ---- 服务装配（束实例一次性接 Runtime 事实源；幂等） ----
    def _region_svc(request: Request):
        svc = _layout_bundle(request)["region_svc"]
        if getattr(svc, "_d2_env_attached", False):
            return svc
        rt_ = rt(request)
        # typed region_preview/region_draft/rollback_preview/操作台账
        # （I 合入时移入 runtime.layout_bundle 构造）
        svc._state = _layout_state(request)
        svc.configure(
            evidence_get=rt_.evidence.get,
            frame_reader=rt_.evidence.read_source_frame_bytes,
            latest_frames=rt_.evidence.latest_source_frames_v2,
            arm_binding=lambda device: _device_arm_binding(rt_, device),
            device_idle=lambda device: rt_.leases.get(device) is None,
            binding_snapshot=lambda device: (
                _binding_facts(rt_, device) or {}).get("camera_binding_id"))
        svc._d2_env_attached = True
        return svc

    def _capture_provider(rt_):
        return make_evidence_capture_provider(
            rt_.evidence,
            arm_binding_getter=lambda device: _device_arm_binding(rt_, device))

    def _active_region(region_store, device_id: str):
        pointer = region_store.get_pointer(device_id)
        if pointer is None:
            return None
        record = region_store.get_version(device_id, pointer["selected_version"])
        if record is None or record.get("device_id") != device_id \
                or record.get("version") != pointer["selected_version"] \
                or not record.get("sha256") \
                or record["sha256"] != pointer["selected_sha256"] \
                or record["sha256"] != content_sha(
                    {k: v for k, v in record.items() if k != "sha256"}):
            raise LayoutHttpError("layout_items_unverified",
                                  "当前生效粗框版本或摘要不一致", 409)
        return record

    def _adaptation_svc(request: Request):
        svc = _layout_bundle(request)["adaptation_svc"]
        if getattr(svc, "_d2_providers_attached", False):
            return svc
        rt_ = rt(request)
        bundle = _layout_bundle(request)
        region_store = bundle["region_store"]

        def region_ref_provider(device_id: str):
            record = _active_region(region_store, device_id)
            if record is None:
                return None
            return {"id": device_id, "version": record["version"],
                    "sha256": record.get("sha256")}

        svc.attach_providers(
            capture_provider=_capture_provider(rt_),
            calibration_provider=lambda device: _calibration_facts(rt_, device),
            binding_provider=lambda device: _binding_facts(rt_, device),
            frame_reader=rt_.evidence.read_source_frame_bytes,
            region_ref_provider=region_ref_provider,
            region_record_provider=lambda device: _active_region(region_store, device))
        svc._d2_providers_attached = True
        return svc

    def _draft_svc(request: Request) -> LayoutDraftService:
        app_cache = getattr(request.app.state, "_d2_draft_svcs", None)
        if app_cache is None:
            app_cache = {}
            request.app.state._d2_draft_svcs = app_cache
        bundle = _layout_bundle(request)
        key = str(bundle["db_path"])
        svc = app_cache.get(key)
        if svc is not None:
            return svc
        rt_ = rt(request)
        bundle["_draft_svc"] = svc = LayoutDraftService(
            bundle["template_store"], bundle["state_store"],
            frame_reader=rt_.evidence.read_source_frame_bytes,
            binding_snapshot_provider=lambda device: _draft_binding_snapshot(
                rt_, device),
            rectifier=_adaptation_svc(request)._rectify_evidence_to_screen)
        app_cache[key] = svc
        return svc

    def _draft_binding_snapshot(rt_, device_id: str) -> dict | None:
        """草稿 binding_snapshot：installation/source/ime/屏幕（v3 事实）。"""
        facts = _binding_facts(rt_, device_id)
        if not facts.get("camera_binding_id") \
                or not facts.get("source_geometry_id") \
                or not facts.get("screen_descriptor"):
            return None
        latest = rt_.evidence.latest_source_frame_v2(device_id) or {}
        size = {"width": int(latest.get("frame_width") or 0),
                "height": int(latest.get("frame_height") or 0)}
        if not size["width"] or not size["height"]:
            return None
        return {"installation_binding": {
                    "camera_id": facts["camera_id"],
                    "camera_device_unique_id":
                        facts["camera_device_unique_id"],
                    "camera_binding_id": facts["camera_binding_id"],
                    "arm_binding_id": facts.get("arm_binding_id")
                    or facts["camera_device_unique_id"]},
                "source_size": size,
                "source_geometry_id": facts["source_geometry_id"],
                "ime_device_id": facts.get("ime_device_id") or device_id,
                "screen_descriptor": facts["screen_descriptor"]}

    def _review_svc(request: Request) -> LayoutReviewService:
        return LayoutReviewService(_layout_store(request),
                                   _layout_state(request))

    def _typed_evidence(request: Request, device_id: str,
                        evidence_id: str) -> dict | None:
        for row in _layout_state(request).state_scan("evidence", device_id):
            if row["value"].get("evidence_id") == evidence_id:
                return row["value"]
        return None

    # ---- 分页（契约 §4：900 秒授权快照、cursor 持久、续页校验身份/route/limit） ----
    PAGINATION_TTL_SECONDS = 900

    def _paginate(request: Request, route_id: str, device_id: str | None,
                  snapshot_items: list, revision: int | None,
                  correlation_id: str, principal: str | None):
        from datetime import datetime, timedelta, timezone
        state = _layout_state(request)
        query = _query_view(request)
        extra = set(query) - {"limit", "cursor"}
        if extra:
            raise LayoutHttpError(
                "layout_request_invalid",
                f"未知查询参数：{', '.join(sorted(extra))}")
        if "limit" not in query:
            raise LayoutHttpError("layout_request_invalid",
                                  "limit 必填（1..100 整数）")
        limit = query["limit"]
        cursor = query.get("cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) \
                or not 1 <= limit <= 100:
            raise LayoutHttpError("layout_request_invalid",
                                  "limit 必须为 1..100 整数")
        now = datetime.now(timezone.utc)
        field = "template_items" if route_id == "template_list" \
            else "issue_items"
        if cursor is not None:
            if not isinstance(cursor, str):
                raise LayoutHttpError("layout_request_invalid",
                                      "cursor 必须为 UUID")
            row = state.state_get("cursor", _cursor_key(cursor))
            if row is None:
                raise LayoutHttpError("layout_resource_missing",
                                      "游标不存在", 404)
            from runtime.util import parse_iso
            if row.get("principal_id") != (principal or ""):
                raise LayoutHttpError("layout_permission_denied",
                                      "游标属于其他身份", 403)
            if row.get("route_id") != route_id \
                    or row.get("device_id") != device_id \
                    or int(row.get("limit") or 0) != limit:
                raise LayoutHttpError("layout_permission_denied",
                                      "身份/路由/设备/limit 不同的续页被拒绝",
                                      403)
            if parse_iso(row["expires_at"]) <= now:
                raise LayoutHttpError("layout_preview_expired",
                                      "快照已过期（900 秒）")
            items = row[field]
            offset = int(row.get("offset") or 0)
            snapshot_at = row["snapshot_at"]
            expires_at = row["expires_at"]
        else:
            items = snapshot_items
            offset = 0
            snapshot_at = iso(now)
            expires_at = iso(now + timedelta(
                seconds=PAGINATION_TTL_SECONDS))
            if principal:
                cursor_id = new_correlation_id()
                record = {"cursor_id": cursor_id,
                          "principal_id": principal,
                          "route_id": route_id, "device_id": device_id,
                          "limit": limit, "offset": 0,
                          "snapshot_at": snapshot_at,
                          "expires_at": expires_at,
                          "template_items":
                              items if route_id == "template_list" else [],
                          "issue_items":
                              items if route_id == "issue_list" else [],
                          "review_revision": int(revision or 0)}
                state.state_insert(
                    "cursor", _cursor_key(cursor_id),
                    device_id, principal, record)
                cursor = cursor_id
            else:
                cursor = None  # 未认证共享列表：单页，不提供续页
        page = items[offset:offset + limit]
        next_offset = offset + len(page)
        next_cursor = None
        if principal and next_offset < len(items):
            next_cursor = new_correlation_id()
            record = {"cursor_id": next_cursor,
                      "principal_id": principal,
                      "route_id": route_id, "device_id": device_id,
                      "limit": limit, "offset": next_offset,
                      "snapshot_at": snapshot_at, "expires_at": expires_at,
                      "template_items":
                          items if route_id == "template_list" else [],
                      "issue_items":
                          items if route_id == "issue_list" else [],
                      "review_revision": int(revision or 0)}
            state.state_insert("cursor", _cursor_key(next_cursor),
                               device_id, principal, record)
        body = {"items": page, "limit": limit, "next_cursor": next_cursor,
                "snapshot_at": snapshot_at, "snapshot_expires_at": expires_at}
        if route_id == "issue_list":
            body["revision"] = int(revision or 0)
        return body

    _LEDGER_ROUTE_BY_KIND = {
        "review": "review", "capture": "capture",
        "adaptation": "adaptation_publish", "region": "region_save",
        "template": "template_publish", "draft": "draft_create",
        "package": "package_export"}

    def _operation_projection(record: dict) -> dict:
        """台账记录 → operation_read（original_response 按冻结 wire:write_response
        联合重建外层 envelope；route/headers 由 resource_kind 推导）。"""
        response = record.get("response")
        route_id = _LEDGER_ROUTE_BY_KIND.get(
            record.get("resource_kind") or "", None)
        original = None
        if response is not None and route_id is not None:
            correlation = (response.get("correlation_id")
                           or new_correlation_id())
            original = {
                "route_id": route_id, "status": 200,
                "headers": {"Content-Type": "application/json",
                            "Cache-Control": "no-store",
                            "X-Correlation-Id": correlation,
                            "X-Layout-Contract-Version":
                                ROUTE_CONTRACT_VERSIONS[route_id]},
                "body": {**response,
                         "contract_version":
                             ROUTE_CONTRACT_VERSIONS[route_id],
                         "correlation_id": correlation}}
        return {"operation_id": record.get("operation_id"),
                "phase": record.get("phase") or "committed",
                "original_response": original,
                "error": record.get("error")}

    def _cursor_key(cursor_id: str) -> str:
        from runtime.layout_state import state_key
        return state_key("cursor", [cursor_id])

    def _template_summaries(request: Request) -> list[dict]:
        # R3-13：统一可见性——半包/未提交模板不出现在列表
        ops = _layout_ops_svc(request)
        summaries = []
        for row in ops.list_visible_templates():
            summaries.append({
                "template_ref": {"id": row["template_id"],
                                 "version": int(row["version"]),
                                 "sha256": row.get("sha256") or ""},
                "parent_version": row.get("parent_version"),
                "compatibility": row.get("compatibility"),
                "created_at": row.get("created_at") or iso(utc_now()),
            })
        summaries.sort(key=lambda item: (item["template_ref"]["id"],
                                         -item["template_ref"]["version"]))
        return summaries

    # ---- R2-15：三个粗框路由（真实证据链；预览检测帧=当前新鲜帧） ----
    @app.get("/v1/devices/{device_id}/phone-search-region")
    async def get_phone_search_region(device_id: str, request: Request):
        try:
            _identity(request, "region_get")
            view, body = await _wire_request(request, "region_get",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, None)
            result = await asyncio.to_thread(
                _region_svc(request).get, device_id)
            return _layout_ok(request, "region_get", correlation, result)
        except LayoutHttpError:
            raise
        except Exception as exc:  # 域异常 → 冻结外壳
            return _layout_fail(request, exc)

    @app.post("/v1/devices/{device_id}/phone-search-region/preview")
    async def preview_phone_search_region(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "region_preview")
            view, body = await _wire_request(
                request, "region_preview", {"device_id": device_id})
            correlation = _correlation_of(view, body)
            result = await asyncio.to_thread(
                _region_svc(request).preview, device_id, body, principal)
            return _layout_ok(request, "region_preview", correlation, result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.put("/v1/devices/{device_id}/phone-search-region")
    async def save_phone_search_region(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "region_save")
            view, body = await _wire_request(request, "region_save",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, body)
            result = await asyncio.to_thread(
                _region_svc(request).save, device_id, body, principal)
            return _layout_ok(request, "region_save", correlation, result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/devices/{device_id}/phone-search-region/versions/{version}")
    async def get_phone_search_region_version(device_id: str, version: int,
                                              request: Request):
        try:
            _identity(request, "region_version")
            view, body = await _wire_request(
                request, "region_version",
                {"device_id": device_id, "version": version})
            correlation = _correlation_of(view, None)
            record = await asyncio.to_thread(
                _region_svc(request).get_version_record, device_id, version)
            if record is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"区域版本 {version} 不存在", 404)
            return _layout_ok(request, "region_version", correlation,
                              {"record": record})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    # ---- 模板读/列表（共享模板只读按现状放行） ----
    @app.get("/v1/layout-templates")
    async def list_layout_templates(request: Request):
        try:
            identity = _identity(request, "template_list")
            view, body = await _wire_request(request, "template_list", {})
            correlation = _correlation_of(view, None)
            items = await asyncio.to_thread(_template_summaries, request)
            result = _paginate(request, "template_list", None, items, None,
                               correlation,
                               identity.principal if identity else None)
            return _layout_ok(request, "template_list", correlation, result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/layout-templates/{template_id}/versions/{version}")
    async def get_template_version(template_id: str, version: int,
                                   request: Request):
        try:
            _identity(request, "template_version")
            view, body = await _wire_request(
                request, "template_version",
                {"template_id": template_id, "version": version})
            correlation = _correlation_of(view, None)
            record = await asyncio.to_thread(
                _layout_ops_svc(request).resolve_template,
                template_id, version)
            if record is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"模板 {template_id} v{version} 不存在",
                                      404)
            return _layout_ok(request, "template_version", correlation,
                              {"record": record})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/layout-templates/{template_id}/versions/{version}/assets/{asset_id}")
    async def get_template_asset(template_id: str, version: int,
                                 asset_id: str, request: Request):
        try:
            _identity(request, "template_asset")
            view, body = await _wire_request(
                request, "template_asset",
                {"template_id": template_id, "version": version,
                 "asset_id": asset_id})
            data = await asyncio.to_thread(
                _layout_ops_svc(request).resolve_template_asset,
                template_id, version, asset_id)
            if data is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"资产 {asset_id} 不存在", 404)
            media_type = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" \
                else "image/jpeg"
            response = binary_response(data, media_type, zip_download=False)
            response.headers["X-Correlation-Id"] = _correlation_of(view, None)
            response.headers["X-Layout-Contract-Version"] = \
                ROUTE_CONTRACT_VERSIONS["template_asset"]
            return response
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    # ---- 模板草稿链（冻结 ui-layout-edit.v1） ----
    @app.post("/v1/layout-templates")
    async def publish_layout_template(request: Request):
        try:
            principal = _require_principal(request, "template_publish")
            view, body = await _wire_request(request, "template_publish", {})
            correlation = _correlation_of(view, body)
            result = await asyncio.to_thread(
                _draft_svc(request).publish, body, principal)
            return _layout_ok(request, "template_publish", correlation,
                              result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.post("/v1/layout-template-drafts")
    async def create_layout_draft(request: Request):
        try:
            principal = _require_principal(request, "draft_create")
            view, body = await _wire_request(request, "draft_create", {})
            correlation = _correlation_of(view, body)
            result = await asyncio.to_thread(
                _draft_svc(request).create, body, principal)
            return _layout_ok(request, "draft_create", correlation, result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/layout-template-drafts/{draft_id}")
    async def get_layout_draft(draft_id: str, request: Request):
        try:
            principal = _require_principal(request, "draft_get")
            view, body = await _wire_request(request, "draft_get",
                                             {"draft_id": draft_id})
            correlation = _correlation_of(view, None)
            result = await asyncio.to_thread(
                _draft_svc(request).get, draft_id, principal)
            return _layout_ok(request, "draft_get", correlation, result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.put("/v1/layout-template-drafts/{draft_id}/metadata")
    async def update_draft_metadata(draft_id: str, request: Request):
        try:
            principal = _require_principal(request, "draft_metadata")
            view, body = await _wire_request(request, "draft_metadata",
                                             {"draft_id": draft_id})
            correlation = _correlation_of(view, body)
            result = await asyncio.to_thread(
                _draft_svc(request).update_metadata, draft_id, body,
                principal)
            return _layout_ok(request, "draft_metadata", correlation,
                              {"operation_id": result["operation_id"],
                               "draft": result["draft"],
                               "replayed": result["replayed"]})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.put("/v1/layout-template-drafts/{draft_id}/pages/{state}")
    async def write_draft_page(draft_id: str, state: str, request: Request):
        try:
            principal = _require_principal(request, "draft_page")
            view, body = await _wire_request(
                request, "draft_page", {"draft_id": draft_id, "state": state})
            correlation = _correlation_of(view, body)
            result = await asyncio.to_thread(
                _draft_svc(request).write_page, draft_id, state, body,
                principal)
            return _layout_ok(request, "draft_page", correlation,
                              {"operation_id": result["operation_id"],
                               "revision": result["revision"],
                               "page": result["page"],
                               "replayed": result["replayed"]})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.post("/v1/layout-template-drafts/{draft_id}/assets")
    async def upload_draft_asset(draft_id: str, request: Request):
        try:
            principal = _require_principal(request, "draft_asset_upload")
            view, body = await _wire_request(request, "draft_asset_upload",
                                             {"draft_id": draft_id})
            correlation = _correlation_of(view, body)
            result = await asyncio.to_thread(
                _draft_svc(request).upload_asset, draft_id, body, principal)
            return _layout_ok(request, "draft_asset_upload", correlation,
                              {"operation_id": result["operation_id"],
                               "revision": result["revision"],
                               "asset": result["asset"],
                               "replayed": result["replayed"]})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/layout-template-drafts/{draft_id}/assets/{asset_id}")
    async def get_draft_asset(draft_id: str, asset_id: str, request: Request):
        try:
            principal = _require_principal(request, "draft_asset_get")
            view, body = await _wire_request(
                request, "draft_asset_get",
                {"draft_id": draft_id, "asset_id": asset_id})
            data = await asyncio.to_thread(
                _layout_bundle(request)["template_store"].get_asset,
                draft_id, asset_id)
            if data is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"资产 {asset_id} 不存在", 404)
            media_type = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" \
                else "image/jpeg"
            response = binary_response(data, media_type, zip_download=False)
            response.headers["X-Correlation-Id"] = _correlation_of(view, None)
            response.headers["X-Layout-Contract-Version"] = \
                ROUTE_CONTRACT_VERSIONS["draft_asset_get"]
            return response
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    # ---- 采集/证据/适配会话链（device-layout-edit.v1 / layout-capture.v1） ----
    @app.post("/v1/devices/{device_id}/layout-captures")
    async def create_layout_capture(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "capture")
            view, body = await _wire_request(request, "capture",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, body)
            body = dict(body)
            # R2-23：作者身份由服务端注入（正文不参与；冻结 body 无 principal）
            body.pop("principal_id", None)
            body["principal_id"] = principal
            result = await asyncio.to_thread(
                _adaptation_svc(request).capture, device_id, body)
            return _layout_ok(request, "capture", correlation,
                              {"operation_id": result["operation_id"],
                               "capture": result["capture"],
                               "replayed": False})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/devices/{device_id}/layout-evidence/{evidence_id}")
    async def get_layout_evidence(device_id: str, evidence_id: str,
                                  request: Request):
        try:
            principal = _require_principal(request, "evidence_get")
            view, body = await _wire_request(
                request, "evidence_get",
                {"device_id": device_id, "evidence_id": evidence_id})
            correlation = _correlation_of(view, None)
            record = await asyncio.to_thread(
                _typed_evidence, request, device_id, evidence_id)
            if record is None:
                raise LayoutHttpError(
                    "layout_resource_missing",
                    "证据不存在或不属于该设备", 404)
            return _layout_ok(request, "evidence_get", correlation,
                              {"evidence": record})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.post("/v1/devices/{device_id}/layout-adaptation/sessions")
    async def create_adaptation_session(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "session_create")
            view, body = await _wire_request(request, "session_create",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, body)
            body = dict(body)
            body.pop("principal_id", None)
            body["principal_id"] = principal
            record = await asyncio.to_thread(
                _adaptation_svc(request).start_session, device_id, body)
            result = await asyncio.to_thread(
                _session_view_frozen, request, record)
            return _layout_ok(request, "session_create", correlation,
                              {"operation_id": new_correlation_id(),
                               "session": result,
                               "required_states": list(PAGE_STATES_ORDER),
                               "replayed": False})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    PAGE_STATES_ORDER = ("wechat_list", "chat_ime_hidden", "chat_ime_empty",
                         "chat_ime_text")

    def _session_view_frozen(request: Request, record: dict) -> dict:
        """D1 会话记录 → 冻结 layout:session 形状（补区域指针/绑定快照/兼容）。"""
        device_id = record["device_id"]
        region_store = _layout_bundle(request)["region_store"]
        region = _active_region(region_store, device_id)
        region_ref = None
        if region is not None:
            region_ref = {"id": device_id, "version": region["version"],
                          "sha256": region.get("sha256")}
        facts = _binding_facts(rt(request), device_id)
        binding_snapshot = None
        if facts.get("camera_binding_id"):
            size = None
            latest = rt(request).evidence.latest_source_frame_v2(device_id)
            if latest:
                size = {"width": int(latest["frame_width"]),
                        "height": int(latest["frame_height"])}
            binding_snapshot = {
                "installation_binding": {
                    "camera_id": facts["camera_id"],
                    "camera_device_unique_id":
                        facts["camera_device_unique_id"],
                    "camera_binding_id": facts["camera_binding_id"],
                    "arm_binding_id": facts.get("arm_binding_id")
                    or facts["camera_device_unique_id"]},
                "source_size": size,
                "source_geometry_id": facts["source_geometry_id"],
                "ime_device_id": facts.get("ime_device_id") or device_id,
                "screen_descriptor": facts.get("screen_descriptor")}
        template = _adaptation_svc(request)._resolve_template(
            record["template_id"], record["template_version"])
        pages = []
        for state in PAGE_STATES_ORDER:
            entry = (record.get("pages") or {}).get(state)
            pages.append({"state": state,
                          "capture": _page_capture_frozen(entry, record)
                          if entry else None})
        # 会话颜色资产记录（hidden/empty 空白输入参考图；服务端真实裁图）
        import hashlib as _hashlib
        assets = []
        assets_dir = (_layout_state(request).data_root / "devices" /
                      device_id / "layout_adaptation" / "sessions" /
                      record["capture_session_id"] / "assets")
        if assets_dir.is_dir():
            import cv2 as _cv2
            import numpy as _np
            for path in sorted(assets_dir.glob("*.png")):
                data = path.read_bytes()
                image = _cv2.imdecode(_np.frombuffer(data, dtype=_np.uint8),
                                      _cv2.IMREAD_COLOR)
                if image is None:
                    continue
                height, width = image.shape[:2]
                assets.append({
                    "asset": {"id": path.stem,
                              "relative_path": f"assets/{path.name}",
                              "sha256": _hashlib.sha256(
                                  data).hexdigest(),
                              "media_type": "image/png",
                              "width": width, "height": height},
                    "byte_size": len(data),
                    "privacy_reviewed_by": record["principal_id"],
                    "privacy_reviewed_at": record["created_at"],
                    "source_evidence_ids": []})
        view = {
            "capture_session_id": record["capture_session_id"],
            "device_id": device_id,
            "principal_id": record["principal_id"],
            "revision": record["revision"],
            "template_ref": record["template_ref"],
            "installation_region_ref": region_ref,
            "binding_snapshot": binding_snapshot,
            "compatibility_snapshot":
                (template or {}).get("compatibility"),
            "keyboard_geometry_mode": record["keyboard_geometry_mode"],
            "expected_pointer_revision": record.get(
                "expected_pointer_revision", 0),
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "server_instance_id": record["server_instance_id"],
            "created_monotonic_ms": record.get("created_monotonic_ms", 0),
            "pages": pages,
            "assets": assets,
        }
        # configuration 摘要以冻结视图自身为输入（两遍装配后统一回填，
        # 保证 session_page 页内摘要与 GET 响应逐字节一致）
        configuration = content_sha(
            {key: view[key] for key in (
                "device_id", "template_ref", "installation_region_ref",
                "binding_snapshot", "compatibility_snapshot",
                "keyboard_geometry_mode")})
        for page in pages:
            if page["capture"] is not None:
                page["capture"]["configuration_sha256"] = configuration
        return view

    def _page_capture_frozen(entry: dict, session: dict) -> dict:
        capture = entry.get("capture") or {}
        check = capture.get("check") or {}
        accepted = capture.get("accepted_at")
        accepted_ms = None
        evidence_ids = list(capture.get("evidence_ids") or [])
        return {
            "accepted_at": accepted,
            "server_instance_id": session["server_instance_id"],
            "accepted_monotonic_ms": accepted_ms or 0,
            "configuration_sha256": None,  # 由 _session_view_frozen 回填
            "capture_id": capture.get("capture_id"),
            "check": check,
            "local_overrides": list(entry.get("local_overrides") or []),
            "color_sample": entry.get("color_sample"),
        }

    @app.get("/v1/devices/{device_id}/layout-adaptation/sessions/{session_id}")
    async def get_adaptation_session(device_id: str, session_id: str,
                                     request: Request):
        try:
            principal = _require_principal(request, "session_get")
            view, body = await _wire_request(
                request, "session_get",
                {"device_id": device_id, "session_id": session_id})
            correlation = _correlation_of(view, None)
            record = await asyncio.to_thread(
                _adaptation_svc(request).get_session, device_id, session_id)
            if record is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"会话 {session_id} 不存在", 404)
            session_view = await asyncio.to_thread(_session_view_frozen,
                                                   request, record)
            from runtime.util import parse_iso
            now = utc_now()
            page_status = []
            for page in session_view["pages"]:
                capture = page["capture"]
                if capture is None:
                    page_status.append({"state": page["state"],
                                        "status": "not_captured",
                                        "expires_at": None,
                                        "reason_codes": []})
                    continue
                check = capture.get("check") or {}
                accepted = parse_iso(capture["accepted_at"])
                age = (now - accepted).total_seconds()
                status = ("passed" if check.get("status") == "passed"
                          else "failed")
                reason_codes = list(check.get("reason_codes") or [])
                if age > 900:
                    status, reason_codes = "expired", ["evidence_expired"]
                page_status.append({
                    "state": page["state"], "status": status,
                    "expires_at": capture.get("accepted_at"),
                    "reason_codes": reason_codes})
            return _layout_ok(request, "session_get", correlation,
                              {"session": session_view,
                               "evaluated_at": iso(now),
                               "page_status": page_status})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.put("/v1/devices/{device_id}/layout-adaptation/sessions/{session_id}/pages/{state}")
    async def write_adaptation_page(device_id: str, session_id: str,
                                    state: str, request: Request):
        try:
            principal = _require_principal(request, "session_page")
            view, body = await _wire_request(
                request, "session_page",
                {"device_id": device_id, "session_id": session_id,
                 "state": state})
            correlation = _correlation_of(view, body)
            body = dict(body)
            body.pop("principal_id", None)
            # D1 write_page 的封闭字段集是业务字段；公共字段由 wire 层校验后剥离
            body.pop("contract_version", None)
            body.pop("correlation_id", None)
            result = await asyncio.to_thread(
                _adaptation_svc(request).write_page, device_id, session_id,
                state, body)
            record = await asyncio.to_thread(
                _adaptation_svc(request)._load_session, session_id)
            frozen = await asyncio.to_thread(_session_view_frozen, request,
                                            record or {})
            page_entry = next((p for p in frozen["pages"]
                               if p["state"] == state), {})
            page_frozen = page_entry.get("capture")
            return _layout_ok(request, "session_page", correlation,
                              {"operation_id": result["operation_id"],
                               "session_revision": result["session_revision"],
                               "page": {"state": state,
                                        "capture": page_frozen},
                               "replayed": False})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.post("/v1/devices/{device_id}/layout-adaptation/preview")
    async def preview_layout_adaptation(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "adaptation_preview")
            view, body = await _wire_request(request, "adaptation_preview",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, body)
            body = dict(body)
            body.pop("principal_id", None)
            record = await asyncio.to_thread(
                _adaptation_svc(request).preview, device_id, body)

            def _frozen_check(check: dict) -> dict:
                return {key: check[key] for key in (
                    "state", "status", "evidence_ids", "anchor_results",
                    "item_results", "reason_codes") if key in check}

            page_checks = [_frozen_check(record["page_checks"][state])
                           for state in PAGE_STATES_ORDER
                           if state in record.get("page_checks", {})]
            return _layout_ok(request, "adaptation_preview", correlation, {
                "preview_id": record["preview_id"],
                "page_checks": page_checks,
                "proposed_overrides": [],
                "accepted_items": record.get("accepted_items") or [],
                "created_at": record["created_at"],
                "expires_at": record["expires_at"]})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.put("/v1/devices/{device_id}/layout-adaptation")
    async def publish_layout_adaptation(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "adaptation_publish")
            view, body = await _wire_request(request, "adaptation_publish",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, body)
            body = dict(body)
            body.pop("principal_id", None)
            body["principal_id"] = principal
            result = await asyncio.to_thread(
                _adaptation_svc(request).publish, device_id, body)
            return _layout_ok(request, "adaptation_publish", correlation, {
                "operation_id": result["operation_id"],
                "revision": result["revision"],
                "adaptation_ref": result["adaptation_ref"],
                "per_item_status": result["per_item_status"],
                "replayed": result["replayed"]})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/devices/{device_id}/layout-adaptation")
    async def get_layout_adaptation(device_id: str, request: Request):
        try:
            _identity(request, "adaptation_get")
            view, body = await _wire_request(request, "adaptation_get",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, None)
            svc = _adaptation_svc(request)
            state = _layout_state(request)
            pointer = state.adaptation_pointer_get(device_id)
            record = None
            if pointer and pointer.get("version"):
                record = await asyncio.to_thread(
                    svc.get_version, device_id, int(pointer["version"]))
            page_checks = []
            if record is not None:
                raw = record.get("page_checks") or {}
                if isinstance(raw, list):
                    page_checks = raw
                else:
                    page_checks = [raw[state] for state in PAGE_STATES_ORDER
                                   if state in raw]
            failed = _failed_keys(page_checks)
            facts = _binding_facts(rt(request), device_id)
            calibration_ok = _calibration_facts(rt(request), device_id) \
                is not None
            binding_changed = False
            if record is not None:
                bound = record.get("installation_region_ref")
                current = _layout_bundle(request)["region_store"] \
                    .latest_version(device_id)
                binding_changed = bool(
                    bound and current[1] and
                    current[1].get("sha256") != bound.get("sha256"))
            if record is None:
                status = "unregistered"
            elif failed or binding_changed:
                status = "needs_review"
            else:
                status = "verified"
            execution_blocked = not calibration_ok
            execution_reasons = ([] if calibration_ok
                                 else ["calibration_unavailable"])
            return _layout_ok(request, "adaptation_get", correlation, {
                "revision": int(pointer["revision"]) if pointer else 0,
                "selected_ref": ({"id": device_id,
                                  "version": int(pointer["version"]),
                                  "sha256": pointer["sha256"]}
                                 if pointer and pointer.get("version")
                                 else None),
                "status": status,
                "page_checks": page_checks,
                "blocking_reasons": [key["item_id"] for key in failed],
                "execution_blocked": execution_blocked,
                "execution_blocking_reasons": execution_reasons})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @staticmethod
    def _failed_keys(page_checks):  # noqa: N805（函数内静态 helper）
        return [key
                for check in page_checks
                for key in [{"item_id": result["id"]}
                            for result in
                            check.get("anchor_results", [])
                            + check.get("item_results", [])
                            if result.get("status") == "failed"]]

    @app.get("/v1/devices/{device_id}/layout-adaptation/versions/{version}")
    async def get_adaptation_version(device_id: str, version: int,
                                     request: Request):
        try:
            _identity(request, "adaptation_version")
            view, body = await _wire_request(
                request, "adaptation_version",
                {"device_id": device_id, "version": version})
            correlation = _correlation_of(view, None)
            record = await asyncio.to_thread(
                _adaptation_svc(request).get_version, device_id, version)
            if record is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"适配版本 {version} 不存在", 404)
            return _layout_ok(request, "adaptation_version", correlation,
                              {"record": record})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/devices/{device_id}/layout-adaptation/versions/{version}/assets/{asset_id}")
    async def get_adaptation_asset(device_id: str, version: int,
                                   asset_id: str, request: Request):
        try:
            _identity(request, "adaptation_asset")
            view, body = await _wire_request(
                request, "adaptation_asset",
                {"device_id": device_id, "version": version,
                 "asset_id": asset_id})
            # D1 遗留：本台颜色资产链（session.assets/adaptation_asset 索引）
            # 尚未生成行；当前只有已发布版本目录中的资产可读。
            data_root = _layout_state(request).data_root
            path = (data_root / "devices" / device_id /
                    "layout_adaptation" / "versions" / str(version) /
                    "assets" / f"{asset_id}.png")
            data = path.read_bytes() if path.exists() else None
            if data is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"适配资产 {asset_id} 不存在", 404)
            media_type = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" \
                else "image/jpeg"
            response = binary_response(data, media_type, zip_download=False)
            response.headers["X-Correlation-Id"] = _correlation_of(view, None)
            response.headers["X-Layout-Contract-Version"] = \
                ROUTE_CONTRACT_VERSIONS["adaptation_asset"]
            return response
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    # ---- 资料问题/复核/操作查询 ----
    @app.get("/v1/devices/{device_id}/layout-review/issues")
    async def list_layout_review_issues(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "issue_list")
            view, body = await _wire_request(request, "issue_list",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, None)
            svc = _review_svc(request)
            items = await asyncio.to_thread(svc.list_issues, device_id, None)
            revision = await asyncio.to_thread(svc.collection_revision,
                                               device_id)
            result = _paginate(request, "issue_list", device_id, items,
                               revision, correlation, principal)
            return _layout_ok(request, "issue_list", correlation, result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.post("/v1/devices/{device_id}/layout-review")
    async def submit_layout_review(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "review")
            view, body = await _wire_request(request, "review",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, body)
            rt_ = rt(request)

            def evidence_check(evidence_id: str):
                try:
                    return rt_.evidence.get(evidence_id)
                except Exception:
                    return None

            result = await asyncio.to_thread(
                _review_svc(request).resolve, device_id, body, principal,
                evidence_check=evidence_check)
            operation = _layout_state(request).operation_get_by_request(
                body["client_request_id"])
            return _layout_ok(request, "review", correlation, {
                "operation_id": (operation or {}).get("operation_id")
                or new_correlation_id(),
                "revision": result["revision"],
                "results": result["results"],
                "replayed": result["replayed"]})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/layout-operations/by-request/{client_request_id}")
    async def get_layout_operation_by_request(client_request_id: str,
                                              request: Request):
        try:
            principal = _require_principal(request, "operation_request")
            view, body = await _wire_request(
                request, "operation_request",
                {"client_request_id": client_request_id})
            correlation = _correlation_of(view, None)
            record = await asyncio.to_thread(
                _layout_state(request).operation_get_by_request,
                client_request_id)
            if record is None:
                raise LayoutHttpError("layout_resource_missing",
                                      "操作记录不存在", 404)
            if record.get("principal_id") != principal:
                raise LayoutHttpError("layout_permission_denied",
                                      "操作属于其他身份", 403)
            return _layout_ok(request, "operation_request", correlation,
                              _operation_projection(record))
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/layout-operations/{operation_id}")
    async def get_layout_operation(operation_id: str, request: Request):
        try:
            principal = _require_principal(request, "operation_get")
            view, body = await _wire_request(request, "operation_get",
                                             {"operation_id": operation_id})
            correlation = _correlation_of(view, None)
            record = await asyncio.to_thread(
                _layout_state(request).operation_get, operation_id)
            if record is None:
                # legacy 台账（导入/导出等旧记录）回退
                legacy = await asyncio.to_thread(
                    _layout_ops_svc(request).get_operation, operation_id)
                record = legacy
            if record is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"操作 {operation_id} 不存在", 404)
            principal_id = record.get("principal_id")
            if principal_id is not None and principal_id != principal:
                raise LayoutHttpError("layout_permission_denied",
                                      "操作属于其他身份", 403)
            if "phase" not in record:
                # legacy KV 台账（导入/导出旧记录）回退投影
                projection = _layout_ops_svc(request).projection(record) or {}
                return _layout_ok(request, "operation_get", correlation, {
                    "operation_id": operation_id,
                    "phase": projection.get("phase", "committed"),
                    "original_response": None,
                    "error": projection.get("error")})
            return _layout_ok(request, "operation_get", correlation,
                              _operation_projection(record))
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    # ---- 资料包（导出/读取/下载/导入；闭包逐资源授权） ----
    @app.post("/v1/layout-packages/export")
    async def export_layout_package(request: Request):
        try:
            principal = _require_principal(request, "package_export")
            view, body = await _wire_request(request, "package_export", {})
            correlation = _correlation_of(view, body)
            svc = _layout_ops_svc(request)
            # 闭包逐资源授权（契约 §2）：根与其引用的设备资料必须属于本人；
            # 模板为共享资料（任何已认证身份可导出）
            for root in body.get("roots") or []:
                kind = root.get("kind")
                ref = root.get("ref") or {}
                if kind == "template":
                    continue
                record = (svc.resolve_adaptation(str(ref.get("id")),
                                                 int(ref.get("version") or 0))
                          if kind == "adaptation"
                          else svc.resolve_region(str(ref.get("id")),
                                                  int(ref.get("version") or 0)))
                if record is None:
                    raise LayoutHttpError(
                        "layout_resource_missing",
                        f"{kind} {ref.get('id')} v{ref.get('version')} 不存在",
                        404)
                owner = (record.get("published_by")
                         or record.get("created_by")
                         or record.get("reviewed_by"))
                if owner is not None and owner != principal:
                    raise LayoutHttpError(
                        "layout_permission_denied",
                        "导出闭包包含属于其他身份的设备资料", 403)
            result = await asyncio.to_thread(
                svc.export_package, principal,
                {"roots": body.get("roots") or [],
                 "client_request_id": body["client_request_id"]})
            operation_id = await _register_export_operation(
                request, principal, body, result)
            return _layout_ok(request, "package_export", correlation, {
                "operation_id": operation_id,
                "package_id": result["package_id"],
                "manifest": result["manifest"],
                "archive_sha256": result["archive_sha256"],
                "archive_byte_size": result["size_bytes"],
                "replayed": False})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    async def _register_export_operation(request: Request, principal: str,
                                         body: dict, result: dict) -> str:
        def _register():
            state = _layout_state(request)
            operation = state.operation_prepare(
                principal_id=principal, device_id=None,
                resource_kind="package", resource_id=result["package_id"],
                client_request_id=body["client_request_id"],
                request={k: v for k, v in body.items()
                         if k not in ("correlation_id",)},
                planned_response={"package_id": result["package_id"]},
                files=[], pointer_before=None, pointer_after=None,
                mutations=[], version_ref=None)
            state.operation_commit(operation["operation_id"], response={
                "package_id": result["package_id"]})
            return operation["operation_id"]

        return await asyncio.to_thread(_register)

    def _package_record(request: Request, package_id: str):
        return _layout_ops_svc(request).get_package(package_id)

    @app.get("/v1/layout-packages/{package_id}")
    async def get_layout_package(package_id: str, request: Request):
        try:
            principal = _require_principal(request, "package_get")
            view, body = await _wire_request(request, "package_get",
                                             {"package_id": package_id})
            correlation = _correlation_of(view, None)
            record = await asyncio.to_thread(_package_record, request,
                                             package_id)
            if record is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"包 {package_id} 不存在", 404)
            if record.get("created_by") not in (None, principal):
                raise LayoutHttpError("layout_permission_denied",
                                      "包属于其他身份", 403)
            return _layout_ok(request, "package_get", correlation, {
                "operation_id": new_correlation_id(),
                "package_id": record["package_id"],
                "manifest": record["manifest"],
                "archive_sha256": record["archive_sha256"],
                "archive_byte_size": record["size_bytes"],
                "replayed": False})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.get("/v1/layout-packages/{package_id}/download")
    async def download_layout_package(package_id: str, request: Request):
        try:
            principal = _require_principal(request, "package_download")
            view, body = await _wire_request(request, "package_download",
                                             {"package_id": package_id})
            record = await asyncio.to_thread(_package_record, request,
                                             package_id)
            if record is None:
                raise LayoutHttpError("layout_resource_missing",
                                      f"包 {package_id} 不存在", 404)
            if record.get("created_by") not in (None, principal):
                raise LayoutHttpError("layout_permission_denied",
                                      "包属于其他身份", 403)
            blob = await asyncio.to_thread(
                _layout_ops_svc(request).read_package_bytes, package_id)
            if blob is None:
                raise LayoutHttpError("layout_archive_invalid",
                                      "包文件缺失或损坏", 503)
            response = binary_response(blob, "application/zip",
                                       zip_download=True)
            response.headers["X-Correlation-Id"] = _correlation_of(view, None)
            response.headers["X-Layout-Contract-Version"] = \
                ROUTE_CONTRACT_VERSIONS["package_download"]
            return response
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.post("/v1/layout-packages/import/preview")
    async def preview_layout_package_import(request: Request):
        try:
            principal = _require_principal(request, "package_import_preview")
            view, body = await _wire_request(request,
                                             "package_import_preview", {})
            correlation = _correlation_of(view, body)
            import base64 as _b64
            try:
                blob = _b64.b64decode(body.get("archive_base64") or "")
            except Exception as exc:
                raise LayoutHttpError("layout_request_invalid",
                                      f"archive_base64 无效：{exc}") from exc
            svc = _layout_ops_svc(request)
            record = await asyncio.to_thread(
                svc.import_preview, principal, blob,
                body.get("privacy_confirmed") is True,
                body.get("client_request_id"))
            return _layout_ok(request, "package_import_preview",
                              correlation, {
                                  "preview_id": record["preview_id"],
                                  "manifest": record["manifest"],
                                  "archive_sha256": record["archive_sha256"],
                                  "items": record["items"],
                                  "can_import": record["can_import"],
                                  "created_at": record["created_at"],
                                  "expires_at": record["expires_at"]})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.post("/v1/layout-packages/import")
    async def import_layout_package(request: Request):
        try:
            principal = _require_principal(request, "package_import")
            view, body = await _wire_request(request, "package_import", {})
            correlation = _correlation_of(view, body)
            svc = _layout_ops_svc(request)
            result = await asyncio.to_thread(
                svc.import_commit, principal,
                str(body.get("preview_id") or ""),
                client_request_id=body.get("client_request_id"),
                archive_sha256=body.get("archive_sha256"))
            return _layout_ok(request, "package_import", correlation, {
                "operation_id": new_correlation_id(),
                "package_id": result.get("package_id"),
                "items": result.get("items") or [],
                "pointers_changed": bool(
                    result.get("pointers_changed")),
                "replayed": bool(result.get("replayed"))})
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    # ---- 回滚（契约 §8；R3-16：按 kind 显式路由——region 走既有合法链，
    #      adaptation 走新四态核验回滚：仅推进 current revision，不改历史） ----
    def _rollback_kind(body: dict) -> str:
        return (body or {}).get("kind") or (body or {}).get("resource") \
            or "phone_search_region"

    @app.post("/v1/devices/{device_id}/layout-rollback/preview")
    async def preview_layout_rollback(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "rollback_preview")
            view, body = await _wire_request(request, "rollback_preview",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, body)
            if _rollback_kind(body) in ("layout_adaptation", "adaptation"):
                result = await asyncio.to_thread(
                    _layout_ops_svc(request).rollback_preview, device_id,
                    body, principal)
            else:
                # 粗框回滚既有合法链（kind=phone_search_region 闭集）
                result = await asyncio.to_thread(
                    _region_svc(request).rollback_preview, device_id, body,
                    principal)
            return _layout_ok(request, "rollback_preview", correlation,
                              result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    @app.put("/v1/devices/{device_id}/layout-rollback")
    async def commit_layout_rollback(device_id: str, request: Request):
        try:
            principal = _require_principal(request, "rollback")
            view, body = await _wire_request(request, "rollback",
                                             {"device_id": device_id})
            correlation = _correlation_of(view, body)
            if _rollback_kind(body) in ("layout_adaptation", "adaptation"):
                result = await asyncio.to_thread(
                    _layout_ops_svc(request).rollback_commit, device_id,
                    body, principal,
                    str(body.get("client_request_id") or ""))
            else:
                result = await asyncio.to_thread(
                    _region_svc(request).rollback_commit, device_id, body,
                    principal)
            return _layout_ok(request, "rollback", correlation, result)
        except LayoutHttpError:
            raise
        except Exception as exc:
            return _layout_fail(request, exc)

    # ---- END 布局 35 路由 ----

    @app.get("/v1/devices/{device_id}/calibrated-scroll")
    async def get_calibrated_scroll(device_id: str, request: Request):
        """calibrated-scroll.v1 只读能力查询（规格 §5.2）：零动作、不发证、不写校准。"""
        if len(request.query_params):
            raise ApiError("invalid_request",
                           f"未知查询参数：{', '.join(request.query_params.keys())}；本端点不接受查询参数")
        rt_ = rt(request)
        from runtime.calibrated_scroll import capability_view
        return capability_view(rt_, device_id)

    @app.post("/v1/devices/{device_id}/calibration-status")
    async def calibration_status(device_id: str, request: Request):
        body = await request.json()
        result = rt(request).devices.set_calibration_status(
            device_id, str(body.get("status") or ""), str(body.get("reason") or "人工更新校准状态"))
        return device_view(rt(request), result)

    @app.get("/v1/devices/{device_id}")
    async def get_device(device_id: str, request: Request):
        rt_ = rt(request)
        return device_view(rt_, rt_.devices.get(device_id))

    @app.get("/v1/devices/{device_id}/timeline")
    async def device_timeline(device_id: str, request: Request, limit: int = 50):
        rt_ = rt(request)
        rt_.devices.get(device_id)
        audit_events = rt_.audit.list_events(limit=limit, device_id=device_id)
        tasks = [t for t in rt_.store.all_tasks() if t["device_id"] == device_id]
        entries = [
            {"at": e["occurred_at"], "kind": "audit", "kind_display": "运维/审计",
             "action_display": e["action_display"], "operator_id": e.get("operator_id"),
             "reason": e.get("reason"), "outcome_display": e.get("outcome_display")}
            for e in audit_events
        ] + [
            {"at": t["created_at"], "kind": "task", "kind_display": "任务",
             "action_display": f"任务 {t['task_id']}（{t.get('task_type_display')}）",
             "status": t["status"], "status_display": t["status_display"], "task_id": t["task_id"]}
            for t in tasks
        ]
        entries.sort(key=lambda e: e.get("at") or "", reverse=True)
        return {"device_id": device_id, "timeline": entries[:limit]}

    @app.post("/v1/devices/{device_id}/pause")
    async def pause_device(device_id: str, request: Request):
        body = await request.json()
        result = rt(request).op_pause(device_id, op_context(body))
        return {"device_id": device_id, "status": result["status"], "status_display": result["status_display"]}

    @app.post("/v1/devices/{device_id}/resume")
    async def resume_device(device_id: str, request: Request):
        body = await request.json()
        result = rt(request).op_resume(device_id, op_context(body))
        return {"device_id": device_id, "status": result["status"], "status_display": result["status_display"]}

    @app.post("/v1/devices/{device_id}/quarantine")
    async def quarantine_device(device_id: str, request: Request):
        body = await request.json()
        result = rt(request).op_quarantine(device_id, op_context(body))
        return {"device_id": device_id, "status": result["status"], "status_display": result["status_display"]}

    @app.post("/v1/devices/{device_id}/unquarantine")
    async def unquarantine_device(device_id: str, request: Request):
        body = await request.json()
        result = rt(request).op_unquarantine(device_id, op_context(body))
        return {"device_id": device_id, "status": result["status"], "status_display": result["status_display"]}

    @app.post("/v1/devices/{device_id}/home-check")
    async def home_check(device_id: str, request: Request):
        body = await request.json()
        result = await rt(request).op_home_check(device_id, op_context(body))
        return result

    @app.post("/v1/devices/{device_id}/reset-home")
    async def reset_home(device_id: str, request: Request):
        body = await request.json()
        result = rt(request).op_reset_home(device_id, op_context(body))
        return result

    @app.post("/v1/devices/{device_id}/status-query")
    async def status_query(device_id: str, request: Request):
        body = await request.json()
        result = await rt(request).op_status_query(device_id, op_context(body))
        return result

    # ---------- 业务托管 ----------
    @app.get("/v1/devices/{device_id}/supervision")
    async def get_supervision(device_id: str, request: Request):
        return rt(request).supervision.view(device_id)

    @app.get("/v1/devices/{device_id}/supervision/events")
    async def supervision_events(device_id: str, request: Request):
        return {"device_id": device_id, "events": rt(request).supervision.events(device_id)}

    @app.put("/v1/devices/{device_id}/supervision/config")
    async def configure_supervision(device_id: str, request: Request):
        body = await request.json()
        context = op_context(body)
        result = rt(request).op_supervision_configure(device_id, body.get("allowed_users", []), context)
        return result

    @app.post("/v1/devices/{device_id}/supervision/start")
    async def start_supervision(device_id: str, request: Request):
        body = await request.json()
        result = await rt(request).op_supervision_start(device_id, op_context(body))
        return result

    @app.post("/v1/devices/{device_id}/supervision/stop")
    async def stop_supervision(device_id: str, request: Request):
        body = await request.json()
        return rt(request).op_supervision_stop(device_id, op_context(body))

    @app.post("/v1/devices/{device_id}/supervision/exit-conversation")
    async def exit_supervision_conversation(device_id: str, request: Request):
        body = await request.json()
        context = op_context(body)
        result = await rt(request).supervision.exit_current_conversation(device_id)
        rt(request).audit.record("supervision_exit_conversation",
                                 operator_id=context["operator_id"], device_id=device_id,
                                 reason=context["reason"])
        return result

    # ---------- 急停 ----------
    @app.post("/v1/emergency-stop")
    async def emergency_stop(request: Request):
        body = await request.json()
        result = rt(request).op_estop(op_context(body, default_role="operator"))
        return {"estop_active": result["estop_active"], "already_active": result.get("already_active", False)}

    @app.post("/v1/emergency-stop/reset")
    async def emergency_stop_reset(request: Request):
        body = await request.json()
        result = await rt(request).op_estop_reset(op_context(body))
        return {"estop_active": result["estop_active"]}

    # ---------- 任务 ----------
    @app.get("/v1/tasks")
    async def list_tasks(request: Request, device_id: str | None = None, status: str | None = None, task_type: str | None = None):
        tasks = rt(request).list_tasks(device_id=device_id, status=status, task_type=task_type)
        return {"tasks": [task_view(rt(request), t) for t in tasks[:200]]}

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        return task_view(rt(request), rt(request).get_task(task_id))

    # ---------- 可选扩展：业务导航服务签名通道（extensions.wx_navigation） ----------
    # 平台核心不硬编码该通道的端点与契约；探测到扩展包才挂载（SDK 纯净发行
    # 剔除 extensions/ 即得无业务导航通道形态），挂载时端点与钩子行为不变。
    try:
        from extensions.wx_navigation import routes as wx_nav
    except ImportError:
        wx_nav = None
    signed_task_contracts = tuple(wx_nav.SIGNED_TASK_CONTRACTS) if wx_nav else ()
    if wx_nav is not None:
        wx_nav.register(app, rt=rt)

    @app.post("/v1/tasks")
    async def submit_task(request: Request):
        rt_ = rt(request)
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8"))
        contract = str(payload.get("contract_version") or "")
        if wx_nav is not None and contract in wx_nav.SIGNED_TASK_CONTRACTS:
            # Q02：业务导航合同分支走服务签名认证；来源恒为 wx，
            # body.source/role 不参与认证（§7.2）。逻辑在可选扩展内。
            wx_nav.verify_signed_submission(rt_, request, payload, raw_body, contract)
        payload = json.loads(json.dumps(payload))
        # 来源判定：显式 source 优先；业务契约自带来源归属由可选扩展登记
        # （如 chat-send.*→wx），确保结果回报到对应业务系统。
        body_source = payload.pop("source", None)
        contract_source = wx_nav.contract_source_hint(payload) if wx_nav else None
        source = body_source or contract_source \
            or ("wx" if contract in signed_task_contracts else "console")
        context = {
            "operator_id": payload.get("submitted_by") or payload.get("operator_id") or source,
            "role": "wx" if source == "wx" else "operator",
            "reason": payload.get("reason") or ("WX Outbox 已批准发送" if source == "wx" else "外部系统提交任务"),
            "correlation_id": payload.get("correlation_id", ""),
            "client_request_id": payload.get("client_request_id") or payload.get("idempotency_key", ""),
        }
        task = rt_.submit_task(payload, source=source, context=context)
        return {"task_id": task["task_id"], "status": task["status"],
                "status_display": task["status_display"], "task_type_display": task["task_type_display"],
                "created_at": task["created_at"]}

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, request: Request):
        body = await request.json()
        task = await rt(request).cancel_task(task_id, op_context(body))
        return {"task_id": task_id, "status": task["status"], "status_display": task["status_display"]}

    @app.post("/v1/tasks/{task_id}/mark-reviewed")
    async def mark_reviewed(task_id: str, request: Request):
        body = await request.json()
        task = rt(request).mark_task_reviewed(task_id, op_context(body))
        return {"task_id": task_id, "status": task["status"], "status_display": task["status_display"],
                "reviewed_by": task.get("reviewed_by")}

    @app.post("/v1/tasks/{task_id}/review-context")
    async def review_task_context(task_id: str, request: Request):
        body = await request.json()
        if isinstance(body, dict) and wx_nav is not None \
                and body.get("contract_version") == wx_nav.REVIEW_CONTEXT_CONTRACT:
            # R2-05：标读恢复核对精确分支（服务签名+安全证据→持久回执），
            # 由可选扩展提供；与旧 role=operator 分支互斥。
            return wx_nav.handle_signed_review_context(rt(request), task_id, body)
        return rt(request).review_task_context(
            task_id, op_context(body),
            expected_device_id=str(body.get("expected_device_id") or ""),
            expected_task_status=str(body.get("expected_task_status") or ""),
            review_context=body.get("review_context"))

    @app.get("/v1/devices/{device_id}/calibrated-scroll/reviews")
    async def list_calibrated_scroll_reviews(device_id: str, request: Request):
        """R2-07（滑动附件 §7.4）：视觉/未知问题清单（只查表，零动作）。"""
        from runtime.calibrated_scroll import get_scroll_action_store
        rt_ = rt(request)
        rt_.devices.get(device_id)
        issues = get_scroll_action_store(rt_).issues(device_id)
        return {"contract_version": "calibrated-scroll-review.v1",
                "device_id": device_id, "issues": issues}

    @app.post("/v1/devices/{device_id}/calibrated-scroll/reviews/{issue_id}/review")
    async def review_calibrated_scroll(device_id: str, issue_id: str,
                                       request: Request):
        """R2-07：人工核对视觉暂停（幂等账本；零机械动作；不自动恢复）。"""
        from runtime.calibrated_scroll import (ScrollActionStoreError,
                                               get_scroll_action_store)
        from runtime.permissions import check_operation
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("invalid_request", "请求体必须为 JSON 对象")
        context = op_context(body)
        check_operation("input_settings_update", context)
        if context.get("role") in ("wx", "readonly"):
            raise ApiError("permission_denied", "wx/readonly 身份不能执行滑动人工核对")
        rt_ = rt(request)
        rt_.devices.get(device_id)
        try:
            return get_scroll_action_store(rt_).review_action(
                device_id, issue_id, request=body,
                reviewed_by=context.get("operator_id") or "")
        except ScrollActionStoreError as exc:
            raise ApiError(exc.code, exc.message)

    # ---------- 设备安全视图（服务签名只读；帧桥换绑门的数据源） ----------
    @app.get("/v1/devices/{device_id}/safety-view")
    async def get_device_safety_view(device_id: str, request: Request):
        """device-safety-view.v1（服务签名 GET，只读零动作）：换绑/排空前核对
        持久在途任务、未解决机械/输入未知、动作租约与急停。帧桥
        （scripts/camera_frame_server）在安装锁内消费本视图，任一未知即
        保守拒绝。密钥经 FLEET_SAFETY_KEYS_FILE 配置（与帧桥侧同文件）。"""
        import os as _os
        from runtime import service_signing

        keys_file = _os.environ.get("FLEET_SAFETY_KEYS_FILE") or ""
        if not keys_file or not Path(keys_file).exists():
            raise ApiError("authorization_unavailable",
                           "缺少安全视图服务 key 配置（FLEET_SAFETY_KEYS_FILE）")
        keys = service_signing.load_keys(keys_file)
        rt_ = rt(request)
        service_signing.ensure_service_auth_schema(rt_.store._conn)
        try:
            service_signing.verify_service_signature(
                rt_.store._conn, keys, "GET",
                request.url.path + (f"?{request.url.query}"
                                    if request.url.query else ""),
                b"", request.headers)
        except service_signing.ServiceAuthError as exc:
            raise ApiError("authorization_unavailable", exc.message) \
                if exc.status >= 500 else ApiError("authorization_invalid", exc.message)
        rt_.devices.get(device_id)
        terminal = {"confirmed", "failed", "unknown", "cancelled", "expired"}
        tasks = rt_.store.all_tasks()
        in_flight = sorted(t["task_id"] for t in tasks
                           if t.get("device_id") == device_id
                           and t.get("status") not in terminal)
        unresolved = sorted(t["task_id"] for t in tasks
                            if t.get("device_id") == device_id
                            and (t.get("status") == "unknown"
                                 or t.get("stop_result_unknown")
                                 or t.get("motion_result_unknown")))
        lease = rt_.leases.get(device_id)
        estop = rt_.estop.active
        blockers: list[str] = []
        if in_flight:
            blockers.append("device_busy")
        if unresolved:
            blockers.append("original_task_unresolved")
        if lease is not None or estop:
            blockers.append("fleet_safety_blocked")
        device = rt_.devices.get(device_id)
        view = {
            "contract_version": "device-safety-view.v1",
            "device_id": device_id,
            "binding": None,
            "snapshot_revision": max(1, int(device.get("binding_revision") or 1)),
            "observed_at": iso(utc_now()),
            "in_flight_task_ids": in_flight,
            "unresolved_task_ids": unresolved,
            "held_session_id": None,
            "lease_active": lease is not None,
            "mechanical_safe": "unknown" if blockers else "confirmed",
            "blocking_reasons": sorted(set(blockers)),
        }
        rt_.audit.record("device_safety_view", operator_id="frame-bridge",
                         device_id=device_id, reason="换绑/排空前安全核对",
                         detail={"in_flight": len(in_flight),
                                 "unresolved": len(unresolved),
                                 "mechanical_safe": view["mechanical_safe"]})
        return view

    # ---------- R3-17：手机配对管理（本机调试台后端机器身份转发） ----------
    def _pairing_svc(request: Request):
        from runtime.ime_pairing import ImePairingError
        rt_ = rt(request)
        svc = getattr(rt_, "ime_pairing", None)
        if svc is None:
            raise ApiError("ime_pairing_unavailable",
                           "配对身份库未初始化（先经离线工具 init_ime_pairing 建立）")
        return svc

    def _pairing_error(exc: "ImePairingError") -> JSONResponse:  # noqa: F821
        return JSONResponse(status_code=exc.status, content={
            "contract_version": "ime-pairing.v1", "request_id": None,
            "error": {"code": exc.code, "message": exc.message}})

    def _require_pairing_admin(request: Request):
        """本机机器身份边界（身份附件 L2/P2）：仅回环 + 布局转发凭据。
        手机/WX/Helper 身份互不替代；缺凭据拒绝，不降级为任意请求可用。"""
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "testclient"):  # testclient=TestClient 回环语义
            raise ApiError("permission_denied", "配对管理仅限本机回环调用")
        import os as _os
        token_file = _os.environ.get("ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE")
        if not token_file:
            return  # 隔离开发/测试部署：回环内放行（部署模板要求生产配置）
        from runtime.layout_auth import load_forwarder_token
        expected = load_forwarder_token(token_file)
        presented = request.headers.get("x-layout-forwarder-token") or ""
        import hmac as _hmac
        if not expected or not _hmac.compare_digest(presented, expected):
            raise ApiError("permission_denied", "缺少有效的本机转发凭据")

    @app.post("/v1/ime-pairing/invitations")
    async def create_pairing_invitation(request: Request, body: dict = None):
        from runtime.ime_pairing import ImePairingError
        try:
            _require_pairing_admin(request)
            svc = _pairing_svc(request)
            payload = body if isinstance(body, dict) else {}
            rt_ = rt(request)
            trust = getattr(rt_, "ime_pairing_trust", None) or {}
            if not trust.get("https_origin") or not trust.get("spki_sha256"):
                raise ImePairingError(
                    "persistence_unavailable",
                    "配对信任材料未配置（部署输入：https_origin/spki_sha256）")
            result = svc.create_invitation(
                request_id=str(payload.get("request_id") or ""),
                invite_secret=str(payload.get("invite_secret") or ""),
                arm_id=payload.get("arm_id"),
                source="console",
                trust=trust)
            return JSONResponse(
                status_code=200 if result.get("replayed") else 201,
                content=result)
        except ImePairingError as exc:
            return _pairing_error(exc)

    @app.get("/v1/ime-pairing/invitations/{invitation_id}")
    async def get_pairing_invitation(invitation_id: str, request: Request):
        from runtime.ime_pairing import ImePairingError
        try:
            _require_pairing_admin(request)
            return _pairing_svc(request).get_invitation(invitation_id)
        except ImePairingError as exc:
            return _pairing_error(exc)

    @app.post("/v1/ime-pairing/invitations/{invitation_id}/revoke")
    async def revoke_pairing_invitation(invitation_id: str, request: Request,
                                        body: dict = None):
        from runtime.ime_pairing import ImePairingError
        try:
            _require_pairing_admin(request)
            payload = body if isinstance(body, dict) else {}
            return _pairing_svc(request).revoke_invitation(
                invitation_id, request_id=str(payload.get("request_id") or ""))
        except ImePairingError as exc:
            return _pairing_error(exc)

    @app.get("/v1/ime-phones")
    async def list_pairing_phones(request: Request):
        _require_pairing_admin(request)
        return {"contract_version": "ime-pairing.v1",
                "items": _pairing_svc(request).list_phones()}

    @app.post("/v1/ime-phones/{ime_device_id}/revoke")
    async def revoke_pairing_phone(ime_device_id: str, request: Request,
                                   body: dict = None):
        from runtime.ime_pairing import ImePairingError
        try:
            _require_pairing_admin(request)
            payload = body if isinstance(body, dict) else {}
            return _pairing_svc(request).revoke_phone(
                ime_device_id,
                request_id=str(payload.get("request_id") or ""),
                expected_revision=int(payload.get("expected_revision") or 0),
                reason=str(payload.get("reason") or ""))
        except ImePairingError as exc:
            return _pairing_error(exc)

    # ---------- 能力 / 审计 / 告警 / 人工处理 ----------
    @app.get("/v1/capabilities")
    async def list_capabilities(request: Request):
        return {"capabilities": rt(request).capabilities.all()}

    @app.get("/v1/audit-events")
    async def audit_events(request: Request, limit: int = 100, device_id: str | None = None):
        return {"events": rt(request).audit.list_events(limit=limit, device_id=device_id)}

    @app.get("/v1/alerts")
    async def list_alerts(request: Request, include_resolved: bool = False):
        return {"alerts": rt(request).store.all_alerts(include_resolved=include_resolved)}

    @app.post("/v1/alerts/{alert_id}/resolve")
    async def resolve_alert(alert_id: str, request: Request):
        body = await request.json()
        result = rt(request).op_alert_resolve(alert_id, op_context(body))
        return {"alert_id": alert_id, "resolved": result["resolved"]}

    @app.get("/v1/takeovers")
    async def takeovers(request: Request):
        return {"items": rt(request).takeovers()}

    @app.post("/v1/takeovers")
    async def request_takeover(request: Request):
        """明确人工接管：先由 Arm Fleet 审计，再转发到 WX 事件接口。"""
        body = await request.json()
        from runtime.permissions import check_operation

        required = (
            "device_id", "conversation_id", "customer_id", "operator_id",
        "reason", "correlation_id", "client_request_id", "contract_version",
        )
        for field in required:
            if not str(body.get(field) or "").strip():
                raise ApiError("missing_field", f"人工接管缺少必填字段：{field}")
        context = op_context(body)
        check_operation("human_takeover_request", context)
        try:
            schema = json.loads((CONTRACT_DIR / "human-takeover.v1.schema.json").read_text(encoding="utf-8"))
            jsonschema.validate(body, schema)
        except jsonschema.ValidationError as exc:
            raise ApiError("invalid_request", f"human-takeover.v1 契约校验失败：{exc.message}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ApiError("invalid_request", "人工接管契约文件不可用，请检查部署文件") from exc
        if body["contract_version"] != "human-takeover.v1":
            raise ApiError("invalid_request", "人工接管契约版本不受支持")
        return await rt(request).request_human_takeover(
            device_id=str(body["device_id"]),
            conversation_id=str(body["conversation_id"]),
            customer_id=str(body["customer_id"]),
            operator_id=context["operator_id"],
            reason=context["reason"],
            correlation_id=context["correlation_id"],
            client_request_id=context["client_request_id"],
            context=dict(body.get("context") or {}),
        )

    # ---------- 插件 ----------
    @app.get("/v1/plugins")
    async def list_plugins(request: Request):
        return {"plugins": rt(request).plugins.all()}

    @app.get("/v1/plugins/{plugin_id}")
    async def get_plugin(plugin_id: str, request: Request):
        return rt(request).plugins.get(plugin_id)

    @app.post("/v1/plugins/install")
    async def install_plugin(request: Request):
        rt_ = rt(request)
        body = await request.json()
        source_dir = body.get("source_dir")
        if not source_dir:
            raise ApiError("missing_field", "缺少 source_dir（插件源目录）")
        record = rt_.plugins.install(source_dir, operator_id=body.get("operator_id", "操作员"),
                                     reason=body.get("reason", "安装插件"))
        return {"plugin_id": record["plugin_id"], "status": record["status"], "status_display": record["status_display"]}

    @app.post("/v1/plugins/{plugin_id}/activate")
    async def activate_plugin(plugin_id: str, request: Request):
        body = await request.json()
        record = rt(request).plugins.activate(plugin_id, operator_id=body.get("operator_id", "操作员"), reason=body.get("reason", "启用插件"))
        return {"plugin_id": plugin_id, "status": record["status"], "status_display": record["status_display"]}

    @app.post("/v1/plugins/{plugin_id}/deactivate")
    async def deactivate_plugin(plugin_id: str, request: Request):
        body = await request.json()
        record = rt(request).plugins.deactivate(plugin_id, operator_id=body.get("operator_id", "操作员"), reason=body.get("reason", "停用插件"))
        return {"plugin_id": plugin_id, "status": record["status"], "status_display": record["status_display"]}

    @app.post("/v1/plugins/{plugin_id}/uninstall")
    async def uninstall_plugin(plugin_id: str, request: Request):
        body = await request.json()
        result = rt(request).plugins.uninstall(plugin_id, operator_id=body.get("operator_id", "操作员"), reason=body.get("reason", "卸载插件"))
        return result

    @app.post("/v1/plugins/{plugin_id}/upgrade")
    async def upgrade_plugin(plugin_id: str, request: Request):
        body = await request.json()
        source_dir = body.get("source_dir")
        if not source_dir:
            raise ApiError("missing_field", "缺少 source_dir（新版插件目录）")
        record = rt(request).plugins.upgrade(plugin_id, source_dir, operator_id=body.get("operator_id", "操作员"), reason=body.get("reason", "升级插件"))
        return {"plugin_id": plugin_id, "version": record["version"], "status_display": record["status_display"]}

    @app.post("/v1/plugins/{plugin_id}/rollback")
    async def rollback_plugin(plugin_id: str, request: Request):
        body = await request.json()
        record = rt(request).plugins.rollback(plugin_id, operator_id=body.get("operator_id", "操作员"), reason=body.get("reason", "回滚插件"))
        return {"plugin_id": plugin_id, "version": record["version"], "status_display": record["status_display"]}

    # ---------- 实时画面查看（只读） ----------
    def _view_operator(request, body):
        """R4-12-D（S2 余项）：查看会话身份只信可信反代在边界重建的
        X-WX-Operator/X-WX-Operator-Role；客户端自报 requested_by/role 仅作
        展示回显，不作为操作者归属或授权依据。无可信头（直连/未配置代理）
        时身份恒为 direct-unverified、角色恒 readonly。

        R4-12-E（wx-camera-binding.v1）：可信反代同时给出 X-WX-Session-Id
        （WX 登录会话摘要）与 X-WX-Credential-Revision（凭据代际）时返回
        绑定四元组，查看会话入库绑定——此后接受 WX 推送撤销与周期授权
        复核（退出/禁用/重置/自然到期 → 已建流 fail-closed 停止）。
        """
        trusted_op = (request.headers.get("X-WX-Operator") or "").strip()
        trusted_role = (request.headers.get("X-WX-Operator-Role") or "").strip()
        wx_session = (request.headers.get("X-WX-Session-Id") or "").strip()
        wx_credrev = (request.headers.get("X-WX-Credential-Revision") or "").strip()
        if trusted_op:
            binding = None
            if wx_session:
                binding = {"username": trusted_op, "session_id": wx_session,
                           "credential_revision": wx_credrev or 0}
            return trusted_op, (trusted_role or "readonly"), True, binding
        return "direct-unverified", "readonly", False, None

    @app.post("/v1/camera/view-sessions")
    async def create_view_session(request: Request):
        rt_ = rt(request)
        body = await request.json()
        for field in ("device_id", "purpose", "requested_by", "correlation_id", "client_request_id"):
            if not body.get(field):
                raise ApiError("missing_field", f"创建查看会话缺少必填字段：{field}")
        if body["purpose"] not in ("主动查看", "接管查看"):
            raise ApiError("invalid_request", "purpose 只允许「主动查看」或「接管查看」")
        operator, operator_role, trusted, wx_binding = _view_operator(request, body)
        if body["purpose"] == "接管查看" and not trusted:
            raise ApiError("identity_unverified",
                           "接管查看需要可信代理身份（X-WX-Operator），"
                           "不接受客户端自报 requested_by/role")
        session = rt_.views.create(
            device_id=body["device_id"], camera_id=body.get("camera_id"),
            purpose=body["purpose"], requested_by=operator,
            correlation_id=body["correlation_id"], client_request_id=body["client_request_id"],
            operator_role=operator_role, wx_binding=wx_binding,
        )
        session = await rt_.views.refresh_camera_status(session["view_session_id"])
        return rt_.views.contract_view(session)

    @app.get("/v1/camera/view-sessions/{view_session_id}")
    async def get_view_session(view_session_id: str, request: Request):
        views = rt(request).views
        return views.contract_view(await views.refresh_camera_status(view_session_id))

    @app.post("/v1/camera/view-sessions/{view_session_id}/renew")
    async def renew_view_session(view_session_id: str, request: Request):
        body = await request.json()
        _op, _role, _trusted, _binding = _view_operator(request, body)
        # R4-12-E：续期前强制授权复核——已失效绑定不得靠续期复活。
        await rt(request).views.recheck_wx_authorization(
            view_session_id, force=True)
        session = rt(request).views.renew(view_session_id, requested_by=_op,
                                          correlation_id=body.get("correlation_id"))
        session = await rt(request).views.refresh_camera_status(view_session_id)
        return rt(request).views.contract_view(session)

    @app.delete("/v1/camera/view-sessions/{view_session_id}")
    async def close_view_session(view_session_id: str, request: Request):
        body = await request.json()
        _op, _role, _trusted, _binding = _view_operator(request, body)
        session = rt(request).views.close(view_session_id, requested_by=_op,
                                          reason=body.get("reason", "前端正常关闭查看会话"))
        return rt(request).views.contract_view(session)

    @app.post("/v1/internal/wx-revocations")
    async def wx_revocation_push(request: Request):
        """R4-12-E：WX 授权失效推送（退出/禁用/重置/改密）。

        信任：X-WX-Internal-Token 与 wx_camera_token_file 常量时间比对；
        未配置令牌文件=端点关闭（403），不提供无令牌模式。
        作用域：精确 (username, session_ids)——迟到/重试幂等，不撤其他用户
        与新登录代际。原始凭据不落日志。
        """
        import hmac as _hmac

        rt_ = rt(request)
        expected = rt_.views._wx_camera_token()
        provided = (request.headers.get("X-WX-Internal-Token") or "").strip()
        if not expected or not provided or not _hmac.compare_digest(
                expected, provided):
            raise ApiError("internal_unauthorized",
                           "内部撤销通知未授权：令牌缺失、不匹配或本节点未配置"
                           "（wx_camera_token_file）")
        body = await request.json()
        username = str(body.get("username") or "").strip()
        session_ids = body.get("session_ids")
        if (not username or len(username) > 64
                or not isinstance(session_ids, list) or not session_ids
                or len(session_ids) > 100
                or any(not isinstance(x, str) or not x or len(x) > 128
                       for x in session_ids)):
            raise ApiError("invalid_request",
                           "username/session_ids 不合法（session_ids 1-100 个，"
                           "每项非空字符串 ≤128 字符）")
        reason = str(body.get("reason") or "WX 授权失效")[:200]
        closed = rt_.views.revoke_wx_sessions(session_ids, username, reason)
        return {"contract_version": "wx-camera-binding.v1",
                "revoked_view_sessions": closed}

    @app.get("/v1/camera/view-sessions/{view_session_id}/frame")
    async def view_session_frame(view_session_id: str, request: Request):
        """最新单帧（仿真用 PNG；前端轮询渲染，兼容所有浏览器）。"""
        rt_ = rt(request)
        session, frame = await rt_.views.capture_frame(view_session_id)
        if session["status"] in ("closed", "expired"):
            raise ApiError("view_session_expired", f"查看会话已{session['status_display']}")
        if session["status"] == "unavailable" or frame is None:
            raise ApiError("camera_unavailable", "摄像头画面异常，查看会话标记为画面不可用")
        return Response(content=frame.get("image_bytes", frame.get("image")), media_type="image/png",
                        headers={"X-Frame-At": frame["captured_at"], "X-Frame-Sha256": frame["sha256"]})

    @app.get("/v1/camera/view-sessions/{view_session_id}/stream")
    async def view_session_stream(view_session_id: str, request: Request):
        """授权查看会话的 MJPEG 流；优先走可选高帧率媒体平面，失败回退旧链路。"""
        rt_ = rt(request)
        session = rt_.views.get(view_session_id)
        if session["status"] in ("closed", "expired"):
            raise ApiError("view_session_expired", f"查看会话已{session['status_display']}")
        if await preview_service_available(rt_):
            preview_url = (
                f"{preview_service_base(rt_)}/cameras/"
                f"{session['camera_id']}/preview.mjpeg"
            )

            async def preview_frames():
                _loop = asyncio.get_running_loop()
                _status_refresh_s = max(
                    0.0, float(rt_.config.get("preview_status_refresh_seconds", 1.0)))
                import httpx

                timeout = httpx.Timeout(None, connect=0.5)
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        async with client.stream(
                            "GET",
                            preview_url,
                            headers={
                                "X-View-Session-Id": view_session_id,
                                "X-Device-Id": session["device_id"],
                            },
                        ) as upstream:
                            if upstream.status_code != 200:
                                rt_.views.mark_unavailable(view_session_id, "画面不可用：预览采集异常")
                                return
                            # R8-F1/R4-12-E.6：看门狗不得取消 httpx 底层读——
                            # wait_for 直接取消 aiter.__anext__ 会破坏流状态，
                            # 上游停帧>1s 后流死亡（误杀）。改为按需拉取协议：
                            # 独立生产者任务只在消费者放行（信号量）后拉下一
                            # chunk——上游副作用/生成推进与旧逐 __anext__ 时序
                            # 一致（B6 语义：状态检查先于下一帧生成）；看门狗
                            # 只对队列读取计时（可超时重试，不动上游连接）。
                            _DONE = object()
                            _ERR = object()
                            _queue: asyncio.Queue = asyncio.Queue(maxsize=1)
                            _demand = asyncio.Semaphore(0)  # 按需放行：每次刷新状态后放行一帧拉取

                            _aiter = upstream.aiter_bytes()

                            async def _produce():
                                try:
                                    while True:
                                        await _demand.acquire()
                                        try:
                                            chunk = await _aiter.__anext__()
                                        except StopAsyncIteration:
                                            await _queue.put(_DONE)
                                            return
                                        await _queue.put(chunk)
                                except (httpx.HTTPError, OSError):
                                    await _queue.put(_ERR)
                                except asyncio.CancelledError:
                                    raise

                            _producer = asyncio.create_task(_produce())
                            last_status_check = 0.0
                            pending_demand = False  # 已放行未交付
                            try:
                                while True:
                                    if await request.is_disconnected():
                                        break
                                    # B6（2026-10-01 backlog）：≈30fps 热路径上逐
                                    # chunk 刷新相机状态（取帧+画质分析+落库）是
                                    # 写放大——按 preview_status_refresh_seconds
                                    # 节流（默认 1s；0=逐 chunk，仿真测试用）。
                                    now = _loop.time()
                                    if now - last_status_check >= _status_refresh_s:
                                        last_status_check = now
                                        current = await rt_.views.refresh_camera_status(
                                            view_session_id)
                                        # R4-12-E：业务绑定会话周期授权复核
                                        # （自然到期/推送失败的兜底通道）。
                                        current = await rt_.views.recheck_wx_authorization(
                                            view_session_id)
                                        if current["status"] in ("closed", "expired",
                                                                 "unavailable"):
                                            break
                                    # 放行下一帧拉取（在状态检查之后——上游
                                    # 生成副作用不早于本帧状态确认）。
                                    if not pending_demand:
                                        _demand.release()
                                        pending_demand = True
                                    # R8-F1：等待有界——上游停帧时借超时窗
                                    # 复核会话（只对队列读计时，不动上游连接）。
                                    try:
                                        item = await asyncio.wait_for(
                                            _queue.get(), timeout=1.0)
                                    except asyncio.TimeoutError:
                                        last_status_check = 0.0  # 强制下一轮立即复核
                                        continue
                                    pending_demand = False  # 已交付，下轮重新放行
                                    if item is _DONE:
                                        rt_.views.mark_unavailable(
                                            view_session_id,
                                            "画面不可用：预览流已断开")
                                        break
                                    if item is _ERR:
                                        rt_.views.mark_unavailable(
                                            view_session_id,
                                            "画面不可用：预览采集异常")
                                        break
                                    # 交付前复查（基线契约：上游拉取副作用可
                                    # 发生在本帧拉取期间——终态帧不转发；
                                    # 相机质量变化也在此时重导出 waiting→valid）。
                                    current = await rt_.views.refresh_camera_status(
                                        view_session_id)
                                    if current["status"] in ("closed", "expired",
                                                             "unavailable"):
                                        break
                                    yield item
                            finally:
                                _producer.cancel()
                except (httpx.HTTPError, OSError):
                    rt_.views.mark_unavailable(view_session_id, "画面不可用：预览采集异常")
                    return

            return StreamingResponse(
                preview_frames(),
                media_type="multipart/x-mixed-replace; boundary=preview",
            )

        interval = float(rt_.config.get("camera_frame_interval_seconds", 0.5))

        async def frames():
            boundary = "armfleetframe"
            yield b"--" + boundary.encode() + b"\r\n"
            while True:
                if await request.is_disconnected():
                    break
                current = rt_.views.get(view_session_id)
                if current["status"] in ("closed", "expired", "unavailable"):
                    break
                # R4-12-E：回退分支同样周期授权复核（无 preview 服务时撤销
                # 传播不缺席）；内部节流，非绑定会话零网络开销。
                current = await rt_.views.recheck_wx_authorization(view_session_id)
                if current["status"] in ("closed", "expired", "unavailable"):
                    break
                current, frame = await rt_.views.capture_frame(view_session_id)
                if current["status"] in ("closed", "expired", "unavailable") or frame is None:
                    break
                image = frame.get("image_bytes", frame.get("image"))
                yield (
                    f"Content-Type: image/png\r\nContent-Length: {len(image)}\r\nX-Frame-At: {frame['captured_at']}\r\n\r\n"
                ).encode() + image + b"\r\n--" + boundary.encode() + b"\r\n"
                await asyncio.sleep(interval)

        return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary=armfleetframe")

    # ---------- SSE ----------
    @app.get("/v1/events/stream")
    async def event_stream(request: Request):
        rt_ = rt(request)
        queue = rt_.events.subscribe()

        async def events():
            yield "event: connected\ndata: {\"message\": \"已连接 Arm Fleet 事件流\"}\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"event: {event['event_type']}\ndata: {payload}\n\n"
            finally:
                rt_.events.unsubscribe(queue)

        return StreamingResponse(events(), media_type="text/event-stream")

    return app
