"""平台配置加载：configs/platform.json + configs/devices.json，环境变量可覆盖。

关键约束：
- hardware_mode 只能是 simulation / real；真机参数未经项目方书面确认时，
  配置加载器强制回退 simulation（无硬件配置时只能运行仿真或待确认状态）。
- 第一版默认仅监听 127.0.0.1；局域网监听必须显式开启 ARM_FLEET_ALLOW_LAN=1。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

try:  # Windows 无 fcntl：降级为无锁，并在文档里如实声明范围
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 非 POSIX 平台
    fcntl = None  # type: ignore[assignment]

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


# 静态绑定表允许落盘的字段（写盘投影白名单）。
# 来源 = devices.json 现存键 ∪ runtime/devices.py 实际读取的 cfg 键；
# 目的是挡住 attach 后把 38 个运行时键（daemon_session/heartbeat/quality/home_*）
# 灌回被 git 跟踪的文件。新增真正需要持久化的字段时，必须同时登记到这里，
# 否则会被静默丢弃（test_device_config_static_keys 会拦）。
STATIC_KEYS = frozenset({
    "arm_binding_id", "arm_slot", "baud_rate", "binding_enabled", "binding_mode",
    "binding_note", "binding_revision", "calibration_status", "calibration_validated_at",
    "camera_binding_id", "camera_device_name", "camera_device_unique_id",
    "camera_frame_provider", "camera_frame_url", "camera_id", "camera_index",
    "camera_mode", "camera_source_mode", "camera_status_url", "capabilities",
    "config_version", "daemon_addr", "daemon_binding_id", "daemon_resource_id",
    "data_root", "device_id", "device_mode", "driver_mode", "enforce_input_gate",
    "expected_frame_fps", "expected_frame_height", "expected_frame_width",
    "hardware_mode", "hotplug", "ime_device_id", "ime_online", "input_device_id",
    "keyboard_layout", "mac_mini_id", "name_display", "serial_port", "sim_calibrated",
    "sim_ui_confidence", "sim_ui_state", "sim_wechat_window", "source_geometry_id",
    "startup_view", "task_id", "transport_factory", "wechat_account_id",
})


_JSON_SCALARS = (str, int, float, bool)


def _json_safe(value) -> bool:
    """落盘值必须是 JSON 可序列化的（transport_factory 是函数、data_root 可能是 Path）。"""
    if value is None or isinstance(value, _JSON_SCALARS):
        return True
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_safe(item)
                   for key, item in value.items())
    return False


def project_static_device(entry: dict) -> dict:
    """把一条设备记录投影到 STATIC_KEYS（运行时键一律不落盘，非 JSON 值丢弃）。"""
    return {key: value for key, value in entry.items()
            if key in STATIC_KEYS and _json_safe(value)}


@contextlib.contextmanager
def device_config_lock(root: Path, *, timeout: float = 1.0):
    """configs/.devices.lock 上的进程间排他锁。

    devices.json 有 7 处写入方（调试台换口、发现循环、相机换绑、绑定变更…），过去各自
    "读整表→改一处→写整表"且无固定锁：共享固定临时名会让生效文件变成半截 JSON，启动直接失败。

    本锁只保证**写入不撕裂、不并发交错**；它**不防丢更新**——调用方若在锁外读整表再整表写回，
    仍会覆盖别人的改动。要防丢更新必须走 update_device_config()（per-device 锁内读改写，P1）。

    超时默认 1.0s：调用点可能在 async 处理器里，长时间 sleep 轮询会卡住整个事件循环。
    POSIX 用 fcntl.flock；Windows 无 fcntl 时降级为无锁（范围如实声明：Windows 部署需单写者纪律）。
    """
    from runtime.errors import ApiError

    lock_path = root / "configs" / ".devices.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            deadline = time.monotonic() + max(0.1, timeout)
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ApiError(
                            "device_config_busy",
                            f"绑定表被其它进程占用超过 {timeout:g}s，请稍后重试")
                    time.sleep(0.02)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def read_device_config_payload(devices_path: Path) -> dict:
    """读 devices.json 顶层对象；损坏时报错，不再静默降级成 {} 丢掉 comment/binding_mode。"""
    from runtime.errors import ApiError

    if not devices_path.exists():
        return {}
    try:
        payload = json.loads(devices_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApiError("device_config_invalid",
                       f"绑定表无法解析，原文件未改动：{devices_path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ApiError("device_config_invalid",
                       f"绑定表顶层必须是对象，原文件未改动：{devices_path}")
    return payload


def atomic_write_json(path: Path, payload: dict) -> None:
    """同目录唯一临时名 + fsync + os.replace + 目录 fsync。

    唯一临时名是关键：固定 `devices.json.tmp` 时两个进程同写一个 inode，慢写方的尾巴
    会落在快写方 replace 之后，把生效文件写成半截 JSON。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    # mkstemp 固定建 0600；不显式继承原文件权限，第一次写就会把 devices.json
    # 从 0644 悄悄收紧成 0600，跨用户/以 root 跑的读者会突然读不到（2026-10-29 审查）。
    existing_mode: int | None = None
    try:
        if path.exists():
            existing_mode = path.stat().st_mode & 0o777
    except OSError:
        existing_mode = None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(tmp_name, existing_mode)
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def update_device_config(root: Path, device_id: str, changes: dict,
                         *, create_if_missing: bool = False,
                         create_seed: dict | None = None,
                         timeout: float = 1.0) -> dict:
    """锁内「重读磁盘 → 只改这一台 → 校验 → 原子写」，返回写后的设备条目。

    与 save_device_configs 的区别是**不丢更新**：save 用调用方手里的整表覆盖磁盘，
    别人在锁外读到旧表再写回就会互相回滚（典型：Fleet 内存副本把调试台刚写的新串口
    改回旧值）。这里在锁内以磁盘为准做字段级合并，其余设备保持磁盘现值。
    """
    from runtime.errors import ApiError

    if not isinstance(changes, dict) or not changes:
        raise ApiError("invalid_request", "绑定变更必须是非空对象")
    if "device_id" in changes and str(changes["device_id"]) != device_id:
        raise ApiError("invalid_request",
                       f"不能通过绑定变更改设备标识（{device_id} → {changes['device_id']}）")
    unknown = set(changes) - STATIC_KEYS
    if unknown:
        raise ApiError("invalid_request",
                       f"不认识的绑定字段：{', '.join(sorted(unknown))}")
    devices_path = root / "configs" / "devices.json"
    devices_path.parent.mkdir(parents=True, exist_ok=True)
    with device_config_lock(root, timeout=timeout):
        payload = read_device_config_payload(devices_path)
        raw_entries = payload.get("devices")
        if raw_entries is None:
            entries = []
        elif isinstance(raw_entries, list):
            entries = [project_static_device(dict(item))
                       for item in raw_entries if isinstance(item, dict)]
        else:
            # 文件被改坏但仍可解析时，旧逻辑会静默清表（只剩目标设备）。
            # 与 read_device_config_payload「损坏报错、原文件不动」保持一致。
            raise ApiError("device_config_invalid",
                           f"绑定表的 devices 字段必须是数组，原文件未改动：{devices_path}")
        target = next((item for item in entries
                       if str(item.get("device_id")) == device_id), None)
        if target is None:
            if not create_if_missing:
                raise ApiError(
                    "device_not_registered",
                    f"设备 {device_id} 不在静态绑定表（configs/devices.json），未做任何修改")
            # 仅创建分支用 create_seed 补齐静态字段；已存在设备一律只合并 changes，
            # 否则"只改 binding_enabled"会退化成整条内存记录覆盖磁盘（同一设备的丢更新）。
            target = {"device_id": device_id,
                      **project_static_device(dict(create_seed or {}))}
            entries.append(target)
        target.update(changes)
        validate_device_configs(entries)
        payload["devices"] = entries
        atomic_write_json(devices_path, payload)
        return dict(target)


def save_device_configs(root: Path, devices: list[dict]) -> Path:
    """把绑定变更写回 configs/devices.json（锁内原子替换 devices 数组，保留顶层字段）。

    整表接口只用于初始化/迁移/测试；运行期绑定变更请用 update_device_config
    （锁外读整表再整表写回会覆盖别人刚写的值，即丢更新）。
    写前统一投影到 STATIC_KEYS，运行时键不落盘。
    """
    devices = [project_static_device(dict(item)) for item in devices]
    validate_device_configs(devices)
    devices_path = root / "configs" / "devices.json"
    devices_path.parent.mkdir(parents=True, exist_ok=True)
    with device_config_lock(root):
        payload = read_device_config_payload(devices_path)
        payload["devices"] = devices
        atomic_write_json(devices_path, payload)
    return devices_path
