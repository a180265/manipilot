"""自动手势档案、资格与设置（规范第 7、12.3、22.12、22.16 节）。

- 档案容器 data/devices/{id}/automatic_gestures.json：独立于机械校准；
  原始采样不是证书，只有状态机完成对应验收才写 valid。
- 谁能写：自检任务发布（publish_run_results）；调试台不能伪造自动通过。
- 设备设置 data/devices/{id}/gesture_settings.json：仅 gesture_rules_version
  （四个设备开关已删，2026-09-30 拍板——可用性由校准门禁自动决定）。
- 版本自举（22.12）：gesture_profile_version=0 仅自检输入；首次写入版本 1，
  之后原子提交加 1；写入时 base 已变 → gesture_binding_changed。
"""
from __future__ import annotations

import logging
from copy import deepcopy
from functools import lru_cache
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "automatic-gesture-profile.v1"
PROFILE_FILENAME = "automatic_gestures.json"
SETTINGS_FILENAME = "gesture_settings.json"
NAVIGATION_PROFILE_ID = "wx_chat_list_auto"

GESTURE_IDS = ("scroll_up", "scroll_down", "system_back_left",
               "system_back_right", "system_home", "double_tap")
NAVIGATION_ACTIONS = ("search_next", "search_previous", "return_top",
                      "back_to_list", "home")
NAVIGATION_GESTURES = {
    "search_next": ("scroll_up",),
    "search_previous": ("scroll_down",),
    "return_top": ("scroll_down",),
    "back_to_list": ("system_back_left", "system_back_right"),
    "home": ("system_home",),
}
STATE_DISPLAY = {"unverified": "未验证", "valid": "有效", "invalid": "已失效",
                 "unsupported": "不支持"}

DEFAULT_SETTINGS = {
    "gesture_rules_version": "gesture-rules.v1",
}
# 2026-09-30 用户拍板：手势/导航可用性不再由人工开关决定——自动校准完成且
# 档案绑定匹配即可用（校准质量本身就是门禁），避免“校准完了还得去开开关”。
# 已删除的开关：automatic_gestures_enabled / automatic_navigation_enabled /
# gesture_program_enabled（同日）、「校准后自动测试手势」auto_validate_
# after_calibration（该手机手势页心跳不可用，自动排队只会堆积失败记录；
# 自检改为手动/视觉路径触发）。旧设置文件里的开关键由 load_settings 过滤丢弃。

_write_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(device_id: str) -> threading.Lock:
    with _locks_guard:
        if device_id not in _write_locks:
            _write_locks[device_id] = threading.Lock()
        return _write_locks[device_id]


def _device_dir(device_id: str, data_root: Path | None) -> Path:
    base = Path(data_root) if data_root else Path("data")
    return base / "devices" / device_id


def profile_path(device_id: str, data_root: Path | None = None) -> Path:
    return _device_dir(device_id, data_root) / PROFILE_FILENAME


def settings_path(device_id: str, data_root: Path | None = None) -> Path:
    return _device_dir(device_id, data_root) / SETTINGS_FILENAME


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class AutomaticGestureError(Exception):
    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        self.message = message
        super().__init__(f"{reason_code}: {message}")


# ---------- 设备开关 ----------

def load_settings(device_id: str, data_root: Path | None = None) -> dict:
    path = settings_path(device_id, data_root)
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update({key: value for key, value in stored.items()
                                 if key in DEFAULT_SETTINGS})
        except (ValueError, UnicodeDecodeError):
            raise AutomaticGestureError(
                "gesture_settings_invalid", "设备手势开关文件损坏")
    return settings


def save_settings(device_id: str, updates: dict,
                  data_root: Path | None = None) -> dict:
    with _lock_for(device_id):
        settings = load_settings(device_id, data_root)
        # 开关已全部删除（2026-09-30 拍板）：写路径只接受未知键拒绝语义，
        # 保留函数供兼容调用与测试断言“开关不可写”。
        unknown = set(updates) - set(DEFAULT_SETTINGS)
        if unknown:
            raise AutomaticGestureError(
                "invalid_request", f"未知开关：{sorted(unknown)}")
        if updates:
            raise AutomaticGestureError(
                "invalid_request", "手势/导航开关已删除，无可写设置")
        _atomic_write_json(settings_path(device_id, data_root), settings)
        return settings


