"""Device-bound saved screen geometry. Persistence never executes a gesture."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

from drivers import screen_gestures as sg

CONTRACT_VERSION = "saved-screen-gesture.v1"
ARCHIVE_FILENAME = "saved_screen_gestures.json"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_LOCK = threading.RLock()
_ENTRY_KEYS = {
    "contract_version", "device_id", "gesture_id", "version", "label", "action",
    "binding", "payload", "effect_status", "effect_status_display",
    "created_by", "created_at", "updated_by", "updated_at",
}


class SavedGestureError(ValueError):
    """Stable code/status for the debug writer and the read-only public API."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        self.code = code
        self.http_status = http_status
        super().__init__(message)


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SavedGestureError("saved_gesture_invalid", f"{label}须为 1~64 位字母、数字、下划线或连字符")
    return value


def _text(value, label, maximum=100):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SavedGestureError("saved_gesture_invalid", f"{label}须为 1~{maximum} 字的非空文本")
    return value


def _point(value):
    if (not isinstance(value, dict) or set(value) != {"x", "y"}
            or any(type(value[key]) not in (int, float) or not math.isfinite(value[key])
                   or not 0 <= value[key] <= 1 for key in ("x", "y"))):
        raise SavedGestureError("saved_gesture_invalid", "手势点仅接受 0~1 的屏幕 x、y，禁止相机或机械坐标")


def _interval(value):
    if type(value) is not int or not 50 <= value <= 500 or value % 10:
        raise SavedGestureError("saved_gesture_invalid", "双击间隔须为 50~500 毫秒、步长 10 的整数")
    return value


