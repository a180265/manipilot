"""相机绑定 v2（2026-10-03 规格 camera-binding 附件 §4/§4.1，B01）。

绑定文件 binding_file：schemaVersion=2、revision、bindings[]（含 cameraBindingId；
无绑定通道从数组删除）。写入走 prepared 意图 → 原子替换 → committed 原响应；
崩溃重启按磁盘摘要对账（=after 补完成 / =before aborted / 其他 unknown 保持阻断）。
同机 helper 身份：CAMERA_CONTROL_TOKEN_FILE（0600 32 字节 hex）+ Bearer + 仅
loopback + 无 Origin + 常量时间比较；缺失/损坏令牌拒绝服务能力，不自动生成。
维护占用：data_root/installation_locks/{device_id}.lock（动作共享、绑定写排他）。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import threading
import uuid
from pathlib import Path

CONTRACT = "camera-binding.v2"

REQUEST_ACTIONS = ("bind", "renew", "unbind", "rename")
# §4.8.7 同款保护语义（camera 侧）：这些意图状态阻断新来源签发
BLOCKING_STATES = ("prepared", "unknown")

_config_lock = threading.Lock()


class CameraBindingError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ---- 摘要 ----

def file_sha256(path: Path) -> str | None:
    path = Path(path)
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parsed_sha256(raw_bytes: bytes | None) -> str | None:
    """严格解析后的规范化 JSON 摘要（§4.1：与原字节摘要分用，不得混用）。"""
    if raw_bytes is None:
        return None
    try:
        value = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return canonical_digest(value)


def canonical_digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---- 读取（严格；坏档不变空） ----

def load_bindings_v2(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {"schemaVersion": 2, "revision": 0, "bindings": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        raise CameraBindingError(
            "camera_archive_invalid", f"绑定文件损坏：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 2:
        raise CameraBindingError(
            "camera_archive_invalid",
            f"绑定文件版本不受支持：{payload.get('schemaVersion')}")
    entries = payload.get("bindings")
    if not isinstance(entries, list):
        raise CameraBindingError("camera_archive_invalid", "bindings 必须为数组")
    seen_channels: set[str] = set()
    seen_physical: set[str] = set()
    seen_instances: set[str] = set()
    clean: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CameraBindingError("camera_archive_invalid", "binding 项必须为对象")
        unique_id = str(entry.get("deviceUniqueId") or "").strip()
        if not unique_id:
            continue  # 无绑定通道：从数组删除，不用空物理 ID 占位
        channel = str(entry.get("cameraId") or "").strip()
        binding_id = str(entry.get("cameraBindingId") or "").strip()
        if not channel:
            raise CameraBindingError("camera_archive_invalid", "binding 缺少 cameraId")
        if channel in seen_channels:
            raise CameraBindingError("camera_archive_invalid",
                                     f"重复逻辑相机通道：{channel}")
        if unique_id in seen_physical:
            raise CameraBindingError("camera_archive_invalid",
                                     f"重复物理相机：{unique_id}")
        if not binding_id or binding_id in seen_instances:
            raise CameraBindingError("camera_archive_invalid",
                                     "binding 缺少 cameraBindingId 或实例重复")
        seen_channels.add(channel)
        seen_physical.add(unique_id)
        seen_instances.add(binding_id)
        clean.append({**entry, "deviceUniqueId": unique_id,
                      "cameraId": channel, "cameraBindingId": binding_id})
    return {"schemaVersion": 2, "revision": int(payload.get("revision") or 0),
            "bindings": clean}


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def _operations_dir(bindings_path: Path) -> Path:
    return bindings_path.parent / "binding_operations"


def operation_path(bindings_path: Path, request_id: str) -> Path:
    return _operations_dir(bindings_path) / f"{request_id}.json"


def load_operation(bindings_path: Path, request_id: str) -> dict | None:
    path = operation_path(bindings_path, request_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"request_id": request_id, "state": "unknown",
                "error": "operation record unreadable"}


def reconcile_operations(bindings_path: Path) -> list[dict]:
    """启动对账（§4.1）：prepared 意图按磁盘摘要落定；unknown 保持阻断。"""
    bindings_path = Path(bindings_path)
    results = []
    ops_dir = _operations_dir(bindings_path)
    if not ops_dir.exists():
        return results
    for path in sorted(ops_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if record.get("state") not in ("prepared", "unknown"):
            continue
        disk_raw = bindings_path.read_bytes() if bindings_path.exists() else None
        disk_digest = parsed_sha256(disk_raw)
        if disk_digest == record.get("after_sha256"):
            record["state"] = "committed"
            _atomic_write_json(path, record)
        elif disk_digest == record.get("before_sha256"):
            record["state"] = "aborted"
            _atomic_write_json(path, record)
        else:
            record["state"] = "unknown"  # 保持阻断，人工核对
            _atomic_write_json(path, record)
        results.append({"request_id": record.get("request_id"),
                        "state": record["state"]})
    return results


def blocking_intents(bindings_path: Path) -> list[dict]:
    """prepared/unknown 意图存在即阻断新来源签发与可运动上下文（§4.1）。"""
    bindings_path = Path(bindings_path)
    blocking = []
    ops_dir = _operations_dir(bindings_path)
    if not ops_dir.exists():
        return blocking
    for path in sorted(ops_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if record.get("state") in BLOCKING_STATES:
            blocking.append({"request_id": record.get("request_id"),
                             "state": record["state"]})
    return blocking


def save_binding_v2(bindings_path: Path, request: dict) -> dict:
    """绑定写入：幂等（同编号同摘要=原响应）→ prepared → 原子替换 → committed。

    §4.1：通道存在 prepared/unknown 绑定意图时阻断新写（camera_commit_unknown），
    须先按磁盘摘要对账；同编号自身意图的重放仍走原幂等/冲突分支。
    """
    bindings_path = Path(bindings_path)
    request_id = str(request.get("request_id") or "").strip()
    action = str(request.get("action") or "").strip()
    if not request_id or action not in REQUEST_ACTIONS:
        raise CameraBindingError("camera_request_invalid",
                                 "request_id/action 缺失或不受支持")
    blocking = [item for item in blocking_intents(bindings_path)
                if item.get("request_id") != request_id]
    if blocking:
        raise CameraBindingError(
            "camera_commit_unknown",
            f"存在未对账绑定意图 {blocking[0]['request_id']}"
            f"（{blocking[0]['state']}）：先查原 request_id 对账，禁止新写")
    op_path = operation_path(bindings_path, request_id)
    existing = load_operation(bindings_path, request_id)
    request_digest = canonical_digest({"request": request, "path": str(bindings_path)})
    if existing is not None:
        if existing.get("request_digest") != request_digest:
            raise CameraBindingError(
                "request_conflict", "同 request_id 携带不同内容：疑似重放/冲突")
        if existing.get("state") == "committed":
            return existing  # 原响应
        raise CameraBindingError(
            "request_conflict", f"原请求仍处于 {existing.get('state')} 状态")

    before_payload = load_bindings_v2(bindings_path)
    before_raw = bindings_path.read_bytes() if bindings_path.exists() else None
    before_digest = parsed_sha256(before_raw)
    channel = str(request.get("camera_id") or "").strip()
    history_ids = _historical_instance_ids(bindings_path)

    after_payload = {"schemaVersion": 2,
                     "revision": int(before_payload["revision"]) + 1,
                     "bindings": [dict(b) for b in before_payload["bindings"]]}
    entries = after_payload["bindings"]
    current = next((b for b in entries if b.get("cameraId") == channel), None)
    if action == "unbind":
        if current is None:
            raise CameraBindingError("camera_not_found", "通道当前无绑定")
        entries[:] = [b for b in entries if b.get("cameraId") != channel]
    elif action in ("bind", "renew"):
        unique_id = str(request.get("device_unique_id") or "").strip()
        if not unique_id:
            raise CameraBindingError("camera_request_invalid",
                                     "bind/renew 需要 device_unique_id")
        physical_owner = next((b for b in entries
                               if b.get("deviceUniqueId") == unique_id
                               and b.get("cameraId") != channel), None)
        if physical_owner is not None:
            raise CameraBindingError(
                "camera_unique_id_conflict", f"物理相机已绑定通道 {physical_owner['cameraId']}")
        if current is None and action == "renew":
            raise CameraBindingError("camera_not_found", "通道当前无绑定，不能 renew")
        # R2-18：绑定/代际 id 用 schema uuid 形（冻结契约 id 定义）
        new_id = str(uuid.uuid4())
        while new_id in history_ids:
            new_id = str(uuid.uuid4())
        entry = {**(current or {}),
                 "cameraId": channel, "deviceUniqueId": unique_id,
                 "cameraBindingId": new_id,
                 "boundAt": request.get("bound_at"),
                 "boundBy": str(request.get("bound_by") or "")}
        if current is None:
            entries.append(entry)
        else:
            entries[entries.index(current)] = entry
    elif action == "rename":
        if current is None:
            raise CameraBindingError("camera_not_found", "通道当前无绑定")
        # 同身份同物理且无 renew 意图：仅元数据更新，不换 UUID
        display_name = str(request.get("display_name") or "").strip()
        if display_name:
            entries[entries.index(current)] = {
                **current, "displayName": display_name}
        else:
            after_payload["revision"] = before_payload["revision"]  # no-op

    intent = {
        "request_id": request_id, "kind": "binding_write",
        "action": action, "camera_id": channel,
        "principal_id": str(request.get("principal_id") or ""),
        "state": "prepared",
        "before_sha256": before_digest, "after_sha256": None,
        "before_revision": before_payload["revision"],
        "after_revision": after_payload["revision"],
        "request_digest": request_digest,
        "created_at": _now(),
    }
    _atomic_write_json(op_path, intent)  # prepared fsync：存在即阻断新来源

    try:
        after_bytes = json.dumps(after_payload, ensure_ascii=False,
                                 indent=2).encode("utf-8")
        intent["after_sha256"] = parsed_sha256(after_bytes)
        _atomic_write_json(op_path, intent)
        _atomic_write_json(bindings_path, after_payload)
    except OSError as exc:
        intent["state"] = "aborted"
        intent["failure"] = str(exc)
        _atomic_write_json(op_path, intent)
        raise CameraBindingError("camera_storage_failed",
                                 f"绑定文件写入失败（未提交）：{exc}") from exc

    intent["state"] = "committed"
    new_generation = (str(uuid.uuid4()) if action in ("bind", "renew") else None)
    intent["response"] = {
        "contract": CONTRACT, "request_id": request_id, "action": action,
        "camera_id": channel, "revision": after_payload["revision"],
        "cameraBindingId": (next((b.get("cameraBindingId") for b in
                                  after_payload["bindings"]
                                  if b.get("cameraId") == channel), None)
                            if action != "unbind" else None),
        "sourceGenerationId": new_generation,
    }
    _atomic_write_json(op_path, intent)
    return intent


def _historical_instance_ids(bindings_path: Path) -> set[str]:
    ids: set[str] = set()
    ops_dir = _operations_dir(bindings_path)
    for path in sorted(ops_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        response = record.get("response") or {}
        if response.get("cameraBindingId"):
            ids.add(str(response["cameraBindingId"]))
        for snapshot in (record.get("proposed_after"), record.get("before_bindings")) or []:
            pass
    try:
        for entry in load_bindings_v2(bindings_path).get("bindings", []):
            if entry.get("cameraBindingId"):
                ids.add(str(entry["cameraBindingId"]))
    except CameraBindingError:
        pass
    return ids


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---- 同机 helper 身份（§4） ----

def load_control_token(token_file: Path) -> str:
    path = Path(token_file)
    if not path.exists():
        raise CameraBindingError(
            "camera_permission_denied",
            f"控制令牌文件缺失：{path}（不自动生成替换）")
    if path.is_symlink() or not path.is_file():
        raise CameraBindingError("camera_permission_denied",
                                 "控制令牌文件必须是常规文件（拒绝符号链接）")
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise CameraBindingError("camera_permission_denied",
                                 f"控制令牌文件权限必须为 0600（当前 {oct(mode)}）")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise CameraBindingError("camera_permission_denied",
                                 "控制令牌必须是 64 位小写 hex")
    return token


def generate_control_token(token_file: Path) -> str:
    """首次显式安装生成（本机安装脚本调用；服务端不自动生成替换）。"""
    token_file = Path(token_file)
    token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(32)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
    return token


def verify_helper_request(handler, token_file: Path) -> None:
    """Bearer + 仅 loopback + 无 Origin + 常量时间比较（§4）。"""
    token = load_control_token(token_file)
    client = handler.client_address[0] if handler.client_address else ""
    if client not in ("127.0.0.1", "::1"):
        raise CameraBindingError("camera_permission_denied", "仅接受本机回环请求")
    if handler.headers.get("Origin"):
        raise CameraBindingError("camera_permission_denied",
                                 "不接受带 Origin 的浏览器请求")
    auth = handler.headers.get("Authorization") or ""
    expected = f"Bearer {token}"
    if not secrets.compare_digest(auth, expected):
        raise CameraBindingError("camera_permission_denied",
                                 "Authorization 无效")


# ---- 维护占用锁（§4.1：动作共享、绑定写排他；文件不删除不替换） ----

def _lock_path(data_root: Path, device_id: str) -> Path:
    return Path(data_root) / "installation_locks" / f"{device_id}.lock"


@contextlib.contextmanager
def installation_lock(data_root: Path, device_id: str, *, exclusive: bool):
    path = _lock_path(data_root, device_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
        except OSError as exc:
            raise CameraBindingError(
                "device_maintenance_busy",
                "设备维护占用中（绑定写或动作进行）") from exc
        yield handle
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