# ---------- 档案容器 ----------

def empty_profile(device_id: str) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "device_id": device_id,
        "version": 0,
        "binding": None,
        "actions": {gesture_id: None for gesture_id in GESTURE_IDS},
        "navigation_profiles": {},
        "updated_at": None,
    }


def load_profile(device_id: str, data_root: Path | None = None) -> dict | None:
    path = profile_path(device_id, data_root)
    if not path.exists():
        return None
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AutomaticGestureError(
            "gesture_profile_invalid", f"自动手势档案损坏：{exc}") from exc
    if not isinstance(profile, dict) or profile.get("device_id") != device_id:
        raise AutomaticGestureError(
            "gesture_profile_invalid", "自动手势档案结构无效")
    if (type(profile.get("version")) is not int or profile["version"] < 0
            or any(not isinstance(profile.get(key, {}), dict)
                   for key in ("actions", "navigation_profiles", "published"))
            or any(not isinstance(nav, dict)
                   for nav in profile.get("navigation_profiles", {}).values())):
        raise AutomaticGestureError(
            "gesture_profile_invalid", "自动手势档案版本或依赖结构损坏")
    return profile


def _save_profile(device_id: str, profile: dict,
                  data_root: Path | None = None) -> None:
    profile["updated_at"] = _now_iso()
    _atomic_write_json(profile_path(device_id, data_root), profile)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _program_snapshot(entry: dict) -> tuple[dict | None, str | None]:
    """兼容同一动作已有的验证快照；绝不按默认参数重造缺失程序。"""
    plan = entry.get("plan")
    plan = plan if isinstance(plan, dict) else {}
    program = entry.get("program")
    if program is None:
        program = entry.get("validated_program")
    if program is None:
        program = plan.get("program")
    return program, entry.get("program_hash", plan.get("program_hash"))