def _binding(value, device):
    if not isinstance(value, dict) or set(value) != set(sg.BINDING_KEYS):
        raise ValueError("完整屏幕绑定缺失")
    if value["device_id"] != device:
        raise ValueError("屏幕绑定设备不符")
    for key in ("device_id", "daemon_session", "camera_id", "camera_device_unique_id"):
        _text(value[key], key, 256)
    if (not isinstance(value["calibration_fingerprint"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["calibration_fingerprint"])):
        raise ValueError("完整校准摘要无效")
    for key in ("binding_revision", "frame_width", "frame_height"):
        if type(value[key]) is not int or value[key] < (0 if key == "binding_revision" else 1):
            raise ValueError("屏幕绑定尺寸或版本无效")


def _validate_entry(entry, device, identifier):
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        raise ValueError("条目字段不完整")
    if (entry["contract_version"] != CONTRACT_VERSION or entry["device_id"] != device
            or entry["gesture_id"] != identifier
            or type(entry["version"]) is not int or entry["version"] < 1):
        raise ValueError("条目身份或版本不符")
    _identifier(identifier, "手势编号")
    for key in ("label", "created_by", "updated_by"):
        _text(entry[key], key)
    for key in ("created_at", "updated_at"):
        stamp = datetime.fromisoformat(entry[key])
        if stamp.tzinfo is None:
            raise ValueError("条目时间缺少时区")
    _binding(entry["binding"], device)
    if entry["effect_status"] != "unverified" or entry["effect_status_display"] != "手机效果未核验":
        raise ValueError("保存不能确认手机效果")
    payload = entry["payload"]
    action = entry["action"]
    fields = ({"point", "interval_ms"} if action == "double_tap" else
              {"start", "end", "duration_ms"} if action == "swipe" else set())
    if (not fields or not isinstance(payload, dict) or set(payload) != {"action", "binding", *fields}
            or payload["action"] != action):
        raise ValueError("手势载荷字段不符")
    _binding(payload["binding"], device)
    if payload["binding"] != entry["binding"]:
        raise ValueError("手势载荷绑定不符")
    for key in (("point",) if action == "double_tap" else ("start", "end")):
        _point(payload[key])
    if action == "double_tap":
        _interval(payload["interval_ms"])
    elif type(payload["duration_ms"]) is not int or not 150 <= payload["duration_ms"] <= 5000:
        raise ValueError("滑动时长无效")


def _path(device, data_root):
    _identifier(device, "设备编号")
    root = Path(data_root) if data_root is not None else Path(__file__).resolve().parents[1] / "data"
    path = root / "devices" / device / ARCHIVE_FILENAME
    # Reject redirected device directories and files, including dangling symlinks.
    for item in (root / "devices", path.parent, path, path.with_suffix(".lock")):
        if item.is_symlink():
            raise SavedGestureError("saved_gesture_archive_invalid", "手势档案路径不可为符号链接", 409)
    return path


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("JSON 字段重复")
        out[key] = value
    return out


def _reject_constant(value):
    raise ValueError(f"JSON 非有限数值：{value}")


def _load(device, data_root):
    path = _path(device, data_root)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"schema_version": 1, "device_id": device, "entries": {}}
    except OSError as exc:
        raise SavedGestureError("saved_gesture_archive_invalid", "手势档案无法读取，禁止覆盖和调用", 409) from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("档案不是普通文件")
            archive = json.load(handle, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if (not isinstance(archive, dict) or set(archive) != {"schema_version", "device_id", "entries"}
                or type(archive["schema_version"]) is not int or archive["schema_version"] != 1
                or archive["device_id"] != device or not isinstance(archive["entries"], dict)):
            raise ValueError("档案结构或设备身份不符")
        for identifier, entry in archive["entries"].items():
            _validate_entry(entry, device, identifier)
        return archive
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise SavedGestureError(
            "saved_gesture_archive_invalid", "手势档案损坏或身份不匹配，禁止覆盖和调用", 409) from exc


@contextmanager
def _edit(device, data_root):
    path = _path(device, data_root)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            archive = _load(device, data_root)
            yield archive
            fd, temporary = tempfile.mkstemp(prefix=".saved-screen-gestures-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(archive, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                _path(device, data_root)
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def _plan(device, action, points, bundle):
    try:
        if not isinstance(bundle, dict) or bundle.get("deviceId") != device:
            raise ValueError("校准档案与请求设备不符")
        result = sg.plan_gesture(action, points, bundle)
        _binding(result["binding"], device)
        return result
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise SavedGestureError("saved_gesture_calibration_invalid", str(exc), 409) from exc


def _view(entry, bundle, session):
    reasons = []
    if not session or session != entry["binding"]["daemon_session"]:
        reasons.append("当前机械臂会话未确认或与保存时不符")
    try:
        sg.require_binding(entry["binding"], bundle)
        keys = ("point",) if entry["action"] == "double_tap" else ("start", "end")
        plan = _plan(entry["device_id"], entry["action"],
                     {key: entry["payload"][key] for key in keys}, bundle)
        if entry["action"] == "swipe" and plan["durationMs"] != entry["payload"]["duration_ms"]:
            reasons.append("保存时长与当前计算不符，请重新登记")
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        reasons.append(str(exc))
    return {**deepcopy(entry), "usable": not reasons, "blocking_reasons": list(dict.fromkeys(reasons))}


def list_gestures(device: str, bundle: dict | None, session: str | None,
                  data_root: Path | None = None) -> list[dict]:
    """Read saved geometry without creating files, querying hardware or taking leases."""
    archive = _load(device, data_root)
    return [_view(entry, bundle, session) for _, entry in sorted(archive["entries"].items())]


def get_gesture(device: str, gesture_id: str, bundle: dict | None, session: str | None,
                data_root: Path | None = None) -> dict:
    _identifier(gesture_id, "手势编号")
    entry = _load(device, data_root)["entries"].get(gesture_id)
    if entry is None:
        raise SavedGestureError("saved_gesture_not_found", "此设备尚未保存该手势", 404)
    return _view(entry, bundle, session)


def save_gesture(device: str, body: dict, bundle: dict,
                 data_root: Path | None = None) -> dict:
    """Save normalized geometry; same-id updates require version and unchanged binding."""
    _identifier(device, "设备编号")
    if not isinstance(body, dict):
        raise SavedGestureError("saved_gesture_invalid", "保存手势参数必须是对象")
    body = deepcopy(body)
    action = body.get("action")
    keys = ("point",) if action == "double_tap" else ("start", "end") if action == "swipe" else ()
    required = {"label", "action", "operator", *keys}
    if action == "double_tap":
        required.add("interval_ms")
    if not keys or set(body) - (required | {"gesture_id", "expected_version"}) or not required <= set(body):
        raise SavedGestureError("saved_gesture_invalid", "手势字段不完整或含未声明参数，禁止覆盖机械参数或滑动时长")
    label = _text(body["label"], "手势名称")
    operator = _text(body["operator"], "操作人")
    for key in keys:
        _point(body[key])
    if action == "double_tap":
        _interval(body["interval_ms"])
    identifier = _identifier(body["gesture_id"], "手势编号") if "gesture_id" in body else None
    expected = body.get("expected_version")
    if "expected_version" in body and (identifier is None or type(expected) is not int or expected < 1):
        raise SavedGestureError("saved_gesture_invalid", "更新必须提供手势编号和正整数 expected_version")
    plan = _plan(device, action, {key: body[key] for key in keys}, bundle)
    payload = {"action": action, "binding": deepcopy(plan["binding"]),
               **{key: deepcopy(plan[key]) for key in keys}}
    payload.update({"interval_ms": body["interval_ms"]} if action == "double_tap" else
                   {"duration_ms": plan["durationMs"]})
    with _edit(device, data_root) as archive:
        if identifier is None:
            identifier = "gesture-" + uuid.uuid4().hex
            while identifier in archive["entries"]:
                identifier = "gesture-" + uuid.uuid4().hex
        previous = archive["entries"].get(identifier)
        if previous is None and expected is not None:
            raise SavedGestureError("saved_gesture_not_found", "更新的手势不存在，未创建新条目", 404)
        if previous is not None:
            if expected != previous["version"]:
                raise SavedGestureError("saved_gesture_version_conflict", "手势版本已变化，请读取后重新确认", 409)
            if previous["binding"] != plan["binding"]:
                raise SavedGestureError("saved_gesture_binding_changed", "校准绑定已变化，禁止重绑旧手势；请重新标记并另存", 409)
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "contract_version": CONTRACT_VERSION, "device_id": device, "gesture_id": identifier,
            "version": previous["version"] + 1 if previous else 1, "label": label, "action": action,
            "binding": deepcopy(plan["binding"]), "payload": payload,
            "effect_status": "unverified", "effect_status_display": "手机效果未核验",
            "created_by": previous["created_by"] if previous else operator,
            "created_at": previous["created_at"] if previous else now,
            "updated_by": operator, "updated_at": now,
        }
        _validate_entry(entry, device, identifier)
        archive["entries"][identifier] = entry
    return _view(entry, bundle, bundle.get("daemonSession"))
