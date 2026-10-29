"""Arm Fleet 运行时总控：平台唯一可写事实源的入口。

组合：存储、事件、审计、证据、能力、设备、租约、插件、执行器、急停、回报、查看会话。
平台核心不内置任何具体应用业务；业务任务类型全部来自外部插件。
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import threading

import jsonschema

from runtime.audit import AuditLog
from runtime.capabilities import CapabilityRegistry
from runtime.config import (PlatformConfig, load_device_configs, load_platform_config,
                            task_callback_base_url, validate_device_configs)
from runtime.device_eligibility import INPUT_TASK_TYPES
from runtime.devices import DeviceManager
from runtime.daemon_supervisor import DaemonSupervisor
from runtime.errors import ApiError
from drivers.arm import DriverError as _DriverError
from runtime.events import EventBus
from runtime.evidence import EvidenceStore
from runtime.estop import EstopController
from runtime.executor import TaskExecutor
from runtime.leases import LeaseManager
from runtime.permissions import ROLE_LEVEL
from runtime.plugin_capabilities import PluginCapabilityMixin
from runtime.plugins import PluginManager
from runtime.reporter import Reporter
from runtime.status import CAMERA_QUALITY, DEVICE_MODE, DEVICE_STATUS, TASK_STATUS, display
from runtime import screen_wake
from runtime.store import Store
from runtime.util import iso, new_id, parse_iso, sha256_text, utc_now
from runtime.views import ViewSessionManager
from runtime.takeovers import TakeoverBridge
CONTRACT_DIR = Path(__file__).resolve().parents[1] / "api" / "contracts"
# 任务类型 → 外壳契约 schema（api/contracts/ 下文件名）。平台核心只提供注册表
# 机制，不硬编码业务契约分支；chat-send 为历史默认注册项（业务 插件任务类型），
# 业务插件可在装载时经 register_task_contract_schema 登记自己的外壳契约。
TASK_CONTRACT_SCHEMAS: dict[str, str] = {"chat_send": "chat-send.v1.schema.json"}
TASK_DEFAULT_CONTRACT_VERSIONS: dict[str, str] = {"chat_send": "chat-send.v1"}


def register_task_contract_schema(task_type: str, schema_filename: str, *,
                                  default_contract_version: str = "") -> None:
    """登记任务类型的外壳契约 schema（api/contracts/ 下文件名）与默认契约版本。"""
    TASK_CONTRACT_SCHEMAS[task_type] = schema_filename
    if default_contract_version:
        TASK_DEFAULT_CONTRACT_VERSIONS[task_type] = default_contract_version
TASK_REQUEST_FORMAT_CHECKER = jsonschema.FormatChecker()


@TASK_REQUEST_FORMAT_CHECKER.checks("date-time")
def _task_timestamp_format(value) -> bool:
    if not isinstance(value, str):
        return True
    if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            value):
        return False
    try:
        return parse_iso(value) is not None
    except ValueError:
        return False


class DisabledSupervisionManager:
    """历史托管接口占位；WX 业务监听由 8787 负责。

    Arm Fleet 核心仍保留旧 supervision API 的形状，方便升级期间的旧
    控制台调用，但不会创建微信监听器、执行身份判断或读取 WX 业务数据。
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self._records: dict[str, dict] = {}

    def initialize_devices(self):
        for device in self.runtime.devices.all():
            device_id = str(device["device_id"])
            self._records.setdefault(device_id, {"device_id": device_id, "allowed_users": []})

    async def shutdown(self):
        return None

    def _view(self, device_id: str) -> dict:
        record = self._records.setdefault(device_id, {"device_id": device_id, "allowed_users": []})
        return {
            "device_id": device_id,
            "status": "stopped",
            "status_display": "未开始托管",
            "compatibility_enabled": False,
            "running": False,
            "allowed_users": [],
            "allowed_user_count": 0,
            "last_reason": record.get("last_reason"),
            "events": [],
            "说明": "Arm Fleet 当前仅提供通用硬件能力；微信业务监听由 WX 项目负责。",
        }

    def view(self, device_id: str) -> dict:
        return self._view(device_id)

    def events(self, device_id: str) -> list[dict]:
        return []

    def stop(self, device_id: str, reason: str = "") -> dict:
        record = self._records.setdefault(device_id, {"device_id": device_id, "allowed_users": []})
        record["last_reason"] = reason
        return self._view(device_id)

    def configure(self, device_id: str, allowed_users) -> dict:
        raise ApiError("wx_compat_disabled", "回复名单由 WX 8787 管理，Arm Fleet 不保存业务名单")

    async def start(self, device_id: str, context: dict) -> dict:
        raise ApiError("wx_compat_disabled", "WX 业务监听由 8787 负责，请在 WX 页面开始托管")


class _PreEnableReporter(Reporter):
    def _base_url(self) -> str:
        # Reporter 会重读环境变量，配置中的空地址不足以冻结回调。
        return ""


