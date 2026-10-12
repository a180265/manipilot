"""平台配置加载：configs/platform.json + configs/devices.json，环境变量可覆盖。

关键约束：
- hardware_mode 只能是 simulation / real；真机参数未经项目方书面确认时，
  配置加载器强制回退 simulation（无硬件配置时只能运行仿真或待确认状态）。
- 第一版默认仅监听 127.0.0.1；局域网监听必须显式开启 ARM_FLEET_ALLOW_LAN=1。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PLATFORM_CONFIG: dict = {
    "listen_host": "127.0.0.1",
    "listen_port": 8788,
    "allow_lan": False,
    "hardware_mode": "simulation",
    "pre_enable_maintenance": False,
    "heartbeat_interval_seconds": 2,
    "camera_check_interval_seconds": 10,
    "view_session_ttl_seconds": 60,
    "view_session_renew_advice_seconds": 20,
    "view_session_max_per_device": 2,
    "camera_frame_interval_seconds": 0.5,
    "camera_broker_max_age_seconds": 1.5,
    "camera_preview_enabled": False,
    "camera_preview_host": "127.0.0.1",
    "camera_preview_port": 8793,
    "camera_preview_target_fps": 30,
    "default_task_timeout_seconds": 120,
    "evidence_dir": "evidence/store",
    "db_path": "data/armfleet.db",
    "plugin_dir": "plugins_installed",
    "mac_mini_id": "mac-mini-01",
    "contract_version": "v1",
    "agent_version": "arm-fleet-0.1.0",
    "wx_callback_base_url": "",
    "wx_callback_results_path": "/v1/arm-fleet/task-results",
    "wx_callback_messages_path": "/v1/arm-fleet/customer-messages",
    "wx_callback_estop_path": "/v1/arm-fleet/emergency-stop",
    "wx_callback_takeover_path": "/internal/conversations/{conversation_id}/takeover",
    "wx_compat_enabled": False,
    # R4-12-E（wx-camera-binding.v1）：业务 绑定查看会话的授权复核通道。
    # introspect URL 未配置=复核关闭（撤销推送仍生效）；令牌文件未配置=
    # 内部撤销端点拒绝一切请求（无无令牌模式）。
    "wx_camera_introspect_url": "",
    "wx_camera_token_file": "",
    "wx_camera_recheck_seconds": 2.0,
    "wx_camera_recheck_grace": 1,
    # §9.3(3)：当前部署不能隔离本地操作员控制通道时，人工无姿态唤醒例外
    # 整体禁用（确认接口 403）；确需启用必须先完成操作员通道隔离确认。
    "screen_wake_manual_confirm_enabled": False,
    "screen_ime_enabled": True,
    "screen_ime_db_path": "data/screen_ime_tasks.db",
    "screen_ime_mappings_path": "data/screen_ime_mappings.json",
    "hotplug_discovery_enabled": True,
    "hotplug_discovery_interval_seconds": 2,
    "daemon_supervisor_enabled": True,
    "daemon_supervisor_poll_interval_seconds": 1.0,
    "daemon_supervisor_restart_delay_seconds": 1.0,
    "daemon_supervisor_max_restarts": 5,
    # 8788 重载默认不停止独立 8795；只有明确开启才在进程退出时杀 daemon。
    "daemon_supervisor_stop_on_shutdown": False,
    "daemon_supervisor_python": "",
    "daemon_supervisor_script": "scripts/arm_daemon3.py",
    "daemon_supervisor_log_dir": "data/daemons",
    "platform_version": "0.1.0",
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_overrides() -> dict:
    """环境变量覆盖：ARM_FLEET_PORT、ARM_FLEET_HOST、ARM_FLEET_MODE、
    ARM_FLEET_ALLOW_LAN、ARM_FLEET_WX_CALLBACK、ARM_FLEET_SCREEN_IME、
    ARM_FLEET_DAEMON_SUPERVISOR、ARM_FLEET_PRE_ENABLE。"""
    overrides: dict = {}
    if os.environ.get("ARM_FLEET_HOST"):
        overrides["listen_host"] = os.environ["ARM_FLEET_HOST"]
    if os.environ.get("ARM_FLEET_ALLOW_LAN") is not None:
        overrides["allow_lan"] = os.environ["ARM_FLEET_ALLOW_LAN"] not in ("0", "false", "no")
    if os.environ.get("ARM_FLEET_PORT"):
        overrides["listen_port"] = int(os.environ["ARM_FLEET_PORT"])
    if os.environ.get("ARM_FLEET_MODE"):
        overrides["hardware_mode"] = os.environ["ARM_FLEET_MODE"]
    callback_env = (os.environ.get("ARM_FLEET_TASK_CALLBACK")
                    or os.environ.get("ARM_FLEET_WX_CALLBACK"))
    if callback_env:
        overrides["wx_callback_base_url"] = callback_env
    if os.environ.get("ARM_FLEET_WX_INTROSPECT_URL"):
        overrides["wx_camera_introspect_url"] = os.environ["ARM_FLEET_WX_INTROSPECT_URL"]
    if os.environ.get("ARM_FLEET_WX_CAMERA_TOKEN_FILE"):
        overrides["wx_camera_token_file"] = os.environ["ARM_FLEET_WX_CAMERA_TOKEN_FILE"]
    if os.environ.get("ARM_FLEET_WX_COMPAT") is not None:
        overrides["wx_compat_enabled"] = os.environ["ARM_FLEET_WX_COMPAT"] not in ("0", "false", "no")
    if os.environ.get("ARM_FLEET_SCREEN_IME") is not None:
        overrides["screen_ime_enabled"] = os.environ["ARM_FLEET_SCREEN_IME"] not in ("0", "false", "no")
    if os.environ.get("ARM_FLEET_DAEMON_SUPERVISOR") is not None:
        overrides["daemon_supervisor_enabled"] = os.environ["ARM_FLEET_DAEMON_SUPERVISOR"] not in ("0", "false", "no")
    if os.environ.get("ARM_FLEET_PRE_ENABLE") is not None:
        overrides["pre_enable_maintenance"] = os.environ["ARM_FLEET_PRE_ENABLE"].lower() not in ("0", "false", "no", "")
    return overrides


def task_callback_base_url(config) -> str:
    """任务回报基址（中性名优先）：ARM_FLEET_TASK_CALLBACK >
    ARM_FLEET_WX_CALLBACK（旧名兼容）> 配置键 wx_callback_base_url。"""
    env = (os.environ.get("ARM_FLEET_TASK_CALLBACK")
           or os.environ.get("ARM_FLEET_WX_CALLBACK")
           or "")
    return env or str(config.get("wx_callback_base_url", ""))


class PlatformConfig:
    def __init__(self, data: dict, root: Path = PROJECT_ROOT):
        self._data = dict(data)
        if self._data.get("pre_enable_maintenance"):
            # 维护冻结最后生效，不能被其他环境开关或调用方注入的配置放开。
            self._data.update(
                screen_ime_enabled=False,
                wx_compat_enabled=False,
                daemon_supervisor_enabled=False,
                daemon_supervisor_stop_on_shutdown=False,
                hotplug_discovery_enabled=False,
                wx_callback_base_url="",
                wx_camera_introspect_url="",
            )
        self.root = root

    def get(self, key: str, default=None):
        return self._data.get(key, DEFAULT_PLATFORM_CONFIG.get(key, default))

    def _resolve_path(self, value: str | Path) -> Path:
        """Resolve relative paths against this configuration's root.

        Tests, embedded deployments, and recovery tooling can construct a
        ``PlatformConfig`` with a private root.  Resolving through the module
        checkout would silently mix their databases and evidence directories.
        """
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    @property
    def hardware_mode(self) -> str:
        return self._data.get("hardware_mode", "simulation")

    @property
    def db_path(self) -> Path:
        return self._resolve_path(self.get("db_path"))

    @property
    def evidence_dir(self) -> Path:
        return self._resolve_path(self.get("evidence_dir"))

    @property
    def plugin_dir(self) -> Path:
        return self._resolve_path(self.get("plugin_dir"))

    @property
    def screen_ime_db_path(self) -> Path:
        return self._resolve_path(self.get("screen_ime_db_path"))

    @property
    def screen_ime_mappings_path(self) -> Path:
        return self._resolve_path(self.get("screen_ime_mappings_path"))

    def to_dict(self) -> dict:
        return dict(self._data)


def load_platform_config(root: Path = PROJECT_ROOT) -> PlatformConfig:
    config_path = root / "configs" / "platform.json"
    data = dict(DEFAULT_PLATFORM_CONFIG)
    if config_path.exists():
        data = _deep_merge(data, json.loads(config_path.read_text(encoding="utf-8")))
    data = _deep_merge(data, _env_overrides())

    # 局域网监听必须显式开启，避免启动环境意外覆盖本机安全默认值。
    listen_host = str(data.get("listen_host") or "127.0.0.1").strip()
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if listen_host not in loopback_hosts and not bool(data.get("allow_lan", False)):
        data["listen_host"] = "127.0.0.1"
        data["listen_host_note"] = (
            f"已阻止未显式授权的局域网监听（请求地址 {listen_host}）；"
            "如需手机或局域网访问，请同时设置 ARM_FLEET_ALLOW_LAN=1，并完成网络隔离与鉴权确认"
        )
    elif listen_host not in loopback_hosts:
        data["listen_host_note"] = (
            f"已显式启用局域网监听（{listen_host}）；请确认防火墙、局域网隔离和访问鉴权"
        )

    # 真机模式必须有项目方签字确认文件，否则强制停留在仿真。
    if data.get("hardware_mode") == "real":
        confirmation = root / "configs" / "硬件输入确认表.json"
        confirmed = False
        if confirmation.exists():
            try:
                table = json.loads(confirmation.read_text(encoding="utf-8"))
                confirmed = bool(table.get("all_confirmed")) and all(
                    item.get("status") == "已确认" for item in table.get("items", [])
                )
            except Exception:
                confirmed = False
        if not confirmed:
            data["hardware_mode"] = "simulation"
            data["hardware_mode_note"] = "真机硬件参数未确认，已强制回退仿真模式（hardware_unconfirmed）"
    return PlatformConfig(data, root)


def validate_device_configs(devices: list[dict]) -> None:
    """十设备绑定表一对一校验：编号/槽位/串口/daemon/摄像头/输入法/微信账号不得重复。

    只校验非空值；None 字段视为未绑定，不参与冲突判定。
    冲突时抛出中文冲突说明，配置加载阶段即拒绝启动。
    """
    from runtime.errors import ApiError

    fields = ("device_id", "arm_slot", "camera_id", "camera_device_unique_id",
              "serial_port", "daemon_addr", "ime_device_id", "wechat_account_id")
    labels = {
        "device_id": "设备编号", "arm_slot": "机械臂槽位", "camera_id": "摄像头编号",
        "camera_device_unique_id": "物理摄像头唯一标识",
        "serial_port": "串口", "daemon_addr": "daemon 地址", "ime_device_id": "输入法设备",
        "wechat_account_id": "微信账号",
    }
    seen: dict[str, dict[str, str]] = {field: {} for field in fields}
    seen_device_ids: set[str] = set()
    for item in devices:
        if not isinstance(item, dict):
            raise ApiError("device_config_invalid", "设备配置必须是对象")
        device_id = str(item.get("device_id") or "").strip()
        if not device_id:
            raise ApiError("device_config_invalid", "设备配置缺少 device_id")
        if device_id in seen_device_ids:
            raise ApiError(
                "device_config_conflict",
                f"设备编号 {device_id} 在绑定表中出现多次，请合并或删除重复条目")
        seen_device_ids.add(device_id)
        for field in fields:
            value = item.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            value = str(value)
            owner = seen[field].get(value)
            if owner is not None and owner != device_id:
                raise ApiError(
                    "device_config_conflict",
                    f"{labels[field]} {value} 同时出现在 {owner} 和 {device_id}，"
                    "一台机械臂一个 daemon、一条串口一个持有者，请修正绑定表后重启")
            seen[field][value] = device_id
        _validate_camera_config(item)


def _validate_camera_config(cfg: dict) -> None:
    """校验正式摄像头模式的分路身份，避免启动后才发现串路配置。"""
    from runtime.errors import ApiError

    device_id = str(cfg.get("device_id") or "").strip()
    camera_id = str(cfg.get("camera_id") or "").strip()
    camera_mode = str(cfg.get("camera_mode") or "").strip()
    is_real = str(cfg.get("binding_mode") or "") == "real" or str(cfg.get("driver_mode") or "") in {"real", "daemon"}
    if not is_real:
        return
    if not camera_id:
        raise ApiError("camera_config_invalid", f"真机设备 {device_id} 缺少 camera_id")
    if camera_mode == "legacy_single" and device_id != "arm-01":
        raise ApiError("camera_config_invalid", "camera_mode=legacy_single 只允许 arm-01")
    if camera_mode == "multi_channel":
        frame_url = str(cfg.get("camera_frame_url") or "").strip()
        if not frame_url:
            raise ApiError("camera_config_invalid", f"真机多路设备 {device_id} 缺少 camera_frame_url")
        frame_path = urlparse(frame_url).path
        if f"/cameras/{camera_id}/" not in frame_path:
            raise ApiError(
                "camera_config_invalid",
                f"设备 {device_id} 的多路 camera_frame_url 未指向本设备 camera_id={camera_id}",
            )
        status_url = str(cfg.get("camera_status_url") or "").strip()
        if status_url and f"/cameras/{camera_id}/" not in urlparse(status_url).path:
            raise ApiError(
                "camera_config_invalid",
                f"设备 {device_id} 的 camera_status_url 未指向本设备 camera_id={camera_id}",
            )


def load_device_configs(root: Path = PROJECT_ROOT) -> list[dict]:
    # 只读取 configs/devices.json 这一份白名单绑定表；同目录其他文件
    # （包括历史遗留配置）一律不参与启动，避免误读冲突。
    devices_path = root / "configs" / "devices.json"
    if not devices_path.exists():
        return []
    payload = json.loads(devices_path.read_text(encoding="utf-8"))
    devices = payload.get("devices", [])
    validate_device_configs(devices)
    for item in devices:
        if isinstance(item, dict):
            item.setdefault("enforce_input_gate", True)
    return devices


def save_device_configs(root: Path, devices: list[dict]) -> Path:
    """把绑定变更写回 configs/devices.json（原子替换 devices 数组，保留注释等顶层字段）。"""
    validate_device_configs(devices)
    devices_path = root / "configs" / "devices.json"
    devices_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    if devices_path.exists():
        try:
            payload = json.loads(devices_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    payload["devices"] = devices
    tmp_path = devices_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(devices_path)
    return devices_path
