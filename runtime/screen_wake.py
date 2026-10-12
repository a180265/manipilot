"""Manual wake policy: one validated task may bypass only camera_waiting."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from functools import lru_cache
import json
from pathlib import Path
import secrets

import jsonschema

from runtime.calibration_gate import check_registered_calibration
from runtime.device_eligibility import compute_device_eligibility
from runtime.errors import ApiError
from runtime.util import iso, parse_iso, sha256_text, utc_now

CONTRACT_VERSION = "screen-wake.v1"
TASK_TYPE = "screen_wake"
CAPABILITY = "arm.wake_screen"
PLUGIN_ID = "screen-wake-adapter"
MAX_TTL_SECONDS = 60

MANUAL_CONFIRM_CONTRACT_VERSION = "screen-wake-manual-confirm.v1"
MANUAL_CONFIRM_TTL_SECONDS = 60
TERMINAL_TASK_STATUSES = {"confirmed", "failed", "unknown", "cancelled", "expired"}


@lru_cache(maxsize=8)
def request_validator(fragment: str | None = None):
    path = Path(__file__).resolve().parents[1] / "api/contracts/screen-wake.v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    if fragment:
        schema = {"$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker())


def claims_screen_wake(task: dict) -> bool:
    return (task.get("task_type") == TASK_TYPE or task.get("capability") == CAPABILITY
            or task.get("contract_version") == CONTRACT_VERSION)


def validate_request(request: dict, entry: dict) -> None:
    try:
        request_validator().validate(request)
    except jsonschema.ValidationError as exc:
        raise ApiError("invalid_request", f"人工唤醒请求不符合契约：{exc.message}") from exc
    if (entry.get("plugin_id") != PLUGIN_ID or entry.get("capability") != CAPABILITY
            or entry.get("read_only_task")):
        raise ApiError("capability_not_allowed", "人工唤醒必须使用独立的写任务插件")
    if request["payload"]["binding"]["device_id"] != request["device_id"]:
        raise ApiError("invalid_request", "唤醒绑定与任务设备不一致")


def cap_expiry(request: dict, created_at: str) -> str:
    return iso(min(parse_iso(request["expires_at"]),
                   parse_iso(created_at) + timedelta(seconds=MAX_TTL_SECONDS)))


def is_screen_wake_task(task: dict) -> bool:
    """The single exception predicate, also valid after expiry for safe finalization.

    Identity, plugin, consent, closed payload and original lifetime are required.
    Live device/lease/expiry checks remain separate and must never be exempted.
    """
    if (not isinstance(task, dict) or task.get("plugin_id") != PLUGIN_ID
            or task.get("read_only_task") is not False
            or task.get("policy_context") not in (None, {})):
        return False
    request = {key: task.get(key) for key in request_validator().schema["required"]}
    if not request_validator().is_valid(request):
        return False
    binding = request["payload"]["binding"]
    if binding["device_id"] != task["device_id"]:
        return False
    try:
        created, expires = parse_iso(task.get("created_at")), parse_iso(task["expires_at"])
        return bool(created and expires and
                    expires <= created + timedelta(seconds=MAX_TTL_SECONDS))
    except (ValueError, TypeError):
        return False


def resolve_target(runtime, device_id: str) -> tuple[dict, dict]:
    from drivers import calibration_bundle
    from drivers.wake_screen import resolve_wake_target

    root = runtime._gesture_data_root(device_id)
    bundle = calibration_bundle.load_bundle(device_id, root)
    target = resolve_wake_target(bundle)
    return target, bundle


def record_unstarted_result(task: dict, outcome: str, message: str) -> None:
    """Record a preflight decision, not an invented motion or screenshot."""
    detail = {
        "contract_version": CONTRACT_VERSION, "binding": deepcopy(task["payload"]["binding"]),
        "controller_completed": False, "tap_count": 0, "effect_status": "unverified",
        "motion_not_started": True, "before_evidence": None, "after_evidence": None,
        "frame_observations": [],
    }
    task.update({key: detail[key] for key in
                 ("controller_completed", "tap_count", "effect_status", "motion_not_started")})
    task.setdefault("step_log", []).append({
        "step_no": 0, "action": "wake_preflight", "action_display": "人工唤醒动作前检查",
        "is_action": False, "phase": "done", "outcome": outcome, "message": message,
        "detail": detail, "at": iso(utc_now()),
    })


def wake_readiness(runtime, device_id: str, *, task: dict | None = None) -> dict:
    """Read current registered facts, without touching state or requesting motion."""
    device = deepcopy(runtime.devices.get(device_id))
    reasons = []

    def block(code, message):
        if not any(item["code"] == code for item in reasons):
            reasons.append({"code": code, "message": message})

    valid_task = task is not None and is_screen_wake_task(task)
    if task is not None and not valid_task:
        block("invalid_request", "唤醒任务身份、人工确认或有效期无效")
    lease = runtime.leases.get(device_id)
    owns = bool(valid_task and lease and lease.get("task_id") == task["task_id"]
                and lease.get("lease_id") == task.get("lease_id")
                and parse_iso(lease.get("expires_at")) > utc_now())
    snapshot = deepcopy(device)
    if snapshot.get("camera_quality") == "waiting" and (task is None or valid_task):
        snapshot["camera_quality"] = "clear"
    if snapshot.get("status") == "busy" and owns:
        snapshot["status"] = "ready"
    for code in compute_device_eligibility(snapshot)["blocking_checks"]:
        block(code, f"设备安全条件未满足：{code}")
    if snapshot.get("status") != "ready":
        block("device_not_ready", "当前设备状态不允许人工唤醒")
    if device.get("camera_quality") not in {"clear", "waiting"}:
        block("camera_unavailable", "画面不可用、冻结或过曝，不能执行唤醒")
    if device.get("camera_quality") == "clear" and not (
            owns and task.get("wake_driver_started")):
        block("screen_not_waiting", "当前画面无需唤醒")
    age = device.get("camera_frame_age_ms")
    if age is not None and (type(age) not in (int, float) or not 0 <= age <= 3000):
        block("camera_unavailable", "画面已过期或帧时间无效")
    if device.get("camera_frozen"):
        block("camera_unavailable", "画面冻结，不能执行唤醒")
    if runtime.estop.active:
        block("estop_active", "全局急停中")
    if device.get("motion_result_unknown") or device.get("stop_result_unknown"):
        block("result_unknown_pending", "设备动作或停止结果未知")
    if lease and not owns:
        block("lease_conflict", "设备仍有其他任务或失效租约，需先核对")
    if task and task.get("lease_id") and not owns:
        block("lease_conflict", "原任务租约已失效")
    tasks = runtime.list_tasks(device_id=device_id)
    # Terminal history is audit evidence, not the current physical safety state.
    # Live uncertainty, active work, leases and calibration remain independent gates.
    if any(item.get("status") in {"awaiting_device", "validating", "executing"}
           and item.get("task_id") != (task or {}).get("task_id") for item in tasks):
        block("device_busy", "设备仍有在途执行任务")
    if CAPABILITY not in device.get("capabilities", []):
        block("capability_not_allowed", "设备未声明人工唤醒能力")
    if not callable(getattr(runtime.devices.driver(device_id), "wake_screen", None)):
        block("capability_unavailable", "当前驱动尚未提供人工唤醒")
    entry = runtime.plugins.task_type_registry().get(TASK_TYPE)
    if not entry or entry.get("plugin_id") != PLUGIN_ID:
        block("plugin_task_type_unknown", "人工唤醒插件尚未启用")
    else:
        for message in runtime.plugin_context_blockers(PLUGIN_ID, device_id):
            block("device_not_eligible", message)

    binding, target = None, None
    try:
        resolved, bundle = resolve_target(runtime, device_id)
        binding = resolved["binding"]
        target = {key: deepcopy(resolved[key]) for key in ("validation_point_id", "point")}
        for check in check_registered_calibration(device, bundle):
            block(check["code"], check["message"])
        if (binding["device_id"] != device_id
                or binding["daemon_session"] != device.get("daemon_session")
                or binding["camera_id"] != device.get("camera_id")):
            block("calibration_unavailable", "当前会话或设备绑定已变化")
        if valid_task and task["payload"]["binding"] != binding:
            block("calibration_unavailable", "请求绑定与当前完整校准不一致")
    except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        block("calibration_unavailable", f"已验证唤醒点不可用：{exc}")
    return {
        "contract_version": CONTRACT_VERSION, "device_id": device_id,
        "binding": binding, "usable": not reasons, "blocking_reasons": reasons,
        "target": target, "effect_status": "unverified",
    }


def validate_preconditions(runtime, task: dict) -> None:
    if not is_screen_wake_task(task):
        raise ApiError("invalid_request", "人工唤醒任务未通过完整契约校验")
    if parse_iso(task["expires_at"]) <= utc_now():
        raise ApiError("invalid_request", "人工唤醒请求已过期，禁止补做")
    view = wake_readiness(runtime, task["device_id"], task=task)
    if not view["usable"]:
        raise ApiError("device_not_eligible",
                       "；".join(item["message"] for item in view["blocking_reasons"]))


async def wake_metadata(runtime, device_id: str) -> dict:
    view = wake_readiness(runtime, device_id)
    driver = runtime.devices.driver(device_id)
    reasons = view["blocking_reasons"]
    try:
        state = await getattr(driver, "read_gesture_state", driver.read_state)()
        if state.get("connected") is not True:
            reasons.append({"code": "device_offline", "message": "控制器未连接或状态不明"})
        if state.get("estop_latched") or state.get("daemon_estop_locked"):
            reasons.append({"code": "estop_active", "message": "控制器急停锁定中"})
        if state.get("motion_lease"):
            reasons.append({"code": "lease_conflict", "message": "控制器有动作占用"})
        if state.get("motion_result_unknown"):
            reasons.append({"code": "result_unknown_pending", "message": "控制器动作结果未知"})
        if "tap2.v1" not in (state.get("motion_features") or []):
            reasons.append({"code": "capability_unavailable", "message": "控制器不支持原子双击"})
        if view["binding"] and state.get("session") != view["binding"]["daemon_session"]:
            reasons.append({"code": "calibration_unavailable", "message": "控制器会话与校准不一致"})
    except Exception:
        reasons.append({"code": "device_offline", "message": "无法确认控制器当前状态"})
    view["usable"] = not reasons
    return view


# ---------- 人工无姿态唤醒的单次现场确认（规范 §9.3，W13） ----------

@lru_cache(maxsize=2)
def manual_confirm_validator(fragment: str | None = None):
    path = (Path(__file__).resolve().parents[1]
            / "api/contracts/screen-wake-manual-confirm.v1.schema.json")
    schema = json.loads(path.read_text(encoding="utf-8"))
    if fragment:
        schema = {"$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker())


def manual_confirm_reference(runtime, device_id: str, task: dict) -> dict:
    """§9.3(1) 只读准备：任务、幂等键、完整绑定、合格唤醒点与参考证据标识。

    参考姿态只取相同有效校准/坐标基准下最后一次已验证姿态；当前没有该持久
    记录时取已发布校准基准并明确标注来源，不冒充实时已验证姿态。"""
    from drivers.wake_screen import wake_target_digest

    if not is_screen_wake_task(task) or task.get("device_id") != device_id:
        raise ApiError("screen_wake_manual_confirm_conflict",
                       "确认只能绑定本设备的合法人工唤醒任务")
    resolved, _bundle = resolve_target(runtime, device_id)
    binding = resolved["binding"]
    if task["payload"]["binding"] != binding:
        raise ApiError("screen_wake_manual_confirm_conflict",
                       "任务绑定与当前完整校准不一致")
    target = {key: deepcopy(resolved[key]) for key in ("validation_point_id", "point")}
    digest = wake_target_digest({**target, "binding": binding})
    reference_evidence_id = "swmc-ref-" + sha256_text(json.dumps(
        [task["task_id"], task["idempotency_key"], digest,
         "published_calibration_basis"],
        sort_keys=True, ensure_ascii=False))[:24]
    return {
        "task_id": task["task_id"], "idempotency_key": task["idempotency_key"],
        "binding": deepcopy(binding), "target": target, "target_digest": digest,
        "reference_evidence_id": reference_evidence_id,
        "reference_source": "published_calibration_basis",
        "reference_pose": None,
    }


class ManualConfirmationStore:
    """§9.3(4)-(5)：内存持久化的单次人工现场确认；服务重启即撤销未消费确认。"""

    def __init__(self):
        self._records: dict[str, dict] = {}       # client_request_id -> record

    @staticmethod
    def _request_digest(request: dict) -> str:
        return sha256_text(json.dumps(request, sort_keys=True, ensure_ascii=False))

    @staticmethod
    def _public(record: dict) -> dict:
        return {"contract_version": MANUAL_CONFIRM_CONTRACT_VERSION,
                "confirmation_id": record["confirmation_id"],
                "task_id": record["task_id"], "expires_at": record["expires_at"]}

    def create(self, runtime, device_id: str, request: dict) -> tuple[int, dict]:
        try:
            manual_confirm_validator().validate(request)
        except jsonschema.ValidationError as exc:
            raise ApiError("invalid_request",
                           f"人工唤醒确认请求不符合契约：{exc.message}") from exc
        task = runtime.store.get_task(request["task_id"])
        if task is None:
            raise ApiError("task_not_found", "确认引用的唤醒任务不存在")
        runtime.devices.get(device_id)
        if task.get("device_id") != device_id:
            raise ApiError("screen_wake_manual_confirm_conflict",
                           "确认任务与设备不一致（跨设备确认拒绝）")
        if task.get("status") in TERMINAL_TASK_STATUSES:
            raise ApiError("screen_wake_manual_confirm_conflict",
                           "任务已终态，不能补签现场确认")
        if parse_iso(task["expires_at"]) <= utc_now():
            raise ApiError("screen_wake_manual_confirm_expired",
                           "唤醒任务已过期，确认引用失效")
        if (task["payload"]["binding"] != request["binding"]
                or task["idempotency_key"] != request["idempotency_key"]):
            raise ApiError("screen_wake_manual_confirm_conflict",
                           "确认的绑定或幂等键与任务不一致")
        if request["binding"]["daemon_session"] != runtime.devices.get(
                device_id).get("daemon_session"):
            raise ApiError("screen_wake_manual_confirm_conflict",
                           "当前机械坐标会话与确认绑定不一致")
        prepared = manual_confirm_reference(runtime, device_id, task)
        if request["reference_evidence_id"] != prepared["reference_evidence_id"]:
            raise ApiError("screen_wake_manual_confirm_conflict",
                           "参考证据与本次界面展示不一致，不接受请求自带参考坐标或变换")
        digest = self._request_digest(request)
        existing = self._records.get(request["client_request_id"])
        if existing is not None:
            if existing["request_digest"] != digest:
                raise ApiError(
                    "screen_wake_manual_confirm_conflict",
                    "同一 client_request_id 提交了不同内容，拒绝覆盖原确认")
            return 200, self._public(existing)  # 原记录重放，不延长有效期
        if any(record["task_id"] == request["task_id"]
               and record["consumed_at"] is None
               and parse_iso(record["expires_at"]) > utc_now()
               for record in self._records.values()):
            raise ApiError("screen_wake_manual_confirm_conflict",
                           "该任务已有一份未消费的现场确认，不得叠加")
        expires_at = iso(min(utc_now() + timedelta(seconds=MANUAL_CONFIRM_TTL_SECONDS),
                             parse_iso(task["expires_at"])))
        record = {
            # 服务端生成 ≥128 位随机确认标识；不下发运动。
            "confirmation_id": f"swmc-{secrets.token_hex(16)}",
            "task_id": task["task_id"], "device_id": device_id,
            "idempotency_key": request["idempotency_key"],
            "binding": deepcopy(request["binding"]),
            "reference_evidence_id": request["reference_evidence_id"],
            "reference_source": prepared["reference_source"],
            "reference_pose": deepcopy(prepared["reference_pose"]),
            "target_digest": prepared["target_digest"],
            "operator_id": request["operator_id"], "reason": request["reason"],
            "correlation_id": request["correlation_id"],
            "client_request_id": request["client_request_id"],
            "request_digest": digest,
            "confirmed_at": iso(utc_now()), "expires_at": expires_at,
            "consumed_at": None,
        }
        self._records[request["client_request_id"]] = record
        runtime.audit.record(
            "screen_wake_manual_confirm", operator_id=request["operator_id"],
            device_id=device_id, task_id=task["task_id"], reason=request["reason"],
            correlation_id=request["correlation_id"],
            client_request_id=request["client_request_id"])
        return 201, self._public(record)

    def consume_for_task(self, runtime, task: dict) -> dict | None:
        """任务受理边界原子消费一次；其他一切情况不授予例外。"""
        record = next((item for item in self._records.values()
                       if item["task_id"] == (task or {}).get("task_id")), None)
        if record is None or record["consumed_at"] is not None:
            return None
        if parse_iso(record["expires_at"]) <= utc_now():
            return None
        try:
            prepared = manual_confirm_reference(runtime, record["device_id"], task)
        except (ApiError, ValueError, TypeError, KeyError, OSError, ImportError):
            return None
        if (record["binding"] != task["payload"]["binding"]
                or record["binding"] != prepared["binding"]
                or record["target_digest"] != prepared["target_digest"]):
            return None
        record["consumed_at"] = iso(utc_now())
        return deepcopy(record)


def confirmations(runtime) -> ManualConfirmationStore:
    store = getattr(runtime, "screen_wake_confirmations", None)
    if not isinstance(store, ManualConfirmationStore):
        store = ManualConfirmationStore()
        runtime.screen_wake_confirmations = store
    return store


def consume_manual_confirmation(runtime, task: dict) -> dict | None:
    if not is_screen_wake_task(task):
        return None
    return confirmations(runtime).consume_for_task(runtime, task)
