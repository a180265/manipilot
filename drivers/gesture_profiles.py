"""Versioned, device-bound gesture drafts. Only the debug console writes them."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

from drivers import calibration_bundle as cb, ui_targets
from drivers.arm import gesture_motion_confirmed

_LOCK = threading.RLock()
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_STATES = {"draft": "草稿", "testing": "测试进行中", "tested": "已测试待确认",
           "enabled": "已启用", "disabled": "已停用"}
_FIELDS = {"max_swipes", "top_max_swipes", "swipe_duration_ms", "settle_ms",
           "return_to_top", "search_gesture", "top_gesture", "top_anchor_text",
           "top_anchor_region", "list_region"}


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label}必须为 1~64 位字母、数字、下划线或连字符")
    return value


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}必须是有限数字")
    return value


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}必须为 {low}~{high} 的整数")
    return value


def _geometry(value, label, *, rectangle=False):
    keys = ("x1", "y1", "x2", "y2") if rectangle else ("cameraX", "cameraY")
    if not isinstance(value, dict) or set(value) != {*keys, "frameW", "frameH"}:
        raise ValueError(f"{label}需要画面坐标和 frameW/frameH，不能使用机械坐标")
    result = {key: _number(value[key], f"{label}.{key}") for key in keys}
    w = result["frameW"] = _integer(value["frameW"], "画面宽度", 1, 32768)
    h = result["frameH"] = _integer(value["frameH"], "画面高度", 1, 32768)
    if rectangle:
        valid = 0 <= result["x1"] < result["x2"] <= w and 0 <= result["y1"] < result["y2"] <= h
    else:
        valid = 0 <= result["cameraX"] < w and 0 <= result["cameraY"] < h
    if not valid:
        raise ValueError(f"{label}必须完全位于画面内，区域不能为空")
    return result


def validate_navigation(config: dict) -> dict:
    """Normalize legacy inline navigation too; profiles additionally require a list ROI."""
    if not isinstance(config, dict) or set(config) - _FIELDS:
        raise ValueError("导航配置必须是对象，且不能包含未声明字段")
    if not config:
        return {}
    out = {}
    for key, default, low, high in (
        ("max_swipes", 5, 0, 10), ("top_max_swipes", 5, 0, 10),
        ("swipe_duration_ms", 700, 150, 5000), ("settle_ms", 600, 100, 2000),
    ):
        out[key] = _integer(config.get(key, default), key, low, high)
    out["return_to_top"] = config.get("return_to_top", True)
    if type(out["return_to_top"]) is not bool:
        raise ValueError("return_to_top 必须为布尔值")
    geometries = []
    for key in ("search_gesture", "top_gesture"):
        required = out["max_swipes"] > 0 if key == "search_gesture" else (
            out["return_to_top"] and out["top_max_swipes"] > 0)
        if key not in config:
            if required:
                raise ValueError(f"缺少 {key} 的人工标记")
            continue
        gesture = config[key]
        if not isinstance(gesture, dict) or set(gesture) != {"start", "end"}:
            raise ValueError(f"{key} 必须包含 start/end")
        out[key] = {point: _geometry(gesture[point], f"{key}.{point}")
                    for point in ("start", "end")}
        start, end = out[key]["start"], out[key]["end"]
        if (start["cameraX"], start["cameraY"]) == (end["cameraX"], end["cameraY"]):
            raise ValueError("滑动起点与终点不能相同")
        geometries.extend(out[key].values())
    for key in ("list_region", "top_anchor_region"):
        if key in config:
            out[key] = _geometry(config[key], key, rectangle=True)
            geometries.append(out[key])
    if len({(p["frameW"], p["frameH"]) for p in geometries}) > 1:
        raise ValueError("所有标记必须来自同一画面尺寸")
    text = config.get("top_anchor_text", "")
    if not isinstance(text, str) or len(text) > 200:
        raise ValueError("顶部标志文字必须为不超过 200 字的文本")
    out["top_anchor_text"] = text.strip()
    if out["return_to_top"]:
        if not out["top_anchor_text"] or "top_anchor_region" not in out:
            raise ValueError("回顶需要顶部标志文字及人工标记的顶部验证区域")
        if re.fullmatch(r"(微信|wechat)(?:[（(]\d+[）)])?", re.sub(r"\s+", "", text).lower()):
            raise ValueError("微信/WeChat 常驻标题不能证明列表已到顶部")
    region = out.get("list_region")
    if region:
        for key in ("search_gesture", "top_gesture"):
            for point in out.get(key, {}).values():
                if not (region["x1"] < point["cameraX"] < region["x2"]
                        and region["y1"] < point["cameraY"] < region["y2"]):
                    raise ValueError("滑动起终点必须位于列表安全区域内部")
    return out


def _path(device_id, data_root=None):
    _identifier(device_id, "设备编号")
    root = Path(data_root) if data_root is not None else Path(__file__).resolve().parents[1] / "data"
    return root / "devices" / device_id / "gesture_profiles.json"


def _load(device_id, data_root):
    path = _path(device_id, data_root)
    if not path.exists():
        return {"schema_version": 1, "device_id": device_id, "entries": {}, "audit": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or data.get("device_id") != device_id or not isinstance(data.get("entries"), dict)
                or not isinstance(data.get("audit"), list)):
            raise ValueError()
        return data
    except (OSError, ValueError) as exc:
        raise ValueError("手势档案损坏或设备身份不匹配，禁止覆盖和调用") from exc


@contextmanager
def _edit(device_id, data_root):
    path = _path(device_id, data_root)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            archive = _load(device_id, data_root)
            yield archive
            fd, tmp = tempfile.mkstemp(prefix=".gesture_profiles-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(archive, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)


def _audit(archive, entry, action, operator):
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("必须填写操作人")
    archive["audit"].append({"at": cb.now_iso(), "profile_id": entry["profile_id"],
                             "version": entry["version"], "revision": entry["revision"],
                             "action": action, "operator": operator.strip()})


def _binding_reasons(entry, bundle, current_session):
    reasons = ui_targets.binding_mismatches(entry, bundle or {})
    if not bundle or bundle.get("status") != "ready":
        reasons.append("校准档案尚未 ready")
    if not current_session or current_session != (bundle or {}).get("daemonSession"):
        reasons.append("当前机械臂会话未连接或与校准会话不符")
    if entry.get("device_id") != (bundle or {}).get("deviceId"):
        reasons.append("校准设备编号不匹配")
    if entry.get("zHash") != cb.profile_hash(bundle or {}, keys=("z",)):
        reasons.append("接触高度或下压参数变化")
    if entry.get("bindingRevision") != (bundle or {}).get("bindingRevision"):
        reasons.append("摄像头绑定版本变化")
    return reasons


def _config_for_bundle(config, bundle):
    normalized = validate_navigation(config)
    region = normalized.get("list_region")
    if not region:
        raise ValueError("手势档案必须人工标记列表安全区域 list_region")
    frame = (bundle or {}).get("frame") or {}
    if (region["frameW"], region["frameH"]) != (frame.get("width"), frame.get("height")):
        raise ValueError("标记画面尺寸与当前校准不符")
    return normalized


def check_binding(entry, bundle, current_session):
    reasons = _binding_reasons(entry, bundle, current_session)
    if reasons:
        raise ValueError("手势档案已失效：" + "；".join(reasons))
    return _config_for_bundle(entry["config"], bundle)


def load_profile(device_id, profile_id, data_root=None) -> dict | None:
    _identifier(profile_id, "手势档案编号")
    return deepcopy(_load(device_id, data_root)["entries"].get(profile_id))


def save_draft(device_id, profile_id, config, *, operator, current_session, data_root=None) -> dict:
    _identifier(profile_id, "手势档案编号")
    if not isinstance(config, dict):
        raise ValueError("导航配置必须是对象")
    config = dict(config)
    owner = _identifier(config.pop("owner_plugin", "wx-wechat-adapter"), "所属插件")
    bundle = cb.load_bundle(device_id, data_root)
    if (not bundle or bundle.get("status") != "ready" or not current_session
            or current_session != bundle.get("daemonSession") or bundle.get("deviceId") != device_id):
        raise ValueError("必须完成当前机械臂会话的校准后才能登记手势")
    normalized = _config_for_bundle(config, bundle)
    with _edit(device_id, data_root) as archive:
        old = archive["entries"].get(profile_id) or {}
        entry = {
            "profile_id": profile_id, "device_id": device_id, "owner_plugin": owner,
            "version": old.get("version", 0) + 1, "revision": old.get("revision", 0) + 1,
            "state": "draft", "config": normalized, "tests": {}, "pending_test": None,
            "test_blocked": False, "marked_at": cb.now_iso(),
            **ui_targets.binding_snapshot(bundle),
            "zHash": cb.profile_hash(bundle, keys=("z",)),
            "bindingRevision": bundle.get("bindingRevision"),
        }
        archive["entries"][profile_id] = entry
        _audit(archive, entry, "draft", operator)
        return deepcopy(entry)


def _required_tests(entry):
    config = entry["config"]
    # Even a zero runtime swipe limit must not bypass physical calibration tests.
    return {"search", "top"} if config["return_to_top"] else {"search"}


def _proofs_valid(entry):
    tests = entry.get("tests") or {}
    for direction in _required_tests(entry):
        test = tests.get(direction) or {}
        if (test.get("outcome") != "confirmed" or test.get("version") != entry["version"]
                or not test.get("evidence")
                or not gesture_motion_confirmed("swipe", test["evidence"].get("motion"))):
            return False
    return True


def view_profile(entry, bundle, current_session) -> dict:
    view = deepcopy(entry)
    reasons = _binding_reasons(entry, bundle, current_session)
    try:
        _config_for_bundle(entry["config"], bundle)
    except (ValueError, KeyError, TypeError) as exc:
        reasons.append(str(exc))
    if entry.get("state") != "enabled":
        reasons.append("档案尚未启用")
    if entry.get("test_blocked") or entry.get("pending_test"):
        reasons.append("测试结果未知或未完成，需人工复核并重新保存草稿")
    if not _proofs_valid(entry) or not entry.get("human_confirmation"):
        reasons.append("缺少实际测试证据或人工确认")
    view.update(usable=not reasons, blocking_reasons=reasons,
                state_display=_STATES.get(entry.get("state"), "未知"),
                readiness_scope="calibrated_gesture_only", production_ready=False,
                production_blocking_reasons=["本档案不验证生产界面识别、会话身份或业务发送资格"])
    return view


def resolve_profile(device_id, profile_id, version, *, owner_plugin, current_session,
                    data_root=None) -> dict:
    entry = load_profile(device_id, profile_id, data_root)
    if entry is None:
        raise ValueError("手势档案尚未登记")
    if type(version) is not int or version != entry.get("version"):
        raise ValueError("手势档案版本已变化，必须重新选择")
    if entry.get("owner_plugin") != owner_plugin:
        raise ValueError("手势档案所属插件不匹配")
    view = view_profile(entry, cb.load_bundle(device_id, data_root), current_session)
    if not view["usable"]:
        raise ValueError("；".join(view["blocking_reasons"]))
    return deepcopy(view["config"])


def _checked(archive, profile_id, expected_revision, device_id, current_session, data_root):
    entry = archive["entries"].get(profile_id)
    if entry is None:
        raise ValueError("手势档案尚未登记")
    if type(expected_revision) is not int or entry.get("revision") != expected_revision:
        raise ValueError("手势档案已修改，请刷新后再操作")
    bundle = cb.load_bundle(device_id, data_root)
    reasons = _binding_reasons(entry, bundle, current_session)
    if reasons:
        raise ValueError("手势档案已失效：" + "；".join(reasons))
    _config_for_bundle(entry["config"], bundle)
    return entry


def begin_test(device_id, profile_id, direction, *, expected_revision, operator,
               current_session, data_root=None):
    with _edit(device_id, data_root) as archive:
        entry = _checked(archive, profile_id, expected_revision, device_id, current_session, data_root)
        if entry["state"] not in {"draft", "tested"} or entry.get("test_blocked"):
            raise ValueError("只能测试无未知结果的草稿；需先人工复核并重新保存草稿")
        if direction not in _required_tests(entry) or f"{direction}_gesture" not in entry["config"]:
            raise ValueError("该方向尚未标记")
        if direction == "search":
            entry["tests"] = {}
        elif "search" not in entry["tests"]:
            raise ValueError("先通过查找方向测试，再测试回顶方向")
        entry["tests"].pop(direction, None)
        entry["state"] = "testing"
        entry["human_confirmation"] = None
        entry["pending_test"] = {"id": uuid.uuid4().hex, "direction": direction}
        entry["revision"] += 1
        _audit(archive, entry, f"test_{direction}_started", operator)
        return deepcopy(entry)


def finish_test(device_id, profile_id, *, expected_revision, test_id, outcome, evidence,
                operator, current_session, data_root=None):
    """Only the server-produced result goes here, never client-supplied proof."""
    with _edit(device_id, data_root) as archive:
        entry = _checked(archive, profile_id, expected_revision, device_id, current_session, data_root)
        pending = entry.get("pending_test") or {}
        if entry["state"] != "testing" or pending.get("id") != test_id:
            raise ValueError("测试编号已失效")
        if outcome not in {"confirmed", "failed", "unknown"}:
            raise ValueError("测试结果无效")
        if outcome == "confirmed":
            before, after = evidence.get("before") or {}, evidence.get("after") or {}
            motion = evidence.get("motion") or {}
            if (not before.get("sha256") or not after.get("sha256")
                    or before["sha256"] == after["sha256"]
                    or not before.get("path") or not after.get("path")
                    or not evidence.get("main_ui_confirmed")
                    or not gesture_motion_confirmed("swipe", motion)):
                raise ValueError("缺少前后画面、界面核对或真实滑动证据")
            if entry["config"]["return_to_top"]:
                expected_before = pending["direction"] == "search"
                if (before.get("top_anchor_found") is not expected_before
                        or after.get("top_anchor_found") is not (not expected_before)):
                    raise ValueError("顶部标志没有按测试方向离开/返回验证区域")
        entry["tests"][pending["direction"]] = {
            "outcome": outcome, "evidence": deepcopy(evidence), "version": entry["version"],
            "at": cb.now_iso(), "operator": operator,
        }
        entry["pending_test"] = None
        entry["test_blocked"] = outcome == "unknown"
        entry["state"] = "tested" if _proofs_valid(entry) else "draft"
        entry["revision"] += 1
        _audit(archive, entry, f"test_{pending['direction']}_{outcome}", operator)
        return deepcopy(entry)


def set_enabled(device_id, profile_id, *, enabled, expected_revision, operator,
                current_session, human_confirmed=False, data_root=None):
    with _edit(device_id, data_root) as archive:
        if enabled:
            entry = _checked(archive, profile_id, expected_revision, device_id, current_session, data_root)
            if (human_confirmed is not True or entry["state"] not in {"tested", "disabled"}
                    or not _proofs_valid(entry) or entry.get("test_blocked") or entry.get("pending_test")):
                raise ValueError("必须完成实际方向测试，并由操作人明确确认，才能启用")
            entry["human_confirmation"] = {"operator": operator, "at": cb.now_iso()}
            entry["state"] = "enabled"
        else:
            # Disabling a stale profile is always safe, but not a stale revision.
            entry = archive["entries"].get(profile_id)
            if entry is None or type(expected_revision) is not int or entry["revision"] != expected_revision:
                raise ValueError("手势档案不存在或已修改，请刷新")
            entry["state"] = "disabled"
        entry["revision"] += 1
        _audit(archive, entry, "enable" if enabled else "disable", operator)
        return deepcopy(entry)