def publish_run_results(device_id: str, run: dict, *,
                        data_root: Path | None = None,
                        base_version: int | None = None) -> dict:
    """自检 run 结果发布为动作证书（唯一写入口；version 原子递增）。

    保留 action_results 原状态和验收结论；只有 valid 且 three_passes
    才记录验收时间，不从历史 state 推造验证结论；不覆盖其他设备。
    """
    from drivers import gesture_program as gprog

    with _lock_for(device_id):
        profile = load_profile(device_id, data_root) or empty_profile(device_id)
        version = int(profile.get("version") or 0)
        if base_version is not None and version != base_version:
            raise AutomaticGestureError(
                "gesture_binding_changed",
                f"档案版本已从 {base_version} 变为 {version}，不得覆盖另一 run 结果")
        version += 1
        profile["version"] = version
        profile["binding"] = {
            **{k: deepcopy(v) for k, v in (run.get("input_binding") or {}).items()
               if k != "gesture_profile_version"},
            "coordinate_frame_hash": run.get("coordinate_frame_hash"),
            "recipe_version": run.get("gesture_rules_version"),
        }
        published = profile.get("published") or {}
        for result in run.get("action_results") or []:
            gesture_id = result.get("gesture_id")
            if gesture_id not in GESTURE_IDS:
                continue
            program, digest = _program_snapshot(result)
            profile["actions"][gesture_id] = {
                "gesture_id": gesture_id,
                "version": version,
                "recipe_version": run.get("gesture_rules_version"),
                "state": result.get("state", "unverified"),
                "binding": run.get("input_binding"),
                "coordinate_frame_hash": run.get("coordinate_frame_hash"),
                "program": deepcopy(program),
                "program_hash": (digest if digest is not None else
                                 (gprog.program_hash(program)
                                  if isinstance(program, dict) else None)),
                "validated_primitive": deepcopy(result.get("validated_primitive")),
                "primitive_hash": result.get("primitive_hash"),
                "validation_policy": {
                    "consecutive_passes_required": 3,
                },
                "validation_evidence": {
                    "trial_ids": result.get("trial_ids") or [],
                    "run_id": run.get("run_id"),
                },
                "validated_at": (_now_iso() if result.get("state") == "valid"
                                 and result.get("validation_status") == "three_passes"
                                 else None),
                "dependency_versions": {
                    "gesture_rules_version": run.get("gesture_rules_version"),
                    "calibration_fingerprint":
                        (run.get("input_binding") or {}).get(
                            "calibration_fingerprint"),
                    "coordinate_frame_hash": run.get("coordinate_frame_hash"),
                },
                "validation_status": result.get("validation_status", "not_tested"),
            }
            published[gesture_id] = version
        profile["published"] = published
        # 22.12：至少一个方向通过后自动创建唯一内置导航引用
        scroll_valid = any(
            (profile["actions"].get(g) or {}).get("state") == "valid"
            for g in ("scroll_up", "scroll_down"))
        nav_profiles = profile.setdefault("navigation_profiles", {})
        if scroll_valid or NAVIGATION_PROFILE_ID in nav_profiles:
            nav = nav_profiles.setdefault(NAVIGATION_PROFILE_ID, {
                "profile_id": NAVIGATION_PROFILE_ID,
                "owner_plugin": "wx-wechat-adapter",
                "layout_version": "wechat-list-layout.v1",
            })
            dependencies = {
                g: (profile["actions"].get(g) or {}).get("version")
                for ids in NAVIGATION_GESTURES.values() for g in ids}
            nav.update({
                "version": version,
                "contract_version": CONTRACT_VERSION,
                "directions": {g: dependencies[g]
                               for g in ("scroll_up", "scroll_down")},
                "dependency_versions": dependencies,
                "binding": deepcopy(profile["binding"]),
            })
        _save_profile(device_id, profile, data_root)
        result = json.loads(json.dumps(profile, ensure_ascii=False))
        # C1（2026-10-01 backlog）：零动作入证的发布必须显式留痕——这是
        # automatic_gestures.json 出现“高版本空壳”的成因（失败 run 也
        # 递增版本+覆写绑定）。发布行为本身是设计（保留 run 结果与绑定
        # 快照供下次提交 base_version 对齐），但必须可观测、可归因。
        published_count = sum(
            1 for entry in result["actions"].values()
            if isinstance(entry, dict) and entry.get("state") == "valid")
        result["published_actions"] = published_count
        if not published_count:
            logger.warning(
                "设备 %s 自检发布零动作入证（版本 %s 递增、绑定已更新，无证书）："
                "run 状态=%s，动作=%s——这是空壳档案的成因，请排查自检失败原因",
                device_id, version, run.get("state"),
                [(row.get("gesture_id"), row.get("state"), row.get("reason_code"))
                 for row in (run.get("action_results") or [])])
        return result


# ---------- 资格计算（GET 端点用） ----------