class ArmFleetRuntime(PluginCapabilityMixin):
    def __init__(self, config: PlatformConfig | None = None, *, db_path=None, evidence_dir=None, plugin_dir=None):
        self.config = config or load_platform_config()
        self.store = Store(db_path or self.config.db_path)
        self.events = EventBus()
        self.audit = AuditLog(self.store, self.events)
        self.evidence = EvidenceStore(self.store, evidence_dir or self.config.evidence_dir)
        self.capabilities = CapabilityRegistry(self.store)
        self.devices = DeviceManager(
            self.store, self.audit, self.events,
            hardware_mode=self.config.hardware_mode,
            mac_mini_id=str(self.config.get("mac_mini_id")),
            hotplug_discovery_enabled=bool(self.config.get("hotplug_discovery_enabled", True)),
            discovery_interval_seconds=float(self.config.get("hotplug_discovery_interval_seconds", 2)),
            camera_broker_max_age_seconds=float(self.config.get("camera_broker_max_age_seconds", 1.5) or 1.5),
            config_root=self.config.root,
        )
        from runtime.layout_observation import LayoutObservationService
        self.layout_observation = LayoutObservationService(self.layout_bundle, self.devices.get)
        self.devices.set_layout_observation_provider(self.layout_observation.resolve)

        def _device_region_context(device_id):
            # S1/S2：本台生效粗框上下文（legacy=None；新模式缺框/损坏抛错）
            from runtime.phone_region_context import load_region_context
            return load_region_context(self.layout_bundle()["region_store"], device_id)
        self.devices.set_region_context_provider(_device_region_context)
        if self.pre_enable_maintenance:
            # provider=None 会在设备只读查询时懒创建默认 IME 服务。
            self.devices.set_input_status_provider(lambda: [])
        self.leases = LeaseManager(self.store, self.devices)
        # C 组：拔出/重绑定/禁用时撤销租约；绑定菜单变更写回绑定表
        self.devices.set_lease_manager(self.leases)
        self.devices.set_config_persistence(self._update_binding_table)
        self.plugins = PluginManager(self.store, self.audit, self.events, plugin_dir or self.config.plugin_dir)
        self.executor = TaskExecutor(self)
        # §10 回调持久队列：客户消息/任务结果回调失败入队，按退避重试。
        from runtime.callback_outbox import CallbackOutbox

        self.callback_outbox = CallbackOutbox(self.store, self.audit)
        reporter_type = _PreEnableReporter if self.pre_enable_maintenance else Reporter
        self.reporter = reporter_type(
            self.config, self.audit, self.evidence,
            callback_outbox=None if self.pre_enable_maintenance else self.callback_outbox)
        self.takeover_bridge = TakeoverBridge(self.config, self.audit)
        self.estop = EstopController(
            self.store, self.audit, self.events, self.devices,
            None if self.pre_enable_maintenance else self.reporter)
        # R2-21：配对部署的每机独立几何凭据登记（token → ime_device_id）。
        # 无 trust 的既有 IP 直连部署保留唯一绑定门，不使用配对凭据门。
        self._phone_credentials: dict[str, str] = {}
        self._phone_credentials_lock = threading.Lock()
        # R3-17：手机配对服务（仅当 data/ime_pairing.db 已离线初始化时挂载，
        # 不静默重建空库）；邀请信任材料是部署输入（env/配置），未配置拒绝。
        self.ime_pairing = None
        self.ime_pairing_trust = self._load_ime_pairing_trust()
        # R3-02：TAP 边界几何门按 nonce 寄存（闭包不进 target_ref/params，
        # 避免随任务进度/证据序列化）；_prepare_input_effect 取出随 input_claim
        # 下沉到驱动传输锁边界。
        self._geometry_gates: dict[str, object] = {}
        self._geometry_gate_lock = threading.Lock()
        self.views = ViewSessionManager(self.store, self.devices, self.audit, self.events, self.config)
        # 业务监听属于可选兼容插件。默认关闭时，Arm Fleet 仍可独立
        # 运行机械臂、daemon、输入法、摄像头和校准；显式开启后才加载旧监听。
        if bool(self.config.get("wx_compat_enabled", False)):
            from tasks.supervision import SupervisionManager

            self.supervision = SupervisionManager(self)
        else:
            self.supervision = DisabledSupervisionManager(self)
        # §4.8 G01：输入控制唯一实例（记录层+设备短锁）。资源排他锁在
        # startup 装配（P07 锁序），此处仅建实例不夺锁。
        from runtime.input_control import InputControl
        self.input_control = InputControl(self.store, self.config.db_path)
        # FIX-04 换绑段装配：换手机流程落 binding_change 意图并系统关闭原许可
        # 需要 InputControl；未注入时设备菜单换手机必须拒绝（不静默走旧路径）。
        self.devices.set_input_control(self.input_control)
        self.input_startup_reconciling = self.pre_enable_maintenance
        # FIX-02 装配段：Runtime 持有的挂载 IME 服务引用（api 投影/关机序使用）。
        # 经 _attach_screen_ime 注入，不是全局单例。
        self.screen_ime = None
        # FIX-15（§11.13）：Runtime 私有几何服务实例（禁止跨 Runtime 全局
        # service）。geometry 三端点身份反查由此注入（ime_geometry_identity）。
        from runtime.ime_geometry import ImeGeometryService
        self.ime_geometry = ImeGeometryService()
        self.ime_geometry.set_identity_verifier(self.ime_geometry_identity)
        self.daemon_supervisor = DaemonSupervisor(
            self.config.root,
            hardware_mode=self.config.hardware_mode,
            enabled=bool(self.config.get("daemon_supervisor_enabled", True)),
            python_executable=(self.config.get("daemon_supervisor_python") or None),
            script_path=self.config.get("daemon_supervisor_script"),
            log_dir=self.config.get("daemon_supervisor_log_dir"),
            poll_interval_seconds=float(self.config.get("daemon_supervisor_poll_interval_seconds", 1.0)),
            restart_delay_seconds=float(self.config.get("daemon_supervisor_restart_delay_seconds", 1.0)),
            max_restarts=int(self.config.get("daemon_supervisor_max_restarts", 5)),
            event_handler=self._handle_daemon_event,
        )
        self._exec_tasks: dict[str, asyncio.Task] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._callback_flush_task: asyncio.Task | None = None
        self._running = False

    @staticmethod
    def _load_ime_pairing_trust() -> dict | None:
        """R3-17 部署输入：ARMFLEET_IME_PAIRING_TRUST_FILE（0600 JSON）→ trust。

        字段 https_origin / ca_cert_b64 / spki_sha256（与配对路由消费形状一致）；
        未配置返回 None（现有直连模式）。显式配置但读取/结构错误则启动失败，
        不得把配置失效当成未配置而关闭手机身份认证。
        """
        import json as _json
        import os as _os
        raw = (_os.environ.get("ARMFLEET_IME_PAIRING_TRUST_FILE") or "").strip()
        if not raw:
            return None
        try:
            doc = _json.loads(Path(raw).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("配对信任配置无法读取或解析，拒绝启动") from None
        if not isinstance(doc, dict):
            raise ValueError("配对信任配置必须是对象，拒绝启动")
        trust = {key: doc.get(key)
                 for key in ("https_origin", "ca_cert_b64", "spki_sha256")}
        if not all(isinstance(value, str) and value.strip()
                   for value in trust.values()):
            raise ValueError("配对信任配置缺少必要字段，拒绝启动")
        return trust

    # ================= 生命周期 =================
    @property
    def pre_enable_maintenance(self) -> bool:
        return bool(self.config.get("pre_enable_maintenance", False))

    def input_switch_allowed(self, device_id: str) -> bool:
        """A-开关（2026-10-03 规格 §4）：操作者持久选择；损坏/缺失/历史事故=禁止。"""
        if self.pre_enable_maintenance:
            return False
        try:
            data_root = self.devices.settings_data_root(device_id)
            # R2-02：统一历史核对入口（丢失/回退/损坏一律禁止）
            stored = self.input_control.load_settings_verified(
                device_id, data_root=data_root)
        except Exception:
            return False
        return stored.get("enabled") is True

    def check_input_owner(self, task: dict) -> dict | None:
        """P03+FIX-04（§4.8.4/§4.5.2/§4.8.6）：输入任务执行前核对许可记录。

        - 缺 owner：旧任务不补办——终结任务并留 legacy_input_review_required；
        - owner 已撤销，或 settings_revision 落在撤销边界及以下/超出当前版本：
          许可失效，任务终结（查询/取消保留，不复活）；
        - FIX-04-3：owner 的 binding_revision 必须等于 DeviceManager.binding_view
          的本设备登记修订（不是相机服务全局诊断 revision）；owner 授权手机必须
          与当前唯一绑定手机一致；设备存在任一未解决 guard 问题即阻断执行。
        纯导航任务无 owner 不受影响；迟到任务进度不恢复许可（记录独立成表）。
        """
        from runtime import input_settings as _iset
        from runtime.input_settings import InputSettingsError
        from runtime.input_control import record_id_for_guard, record_id_for_owner
        device_id = task["device_id"]  # noqa: F841 — 保留语义注释
        owner = self.store.input_record_get(
            record_id_for_owner("fleet_task", task["task_id"]))
        if owner is None:
            try:
                self.input_control.ensure_guard(device_id)
                self.input_control.open_guard_issue(
                    device_id, code="legacy_input_review_required",
                    detail={"task_id": task["task_id"]})
            except Exception:
                pass
            return {"category": "input_authorization_missing",
                    "message": "任务缺少输入许可记录（旧任务不补办），不执行；请人工核对"}
        try:
            # R2-02：启动/派发前统一历史核对（known_check + checkpoint 回退核对）
            settings = self.input_control.load_settings_verified(
                device_id, data_root=self.devices.settings_data_root(device_id))
        except InputSettingsError as exc:
            # 设置丢失/损坏/回退/迁移阻断：旧 owner 不借异常状态继续执行。
            # 类别固定用已登记驱动错误类别；细节进消息。
            return {"category": "input_authorization_revoked",
                    "message": f"输入设置不可安全解释，旧任务不执行：{exc.message}"}
        boundary = int(settings.get("revoked_through_revision") or 0)
        current = int(settings.get("revision") or 0)
        revision = int(owner.get("settings_revision") or 0)
        if owner.get("revocation") == "revoked" or not boundary < revision <= current:
            return {"category": "input_authorization_revoked",
                    "message": (f"任务输入许可已失效（许可版本 {revision}，撤销边界 "
                                f"{boundary}，当前 {current}）：不执行、不复活；"
                                "重新输入请创建新授权任务")}
        # FIX-04-3a：本设备登记绑定修订一致（DeviceManager.binding_view）。
        try:
            registered_revision = int(
                self.devices.binding_view(device_id).get("binding_revision") or 0)
        except Exception as exc:
            return {"category": "input_authorization_revoked",
                    "message": f"无法读取设备登记绑定，旧许可不执行：{exc}"}
        if int(owner.get("binding_revision") or 0) != registered_revision:
            return {"category": "input_authorization_revoked",
                    "message": (f"任务许可登记绑定修订 "
                                f"{int(owner.get('binding_revision') or 0)} 与当前设备登记修订 "
                                f"{registered_revision} 不一致（input_binding_mismatch：换绑后"
                                "旧许可不复活）；请创建新授权任务")}
        # FIX-04-3b：手机绑定一致——owner 的授权手机必须与设备当前保存的
        # 授权手机一致（关闭/换绑会移动保存授权；再次开启必须用新手机显式
        # 授权）。当前唯一运行时绑定由受理门禁（binding_state mismatch）与
        # IME 钩子身份核对把关，此处不重复评估实时在线状态。
        saved_ime = str(settings.get("ime_device_id") or "")
        owner_ime = str(owner.get("ime_device_id") or "")
        if saved_ime and owner_ime not in ("", "unbound") \
                and owner_ime != saved_ime:
            return {"category": "input_authorization_revoked",
                    "message": (f"任务许可授权手机 {owner_ime} 与设备当前保存授权手机 "
                                f"{saved_ime} 不一致（input_binding_mismatch：换手机后"
                                "旧 owner 即使再次开启也不可执行）；请创建新授权任务")}
        # FIX-04-3c：guard 一致——任一未解决问题阻断新输入执行（对账/取消除外）。
        guard = self.store.input_record_get(record_id_for_guard(device_id))
        if isinstance(guard, dict):
            open_codes = sorted({str(issue.get("code")) for issue in guard.get("issues") or []
                                 if isinstance(issue, dict) and issue.get("state") == "open"})
            if open_codes:
                return {"category": "input_authorization_revoked",
                        "message": (f"设备存在未解决输入恢复问题（{'、'.join(open_codes)}）："
                                    "不执行新输入；取消/对账保留")}
        return None

    # ---- P04：输入 effect 固定 slot 准备与最后认领（§4.8.4） ----
    _CLICK_EFFECT_TARGETS = {
        "send_button": "message_send",
        "ime_candidate_bar": "candidate_commit",
        "ime_delete_key": "editor_clear",
    }

    def _input_effect_kind(self, capability_id: str, params: dict) -> str | None:
        if capability_id == "input.type_text":
            return "text_type"
        if capability_id == "input.ime_create":
            return "ime_create"
        if capability_id == "arm.click":
            target = params.get("target_id") if isinstance(params, dict) else None
            target = str(target)
            if target in self._CLICK_EFFECT_TARGETS:
                return self._CLICK_EFFECT_TARGETS[target]
            if target.startswith("kb:"):
                return "text_type"  # 逐键输入：每次按键分别认领（§4.8.4）
        return None  # 聚焦输入框/打开聊天/返回/导航点击不是受控副作用

    def _prepare_input_effect(self, stored: dict, task: dict,
                              effect_kind: str, params: dict) -> dict:
        """服务端固定 slot 并持久 prepared；认领回调经 params 下沉到驱动
        最后下发边界（sendall/接触模拟/输入缓冲前）同步执行（§4.8.4-5）。

        调用序号由运行时分配并随任务进度持久（插件不可指定 slot）；
        认领失败=禁止下发（零动作拒绝，收据按未开始处理）。
        """
        from runtime.input_control import InputControlError, record_id_for_owner
        device_id = task["device_id"]
        verdict = self.check_input_owner(stored)
        if verdict is not None:
            raise _DriverError(verdict["category"], verdict["message"])
        seq = int(stored.get("input_effect_seq") or 0) + 1
        slot_key = [f"step-{int(task.get('step_cursor') or 0) + 1}", seq, effect_kind]
        ime_task_id = (f"{task['task_id']}-ime" if effect_kind == "ime_create" else None)
        effect = self.input_control.build_effect(
            owner_id=record_id_for_owner("fleet_task", task["task_id"]),
            device_id=device_id, slot_key=slot_key,
            payload=params if isinstance(params, dict) else {},
            ime_task_id=ime_task_id)
        effect, _created = self.input_control.insert_effect(effect)
        task["input_effect_seq"] = seq

        def claim_callback() -> dict:
            """驱动边界最后认领（R2-01）：短锁内重验全部授权事实后 CAS
            prepared→claimed；任一失效不认领（零接触/零文本，拒绝不抹掉
            已发生的走位事实）。短锁只做本地读与 CAS，不跨网络等待。
            重验范围（§4.8.4-5/R2-01）：owner/开关/撤销边界/唯一手机/绑定
            修订/guard（check_input_owner）+ 任务取消/急停 + 运行时资格
            （设备/校准/手机在线）+ 冻结 payload 摘要（不含后注入 input_claim）。
            """
            from runtime.input_control import InputControlError as _ICE, \
                canonical_digest, record_id_for_owner
            try:
                with self.input_control.device_lock(device_id):
                    verdict = self.check_input_owner(task)
                    if verdict is not None:
                        error = _DriverError(
                            verdict["category"],
                            f"最后认领重验失败，未下发：{verdict['message']}")
                        error.motion_not_started = True
                        raise error
                    task_row = self.store.get_task(task["task_id"])
                    if task_row is not None and (
                            task_row.get("status") in (
                                "confirmed", "failed", "unknown", "cancelled",
                                "expired")
                            or task_row.get("cancel_requested")
                            or task_row.get("stop_result_unknown")):
                        error = _DriverError(
                            "input_authorization_revoked",
                            f"任务已取消/终态（{task_row.get('status')}），"
                            "最后认领拒绝，未下发")
                        error.motion_not_started = True
                        raise error
                    if self.estop.active:
                        error = _DriverError("estop_active",
                                             "急停生效中：最后认领拒绝，未下发")
                        error.motion_not_started = True
                        raise error
                    # owner-手机一致性已由 check_input_owner 核对；此处不向
                    # eligibility 传 ime_device_id（否则会击穿仿真设备的
                    # 无手机心跳快捷路径，使存量仿真测试全红）。
                    eligibility = self.devices.eligibility(
                        device_id, require_input=True)
                    # device_busy 是"本动作正在执行"的固有状态：最后认领发生在
                    # 动作中，不得自否；其余阻断（离线/校准/开关/手机/键盘/
                    # 能力门）任一出现即拒绝。
                    blocking = [code for code in
                                (eligibility.get("blocking_checks") or [])
                                if code != "device_busy"]
                    if blocking:
                        error = _DriverError(
                            "input_switch_disabled",
                            "最后认领重验：运行时资格不满足"
                            f"（{'、'.join(blocking)}），未下发")
                        error.motion_not_started = True
                        raise error
                    fresh = self.store.input_record_get(effect["record_id"])
                    presented = {k: v for k, v in params.items()
                                 if k != "input_claim"}
                    if fresh is None or fresh.get("state") != "prepared" \
                            or fresh.get("payload_digest") != effect["payload_digest"] \
                            or canonical_digest(presented) != effect["payload_digest"]:
                        error = _DriverError(
                            "input_switch_disabled",
                            "输入副作用记录已被演化或下发参数与认领摘要不一致："
                            "最后认领拒绝，未下发")
                        error.motion_not_started = True
                        raise error
                    claimed = self.input_control.claim_effect(
                        effect["record_id"], expected_revision=effect["revision"])
            except _ICE as exc:
                error = _DriverError(
                    "input_switch_disabled",
                    f"输入副作用最后认领被拒绝，未下发：{exc.message}")
                error.motion_not_started = True  # 回调即最后边界：确定未下发
                raise error from exc
            task["pending_input_effect"] = {"record_id": claimed["record_id"],
                                            "revision": claimed["revision"]}
            return {"record_id": claimed["record_id"],
                    "revision": claimed["revision"]}

        task["pending_input_effect"] = {"record_id": effect["record_id"],
                                        "revision": effect["revision"]}
        self._save_task_progress(task)
        if isinstance(params, dict):
            params["input_claim"] = {"record_id": effect["record_id"],
                                     "callback": claim_callback}
            # R3-02：live 几何门随认领下沉到驱动传输锁边界（先几何后认领）
            target_ref = params.get("target_ref")
            nonce = target_ref.get("nonce") if isinstance(target_ref, dict) else None
            if nonce:
                with self._geometry_gate_lock:
                    gate = self._geometry_gates.pop(nonce, None)
                if gate is not None:
                    params["input_claim"]["geometry_gate"] = gate
        return effect

    def _finish_pending_input_effect(self, task: dict, receipt_state: str) -> None:
        effect_ref = task.pop("pending_input_effect", None)
        if not effect_ref:
            return
        try:  # R3-02：清理未消费的几何门（防长进程缓涨）
            with self._geometry_gate_lock:
                if len(self._geometry_gates) > 64:
                    self._geometry_gates.clear()
        except Exception:
            pass
        try:
            record = self.store.input_record_get(effect_ref["record_id"])
            if not record:
                return
            if record["state"] == "prepared":
                # 认领回调未成功（零动作拒绝）：仅可靠的未开始收尾
                if receipt_state == "not_started":
                    self.input_control.mark_effect_not_started(
                        effect_ref["record_id"],
                        expected_revision=effect_ref["revision"])
                return
            if receipt_state == "not_started":
                self.input_control.mark_effect_not_started(
                    effect_ref["record_id"], expected_revision=effect_ref["revision"])
            else:
                self.input_control.finish_effect(
                    effect_ref["record_id"], expected_revision=effect_ref["revision"],
                    state="completed" if receipt_state == "confirmed" else "unknown")
        except Exception:
            pass  # 回执收尾失败不改变任务结果；记录保持 claimed=可能已动作

    async def startup(self, *, register_devices: bool = True, device_configs: list[dict] | None = None) -> None:
        if self.pre_enable_maintenance:
            await self._startup_pre_enable(register_devices, device_configs)
            return
        # §4.8.6-4 启动顺序：资源排他锁 → 标记输入暂不可用 → 注册设备与绑定
        # → 核对设置提交意图 → 装载 owner/effect/guard → 按撤销边界扫旧任务
        # → recover_tasks 其余安全分支 → 派发。任一步失败不得变成无锁模式。
        self.input_startup_reconciling = True
        # R3-17：挂载配对身份库（存在才挂；server_id 缺失时认证自然 401，
        # 不静默重建空库——离线初始化工具负责首次建立）。
        pairing_db = Path(self.config.root) / "data" / "ime_pairing.db"
        if pairing_db.exists():
            from runtime.ime_pairing import ImePairingService
            self.ime_pairing = ImePairingService(pairing_db)
        self.capabilities.bootstrap()
        self._reload_activated_plugins()
        configs = device_configs if device_configs is not None else load_device_configs(self.config.root)
        data_roots = {self.config.root / "data"}
        for cfg in configs:
            root = cfg.get("data_root")
            if root:
                data_roots.add(Path(root))
        # 第二实例必须启动失败（fcntl 排他锁；失败即退出，不自动变无锁模式）
        self.input_control.acquire_resource_locks(sorted(data_roots, key=str))
        cleared_leases = self.leases.clear_all()
        if cleared_leases:
            self.audit.record("driver_selfcheck", reason=f"重启恢复：{cleared_leases} 个租约全部失效")
        if register_devices:
            # 先确保 daemon 监听进程存在，再注册设备。监督器不会发送 CONNECT；
            # 真正打开串口仍由人工调试操作触发，避免启动时复位硬件。
            await self.daemon_supervisor.start(configs)
            await self.devices.register_all(configs)
        else:
            await self.daemon_supervisor.start([])
        self.supervision.initialize_devices()
        self._reconcile_input_state(configs if register_devices else [])
        await self.devices.start_background()
        await self.views.start_background()
        # R2-07（滑动附件 README §7.3）：启动扫描先于 recover_tasks——
        # scroll_action_records 的 prepared/claimed 残留先重建阻断，再恢复任务；
        # 扫描失败按记录审计并保持存储层 gate，不当空表跳过。
        try:
            from runtime.calibrated_scroll import get_scroll_action_store
            scan = get_scroll_action_store(self).startup_scan()
            self.audit.record("scroll_startup_scan", operator_id="system",
                              reason=f"prepared_aborted={scan.get('prepared_aborted')}, "
                                     f"claimed_to_review={scan.get('claimed_to_review')}")
        except Exception as exc:
            self.audit.record("scroll_startup_scan_failed", operator_id="system",
                              reason=str(exc))
        self.recover_tasks()
        self.input_startup_reconciling = False
        # R2-19（§9）：布局存储启动对账（unknown 阻断新写，只读 GET 不受影响）
        try:
            bundle = self.layout_bundle()
            self._layout_reconcile_report = bundle["state_store"].reconcile()
        except Exception as exc:  # 布局库异常不阻断输入安全主链
            self._layout_reconcile_report = {"error": str(exc)}
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._callback_flush_task = asyncio.create_task(self._callback_flush_loop())

    async def _startup_pre_enable(self, register_devices: bool,
                                 device_configs: list[dict] | None) -> None:
        """只注册暂停镜像与读取相机；不进入普通启动的恢复或后台路径。"""
        self.input_startup_reconciling = True
        configs = device_configs if device_configs is not None else load_device_configs(self.config.root)
        roots = {self.config.root / "data"}
        roots.update(Path(cfg["data_root"]) for cfg in configs if cfg.get("data_root"))
        self.input_control.acquire_resource_locks(sorted(roots, key=str))
        try:
            validate_device_configs(configs)
            for cfg in configs:
                real = (self.config.hardware_mode == "real"
                        or cfg.get("binding_mode") == "real"
                        or cfg.get("driver_mode") in {"real", "daemon"})
                if real and cfg.get("driver_mode") != "daemon":
                    raise ApiError("device_config_invalid", "启用前维护只允许 daemon 真机绑定，禁止串口直连或隐式回退")
                if real and not cfg.get("camera_frame_url"):
                    raise ApiError("device_config_invalid", "启用前维护必须读取已有相机帧桥，禁止直接打开摄像头")
            if register_devices:
                self.devices._registered_configs = [dict(cfg) for cfg in configs]
                for cfg in configs:
                    device_id = cfg["device_id"]
                    previous = self.store.get_device(device_id) or {}
                    device = self.devices.register_device({**cfg, "calibration_status": "due"})
                    hold = self.devices._startup_holds.pop(device_id, None) or "paused"
                    device.update(
                        status=hold, status_display=display(DEVICE_STATUS, hold),
                        status_reason="启用前维护冻结，只读检查",
                        pause_reason=previous.get("pause_reason") or "启用前维护冻结",
                        device_mode="unmanaged", device_mode_display=DEVICE_MODE["unmanaged"],
                        daemon_session=None, lease_id=previous.get("lease_id"),
                    )
                    self.store.save_device(device)
                    camera = self.devices.camera(device_id)
                    try:
                        await camera.open()
                        quality = await camera.check_quality()
                        self.devices._record_camera_source_metadata(device_id, camera)
                    except Exception as exc:
                        quality = {
                            **(getattr(exc, "diagnostics", None) or {}),
                            "quality": "abnormal",
                            "quality_display": display(CAMERA_QUALITY, "abnormal"),
                        }
                    device["camera_quality"] = quality["quality"]
                    device["camera_quality_display"] = quality["quality_display"]
                    device["last_camera_check_at"] = iso(utc_now())
                    self.devices._record_camera_quality_diagnostics(device, quality)
                    self.store.save_device(device)
            self._running = True
        except BaseException:
            await self.shutdown()
            raise

    def _reconcile_input_state(self, configs: list[dict]) -> None:
        """核对设置提交意图 → 装载记录 → 按撤销边界扫旧 owner（§4.8.6-4/§4.5.2）。"""
        from runtime import input_settings as _iset
        from runtime.input_control import InputControlError, record_id_for_effect
        for cfg in configs:
            device_id = str(cfg.get("device_id") or "")
            if not device_id:
                continue
            data_root = self.devices.settings_data_root(device_id)
            try:
                # 1) 核对 prepared 设置意图（P02 对账：匹配 after=applied/
                #    before+关闭=完成关闭/before+开启=aborted/双不匹配=保持阻断）
                while True:
                    pending = self.input_control.has_prepared_intent(device_id)
                    if pending is None:
                        break
                    self.input_control._reconcile_settings_intent(
                        device_id, pending, data_root=data_root)
                # 2) 按撤销边界扫旧 owner：active 且 settings_revision ≤ 边界
                settings = _iset.load_settings(device_id, data_root)
                boundary = int(settings.get("revoked_through_revision") or 0)
                for owner in self.store.input_records_by_device(device_id, "owner"):
                    if owner.get("revocation") == "active"                             and int(owner.get("settings_revision") or 0) <= boundary:
                        self.input_control.revoke_owner(
                            owner["record_id"],
                            expected_revision=owner["revision"],
                            revoked_at_revision=boundary)
                    if owner.get("owner_type") == "direct_ime":
                        task_id = str(owner["owner_ref"])
                        effect = self.store.input_record_get(record_id_for_effect(
                            owner["record_id"], ["ime:" + task_id, 0, "ime_create"]))
                        if effect and effect.get("state") in ("claimed", "unknown"):
                            # 新进程已独占资源：原创建不会再晚到；分库窗口只核对，
                            # 不把存在的 pending 行当作允许继续输入的完成回执。
                            if effect["state"] == "claimed":
                                self.input_control.finish_effect(
                                    effect["record_id"], expected_revision=effect["revision"],
                                    state="unknown")
                            self.input_control.open_guard_issue(
                                device_id, code="input_effect_unknown", owner_id=owner["record_id"],
                                detail={"ime_task_id": task_id, "effect_kind": "ime_create"})
            except InputControlError as exc:
                # 损坏/对账受阻：记 guard 阻断（禁止新输入认领），不重建空记录
                try:
                    self.input_control.ensure_guard(device_id)
                    self.input_control.open_guard_issue(
                        device_id, code="input_record_invalid",
                        detail={"reason": str(exc)})
                except Exception:
                    pass
            except (OSError, ValueError):
                continue

    # ---- FIX-12：布局资料服务（Runtime 持有；region/template/adaptation 共库） ----
    @property
    def layout_db_path(self) -> Path:
        """布局库默认路径：解析自同一配置根（config.root/data/layout.db），
        不再使用进程 cwd 下的 data/layout.db。测试可用 app.state.layout_db 覆盖。"""
        configured = self.config.get("layout_db_path")
        if configured:
            return Path(str(self.config._resolve_path(configured)))
        return self.config.root / "data" / "layout.db"

    def layout_bundle(self, db_path=None) -> dict:
        """构建并缓存布局资料服务束：region service/store、模板 store、
        共享 KV store 与包 blob 目录，全部解析到同一 SQLite 布局库。"""
        db = Path(db_path) if db_path is not None else self.layout_db_path
        cached = getattr(self, "_layout_bundle_cache", None)
        if cached is not None and str(cached["db_path"]) == str(db):
            return cached
        db.parent.mkdir(parents=True, exist_ok=True)
        from runtime.layout_region import LayoutRegionService, LayoutRegionStore
        from runtime.layout_template import LayoutTemplateStore

        class _KVStore:
            """L04/L06 共享 KV（layout_kv 表），与布局库同文件。"""

            def __init__(self, path: Path):
                import sqlite3 as _sq
                self._conn = _sq.connect(str(path), check_same_thread=False)
                self._conn.row_factory = _sq.Row
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

        calibration_data_root = self.config.root / "data"
        region_store = LayoutRegionStore(db, calibration_data_root=calibration_data_root)
        template_store = LayoutTemplateStore(db)
        # R2-25/R2-19（D 包交付）：typed state store + ops/适配服务由本
        # Runtime 束持有（无进程级单例）；Clock 注入本 Runtime 时钟。
        from runtime.layout_state import LayoutStateStore
        from runtime.layout_ops import CatalogTemplateResolver, LayoutOpsService
        from runtime.layout_adaptation import LayoutAdaptationService
        import uuid as _uuid
        state_store = LayoutStateStore(db, data_root=db.parent)
        kv_store = _KVStore(db)
        ops_svc = LayoutOpsService(
            kv_store, blobs_dir=db.parent / "layout_packages",
            region_store=region_store, template_store=template_store,
            state_store=state_store, data_root=db.parent)
        # R2-20：适配服务与 ops 共用同一解析器（start_session 模板核验=GET 路径）
        adaptation_svc = LayoutAdaptationService(
            store=kv_store,
            template_store=CatalogTemplateResolver(ops_svc, template_store),
            state_store=state_store,
            server_instance_id=f"fleet-{_uuid.uuid4()}")
        bundle = {
            "db_path": db,
            "calibration_data_root": calibration_data_root,
            "kv_store": kv_store,
            "region_store": region_store,
            "region_svc": LayoutRegionService(region_store),
            "template_store": template_store,
            "state_store": state_store,
            "ops_svc": ops_svc,
            "adaptation_svc": adaptation_svc,
            "blobs_dir": db.parent / "layout_packages",
        }
        self._layout_bundle_cache = bundle
        return bundle

    def _attach_screen_ime(self, service) -> None:
        """FIX-02 装配段（§4.8.5-10/§4.8.6-7）：Runtime 持有挂载 IME 服务引用，
        并把取消工作器接到输入控制通知（关闭写入后提前唤醒扫描）。"""
        self.screen_ime = service
        worker = getattr(getattr(service, "tasks", None), "cancellation_worker", None)
        if worker is not None:
            self.input_control.set_cancellation_notifier(worker.notify)

    def register_phone_credential(self, ime_device_id: str, token: str) -> None:
        """R2-21（§7.1）：登记每机独立几何凭据（测试/内部装配用）。

        生产部署的凭据登记走 R3-17 配对协议（ime_pairing）；本登记表仅供
        测试/内部装配，两类来源在 phone_identity_for_token 统一解析。
        未配置任何凭据时，几何三入口一律 401 拒绝（会话变更与 nonce 领取
        禁止），不回落路径绑定计数（R2-21 否定的旧语义）。
        """
        if not isinstance(token, str) or len(token) < 16:
            raise ApiError("invalid_request", "几何凭据令牌必须为 ≥16 字符字符串")
        with self._phone_credentials_lock:
            self._phone_credentials[token] = str(ime_device_id)

    def phone_identity_for_token(self, token: str) -> dict | None:
        """R3-17-P4：手机 LAN 入口统一身份解析（token → 身份视图）。

        优先 R3-17 配对凭据（生产；pending 仅可查/激活自身配对），其次
        R2-21 登记凭据（测试/内部装配，等价 active）。未命中 → None（路由
        层统一 401）。不使用路径/body/自报 header/IP 作身份。
        """
        if not isinstance(token, str) or not token:
            return None
        pairing = getattr(self, "ime_pairing", None)
        if pairing is not None:
            cred = pairing.verify_phone_token(token)
            if cred is not None:
                return cred
        with self._phone_credentials_lock:
            device = self._phone_credentials.get(token)
        if device is None:
            return None
        return {"ime_device_id": str(device), "credential_id": "registered",
                "generation": 0, "state": "active"}

    def revoke_phone_credential(self, ime_device_id: str) -> int:
        """撤销某手机的全部几何凭据，返回撤销条数。"""
        with self._phone_credentials_lock:
            doomed = [t for t, p in self._phone_credentials.items()
                      if p == str(ime_device_id)]
            for token in doomed:
                self._phone_credentials.pop(token, None)
            return len(doomed)

    def ime_geometry_identity(self, ime_device_id: str, credentials=None) -> None:
        """R2-21（§11.13+§7.1）/R3-17：几何三入口身份验证——先于一切会话/
        nonce 操作。

        - 无凭据/令牌未知 → 401 ime_geometry_unauthenticated（生产凭据来源
          = R3-17 配对 active 凭据；R2-21 登记表供测试/内部装配）；
        - 令牌有效但归属与路径手机不一致 → 403 ime_geometry_forbidden；
        - 该手机不是任何机械臂当前唯一绑定 → 403 ime_geometry_forbidden
          （校准档案是正式绑定源 input_binding）。
        配置配对 trust 的部署按上述凭据验证。未配置 trust 时与既有 IP
        直连模式一致，不要求配对令牌，但仍要求当前唯一机械臂绑定。
        """
        from runtime.ime_geometry import ImeGeometryError
        if self.ime_pairing_trust is None:
            self._ime_geometry_bound_arm(ime_device_id)
            return
        token = credentials.get("token") if isinstance(credentials, dict) else credentials
        if not isinstance(token, str) or not token:
            raise ImeGeometryError(
                401, "ime_geometry_unauthenticated",
                "缺少手机身份凭据（几何会话/nonce 需要每机独立配对令牌）")
        phone_id = None
        pairing = getattr(self, "ime_pairing", None)
        if pairing is not None:
            cred = pairing.verify_phone_token(token)
            if cred is not None:
                if cred.get("state") != "active":
                    raise ImeGeometryError(
                        401, "ime_geometry_unauthenticated",
                        "配对凭据未激活（pending 不能建立几何会话）")
                phone_id = cred["ime_device_id"]
        if phone_id is None:
            with self._phone_credentials_lock:
                phone_id = self._phone_credentials.get(token)
        if phone_id is None:
            raise ImeGeometryError(
                401, "ime_geometry_unauthenticated", "手机身份凭据无效或已撤销")
        if str(phone_id) != str(ime_device_id):
            raise ImeGeometryError(
                403, "ime_geometry_forbidden",
                f"凭据属于手机 {phone_id}，不能访问 {ime_device_id} 的几何会话")
        self._ime_geometry_bound_arm(ime_device_id)

    def _ime_geometry_bound_arm(self, ime_device_id: str) -> str:
        """Both deployment modes require one current arm binding."""
        from runtime.ime_geometry import ImeGeometryError
        bound: list[str] = []
        for device in self.devices.all():
            try:
                ime = self.devices.input_binding(device["device_id"]).get("ime_device_id")
            except Exception:
                continue
            if ime and str(ime) == str(ime_device_id):
                bound.append(str(device["device_id"]))
        if len(bound) != 1:
            raise ImeGeometryError(
                403, "ime_geometry_forbidden",
                f"手机 {ime_device_id} 不是任何机械臂的当前唯一绑定"
                f"（匹配 {len(bound)} 台），拒绝几何会话访问")
        return bound[0]

    async def _ime_live_geometry_target(self, device_id: str, target_id: str) -> dict:
        """FIX-15（§11.13.2）：候选/删除点击规划前的实时几何快照门。

        - 向本 Runtime 几何服务请求 nonce（目的=action_snapshot），等待手机
          长轮询领取并回快照（nonce≤2 秒、接收≤1 秒），fresh_snapshot 复核；
        - 失败最多再取一次只读快照重新复核，仍失败停止（零接触、零 prepared）；
        - 等待在线程池执行、不持输入短锁/几何锁，不阻塞事件循环；
          关闭与取消不被几何等待阻塞（F15-4）；
        - 成功返回 target_ref（屏幕像素坐标 + 会话/seq/nonce 证据），
          条件过期绝不落回旧固定坐标。
        """
        from runtime.ime_geometry import NONCE_TTL_S
        geometry = getattr(self, "ime_geometry", None)
        if geometry is None:
            error = _DriverError("ime_geometry_unavailable",
                                 "几何服务未装配，候选/删除点击禁止执行")
            error.motion_not_started = True
            raise error
        ime_device = self.devices.input_binding(device_id).get("ime_device_id")
        if not ime_device:
            error = _DriverError("input_binding_unavailable",
                                 "尚未确定唯一输入法手机，禁止候选/删除点击")
            error.motion_not_started = True
            raise error
        last_reason = "no_attempt"
        for attempt in range(2):  # 首次 + 最多一次只读重取
            nonce = geometry.create_nonce_request(ime_device,
                                                  purpose="click_planning")
            # R3-02-3：读取原子一致的已验证快照（校验与取快照同一锁），
            # 不再"验证后另取 latest"。
            ok, reason, snap = await asyncio.to_thread(
                geometry.wait_checked_snapshot, ime_device, nonce, NONCE_TTL_S,
                target_id)
            if ok:
                targets = {t.get("id"): t for t in snap.get("targets") or []
                           if isinstance(t, dict)}
                target = targets.get(target_id) or {}
                display = snap.get("display") or {}
                bounds = target.get("bounds")
                candidate_task = snap.get("current_task_id")
                if target.get("visible") and target.get("clickable") \
                        and isinstance(bounds, dict) \
                        and isinstance(display.get("width_px"), int) \
                        and isinstance(display.get("height_px"), int):
                    if target_id == "ime_candidate_bar" and not candidate_task:
                        last_reason = "candidate_task_missing"
                        continue
                    ref = {
                        "source": "live_ime_geometry",
                        "cameraX": (bounds["left"] + bounds["right"]) / 2.0,
                        "cameraY": (bounds["top"] + bounds["bottom"]) / 2.0,
                        "bounds": deepcopy(bounds),
                        "display": deepcopy(display),
                        "frameW": display["width_px"],
                        "frameH": display["height_px"],
                        "runtimeSessionId": snap.get("runtime_session_id"),
                        "viewInstanceId": snap.get("view_instance_id"),
                        "seq": snap.get("seq"),
                        "nonce": nonce,
                        "coordinateVersion": "live_ime_geometry.v1",
                        "currentTaskId": candidate_task,
                    }
                    # R3-02：TAP 边界几何门按 nonce 寄存（_prepare_input_effect
                    # 取出随 input_claim 下沉；闭包不进 target_ref）。
                    with self._geometry_gate_lock:
                        self._geometry_gates[nonce] = self._ime_geometry_final_gate(
                            device_id=device_id, ime_device=ime_device,
                            target_id=target_id, nonce=nonce,
                            expected_task=candidate_task,
                            expected_session=snap.get("runtime_session_id"),
                            expected_bounds=deepcopy(bounds),
                            target_ref=ref,
                            expected_display=deepcopy(display))
                    return ref
                last_reason = "target_not_executable"
                continue
            last_reason = reason
            if reason in ("geometry_invalid", "view_stale", "session_stale",
                          "snapshot_superseded"):
                continue  # 条件性失败：只读重取一次
            break  # nonce 过期/无快照：不空等第二次
        error = _DriverError(
            "ime_geometry_unavailable",
            f"实时键盘几何不可用（{last_reason}）：候选/删除点击已停止，"
            "不回退旧固定坐标；请确认手机输入法会话新鲜后重试")
        error.motion_not_started = True
        raise error

    def _ime_geometry_final_gate(self, *, device_id: str, ime_device: str,
                                 target_id: str, nonce: str,
                                 expected_task: str | None,
                                 expected_session, expected_display: dict,
                                 expected_bounds: dict | None = None,
                                 target_ref: dict | None = None):
        """R3-02：TAP 下发边界（传输锁取得后、sendall 前）的最终几何门。

        返回闭包供驱动在输入认领回调**之前**调用：凭据/当前唯一绑定复核 +
        几何服务 consume_for_click（同一把几何锁内核对快照年龄<=1s、最高
        seq/当前 View/目标可见可点/候选 task 未变，并一次性消费 nonce）。
        失败抛 DriverError 且**不置 motion_not_started**——走位已发生时
        走位事实保留，不被伪装为"完全未运动"；也不立即重取新坐标续点。
        闭包只做本地读，不持输入短锁、不等网络。
        """
        from runtime.ime_geometry import ImeGeometryError, NONCE_TTL_S
        planned_ref = deepcopy(target_ref)
        expected_view = (planned_ref or {}).get("viewInstanceId")
        state_lock = threading.Lock()
        state = {"nonce": nonce, "refresh_started": False,
                 "refresh_ready": False, "contact_attempted": False}

        def _require_planned_ref() -> None:
            if target_ref != planned_ref:
                raise _DriverError("ime_geometry_unavailable",
                                   "最终接触核验：目标范围或落点参数被修改，禁止 TAP")

        def _credential_and_binding_guard() -> None:
            # 配对部署须有有效凭据；IP 直连仍走下方唯一绑定复核。
            identity_ok = self.ime_pairing_trust is None
            pairing = getattr(self, "ime_pairing", None)
            if not identity_ok and pairing is not None:
                identity_ok = pairing.phone_has_active_credential(ime_device)
            if not identity_ok:
                with self._phone_credentials_lock:
                    identity_ok = any(
                        phone == ime_device
                        for phone in self._phone_credentials.values())
            if not identity_ok:
                raise _DriverError(
                    "ime_geometry_unavailable",
                    "最终接触核验：手机几何凭据已撤销/未配对，禁止 TAP")
            bound = str((self.devices.input_binding(device_id) or {})
                        .get("ime_device_id") or "")
            if bound != ime_device:
                raise _DriverError(
                    "ime_geometry_unavailable",
                    f"最终接触核验：设备当前绑定手机 {bound or '空'} 与"
                    f"规划手机 {ime_device} 不一致，禁止 TAP")
            if self._ime_geometry_bound_arm(ime_device) != device_id:
                raise _DriverError("ime_geometry_unavailable",
                                   "最终接触核验：手机唯一绑定已变化，禁止 TAP")

        def _require_planned_geometry(snapshot: dict) -> None:
            if (snapshot.get("runtime_session_id") != expected_session
                    or (expected_view is not None
                        and snapshot.get("view_instance_id") != expected_view)
                    or snapshot.get("display") != expected_display
                    or snapshot.get("current_task_id") != expected_task):
                raise _DriverError("ime_geometry_unavailable",
                                   "最终接触核验：会话、视图、显示器或输入任务已变化，禁止 TAP")
            current_target = next((item for item in snapshot.get("targets", [])
                                   if item.get("id") == target_id), {})
            if (not current_target.get("visible") or not current_target.get("clickable")
                    or (expected_bounds is not None
                        and current_target.get("bounds") != expected_bounds)):
                raise _DriverError("ime_geometry_unavailable",
                                   "最终接触核验：目标可点击范围已变化，禁止按旧范围点击")

        async def refresh_before_contact() -> None:
            # Travel and camera/session checks may outlast the 1s snapshot age.
            # Renew evidence once, outside transport/input locks; never replan XY.
            with state_lock:
                if state["refresh_started"] or state["contact_attempted"]:
                    raise _DriverError("ime_geometry_unavailable",
                                       "最终接触核验：同一 nonce 门不授权第二次点击")
                state["refresh_started"] = True
            _require_planned_ref()
            _credential_and_binding_guard()
            try:
                contact_nonce = self.ime_geometry.create_nonce_request(
                    ime_device, purpose="click_contact")
                ok, reason, snapshot = await asyncio.to_thread(
                    self.ime_geometry.wait_checked_snapshot, ime_device,
                    contact_nonce, NONCE_TTL_S, target_id)
                if not ok or snapshot is None:
                    raise _DriverError(
                        "ime_geometry_unavailable",
                        f"最终接触核验：新鲜几何不可用（{reason}），禁止 TAP")
                _require_planned_ref()
                _require_planned_geometry(snapshot)
            except ImeGeometryError as exc:
                raise _DriverError("ime_geometry_unavailable",
                                   f"最终接触核验失败（{exc.code}）：{exc.message}") from exc
            with state_lock:
                if state["contact_attempted"]:
                    raise _DriverError("ime_geometry_unavailable",
                                       "最终接触核验：nonce 门已使用，禁止 TAP")
                state["nonce"] = contact_nonce
                state["refresh_ready"] = True

        def gate() -> None:
            _require_planned_ref()
            with state_lock:
                if (state["contact_attempted"]
                        or (state["refresh_started"] and not state["refresh_ready"])):
                    raise _DriverError("ime_geometry_unavailable",
                                       "最终接触核验：nonce 门已使用或未完成刷新，禁止 TAP")
                state["contact_attempted"] = True
                contact_nonce = state["nonce"]
            try:
                snapshot = self.ime_geometry.consume_for_click(
                    ime_device, contact_nonce, target_id=target_id,
                    expected_task=expected_task,
                    expected_session=expected_session,
                    expected_display=expected_display,
                    guard=_credential_and_binding_guard)
                _require_planned_geometry(snapshot)
            except ImeGeometryError as exc:
                raise _DriverError(
                    "ime_geometry_unavailable",
                    f"最终接触核验失败（{exc.code}）：{exc.message}；"
                    "TAP 已停止，不按旧路径补点") from exc

        gate.refresh_before_contact = refresh_before_contact

        return gate

    def _fleet_owner_for_ime_task(self, ime_task_id: str) -> dict | None:
        """按原任务身份查 owner（owner:task:<fleet 任务> 或 owner:ime:<ref>）。"""
        from runtime.input_control import InputControlError, record_id_for_owner
        fleet_task_id = (ime_task_id[:-4] if ime_task_id.endswith("-ime")
                         else ime_task_id)
        owner = self.store.input_record_get(
            record_id_for_owner("fleet_task", fleet_task_id))
        direct_owner = self.store.input_record_get(
            record_id_for_owner("direct_ime", ime_task_id))
        if owner is not None and direct_owner is not None:
            error = InputControlError("同一输入法任务存在平台与直发两种归属，拒绝借用许可")
            error.hook_code = "input_owner_conflict"
            raise error
        return owner if owner is not None else direct_owner

    def _device_for_ime(self, ime_device: str) -> str:
        """直发及输入认领只能使用当前唯一的 Fleet 绑定。"""
        from runtime.input_control import InputControlError
        matches = [str(d["device_id"]) for d in self.devices.all()
                   if self.devices.input_binding(d["device_id"]).get("ime_device_id")
                   == ime_device]
        if len(matches) != 1:
            error = InputControlError(f"手机 {ime_device} 无唯一 Fleet 绑定（{len(matches)} 个）")
            error.hook_code = "input_binding_not_unique"
            raise error
        return matches[0]

    def ime_control_hooks_fleet(self) -> dict:
        """注入 Fleet 挂载输入法仓储的控制钩子：每次调用按手机反查唯一设备。

        - typing_claim/deliver 是输入副作用：先按当前绑定解析设备，再完整核对
          许可（owner + 撤销边界）；
        - cancel/ack/取消检查是安全对账：按原任务 owner 记录的身份（原手机）
          核对，不要求当前输入许可仍有效、不要求当前绑定仍指向原手机（§4.8.5）。
        """
        from runtime.input_control import InputControlError

        def wrapped(name: str, needs_device: bool):
            per_device = self.ime_control_hooks  # 方法引用，按设备取闭包
            def call(ime_task_id: str, ime_device: str, **kwargs):
                device_id = self._device_for_ime(ime_device)
                hooks = per_device(device_id)
                fn = hooks[name]
                return fn(ime_task_id, ime_device, **kwargs) if kwargs else fn(ime_task_id, ime_device)
            return call

        def identity_wrapped(name: str):
            def call(ime_task_id: str, ime_device: str, **kwargs):
                owner = self._fleet_owner_for_ime_task(ime_task_id)
                if owner is None:
                    # 无归属旧任务：IME 层取消/对账继续（§4.8.6-6），不补当前授权，
                    # 也不阻断其他设备。
                    return None
                if str(owner.get("ime_device_id") or "") != ime_device:
                    # 新手机冒认旧任务：拒绝
                    error = InputControlError(
                        f"手机 {ime_device} 与原任务归属 "
                        f"{owner.get('ime_device_id')} 不一致")
                    error.hook_code = "device_mismatch"
                    raise error
                hooks = self.ime_control_hooks(str(owner["device_id"]))
                fn = hooks[name]
                return fn(ime_task_id, ime_device, **kwargs) if kwargs else fn(ime_task_id, ime_device)
            return call

        def identity_wrapped_check(name: str):
            def call(ime_task_id: str, ime_device: str) -> bool:
                owner = self._fleet_owner_for_ime_task(ime_task_id)
                if owner is None:
                    return True  # 无归属旧任务：按旧任务取消（不补当前授权）
                if str(owner.get("ime_device_id") or "") != ime_device:
                    return True  # 手机已换：原候选不再归当前手机
                hooks = self.ime_control_hooks(str(owner["device_id"]))
                check = hooks.get(name)
                return bool(check(ime_task_id, ime_device)) if check else False
            return call

        def recover_direct(ime_task_id: str, ime_device: str, *, delivered: bool) -> None:
            # 已完成 inserted 等历史行不进入本回调；未结束的旧直发候选在
            # 重启后只能取消/核对，人工再次点击也不能沿用旧开始许可。
            owner = self._fleet_owner_for_ime_task(ime_task_id)
            if owner is None or owner.get("owner_type") != "direct_ime":
                return
            with self.input_control.device_lock(str(owner["device_id"])):
                owner = self._fleet_owner_for_ime_task(ime_task_id)
                if owner is not None and delivered:
                    self.input_control.open_guard_issue(
                        str(owner["device_id"]), code="editor_review_required",
                        owner_id=owner["record_id"],
                        detail={"ime_task_id": ime_task_id,
                                "reason": "direct_input_interrupted",
                                "candidate_and_editor_review_required": True})
                if owner is not None and owner.get("revocation") == "active":
                    self.input_control.revoke_owner(
                        owner["record_id"], expected_revision=owner["revision"],
                        revoked_at_revision=int(owner["settings_revision"]))

        return {"direct_create": wrapped("direct_create", True),
                "recover_direct": recover_direct,
                "typing_claim": wrapped("typing_claim", True),
                "deliver": wrapped("deliver", True),
                "cancel": identity_wrapped("cancel"),
                "ack": identity_wrapped("ack"),
                "cancel_pending_check": identity_wrapped_check("cancel_pending_check"),
                "attach_runtime": self._attach_screen_ime}

    async def shutdown(self, *, stop_daemons: bool = False) -> None:
        self._running = False
        if self.pre_enable_maintenance:
            # 本路径未连接机械臂、未启动工作器；即使调用方要求也不停止 daemon。
            try:
                for camera in self.devices._cameras.values():
                    try:
                        await camera.close()
                    except Exception:
                        pass
            finally:
                self.input_control.release_resource_locks()
            return
        # FIX-02 §4.8.6-7 关机序：先停接纳/交付（_running=False 即停止派发），
        # 停取消工作器并关闭挂载 IME 仓储（幂等；API 装配可能已先关），再释放资源锁。
        service = self.screen_ime
        if service is not None:
            self.screen_ime = None
            try:
                service.close()
            except Exception:
                pass
        self.input_control.set_cancellation_notifier(None)
        # R3-17：关闭配对服务（幂等）
        pairing = getattr(self, "ime_pairing", None)
        if pairing is not None:
            self.ime_pairing = None
            try:
                pairing.close()
            except Exception:
                pass
        # R2-25：关闭只释放本实例的布局服务与各 Store（不借进程级单例）
        bundle = getattr(self, "_layout_bundle_cache", None)
        if bundle is not None:
            self._layout_bundle_cache = None
            try:
                bundle["adaptation_svc"].close()
            except Exception:
                pass
            for key in ("state_store", "template_store", "region_store"):
                try:
                    bundle[key].close()
                except Exception:
                    pass
        # §4.8.1：资源排他锁随进程生命周期——关闭即释放（重启场景可再取得）
        self.input_control.release_resource_locks()
        if stop_daemons:
            await self.daemon_supervisor.stop_all()
        else:
            # 8788 重启/重载不应让 8795 丢失串口会话；新 runtime
            # 启动时会通过 daemon supervisor 的只读 STATUS 接管它。
            await self.daemon_supervisor.detach_all()
        await self.supervision.shutdown()
        if self._callback_flush_task:
            self._callback_flush_task.cancel()
            try:
                await self._callback_flush_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._exec_tasks.values()):
            task.cancel()
        for task_id in list(self._exec_tasks):
            try:
                await self._exec_tasks[task_id]
            except (asyncio.CancelledError, Exception):
                pass
        self._exec_tasks.clear()
        await self.devices.stop_background()
        await self.views.stop_background()

    def _reload_activated_plugins(self) -> None:
        """进程重启后重新加载已启用插件（插件独立于平台生命周期）。"""
        for record in self.plugins.all():
            if record["status"] == "activated" and record.get("install_dir") != "(in-memory)":
                try:
                    self.plugins.activate(record["plugin_id"])
                except ApiError:
                    pass

    # ================= 重启恢复 =================
    def recover_tasks(self) -> list[dict]:
        """按任务记录、步骤游标和证据恢复，不能因重启重复点击或发送。

        - 未开始任务重新排队；
        - 非动作步骤中断：从当前步骤继续；
        - 动作步骤中断：结果未知，设备暂停进入人工路径；
        - 步骤全部完成但未收尾：按步骤结果收尾。
        """
        if self.pre_enable_maintenance:
            return []
        recovered: list[dict] = []
        for task in self.store.all_tasks():
            if task["status"] not in ("queued", "validating", "awaiting_device", "executing"):
                continue
            step_log = task.get("step_log", [])
            if screen_wake.is_screen_wake_task(task) and (
                    step_log or task.get("wake_driver_started") or task.get("action_call_count")):
                # Even a completed controller step may have been interrupted while
                # releasing its lease. Preserve the evidence, never reenter motion.
                message = "人工唤醒在安全收尾前中断，保留原步骤证据；禁止重放，需人工核对"
                task.update(status="unknown", status_display=display(TASK_STATUS, "unknown"),
                            uncertainty_reason=message, result_message=message,
                            finished_at=iso(utc_now()), home_verified=False)
                self.devices.hold_unknown(task["device_id"], task["task_id"], message)
                self.store.save_task(task)
                device = self.devices.get(task["device_id"])
                self.devices._alert(task["device_id"], "critical", message,
                                    task_id=task["task_id"], action="auto_pause_device")
                recovered.append(task)
                continue
            stop = task.get("stop_result") or {}
            stop_unconfirmed = (task.get("stop_result_unknown")
                                or (task.get("stop_attempt_count", 0) > 0
                                    and (stop.get("ok") is not True
                                         or stop.get("stopped") is not True
                                         or stop.get("outcome_hint") not in (None, "success"))))
            action_receipt = task.get("action_receipt") or {}
            recovery = task.get("recovery_result") or {}
            recovery_motion_unknown = any(recovery.get(key) == "unknown" for key in (
                "outcome", "outcome_hint", "controller_status", "mechanical_status"))
            finalization_interrupted = (
                task.get("finalization_phase") in {"recover", "cleanup", "home_check"}
                or (task.get("recovery_attempted") and not task.get("recovery_result")))
            if (stop_unconfirmed or recovery_motion_unknown or task.get("motion_result_unknown")
                    or action_receipt.get("state") in {"pending", "unknown"}
                    or finalization_interrupted or any(
                    entry.get("phase") == "done" and entry.get("outcome") == "unknown"
                    for entry in step_log)):
                message = "重启恢复：动作、停止或安全收尾结果未知，禁止继续收尾或重放，需人工核对"
                task.update(status="unknown", status_display=display(TASK_STATUS, "unknown"),
                            uncertainty_reason=message, result_message=message,
                            finished_at=iso(utc_now()), home_verified=False)
                self.devices.hold_unknown(task["device_id"], task["task_id"], message)
                self.store.save_task(task)
                self.devices._alert(task["device_id"], "critical", message,
                                    task_id=task["task_id"], action="auto_pause_device")
                recovered.append(task)
                continue
            in_flight = None
            done_nos = set()
            for entry in step_log:
                if entry.get("phase") == "done":
                    done_nos.add(entry["step_no"])
                elif entry.get("phase") == "started":
                    in_flight = entry
            if in_flight and in_flight["step_no"] not in done_nos:
                if in_flight.get("is_action"):
                    # 动作已开始但结果不明：结果未知 + 人工路径
                    task["status"] = "unknown"
                    task["status_display"] = display(TASK_STATUS, "unknown")
                    task["uncertainty_reason"] = f"进程重启时步骤「{in_flight['action_display']}」正在执行，动作结果无法确认"
                    task["finished_at"] = iso(utc_now())
                    self.devices.hold_unknown(task["device_id"], task["task_id"],
                                              "重启恢复：动作结果未知，自动暂停待人工确认")
                    self.store.save_task(task)
                    try:
                        self.devices._alert(task["device_id"], "critical", task["uncertainty_reason"], task_id=task["task_id"], action="auto_pause_device")
                    except ApiError:
                        pass
                    recovered.append(task)
                    continue
                # 非动作步骤（纯观测）：无物理影响，从当前步骤继续
                task["status"] = "queued"
                task["status_display"] = display(TASK_STATUS, "queued")
                task["restart_note"] = f"重启恢复：从步骤 {in_flight['step_no']} 继续（非动作步骤）"
                self.store.save_task(task)
                recovered.append(task)
                continue
            if len(done_nos) >= len(task.get("plan", [])) and task.get("plan"):
                # 已完成且明确的步骤可继续收尾，未知结果已在上方锁定。
                task["status"] = "queued"  # 交给执行器从游标末尾收尾
                task["step_cursor"] = len(task["plan"])
                task["restart_note"] = "重启恢复：步骤已完成，补收尾与回报"
                self.store.save_task(task)
                self._exec_tasks[task["task_id"]] = asyncio.ensure_future(self.executor.execute(task["task_id"]))
                recovered.append(task)
                continue
            # 未开始：重新排队
            task["status"] = "queued"
            task["status_display"] = display(TASK_STATUS, "queued")
            task["restart_note"] = "重启恢复：任务未开始，重新排队"
            self.store.save_task(task)
            recovered.append(task)
        return recovered

    # ================= 任务提交 =================
    def submit_task(self, payload: dict, *, source: str, context: dict) -> dict:
        """提交任务：幂等、过期校验、契约/插件 schema 校验、排队。

        context: {operator_id, role, reason, correlation_id, client_request_id}
        """
        if self.pre_enable_maintenance:
            raise ApiError("pre_enable_maintenance")
        task_type = payload.get("task_type")
        registry = self.plugins.task_type_registry()
        if task_type not in registry:
            raise ApiError("plugin_task_type_unknown", f"任务类型 {task_type} 未由任何已启用插件注册")

        idempotency_key = payload.get("idempotency_key")
        if not idempotency_key:
            raise ApiError("missing_field", "缺少 idempotency_key（幂等键）")
        payload_hash = sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        existing = self.store.find_task_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.get("payload_hash") != payload_hash:
                raise ApiError("idempotency_conflict", f"幂等键 {idempotency_key} 已绑定不同内容")
            self.audit.record(
                "task_duplicate_blocked",
                operator_id=context.get("operator_id", source),
                task_id=existing["task_id"],
                reason="重复幂等键提交，拦截重复执行",
                correlation_id=payload.get("correlation_id"),
                client_request_id=context.get("client_request_id"),
            )
            return existing

        # 契约校验
        entry = registry[task_type]
        if screen_wake.claims_screen_wake(payload):
            screen_wake.validate_request(payload, entry)
        if entry.get("request_schema") is not None:
            try:
                jsonschema.validate(payload, entry["request_schema"],
                                    format_checker=TASK_REQUEST_FORMAT_CHECKER)
            except jsonschema.ValidationError as exc:
                raise ApiError("invalid_request", f"任务请求外壳校验失败：{exc.message}") from exc
        contract_schema_name = TASK_CONTRACT_SCHEMAS.get(task_type)
        if contract_schema_name is not None:
            schema_path = CONTRACT_DIR / contract_schema_name
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            try:
                jsonschema.validate(payload, schema)
            except jsonschema.ValidationError as exc:
                display_name = contract_schema_name.removesuffix(".schema.json")
                raise ApiError("invalid_request",
                               f"{display_name} 契约校验失败：{exc.message}") from exc
        else:
            schema = entry.get("input_schema") or {"type": "object"}
            try:
                jsonschema.validate(payload.get("payload", {}), schema)
            except jsonschema.ValidationError as exc:
                raise ApiError("invalid_request", f"任务参数校验失败：{exc.message}") from exc

        device_id = payload.get("device_id")
        device = self.devices.get(device_id)  # 不存在则抛 device_not_found
        context_blockers = self.plugin_context_blockers(entry["plugin_id"], device_id)
        if context_blockers:
            raise ApiError("device_not_eligible", "；".join(context_blockers))
        input_entry = next(
            item for item in self.plugins.get(entry["plugin_id"])["manifest"]["task_types"]
            if item["task_type"] == task_type)
        requires_input_gate = input_entry.get("requires_input", False) or task_type in INPUT_TASK_TYPES
        if requires_input_gate and not self.input_switch_allowed(device_id):
            # A-开关（2026-10-03 规格 §4.6）：E 派生标记退役，改查操作者开关
            raise ApiError("input_switch_disabled",
                           f"设备 {device_id} 输入与发送未允许，禁止输入或发送")
        if requires_input_gate and device.get("enforce_input_gate", False):
            payload_body = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
            eligibility = self.devices.eligibility(
                device_id, require_input=True,
                ime_device_id=payload_body.get("ime_device_id") if isinstance(payload_body, dict) else None,
                require_keyboard_active=input_entry.get("requires_keyboard_active", True),
                require_ime_ready=input_entry.get("focuses_input_before_typing") is not True,
            )
            if not eligibility["can_accept_task"]:
                raise ApiError("device_not_eligible", f"设备 {device_id} 不具备输入任务资格：{'、'.join(eligibility['blocking_checks'])}")

        task = {
            "task_id": payload.get("task_id") or new_id("task"),
            "task_type": task_type,
            "task_type_display": entry["name_display"],
            "capability": entry["capability"],
            "plugin_id": entry["plugin_id"],
            "device_id": device_id,
            "idempotency_key": idempotency_key,
            "payload": payload.get("payload", payload),
            "payload_hash": payload_hash,
            "expires_at": payload.get("expires_at"),
            "contract_version": payload.get(
                "contract_version", TASK_DEFAULT_CONTRACT_VERSIONS.get(task_type, "v1")),
            "correlation_id": payload.get("correlation_id", ""),
            "policy_context": payload.get("policy_context", {}),
            "source": source,
            "submitted_by": context.get("operator_id", source),
            "status": "queued",
            "status_display": display(TASK_STATUS, "queued"),
            "step_cursor": 0,
            "step_log": [],
            "timeline": [],
            "evidence_ids": [],
            "cancel_requested": False,
            "attempts": 0,
            "capability_call_count": 0,
            "action_call_count": 0,
            "read_only_task": entry.get("read_only_task") is True,
            "created_at": iso(utc_now()),
            "started_at": None,
            "finished_at": None,
        }
        for field in entry.get("context_fields", []):
            if field in task:
                raise ApiError("plugin_manifest_invalid", "任务附加上下文不能覆盖通用字段")
            task[field] = deepcopy(payload.get(field))
        if screen_wake.claims_screen_wake(task):
            task["expires_at"] = screen_wake.cap_expiry(payload, task["created_at"])
        expired_wake = (screen_wake.is_screen_wake_task(task)
                        and parse_iso(task["expires_at"]) <= utc_now())
        if not expired_wake:
            self.validate_task_preconditions(task)
        # 过期任务：登记为已过期，永不取得租约
        expires = parse_iso(task["expires_at"])
        if expires and expires <= utc_now():
            task["status"] = "expired"
            task["status_display"] = display(TASK_STATUS, "expired")
            task["result_message"] = "任务提交即已过期，不取得租约，不执行"
            if screen_wake.is_screen_wake_task(task):
                screen_wake.record_unstarted_result(task, "failed", task["result_message"])
        if task_type == "chat_send":
            # 契约顶层字段（账号/会话）随任务保存，供插件校验，不得被改写
            task["wechat_account_id"] = payload.get("wechat_account_id", "")
            task["external_conversation_id"] = payload.get("external_conversation_id", "")
        if requires_input_gate:
            # §4.8.4-1：新输入任务在单事务内创建任务+owner（重放只取旧 owner，
            # 不给它换当前许可版本）；纯导航任务不创建 owner。
            from runtime import input_settings as _iset
            _settings = _iset.load_settings(
                device_id, self.devices.settings_data_root(device_id))
            _binding_ime = self.devices.input_binding(device_id).get("ime_device_id")
            owner = self.input_control.build_owner(
                owner_type="fleet_task", owner_ref=task["task_id"],
                device_id=device_id,
                ime_device_id=str(_settings.get("ime_device_id")
                                  or _binding_ime or "unbound"),
                settings_revision=max(1, int(_settings.get("revision") or 0)),
                binding_revision=int(self.devices.binding_view(device_id)
                                     .get("binding_revision") or 0),
                payload=task.get("payload") if isinstance(task.get("payload"), dict)
                else payload)
            _, task, created = self.input_control.admit_owner_with_task(owner, task)
        else:
            task, created = self.store.create_task(task)
        if not created:
            self.audit.record(
                "task_duplicate_blocked", operator_id=context.get("operator_id", source),
                task_id=task["task_id"], device_id=task["device_id"],
                reason="并发重复提交，返回原任务，不重复执行",
                correlation_id=payload.get("correlation_id"),
                client_request_id=context.get("client_request_id"),
            )
            return task
        self.audit.record(
            "task_submit",
            operator_id=context.get("operator_id", source),
            operator_role=context.get("role", source),
            task_id=task["task_id"],
            device_id=device_id,
            reason=context.get("reason", f"提交任务：{entry['name_display']}"),
            correlation_id=task["correlation_id"],
            client_request_id=context.get("client_request_id"),
            detail={"task_type": task_type, "status": task["status"]},
        )
        self.events.publish("task.updated", {"task_id": task["task_id"], "status": task["status"], "status_display": task["status_display"]})
        return task

    def device_eligibility(self, device_id: str, *, require_input: bool = False) -> dict:
        return self.devices.eligibility(device_id, require_input=require_input)

    async def register_device(self, config: dict) -> dict:
        return await self.devices.register_and_selfcheck(config)

    async def attach_device(self, config: dict) -> dict:
        return await self.devices.attach_device(config)

    async def remove_device(self, device_id: str, reason: str = "设备拔出") -> dict:
        self.supervision.stop(device_id, reason)
        await self.daemon_supervisor.stop_device(device_id, reason)
        return await self.devices.remove_device(device_id, reason)

    async def _handle_daemon_event(self, event: dict) -> None:
        """把 daemon 进程异常传播为设备资格失效，不伪造可用状态。"""
        kind = str(event.get("kind") or "")
        device_id = str(event.get("device_id") or "")
        if device_id and kind in {"daemon_exited", "daemon_start_failed", "daemon_restart_exhausted"}:
            marker = getattr(self.devices, "mark_daemon_unavailable", None)
            if marker is not None:
                marker(device_id, event.get("last_error") or "daemon 进程不可用")
        self.events.publish("daemon.updated", {
            key: value for key, value in event.items()
            if key in {"kind", "device_id", "status", "status_display", "pid",
                       "daemon_addr", "serial_port", "restart_count", "last_error",
                       "started_at", "last_exit_at", "exit_code"}
        })

    def daemon_status(self) -> list[dict]:
        return self.daemon_supervisor.states

    # ================= 调度 =================
    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.05)

    async def _callback_flush_loop(self) -> None:
        """§10 回调重试：每秒检查到期回调并按退避重投。"""
        while self._running and not self.pre_enable_maintenance:
            try:
                base = task_callback_base_url(self.config)
                await self.callback_outbox.flush(base)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def _dispatch_once(self) -> None:
        if self.pre_enable_maintenance:
            return
        # S1（第六轮）：协程早期异常且任务暂不可读的收尾重试——存储恢复后
        # 自动补齐无动作收尾，不让 awaiting_device/busy 永久悬挂（L46）。
        pending = getattr(self.executor, "_pending_early_finalize", None)
        if pending:
            for pend_id in list(pending):
                try:
                    await self.executor.finalize_early_error(
                        pend_id, RuntimeError("存储恢复后重试早期收尾"), _retry=True)
                except Exception:
                    pass
        # S1（第六轮）：done 协程句柄与重试审计缓存有界回收（不影响活任务）
        try:
            done_ids = [tid for tid, t in self._exec_tasks.items()
                        if t.done()]
            for tid in done_ids[len(self._exec_tasks) - 200:]:
                self._exec_tasks.pop(tid, None)
            retry_cache = getattr(self, "_dispatch_retry_audit_at", None)
            if isinstance(retry_cache, dict) and len(retry_cache) > 200:
                for k in list(retry_cache)[:len(retry_cache) - 200]:
                    retry_cache.pop(k, None)
        except Exception:
            pass
        try:
            all_tasks = self.store.all_tasks()
        except Exception:
            return
        # S1（第六轮）：awaiting_device 孤儿对账——执行协程不存在且设备无
        # 活动租约的 awaiting_device 任务（保存失败/协程早亡后存储恢复），
        # 按动作事实分账终结，不悬挂（与 _fail_dispatch 同一判据）。
        for task in all_tasks:
            if task.get("status") != "awaiting_device":
                continue
            tid = task.get("task_id")
            exec_task = self._exec_tasks.get(tid)
            if exec_task is not None and not exec_task.done():
                continue
            try:
                if self.leases.active(task["device_id"]) is not None:
                    continue
            except Exception:
                continue
            acted = any(e.get("is_action") for e in task.get("step_log") or [])
            receipt = task.get("action_receipt") or {}
            in_flight = bool(task.get("stop_result_unknown")
                             or task.get("motion_result_unknown")
                             or receipt.get("state") in ("pending", "unknown")
                             or task.get("recovery_attempted"))
            task["status"] = "unknown" if (acted or in_flight) else "failed"
            task["status_display"] = display(TASK_STATUS, task["status"])
            task["error_category"] = "dispatch_error"
            task["result_message"] = (
                "派发收尾未完成的对账：此前已有动作或未知回执，结果待人工核对"
                if (acted or in_flight) else "派发收尾未完成的对账：任务未执行")
            task["finished_at"] = iso(utc_now())
            task.setdefault("timeline", []).append({
                "at": task["finished_at"], "status": task["status"],
                "note": task["result_message"],
                "detail": {"error_category": "dispatch_error",
                           "reconcile": "orphan_awaiting_device"}})
            try:
                self.store.save_task(task)
                self.events.publish("task.updated", {
                    "task_id": tid, "device_id": task["device_id"],
                    "status": task["status"],
                    "status_display": task["status_display"]})
                self.audit.record("task_dispatch_failed", operator_id="runtime",
                                  task_id=tid, device_id=task["device_id"],
                                  reason=task["result_message"])
                if acted or in_flight:
                    self.devices.hold_unknown(
                        task["device_id"], tid,
                        "派发收尾未完成但任务已有动作或未知回执，暂停待人工核对")
                elif (self.devices.get(task["device_id"]).get("status") == "busy"
                      and self.leases.active(task["device_id"]) is None):
                    self.devices._set_status(
                        task["device_id"], "ready", "派发收尾对账完成，设备空闲")
                if task.get("source") == "wx":
                    await self.reporter.send_task_result(task)
            except Exception:
                pass
        queued = [t for t in all_tasks if t["status"] == "queued"]
        queued.sort(key=lambda t: t["created_at"])
        for task in queued:
            task_id = task["task_id"]
            if task_id in self._exec_tasks and not self._exec_tasks[task_id].done():
                continue
            device_id = task["device_id"]
            expires = parse_iso(task.get("expires_at"))
            if expires and expires <= utc_now():
                moved = (any(entry.get("is_action") for entry in task.get("step_log", []))
                         or (task.get("action_receipt") or {}).get("state") in ("pending", "unknown")
                         or bool(task.get("stop_result_unknown")
                                 or task.get("motion_result_unknown")
                                 or task.get("recovery_attempted")))
                # P2-1（第五轮复审）：暗屏代提重试群体过期前失败化——queued
                # 过期本不入 reporter 回报名单，业务 将零回调；失败化让 业务 收到
                # 明确 failed（其余任务维持 expired 原语义）。
                fail_expired = (task.get("source") == "wx" and not moved
                                and task.get("wake_retried"))
                task["status"] = "unknown" if moved else ("failed" if fail_expired else "expired")
                task["status_display"] = display(TASK_STATUS, task["status"])
                task["result_message"] = (
                    "已发生动作的恢复任务等待超时，结果待人工核对，禁止自动补做"
                    if moved else "等待期间任务已过期，未执行；设备恢复后不会补做"
                )
                if fail_expired:
                    task["error_category"] = "task_expired"
                if moved:
                    task["uncertainty_reason"] = task["result_message"]
                    self.devices.hold_unknown(device_id, task_id, task["result_message"])
                task["finished_at"] = iso(utc_now())
                if screen_wake.is_screen_wake_task(task) and not moved:
                    screen_wake.record_unstarted_result(task, "failed", task["result_message"])
                task.setdefault("timeline", []).append({
                    "at": task["finished_at"], "status": task["status"],
                    "status_display": task["status_display"], "note": task["result_message"],
                })
                self.store.save_task(task)
                self.events.publish("task.updated", {
                    "task_id": task_id, "device_id": device_id,
                    "status": task["status"], "status_display": task["status_display"],
                })
                if task.get("source") == "wx" and task["status"] in ("failed", "unknown"):
                    try:
                        await self.reporter.send_task_result(task)
                    except Exception:
                        pass
                continue
            if self.estop.active:
                continue
            if screen_wake.is_screen_wake_task(task):
                taskable = screen_wake.wake_readiness(self, device_id, task=task)["usable"]
            else:
                taskable, _reason = self.devices.is_taskable(device_id)
            if not taskable:
                continue
            if self.leases.active(device_id) is not None:
                continue
            # P1（第五轮§6.1/6.4）：逐阶段收尾——租约写入、awaiting_device 持久化、
            # busy 更新、创建执行协程各自失败时走正式收尾调用者，不再裸吞异常。
            lease = None
            stage = "lease"
            prev = None
            try:
                try:
                    lease = self.leases.acquire(device_id, task_id)
                except ApiError:
                    continue  # lease_conflict 等已分类忙冲突：合法等待
                stage = "persist_awaiting"
                prev = {"status": task.get("status"),
                        "status_display": task.get("status_display"),
                        "lease_id": task.get("lease_id"),
                        "timeline_len": len(task.get("timeline") or [])}
                task["lease_id"] = lease["lease_id"]
                task["status"] = "awaiting_device"
                task["status_display"] = display(TASK_STATUS, "awaiting_device")
                task.setdefault("timeline", []).append(
                    {"at": iso(utc_now()), "status": "awaiting_device", "status_display": task["status_display"],
                     "note": "已获得设备租约，等待执行", "detail": {"lease_id": lease["lease_id"]}}
                )
                self.store.save_task(task)
                stage = "busy"
                self.devices._set_status(device_id, "busy", f"执行任务 {task_id}")
                stage = "create_coroutine"
                # 执行协程本体异常也有守卫收尾（executor.execute_guarded），
                # 取任务/插件等协程早期异常不再靠 asyncio 默认吞掉。
                self._exec_tasks[task_id] = asyncio.create_task(
                    self.executor.execute_guarded(task_id))
            except Exception as exc:
                await self._fail_dispatch(task, device_id, task_id,
                                          lease, stage, prev, exc)

    async def _fail_dispatch(self, task: dict, device_id: str, task_id: str,
                             lease, stage: str, prev, exc: Exception) -> None:
        """派发段失败的正式收尾调用者（第五轮§6.3/6.4/6.5）。

        - lease/persist_awaiting 阶段：任务尚未进入持久中间态——回滚内存改动、
          释放已写租约，任务保持 queued 由下一轮重试；存储不可用时不伪造终态。
        - busy/create_coroutine 阶段：执行协程未启动（零新动作）——无既往动作
          记明确 failed(dispatch_error)；有既往动作/在途未知按 unknown+设备阻断，
          禁止把有动作的任务写成"未执行"。
        - 释放未证实不写"已释放"；设备仅在确实被本任务置 busy 时恢复 ready。
        """
        def _audit(action, reason):
            try:
                self.audit.record(action, operator_id="runtime",
                                  task_id=task_id, device_id=device_id,
                                  reason=reason)
            except Exception:
                pass

        # 排队重试类失败（lease/persist 阶段）每任务 30 秒只留痕一次，防刷屏
        retry_stage = stage in ("lease", "persist_awaiting")
        throttled = False
        if retry_stage:
            now = utc_now()
            seen = getattr(self, "_dispatch_retry_audit_at", None)
            if seen is None:
                seen = {}
                self._dispatch_retry_audit_at = seen
            last = seen.get(task_id)
            if last is not None and (now - last).total_seconds() < 30:
                throttled = True
            else:
                seen[task_id] = now

        # R6-F3（最终收尾）：消费 release() 的实值返回——过期竞态下（他任务
        # 已抢先回收/重取）release 返回 False，文案不得误写"租约已释放"。
        released = None
        if lease is not None:
            try:
                released = bool(self.leases.release(device_id, lease["lease_id"]))
            except Exception:
                released = False

        if retry_stage:
            # 未持久化任何中间态：回滚内存改动，任务保持 queued 重试
            if prev is not None:
                task["status"] = prev["status"]
                task["status_display"] = prev["status_display"]
                if prev["lease_id"] is None:
                    task.pop("lease_id", None)
                else:
                    task["lease_id"] = prev["lease_id"]
                tl = task.get("timeline") or []
                while len(tl) > (prev.get("timeline_len") or 0):
                    tl.pop()
            if not throttled:
                _audit("task_dispatch_storage_error",
                       f"派发在 {stage} 阶段失败（{exc}）；任务保持排队重试，"
                       f"租约{'已释放' if released else '释放失败待回收'}")
            return

        # S1（第六轮）：动作事实=step_log 动作+停止/回执未知+action_receipt
        # pending/unknown+已尝试恢复——空 step_log 不等于"未执行"（L48）
        acted = any(entry.get("is_action") for entry in task.get("step_log", []))
        receipt = task.get("action_receipt") or {}
        in_flight = bool(task.get("stop_result_unknown")
                         or task.get("motion_result_unknown")
                         or receipt.get("state") in ("pending", "unknown")
                         or task.get("recovery_attempted"))
        unknown = acted or in_flight
        release_note = ("租约已释放" if released is True
                        else "租约释放失败，待对账回收" if released is False else "")
        task["status"] = "unknown" if unknown else "failed"
        task["status_display"] = display(TASK_STATUS, task["status"])
        task["error_category"] = "dispatch_error"
        task["result_message"] = (
            f"派发失败（{'任务未执行' if not unknown else '此前已有动作或未知回执，结果待人工核对'}，"
            f"{release_note}）：{exc}" if release_note else
            f"派发失败（{'任务未执行' if not unknown else '此前已有动作或未知回执，结果待人工核对'}）：{exc}")
        task["finished_at"] = iso(utc_now())
        task.setdefault("timeline", []).append({
            "at": task["finished_at"], "status": task["status"],
            "note": task["result_message"],
            "detail": {"error_category": "dispatch_error", "stage": stage}
        })
        # S1（第六轮）：保存结果约束回报/ready/事件——未持久不得发布假终态
        saved = True
        try:
            self.store.save_task(task)
        except Exception:
            saved = False
            _audit("task_dispatch_storage_error",
                   f"派发失败终态无法持久化（{exc}），内存态：{task['result_message']}；"
                   "回报与设备放行暂缓，待存储恢复对账")
        if saved:
            _audit("task_dispatch_failed", task["result_message"])
            try:
                self.events.publish("task.updated", {
                    "task_id": task_id, "device_id": device_id,
                    "status": task["status"],
                    "status_display": task["status_display"],
                })
            except Exception:
                pass
        if unknown:
            try:
                self.devices.hold_unknown(device_id, task_id,
                                          "派发失败但任务已有动作或未知回执，暂停待人工核对")
            except Exception:
                pass
        else:
            try:
                # 释放证实失败时不得置 ready：busy+幽灵租约可经过期回收/人工对账，
                # 不能靠谎报空闲掩盖；且仅当设备租约确实不归属他任务时
                if (saved and released is not False
                        and self.devices.get(device_id).get("status") == "busy"):
                    lease_now = self.leases.get(device_id)
                    if not (lease_now and lease_now.get("task_id") != task_id):
                        self.devices._set_status(device_id, "ready", "派发失败，设备空闲")
            except Exception:
                pass
        if task.get("source") == "wx" and saved:
            try:
                await self.reporter.send_task_result(task)
            except Exception:
                pass

    # 插件能力调用段已外迁 runtime/plugin_capabilities.py（Auto_CS#7；ArmFleetRuntime 经 Mixin 继承）

    # ================= 插件私有状态 =================
    def plugin_context_blockers(self, plugin_id: str, device_id: str) -> list[str]:
        manifest = self.plugins.get(plugin_id)["manifest"]
        return [
            "其他插件仍保留屏幕执行上下文，请先完成退出或人工核对"
            for guard in manifest.get("screen_context_guards", [])
            if self.plugins.state_get(guard["plugin_id"], device_id, guard["state_key"])
        ]

    def plugin_state_get(self, plugin_id: str, device_id: str, key: str):
        return self.plugins.state_get(plugin_id, device_id, key)

    def plugin_state_set(self, plugin_id: str, device_id: str, key: str, value) -> None:
        self.plugins.state_set(plugin_id, device_id, key, value)

    # ================= 任务查询 / 取消 =================
    def get_task(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        if not task:
            raise ApiError("task_not_found", task_id)
        return task

    def list_tasks(self, *, device_id: str | None = None, status: str | None = None, task_type: str | None = None) -> list[dict]:
        tasks = self.store.all_tasks()
        if device_id:
            tasks = [t for t in tasks if t["device_id"] == device_id]
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        if task_type:
            tasks = [t for t in tasks if t["task_type"] == task_type]
        return sorted(tasks, key=lambda t: t["created_at"], reverse=True)

    async def cancel_task(self, task_id: str, context: dict) -> dict:
        from runtime.permissions import check_operation

        check_operation("task_cancel", context)
        task = self.get_task(task_id)
        if task["status"] in ("confirmed", "failed", "unknown", "cancelled", "expired"):
            raise ApiError("task_not_cancellable", f"任务当前状态：{task['status_display']}")
        if task["status"] == "executing" or (
                screen_wake.is_screen_wake_task(task)
                and task["status"] in {"awaiting_device", "validating"}):
            task["cancel_requested"] = True
            task["cancel_reason"] = context["reason"]
            self.store.save_task(task)
            self.audit.record(
                "task_cancel", operator_id=context["operator_id"], operator_role=context.get("role"),
                task_id=task_id, device_id=task["device_id"], reason=context["reason"],
                correlation_id=context.get("correlation_id"), client_request_id=context.get("client_request_id"),
                detail={"mode": "执行中，将停止、清理并取证"},
            )
            return task
        # 排队/等待中：直接取消，不取得租约
        task["status"] = "cancelled"
        task["status_display"] = display(TASK_STATUS, "cancelled")
        task["cancel_reason"] = context["reason"]
        task["finished_at"] = iso(utc_now())
        if screen_wake.is_screen_wake_task(task) and not task.get("wake_driver_started"):
            screen_wake.record_unstarted_result(task, "failed", "请求在动作开始前取消")
        task.setdefault("timeline", []).append({"at": task["finished_at"], "status": "cancelled", "status_display": task["status_display"], "note": "任务在执行前被取消", "detail": {}})
        self.store.save_task(task)
        self.audit.record(
            "task_cancel", operator_id=context["operator_id"], operator_role=context.get("role"),
            task_id=task_id, device_id=task["device_id"], reason=context["reason"],
            correlation_id=context.get("correlation_id"), client_request_id=context.get("client_request_id"),
            detail={"mode": "执行前取消"},
        )
        self.events.publish("task.updated", {"task_id": task_id, "status": task["status"], "status_display": task["status_display"]})
        return task

    # ================= 运维操作 =================
    def _op(self, operation: str, device_id: str, context: dict):
        from runtime.permissions import check_operation

        check_operation(operation, context)
        client_request_id = context["client_request_id"]
        dedup = self.store.get_op_result(client_request_id)
        if dedup is not None:
            return dedup["result"], True

        def _record(result: dict):
            self.store.save_op_result(client_request_id, {"result": result})
            return result

        return _record, False

    def op_pause(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_pause", device_id, context)
        if dedup:
            return record
        device = self.devices.get(device_id)
        self.supervision.stop(device_id, "设备暂停，自动停止微信托管")
        result = self.devices.pause(device_id, context["reason"])
        self.audit.record("device_pause", operator_id=context["operator_id"], operator_role=context.get("role"),
                          device_id=device_id, reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return record(result)

    def op_resume(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_resume", device_id, context)
        if dedup:
            return record
        result = self.devices.resume(device_id, context["reason"])
        self.audit.record("device_resume", operator_id=context["operator_id"], operator_role=context.get("role"),
                          device_id=device_id, reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return record(result)

    def op_quarantine(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_quarantine", device_id, context)
        if dedup:
            return record
        self.supervision.stop(device_id, "设备隔离，自动停止微信托管")
        result = self.devices.quarantine(device_id, context["reason"])
        self.audit.record("device_quarantine", operator_id=context["operator_id"], operator_role=context.get("role"),
                          device_id=device_id, reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return record(result)

    def op_unquarantine(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_unquarantine", device_id, context)
        if dedup:
            return record
        result = self.devices.unquarantine(device_id, context["reason"])
        self.audit.record("device_unquarantine", operator_id=context["operator_id"], operator_role=context.get("role"),
                          device_id=device_id, reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return record(result)

    async def op_home_check(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_home_check", device_id, context)
        if dedup:
            return record
        result = await self.devices.home_check(device_id)
        # 恢复中设备：归位检查通过后回到可用
        device = self.devices.get(device_id)
        if device["status"] == "recovering" and result.get("home_ok"):
            held = device.get("unknown_hold_task_id")
            self.devices._set_status(device_id, "paused" if held else "ready",
                                     "结果未知待复核" if held else "恢复检查通过，归位正常")
        return record(result)

    def op_reset_home(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_reset_home", device_id, context)
        if dedup:
            return record
        result = self.devices.mark_home_ok(device_id, context["operator_id"], context["reason"])
        return record({"device_id": device_id, "home_ok": True, "checked_at": result.get("home_checked_at")})

    # ================= 设备菜单：绑定事实与绑定变更（C 组） =================
    def _update_binding_table(self, device_id: str, changes: dict,
                              *, create_if_missing: bool = True,
                              create_seed: dict | None = None) -> dict:
        """按设备写回绑定表（锁内读改写）；返回写后条目。

        2026-10-29：原实现拿调用方整表覆盖磁盘，会回滚别处刚写的值（丢更新）。
        """
        from runtime.config import update_device_config

        return update_device_config(self.config.root, device_id, dict(changes),
                                    create_if_missing=create_if_missing,
                                    create_seed=create_seed)

    def device_binding_view(self, device_id: str) -> dict:
        view = self.devices.binding_view(device_id)
        view["eligibility"] = self.device_eligibility(device_id, require_input=True)
        view["recent_audit"] = [
            {"occurred_at": e.get("occurred_at"), "action": e.get("action"),
             "action_display": e.get("action_display"), "operator_id": e.get("operator_id"),
             "reason": e.get("reason")}
            for e in self.audit.list_events(limit=10, device_id=device_id)
        ]
        return view

    async def op_device_register(self, cfg: dict, context: dict) -> dict:
        """菜单新增设备：权限/幂等/审计与设备状态机一致。"""
        record, dedup = self._op("device_register", str(cfg.get("device_id") or ""), context)
        if dedup:
            return record
        cfg = dict(cfg)
        cfg.setdefault("hotplug", True)
        result = await self.register_device(cfg)
        self.audit.record("device_register", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=result["device_id"],
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return record(result)

    async def op_device_attach(self, device_id: str, cfg: dict, context: dict) -> dict:
        """菜单重挂载/重绑定（危险操作，二次确认后执行）。"""
        record, dedup = self._op("device_attach", device_id, context)
        if dedup:
            return record
        cfg = dict(cfg)
        cfg["device_id"] = device_id
        result = await self.attach_device(cfg)
        self.audit.record("device_attach", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=device_id,
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"],
                          detail={"binding_revision": result.get("binding_revision")})
        return record(result)

    async def op_device_remove(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_remove", device_id, context)
        if dedup:
            return record
        result = await self.remove_device(device_id, context["reason"])
        self.audit.record("device_remove", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=device_id,
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return record(result)

    async def op_device_binding_update(self, device_id: str, updates: dict, context: dict) -> dict:
        record, dedup = self._op("device_binding_update", device_id, context)
        if dedup:
            return record
        result = await self.devices.update_binding(device_id, updates,
                                                   operator_id=context["operator_id"],
                                                   reason=context["reason"])
        self.audit.record("device_binding_update", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=device_id,
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"],
                          detail={"binding_revision": result.get("binding_revision")})
        return record(result)

    def op_device_binding_disable(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_binding_disable", device_id, context)
        if dedup:
            return record
        result = self.devices.disable_binding(device_id,
                                              operator_id=context["operator_id"],
                                              reason=context["reason"])
        self.audit.record("device_binding_disable", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=device_id,
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return record(result)

    def op_device_binding_enable(self, device_id: str, context: dict) -> dict:
        record, dedup = self._op("device_binding_enable", device_id, context)
        if dedup:
            return record
        result = self.devices.enable_binding(device_id,
                                             operator_id=context["operator_id"],
                                             reason=context["reason"])
        self.audit.record("device_binding_enable", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=device_id,
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return record(result)

    async def op_status_query(self, device_id: str, context: dict) -> dict:
        """人工状态查询：摄像头确认当前界面（只读，不改变设备状态）。"""
        from runtime.permissions import check_operation

        check_operation("device_status_query", context)
        camera = self.devices.camera(device_id)
        ui = await camera.identify_ui_state()
        quality = await camera.check_quality()
        self.devices.update_ui_state(device_id, ui["ui_state"], ui["confidence"])
        self.audit.record("device_status_query", operator_id=context["operator_id"], operator_role=context.get("role"),
                          device_id=device_id, reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"],
                          detail={"ui_state": ui["ui_state_display"], "quality": quality["quality_display"]})
        return {"device_id": device_id, "ui_state": ui["ui_state"], "ui_state_display": ui["ui_state_display"],
                "confidence": ui["confidence"], "quality": quality["quality"], "quality_display": quality["quality_display"]}

    def op_alert_resolve(self, alert_id: str, context: dict) -> dict:
        from runtime.permissions import check_operation

        check_operation("alert_resolve", context)
        result = self.devices.resolve_alert(alert_id, context["operator_id"], context["reason"])
        self.audit.record("alert_resolve", operator_id=context["operator_id"], operator_role=context.get("role"),
                          device_id=result.get("device_id"), reason=context["reason"],
                          correlation_id=context.get("correlation_id"), client_request_id=context["client_request_id"],
                          detail={"alert_id": alert_id})
        return result

    def op_estop(self, context: dict) -> dict:
        from runtime.permissions import check_operation

        check_operation("emergency_stop", context)
        for device in self.devices.all():
            self.supervision.stop(device["device_id"], "全局急停，自动停止微信托管")
        result = self.estop.trigger(context["operator_id"], context["reason"], context.get("correlation_id"))
        self.audit.record("emergency_stop", operator_id=context["operator_id"], operator_role=context.get("role"),
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return result

    async def op_estop_reset(self, context: dict) -> dict:
        from runtime.permissions import check_operation

        check_operation("emergency_stop_reset", context)
        result = await self.estop.reset(context["operator_id"], context["reason"])
        return result

    # ================= 业务托管 =================
    async def op_supervision_start(self, device_id: str, context: dict) -> dict:
        from runtime.permissions import check_operation

        check_operation("supervision_start", context)
        # wx_compat 关闭时（默认），托管入口先按原语义拒绝，不暴露输入开关状态
        if isinstance(self.supervision, DisabledSupervisionManager):
            return await self.supervision.start(device_id, context)
        device = self.devices.get(device_id)
        if not self.input_switch_allowed(device_id):
            raise ApiError("input_switch_disabled",
                           f"设备 {device_id} 输入与发送未允许，禁止开始托管")
        if device.get("enforce_input_gate", False):
            eligibility = self.devices.eligibility(device_id, require_input=True)
            if not eligibility["can_managed"]:
                raise ApiError("device_not_eligible", f"设备 {device_id} 未满足托管资格：{'、'.join(eligibility['blocking_checks'])}")
        result = await self.supervision.start(device_id, context)
        self.audit.record("supervision_start", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=device_id,
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"],
                          detail={"allowed_user_count": result.get("allowed_user_count", 0)})
        return result

    def op_supervision_stop(self, device_id: str, context: dict) -> dict:
        from runtime.permissions import check_operation

        check_operation("supervision_stop", context)
        result = self.supervision.stop(device_id, context["reason"])
        self.audit.record("supervision_stop", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=device_id,
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"])
        return result

    def op_supervision_configure(self, device_id: str, allowed_users, context: dict) -> dict:
        from runtime.permissions import check_operation

        check_operation("supervision_configure", context)
        result = self.supervision.configure(device_id, allowed_users)
        self.audit.record("supervision_configure", operator_id=context["operator_id"],
                          operator_role=context.get("role"), device_id=device_id,
                          reason=context["reason"], correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"],
                          detail={"allowed_user_count": result.get("allowed_user_count", 0)})
        return result

    # ================= 人工处理队列 =================
    def takeovers(self) -> list[dict]:
        """聚合：结果未知任务、隔离设备、归位失败设备、未处理严重告警。"""
        items: list[dict] = []
        for task in self.store.all_tasks():
            if task["status"] == "unknown" and not task.get("reviewed_at"):
                items.append({
                    "kind": "task_unknown",
                    "kind_display": "结果未知任务",
                    "task_id": task["task_id"],
                    "device_id": task["device_id"],
                    "task_type_display": task.get("task_type_display"),
                    "reason": task.get("uncertainty_reason", "动作结果无法确认"),
                    "created_at": task.get("finished_at") or task.get("created_at"),
                    "evidence_ids": task.get("evidence_ids", []),
                })
        for device in self.devices.all():
            if device["status"] == "quarantined":
                items.append({
                    "kind": "device_quarantined",
                    "kind_display": "设备已隔离",
                    "device_id": device["device_id"],
                    "reason": device.get("quarantine_reason"),
                    "created_at": device.get("observed_at"),
                })
            elif not device.get("home_ok", True):
                items.append({
                    "kind": "home_not_confirmed",
                    "kind_display": "归位未确认",
                    "device_id": device["device_id"],
                    "reason": "归位检查未通过，设备不能领取新任务",
                    "created_at": device.get("home_checked_at"),
                })
        for alert in self.store.all_alerts(include_resolved=False):
            if alert["severity"] == "critical":
                items.append({
                    "kind": "alert_critical",
                    "kind_display": "严重告警",
                    "alert_id": alert["alert_id"],
                    "device_id": alert.get("device_id"),
                    "reason": alert["message"],
                    "created_at": alert["created_at"],
                })
        items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
        return items

    async def request_human_takeover(self, *, device_id: str, conversation_id: str,
                                     customer_id: str, operator_id: str, reason: str,
                                     correlation_id: str, client_request_id: str,
                                     context: dict | None = None) -> dict:
        """把明确确认的接管请求转给 WX；接管事实由 WX 事件库保存。"""
        device = self.devices.get(device_id)
        context = dict(context or {})
        requested_account = str(context.get("wechat_account_id") or "").strip()
        bound_account = str(device.get("wechat_account_id") or "").strip()
        if requested_account and bound_account and requested_account != bound_account:
            self.audit.record(
                "human_takeover_request", operator_id=operator_id,
                device_id=device_id, outcome="rejected",
                reason=f"人工接管账号与设备绑定不一致：请求 {requested_account}，绑定 {bound_account}",
                correlation_id=correlation_id, client_request_id=client_request_id,
                detail={"conversation_id": conversation_id, "customer_id": customer_id},
            )
            raise ApiError(
                "account_mismatch",
                f"人工接管请求的微信账号 {requested_account} 与设备 {device_id} 绑定账号 {bound_account} 不一致",
            )
        return await self.takeover_bridge.request(
            device_id=device_id, conversation_id=conversation_id,
            customer_id=customer_id, operator_id=operator_id, reason=reason,
            correlation_id=correlation_id, client_request_id=client_request_id,
            context=context,
        )

    def mark_task_reviewed(self, task_id: str, context: dict) -> dict:
        """人工核对结果未知任务后：标记已核对（任务保持未知结果，不猜成已发送）。"""
        from runtime.permissions import check_operation

        check_operation("alert_resolve", context)
        task = self.get_task(task_id)
        if task["status"] != "unknown":
            raise ApiError("state_transition_invalid", "只有结果未知任务需要人工核对")
        task["reviewed_by"] = context["operator_id"]
        task["reviewed_reason"] = context["reason"]
        task["reviewed_at"] = iso(utc_now())
        self.store.save_task(task)
        self.audit.record("task_marked_reviewed", operator_id=context["operator_id"], operator_role=context.get("role"),
                          task_id=task_id, device_id=task["device_id"], reason=context["reason"],
                          correlation_id=context.get("correlation_id"), client_request_id=context["client_request_id"])
        return task

    def review_task_context(self, task_id: str, context: dict, *,
                            expected_device_id: str, expected_task_status: str,
                            review_context: dict) -> dict:
        """人工核对后的插件状态释放，不修改执行结论、校准、暂停或租约。"""
        from runtime.permissions import check_operation
        from tasks.plugin_api import ReviewContext

        check_operation("alert_resolve", context)
        if not isinstance(review_context, dict):
            raise ApiError("invalid_request", "人工核对内容必须为对象")
        fingerprint = sha256_text(json.dumps(
            [task_id, expected_device_id, expected_task_status, review_context],
            ensure_ascii=False, sort_keys=True))
        request_id = "review-context:" + context["client_request_id"]
        previous = self.store.get_op_result(request_id)
        if previous:
            if previous.get("fingerprint") != fingerprint:
                raise ApiError("idempotency_conflict", "核对请求编号已绑定其他任务或内容")
            return previous["result"]
        task = self.get_task(task_id)
        if (task["device_id"] != expected_device_id or task["status"] != expected_task_status
                or task["status"] not in {"confirmed", "failed", "unknown", "cancelled", "expired"}):
            raise ApiError("state_transition_invalid", "原任务或设备状态已变化，请刷新后重新核对")
        if any(other["device_id"] == task["device_id"] and other["task_id"] != task_id
               and other["status"] not in {"confirmed", "failed", "unknown", "cancelled", "expired"}
               for other in self.store.all_tasks()):
            raise ApiError("state_transition_invalid", "设备还有未结束任务，请先暂停并处理队列")
        running = self._exec_tasks.get(task_id)
        if running and not running.done():
            raise ApiError("state_transition_invalid", "原任务仍在收尾，请稍后再核对")
        lease = self.leases.get(task["device_id"])
        if lease and lease.get("task_id") != task_id:
            raise ApiError("state_transition_invalid", "设备正被其他操作占用，不能解除当前上下文")
        plugin, _ = self.plugins.instance_for_task_type(task["task_type"])
        ctx = ReviewContext(self, task, plugin.manifest)
        result = plugin.review_task(task, ctx, review_context)
        if not isinstance(result, dict) or result.get("context_released") is not True:
            message = result.get("message") if isinstance(result, dict) else None
            raise ApiError("state_transition_invalid", message or "插件拒绝解除执行上下文")
        ctx.commit()
        task.update(reviewed_by=context["operator_id"], reviewed_reason=context["reason"],
                    reviewed_at=iso(utc_now()), review_context=review_context)
        self.store.save_task(task)
        self.audit.record("task_marked_reviewed", operator_id=context["operator_id"],
                          operator_role=context.get("role"), task_id=task_id,
                          device_id=task["device_id"], reason=context["reason"],
                          correlation_id=context.get("correlation_id"),
                          client_request_id=context["client_request_id"],
                          detail={"context_released": True, "review_context": review_context})
        response = {"contract_version": "task-review.v1",
                    "task_id": task_id, "device_id": task["device_id"],
                    "status": task["status"], "context_released": True,
                    "reviewed_by": task["reviewed_by"]}
        self.store.save_op_result(request_id, {"fingerprint": fingerprint, "result": response})
        return response

    # ---- P06：Fleet 挂载 IME 仓储的控制钩子（§4.8.5） ----
    def ime_control_hooks(self, device_id: str) -> dict:
        """注入 Fleet 挂载的输入法仓储：typing 单次许可/交付/取消/确认。"""
        import contextlib
        from drivers.arm import DriverError
        from runtime import input_settings as _iset
        from runtime.input_control import (
            InputControlError, record_id_for_guard)

        def _owner_for_input(ime_task_id: str) -> dict:
            """输入副作用（typing/deliver）许可核对：owner + 当前撤销边界。"""
            owner = self._fleet_owner_for_ime_task(ime_task_id)
            if owner is None:
                raise InputControlError("任务缺少输入许可记录（不补办）")
            settings = _iset.load_settings(
                device_id, self.devices.settings_data_root(device_id))
            boundary = int(settings.get("revoked_through_revision") or 0)
            current = int(settings.get("revision") or 0)
            revision = int(owner.get("settings_revision") or 0)
            if owner.get("revocation") == "revoked" or not boundary < revision <= current:
                error = InputControlError(
                    f"输入许可已失效（版本 {revision}，边界 {boundary}）")
                error.hook_code = "input_authorization_revoked"
                raise error
            return owner

        def _owner_identity(ime_task_id: str, ime_device: str) -> dict:
            """取消/确认/补报身份核对：只核原任务与原手机，不要求当前输入许可
            仍有效（§4.8.5-6/7：撤销后仍接收安全对账，不能拒收旧 ack）。"""
            owner = self._fleet_owner_for_ime_task(ime_task_id)
            if owner is None:
                raise InputControlError("任务缺少输入许可记录（不补办）")
            if str(owner.get("ime_device_id") or "") != ime_device:
                error = InputControlError(
                    f"手机 {ime_device} 与原任务归属 "
                    f"{owner.get('ime_device_id')} 不一致")
                error.hook_code = "device_mismatch"
                raise error
            return owner

        def _license_reject(code: str, message: str) -> InputControlError:
            error = InputControlError(message)
            error.hook_code = code
            return error

        def _assert_input_license_locked(ime_task_id: str, ime_device: str,
                                         *, prospective_owner: dict | None = None) -> dict:
            """R3-01：最终认领（typing_claim/deliver）在设备短锁内的统一许可
            复核。与 _prepare_input_effect.claim_callback 同一判断集合
            （§4.8.4-5/R3-01-2）：owner（fleet_task 与 direct_ime 两类正式接纳
            来源）/设置历史与撤销边界/唯一当前绑定及修订/guard 未解决问题/
            prepared 设置意图/任务取消终态/急停/运行时资格。只做本地读与 CAS，
            不跨网络、不持设备锁等 IO；任一不成立抛 InputControlError
            （hook_code 标明原因），不认领、不补办当下许可。
            """
            owner = (prospective_owner if prospective_owner is not None
                     else self._fleet_owner_for_ime_task(ime_task_id))
            if owner is None:
                raise _license_reject(
                    "input_authorization_missing",
                    "任务缺少输入许可记录（不补办当下许可），最终认领拒绝")
            if (owner.get("device_id") != device_id
                    or str(owner.get("ime_device_id") or "") != ime_device):
                raise _license_reject(
                    "device_mismatch",
                    f"手机 {ime_device} 与原任务归属 "
                    f"{owner.get('ime_device_id')} 不一致，最终认领拒绝")
            if self._device_for_ime(ime_device) != device_id:
                raise _license_reject("input_binding_mismatch", "输入绑定已变化，最终认领拒绝")
            if prospective_owner is None and owner.get("owner_type") == "direct_ime":
                created = _effect(owner, ime_task_id, "ime_create")
                if created is None or created.get("state") != "completed":
                    raise _license_reject(
                        "input_create_uncertain", "直发创建没有完成回执，须先核对，不继续输入")
            # 设置历史（known check + checkpoint 回退/替换核对）与撤销边界：
            # 与关闭写入共用 load_settings_verified，不走低层 load_settings。
            from runtime.input_settings import InputSettingsError
            try:
                settings = self.input_control.load_settings_verified(
                    device_id, data_root=self.devices.settings_data_root(device_id))
            except InputSettingsError as exc:
                raise _license_reject(
                    "input_settings_invalid",
                    f"输入设置不可安全解释，最终认领拒绝：{exc.message}") from exc
            if not settings.get("enabled"):
                raise _license_reject("input_switch_disabled", "输入开关已关闭，拒绝输入")
            if self.pre_enable_maintenance:
                raise _license_reject("pre_enable_maintenance", "维护模式禁止输入")
            boundary = int(settings.get("revoked_through_revision") or 0)
            current = int(settings.get("revision") or 0)
            revision = int(owner.get("settings_revision") or 0)
            if owner.get("revocation") == "revoked" or not boundary < revision <= current:
                raise _license_reject(
                    "input_authorization_revoked",
                    f"输入许可已失效（许可版本 {revision}，撤销边界 {boundary}，"
                    f"当前 {current}），最终认领拒绝")
            saved_ime = str(settings.get("ime_device_id") or "")
            if saved_ime and saved_ime != ime_device:
                raise _license_reject(
                    "input_binding_mismatch",
                    f"设备当前保存授权手机 {saved_ime} 与请求手机 {ime_device} "
                    "不一致（换手机后旧许可不复活），最终认领拒绝")
            binding_ime = str((self.devices.input_binding(device_id) or {})
                              .get("ime_device_id") or "")
            if binding_ime and binding_ime != ime_device:
                raise _license_reject(
                    "input_binding_mismatch",
                    f"设备当前运行绑定手机 {binding_ime} 与请求手机 "
                    f"{ime_device} 不一致，最终认领拒绝")
            registered = int(self.devices.binding_view(device_id)
                             .get("binding_revision") or 0)
            if int(owner.get("binding_revision") or 0) != registered:
                raise _license_reject(
                    "input_binding_mismatch",
                    f"许可登记绑定修订 {owner.get('binding_revision')} 与当前设备"
                    f"登记修订 {registered} 不一致，最终认领拒绝")
            guard = self.store.input_record_get(record_id_for_guard(device_id))
            if isinstance(guard, dict):
                open_codes = sorted({
                    str(issue.get("code")) for issue in guard.get("issues") or []
                    if isinstance(issue, dict) and issue.get("state") == "open"})
                if open_codes:
                    raise _license_reject(
                        "input_guard_open",
                        f"设备存在未解决输入恢复问题（{'、'.join(open_codes)}），"
                        "最终认领拒绝")
            if self.input_control.has_prepared_intent(device_id) is not None:
                raise _license_reject(
                    "input_settings_commit_pending",
                    "同设备存在未对账的设置提交意图，最终认领拒绝")
            if str(owner.get("owner_type")) == "fleet_task":
                task_row = self.store.get_task(str(owner.get("owner_ref")))
                if task_row is not None and (
                        task_row.get("status") in (
                            "confirmed", "failed", "unknown", "cancelled",
                            "expired")
                        or task_row.get("cancel_requested")
                        or task_row.get("stop_result_unknown")):
                    raise _license_reject(
                        "input_task_cancelled",
                        f"任务已取消/终态（{task_row.get('status')}），"
                        "最终认领拒绝")
            if self.estop.active:
                raise _license_reject("estop_active", "急停生效中，最终认领拒绝")
            # 平台任务的 device_busy 可来自本动作正在执行；直发不持该任务
            # 租约，必须保留 busy 阻断，不能插入另一任务正在操作的编辑框。
            eligibility = self.devices.eligibility(device_id, require_input=True)
            blocking = [code for code in (eligibility.get("blocking_checks") or [])
                        if code != "device_busy" or owner.get("owner_type") != "fleet_task"]
            if blocking:
                raise _license_reject(
                    "runtime_eligibility_blocked",
                    f"运行时资格不满足（{'、'.join(blocking)}），最终认领拒绝")
            return owner

        def _effect(owner: dict, ime_task_id: str, kind: str) -> dict | None:
            from runtime.input_control import record_id_for_effect
            return self.store.input_record_get(
                record_id_for_effect(owner["record_id"],
                                     ["ime:" + ime_task_id, 0, kind]))

        @contextlib.contextmanager
        def direct_create(ime_task_id: str, ime_device: str, *, payload: dict,
                          find_existing):
            """正式直发接纳；持设备短锁覆盖许可认领与同步候选入库。

            IME 与 Fleet 分库：先持久认领再创建候选，失败按未知保留事实，
            同 taskId 永不换用新设置修订，也不重放已认领的创建 slot。
            """
            from runtime.input_settings import InputSettingsError
            with self.input_control.device_lock(device_id):
                original = find_existing()
                if original is not None:
                    yield original
                    return
                if self._device_for_ime(ime_device) != device_id:
                    raise _license_reject("input_binding_mismatch", "输入绑定已变化")
                existing = self._fleet_owner_for_ime_task(ime_task_id)
                if existing is not None and existing.get("owner_type") != "direct_ime":
                    raise _license_reject("input_owner_conflict", "直发编号已属于平台任务")
                try:
                    settings = self.input_control.load_settings_verified(
                        device_id, data_root=self.devices.settings_data_root(device_id))
                except InputSettingsError as exc:
                    raise _license_reject("input_settings_invalid", exc.message) from exc
                if not settings.get("enabled"):
                    raise _license_reject("input_switch_disabled", "输入开关已关闭，拒绝直发")
                proposed = self.input_control.build_owner(
                    owner_type="direct_ime", owner_ref=ime_task_id, device_id=device_id,
                    ime_device_id=ime_device,
                    settings_revision=int(settings["revision"]),
                    binding_revision=int(self.devices.binding_view(device_id)
                                         .get("binding_revision") or 0), payload=payload)
                if existing is not None and (
                        existing["payload_digest"] != proposed["payload_digest"]
                        or existing["device_id"] != device_id):
                    raise _license_reject("task_conflict", "taskId 已存在不同的直发许可")
                _assert_input_license_locked(
                    ime_task_id, ime_device, prospective_owner=existing or proposed)
                owner, _created = self.input_control.admit_owner(proposed)
                effect, _created = self.input_control.insert_effect(
                    self.input_control.build_effect(
                        owner_id=owner["record_id"], device_id=device_id,
                        slot_key=["ime:" + ime_task_id, 0, "ime_create"],
                        payload=payload, ime_task_id=ime_task_id))
                if effect["state"] != "prepared":
                    raise _license_reject(
                        "input_create_already_claimed", "该任务的创建许可已使用，不再创建候选")
                effect = self.input_control.claim_effect(
                    effect["record_id"], expected_revision=effect["revision"])
                try:
                    yield None
                    self.input_control.finish_effect(
                        effect["record_id"], expected_revision=effect["revision"], state="completed")
                except BaseException as exc:
                    with contextlib.suppress(Exception):
                        self.input_control.finish_effect(
                            effect["record_id"], expected_revision=effect["revision"], state="unknown")
                    self.input_control.open_guard_issue(
                        device_id, code="input_effect_unknown", owner_id=owner["record_id"],
                        detail={"ime_task_id": ime_task_id, "effect_kind": "ime_create"})
                    error = _license_reject(
                        "input_create_uncertain", "直发创建结果需核对，原许可不得重复使用")
                    error.hook_status = 503
                    raise error from exc

        def typing_claim(ime_task_id: str, ime_device: str) -> dict:
            owner = _owner_for_input(ime_task_id)
            existing = _effect(owner, ime_task_id, "ime_start")
            if existing is not None and existing["state"] in {"claimed", "completed", "unknown"}:
                error = InputControlError("该任务的开始许可已认领，不再发第二次")
                error.hook_code = "input_start_already_claimed"
                raise error
            effect = self.input_control.build_effect(
                owner_id=owner["record_id"], device_id=device_id,
                slot_key=["ime:" + ime_task_id, 0, "ime_start"],
                payload={"ime_task_id": ime_task_id},
                ime_task_id=ime_task_id)
            effect, _created = self.input_control.insert_effect(effect)
            with self.input_control.device_lock(device_id):
                # R3-01：初检后在同一设备短锁内统一复核（关闭/guard/设置意图/
                # 任务取消/急停/换绑/资格任一先赢即零新认领），再认领同一 effect。
                _assert_input_license_locked(ime_task_id, ime_device)
                return self.input_control.claim_effect(
                    effect["record_id"], expected_revision=effect["revision"])

        def deliver(ime_task_id: str, ime_device: str) -> None:
            owner = _owner_for_input(ime_task_id)
            effect = self.input_control.build_effect(
                owner_id=owner["record_id"], device_id=device_id,
                slot_key=["ime:" + ime_task_id, 0, "ime_deliver"],
                payload={"ime_task_id": ime_task_id},
                ime_task_id=ime_task_id)
            effect, _created = self.input_control.insert_effect(effect)
            with self.input_control.device_lock(device_id):
                # R3-01：交付与开始共用同一锁内统一复核（R3-01-1/2）。
                _assert_input_license_locked(ime_task_id, ime_device)
                self.input_control.claim_effect(
                    effect["record_id"], expected_revision=effect["revision"])

        def cancel(ime_task_id: str, ime_device: str, *, delivered: bool) -> None:
            owner = _owner_identity(ime_task_id, ime_device)
            effect = _effect(owner, ime_task_id, "ime_create")
            if effect is None:
                # 直接经仓储创建（未经 input.ime_create）的候选：取消时补建
                # ime_create 记录，承载 cancel_state 事实链。
                effect, _created = self.input_control.insert_effect(
                    self.input_control.build_effect(
                        owner_id=owner["record_id"], device_id=device_id,
                        slot_key=["ime:" + ime_task_id, 0, "ime_create"],
                        payload={"ime_task_id": ime_task_id},
                        ime_task_id=ime_task_id))
            revision = effect["revision"]
            if not delivered:
                if effect["state"] == "prepared":
                    # 未交付未认领：可靠的未开始（repository_cancelled，无需握手）
                    self.input_control.mark_effect_not_started(
                        effect["record_id"], expected_revision=revision,
                        cancel_state="not_created")
                else:
                    self.input_control.set_cancel_state(
                        effect["record_id"], expected_revision=revision,
                        cancel_state="not_created")
            elif effect.get("cancel_state") in (None, "none"):
                self.input_control.set_cancel_state(
                    effect["record_id"], expected_revision=revision,
                    cancel_state="requested")
            if owner.get("owner_type") == "direct_ime" and delivered:
                # 原手机取消回执须覆盖该直发任务所有已经交付/开始的固定 slot，
                # 才能让后续人工核对解除阻断；保持 claimed 执行事实，不伪造完成。
                for kind in ("ime_deliver", "ime_start"):
                    pending = _effect(owner, ime_task_id, kind)
                    if pending is not None and pending.get("cancel_state") in (None, "none"):
                        self.input_control.set_cancel_state(
                            pending["record_id"], expected_revision=pending["revision"],
                            cancel_state="requested")

        def cancel_pending_check(ime_task_id: str, ime_device: str) -> bool:
            """取消工作器扫描检查：pending 候选是否已有取消理由
            （owner 撤销/撤销边界以下/许可不可解释）。只读，不抛错。"""
            try:
                owner = self._fleet_owner_for_ime_task(ime_task_id)
                if owner is None:
                    return True  # 无归属旧任务：不补当前授权，按旧任务取消
                if str(owner.get("ime_device_id") or "") != ime_device:
                    return True
                if owner.get("owner_type") == "direct_ime":
                    created = _effect(owner, ime_task_id, "ime_create")
                    if created is None or created.get("state") in ("unknown", "not_started"):
                        return True  # 分库提交窗口中断：只取消/核对，不自动续输入
                settings = _iset.load_settings(
                    device_id, self.devices.settings_data_root(device_id))
                boundary = int(settings.get("revoked_through_revision") or 0)
                current = int(settings.get("revision") or 0)
                revision = int(owner.get("settings_revision") or 0)
                return (owner.get("revocation") == "revoked"
                        or not boundary < revision <= current)
            except Exception:
                return True  # 许可不可解释：保守取消，阻断不放开

        def ack(ime_task_id: str, ime_device: str,
                typed_character_count: int | None = None) -> None:
            owner = _owner_identity(ime_task_id, ime_device)
            for kind in ("ime_create", "ime_deliver", "ime_start"):
                effect = _effect(owner, ime_task_id, kind)
                if effect is None or effect.get("cancel_state") != "requested":
                    continue
                self.input_control.set_cancel_state(
                    effect["record_id"], expected_revision=effect["revision"],
                    cancel_state="confirmed")
            # §4.8.5-8：取消确认不证明编辑框为空——计数>0/计数未知/已认领
            # ime_start/候选点击/文本输入，任一成立即建立 editor_review_required。
            claimed_kinds = []
            for kind in ("ime_start", "candidate_commit", "editor_clear", "text_type"):
                effect = _effect(owner, ime_task_id, kind)
                if effect is not None and effect.get("state") in ("claimed", "completed", "unknown"):
                    claimed_kinds.append(kind)
            # 候选点击/文本输入 effect 使用任务步骤 slot（非 ime: slot），按 owner 扫描
            for record in self.store.input_records_by_device(device_id, "effect"):
                if record.get("owner_id") != owner["record_id"]:
                    continue
                if record.get("effect_kind") in ("candidate_commit", "editor_clear", "text_type") \
                        and record.get("state") in ("claimed", "completed", "unknown") \
                        and record.get("effect_kind") not in claimed_kinds:
                    claimed_kinds.append(record["effect_kind"])
            if typed_character_count is None or typed_character_count > 0 or claimed_kinds:
                self.input_control.open_guard_issue(
                    device_id, code="editor_review_required",
                    owner_id=owner["record_id"],
                    detail={"ime_task_id": ime_task_id,
                            "typed_character_count": typed_character_count,
                            "claimed_effects": claimed_kinds})

        return {"direct_create": direct_create,
                "typing_claim": typing_claim, "deliver": deliver,
                "cancel": cancel, "ack": ack,
                "cancel_pending_check": cancel_pending_check}
