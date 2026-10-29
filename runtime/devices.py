"""设备管理器：注册、自检、心跳、摄像头检查、状态机与运维迁移。

不变量：
- Arm Fleet 是设备物理状态、租约、锁、能力和健康状态的唯一可写事实源。
- 设备账号/槽位变更必须暂停设备、二次确认、追加审计后重新注册。
- 摄像头遮挡/断开/过曝/冻结必须被识别并告警，不能当作可用。
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from drivers import FakeArmDriver, FakeCameraDriver, FakeInputDriver
from drivers.camera_broker import (
    CameraQualityCheckError, quality_diagnostics, quality_failure_diagnostics,
)
from runtime.arm_binding import normalize_daemon_binding
from runtime.capabilities import DEFAULT_DEVICE_CAPABILITIES
from runtime.errors import ApiError
from runtime.status import (
    CAMERA_QUALITY,
    DEVICE_STATUS,
    DEVICE_MODE,
    UI_STATE,
    display,
    pair,
)
from runtime.util import iso, new_id, parse_iso, utc_now

# 允许的状态迁移表（状态机）
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "starting": {"ready", "degraded", "offline", "quarantined", "maintenance", "paused"},
    "ready": {"busy", "paused", "quarantined", "offline", "degraded", "returning_home", "maintenance", "starting"},
    "busy": {"ready", "returning_home", "paused", "quarantined", "offline", "degraded", "recovering"},
    "returning_home": {"ready", "degraded", "quarantined", "paused", "offline"},
    "offline": {"starting", "maintenance", "quarantined", "paused"},
    "degraded": {"ready", "offline", "quarantined", "paused", "maintenance", "starting"},
    "paused": {"ready", "quarantined", "maintenance", "offline"},
    "quarantined": {"recovering", "maintenance"},
    "recovering": {"ready", "quarantined", "degraded", "paused"},
    "maintenance": {"ready", "offline", "starting", "paused"},
    "registered": {"starting"},
}


class DeviceManager:
    def __init__(self, store, audit, events, hardware_mode: str = "simulation", mac_mini_id: str = "mac-mini-01",
                 hotplug_discovery_enabled: bool = False, discovery_interval_seconds: float = 2.0,
                 camera_broker_max_age_seconds: float = 1.5, config_root=None):
        from runtime.config import PROJECT_ROOT

        self._config_root = Path(config_root).resolve() if config_root else PROJECT_ROOT
        self._store = store
        self._audit = audit
        self._events = events
        self.hardware_mode = hardware_mode
        self.mac_mini_id = mac_mini_id
        self._devices: dict[str, dict] = {}
        self._lifecycle_versions: dict[str, int] = {}
        self._arms: dict[str, FakeArmDriver | object] = {}
        self._cameras: dict[str, FakeCameraDriver | object] = {}
        self._camera_brokers: dict[str, object] = {}
        self.camera_broker_max_age_seconds = max(0.0, float(camera_broker_max_age_seconds))
        self._inputs: dict[str, FakeInputDriver | object] = {}
        self._heartbeat_task: asyncio.Task | None = None
        self._camera_task: asyncio.Task | None = None
        self._last_camera_hash: dict[str, str] = {}
        self._running = False
        self._ime_status_provider = None
        self._layout_observation_provider = None
        self._region_context_provider = None
        self._registered_configs: list[dict] = []
        self._discovery_task: asyncio.Task | None = None
        self._hotplug_discovery_enabled = bool(hotplug_discovery_enabled)
        self._discovery_interval_seconds = max(0.5, float(discovery_interval_seconds))
        self._last_presence: dict[str, bool] = {}
        self._known_discovery_fingerprints: set[str] = set()
        # 换口重枚举（2026-09-30）：记录每台绑定设备的硬件身份三元组
        # （vid/pid/serial，跨端口稳定），绑定串口消失时按身份找回新端口。
        self._last_serial_identity: dict[str, tuple] = {}
        self._startup_holds: dict[str, str] = {}
        self._leases = None  # 由 runtime 注入：拔出/重绑定时撤销租约
        self._persist_configs = None  # 由 runtime 注入：绑定变更写回绑定表
        # FIX-04：换手机流程需要 InputControl（binding_change 意图 + 系统关闭）。
        # 由 Runtime 装配段注入；未注入时换手机必须拒绝，不得静默走旧路径。
        self._input_control = None

    def set_input_control(self, input_control) -> None:
        """注入输入控制实例：换手机流程落 binding_change 意图并系统关闭原许可。"""
        self._input_control = input_control

    def input_settings_known_check(self, device_id: str):
        """FIX-04-1：已知输入设备判据（供 input_settings 拒绝空白重建）。

        判据（任一成立即已知设备）：设置文件存在；存在迁移维护意图/备份；
        Fleet DB 存在本设备任何输入控制记录（owner/effect/guard/settings_write）。
        """
        from runtime import input_settings as _iset

        def known() -> bool:
            try:
                root = self.settings_data_root(device_id)
                if _iset.settings_path(device_id, root).exists():
                    return True
                if _iset.known_device_by_maintenance(device_id, root):
                    return True
            except Exception:
                pass
            try:
                for kind in ("owner", "effect", "settings_write"):
                    if self._store.input_records_by_device(device_id, kind):
                        return True
                # 只有空 guard（状态投影/换绑预检惰性创建、无任何问题/检查点/
                # 换绑意图）不构成「已知设备」；否则一次只读查询就能让设备
                # 永远无法首次建立设置文件。
                guard = self._store.input_record_get(f"guard:{device_id}")
                if isinstance(guard, dict):
                    if guard.get("issues") or guard.get("settings_checkpoint") \
                            or guard.get("binding_change") or guard.get("review_requests"):
                        return True
            except Exception:
                pass
            return False

        return known

    def set_lease_manager(self, leases) -> None:
        """注入租约管理器：设备拔出/重绑定/禁用时必须撤销活动租约。"""
        self._leases = leases

    def set_config_persistence(self, update_fn) -> None:
        """注入按设备持久化函数 ``update_fn(device_id, changes, *, create_if_missing,
        create_seed) -> dict``。

        2026-10-29：由"整表保存"改为"锁内单台读改写"，否则调用方拿旧整表覆盖磁盘
        会回滚别人刚写的值（丢更新）。实现见 runtime.config.update_device_config。
        """
        self._persist_configs = update_fn

    def _release_device_lease(self, device_id: str, reason: str) -> None:
        """撤销设备当前活动租约（拔出/重绑定/禁用时调用）。"""
        if self._leases is None:
            return
        lease = self._leases.get(device_id)
        if lease is None:
            return
        self._leases.release(device_id, lease["lease_id"])
        self._audit.record("device_remove", device_id=device_id,
                           reason=f"已撤销设备租约：{reason}",
                           detail={"lease_id": lease["lease_id"], "task_id": lease.get("task_id")})

    # ---------- 注册 ----------
    def _create_drivers(self, cfg: dict) -> tuple[object, object, object, str, str]:
        """按设备配置实例化驱动：daemon=驱动桥（正式业务路径），real=串口直连（G 阶段）。

        真机实例化前提：platform hardware_mode=real 且硬件确认表全量确认；
        任一前提不满足时该槽位自动回退仿真驱动并在绑定说明中注明。
        返回：(机械臂, 摄像头, 输入, 绑定说明, 实际生效的绑定模式)。
        """
        device_id = cfg["device_id"]
        driver_mode = cfg.get("driver_mode") or ("real" if cfg.get("binding_mode") == "real" else "simulation")
        if driver_mode not in ("daemon", "real"):
            return None, None, None, cfg.get("binding_note", "仿真绑定，真机绑定以项目方确认为准"), "simulation"
        if self.hardware_mode != "real":
            return None, None, None, "平台处于仿真模式，该槽位真机配置回退仿真驱动", "simulation"
        from drivers.real_camera import CameraMotionVerifier, RealCameraDriver
        from drivers.probe_motion import ProbeMotionVerifier
        from drivers.real_input import RealInputDriver
        from drivers.real_stubs import RealHardwareUnavailable, require_confirmed_hardware

        try:
            require_confirmed_hardware()
        except RealHardwareUnavailable as exc:
            raise ApiError("hardware_unconfirmed", f"设备 {device_id} 无法实例化真机驱动：{exc}")
        # transport_factory / camera_frame_provider 为测试注入钩子：
        # 测试用模拟 Grbl 控制器与合成画面替换真机串口与采集，不改变驱动逻辑。
        transport_factory = cfg.get("transport_factory")
        if driver_mode == "daemon":
            # daemon 驱动桥：串口由 arm_daemon3 独占，运行时经 TCP 驱动（正式业务路径）
            from drivers.daemon_arm import DaemonArmDriver

            arm = DaemonArmDriver(
                device_id,
                daemon_addr=cfg.get("daemon_addr", "127.0.0.1:8795"),
                task_id=cfg.get("task_id", ""),
                data_root=cfg.get("data_root"),
            )
            binding_note = cfg.get("binding_note", "daemon 驱动桥绑定（串口由 arm_daemon3 独占）")
        else:
            from drivers.real_arm import RealArmDriver

            arm = RealArmDriver(
                device_id,
                transport=transport_factory() if transport_factory else None,
                port=cfg.get("serial_port", "/dev/cu.usbserial-212420"),
                baud=int(cfg.get("baud_rate", 115200)),
            )
            binding_note = cfg.get("binding_note", "真机绑定（现场文档 2026-08-30）")
        camera = self._make_real_camera(cfg)
        if driver_mode == "daemon":
            from drivers import calibration_bundle
            from runtime.calibration_gate import check_live_calibration

            async def live_gate(*, wechat_send: bool = False,
                                screen_edge_advisory: bool = False,
                                contact_reference=None,
                                _arm=arm, _camera=camera, _device_id=device_id,
                                _camera_id=cfg.get("camera_id")):
                from runtime.layout_observation import BINDING_KEYS
                from runtime.phone_region_context import (
                    RegionContextError, context_matches_bundle)

                def binding_snapshot():
                    current = self.get(_device_id) or {}
                    return deepcopy({
                        key: current.get(key) for key in (*BINDING_KEYS, "daemon_session")
                    })

                # Camera assignment can change without reconnecting the arm.
                _camera = self._cameras.get(_device_id, _camera)
                _camera_id = (self.get(_device_id) or {}).get("camera_id", _camera_id)
                bundle = deepcopy(calibration_bundle.load_bundle(_device_id, _arm.data_root))
                binding = binding_snapshot()
                search_region = None
                provider = self._region_context_provider
                try:
                    ctx = deepcopy(provider(_device_id)) if provider is not None else None
                    if ctx is not None and isinstance(bundle, dict):
                        context_matches_bundle(ctx, bundle)
                        search_region = ctx["region"]
                except RegionContextError as exc:
                    return {"checks": [{"code": "region_context_invalid",
                                        "message": f"生效粗框不可用：{exc.message}"}],
                            "pose": None}

                def context_checks():
                    if binding_snapshot() != binding:
                        return [{"code": "camera_binding_changed",
                                 "message": "动作前核验期间设备绑定已变化，禁止沿用原画面"}]
                    if calibration_bundle.load_bundle(_device_id, _arm.data_root) != bundle:
                        return [{"code": "calibration_bundle_changed",
                                 "message": "动作前核验期间校准已变化，请重新核验"}]
                    current_provider = self._region_context_provider
                    try:
                        current_ctx = (current_provider(_device_id)
                                       if current_provider is not None else None)
                    except RegionContextError as exc:
                        return [{"code": "region_context_invalid", "message": exc.message}]
                    if current_ctx != ctx:
                        return [{"code": "region_context_changed",
                                 "message": "动作前核验期间粗框已变化，请重新核验"}]
                    return []

                state = await _arm.read_state()
                changed = context_checks()
                if changed:
                    return {"checks": changed, "pose": None}
                checks, _frame, _deviation = await check_live_calibration(
                    device_id=_device_id,
                    camera_id=str(_camera_id or ""),
                    daemon_addr=str(_arm.daemon_addr),
                    daemon_session=state.get("session"),
                    estop_locked=bool(state.get("estop_latched")
                                      or state.get("daemon_estop_locked")),
                    camera=_camera,
                    bundle=bundle,
                    search_region=search_region,
                    wechat_send=wechat_send,
                    screen_edge_advisory=screen_edge_advisory,
                    require_pose=not screen_edge_advisory,
                    contact_reference=contact_reference,
                )
                changed = context_checks()
                if changed:
                    return {"checks": checks + changed, "pose": None}
                return {"checks": checks, "pose": _frame.get("_screen_pose")}

            arm.live_calibration_gate = live_gate

            async def wake_gate(*, require_pose: bool = False,
                                _arm=arm, _device_id=device_id,
                                _camera_id=cfg.get("camera_id")):
                _camera_id = (self.get(_device_id) or {}).get("camera_id", _camera_id)
                bundle = calibration_bundle.load_bundle(_device_id, _arm.data_root)
                state = await _arm.read_state()
                return await check_live_calibration(
                    device_id=_device_id,
                    camera_id=str(_camera_id or ""),
                    daemon_addr=str(_arm.daemon_addr),
                    daemon_session=state.get("session"),
                    estop_locked=bool(state.get("estop_latched")
                                      or state.get("daemon_estop_locked")),
                    camera=self.camera_broker(_device_id),
                    bundle=bundle,
                    screen_wake=True,
                    require_pose=require_pose,
                )

            arm.screen_wake_gate = wake_gate
        inp = RealInputDriver(device_id, keyboard_layout=cfg.get("keyboard_layout"))
        arm.motion_verifier = CameraMotionVerifier(camera)
        if (driver_mode == "daemon"
                and os.environ.get("ARMFLEET_RUNTIME_VISION_MODE") == "calibrated"):
            # Saved calibration positions the arm; camera frames only observe
            # motion. Plugins independently review the resulting screen effect.
            arm.gesture_motion_verifier = arm.motion_verifier
        elif driver_mode == "daemon":
            # 探头照片属于当前校准绑定时保持强校验；过期（换摄像头/重绑/
            # 未登记）自动降级为观察模式——校准档案作为身份源，过期探头
            # 照片只是证据（重新登记探头即恢复强校验）。
            from drivers.probe_motion import profile_stale_for

            arm.gesture_motion_verifier = ProbeMotionVerifier(
                camera, device_id=device_id, data_root=getattr(arm, "data_root", None),
                observation_only=profile_stale_for(
                    device_id, getattr(arm, "data_root", None)))
        return arm, camera, inp, binding_note, "real"

    @staticmethod
    def _make_real_camera(cfg: dict):
        """Build a camera driver without silently opening a second UVC handle.

        The normal multi-device path consumes CameraHelper/8790 frames. Direct
        AVFoundation capture is retained only for an explicit
        ``camera_source_mode=direct`` (or the explicitly named legacy-single
        compatibility mode).
        """
        import cv2

        from drivers.real_camera import HttpFrameSource, RealCameraDriver

        frame_provider = cfg.get("camera_frame_provider")
        if frame_provider is None:
            frame_url = cfg.get("camera_frame_url")
            camera_mode = cfg.get("camera_mode")
            source_mode = str(cfg.get("camera_source_mode") or "").strip().lower()
            if camera_mode == "multi_channel" and not frame_url:
                raise ApiError("camera_config_invalid", f"设备 {cfg['device_id']} 缺少 camera_frame_url")
            if frame_url:
                frame_provider = HttpFrameSource(
                    str(frame_url),
                    expected_camera_id=cfg.get("camera_id"),
                    expected_device_id=cfg.get("device_id"),
                    # USB接入身份由帧桥当前绑定核验；动作另比对新校准。
                    # 固定旧USB编号会阻断换口后取新帧，导致无法人工重新校准。
                    expected_device_unique_id=(None if camera_mode == "multi_channel"
                                               else cfg.get("camera_device_unique_id")),
                    expected_width=(None if camera_mode == "multi_channel"
                                    else cfg.get("expected_frame_width")),
                    expected_height=(None if camera_mode == "multi_channel"
                                     else cfg.get("expected_frame_height")),
                    allow_legacy=camera_mode == "legacy_single",
                )
            elif camera_mode == "legacy_single" or source_mode == "direct":
                # Direct capture is an explicit compatibility choice. It is
                # never inferred from a missing URL, which would otherwise
                # compete with CameraHelper for the same physical camera.
                probe = cv2.VideoCapture(int(cfg.get("camera_index", 0)), cv2.CAP_AVFOUNDATION)
                ok, _frame = probe.read()
                probe.release()
                if not ok:
                    if camera_mode == "legacy_single":
                        frame_provider = HttpFrameSource(
                            "http://127.0.0.1:8790/latest.jpg",
                            expected_camera_id=cfg.get("camera_id"),
                            expected_device_id=cfg.get("device_id"),
                            allow_legacy=True,
                        )
                    else:
                        raise ApiError(
                            "camera_config_invalid",
                            f"设备 {cfg['device_id']} 已显式要求 direct 摄像头，但无法获取摄像头权限",
                        )
            else:
                raise ApiError(
                    "camera_config_invalid",
                    f"设备 {cfg['device_id']} 未提供多路 camera_frame_url，且未声明 camera_source_mode；"
                    "请启动 CameraHelper/8790 分路，或显式声明 direct/legacy_single",
                )
        return RealCameraDriver(
            cfg.get("camera_id", cfg["device_id"]),
            camera_name=cfg.get("camera_device_name", "UVC Camera"),
            camera_index=int(cfg.get("camera_index", 0)),
            frame_provider=frame_provider,
            screen_device_id=cfg["device_id"],
            data_root=cfg.get("data_root"),
        )

    def set_input_status_provider(self, provider) -> None:
        """注入输入法设备查询函数，避免核心硬依赖输入法适配器。"""
        self._ime_status_provider = provider

    def set_layout_observation_provider(self, provider) -> None:
        self._layout_observation_provider = provider
        for device_id in self._cameras:
            self._attach_layout_consumers(device_id)

    def set_region_context_provider(self, provider) -> None:
        """注入本台生效粗框上下文查询（S1/S2 现场门与相机质量前置共用）。"""
        self._region_context_provider = provider
        for device_id, camera in self._cameras.items():
            attach = getattr(camera, "attach_region_context_provider", None)
            if attach:
                attach(provider)

    def _attach_layout_consumers(self, device_id):
        camera = self._cameras[device_id]
        region_attach = getattr(camera, "attach_region_context_provider", None)
        if region_attach:
            region_attach(self._region_context_provider)
        attach = getattr(camera, "attach_layout_provider", None)
        if attach:
            attach(self._layout_observation_provider)
        context_attach = getattr(camera, "attach_target_context_provider", None)
        if context_attach:
            def target_context():
                from runtime.layout_observation import BINDING_KEYS
                from runtime.phone_region_context import context_pointer
                device = self.get(device_id)
                provider = self._region_context_provider
                return {
                    "device": {key: device.get(key) for key in (*BINDING_KEYS, "daemon_session")},
                    "region": context_pointer(provider(device_id)) if provider else None,
                }
            context_attach(target_context)
        action_attach = getattr(self._arms[device_id], "attach_text_action_provider", None)
        if action_attach:
            def begin_text_action(ref):
                begin = getattr(self._cameras[device_id], "begin_text_action", None)
                if begin is None:
                    raise ValueError("当前摄像头不能受理独立文字点击动作")
                return begin(ref)
            action_attach(begin_text_action)
        verify_attach = getattr(self._arms[device_id], "attach_text_target_verifier", None)
        if verify_attach:
            async def verify_text_target(ref, frame):
                from adapters.vision.ocr import OcrEngineError

                verify = getattr(self._cameras[device_id], "verify_text_target", None)
                if verify is None:
                    raise ValueError("当前摄像头不能复核动态文字目标")
                try:
                    return await verify(ref, frame)
                except OcrEngineError as exc:
                    raise ValueError(f"动态文字目标无法唯一复核：{exc}") from exc
            verify_attach(verify_text_target)
        arm_attach = getattr(self._arms[device_id], "attach_layout_provider", None)
        if arm_attach:
            async def current_frame(*, observed_state=None):
                capture = getattr(self._cameras[device_id], "_selected_layout_frame", None)
                if capture is not None:
                    return await capture(**({"observed_state": observed_state} if observed_state else {}))
                provider = self._layout_observation_provider
                if provider is not None and provider(device_id) is not None:
                    raise ValueError("当前摄像头不支持已选共享布局，禁止回退旧目标")
                return None
            arm_attach(current_frame)
        text_attach = getattr(self._arms[device_id], "attach_text_layout_provider", None)
        if text_attach:
            async def current_text_frame(ref):
                capture = getattr(self._cameras[device_id], "current_text_frame", None)
                if capture is None:
                    raise ValueError("当前摄像头不支持 screen_text 文字几何观测")
                return await capture(ref)
            text_attach(current_text_frame)

    def _validate_registration(self, cfg: dict, *, replacing: bool = False) -> None:
        """一对一约束：摄像头、串口、daemon 地址、输入法设备、微信账号和槽位
        不能同时占用多个未移除设备（业务明文例外须走绑定菜单并写审计）。"""
        device_id = str(cfg.get("device_id") or "").strip()
        if not device_id:
            raise ApiError("invalid_request", "注册设备缺少 device_id")
        existing = self._devices.get(device_id)
        if existing and not replacing:
            raise ApiError("device_already_registered", f"设备 {device_id} 已注册，请使用 attach 更新绑定")
        labels = {
            "daemon_addr": "daemon 地址", "camera_id": "摄像头编号",
            "camera_device_unique_id": "物理摄像头唯一标识", "serial_port": "串口",
            "ime_device_id": "输入法设备", "wechat_account_id": "微信账号", "arm_slot": "机械臂槽位",
        }
        for field, label in labels.items():
            value = cfg.get(field)
            if value is None or (isinstance(value, str) and not str(value).strip()):
                continue
            for other_id, other in self._devices.items():
                if other_id != device_id and other.get("presence_state") != "removed":
                    occupied = other.get(field)
                    if occupied is None or (isinstance(occupied, str) and not str(occupied).strip()):
                        continue
                    # 标量统一按字符串比较，避免 arm_slot 2 与 "2" 漏检
                    if str(occupied) == str(value):
                        raise ApiError(
                            "device_binding_conflict",
                            f"{label} {value} 已被设备 {other_id} 占用；一台机械臂一个 daemon、"
                            "一条串口一个持有者，请先解除旧绑定再保存")
        from runtime.config import _validate_camera_config

        _validate_camera_config(cfg)

    def register_device(self, cfg: dict, *, replacing: bool = False) -> dict:
        """注册一台设备：创建驱动 → 自检 → 发布可用镜像。

        真机模式（hardware_mode=real）下只有绑定表声明 driver_mode=real 的槽位
        实例化真机驱动，其余槽位仍为仿真驱动（先接单台，后扩展十台）。
        """
        cfg = normalize_daemon_binding(cfg)
        device_id = cfg["device_id"]
        self._validate_registration(cfg, replacing=replacing)
        previous = self._store.get_device(device_id) or {}
        home_receipt = previous.get("home_check_receipt") or {}
        home_motion_review = bool(previous.get("home_motion_review_required")
                                  or home_receipt.get("state") in {"pending", "unknown"})
        hold_status = previous.get("status") if previous.get("status") in {"paused", "quarantined"} else None
        if previous.get("quarantine_reason"):
            hold_status = "quarantined"
        elif previous.get("pause_reason") and previous.get("status") == "offline":
            hold_status = "paused"
        unknown_task_id = previous.get("unknown_hold_task_id")
        unknown_hold_at = previous.get("unknown_hold_at")
        unknown_task_ids = list(previous.get("unknown_hold_task_ids") or [])
        if unknown_task_id and unknown_task_id not in unknown_task_ids:
            unknown_task_ids.insert(0, unknown_task_id)
        if unknown_task_ids:
            unknown_task_id = unknown_task_ids[0]
        if (unknown_task_id or home_motion_review or previous.get("operator_recovery_pending")) and hold_status != "quarantined":
            hold_status = "paused"
        if hold_status:
            self._startup_holds[device_id] = hold_status
        arm, camera, inp, binding_note, effective_mode = self._create_drivers(cfg)
        # 设备运行态必须反映实际实例化的驱动。真机配置在仿真平台会
        # 回退 FakeArmDriver；若仍保留 driver_mode=daemon，心跳逻辑会
        # 错误要求 daemon session，导致设备被持续判为 offline、任务永久排队。
        configured_driver_mode = cfg.get("driver_mode", "simulation")
        effective_driver_mode = (
            configured_driver_mode if effective_mode == "real" else "simulation"
        )
        if arm is None:
            arm = FakeArmDriver(device_id)
            camera = FakeCameraDriver(
                device_id=cfg.get("camera_id", device_id),
                ui_state=cfg.get("sim_ui_state", "wechat_main"),
                confidence=float(cfg.get("sim_ui_confidence", 0.97)),
                account_clue=cfg.get("wechat_account_id"),
                conversation_clue=None,
            )
            inp = FakeInputDriver(device_id)

        calibration_status = cfg.get("calibration_status")
        if calibration_status is None:
            if self.hardware_mode == "simulation" or cfg.get("sim_calibrated", False):
                calibration_status = "validated"
            else:
                from drivers import calibration_bundle
                bundle = calibration_bundle.load_bundle(device_id)
                calibration_status = "validated" if bundle and bundle.get("status") == "ready" else "due"
        device = {
            "device_id": device_id,
            "name_display": cfg.get("name_display", f"设备 {device_id}"),
            "mac_mini_id": cfg.get("mac_mini_id", self.mac_mini_id),
            "arm_slot": cfg.get("arm_slot"),
            "serial_port": cfg.get("serial_port"),
            "daemon_addr": cfg.get("daemon_addr"),
            "driver_mode": effective_driver_mode,
            "camera_id": cfg.get("camera_id"),
            "camera_device_unique_id": cfg.get("camera_device_unique_id"),
            "camera_binding_id": cfg.get("camera_binding_id"),
            "daemon_binding_id": cfg.get("daemon_binding_id") or cfg.get("arm_binding_id"),
            "daemon_resource_id": cfg.get("daemon_resource_id"),
            "source_geometry_id": cfg.get("source_geometry_id"),
            "camera_mode": cfg.get("camera_mode"),
            "camera_frame_url": cfg.get("camera_frame_url"),
            "camera_status_url": cfg.get("camera_status_url"),
            "expected_frame_width": cfg.get("expected_frame_width"),
            "expected_frame_height": cfg.get("expected_frame_height"),
            "wechat_account_id": cfg.get("wechat_account_id"),
            "enforce_input_gate": bool(cfg.get("enforce_input_gate", cfg.get("hotplug", False) or self.hardware_mode != "simulation")),
            "ime_device_id": cfg.get("ime_device_id") or cfg.get("input_device_id"),
            "ime_online": bool(cfg.get("ime_online", self.hardware_mode == "simulation")),
            "ime_last_seen_at": None,
            "presence_state": "present",
            "calibration_status": calibration_status,
            "calibration_status_display": {"validated": "已校准", "due": "待校准", "unknown": "未知"}.get(calibration_status, calibration_status),
            "calibration_validated_at": cfg.get("calibration_validated_at"),
            "device_mode": "ready_manual" if calibration_status == "validated" else "unmanaged",
            "device_mode_display": DEVICE_MODE["ready_manual"] if calibration_status == "validated" else DEVICE_MODE["unmanaged"],
            "binding_mode": effective_mode,
            "binding_note": binding_note,
            "binding_enabled": bool(cfg.get("binding_enabled", True)),
            "binding_revision": int(cfg.get("binding_revision", 1)),
            "capabilities": cfg.get("capabilities") or list(DEFAULT_DEVICE_CAPABILITIES),
            "driver_versions": {
                "arm": arm.driver_version,
                "camera": camera.driver_version,
                "input": inp.driver_version,
            },
            "config_version": cfg.get("config_version", "1"),
            "status": "starting",
            "status_display": display(DEVICE_STATUS, "starting"),
            "status_reason": None,
            "current_ui_state": "unknown",
            "current_ui_state_display": display(UI_STATE, "unknown"),
            "ui_confidence": 0.0,
            "last_heartbeat_at": None,
            "last_camera_check_at": None,
            "camera_quality": "unknown",
            "camera_quality_display": "未知",
            "camera_quality_reason_code": None,
            "camera_quality_note": None,
            "camera_quality_metrics": {},
            "lease_id": None,
            "home_ok": False if unknown_task_ids or home_motion_review or previous.get("operator_recovery_pending") else previous.get("home_ok", True),
            "home_checked_at": previous.get("home_checked_at"),
            "home_check_receipt": home_receipt,
            "home_motion_review_required": home_motion_review,
            "home_motion_unknown_at": previous.get("home_motion_unknown_at"),
            "operator_recovery_pending": bool(previous.get("operator_recovery_pending")),
            "quarantine_reason": previous.get("quarantine_reason"),
            "pause_reason": (previous.get("pause_reason") or
                             ("安全检查动作回执未知，需人工确认" if home_motion_review else None)),
            "unknown_hold_task_id": unknown_task_id,
            "unknown_hold_task_ids": unknown_task_ids,
            "unknown_hold_at": unknown_hold_at,
            "estop_latched": False,
            "registered_at": iso(utc_now()),
            "observed_at": iso(utc_now()),
        }
        self._arms[device_id] = arm
        self._cameras[device_id] = camera
        self._attach_layout_consumers(device_id)
        # 设备级唯一帧入口：监听、质量巡检、查看和运动验证共享同一 broker。
        self._camera_brokers.pop(device_id, None)
        self._inputs[device_id] = inp
        # fake 应用窗口联动：机械臂点击会话/返回时，fake 摄像头画面同步切换
        if cfg.get("sim_wechat_window"):
            def _on_click(target, _camera=camera):
                if isinstance(target, dict):
                    target_id = str(target.get("target_id", ""))
                    target_ref = target.get("target_ref") or {}
                else:
                    target_id = str(target)
                    target_ref = {}
                if target_id.startswith("conversation:"):
                    _camera.show_conversation(target_id.split(":", 1)[1])
                elif target_id == "conversation_item":
                    # 动态 OCR 目标携带会话编号时，仿真窗口同步切换到精确会话。
                    _camera.show_conversation(str(target_ref.get("conversation_id") or "dynamic"))
                elif target_id == "back_button":
                    _camera.show_main()
            arm.click_callback = _on_click
        self._devices[device_id] = device
        self._store.save_device(dict(device))
        for name in ("motion_verifier", "gesture_motion_verifier"):
            verifier = getattr(arm, name, None)
            if verifier is not None:
                # 单击与手势核验和监听/查看共用设备级 broker。
                verifier._camera = self.camera_broker(device_id)
        self._events.publish("device.registered", {"device_id": device_id, "presence_state": "present"})
        return device

    async def register_and_selfcheck(self, cfg: dict, *, replacing: bool = False) -> dict:
        """硬件自检不要求特定应用界面；应用核对由外部任务插件负责。"""
        cfg = normalize_daemon_binding(cfg)
        if not any(str(item.get("device_id")) == str(cfg.get("device_id")) for item in self._registered_configs):
            self._registered_configs.append(dict(cfg))
        else:
            self._registered_configs = [dict(cfg) if str(item.get("device_id")) == str(cfg.get("device_id")) else item
                                        for item in self._registered_configs]
        device = self.register_device(cfg, replacing=replacing)
        device_id = device["device_id"]
        held_status = self._startup_holds.pop(device_id, None)
        arm_error = None
        try:
            connection = await self._arms[device_id].connect()
            if isinstance(connection, dict) and connection.get("session"):
                device["daemon_session"] = connection["session"]
        except Exception as exc:  # 机械臂串口失败不应连带关闭摄像头
            arm_error = exc
            self._set_status(device_id, held_status or "offline", f"机械臂连接失败：{exc}")
            # 没有可确认的 daemon 会话时，旧校准和人工执行资格不能继续
            # 展示为有效；否则设备虽被任务门禁拦截，控制台却会显示矛盾状态。
            self.revoke_execution_mode(
                device_id,
                "机械臂连接失败，等待人工连接和重新自检",
                calibration_due=True,
            )
            self._alert(device_id, "critical",
                        f"机械臂连接失败，设备{device['status_display']}；摄像头仍可独立使用",
                        action="driver_selfcheck")
        try:
            await self._cameras[device_id].open()
            self._record_camera_source_metadata(device_id, self._cameras[device_id])
        except Exception as exc:
            # 摄像头失败本身只降级；机械臂失败后保留原暂停/隔离或离线，
            # 绝不让自检异常炸掉整个服务启动（未接硬件也要能起端点）。
            diagnostics = quality_failure_diagnostics(exc, capture_failed=True)
            degraded = False
            try:
                if arm_error is None:
                    self._set_status(device_id, held_status or "degraded",
                                     diagnostics["note"])
                    degraded = held_status is None
            except ApiError:
                pass
            note = ("摄像头连接失败，设备降级" if degraded
                    else f"摄像头连接失败；设备保持{device['status_display']}")
            self._alert(device_id, "warning", note, action="camera_check")
            self._record_camera_quality_diagnostics(device, diagnostics)
            self._store.save_device(device)
            self._publish_device_updated(device_id)
            return self._devices[device_id]

        quality = await self._cameras[device_id].check_quality()
        ui = await self._cameras[device_id].identify_ui_state()
        self._record_camera_source_metadata(device_id, self._cameras[device_id])
        expected_view = cfg.get("startup_view")
        confirm = (await self._cameras[device_id].confirm_main_view(expected_view)
                   if expected_view else {"confirmed": True, "reason": None})
        self._store.save_device(dict(device))
        self._audit.record("driver_selfcheck", device_id=device_id, reason="设备注册自检", detail={"quality": quality["quality"]})

        if arm_error is not None:
            # 摄像头质量独立记录；保留暂停/隔离或离线，不能因视频正常伪造可执行。
            pass
        elif quality["quality"] == "waiting":
            # 黑帧不是硬件离线/失效，动作资格由独立的 camera_waiting 门禁控制。
            device["current_ui_state"] = "unknown"
            device["current_ui_state_display"] = display(UI_STATE, "unknown")
            device["ui_confidence"] = 0.0
            self._set_status(device_id, held_status or "ready", "硬件已连接")
        elif quality["quality"] != "clear" or not confirm["confirmed"]:
            self._set_status(device_id, held_status or "degraded",
                             f"自检异常：{confirm['reason'] or quality['quality_display']}")
            self._alert(device_id, "warning", "摄像头自检未通过，设备降级", action="camera_check")
        else:
            device["current_ui_state"] = ui["ui_state"]
            device["current_ui_state_display"] = ui["ui_state_display"]
            device["ui_confidence"] = ui["confidence"]
            reason = ((device.get("pause_reason") or "设备仍暂停，需人工确认")
                      if held_status == "paused" else
                      (device.get("quarantine_reason") or "设备仍隔离，需人工确认")
                      if held_status == "quarantined" else "自检通过")
            self._set_status(device_id, held_status or "ready", reason)
        device["camera_quality"] = quality["quality"]
        device["camera_quality_display"] = quality["quality_display"]
        self._record_camera_quality_diagnostics(device, quality)
        device["last_camera_check_at"] = iso(utc_now())
        self._devices[device_id] = device
        self._store.save_device(dict(device))
        self._publish_device_updated(device_id)
        return device

    async def register_all(self, configs: list[dict]) -> list[dict]:
        self._registered_configs = [dict(cfg) for cfg in configs]
        devices = []
        for cfg in configs:
            devices.append(await self.register_and_selfcheck(cfg))
        return devices

    async def attach_device(self, cfg: dict) -> dict:
        """热插拔挂载：替换绑定并重新自检；绑定变化自动要求重新校准。

        重挂载必须让旧世界失效：先撤销活动租约并关闭旧驱动，旧任务、
        调试上下文与旧 daemon 会话不得跨挂载继续使用。
        """
        device_id = str(cfg.get("device_id") or "")
        old = self._devices.get(device_id)
        old_binding = {key: (old or {}).get(key)
                       for key in ("camera_id", "camera_device_unique_id", "camera_frame_url",
                                   "camera_status_url", "camera_mode", "expected_frame_width",
                                   "expected_frame_height", "daemon_addr", "serial_port",
                                   "ime_device_id", "wechat_account_id", "arm_slot")}
        new_binding = {key: cfg.get(key, old_binding[key]) for key in old_binding}
        self._release_device_lease(device_id, "设备重新挂载，撤销旧租约")
        if old:
            self._lifecycle_versions[device_id] = self._lifecycle_versions.get(device_id, 0) + 1
            old["daemon_session"] = None
            self.revoke_execution_mode(device_id, "设备重新挂载，旧资格失效", calibration_due=True)
            self._set_status(device_id, "offline", "设备重新挂载", force=True)
            self._camera_brokers.pop(device_id, None)
            for driver in (self._arms.get(device_id), self._cameras.get(device_id)):
                close = (getattr(driver, "disconnect", None)
                         if driver is self._arms.get(device_id)
                         else getattr(driver, "close", None))
                if close:
                    try:
                        result = close()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass
        merged = {**(old or {}), **cfg}
        merged.pop("status", None)
        merged.pop("status_display", None)
        merged.pop("lease_id", None)
        if old:
            merged["calibration_status"] = "due"
            merged["device_mode"] = "unmanaged"
        try:
            device = await self.register_and_selfcheck(merged, replacing=bool(old)) if old else await self.register_and_selfcheck(cfg)
        except ApiError:
            # 新配置无法实例化驱动（如清空多路取流地址）：旧驱动已关闭，
            # 必须显式离线并告警，避免留下“记录可用、驱动已死”的半挂载状态。
            if old:
                self._set_status(device_id, "offline", "重挂载失败：新绑定配置无法实例化驱动", force=True)
                self._devices[device_id]["status_reason"] = "重挂载失败：新绑定配置无法实例化驱动"
                self._store.save_device(self._devices[device_id])
                self._alert(device_id, "critical", "重挂载失败，设备已离线；请修正绑定后重新挂载",
                            action="driver_selfcheck")
            raise
        binding_changed = old is not None and old_binding != new_binding
        if binding_changed:
            device = self._devices[device_id]
            device["binding_revision"] = int((old or {}).get("binding_revision", 1)) + 1
            device["calibration_status"] = "due"
            device["calibration_status_display"] = "待校准"
            device["device_mode"] = "unmanaged"
            device["device_mode_display"] = DEVICE_MODE["unmanaged"]
            device["config_version"] = str(int((old or {}).get("config_version", "1") or 1) + 1)
            self._store.save_device(device)
            # 绑定变化后的自检只证明驱动可连接，不代表设备重新具备业务资格。
            # 明确保持暂停，直到人工完成校准并重新放行，避免短暂 ready 窗口接收任务。
            if device.get("status") in ("ready", "degraded"):
                self.pause(device_id, "设备绑定已变化，等待重新校准")
            self._events.publish("device.calibration_required", {
                "device_id": device_id, "reason": "设备绑定发生变化",
                "binding_revision": device["binding_revision"]})
        self._audit.record("device_attach", device_id=device_id,
                           reason="设备挂载/重绑定" + ("（绑定已变化，待重新校准）" if binding_changed else ""),
                           detail={"binding_revision": device.get("binding_revision"),
                                   "binding_changed": binding_changed})
        self._events.publish("device.attached", {"device_id": device_id})
        return device

    async def remove_device(self, device_id: str, reason: str = "设备拔出") -> dict:
        device = self.get(device_id)
        self._lifecycle_versions[device_id] = self._lifecycle_versions.get(device_id, 0) + 1
        self._release_device_lease(device_id, f"设备移除：{reason}")
        device["presence_state"] = "removed"
        device["daemon_session"] = None
        device["calibration_status"] = "due"
        device["calibration_status_display"] = "待校准"
        device["device_mode"] = "unmanaged"
        device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        device["status_reason"] = reason
        device["observed_at"] = iso(utc_now())
        if device["status"] != "offline":
            self._set_status(device_id, "offline", reason, force=True)
        for driver in (self._arms.get(device_id), self._cameras.get(device_id), self._inputs.get(device_id)):
            close = (getattr(driver, "disconnect", None)
                     if driver is self._arms.get(device_id)
                     else getattr(driver, "close", None))
            if close:
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
        self._camera_brokers.pop(device_id, None)
        self._store.save_device(device)
        self._audit.record("device_remove", device_id=device_id, reason=f"设备移除：{reason}")
        self._events.publish("device.removed", {"device_id": device_id, "reason": reason})
        return device

    def mark_daemon_unavailable(self, device_id: str, reason: str) -> dict:
        """daemon 进程退出时立即撤销执行资格，但保留物理串口在场事实。

        进程崩溃不等于 USB 已拔出，因此 ``presence_state`` 保持 present；
        但旧 TCP 会话、租约和校准资格都必须失效，直到人工重新 CONNECT
        并完成自检/校准。该方法供 ``DaemonSupervisor`` 的管理事件调用。
        """
        device = self._devices.get(device_id)
        if device is None:
            return self.get(device_id)
        self._lifecycle_versions[device_id] = self._lifecycle_versions.get(device_id, 0) + 1
        self._release_device_lease(device_id, f"daemon 不可用：{reason}")
        arm = self._arms.get(device_id)
        disconnect = getattr(arm, "disconnect", None)
        if disconnect:
            try:
                result = disconnect()
                if asyncio.iscoroutine(result):
                    try:
                        asyncio.get_running_loop().create_task(result)
                    except RuntimeError:
                        result.close()
            except Exception:
                pass
        device["daemon_session"] = None
        device["device_mode"] = "unmanaged"
        device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        device["calibration_status"] = "due"
        device["calibration_status_display"] = "待校准"
        device["status_reason"] = reason
        device.setdefault("presence_state", "present")
        if device.get("status") != "offline":
            self._set_status(device_id, "offline", reason, force=True)
        self._store.save_device(device)
        self._audit.record("daemon_unavailable", device_id=device_id, reason=reason)
        self._events.publish("device.daemon_unavailable", {
            "device_id": device_id, "reason": reason,
            "calibration_required": True,
        })
        return device

    # ---------- 设备菜单：绑定查看 / 修改 / 禁用 / 恢复 ----------
    BINDING_FIELDS = ("arm_slot", "camera_id", "camera_device_unique_id", "ime_device_id",
                      "wechat_account_id", "daemon_addr", "serial_port", "camera_frame_url",
                      "camera_status_url")
    BINDING_FIELD_LABELS = {
        "device_id": "设备编号（内部键，只读）",
        "arm_slot": "机械臂槽位（第几号工位）",
        "camera_id": "摄像头编号（对应 8790 分路 cam-XX）",
        "camera_device_unique_id": "物理摄像头唯一标识（UVC 唯一 ID）",
        "ime_device_id": "输入法设备编号（手机/输入法目标）",
        "wechat_account_id": "微信账号编号（该设备登录的微信号）",
        "daemon_addr": "daemon 地址（一臂一 daemon 的 TCP 端口）",
        "serial_port": "串口（一条串口一个持有者）",
        "camera_frame_url": "摄像头取流地址（8790 分路帧接口）",
        "camera_status_url": "摄像头状态地址（8790 分路状态接口）",
    }

    def binding_view(self, device_id: str) -> dict:
        """绑定事实视图：绑定字段 + 硬件指纹 + daemon 会话 + 资格 + 最近审计。"""
        device = self.get(device_id)
        return {
            "device_id": device_id,
            "binding": {field: device.get(field) for field in self.BINDING_FIELDS},
            "binding_revision": device.get("binding_revision", 1),
            "daemon_binding_id": device.get("daemon_binding_id"),
            "daemon_resource_id": device.get("daemon_resource_id"),
            "config_version": device.get("config_version", "1"),
            "binding_enabled": device.get("binding_enabled", True),
            "binding_enabled_display": "启用" if device.get("binding_enabled", True) else "已禁用",
            "binding_note": device.get("binding_note"),
            "daemon_session": device.get("daemon_session"),
            "hardware_fingerprint": device.get("hardware_fingerprint"),
            "camera_quality": device.get("camera_quality"),
            "camera_quality_display": device.get("camera_quality_display"),
            "camera_quality_reason_code": device.get("camera_quality_reason_code"),
            "camera_quality_note": device.get("camera_quality_note"),
            "camera_quality_metrics": dict(device.get("camera_quality_metrics") or {}),
            **self.input_status_view(device_id),
            "wechat_account_id": device.get("wechat_account_id"),
            "calibration_status": device.get("calibration_status"),
            "calibration_status_display": device.get("calibration_status_display"),
            "status": device.get("status"),
            "status_display": device.get("status_display"),
            "field_descriptions": dict(self.BINDING_FIELD_LABELS),
        }

    def binding_options(self, device_id: str) -> dict:
        """返回设备菜单可选择的已登记资源，不把任意地址交给前端填写。

        资源编号由服务端生成；更新时再由服务端解析为真实绑定字段。这样
        控制台可以完成换绑，同时不会成为任意 daemon/摄像头 URL 的注入入口。
        """
        self.get(device_id)
        configs = list(self._registered_configs)
        current = self._devices.get(device_id) or {}

        def _unique(items: list[dict], key: str) -> list[dict]:
            seen: set[str] = set()
            result: list[dict] = []
            for item in items:
                value = str(item.get(key) or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                result.append(item)
            return result

        camera_options = []
        for cfg in configs:
            camera_id = str(cfg.get("camera_id") or "").strip()
            if not camera_id:
                continue
            camera_options.append({
                "resource_id": f"camera:{camera_id}",
                "camera_id": camera_id,
                "camera_device_unique_id": cfg.get("camera_device_unique_id"),
                "device_id": cfg.get("device_id"),
                "name_display": cfg.get("name_display") or f"设备 {cfg.get('device_id')}",
                "label": f"{camera_id} · {cfg.get('name_display') or cfg.get('device_id')}",
                "selected": str(current.get("camera_id") or "") == camera_id,
            })
        if current.get("camera_id") and not any(
                item["camera_id"] == str(current["camera_id"]) for item in camera_options):
            camera_options.append({
                "resource_id": f"camera:{current['camera_id']}",
                "camera_id": current["camera_id"],
                "camera_device_unique_id": current.get("camera_device_unique_id"),
                "device_id": device_id,
                "name_display": current.get("name_display") or f"设备 {device_id}",
                "label": f"{current['camera_id']} · 当前绑定",
                "selected": True,
            })

        # Capture channels exist independently of registered arms.
        for number in range(1, 11):
            camera_id = f"cam-{number:02d}"
            if any(item["camera_id"] == camera_id for item in camera_options):
                continue
            camera_options.append({
                "resource_id": f"camera:{camera_id}", "camera_id": camera_id,
                "device_id": None, "camera_device_unique_id": None,
                "label": f"{camera_id} · 未分配机械臂",
                "selected": str(current.get("camera_id") or "") == camera_id,
            })

        daemon_options = []
        for cfg in configs:
            if not str(cfg.get("daemon_addr") or "").strip():
                continue
            daemon_options.append({
                "resource_id": f"daemon:{cfg.get('device_id')}",
                "device_id": cfg.get("device_id"),
                "name_display": cfg.get("name_display") or f"设备 {cfg.get('device_id')}",
                "label": f"{cfg.get('name_display') or cfg.get('device_id')} · 已登记 daemon",
                "selected": str(cfg.get("daemon_addr") or "") == str(current.get("daemon_addr") or ""),
            })

        serial_options = []
        for cfg in configs:
            if not str(cfg.get("serial_port") or "").strip():
                continue
            serial_options.append({
                "resource_id": f"serial:{cfg.get('device_id')}",
                "device_id": cfg.get("device_id"),
                "name_display": cfg.get("name_display") or f"设备 {cfg.get('device_id')}",
                "label": f"{cfg.get('name_display') or cfg.get('device_id')} · 已登记串口",
                "selected": str(cfg.get("serial_port") or "") == str(current.get("serial_port") or ""),
            })

        return {
            "device_id": device_id,
            "camera_options": _unique(camera_options, "camera_id"),
            "daemon_options": _unique(daemon_options, "device_id"),
            "serial_options": _unique(serial_options, "device_id"),
            "说明": "选项来自服务端已加载绑定表；摄像头取流地址和 daemon 地址由服务端解析，前端不直接填写",
        }

    def _resolve_binding_resource(self, kind: str, resource_id: str, device_id: str) -> dict:
        """把设备菜单资源编号解析成绑定字段。"""
        prefix, sep, value = str(resource_id or "").partition(":")
        if not sep or prefix != kind or not value:
            raise ApiError("binding_resource_invalid", f"{kind} 绑定资源编号无效")
        configs = self._registered_configs
        if kind == "camera":
            cfg = next((item for item in configs if str(item.get("camera_id") or "") == value), None)
            independent = value in {f"cam-{number:02d}" for number in range(1, 11)}
            if independent:
                from urllib.parse import urlsplit, urlunsplit
                current = next((item for item in configs
                                if str(item.get("device_id")) == device_id), self.get(device_id))
                # The configured bridge is trusted; the client only supplies a channel ID.
                base = urlsplit(str(current.get("camera_frame_url")
                                    or "http://127.0.0.1:8790/cameras/cam-01/latest.jpg"))
                if base.scheme not in {"http", "https"} or not base.netloc:
                    raise ApiError("camera_config_invalid", "当前设备的帧桥地址无效")
                resolved = {
                    "camera_id": value, "camera_device_unique_id": None,
                    "camera_frame_url": urlunsplit((base.scheme, base.netloc,
                                                     f"/cameras/{value}/latest.jpg", "", "")),
                    "camera_status_url": urlunsplit((base.scheme, base.netloc,
                                                      f"/cameras/{value}/status", "", "")),
                    "camera_mode": "multi_channel",
                }
                # Simulation must not acquire real hardware URLs.
                if str(current.get("driver_mode") or "") not in {"real", "daemon"} \
                        and current.get("binding_mode") != "real":
                    return {"camera_id": value, "camera_device_unique_id": None}
                for key in ("expected_frame_width", "expected_frame_height"):
                    if current.get(key) is not None:
                        resolved[key] = current[key]
                return resolved
            if cfg is None:
                raise ApiError("binding_resource_not_found", f"未找到已登记摄像头资源：{value}")
            resolved = {
                key: cfg.get(key) for key in (
                    "camera_id", "camera_device_unique_id", "camera_frame_url",
                    "camera_status_url", "camera_mode", "expected_frame_width",
                    "expected_frame_height",
                ) if cfg.get(key) is not None
            }
            return resolved
        if kind in {"daemon", "serial"}:
            cfg = next((item for item in configs if str(item.get("device_id") or "") == value), None)
            if cfg is None:
                raise ApiError("binding_resource_not_found", f"未找到已登记设备资源：{value}")
            field = "daemon_addr" if kind == "daemon" else "serial_port"
            if not str(cfg.get(field) or "").strip():
                raise ApiError("binding_resource_unavailable", f"设备 {value} 没有已登记的{field}")
            resolved = {field: cfg[field]}
            if kind == "daemon":
                resolved.update(daemon_resource_id=resource_id,
                                serial_port=cfg.get("serial_port"))
            return resolved
        raise ApiError("binding_resource_invalid", f"不支持的绑定资源类型：{kind}")

    # 影响 physical 连接的字段：变更必须重建驱动并重新自检（走挂载路径）
    REATTACH_FIELDS = frozenset({"daemon_addr", "serial_port", "camera_frame_url",
                                 "camera_id", "camera_device_unique_id"})

    # ---------- FIX-04：换手机流程（§4.8.3 binding_change / §4.8.6） ----------

    def _binding_change_pending(self, device_id: str) -> dict | None:
        """读取未完成的换手机意图（跨重启持久于 guard.binding_change）。"""
        try:
            guard = self._store.input_record_get(f"guard:{device_id}")
        except Exception:
            return None
        if not isinstance(guard, dict):
            return None
        change = guard.get("binding_change")
        if isinstance(change, dict) and change.get("phase") in ("prepared", "closed", "reconciling"):
            return change
        for issue in guard.get("issues") or []:
            if isinstance(issue, dict) and issue.get("code") == "binding_change_pending" \
                    and issue.get("state") == "open":
                if isinstance(change, dict):
                    return change
                detail = issue.get("detail") or {}
                return {"operation_id": str(detail.get("operation_id")
                                            or f"binding-change:{device_id}"),
                        "old_ime_device_id": str(detail.get("old_ime_device_id") or "unbound"),
                        "new_ime_device_id": str(detail.get("new_ime_device_id") or ""),
                        "phase": "prepared"}
        return None

    def _binding_change_issue_id(self, detail: dict) -> str:
        from runtime.input_control import canonical_digest
        return canonical_digest(["binding_change_pending", None, None, detail])[:24]

    def _system_close_input_license(self, device_id: str, operation_id: str,
                                    old_ime: str | None,
                                    settings: dict | None = None) -> dict | None:
        """复用 input_control write/close 路径系统关闭原许可（不改变绑定）。

        - 设置未开启：无需关闭（幂等）；
        - 关闭请求保留原授权手机（设置文件中的授权手机优先），origin 固定
          system_binding_change；
        - 每次尝试独立 client_request_id（{operation_id}:close:{n}）：先前的
          aborted 尝试不阻塞重试；已提交的关闭由 enabled=False 幂等跳过。
        settings 传入换绑预检已读取的解释结果，避免用本流程刚创建的意图记录
        反向污染「已知设备」判据。
        """
        from runtime import input_settings as iset
        from runtime.input_settings import InputSettingsError
        ctrl = self._input_control
        root = self.settings_data_root(device_id)
        if settings is None:
            try:
                settings = iset.load_settings(device_id, root)
            except InputSettingsError as exc:
                raise ApiError("input_settings_invalid",
                               f"换手机前无法解释输入设置（保持阻断）：{exc.message}")
        if settings.get("enabled") is not True:
            return None  # 已关闭：幂等跳过（此前关闭可能已提交）
        revision = int(settings.get("revision") or 0)
        authorized = settings.get("ime_device_id") or old_ime
        previous = [record for record in self._store.input_records_by_device(device_id, "settings_write")
                    if str(record.get("client_request_id") or "").startswith(f"{operation_id}:close:")]
        request_id = f"{operation_id}:close:{len(previous)}"
        try:
            return ctrl.write_settings(
                device_id, data_root=root,
                request={"enabled": False, "ime_device_id": authorized,
                         "expected_revision": revision,
                         "operator_id": "system",
                         "reason": f"换手机系统关闭原许可：{operation_id}",
                         "client_request_id": request_id},
                call_context={"origin": "system_binding_change",
                              "operator_id": "system", "role": "system",
                              "operation_id": operation_id},
                calibration_ready=False, binding_state=None)
        except ApiError:
            raise
        except Exception as exc:
            # 关闭失败：换绑中止，绑定不改变；binding_change 意图保持阻断。
            code = getattr(exc, "reason_code", "input_binding_unavailable")
            raise ApiError(code, f"换手机系统关闭原许可失败（绑定未改变）：{exc}")

    def _revoke_owners_below_boundary(self, device_id: str) -> int:
        """推进撤销边界后撤销边界之下的 active owner（取消/核对交由
        持久取消工作器按原任务/原手机处理；不改变其取消事实）。

        不带 known_check 读取：本步骤运行在换绑预检之后，不能用本流程
        刚创建的意图记录反向触发「已知设备缺文件」阻断。
        """
        from runtime import input_settings as iset
        from runtime.input_control import InputControlError
        ctrl = self._input_control
        try:
            settings = iset.load_settings(device_id, self.settings_data_root(device_id))
        except iset.InputSettingsError as exc:
            raise ApiError("input_settings_invalid",
                           f"换手机前无法解释输入设置（保持阻断）：{exc.message}")
        boundary = int(settings.get("revoked_through_revision") or 0)
        revoked = 0
        for owner in self._store.input_records_by_device(device_id, "owner"):
            if owner.get("revocation") != "active":
                continue
            if int(owner.get("settings_revision") or 0) > boundary:
                continue
            try:
                ctrl.revoke_owner(owner["record_id"],
                                  expected_revision=int(owner["revision"]),
                                  revoked_at_revision=boundary)
                revoked += 1
            except InputControlError:
                continue  # 并发已撤销
        return revoked

    def _binding_change_shutdown_old_license(self, device_id: str, *, old_ime: str | None,
                                             new_ime: str, operator_id: str,
                                             reason: str) -> None:
        """换手机前三步：落 binding_change 意图阻断 → 系统关闭原许可 →
        推进撤销边界并撤销旧 owner（取消/核对由持久工作器继续）。"""
        from runtime import input_settings as iset
        ctrl = self._input_control
        pending = self._binding_change_pending(device_id)
        if pending is not None:
            change = dict(pending)
            operation_id = str(change["operation_id"])
            phase = str(change.get("phase") or "prepared")
        else:
            operation_id = f"binding-change:{device_id}:{new_id('bind')}"
            change = {"operation_id": operation_id,
                      "old_ime_device_id": str(old_ime or "unbound"),
                      "new_ime_device_id": new_ime, "phase": "prepared",
                      "opened_at": iso(utc_now()),
                      "operator_id": operator_id, "reason": reason}
            phase = "prepared"
        issue_detail = {"operation_id": operation_id,
                        "old_ime_device_id": str(old_ime or "unbound"),
                        "new_ime_device_id": new_ime, "phase": "prepared"}
        # 0) 预检：设置必须可安全解释（此时 guard 尚未创建，known 判据不被
        #    本流程自身惰性创建的空 guard 污染）；未开启则无需系统关闭。
        from runtime import input_settings as iset
        from runtime.input_settings import InputSettingsError
        try:
            current_settings = iset.load_settings(
                device_id, self.settings_data_root(device_id),
                known_check=self.input_settings_known_check(device_id))
        except InputSettingsError as exc:
            raise ApiError("input_settings_invalid",
                           f"换手机前无法解释输入设置（保持阻断）：{exc.message}")
        # 1) 意图阻断：binding_change 持久 + binding_change_pending 问题
        guard = ctrl.ensure_guard(device_id)
        if phase == "prepared" and \
                (guard.get("binding_change") or {}).get("operation_id") != operation_id:
            ctrl.set_binding_change(device_id, expected_revision=int(guard["revision"]),
                                    binding_change=change)
        ctrl.open_guard_issue(device_id, code="binding_change_pending",
                              detail=dict(issue_detail))
        # 2) 系统关闭原许可（复用 write/close 路径；关闭请求不改变绑定）
        if phase in ("prepared",):
            self._system_close_input_license(device_id, operation_id, old_ime,
                                             settings=current_settings)
            guard = ctrl.ensure_guard(device_id)
            change = {**change, "phase": "closed", "closed_at": iso(utc_now())}
            ctrl.set_binding_change(device_id, expected_revision=int(guard["revision"]),
                                    binding_change=change)
            phase = "closed"
        # 3) 推进撤销边界（关闭写入已完成）+ 撤销旧 owner；原任务取消/核对
        #    由持久取消工作器按原手机继续（旧手机对账不要求当前许可）
        if phase == "closed":
            revoked = self._revoke_owners_below_boundary(device_id)
            guard = ctrl.ensure_guard(device_id)
            change = {**change, "phase": "reconciling", "revoked_owners": revoked}
            ctrl.set_binding_change(device_id, expected_revision=int(guard["revision"]),
                                    binding_change=change)
        self._audit.record(
            "device_binding_update", device_id=device_id, operator_id=operator_id,
            reason=f"换手机：原许可已系统关闭并撤销（{reason}）",
            detail={"operation_id": operation_id, "old_ime_device_id": str(old_ime or "unbound"),
                    "new_ime_device_id": new_ime, "phase": "reconciling"})

    def _binding_change_complete(self, device_id: str, *, old_ime: str | None,
                                 new_ime: str, operator_id: str) -> None:
        """应用新手机后收尾：binding_change → applied 并解除阻断问题。

        仅标记 applied/解除换绑阻断；输入恢复扫描的其他问题（孤儿任务、
        取消未确认等）保持原状，不因换绑完成清除。
        """
        ctrl = self._input_control
        if ctrl is None:
            return
        pending = self._binding_change_pending(device_id)
        operation_id = str((pending or {}).get("operation_id")
                           or f"binding-change:{device_id}:{new_id('bind')}")
        guard = ctrl.ensure_guard(device_id)
        ctrl.set_binding_change(
            device_id, expected_revision=int(guard["revision"]),
            binding_change={"operation_id": operation_id,
                            "old_ime_device_id": str(old_ime or "unbound"),
                            "new_ime_device_id": new_ime, "phase": "applied",
                            "applied_at": iso(utc_now())})
        issue_id = self._binding_change_issue_id({
            "operation_id": operation_id,
            "old_ime_device_id": str((pending or {}).get("old_ime_device_id") or old_ime or "unbound"),
            "new_ime_device_id": new_ime, "phase": "prepared"})
        try:
            ctrl.resolve_guard_issue(
                device_id, issue_id=issue_id,
                resolution={"phase": "applied", "operation_id": operation_id,
                            "new_ime_device_id": new_ime,
                            "resolved_by": operator_id})
        except Exception:
            pass  # 问题不存在/已解决：幂等收尾
        self._audit.record(
            "device_binding_update", device_id=device_id, operator_id=operator_id,
            reason="换手机完成：新手机已应用，旧许可/旧 owner 不可复活",
            detail={"operation_id": operation_id, "phase": "applied",
                    "new_ime_device_id": new_ime})

    CAMERA_ASSIGNMENT_FIELDS = frozenset({
        "camera_id", "camera_device_unique_id", "camera_frame_url", "camera_status_url",
        "camera_mode", "expected_frame_width", "expected_frame_height",
    })

    async def _update_camera_assignment(self, device_id: str, allowed: dict, merged: dict,
                                        *, operator_id: str, reason: str) -> dict:
        """Persist camera mapping and replace only its consumer; arm session stays intact."""
        import scripts.camera_binding_v2 as camera_v2
        from contextlib import ExitStack

        try:
            with ExitStack() as guards:
                root = self._config_root / "data"
                guards.enter_context(camera_v2.installation_lock(root, device_id, exclusive=True))
                device = self._devices[device_id]
                channels = {str(device.get("camera_id") or ""), str(merged.get("camera_id") or "")}
                for channel in sorted(channels - {""}):
                    guards.enter_context(camera_v2.installation_lock(
                        root, f"camera-{channel}", exclusive=True))
                # Another assignment may have completed before these guards were acquired.
                self._validate_registration(merged, replacing=True)
                if device.get("status") in {"busy", "returning_home", "recovering"}:
                    raise ApiError("device_busy", "设备正在执行任务，请停止后再修改相机对应关系")
                if self._leases is not None and self._leases.get(device_id) is not None:
                    raise ApiError("device_busy", "设备仍持有动作租约，请停止任务后再修改相机对应关系")
                if device.get("driver_mode") in {"real", "daemon"}:
                    camera = self._make_real_camera(merged)
                else:
                    camera = FakeCameraDriver(
                        device_id=str(merged.get("camera_id") or device_id),
                        ui_state=merged.get("sim_ui_state", "wechat_main"),
                        account_clue=merged.get("wechat_account_id"))
                applied = {**allowed, "calibration_status": "due", "device_mode": "unmanaged",
                           "camera_binding_id": None, "source_geometry_id": None}
                candidate = [dict(item, **applied) if str(item.get("device_id")) == device_id
                             else dict(item) for item in self._registered_configs]
                if not any(str(item.get("device_id")) == device_id for item in candidate):
                    candidate.append({**merged, **applied})
                # Never report a successful mapping after swallowing a disk failure.
                # 锁内按设备读改写：拿整表 candidate 覆盖会回滚别处（如换口）刚写的值。
                if self._persist_configs is not None:
                    try:
                        self._persist_device(device_id, **applied)
                    except Exception as exc:
                        raise ApiError("device_config_invalid", f"相机对应关系未保存：{exc}") from exc
                old_camera = self._cameras[device_id]
                self._lifecycle_versions[device_id] = self._lifecycle_versions.get(device_id, 0) + 1
                self._registered_configs = candidate
                device.update(applied)
                device.update(calibration_status="due", calibration_status_display="待校准",
                              device_mode="unmanaged", device_mode_display=DEVICE_MODE["unmanaged"],
                              binding_revision=int(device.get("binding_revision", 1)) + 1,
                              config_version=str(int(device.get("config_version", "1") or 1) + 1),
                              camera_quality="unknown", camera_quality_display="未知",
                              camera_last_frame_at=None, camera_frame_age_ms=None)
                if device.get("status") != "quarantined":
                    self._set_status(device_id, "paused", "相机对应关系已更新，等待人工重新校准", force=True)
                self._cameras[device_id] = camera
                self._camera_brokers.pop(device_id, None)
                self._last_camera_hash.pop(device_id, None)
                self._attach_layout_consumers(device_id)
                for name in ("motion_verifier", "gesture_motion_verifier"):
                    verifier = getattr(self._arms[device_id], name, None)
                    if verifier is not None:
                        verifier._camera = self.camera_broker(device_id)
                self._store.save_device(device)
                try:
                    await old_camera.close()
                except Exception:
                    pass
                try:
                    await camera.open()
                except Exception as exc:
                    device["camera_quality_note"] = f"对应关系已保存，画面暂不可用：{exc}"
                    self._store.save_device(device)
                self._audit.record("device_binding_update", device_id=device_id,
                                   operator_id=operator_id, reason=reason,
                                   detail={"updates": allowed, "camera_only": True,
                                           "binding_revision": device["binding_revision"]})
                self._events.publish("device.binding_updated", {
                    "device_id": device_id, "binding_revision": device["binding_revision"],
                    "reason": reason, "calibration_required": True})
                return self.binding_view(device_id)
        except camera_v2.CameraBindingError as exc:
            raise ApiError("device_busy", exc.message) from exc

    async def update_binding(self, device_id: str, updates: dict, *, operator_id: str, reason: str) -> dict:
        """设备菜单绑定变更：一对一校验、暂停、版本递增、待校准、审计、写回绑定表。

        纯摄像头变更只替换摄像头消费者，保留机械臂/输入会话；daemon/串口等
        重绑定走挂载路径重建驱动并重新自检。调用前必须已完成二次确认。

        FIX-04 换手机流程（§4.8.3/§4.8.6）：ime_device_id 变更时先落
        binding_change 意图并阻断 → 系统关闭原许可（复用 input_control
        write/close 路径，关闭请求不改变绑定并保留原授权手机）→ 推进撤销
        边界并撤销旧 owner（取消/核对交持久工作器）→ 应用新手机。
        换绑中断后按原意图继续（同目标），不得另起新手机。
        """
        device = self.get(device_id)
        if device.get("presence_state") == "removed":
            raise ApiError("device_removed", f"设备 {device_id} 已移除，请先重新挂载")
        if device.get("status") == "busy":
            raise ApiError(
                "device_busy",
                f"设备 {device_id} 正在执行任务，禁止变更绑定；请先等任务结束或取消任务")
        # 2026-10-29 业主授权：崩溃/中断后动作结果未知、账本未核对时禁止换绑
        # （换源会让"未知"永远无法核对）。只看本地内存事实，不发 HTTP；字段按
        # None/空串容错，避免历史数据缺键时抛空值异常。
        unknown_first = str(device.get("unknown_hold_task_id") or "").strip()
        unknown_rest = [str(item).strip()
                        for item in (device.get("unknown_hold_task_ids") or [])
                        if str(item or "").strip()]
        unknown_ids = ([unknown_first] if unknown_first else []) + unknown_rest
        if unknown_ids:
            raise ApiError(
                "device_unknown_hold",
                f"设备 {device_id} 存在未核对的动作结果（任务 {unknown_ids[0]}），"
                "请先完成核对/收尾再变更绑定")
        allowed = {field: updates[field] for field in self.BINDING_FIELDS if field in updates}
        # 控制台使用资源编号；真实地址只在服务端从已加载绑定表解析。
        for selector, kind in (("camera_binding_id", "camera"),
                               ("daemon_binding_id", "daemon"),
                               ("serial_binding_id", "serial")):
            if selector in updates:
                allowed.update(self._resolve_binding_resource(kind, str(updates[selector]), device_id))
        if not allowed:
            raise ApiError("invalid_request", "绑定变更请求不包含任何可修改字段")
        # FIX-04：换手机意图检查必须在一对一校验之后、任何状态改变之前。
        old_ime = device.get("ime_device_id")
        requested_ime = allowed.get("ime_device_id")
        ime_changing = "ime_device_id" in allowed and requested_ime != old_ime
        pending_change = self._binding_change_pending(device_id)
        if pending_change is not None:
            target = str(pending_change.get("new_ime_device_id") or "")
            if ime_changing and requested_ime != target:
                raise ApiError(
                    "binding_change_pending",
                    f"设备 {device_id} 存在未完成的换手机流程（目标手机 {target}）："
                    "请按原目标继续或人工核对，不得另起新手机")
            ime_changing = True
            requested_ime = target
        if ime_changing:
            if not isinstance(requested_ime, str) or not requested_ime.strip():
                raise ApiError("invalid_request", "换手机必须提供非空 ime_device_id")
            requested_ime = requested_ime.strip()
            if self._input_control is None:
                raise ApiError("input_control_not_ready",
                               "输入控制未装配：不能安全换手机；本次绑定变更被拒绝，绑定未改变")
            # 续办（同目标）时把目标手机补进本次应用集合，保证 close 后应用新手机。
            allowed["ime_device_id"] = requested_ime
            # 意图阻断 → 系统关闭原许可 → 推进撤销边界/撤销旧 owner。
            # 失败抛错中止：绑定不改变，意图保持阻断等待恢复。
            self._binding_change_shutdown_old_license(
                device_id, old_ime=old_ime, new_ime=requested_ime,
                operator_id=operator_id, reason=reason)
        cfg = next((item for item in self._registered_configs
                    if str(item.get("device_id")) == device_id), None)
        if cfg is None:
            cfg = dict(device)
        merged = normalize_daemon_binding({**cfg, **allowed, "device_id": device_id})
        if merged.get("daemon_addr") and not set(allowed) <= self.CAMERA_ASSIGNMENT_FIELDS:
            allowed.update({key: merged[key] for key in
                            ("daemon_resource_id", "daemon_binding_id")})
        # 先做一对一校验：冲突请求不得改变设备状态（不暂停、不递增版本）
        self._validate_registration(merged, replacing=True)
        if set(allowed) <= self.CAMERA_ASSIGNMENT_FIELDS:
            return await self._update_camera_assignment(
                device_id, allowed, merged, operator_id=operator_id, reason=reason)
        self._release_device_lease(device_id, "绑定变更，撤销旧租约")
        heavy = bool(self.REATTACH_FIELDS & set(allowed))
        if heavy:
            # 重绑定：更新注册配置后走挂载路径（重建驱动、自检、版本递增、待校准）
            # 先落盘再改内存：写失败时内存保持旧值，避免"内存新、磁盘旧"分叉。
            self._persist_device(device_id, **allowed)
            for item in self._registered_configs:
                if str(item.get("device_id")) == device_id:
                    item.update(allowed)
            await self.attach_device({**merged})
            self._audit.record("device_binding_update", device_id=device_id,
                               operator_id=operator_id, reason=reason,
                               detail={"updates": allowed, "reattached": True,
                                       "binding_revision": self._devices[device_id].get("binding_revision")})
            if ime_changing:
                self._binding_change_complete(device_id, old_ime=old_ime,
                                              new_ime=requested_ime,
                                              operator_id=operator_id)
            return self.binding_view(device_id)
        # 轻量字段：先落盘（失败则设备状态也不动），再暂停，再改内存/DB。
        self._persist_device(device_id, **allowed)
        if device.get("status") in ("ready", "busy", "degraded", "returning_home"):
            self.pause(device_id, "绑定变更，设备自动暂停")
        for item in self._registered_configs:
            if str(item.get("device_id")) == device_id:
                item.update(allowed)
        device = self._devices[device_id]
        previous_revision = int(device.get("binding_revision", 1))
        device.update(allowed)
        device["binding_revision"] = previous_revision + 1
        device["config_version"] = str(int(device.get("config_version", "1") or 1) + 1)
        needs_recheck = any(field in allowed for field in
                            ("camera_id", "camera_device_unique_id", "camera_frame_url",
                             "ime_device_id", "wechat_account_id", "daemon_addr", "serial_port"))
        if needs_recheck:
            device["calibration_status"] = "due"
            device["calibration_status_display"] = "待校准"
            device["device_mode"] = "unmanaged"
            device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        self._store.save_device(device)
        self._audit.record("device_binding_update", device_id=device_id,
                           operator_id=operator_id, reason=reason,
                           detail={"updates": allowed,
                                   "binding_revision": device["binding_revision"],
                                   "calibration_due": needs_recheck})
        self._events.publish("device.binding_updated", {
            "device_id": device_id, "binding_revision": device["binding_revision"],
            "reason": reason, "calibration_required": needs_recheck})
        if ime_changing:
            # 应用新手机后收尾：binding_change → applied，解除换绑阻断。
            self._binding_change_complete(device_id, old_ime=old_ime,
                                          new_ime=requested_ime,
                                          operator_id=operator_id)
        return self.binding_view(device_id)

    def disable_binding(self, device_id: str, *, operator_id: str, reason: str) -> dict:
        """禁用旧绑定：暂停 + 撤销资格 + 审计；禁用后不能接任务，可再恢复。"""
        device = self.get(device_id)
        self._release_device_lease(device_id, f"绑定禁用：{reason}")
        if device.get("status") in ("ready", "busy", "degraded", "returning_home"):
            self.pause(device_id, f"绑定禁用：{reason}")
        # 先落盘：写失败时设备仍是"启用"，避免"页面已禁用、重启又复活"。
        self._persist_device(device_id, binding_enabled=False)
        device["binding_enabled"] = False
        device["device_mode"] = "unmanaged"
        device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        self._store.save_device(device)
        self._audit.record("device_binding_disable", device_id=device_id,
                           operator_id=operator_id, reason=reason)
        self._events.publish("device.updated", {"device_id": device_id, "reason": "绑定已禁用"})
        return self.binding_view(device_id)

    def enable_binding(self, device_id: str, *, operator_id: str, reason: str) -> dict:
        """恢复被禁用的绑定：重新进入待校准门禁，未重新校准不得托管。"""
        device = self.get(device_id)
        if device.get("binding_enabled", True):
            raise ApiError("invalid_request", f"设备 {device_id} 绑定未被禁用，无需恢复")
        self._persist_device(device_id, binding_enabled=True, calibration_status="due")
        device["binding_enabled"] = True
        device["calibration_status"] = "due"
        device["calibration_status_display"] = "待校准"
        device["device_mode"] = "unmanaged"
        device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        self._store.save_device(device)
        self._audit.record("device_binding_enable", device_id=device_id,
                           operator_id=operator_id, reason=reason,
                           detail={"note": "恢复后必须重新校准/主界面检查后才能托管"})
        self._events.publish("device.binding_enabled", {"device_id": device_id, "reason": reason})
        return self.binding_view(device_id)

    def _persist_device(self, device_id: str, *, create_if_missing: bool = True,
                        **changes) -> dict:
        """按设备写回静态绑定表（锁内读改写，不覆盖其它设备）。

        changes 必须是纯 delta：已存在的设备只合并这些字段。把内存整条记录并进来
        会让"只改 binding_enabled"退化成整条覆盖，把别的进程刚写的值改回（丢更新）。

        create_if_missing 默认 True 以保持既有语义：动态注册的设备也会被写进静态表；
        此时用内存视图作 create_seed 补全静态字段，避免留下空壳条目。
        """
        if self._persist_configs is None:
            return dict(changes)
        seed = None
        if create_if_missing:
            from runtime.config import project_static_device

            entry = next((item for item in self._registered_configs
                          if str(item.get("device_id")) == device_id), None)
            if entry is not None:
                seed = project_static_device(dict(entry))
        try:
            return self._persist_configs(device_id, dict(changes),
                                         create_if_missing=create_if_missing,
                                         create_seed=seed)
        except Exception as exc:
            self._audit.record("device_binding_update", device_id=device_id,
                               reason=f"绑定表写回失败：{exc}", outcome="failed")
            if isinstance(exc, ApiError):
                raise
            raise ApiError("device_config_write_failed",
                           f"绑定表写回失败，本次操作未生效：{exc}") from exc

    # ---------- 状态机 ----------
    def _set_status(self, device_id: str, status: str, reason: str | None = None, *, force: bool = False) -> None:
        device = self._devices[device_id]
        if status == "ready":
            from runtime.operator_recovery import pending_operator_recovery
            if pending_operator_recovery(self._store, device_id):
                raise ApiError("home_not_confirmed", "人工机械臂操作尚未核实，不能恢复设备")
        if status == "ready" and (device.get("unknown_hold_task_id")
                                   or device.get("operator_recovery_pending")
                                   or device.get("communication_recovery_required")
                                   or device.get("home_motion_review_required")
                                   or (device.get("home_check_receipt") or {}).get("state") in {"pending", "unknown"}
                                   or device.get("pause_reason")
                                   or device.get("quarantine_reason")):
            raise ApiError("home_not_confirmed", "设备仍处于人工暂停或隔离，不能自动恢复")
        current = device["status"]
        if status == current:
            return
        if not force and status not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ApiError(
                "state_transition_invalid",
                f"设备 {device_id} 不允许从 {display(DEVICE_STATUS, current)} 迁移到 {display(DEVICE_STATUS, status)}",
            )
        device["status"] = status
        device["status_display"] = display(DEVICE_STATUS, status)
        device["status_reason"] = reason
        device["observed_at"] = iso(utc_now())
        self._store.save_device(device)
        self._publish_device_updated(device_id)

    def _alert(self, device_id: str | None, severity: str, message: str, *, action: str | None = None, task_id: str | None = None) -> dict:
        alert = {
            "alert_id": new_id("alert"),
            "device_id": device_id,
            "task_id": task_id,
            "severity": severity,
            "severity_display": display({"info": "提示", "warning": "警告", "critical": "严重"}, severity),
            "message": message,
            "resolved": False,
            "created_at": iso(utc_now()),
            "resolved_at": None,
            "resolved_by": None,
        }
        self._store.save_alert(alert)
        self._events.publish("alert.created", {"alert_id": alert["alert_id"], "device_id": device_id, "message": message})
        if action:
            self._audit.record(action, device_id=device_id, task_id=task_id, reason=message)
        return alert

    def resolve_alert(self, alert_id: str, operator_id: str, reason: str) -> dict:
        alert = self._store.get_alert(alert_id)
        if not alert:
            from runtime.errors import ApiError

            raise ApiError("alert_not_found", alert_id)
        alert["resolved"] = True
        alert["resolved_at"] = iso(utc_now())
        alert["resolved_by"] = operator_id
        alert["resolve_reason"] = reason
        self._store.save_alert(alert)
        self._audit.record("alert_resolve", operator_id=operator_id, device_id=alert.get("device_id"), reason=reason, detail={"alert_id": alert_id})
        return alert

    # ---------- 查询 ----------
    def _refresh_calibration_status(self, device: dict) -> dict:
        """真机公有校准状态不能沿用旧 ready 标记，必须匹配当前会话和绑定。"""
        if device.get("binding_mode") != "real" or device.get("driver_mode") != "daemon":
            return device
        from drivers import calibration_bundle
        from runtime.calibration_gate import check_registered_calibration

        camera = self._cameras.get(device["device_id"])
        if camera is not None:
            self._record_camera_source_metadata(device["device_id"], camera)
        arm = self._arms.get(device["device_id"])
        facts = dict(device)
        if arm is None or not getattr(arm, "connected", False):
            facts["daemon_session"] = None
        try:
            bundle = calibration_bundle.load_bundle(
                device["device_id"], getattr(arm, "data_root", None))
            checks = check_registered_calibration(facts, bundle)
        except (OSError, ValueError, TypeError):
            checks = [{"code": "calibration_bundle_invalid",
                       "message": "无法读取统一校准档案，必须重新校准"}]
        device["calibration_checks"] = checks
        device["calibration_check_scope"] = "archive_and_binding"
        device["calibration_status_reason"] = "；".join(check["message"] for check in checks) or None
        if checks:
            device["calibration_status"] = "due"
            device["calibration_status_display"] = "待重新校准"
            device["calibration_validated_at"] = None
            device["device_mode"] = "unmanaged"
            device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        elif device.get("calibration_marked_status") in {"due", "unknown"}:
            # 显式人工保留须由操作者解除，不能被只读档案检查覆盖。
            marked = device["calibration_marked_status"]
            device["calibration_status"] = marked
            device["calibration_status_display"] = {"due": "待校准", "unknown": "未知"}[marked]
            device["calibration_validated_at"] = None
            device["device_mode"] = "unmanaged"
            device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        else:
            # 只同步已验证的校准事实；读取状态不能解除人工暂停或恢复托管。
            device["calibration_status"] = "validated"
            device["calibration_status_display"] = "已校准"
            device["calibration_validated_at"] = (
                (bundle.get("autoCalibration") or {}).get("promotedAt")
                or bundle.get("updatedAt"))
        return device

    def get(self, device_id: str) -> dict:
        device = self._devices.get(device_id)
        if device is None:
            device = self._store.get_device(device_id)
            if device is not None:
                # 动态设备记录跨进程保留；重新插入前只可查询，不能直接调用驱动动作。
                self._devices[device_id] = device
        if not device:
            from runtime.errors import ApiError

            raise ApiError("device_not_found", device_id)
        return self._refresh_calibration_status(device)

    def all(self) -> list[dict]:
        merged = {d["device_id"]: d for d in self._store.all_devices()}
        merged.update(self._devices)
        return [self._refresh_calibration_status(device)
                for device in sorted(merged.values(), key=lambda d: d["device_id"])]

    def driver(self, device_id: str):
        return self._arms[device_id]

    def camera(self, device_id: str):
        return self._cameras[device_id]

    def camera_broker(self, device_id: str):
        """返回设备级共享 CameraFrameBroker，不重复打开底层摄像头。"""
        from drivers.camera_broker import CameraFrameBroker

        camera = self.camera(device_id)
        broker = self._camera_brokers.get(device_id)
        if broker is None or getattr(broker, "_driver", None) is not camera:
            broker = CameraFrameBroker(
                camera,
                camera_id=str(self._devices[device_id].get("camera_id") or device_id),
                max_age_seconds=float(getattr(self, "camera_broker_max_age_seconds", 1.5)),
            )
            self._camera_brokers[device_id] = broker
            attach = getattr(camera, "attach_broker", None)
            if attach is not None:
                attach(broker)
        return broker

    def input_driver(self, device_id: str):
        return self._inputs[device_id]

    def set_lease(self, device_id: str, lease_id: str | None) -> None:
        device = self._devices[device_id]
        device["lease_id"] = lease_id
        self._store.save_device(device)
        self._publish_device_updated(device_id)

    def update_ui_state(self, device_id: str, ui_state: str, confidence: float) -> None:
        device = self._devices[device_id]
        device["current_ui_state"] = ui_state
        device["current_ui_state_display"] = display(UI_STATE, ui_state)
        device["ui_confidence"] = round(confidence, 3)
        device["observed_at"] = iso(utc_now())
        self._store.save_device(device)
        self._publish_device_updated(device_id)

    def is_taskable(self, device_id: str) -> tuple[bool, str | None]:
        """设备当前能否领取新任务。"""
        device = self._devices[device_id]
        if device["status"] == "quarantined":
            return False, "device_quarantined"
        if device["status"] == "paused":
            return False, "device_paused"
        if device["status"] == "offline":
            return False, "device_offline"
        if device["status"] in ("busy", "returning_home", "recovering", "starting", "maintenance"):
            return False, "device_busy"
        if not device.get("home_ok", True):
            return False, "home_not_confirmed"
        if device.get("estop_latched", False):
            return False, "estop_active"
        if device.get("camera_quality") == "waiting":
            return False, "camera_waiting"
        return True, None

    def input_binding(self, device_id: str) -> dict:
        """校准档案是 daemon 设备的正式手机绑定源；绝不猜测在线手机。"""
        device = self.get(device_id)
        configured = device.get("ime_device_id")
        result = {"ime_device_id": configured, "configured_ime_device_id": configured,
                  "ime_binding_source": "device_config"}
        driver = self.driver(device_id)
        if device.get("binding_mode") != "real" or not hasattr(driver, "data_root"):
            return result
        from drivers import calibration_bundle
        bundle = calibration_bundle.load_bundle(device_id, driver.data_root)
        if not bundle:
            return result
        valid = (bundle.get("status") in {"ready", calibration_bundle.MOTION_READY_STATUS}
                 and bundle.get("deviceId") == device_id
                 and bundle.get("cameraId") == device.get("camera_id")
                 and bool(device.get("daemon_session"))
                 and bundle.get("daemonSession") == device.get("daemon_session"))
        if valid and bundle["status"] == calibration_bundle.MOTION_READY_STATUS:
            from runtime.calibration_gate import check_registered_calibration
            # A phone identity permits sampling, not input acceptance or sending.
            valid = not check_registered_calibration(device, bundle)
        return {**result, "ime_device_id": bundle.get("imeDeviceId") if valid else None,
                "ime_binding_source": "calibration_bundle" if valid else "calibration_invalid"}

    def _ime_status(self, device: dict) -> dict | None:
        ime_id = self.input_binding(device["device_id"]).get("ime_device_id")
        if not ime_id:
            return None
        provider = self._ime_status_provider
        if provider is None:
            try:
                from adapters.screen_ime.service import get_default_service
                rows = get_default_service().tasks.list_devices()
            except Exception:
                return None
        else:
            try:
                rows = provider()
            except Exception:
                return None
        return next((row for row in rows if row.get("deviceId") == ime_id or row.get("device_id") == ime_id), None)

    def input_status_view(self, device_id: str) -> dict:
        """输出实际绑定手机状态，不覆盖持久配置或把键盘未打开当作离线。"""
        binding = self.input_binding(device_id)
        status = self._ime_status(self.get(device_id)) or {}
        return {
            **binding,
            "ime_online": bool(status.get("online")),
            "ime_keyboard_active": bool(status.get("keyboardActive", status.get("keyboard_active", False))),
            "ime_last_seen_at": status.get("lastSeenAt") or status.get("last_seen_at"),
        }

    def settings_data_root(self, device_id: str):
        """设备级设置持久根（2026-10-03 规格 §4.1）：driver.data_root 优先，回退运行根/data。

        与 ArmFleetRuntime._gesture_data_root 同一解析；禁止回退到模块目录，
        否则测试/部署会读写到生产数据目录。
        """
        try:
            arm = self._arms.get(device_id)
            root = getattr(arm, "data_root", None)
            if root:
                return Path(root)
        except Exception:
            pass
        return self._config_root / "data"

    def eligibility(self, device_id: str, *, require_input: bool = False, ime_device_id: str | None = None,
                    require_keyboard_active: bool = True, require_ime_ready: bool = True) -> dict:
        from runtime.device_eligibility import compute_device_eligibility
        device = dict(self.get(device_id))
        try:
            device["_settings_data_root"] = self.settings_data_root(device_id)
        except Exception:
            pass
        if require_input and device.get("binding_mode") == "real":
            pass  # A-开关（2026-10-03 规格）：E 派生的 deferred 标记不再参与资格
        device["ime_device_id"] = self.input_binding(device_id).get("ime_device_id")
        if ime_device_id and not device.get("ime_device_id"):
            device["ime_device_id"] = ime_device_id
        ime = self._ime_status(device)
        if self.hardware_mode == "simulation" and not device.get("ime_device_id"):
            # 仿真设备没有手机心跳时仍可跑平台测试；真机/已绑定设备严格检查。
            device["ime_online"] = True
            device["wechat_account_id"] = device.get("wechat_account_id") or "simulation-account"
            ime = {"online": True, "keyboardActive": True}
        return compute_device_eligibility(device, ime_status=ime, require_input=require_input,
                                          require_keyboard_active=require_keyboard_active,
                                          require_ime_ready=require_ime_ready)

    def set_calibration_status(self, device_id: str, status: str, reason: str = "") -> dict:
        if status not in {"validated", "due", "unknown"}:
            raise ApiError("invalid_request", "calibration_status 只允许 validated、due、unknown")
        device = self.get(device_id)
        device["calibration_marked_status"] = status
        device["calibration_marked_status_display"] = {
            "validated": "已校准", "due": "待校准", "unknown": "未知"}[status]
        device["calibration_marked_at"] = iso(utc_now())
        device["calibration_status"] = status
        device["calibration_status_display"] = {"validated": "已校准", "due": "待校准", "unknown": "未知"}[status]
        device["calibration_validated_at"] = iso(utc_now()) if status == "validated" else None
        if status != "validated":
            device["device_mode"] = "unmanaged"
            device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        elif device.get("status") == "ready":
            device["device_mode"] = "ready_manual"
            device["device_mode_display"] = DEVICE_MODE["ready_manual"]
        self._refresh_calibration_status(device)
        self._store.save_device(device)
        effective_status = device["calibration_status"]
        self._events.publish(
            "device.calibration_validated" if effective_status == "validated" else "device.calibration_required",
            {"device_id": device_id, "reason": device.get("calibration_status_reason") or reason})
        return device

    def revoke_execution_mode(self, device_id: str, reason: str, *, calibration_due: bool = False) -> dict:
        """组件掉线/异常时立即撤销人工输入和托管资格。"""
        device = self.get(device_id)
        device["device_mode"] = "unmanaged"
        device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        if calibration_due:
            device["calibration_status"] = "due"
            device["calibration_status_display"] = "待校准"
        device["status_reason"] = reason
        self._store.save_device(device)
        self._events.publish("device.task_eligibility_changed", {"device_id": device_id, "reason": reason})
        return device

    # ---------- 运维操作（状态迁移 + 审计由上层包装） ----------
    def pause(self, device_id: str, reason: str) -> dict:
        self._set_status(device_id, "paused", reason)
        device = self._devices[device_id]
        device["device_mode"] = "unmanaged"
        device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        device["pause_reason"] = reason
        self._store.save_device(device)
        return self.get(device_id)

    def hold_unknown(self, device_id: str, task_id: str, reason: str) -> dict:
        """Persist the physical interlock before any untrusted recovery hook."""
        device = self._devices[device_id]
        held = list(device.get("unknown_hold_task_ids") or [])
        first = device.get("unknown_hold_task_id")
        if first and first not in held:
            held.insert(0, first)
        if task_id not in held:
            held.append(task_id)
        device["unknown_hold_task_ids"] = held
        if not first:
            device["unknown_hold_task_id"] = task_id
            device["unknown_hold_at"] = iso(utc_now())
        device["home_ok"] = False
        device["home_checked_at"] = None
        device["pause_reason"] = reason
        device["device_mode"] = "unmanaged"
        device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        if device["status"] not in {"offline", "quarantined"}:
            self._set_status(device_id, "paused", reason)
        self._store.save_device(device)
        return device

    def resume(self, device_id: str, reason: str) -> dict:
        device = self._devices[device_id]
        from runtime.operator_recovery import pending_operator_recovery
        if pending_operator_recovery(self._store, device_id):
            raise ApiError("home_not_confirmed", "人工机械臂操作尚未核实，不能恢复设备")
        if device.get("operator_recovery_pending") or device.get("communication_recovery_required"):
            raise ApiError("home_not_confirmed", "机械臂通信或人工停车仍待核实，不能恢复设备")
        if (device.get("home_motion_review_required")
                or (device.get("home_check_receipt") or {}).get("state") in {"pending", "unknown"}):
            raise ApiError("home_not_confirmed", "安全检查动作尚未人工确认，不能恢复设备")
        held_task_id = device.get("unknown_hold_task_id")
        held_task_ids = list(device.get("unknown_hold_task_ids") or [])
        if held_task_id and held_task_id not in held_task_ids:
            held_task_ids.insert(0, held_task_id)
        if held_task_ids:
            if any(
                not (held_task := self._store.get_task(task_id))
                or held_task.get("device_id") != device_id
                or held_task.get("status") != "unknown"
                or not held_task.get("reviewed_at")
                for task_id in held_task_ids
            ):
                raise ApiError("home_not_confirmed", "结果未知任务尚未人工核对，不能恢复设备")
            checked_at = parse_iso(device.get("home_checked_at"))
            held_at = parse_iso(device.get("unknown_hold_at"))
            if not checked_at or not held_at or checked_at < held_at:
                raise ApiError("home_not_confirmed", "需在结果未知后重新确认探头位置及安全路径")
            if device.get("binding_mode") == "real":
                heartbeat = parse_iso(device.get("last_heartbeat_at"))
                if (device.get("calibration_status") != "validated"
                        or not device.get("daemon_session") or not heartbeat
                        or (utc_now() - heartbeat).total_seconds() > 5):
                    raise ApiError("home_not_confirmed", "当前 daemon 会话、心跳及校准尚未重新确认")
        if not device.get("home_ok", True):
            raise ApiError("home_not_confirmed", "设备归位或微信主界面尚未确认，不能直接恢复；请先完成人工确认")
        held_at = device.get("unknown_hold_at")
        paused_reason = device.get("pause_reason")
        device["unknown_hold_task_id"] = None
        device["unknown_hold_task_ids"] = []
        device["unknown_hold_at"] = None
        device["pause_reason"] = None
        try:
            self._set_status(device_id, "ready", reason)
        except Exception:
            device["unknown_hold_task_id"] = held_task_id
            device["unknown_hold_task_ids"] = held_task_ids
            device["unknown_hold_at"] = held_at
            device["pause_reason"] = paused_reason
            raise
        self._store.save_device(device)
        self._release_terminal_lease(device_id, "人工确认 home_verified 后释放任务租约")
        return self.get(device_id)

    def quarantine(self, device_id: str, reason: str) -> dict:
        self._set_status(device_id, "quarantined", reason)
        device = self._devices[device_id]
        device["device_mode"] = "unmanaged"
        device["device_mode_display"] = DEVICE_MODE["unmanaged"]
        device["quarantine_reason"] = reason
        self._store.save_device(device)
        self._alert(device_id, "critical", f"设备已隔离：{reason}", action="device_quarantine")
        return self.get(device_id)

    def unquarantine(self, device_id: str, reason: str) -> dict:
        device = self._devices[device_id]
        device["quarantine_reason"] = None
        self._set_status(device_id, "recovering", reason)
        self._store.save_device(device)
        return self.get(device_id)

    async def finish_recovering(self, device_id: str) -> dict:
        """恢复中设备完成归位检查后回到可用。"""
        result = await self.home_check(device_id)
        if result["home_ok"]:
            held = self.get(device_id).get("unknown_hold_task_id")
            self._set_status(device_id, "paused" if held else "ready",
                             "结果未知待复核" if held else "恢复完成，归位检查通过")
        else:
            self._set_status(device_id, "quarantined", "恢复检查失败，重新隔离")
        return self.get(device_id)

    async def home_check(self, device_id: str, *, read_only: bool = False,
                         execution_guard=None) -> dict:
        """平台级安全检查：停止并保持当前位置 + 摄像头画面清晰。

        注意：主界面确认属于具体插件步骤的职责（例如微信适配插件的
        “返回主界面并确认”步骤）。无硬件限位器时不执行真实回零；
        校准安全停车位和业务 UI 停靠位也不属于本检查的目标。
        """
        from runtime.action_receipts import motion_receipt_unknown

        device = self._devices[device_id]
        held_unknown = bool(device.get("unknown_hold_task_id")
                            or device.get("home_motion_review_required")
                            or (device.get("home_check_receipt") or {}).get("state") in {"pending", "unknown"})
        if held_unknown:
            read_only = True
        arm = self._arms[device_id]
        camera = self._cameras[device_id]
        session = device.get("daemon_session")
        lifecycle = self._lifecycle_versions.get(device_id, 0)
        lease = self._leases.get(device_id) if self._leases else None
        lease_identity = ((lease.get("task_id"), lease.get("lease_id")) if lease else None)

        def still_owned():
            current = self._leases.get(device_id) if self._leases else None
            identity = ((current.get("task_id"), current.get("lease_id")) if current else None)
            return (self._devices.get(device_id) is device
                    and self._arms.get(device_id) is arm
                    and self._lifecycle_versions.get(device_id, 0) == lifecycle
                    and device.get("daemon_session") == session
                    and identity == lease_identity
                    and (execution_guard is None or execution_guard()))

        if lease and execution_guard is None:
            return {"device_id": device_id, "home_ok": False, "checked_at": None,
                    "detail": {"skip": "设备仍有任务租约，独立检查不执行动作"}}
        prev_status = device["status"]
        if prev_status == "ready":
            self._set_status(device_id, "returning_home", "执行归位检查")
        home_ok = False
        detail: dict = {}
        receipt = None
        cancelled = None
        try:
            state = await arm.read_state()
            if not still_owned() or (session and state.get("session") != session):
                return {"device_id": device_id, "home_ok": False, "checked_at": None,
                        "detail": {"skip": "原租约、会话或取消状态已变化，未执行安全检查动作"}}
            detail["position_before"] = state.get("position")
            if read_only:
                result = {"ok": state.get("connected") is True and not state.get("estop_locked", False),
                          "position": state.get("position")}
                detail["read_only"] = True
                detail["position_semantics"] = "只读状态检查；未调用停止、归位或移动"
            else:
                receipt = {"state": "pending", "started_at": iso(utc_now()),
                           "daemon_session": session, "lease_identity": lease_identity}
                device["home_check_receipt"] = receipt
                device["home_ok"] = False
                device["home_checked_at"] = None
                self._store.save_device(device)
                try:
                    result = await arm.go_home()
                except BaseException as exc:
                    receipt["state"] = ("not_started" if getattr(exc, "motion_not_started", False)
                                        else "unknown")
                    if receipt["state"] == "unknown":
                        detail["motion_result_unknown"] = True
                    raise
                if motion_receipt_unknown(result):
                    receipt["state"] = "unknown"
                    detail["motion_result_unknown"] = True
                    raise RuntimeError("安全检查动作回执未确认，禁止后续动作")
                receipt["state"] = "confirmed"
                detail["position_semantics"] = "停止并保持当前位置（无硬件限位器，不是真实回零）"
            detail["position_after"] = result.get("position")
            if not still_owned():
                return {"device_id": device_id, "home_ok": False, "checked_at": None,
                        "detail": {**detail, "skip": "原执行权限已变化，不更新新持有者状态"}}
            quality = await camera.check_quality()
            if not still_owned():
                return {"device_id": device_id, "home_ok": False, "checked_at": None,
                        "detail": {**detail, "skip": "原执行权限已变化，不更新新持有者状态"}}
            detail["quality"] = {"quality": quality["quality"], "quality_display": quality["quality_display"]}
            home_ok = not held_unknown and bool(result.get("ok")) and quality["quality"] == "clear"
            if held_unknown:
                detail["fail_reason"] = "结果未知期间仅允许只读核查；需人工确认安全状态"
            if quality["quality"] != "clear":
                detail["fail_reason"] = f"画面异常：{quality['quality_display']}"
        except BaseException as exc:
            detail["fail_reason"] = str(exc) or type(exc).__name__
            home_ok = False
            if not isinstance(exc, Exception):
                cancelled = exc
        finally:
            if receipt is not None and still_owned():
                receipt["finished_at"] = iso(utc_now())
                self._store.save_device(device)
        if not still_owned():
            if cancelled is not None:
                raise cancelled
            return {"device_id": device_id, "home_ok": False, "checked_at": None,
                    "detail": {**detail, "skip": "原执行权限已变化，不更新新持有者状态"}}
        device["home_ok"] = home_ok
        device["home_checked_at"] = iso(utc_now())
        if detail.get("motion_result_unknown"):
            device["home_motion_review_required"] = True
            device["home_motion_unknown_at"] = iso(utc_now())
            self._set_status(device_id, "paused", "安全检查动作回执未知，需人工确认")
            device["pause_reason"] = "安全检查动作回执未知，需人工确认"
        elif prev_status == "ready" and not held_unknown:
            self._set_status(device_id, "ready" if home_ok else "degraded", "归位检查通过" if home_ok else "归位检查未通过")
        if not home_ok:
            device["home_ok"] = False
            self._alert(device_id, "critical", "归位检查未通过，设备不能领取新任务", action="device_home_check")
        else:
            self._audit.record("device_home_check", device_id=device_id, reason="归位检查通过")
        self._store.save_device(device)
        if cancelled is not None:
            raise cancelled
        return {"device_id": device_id, "home_ok": home_ok, "checked_at": device["home_checked_at"], "detail": detail}

    def mark_home_ok(self, device_id: str, operator_id: str, reason: str) -> dict:
        """人工确认归位（仅人工处理路径，必须审计）。"""
        device = self._devices[device_id]
        device["home_ok"] = True
        device["home_checked_at"] = iso(utc_now())
        if (device.get("home_motion_review_required")
                or (device.get("home_check_receipt") or {}).get("state") in {"pending", "unknown"}):
            device["home_motion_review_required"] = False
            device["home_check_receipt"] = {
                **(device.get("home_check_receipt") or {}),
                "state": "reviewed", "reviewed_at": device["home_checked_at"],
                "reviewed_by": operator_id,
            }
        self._store.save_device(device)
        self._audit.record("device_reset_home", operator_id=operator_id, device_id=device_id, reason=reason)
        self._release_terminal_lease(device_id, "人工确认归位后释放已结束任务租约")
        # 人工确认归位即完成该路径的恢复：归位失败造成的降级同步恢复
        # 就绪——与自动归位检查（home_ok=True→ready）同语义。状态机禁止
        # degraded→busy，降级设备若停留降级将永远无法领取任务（派发在
        # awaiting_device 后置 busy 必被拒）。仍有其他人工阻断（暂停/
        # 隔离/未知持有）时 _set_status 的 ready 门会拒绝，此时不越权恢复。
        if device["status"] == "degraded":
            try:
                self._set_status(device_id, "ready", "人工确认归位，恢复就绪")
            except ApiError:
                pass
            device = self._devices[device_id]
        return device

    def _release_terminal_lease(self, device_id: str, reason: str) -> None:
        """仅在关联任务已结束且人工确认安全状态后释放遗留租约。"""
        if self._leases is None:
            return
        lease = self._leases.get(device_id)
        if lease is None:
            return
        task = self._store.get_task(lease.get("task_id")) if lease.get("task_id") else None
        if task is not None and task.get("status") not in {"confirmed", "failed", "unknown", "cancelled", "expired"}:
            return
        self._leases.release(device_id, lease["lease_id"])
        self._audit.record(
            "task_lease_released_after_review",
            device_id=device_id,
            task_id=lease.get("task_id"),
            reason=reason,
            detail={"lease_id": lease.get("lease_id")},
        )

    # ---------- 心跳与摄像头巡检 ----------
    async def start_background(self) -> None:
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._camera_task = asyncio.create_task(self._camera_loop())
        if self._hotplug_discovery_enabled:
            self._discovery_task = asyncio.create_task(self._discovery_loop())

    async def stop_background(self) -> None:
        self._running = False
        for task in (self._heartbeat_task, self._camera_task, self._discovery_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def scan_discovery(self, *, ports: list[dict] | None = None) -> dict:
        """执行一次热插拔扫描；ports 仅用于测试或外部探测器注入。"""
        from runtime.discovery import discover

        result = discover(self._registered_configs, ports=ports)
        if not result.get("probe_available", True) and ports is None:
            return result
        for binding in result["bindings"]:
            device_id = str(binding.get("device_id"))
            if device_id not in self._devices:
                continue
            present = bool(binding["present"])
            # 绑定串口在场即记录硬件身份（换口重枚举比对用；与 presence 无关）
            if present and binding.get("vid") is not None:
                self._last_serial_identity[device_id] = (
                    binding.get("vid"), binding.get("pid"),
                    binding.get("serial_number"))
            previous = self._last_presence.get(device_id)
            self._last_presence[device_id] = present
            device = self._devices[device_id]
            # 绑定串口可见时记录硬件指纹，供设备菜单展示绑定事实
            fingerprint = binding.get("hardware_fingerprint")
            if present and fingerprint and device.get("hardware_fingerprint") != fingerprint:
                device["hardware_fingerprint"] = fingerprint
                self._store.save_device(device)
            if previous is None:
                continue
            if present == previous:
                continue
            if present:
                # 重新插入不能只改 presence 标记：拔出时旧驱动已 close，
                # 必须重新实例化、连接并完成自检，才能清除 device_removed 门禁。
                cfg = next((item for item in self._registered_configs
                            if str(item.get("device_id")) == device_id), None)
                try:
                    if cfg is None:
                        raise ApiError("device_not_registered", f"设备 {device_id} 没有注册配置")
                    await self.attach_device(dict(cfg))
                    device = self._devices[device_id]
                    device["presence_state"] = "present"
                    device["status_reason"] = "检测到机械臂重新插入，已完成自动挂载与自检"
                    if binding.get("hardware_fingerprint"):
                        device["hardware_fingerprint"] = binding["hardware_fingerprint"]
                    self._store.save_device(device)
                    self._events.publish("device.discovered", {
                        "device_id": device_id, "serial_port": binding["serial_port"],
                        "present": True, "attached": True,
                    })
                except Exception as exc:
                    device["presence_state"] = "present"
                    device["status_reason"] = f"检测到重新插入，但自动挂载/自检失败：{exc}"
                    self._store.save_device(device)
                    self._events.publish("device.discovered", {
                        "device_id": device_id, "serial_port": binding["serial_port"],
                        "present": True, "attached": False, "error": str(exc),
                    })
            else:
                # 换口重枚举（用户拍板 2026-09-30：串口号随 USB 口变化是常态）：
                # 绑定路径消失但未知端口携带同一硬件身份（vid/pid/serial）→
                # 视为同一设备换口：更新绑定串口并保存，自动挂载交给下轮扫描；
                # 无身份匹配才是真拔出，按原移除路径处理。
                identity = self._last_serial_identity.get(device_id)
                reenum = None
                if identity:
                    for item in result.get("unknown_ports") or []:
                        if (item.get("vid"), item.get("pid"),
                                item.get("serial_number")) == identity:
                            reenum = str(item.get("path"))
                            break
                if reenum:
                    cfg = next((item for item in self._registered_configs
                                if str(item.get("device_id")) == device_id), None)
                    if cfg is not None:
                        from runtime.config import update_device_config

                        # 先落盘再改内存；带 create_seed，避免造出空壳条目。
                        update_device_config(self._config_root, device_id,
                                             {"serial_port": reenum},
                                             create_if_missing=True,
                                             create_seed=dict(cfg))
                        cfg["serial_port"] = reenum
                        self._events.publish("device.serial_reenumerated", {
                            "device_id": device_id,
                            "old_serial_port": binding.get("serial_port"),
                            "new_serial_port": reenum,
                        })
                        # 新端口已绑定：标记指纹避免下方再发 requires_registration
                        matched = next((item for item in result.get("unknown_ports") or []
                                        if str(item.get("path")) == reenum), None)
                        if matched is not None and matched.get("fingerprint"):
                            self._known_discovery_fingerprints.add(
                                str(matched["fingerprint"]))
                        print(f"检测到 {device_id} 串口换口重枚举："
                              f"{binding.get('serial_port')} → {reenum}（已更新绑定）",
                              flush=True)
                        continue
                await self.remove_device(device_id, "检测到机械臂串口拔出")
        # 用户拍板 2026-09-30：USB 串口自动排序绑定——新出现的 USB 串口按
        # arm_slot/device_id 顺序自动分配给第一个空闲设备槽位（arm-01 优先）。
        # 仅认 USB 串口（usbserial/usbmodem）：系统蓝牙/调试口不参与；
        # 分配只改绑定，串口连接仍需调试台「连接设备」→ 正常校准流程。
        present_by_device = {str(b.get("device_id")): bool(b.get("present"))
                             for b in result["bindings"]}
        assigned_this_pass: set[str] = set()
        for item in result["unknown_ports"]:
            fingerprint = str(item.get("fingerprint") or item.get("path"))
            if fingerprint in self._known_discovery_fingerprints:
                continue
            port_path = str(item.get("path") or "")
            is_usb_serial = ("usbserial" in port_path or "usbmodem" in port_path)
            free_cfg = None
            if is_usb_serial and port_path not in assigned_this_pass:
                # 空闲槽位 = 绑定端口物理不在场（拔出/断电=绑定作废，用户拍板
                # 2026-09-30「一直在线否则需要重新绑定」）或从未绑定
                free = [cfg for cfg in self._registered_configs
                        if not present_by_device.get(str(cfg.get("device_id")), True)
                        and str(cfg.get("serial_port") or "") not in assigned_this_pass]
                free.sort(key=lambda c: (c.get("arm_slot") is None,
                                         c.get("arm_slot") or 0,
                                         str(c.get("device_id") or "")))
                free_cfg = free[0] if free else None
            if free_cfg is not None:
                device_id = str(free_cfg.get("device_id"))
                free_cfg["serial_port"] = port_path
                assigned_this_pass.add(port_path)
            if free_cfg is not None:
                device_id = str(free_cfg.get("device_id"))
                from runtime.config import update_device_config

                update_device_config(self._config_root, device_id,
                                     {"serial_port": port_path},
                                     create_if_missing=True,
                                     create_seed=dict(free_cfg))
                free_cfg["serial_port"] = port_path
                self._known_discovery_fingerprints.add(fingerprint)
                self._events.publish("device.serial_auto_assigned", {
                    "device_id": device_id, "serial_port": port_path,
                    "hardware_fingerprint": fingerprint,
                })
                print(f"USB 串口 {port_path} 已自动分配给 {device_id}"
                      "（调试台「连接设备」后走正常校准流程）", flush=True)
                continue
            self._known_discovery_fingerprints.add(fingerprint)
            self._events.publish("device.discovered", {
                "device_id": None, "serial_port": item.get("path"),
                "hardware_fingerprint": fingerprint, "requires_registration": True,
            })
        return result

    async def _discovery_loop(self) -> None:
        while self._running:
            try:
                await self.scan_discovery()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self._discovery_interval_seconds)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            for device_id in list(self._devices):
                try:
                    await self._heartbeat_device(device_id)
                except Exception:
                    pass
            await asyncio.sleep(2)

    async def _heartbeat_device(self, device_id: str) -> None:
        """处理单台设备心跳，并在 daemon 已恢复后重新接回业务驱动桥。

        调试台和业务运行时是两个 TCP 客户端。机械臂拔出时业务客户端会先
        进入 offline；用户随后在调试台点击 CONNECT 只会恢复 daemon 的串口
        会话，不会自动把已断开的业务客户端 transport 接回来。这里针对
        daemon bridge 做一次轻量级重连（只重新打开 TCP，不发送 CONNECT），
        让页面的“已连接”状态和运行时的设备状态最终一致。
        """
        arm = self._arms[device_id]
        version = self._lifecycle_versions.get(device_id, 0)
        if self._devices[device_id].get("presence_state") == "removed":
            return
        state = await arm.read_state()
        if (self._arms.get(device_id) is not arm
                or version != self._lifecycle_versions.get(device_id, 0)):
            return
        device = self._devices[device_id]

        # daemon 的串口仍由人工 CONNECT 控制；这里仅在 daemon 已经可用时
        # 恢复业务驱动桥，避免拔出期间擅自打开串口或绕过人工连接。
        # 启动时串口未连接也可能保留 paused/quarantined/maintenance；这些
        # 运行限制不能阻止只读 TCP 会话同步，恢复连接本身不解除限制。
        if (
            not state.get("connected")
            and device.get("presence_state") == "present"
            and device.get("driver_mode") == "daemon"
        ):
            try:
                await arm.connect()
                state = await arm.read_state()
            except Exception:
                # daemon 尚未人工 CONNECT 时保持原状态；下一轮心跳继续探测。
                pass

        if (self._arms.get(device_id) is not arm
                or version != self._lifecycle_versions.get(device_id, 0)):
            return
        device["last_heartbeat_at"] = iso(utc_now())
        device["estop_latched"] = bool(state.get("estop_latched")
                                      or state.get("daemon_estop_locked"))
        device["communication_recovery_required"] = bool(state.get("communication_recovery_required"))
        device["communication_recovery_id"] = state.get("recovery_id")
        device["communication_recovery_reason"] = state.get("recovery_reason")
        if device["communication_recovery_required"]:
            device["home_ok"] = False
            device["home_checked_at"] = None
            reason = "串口回包超时，连接已保留；请人工检查机械臂后复核通信"
            device["pause_reason"] = device.get("pause_reason") or reason
            self.revoke_execution_mode(device_id, reason, calibration_due=False)
            if device["status"] in {"ready", "busy", "degraded", "recovering"}:
                self._set_status(device_id, "paused", reason)
        current_session = state.get("session")
        previous_session = device.get("daemon_session")
        if previous_session and previous_session != current_session:
            self._release_device_lease(device_id, "daemon 会话变化，撤销旧租约")
            self.revoke_execution_mode(device_id, "daemon 会话变化，旧校准已失效", calibration_due=True)
            self._events.publish("device.calibration_required", {"device_id": device_id, "reason": "daemon_session_changed"})
            device["daemon_session"] = current_session
        if current_session:
            device["daemon_session"] = current_session
        if device.get("driver_mode") == "daemon" and not current_session:
            state = {**state, "connected": False}
        if state.get("connected"):
            if device["status"] == "offline":
                # 心跳恢复：重新接回业务驱动桥；校准状态仍由会话变化门禁控制。
                # 状态机要求 offline 先经过 starting，再回到 ready。
                if device.get("quarantine_reason"):
                    self._set_status(device_id, "quarantined", "daemon 已重连，隔离仍需人工解除")
                elif device.get("pause_reason") or device.get("unknown_hold_task_id"):
                    self._set_status(device_id, "paused", "daemon 已重连，人工暂停仍有效")
                else:
                    self._set_status(device_id, "starting", "daemon 连接恢复，正在完成业务驱动自检")
                    self._set_status(device_id, "ready", "daemon 连接恢复，业务驱动桥已重连")
                self._audit.record("driver_selfcheck", device_id=device_id, reason="心跳恢复重连")
            # 断线恢复本身不等于重新校准。若 daemon 会话、设备绑定和帧尺寸
            # 仍与活动校准档案一致，只恢复之前被心跳故障撤销的运行资格。
            # 会话或绑定发生变化时，上面的失效分支已经明确保持 due。
            if (device.get("calibration_status") == "due" and not device["estop_latched"]
                    and device.get("calibration_marked_status") not in {"due", "unknown"}):
                try:
                    from drivers import calibration_bundle
                    bundle = calibration_bundle.load_bundle(
                        device_id, getattr(arm, "data_root", None))
                    frame = bundle.get("frame") or {} if isinstance(bundle, dict) else {}
                    matching_bundle = (
                        isinstance(bundle, dict)
                        and bundle.get("status") in ("ready", "motion_ready")
                        # A2（2026-10-01 backlog）：motion_ready 是合法可运行态
                        # （机械资格完整、输入延后），恢复资格不得只认字面 ready。
                        and bundle.get("deviceId") == device.get("device_id")
                        and bundle.get("cameraId") == device.get("camera_id")
                        and bundle.get("cameraDeviceUniqueId") == device.get("camera_device_unique_id")
                        and int(bundle.get("bindingRevision", -1)) == int(
                            device.get("camera_binding_revision")
                            if device.get("camera_binding_revision") is not None
                            else device.get("binding_revision", -2))
                        and bundle.get("daemonAddr") == device.get("daemon_addr")
                        and bundle.get("daemonSession") == current_session
                        and int(frame.get("width", -1)) == int(device.get("expected_frame_width", -2))
                        and int(frame.get("height", -1)) == int(device.get("expected_frame_height", -2))
                    )
                    if matching_bundle and device.get("binding_mode") == "real":
                        from runtime.calibration_gate import check_registered_calibration
                        matching_bundle = not check_registered_calibration(device, bundle)
                except (TypeError, ValueError, OSError):
                    matching_bundle = False
                if matching_bundle:
                    device["calibration_status"] = "validated"
                    device["calibration_status_display"] = "已校准"
                    device["calibration_validated_at"] = iso(utc_now())
                    if (device.get("status") == "ready"
                            and not device.get("pause_reason")
                            and not device.get("quarantine_reason")
                            and not device.get("lease_id")):
                        device["device_mode"] = "ready_manual"
                        device["device_mode_display"] = DEVICE_MODE["ready_manual"]
                    self._audit.record(
                        "calibration_restored",
                        device_id=device_id,
                        reason="daemon 恢复且活动校准档案仍匹配",
                    )
        else:
            self._release_device_lease(device_id, "机械臂连接中断，撤销旧租约")
            # 即使设备已经是 offline，也要清掉可能残留的旧资格。
            # 这覆盖启动时未建立过会话、以及旧进程留下 validated/ready_manual
            # 镜像的情况；仅依赖状态迁移会漏掉这两类路径。
            self.revoke_execution_mode(
                device_id,
                "机械臂连接中断，等待人工连接和重新自检",
                calibration_due=True,
            )
            if device["status"] in ("ready", "busy", "degraded"):
                self._set_status(device_id, "offline", "机械臂连接中断，心跳丢失")
                self._alert(device_id, "critical", "心跳丢失，设备离线", action="heartbeat_lost")
        self._refresh_calibration_status(device)
        self._store.save_device(device)

    # 摄像头帧来源中文显示（与 camera_frame_server 的来源标记一致）
    CAMERA_SOURCE_DISPLAY = {
        "camera_helper": "CameraHelper 正式采集",
        "chrome_compat": "Chrome 兼容降级桥",
        "legacy": "旧单路兼容来源",
    }

    def _record_camera_source_metadata(self, device_id: str, camera) -> None:
        """把摄像头分路事实（来源/最后帧时间/帧年龄）写入设备镜像。

        仅真机帧源驱动提供 last_source_info()；仿真摄像头无此方法时跳过。
        """
        getter = getattr(camera, "last_source_info", None)
        if getter is None:
            return
        info = getter()
        if not info:
            return
        device = self._devices.get(device_id)
        if device is None or self._cameras.get(device_id) is not camera:
            return
        source = info.get("source") or ("legacy" if info.get("legacy_mode") else None)
        updates = {
            "camera_source": source,
            "camera_source_display": self.CAMERA_SOURCE_DISPLAY.get(source or "", "未知来源")
                                      if source else None,
            "camera_last_frame_at": info.get("received_at") or info.get("captured_at"),
            "camera_binding_revision": info.get("binding_revision"),
        }
        received = info.get("received_at")
        source_age_ms = None
        if received:
            try:
                moment = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                source_age_ms = (datetime.now(timezone.utc) - moment).total_seconds() * 1000
                updates["camera_frame_age_ms"] = max(0, int(source_age_ms))
            except ValueError:
                pass
        # Only the current driver's recently validated frame can refresh camera identity.
        # A saved calibration is never used to guess a newly attached source.
        # Task layout snapshots freeze these identity fields. Late metadata from a
        # task's first capture must not mutate its baseline; live frame/bundle gates
        # still reject an actually changed source. Idle reads/polls adopt it later.
        identity_idle = (device.get("status") not in {"busy", "returning_home", "recovering"}
                         and not (self._leases is not None and self._leases.get(device_id)))
        if (identity_idle and source_age_ms is not None
                and 0 <= source_age_ms <= self.camera_broker_max_age_seconds * 1000
                and info.get("camera_id") == device.get("camera_id")
                and info.get("device_id") == device_id
                and info.get("device_unique_id") and info.get("camera_binding_id")
                and info.get("source_geometry_id") and not info.get("legacy_mode")):
            updates.update(camera_device_unique_id=info["device_unique_id"],
                           camera_binding_id=info["camera_binding_id"],
                           source_geometry_id=info["source_geometry_id"])
            # Runtime facts follow decoded pixels from the current trusted v2 source.
            # Configuration and capture format stay unchanged; calibration must match
            # these observed dimensions before any formal action can run.
            width, height = info.get("frame_width"), info.get("frame_height")
            if (device.get("camera_mode") == "multi_channel"
                    and type(width) is int and width > 0
                    and type(height) is int and height > 0):
                updates.update(expected_frame_width=width, expected_frame_height=height)
        changed = any(device.get(key) != value for key, value in updates.items())
        if changed:
            device.update(updates)
            self._store.save_device(device)

    @staticmethod
    def _record_camera_quality_diagnostics(device: dict, quality: dict) -> bool:
        diagnostics = quality_diagnostics(quality)
        changed = any(device.get(f"camera_quality_{key}") != diagnostics[key]
                      for key in ("reason_code", "note"))
        device.update({f"camera_quality_{key}": value for key, value in diagnostics.items()})
        if (quality.get("quality") == "clear"
                and device.get("status_reason") == "摄像头画面异常，撤销输入/托管资格"):
            # A recovered camera does not resolve a paused task or grant execution.
            # Show the surviving hold instead of a stale camera failure.
            device["status_reason"] = (device.get("pause_reason")
                                       or device.get("quarantine_reason")
                                       or "摄像头画面已恢复；执行资格需另行确认")
            changed = True
        return changed

    async def _camera_loop(self) -> None:
        while self._running:
            for device_id in list(self._devices):
                try:
                    device = self._devices[device_id]
                    camera = self._cameras[device_id]
                    version = self._lifecycle_versions.get(device_id, 0)
                    broker = self.camera_broker(device_id)
                    try:
                        quality = await broker.check_quality()
                    except CameraQualityCheckError as exc:
                        if (self._devices.get(device_id) is not device
                                or self._cameras.get(device_id) is not camera
                                or version != self._lifecycle_versions.get(device_id, 0)):
                            continue
                        # Preserve the existing exception-path quality and gates.
                        changed = self._record_camera_quality_diagnostics(device, exc.diagnostics)
                        self._store.save_device(device)
                        if changed:
                            self._publish_device_updated(device_id)
                        continue
                    if (self._devices.get(device_id) is not device
                            or self._cameras.get(device_id) is not camera
                            or version != self._lifecycle_versions.get(device_id, 0)):
                        continue
                    self._record_camera_source_metadata(device_id, camera)
                    ui = ({"ui_state": "unknown", "ui_state_display": "未知", "confidence": 0.0}
                          if quality["quality"] == "waiting"
                          else await camera.identify_ui_state())
                    if (self._devices.get(device_id) is not device
                            or self._cameras.get(device_id) is not camera
                            or version != self._lifecycle_versions.get(device_id, 0)):
                        continue
                    # Commit quality and page state only for this attachment.
                    device["last_camera_check_at"] = iso(utc_now())
                    prev = device.get("camera_quality")
                    device["camera_quality"] = quality["quality"]
                    device["camera_quality_display"] = quality["quality_display"]
                    diagnostic_changed = self._record_camera_quality_diagnostics(device, quality)
                    device["current_ui_state"] = ui["ui_state"]
                    device["current_ui_state_display"] = ui["ui_state_display"]
                    device["ui_confidence"] = ui["confidence"]
                    if quality["quality"] not in {"clear", "waiting"} and prev in {"clear", "waiting"}:
                        self.revoke_execution_mode(device_id, "摄像头画面异常，撤销输入/托管资格")
                        self._alert(device_id, "warning", f"摄像头画面异常：{quality['quality_display']}", action="camera_check")
                    self._store.save_device(device)
                    if quality["quality"] != prev or diagnostic_changed:
                        self._publish_device_updated(device_id)
                except Exception:
                    pass
            await asyncio.sleep(10)

    def _publish_device_updated(self, device_id: str) -> None:
        device = self._devices.get(device_id)
        if device:
            eligibility = self.eligibility(device_id, require_input=True)
            self._events.publish(
                "device.updated",
                {
                    "device_id": device_id,
                    "status": device["status"],
                    "status_display": device["status_display"],
                    "presence_state": device.get("presence_state", "present"),
                    "device_mode": device.get("device_mode", "unmanaged"),
                    "device_mode_display": device.get("device_mode_display", "未托管"),
                    "calibration_status": device.get("calibration_status", "unknown"),
                    "calibration_status_display": device.get("calibration_status_display", "未知"),
                    "calibration_status_reason": device.get("calibration_status_reason"),
                    "calibration_checks": device.get("calibration_checks", []),
                    "calibration_check_scope": device.get("calibration_check_scope"),
                    "camera_quality": device.get("camera_quality"),
                    "camera_quality_display": device.get("camera_quality_display"),
                    "camera_quality_reason_code": device.get("camera_quality_reason_code"),
                    "camera_quality_note": device.get("camera_quality_note"),
                    "camera_quality_metrics": dict(device.get("camera_quality_metrics") or {}),
                    "eligibility": eligibility,
                },
            )


def raise_api(key: str, detail: str | None = None):
    from runtime.errors import ApiError

    raise ApiError(key, detail)