def _reason(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _same_json(left, right) -> bool:
    """协议值严格比较，避免 True 与版本 1 或非有限数字等同。"""
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return False


def _certificate_binding_matches(stored: dict | None, current: dict) -> bool:
    from drivers import screen_gestures as sg
    if not isinstance(stored, dict) or not set(sg.BINDING_KEYS) <= set(stored):
        return False
    # 自检输入可含发布前的容器版本；未重新验证的动作不会随发布自动重绑。
    keys = set(sg.BINDING_KEYS) | (set(stored) & {
        "coordinate_frame_hash", "recipe_version"})
    return set(stored) <= set(current) and all(
        _same_json(stored[key], current[key]) for key in keys)


def _action_reasons(gesture_id: str, entry: dict, profile: dict,
                    binding: dict | None, *, internal_frame: bool = False) -> list[dict]:
    from drivers import gesture_program as gprog

    reasons = []
    if internal_frame and gesture_id.startswith("system_"):
        reasons.append(_reason("navigation_mode_unsupported",
                               "系统边缘手势需要手机真实页面坐标与手势区元数据"))
    if (entry.get("state") != "valid"
            or entry.get("validation_status") != "three_passes"):
        reasons.append(_reason("gesture_not_validated",
                               f"动作 {gesture_id} 缺少已通过验收的证书"))
    version = entry.get("version")
    if (entry.get("gesture_id") != gesture_id or type(version) is not int
            or version < 1 or version > profile.get("version", 0)
            or not _same_json((profile.get("published") or {}).get(gesture_id),
                              version)):
        reasons.append(_reason("gesture_dependency_changed",
                               f"动作 {gesture_id} 的证书编号或发布版本已变化"))
    if binding is not None:
        if (not _certificate_binding_matches(entry.get("binding"), binding)
                or entry.get("coordinate_frame_hash")
                != binding["coordinate_frame_hash"]):
            reasons.append(_reason("gesture_binding_changed",
                                   "完整校准绑定或坐标桥已变化，证书失效"))
        dependencies = entry.get("dependency_versions")
        dependencies = dependencies if isinstance(dependencies, dict) else {}
        if (entry.get("recipe_version") != binding["recipe_version"]
                or dependencies.get("gesture_rules_version")
                != binding["recipe_version"]):
            reasons.append(_reason("gesture_rules_changed",
                                   "手势规则版本已变化或缺失，需重新验收"))
        if any(dependencies.get(key) != binding[key] for key in (
                "calibration_fingerprint", "coordinate_frame_hash")):
            reasons.append(_reason("gesture_dependency_changed",
                                   "动作证书的校准或坐标桥依赖已变化或缺失"))
    program, digest = _program_snapshot(entry)
    primitive = entry.get("validated_primitive")
    if program is None and gesture_id == "double_tap" and primitive is not None:
        if not _valid_double_tap_primitive(primitive, entry, binding):
            reasons.append(_reason("gesture_program_changed",
                                   "已验证双击参数或摘要已变化，需重新验收"))
    elif program is None:
        reasons.append(_reason("gesture_program_missing",
                               f"动作 {gesture_id} 缺少已验证程序，不能执行"))
    else:
        try:
            gprog.validate_program(program)
        except (gprog.GestureProgramError, TypeError, ValueError) as exc:
            reasons.append(_reason("invalid_program", f"动作程序无效：{exc}"))
        else:
            if digest != gprog.program_hash(program):
                reasons.append(_reason("gesture_program_changed",
                                       "动作程序摘要缺失或已变化，需重新验收"))
    return reasons


def _valid_double_tap_primitive(primitive: dict, entry: dict,
                                binding: dict | None) -> bool:
    from drivers import gesture_program as gprog

    if (not isinstance(primitive, dict)
            or set(primitive) != {"kind", "point", "binding", "interval_ms"}
            or primitive["kind"] != "calibrated_double_tap"
            or type(primitive["interval_ms"]) is not int
            or primitive["interval_ms"] != 50):
        return False
    point = primitive["point"]
    if (not isinstance(point, dict) or set(point) != {"x", "y"}
            or any(type(v) not in (int, float) or not 0 <= v <= 1
                   for v in point.values())
            or binding is None
            or not _certificate_binding_matches(primitive["binding"], binding)):
        return False
    try:
        return entry.get("primitive_hash") == gprog.canonical_hash(primitive)
    except (TypeError, ValueError):
        return False


def _current_binding(bundle: dict | None, frame: dict | None,
                      rules_version: str) -> tuple[dict | None, list[dict]]:
    """当前绑定 = 旧完整 binding + coordinate_frame_hash + 版本引用。"""
    from drivers import screen_gestures as sg
    from drivers import gesture_program as gprog
    if bundle is None:
        return None, [_reason("calibration_unavailable", "当前校准不可用")]
    try:
        binding = sg.binding_snapshot(bundle)
    except (ValueError, TypeError, KeyError) as exc:
        return None, [_reason("calibration_unavailable", str(exc))]
    try:
        frame = gprog.frame_for_internal_gesture(bundle, frame)
    except gprog.GestureProgramError as exc:
        return None, [_reason(exc.reason_code, exc.message)]
    return {
        **binding,
        "coordinate_frame_hash": frame["coordinate_frame_hash"],
        "gesture_profile_version": 1,  # 由调用方用容器 version 覆盖
        "recipe_version": rules_version,
    }, []


def gestures_view(device_id: str, *, bundle: dict | None, frame: dict | None,
                  data_root: Path | None = None) -> dict:
    """GET /v1/devices/{id}/automatic-gestures 响应（12.3 节字段）。"""
    settings = load_settings(device_id, data_root)
    profile = load_profile(device_id, data_root)
    return _gestures_view(device_id, bundle=bundle, frame=frame,
                          settings=settings, profile=profile)


def _gestures_view(device_id: str, *, bundle: dict | None, frame: dict | None,
                   settings: dict, profile: dict | None) -> dict:
    """在同一份档案快照上计算单动作资格，供导航查询与解析复用。"""
    from drivers import gesture_program as gprog
    rules_version = settings.get("gesture_rules_version",
                                 DEFAULT_SETTINGS["gesture_rules_version"])
    if profile is None:
        return {
            "contract_version": CONTRACT_VERSION,
            "device_id": device_id,
            "binding": None,
            "actions": {gesture_id: _idle_action(gesture_id) for gesture_id
                        in GESTURE_IDS},
            "blocking_reasons": [_reason("profile_missing", "尚未建立自动档案")],
        }
    binding, blocking = _current_binding(bundle, frame, rules_version)
    # 开关已删（2026-09-30 拍板）：可用性由校准绑定与档案有效性决定。
    version = profile.get("version")
    if (profile.get("contract_version") != CONTRACT_VERSION
            or type(version) is not int or version < 1):
        blocking.append(_reason("gesture_profile_invalid", "自动手势档案契约或版本无效"))
        version = 1
    if binding is not None:
        binding = {**binding, "gesture_profile_version": max(version, 1)}
        if binding["device_id"] != device_id:
            blocking.append(_reason("gesture_binding_changed", "当前校准不属于请求设备"))
        if not _certificate_binding_matches(profile.get("binding"), binding):
            blocking.append(_reason("gesture_binding_changed", "自动手势档案完整绑定已变化"))
    internal_frame = (
        frame is None or gprog.is_internal_coordinate_frame(frame)
        or (binding is not None and isinstance(frame, dict)
            and frame.get("coordinate_frame_hash") != binding["coordinate_frame_hash"]))
    actions = {}
    for gesture_id in GESTURE_IDS:
        entry = (profile.get("actions") or {}).get(gesture_id)
        if not isinstance(entry, dict):
            edge_reasons = ([_reason("navigation_mode_unsupported",
                                     "系统边缘手势需要手机真实页面坐标与手势区元数据")]
                            if internal_frame and gesture_id.startswith("system_") else [])
            actions[gesture_id] = _idle_action(gesture_id, blocking + edge_reasons)
            continue
        reasons = list(blocking) + _action_reasons(
            gesture_id, entry, profile, binding,
            internal_frame=internal_frame)
        validated = (entry.get("state") == "valid"
                     and entry.get("validation_status") == "three_passes")
        actions[gesture_id] = {
            "gesture_id": gesture_id,
            "version": entry.get("version", 1),
            "state": entry.get("state", "unverified"),
            "state_display": STATE_DISPLAY.get(entry.get("state"), "未验证"),
            "calculated": (_program_snapshot(entry)[0] is not None
                           or (gesture_id == "double_tap"
                               and entry.get("validated_primitive") is not None)),
            "controller_supported": True,
            "contact_validated": validated,
            "effect_validated": validated,
            "usable": not reasons,
            "blocking_reasons": reasons,
            "validation_status": entry.get("validation_status", "not_tested"),
        }
    return {
        "contract_version": CONTRACT_VERSION,
        "device_id": device_id,
        "binding": binding,
        "actions": actions,
        "blocking_reasons": blocking,
    }


def _idle_action(gesture_id: str, blocking: list | None = None) -> dict:
    return {
        "gesture_id": gesture_id, "version": 1, "state": "unverified",
        "state_display": "未验证", "calculated": False,
        "controller_supported": False, "contact_validated": False,
        "effect_validated": False, "usable": False,
        "blocking_reasons": list(blocking or []) + [
            _reason("gesture_certificate_missing", f"动作 {gesture_id} 尚无验收证书")],
        "validation_status": "not_tested",
    }


def navigation_view(device_id: str, profile_id: str, *, bundle: dict | None,
                    frame: dict | None, data_root: Path | None = None,
                    evidence_root: Path | None = None, evidence_get=None) -> dict | None:
    """GET automatic-navigation/{profile_id}；不存在返回 None（404）。"""
    profile = load_profile(device_id, data_root)
    if profile is None or profile_id not in (profile.get("navigation_profiles")
                                             or {}):
        return None
    settings = load_settings(device_id, data_root)
    gestures = _gestures_view(device_id, bundle=bundle, frame=frame,
                              profile=profile, settings=settings)
    return _navigation_view(device_id, profile_id, profile=profile,
                            settings=settings, gestures=gestures, data_root=data_root,
                            evidence_root=evidence_root, evidence_get=evidence_get,
                            screen=(bundle or {}).get("screen"))


def _navigation_gesture(gestures: dict, nav: dict, action: str
                        ) -> tuple[str | None, list[dict]]:
    """左右返回各自验收，优先左侧；一侧失效不妨碍另一有效侧。"""
    reasons = []
    for gesture_id in NAVIGATION_GESTURES[action]:
        item = gestures["actions"][gesture_id]
        why = list(item["blocking_reasons"])
        dependencies = nav.get("dependency_versions")
        dependencies = dependencies if isinstance(dependencies, dict) else {}
        directions = nav.get("directions")
        directions = directions if isinstance(directions, dict) else {}
        if (not _same_json(dependencies.get(gesture_id), item["version"])
                or (gesture_id in ("scroll_up", "scroll_down") and not
                    _same_json(directions.get(gesture_id), item["version"]))):
            why.append(_reason("navigation_dependency_changed",
                               f"导航依赖动作 {gesture_id} 的证书版本已变化或缺失"))
        if item["usable"] and not why:
            return gesture_id, []
        for reason in why:
            if reason not in reasons:
                reasons.append(reason)
    return None, reasons


def _navigation_view(device_id: str, profile_id: str, *, profile: dict,
                     settings: dict, gestures: dict,
                     data_root: Path | None, evidence_root=None, evidence_get=None,
                     screen=None) -> dict:
    nav = profile["navigation_profiles"][profile_id]
    # 开关已删（2026-09-30 拍板）：导航资格由证书绑定/导航档案/模板证据决定。
    reasons = list(gestures["blocking_reasons"])
    if (nav.get("contract_version") != CONTRACT_VERSION
            or nav.get("profile_id") != profile_id
            or not _same_json(nav.get("version"), profile.get("version"))):
        reasons.append(_reason("navigation_profile_changed",
                               "导航档案引用、契约或版本已变化，请重新读取资格"))
    if gestures["binding"] is not None and not _certificate_binding_matches(
            nav.get("binding"), gestures["binding"]):
        reasons.append(_reason("gesture_binding_changed",
                               "导航档案完整绑定已变化，请重新验收"))
    actions = {}
    for action in NAVIGATION_GESTURES:
        gesture_id, action_reasons = _navigation_gesture(gestures, nav, action)
        why = list(reasons) + [r for r in action_reasons if r not in reasons]
        actions[action] = {"usable": gesture_id is not None and not why,
                           "blocking_reasons": why}
    for action, kind in (("return_top", "top"), ("home", "launcher")):
        reason = _template_blocking_reason(
            device_id, kind, gestures["binding"], nav.get("layout_version"), data_root,
            evidence_root=evidence_root, evidence_get=evidence_get, screen=screen)
        if reason is not None:
            actions[action]["blocking_reasons"].append(reason)
            actions[action]["usable"] = False
        if action == "return_top":
            actions[action]["top_proof_available"] = reason is None
    return {
        "contract_version": CONTRACT_VERSION,
        "device_id": device_id,
        "binding": gestures["binding"],
        "actions": actions,
        "blocking_reasons": reasons,
        "profile_id": profile_id,
        "version": nav.get("version", 1),
        "owner_plugin": nav.get("owner_plugin", "wx-wechat-adapter"),
        "layout_version": nav.get("layout_version", "wechat-list-layout.v1"),
        "navigation_limits": {"max_steps": 8, "max_physical_actions": 8,
                              "timeout_ms": 90000, "max_boundary_probes": 2},
    }


@lru_cache(maxsize=1)
def _template_validator():
    import jsonschema
    path = (Path(__file__).resolve().parents[1] / "api" / "contracts"
            / "navigation-template.v1.schema.json")
    schema = json.loads(path.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(
        {**schema, "$ref": "#/$defs/templateRecord"},
        format_checker=jsonschema.FormatChecker())


def _template_blocking_reason(device_id: str, kind: str, binding: dict | None,
                              layout_version: str, data_root: Path | None, *,
                              evidence_root=None, evidence_get=None, screen=None
                              ) -> dict | None:
    """只认完整登记且当前绑定匹配的模板；不采帧、不证明当前页面命中。"""
    label = "顶部专属" if kind == "top" else "桌面"
    path = _device_dir(device_id, data_root) / "navigation_templates" / f"{kind}.json"
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        code = "top_proof_unavailable" if kind == "top" else "launcher_template_missing"
        return _reason(code, f"缺少{label}模板，不能授予该导航动作资格")
    except (OSError, ValueError, UnicodeDecodeError):
        return _reason("navigation_template_invalid", f"{label}模板文件损坏或不可读")
    if (not _template_validator().is_valid(template)
            or template["kind"] != kind or template["device_id"] != device_id
            or template["template_id"] != kind
            or template["layout_version"] != layout_version
            or not (1 if kind == "top" else 3) <= len(template["anchor_evidence"])
            <= (3 if kind == "top" else 6)):
        return _reason("navigation_template_invalid", f"{label}模板登记、证据或布局版本无效")
    if binding is None or not _certificate_binding_matches(template["binding"], binding):
        return _reason("navigation_template_binding_changed",
                       f"{label}模板完整绑定与当前校准不一致，请重新登记")
    from adapters.uinav.navigation_templates import load_template_patch
    try:
        size = (screen["screenWidth"], screen["screenHeight"]) if screen else None
        for entry in template["anchor_evidence"]:
            load_template_patch(entry, kind=kind, device_id=device_id, binding=binding,
                                evidence_root=evidence_root, evidence_get=evidence_get,
                                screen_size=size)
    except (ValueError, TypeError, KeyError):
        return _reason("navigation_template_invalid", f"{label}模板证据缺失、损坏或归属不符，请重新登记")
    return None


def resolve_navigation_action(device_id: str, profile_ref: dict, binding: dict,
                              action: str, *, bundle: dict | None,
                              frame: dict | None,
                              data_root: Path | None = None,
                              evidence_root: Path | None = None, evidence_get=None) -> dict:
    """只读解析当前已验收动作，不落盘、不规划替代动作、不移动硬件。

    profile_ref 就是请求的 navigation_profile；view 是完整导航资格响应。
    提交与物理动作前均需调用，返回值不是跨请求可缓存的运动授权。
    """
    from runtime.errors import ApiError

    if (not isinstance(profile_ref, dict)
            or set(profile_ref) != {"profile_id", "version", "contract_version"}
            or not isinstance(profile_ref.get("profile_id"), str)
            or not profile_ref["profile_id"].strip()
            or type(profile_ref.get("version")) is not int
            or profile_ref["version"] < 1
            or profile_ref.get("contract_version") != CONTRACT_VERSION):
        raise ApiError("device_not_eligible", "导航档案引用的编号、版本或契约无效")
    if not isinstance(action, str) or action not in NAVIGATION_GESTURES:
        raise ApiError("device_not_eligible", "请求的自动导航动作不受支持")
    try:
        profile = load_profile(device_id, data_root)
        profile_id = profile_ref["profile_id"]
        if profile is None or profile_id not in (profile.get("navigation_profiles") or {}):
            raise ApiError("device_not_eligible", "尚未建立请求的自动导航档案")
        settings = load_settings(device_id, data_root)
        gestures = _gestures_view(device_id, bundle=bundle, frame=frame,
                                  settings=settings, profile=profile)
        view = _navigation_view(device_id, profile_id, profile=profile,
                                settings=settings, gestures=gestures,
                                data_root=data_root, evidence_root=evidence_root,
                                evidence_get=evidence_get, screen=(bundle or {}).get("screen"))
    except (AutomaticGestureError, OSError) as exc:
        raise ApiError("device_not_eligible", f"无法读取自动导航资格：{exc}") from exc
    if not _same_json(profile_ref["version"], view["version"]):
        raise ApiError("device_not_eligible", "导航档案版本已变化，请重新读取资格")
    current = view["binding"]
    if current is None:
        message = "；".join(r["message"] for r in view["blocking_reasons"])
        raise ApiError("device_not_eligible", message or "当前校准绑定不可用")
    if not isinstance(binding, dict) or not _same_json(binding, current):
        raise ApiError("device_not_eligible", "请求完整绑定与当前快照不一致，请重新读取资格")
    eligibility = view["actions"][action]
    if not eligibility["usable"] or eligibility["blocking_reasons"]:
        message = "；".join(r["message"] for r in eligibility["blocking_reasons"])
        raise ApiError("device_not_eligible", message or "导航动作尚未通过验收")
    gesture_id, _ = _navigation_gesture(
        gestures, profile["navigation_profiles"][profile_id], action)
    return {
        "gesture_id": gesture_id,
        "program": deepcopy(_program_snapshot(profile["actions"][gesture_id])[0]),
        "binding": deepcopy(current),
        "view": deepcopy(view),
    }




def restore_certificates(device_id: str, bundle: dict,
                         data_root: Path | None = None,
                         coordinate_frame: dict | None = None) -> dict:
    """A3（2026-10-01 拍板）：daemon 会话重置且位置复核通过后，已发布动作
    证书直接恢复——程序不变，绑定重签为新会话（版本原子 +1）。

    校准指纹/坐标桥哈希不一致时拒绝（几何已变的证书不得复活）；无已发布
    动作时拒绝（空壳不参与恢复）。"""
    with _lock_for(device_id):
        profile = load_profile(device_id, data_root) or empty_profile(device_id)
        published_actions = {gid: entry for gid, entry in (profile["actions"] or {}).items()
                             if isinstance(entry, dict) and entry.get("version")}
        if not published_actions:
            raise AutomaticGestureError(
                "restore_nothing_published", "无已发布动作证书可恢复，请重新自检")
        # 传原始帧：_current_binding 内部做与 runner 同源的
        # frame_for_internal_gesture 单次转换（勿预先转换=双重转换会换算出
        # 另一哈希，误报几何变化）。
        current_binding, _blocking = _current_binding(bundle, coordinate_frame,
                                                      profile.get("gesture_rules_version")
                                                      or DEFAULT_SETTINGS["gesture_rules_version"])
        if current_binding is None:
            raise AutomaticGestureError(
                "restore_binding_unavailable", "当前档案无法产出完整绑定，不能恢复证书")
        stored = profile.get("binding") or {}
        # 几何守卫（OCR 审查修正）：calibration_fingerprint 的键表含
        # daemonSession——会话重置后直接比对新指纹必失败。正确姿势=会话
        # 探针：把当前档案会话临时拨回发布时会话重算指纹，与 stored 比对
        # ——只有几何变化才会不一致。
        from drivers import calibration_bundle as cb
        stored_fp = stored.get("calibration_fingerprint")
        stored_session = stored.get("daemon_session")
        if stored_fp and stored_session:
            probe = deepcopy(bundle)
            probe["daemonSession"] = stored_session
            probe_fp = cb.motion_calibration_fingerprint(probe)
            if probe_fp != stored_fp:
                raise AutomaticGestureError(
                    "restore_geometry_changed",
                    "校准指纹已变化（剔除会话因素后仍不一致）：几何已变的证书"
                    "不得恢复，请重新校准并自检")
        restored_binding = {**current_binding}
        profile["version"] = int(profile.get("version") or 0) + 1
        profile["binding"] = restored_binding
        for gid, entry in profile["actions"].items():
            if isinstance(entry, dict) and entry.get("version"):
                entry["binding"] = deepcopy(restored_binding)
                entry["restored_at"] = _now_iso()
        _save_profile(device_id, profile, data_root)
        return json.loads(json.dumps(profile, ensure_ascii=False))
