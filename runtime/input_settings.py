"""设备级「允许输入与发送」开关（规格《输入开关与校准直驱上下滑动修改规格》§4）。

事实只保存一份：data/devices/{device_id}/input_settings.json。
- 文件不存在等价于关闭、revision=0；不从旧 E 通过记录推断开启。
- 文件损坏保留原文件，按禁止输入处理（调用方以 input_settings_invalid 处理）。
- 开关不写 calibration_bundle、不影响校准指纹；只是操作者持久选择，不代表当前就绪。
- 幂等账本持久保存在同文件 requests：完整规范化摘要 + before/after + 原响应快照，
  同 client_request_id 同内容重放返回原快照，不同内容= request_conflict。
- revoked_through_revision：关闭写入时推进到当次新 revision；开启不降低（§4.5.2）。

FIX-04（2026-10-04 实施单）补强：
- 旧格式文件（无 revoked_through_revision 的缺失撤销字段档案）不再默认边界 0，
  一律 input_settings_invalid 拒绝——旧开启档不能借缺失字段绕过撤销边界。
- 严格区分「真正从未建立资料的新设备」（缺文件→空白默认）与
  「已知设备文件丢失/损坏/回退」（拒绝输入，不创建空白默认覆盖）。
  已知判据：迁移维护意图存在（maintenance/input-control 下本设备 intent），
  或调用方注入的 known_check（Fleet DB 有输入控制记录）返回 True。
- 迁移维护意图未 committed（prepared/损坏）时阻断该设备一切设置读取：
  只能由 tools/migrate_input_settings.py --resume 处理，不自动迁移/开启/重放。
- migration_baseline 存在时校验其结构与不变量；旧编号 tombstone
  （legacy_request_ids）与维护编号在 PUT 时按 409 语义拒绝重放。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

CONTRACT_VERSION = "device-input-settings.v1"
MIGRATION_BASELINE_VERSION = "input-settings-migration.v1"
MAINTENANCE_INTENT_VERSION = "input-settings-migration-intent.v1"
MAINTENANCE_DIR = Path("maintenance") / "input-control"

# §4.2.1：安全整数上限；到达上限只允许已关闭状态，禁止回绕或重置。
MAX_REVISION = 9007199254740991

# §4.2.2 状态对象中的持久字段（requests/操作者资料不对普通 GET 返回）。
PUBLIC_FIELDS = ("contract_version", "device_id", "ime_device_id", "enabled",
                 "revision", "revoked_through_revision", "updated_at")

_write_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class InputSettingsError(Exception):
    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        self.message = message
        super().__init__(f"{reason_code}: {message}")


def _lock_for(device_id: str) -> threading.Lock:
    with _locks_guard:
        if device_id not in _write_locks:
            _write_locks[device_id] = threading.Lock()
        return _write_locks[device_id]


def settings_path(device_id: str, data_root: Path | str | None = None) -> Path:
    root = Path(data_root) if data_root else Path(__file__).resolve().parents[1] / "data"
    return root / "devices" / device_id / "input_settings.json"


def _blank(device_id: str) -> dict:
    return {"contract_version": CONTRACT_VERSION, "device_id": device_id,
            "ime_device_id": None, "enabled": False, "revision": 0,
            "revoked_through_revision": 0,
            "updated_at": None, "operator_id": None, "reason": None,
            "client_request_id": None, "requests": {}}


def _valid_revision(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_REVISION


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 \
        and all(ch in "0123456789abcdef" for ch in value)


# ---------- 迁移维护意图（§4.9：正常启动先查维护目录） ----------

def maintenance_intent_rel_dir(device_id: str, migration_id: str) -> Path:
    """{data_root}/maintenance/input-control/{SHA256([device_id, migration_id])}/"""
    key = json.dumps([device_id, migration_id], ensure_ascii=False,
                     separators=(",", ":"))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return Path(MAINTENANCE_DIR) / digest


def load_maintenance_intents(device_id: str, data_root: Path | str | None = None) -> list[dict]:
    """读取本设备全部迁移维护意图；解析失败的意图原样带 _corrupt 标记返回。"""
    root = Path(data_root) if data_root else Path(__file__).resolve().parents[1] / "data"
    base = root / MAINTENANCE_DIR
    if not base.is_dir():
        return []
    found: list[dict] = []
    for intent_path in sorted(base.glob("*/intent.json")):
        try:
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            found.append({"_corrupt": True, "_path": str(intent_path),
                          "device_id": None})
            continue
        if not isinstance(intent, dict):
            found.append({"_corrupt": True, "_path": str(intent_path),
                          "device_id": None})
            continue
        if intent.get("device_id") == device_id:
            found.append(intent)
    return found


def _maintenance_block(device_id: str, data_root: Path | str | None) -> None:
    """§4.9 执行顺序-7：任何未完成/损坏迁移意图均阻断输入。"""
    for intent in load_maintenance_intents(device_id, data_root):
        if intent.get("_corrupt"):
            raise InputSettingsError(
                "input_settings_invalid",
                "存在无法解析的输入设置迁移维护意图，按阻断处理；"
                "请用 tools/migrate_input_settings.py --resume 核对")
        if intent.get("state") != "committed":
            raise InputSettingsError(
                "input_settings_invalid",
                "存在未完成（prepared）的输入设置迁移维护意图：输入保持阻断，"
                "交由 tools/migrate_input_settings.py --resume 处理，不自动迁移/开启")


def known_device_by_maintenance(device_id: str, data_root: Path | str | None = None) -> bool:
    """文件系统判据：本设备存在迁移维护意图/备份 → 已知设备（非全新）。"""
    return bool(load_maintenance_intents(device_id, data_root))


# ---------- §4.8.3-5 拟提交视图（prepared 原响应构造窗口的读-己-写） ----------
# 仅线程内、显式 with 作用域生效；不落盘、不跨线程、退出即消失。
_proposed_tls = threading.local()


def _proposed_view_get(device_id: str) -> dict | None:
    stack = getattr(_proposed_tls, "views", None)
    if not stack:
        return None
    for view_device, stored in reversed(stack):
        if view_device == device_id:
            return stored
    return None


class proposed_view:
    """作用域内 load_settings/is_enabled 返回拟提交状态（原响应预构造用）。

    write_settings 在文件/DB 提交之前调用 observe 构造 original_response；
    观察器内部的资格计算（is_enabled）必须看到拟提交状态而不是磁盘旧值。
    """

    def __init__(self, device_id: str, stored: dict):
        self._device_id = device_id
        self._stored = stored

    def __enter__(self):
        stack = getattr(_proposed_tls, "views", None)
        if stack is None:
            stack = []
            _proposed_tls.views = stack
        stack.append((self._device_id, self._stored))
        return self

    def __exit__(self, exc_type, exc, tb):
        stack = getattr(_proposed_tls, "views", None)
        if stack:
            stack.pop()
        return False


def _validate_baseline(baseline: Any) -> None:
    """migration_baseline 结构与不变量校验（输入附件迁移契约）。"""
    if not isinstance(baseline, dict):
        raise ValueError("migration_baseline 必须为对象")
    if baseline.get("contract_version") != MIGRATION_BASELINE_VERSION:
        raise ValueError("migration_baseline contract_version 不受支持")
    for key in ("operator_id", "reason", "client_request_id", "migration_id",
                "backup_manifest_ref", "backup_manifest_sha256"):
        if not isinstance(baseline.get(key), str) or not baseline[key].strip():
            raise ValueError(f"migration_baseline 缺少 {key}")
    if not _is_digest(baseline.get("backup_manifest_sha256")):
        raise ValueError("backup_manifest_sha256 必须为 SHA-256 摘要")
    floor = baseline.get("revision_floor")
    if type(floor) is not int or floor < 1 or floor > MAX_REVISION:
        raise ValueError("revision_floor 必须为正安全整数")
    closed = baseline.get("closed_saved_value")
    if not isinstance(closed, dict) or closed.get("enabled") is not False:
        raise ValueError("closed_saved_value 必须为关闭状态")
    if type(closed.get("revision")) is not int or closed["revision"] < 1:
        raise ValueError("closed_saved_value.revision 必须为正整数")
    legacy_ids = baseline.get("legacy_request_ids")
    if not isinstance(legacy_ids, dict) or not legacy_ids:
        raise ValueError("legacy_request_ids 必须为非空字典")
    for key, digest in legacy_ids.items():
        if not isinstance(key, str) or not key or key != key.strip() or len(key) > 160:
            raise ValueError("legacy_request_ids 键必须为非空、无首尾空白且 ≤160 字符")
        if not _is_digest(digest):
            raise ValueError("legacy_request_ids 值必须为 SHA-256 摘要")


def _validate_stored(device_id: str, stored: Any) -> None:
    """新格式严格校验（FIX-04-2：旧档/损坏/回退一律拒绝，不默认兼容）。"""
    if not isinstance(stored, dict) or stored.get("contract_version") != CONTRACT_VERSION \
            or stored.get("device_id") != device_id:
        raise ValueError("shape mismatch")
    if not _valid_revision(stored.get("revision")):
        raise ValueError("revision invalid")
    # FIX-04：旧缺失撤销字段默认 0 的历史缺陷在此封死——字段缺失即拒绝，
    # 旧 E 时代档案（无撤销边界）必须经迁移工具处理，不能按边界 0 兼容执行。
    if "revoked_through_revision" not in stored:
        raise ValueError("revoked_through_revision 缺失：旧格式档案必须先经迁移工具处理")
    boundary = stored.get("revoked_through_revision")
    if not _valid_revision(boundary) or boundary > stored["revision"]:
        raise ValueError("revoked_through_revision invalid")
    if type(stored.get("enabled")) is not bool:
        raise ValueError("enabled invalid")
    ime = stored.get("ime_device_id")
    if stored["enabled"] and (not isinstance(ime, str) or not ime.strip()):
        raise ValueError("enabled=true 必须携带授权手机")
    requests = stored.get("requests")
    if not isinstance(requests, dict):
        raise ValueError("requests 必须为对象")
    for key, entry in requests.items():
        if not isinstance(key, str) or not key or key != key.strip() or len(key) > 160:
            raise ValueError("requests 键必须为非空、无首尾空白且 ≤160 字符")
        if not isinstance(entry, dict) or not _is_digest(entry.get("digest")):
            raise ValueError(f"requests[{key}] 缺少有效摘要")
    baseline = stored.get("migration_baseline")
    if baseline is not None:
        _validate_baseline(baseline)
        closed = baseline["closed_saved_value"]
        if stored["revision"] < closed["revision"] \
                or stored["revision"] < baseline["revision_floor"]:
            raise ValueError("文件版本低于迁移基线：疑似回退，按资料事故阻断")
        if boundary < closed.get("revoked_through_revision", 0):
            raise ValueError("撤销边界低于迁移基线：疑似回退，按资料事故阻断")


def load_settings(device_id: str, data_root: Path | str | None = None,
                  *, known_check: Callable[[], bool] | None = None) -> dict:
    """读取已保存选择；损坏/回退/未完成迁移=input_settings_invalid。

    FIX-04-1：严格区分「真正从未建立资料的新设备」（文件缺失→关闭/revision 0）
    与「已知设备文件丢失/回退」（known_check 或维护意图判真 → input_settings_invalid，
    绝不创建空白默认值覆盖历史）。
    """
    # §4.8.3：本线程存在拟提交视图（prepared 原响应构造窗口）时读-己-写。
    view = _proposed_view_get(device_id)
    if view is not None:
        return deepcopy(view)
    # §4.9：正常读取先查维护目录——未完成/损坏迁移意图阻断一切设置解释。
    _maintenance_block(device_id, data_root)
    path = settings_path(device_id, data_root)
    if not path.exists():
        if known_check is not None and known_check():
            raise InputSettingsError(
                "input_settings_invalid",
                f"设备 {device_id} 是已知输入设备，但设置文件丢失："
                "拒绝按全新设备重建空白默认值；请从备份核对恢复")
        return _blank(device_id)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        _validate_stored(device_id, stored)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InputSettingsError(
            "input_settings_invalid", f"输入开关设置文件损坏，按禁止输入处理：{exc}") from exc
    return stored


def is_enabled(device_id: str, data_root: Path | str | None = None,
               *, known_check: Callable[[], bool] | None = None) -> bool:
    """当前保存的选择（损坏/丢失（已知设备）/迁移阻断按禁止输入）。不查实时条件。"""
    try:
        return load_settings(device_id, data_root, known_check=known_check).get("enabled") is True
    except InputSettingsError:
        return False


def public_settings(stored: dict) -> dict:
    """对外可见的持久字段视图（不含 requests 账本与操作者资料）。"""
    return {key: deepcopy(stored.get(key)) for key in PUBLIC_FIELDS}


def _snapshot5(stored: dict) -> dict:
    return {key: deepcopy(stored.get(key)) for key in
            ("enabled", "ime_device_id", "revision", "revoked_through_revision", "updated_at")}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        # §4.7：替换后同步父目录，保证崩溃后提交可见（提交持久性）。
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _validate_request(enabled: Any, ime_device_id: Any, operator_id: Any,
                      reason: Any, client_request_id: Any,
                      expected_revision: Any) -> Any:
    """§4.2.1 字段校验；返回规范化 ime_device_id（关闭允许省略→None）。

    FIX-04：标识/操作者/原因首尾空白拒绝（协议按 Unicode 码点，不做折叠）。
    """
    if type(enabled) is not bool:
        raise InputSettingsError("invalid_request", "enabled 必须为布尔值")
    if enabled:
        if not isinstance(ime_device_id, str) or not ime_device_id.strip() \
                or len(ime_device_id.strip()) > 256:
            raise InputSettingsError("invalid_request",
                                     "开启必须提供非空 ime_device_id（≤256 字符）")
        ime_normalized = ime_device_id.strip()
    else:
        if ime_device_id is None:
            ime_normalized = None
        elif isinstance(ime_device_id, str) and ime_device_id.strip():
            if len(ime_device_id.strip()) > 256:
                raise InputSettingsError("invalid_request", "ime_device_id 过长（≤256 字符）")
            ime_normalized = ime_device_id.strip()
        else:
            ime_normalized = None
    for field, value, limit in (("operator_id", operator_id, 100),
                                ("reason", reason, 500),
                                ("client_request_id", client_request_id, 160)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise InputSettingsError(
                "invalid_request", f"{field} 必须为非空字符串（≤{limit}）")
        if value != value.strip():
            raise InputSettingsError(
                "invalid_request", f"{field} 首尾空白拒绝（不做空白折叠）")
    if type(expected_revision) is not int or expected_revision < 0 \
            or expected_revision > MAX_REVISION:
        raise InputSettingsError("invalid_request", "expected_revision 必须为非负安全整数")
    return ime_normalized


def _request_digest(device_id: str, enabled: bool, ime_device_id: str | None,
                    operator_id: str, reason: str, expected_revision: int,
                    correlation_id: str | None, call_context: dict | None) -> str:
    payload = json.dumps(
        {"contract_version": CONTRACT_VERSION, "device_id": device_id,
         "enabled": enabled, "ime_device_id": ime_device_id,
         "operator_id": operator_id, "reason": reason,
         "expected_revision": expected_revision,
         "correlation_id": correlation_id, "call_context": call_context},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def update_settings(device_id: str, *, data_root: Path | str | None = None,
                    enabled: Any = None, ime_device_id: Any = None,
                    expected_revision: Any = None, operator_id: Any = None,
                    reason: Any = None, client_request_id: Any = None,
                    calibration_ready: bool = True,
                    binding_state: str | None = None,
                    permission_denied: str | None = None,
                    correlation_id: str | None = None,
                    call_context: dict | None = None,
                    known_check: Callable[[], bool] | None = None,
                    observe: Callable[[str, dict], dict] | None = None) -> dict:
    """写入开关选择（§4.2）。

    - 权限先于幂等：permission_denied（wx/readonly 等现时无权）先拒绝，
      被撤权身份不能借重放获取历史结果。
    - calibration_ready/binding_state：仅对新请求评估（重放返回原快照，不再
      评估旧请求的开启条件）。开启（含已开启的显式新写入）要求机械校准有效
      且请求手机与唯一绑定匹配；关闭不受校准/绑定/离线限制。
      binding_state 取 "unavailable"/"mismatch" 时拒绝开启。
    - observe(device_id, stored)：调用方注入的完整状态观察器，用于构造原响应
      快照；未提供时用持久字段的简化视图。重放只返回保存的原快照。
    - 关闭写入推进 revoked_through_revision=新 revision；开启不降低该值。
    - known_check：已知设备判据（FIX-04-1）；已知设备文件丢失时拒绝写入，
      不重建空白默认。
    返回 {"settings": <原响应快照>, "replayed": bool}。
    """
    ime_normalized = _validate_request(enabled, ime_device_id, operator_id, reason,
                                       client_request_id, expected_revision)
    operator_id = operator_id.strip()
    reason = reason.strip()
    client_request_id = client_request_id.strip()
    with _lock_for(device_id):
        prepared = prepare_update(
            device_id, data_root=data_root, enabled=enabled,
            ime_device_id=ime_normalized, expected_revision=expected_revision,
            operator_id=operator_id, reason=reason,
            client_request_id=client_request_id,
            calibration_ready=calibration_ready, binding_state=binding_state,
            permission_denied=permission_denied, correlation_id=correlation_id,
            call_context=call_context, known_check=known_check)
        if prepared["kind"] == "replay":
            return {"settings": deepcopy(prepared["settings"]), "replayed": True}
        commit_update(device_id, prepared, data_root=data_root)
        stored = prepared["stored"]
        entry = stored["requests"][client_request_id]
        # 观察器读已提交文件（资格快照必须反映本次写入）；观察失败退回简化
        # 视图，不回滚已提交写入；原响应随账本二次原子落盘。
        if observe is not None:
            try:
                original_response = observe(device_id, stored)
            except Exception:
                original_response = None
        else:
            original_response = None
        if original_response is None:
            original_response = {**public_settings(stored), "effective": enabled}
        entry["original_response"] = deepcopy(original_response)
        _atomic_write(settings_path(device_id, data_root), stored)
        return {"settings": deepcopy(original_response), "replayed": False}


def file_digest(path: Path | str | None) -> str | None:
    """设置文件当前字节 SHA-256；不存在返回 None（首次安装哨兵见 §4.8.2）。"""
    path = Path(path) if path is not None else None
    if path is None or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_update(device_id: str, *, data_root: Path | str | None = None,
                   enabled: Any = None, ime_device_id: Any = None,
                   expected_revision: Any = None, operator_id: Any = None,
                   reason: Any = None, client_request_id: Any = None,
                   calibration_ready: bool = True,
                   binding_state: str | None = None,
                   permission_denied: str | None = None,
                   correlation_id: str | None = None,
                   call_context: dict | None = None,
                   known_check: Callable[[], bool] | None = None) -> dict:
    """§4.8.3-1：短锁内完成权限/幂等/CAS/开启条件检查并构造完整新文件。

    不落盘——落盘由 commit_update 或 InputControl 的意图编排负责。
    返回 {"kind":"replay","settings":...} 或
    {"kind":"prepared","stored":...,"original_response":...,
     "before_file_digest":...,"enabled":bool}。

    FIX-04：known_check 注入已知设备判据（文件丢失拒绝重建空白默认）；
    migration_baseline 的旧编号 tombstone 与维护编号在幂等检查后、CAS 前
    按 409 语义拒绝（legacy_request_replay_unavailable / request_conflict）。
    """
    ime_normalized = _validate_request(enabled, ime_device_id, operator_id, reason,
                                       client_request_id, expected_revision)
    operator_id = operator_id.strip()
    reason = reason.strip()
    client_request_id = client_request_id.strip()
    stored = load_settings(device_id, data_root, known_check=known_check)
    if permission_denied:
        raise InputSettingsError("permission_denied", permission_denied)
    request_digest = _request_digest(
        device_id, enabled, ime_normalized, operator_id, reason,
        expected_revision, correlation_id, call_context)
    # §4.9/附件：合法 PUT 顺序=权限（上方）→ 旧编号 tombstone → 维护编号 →
    # 新版幂等/CAS。迁移后旧请求编号只保留 tombstone——同旧编号无论内容
    # 如何都不重执；维护编号不得被普通 PUT 复用。
    baseline = stored.get("migration_baseline")
    if isinstance(baseline, dict):
        legacy_ids = baseline.get("legacy_request_ids") or {}
        if client_request_id in legacy_ids:
            raise InputSettingsError(
                "legacy_request_replay_unavailable",
                "该请求编号属于迁移保留的旧请求 tombstone：旧编号不重执，"
                "也不返回当前值伪装原响应；请使用新编号")
        if client_request_id in (baseline.get("client_request_id"),
                                 baseline.get("migration_id")):
            raise InputSettingsError(
                "request_conflict",
                "该请求编号属于迁移维护请求/迁移编号：不得复用为普通设置写入")
    replay = (stored.get("requests") or {}).get(client_request_id)
    if replay:
        if replay.get("digest") != request_digest:
            raise InputSettingsError(
                "request_conflict",
                "同 client_request_id 但内容不同：疑似重放/冲突，拒绝执行")
        original = replay.get("original_response")
        if original is None:
            original = {**public_settings(stored), "effective": enabled}
        return {"kind": "replay", "settings": deepcopy(original)}
    current_revision = stored.get("revision") or 0
    if current_revision >= MAX_REVISION or (enabled and current_revision + 1 >= MAX_REVISION):
        raise InputSettingsError(
            "input_revision_exhausted",
            "输入开关设置版本已达上限：请维护处理，禁止重置计数器")
    if expected_revision != current_revision:
        raise InputSettingsError("revision_conflict",
                                 f"输入开关设置版本已变化（当前 {current_revision}），请刷新后重试")
    if enabled and not calibration_ready:
        raise InputSettingsError("calibration_not_ready",
                                 "机械校准未就绪：不能开启输入与发送（关闭不受限）")
    if enabled and binding_state == "unavailable":
        raise InputSettingsError("input_binding_unavailable",
                                 "尚未确定唯一输入法手机：不能开启")
    if enabled and binding_state == "mismatch":
        raise InputSettingsError("input_binding_mismatch",
                                 "请求手机与服务端唯一绑定不一致：不能开启")
    before = _snapshot5(stored)
    stored["enabled"] = enabled
    stored["revision"] = current_revision + 1
    # §4.1：开启写入请求手机；关闭保留原授权手机（请求携带的手机只进
    # 审计/意图记录，不覆盖保存值）。
    if enabled:
        stored["ime_device_id"] = ime_normalized
    stored["updated_at"] = _now_iso()
    stored["operator_id"] = operator_id
    stored["reason"] = reason
    stored["client_request_id"] = client_request_id
    if not enabled:
        # §4.5.2：关闭推进撤销边界；开启/保持开启不降低。
        stored["revoked_through_revision"] = stored["revision"]
    entry = {
        "enabled": enabled, "revision": stored["revision"],
        "operator_id": operator_id, "at": stored["updated_at"],
        "digest": request_digest,
        "before": before, "after": _snapshot5(stored),
    }
    stored.setdefault("requests", {})[client_request_id] = entry
    return {"kind": "prepared", "stored": stored,
            "original_response": None,
            "before_file_digest": file_digest(settings_path(device_id, data_root)),
            "enabled": enabled}


def commit_update(device_id: str, prepared: dict, *,
                  data_root: Path | str | None = None) -> dict:
    """§4.8.3-2：原子落盘（文件 fsync + 父目录 fsync）。返回原响应。"""
    if prepared.get("kind") != "prepared":
        raise InputSettingsError("invalid_request", "commit 只接受 prepared 结果")
    _atomic_write(settings_path(device_id, data_root), prepared["stored"])
    return {"settings": deepcopy(prepared["original_response"]), "replayed": False}
