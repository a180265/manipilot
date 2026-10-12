"""输入控制记录层（2026-10-03 规格 §4.8 G01 P01）。

四类持久记录（input_records 表）：owner（许可归属）、effect（逐次副作用）、
device_guard（设备恢复阻断与核对账本）、settings_write（设置提交意图）。
本模块负责：严格校验（未知 kind/版本、身份不匹配、负数/布尔版本即阻断）、
规范摘要、设备短锁、资源排他锁与受限 CAS 演化。不复制 enabled，不覆写
任务历史；设置读取仍走 runtime/input_settings.py（唯一事实源）。

P03-P08 在此之上接任务接纳、能力认领、驱动边界、IME 撤销与核对出口。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
from copy import deepcopy
import re
import threading
from pathlib import Path
from typing import Callable, Iterator

RECORD_VERSION = "input-control-record.v1"

OWNER_TYPES = ("fleet_task", "direct_ime")
REVOCATIONS = ("active", "revoked")

# §4.8.2：effect_kind / state / cancel_state 固定字典。
EFFECT_KINDS = ("ime_create", "ime_deliver", "ime_start", "candidate_commit",
                "text_type", "editor_clear", "message_send")
EFFECT_STATES = ("prepared", "claimed", "completed", "not_started", "unknown")
CANCEL_STATES = ("none", "requested", "waiting_ack", "confirmed", "not_created", "unknown")

# 恢复阻断固定码；startup_reconciling 是进程内资格，不入持久 issues。
GUARD_CODES = ("settings_commit_pending", "input_record_invalid",
               "input_effect_unknown", "ime_cancel_pending",
               "editor_review_required", "binding_change_pending",
               "legacy_input_review_required")
GUARD_ISSUE_STATES = ("open", "resolved")

WRITE_STATES = ("prepared", "applied", "aborted")
WRITE_ORIGINS = ("human_put", "human_review", "system_binding_change")

# effect 状态机：prepared 尚无下发资格；claimed 可能已下发；completed 仅代表
# 该接口调用取得可核验回执。终态不可再演化（恢复只查询不重放）。
EFFECT_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"prepared"},
    "prepared": {"claimed", "not_started"},
    # claimed→not_started 仅限驱动证实未下发（motion_not_started）的可靠情形
    "claimed": {"completed", "unknown", "not_started"},
    "completed": set(),
    "not_started": set(),
    "unknown": set(),
}

_OWNER_RE = re.compile(r"^owner:(task|ime):(?P<ref>.+)$")


class InputControlError(Exception):
    """记录层拒绝（reason_code 固定 input_record_invalid：阻断该设备输入）。"""

    def __init__(self, message: str):
        self.reason_code = "input_record_invalid"
        self.message = message
        super().__init__(f"{self.reason_code}: {message}")


class InputControlCommitUnknown(InputControlError, RuntimeError):
    """§4.8.3 提交分类：替换/同步或 DB 提交无法确认是否已提交（503）。

    继承 RuntimeError 保持崩溃窗口的进程内中断语义；对 API 暴露
    reason_code=input_settings_commit_unknown（按原请求编号对账）。
    """

    def __init__(self, message: str, reason_code: str = "input_settings_commit_unknown"):
        Exception.__init__(self, f"{reason_code}: {message}")
        self.reason_code = reason_code
        self.message = message


class InputRecordConflict(Exception):
    """CAS 版本冲突或同编号不同身份。"""

    def __init__(self, record_id: str, expected: int, actual: int):
        self.record_id = record_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"input record CAS conflict: {record_id} expected r{expected}, actual r{actual}")


def canonical_digest(value) -> str:
    """§4.8.2 统一包摘要：UTF-8 canonical JSON 的 SHA-256。"""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalized_settings_for_checkpoint(stored: dict) -> dict:
    """R2-02：checkpoint 语义摘要剔除账本 original_response 注入差异——
    原响应在提交后可能二次落盘，不影响设置语义本身。"""
    normalized = deepcopy_request(stored)
    for entry in (normalized.get("requests") or {}).values():
        if isinstance(entry, dict):
            entry.pop("original_response", None)
    return normalized


def known_device_fact(store, device_id: str, data_root) -> bool:
    """R2-02：已知输入设备统一判据（正式读写共用，调用者不得略过）。

    任一成立即已知设备：设置文件存在；迁移维护意图/备份存在；Fleet DB 存在
    本设备任何输入控制记录（owner/effect；settings_write 仅 applied——
    aborted 意图未产生过已提交版本，不阻止首次建立文件）；guard 携带版本/
    提交相关状态（检查点/换绑意图/核对账本）。纯 open issue 的 guard 不构成
    已知（否则状态投影一次只读查询就永远无法首次建立设置文件，I55 GET 503）；
    owner 复活防护由 owner/settings_write/checkpoint 判据独立保证。
    """
    from runtime import input_settings as iset

    try:
        if iset.settings_path(device_id, data_root).exists():
            return True
        if iset.known_device_by_maintenance(device_id, data_root):
            return True
    except Exception:
        pass
    try:
        for kind in ("owner", "effect"):
            if store.input_records_by_device(device_id, kind):
                return True
        for record in store.input_records_by_device(device_id, "settings_write"):
            if record.get("state") == "applied":
                return True
        guard = store.input_record_get(record_id_for_guard(device_id))
        if isinstance(guard, dict):
            if guard.get("settings_checkpoint") \
                    or guard.get("binding_change") or guard.get("review_requests"):
                return True
    except Exception:
        pass
    return False


def record_id_for_owner(owner_type: str, owner_ref: str) -> str:
    prefix = "task" if owner_type == "fleet_task" else "ime"
    return f"owner:{prefix}:{owner_ref}"


def record_id_for_effect(owner_id: str, slot_key: list) -> str:
    # 编号摘要输入用 JSON 数组，不用可能碰撞的字符串拼接。
    return "effect:" + canonical_digest([owner_id, slot_key])


def record_id_for_guard(device_id: str) -> str:
    return f"guard:{device_id}"


def record_id_for_settings_write(device_id: str, client_request_id: str) -> str:
    return "write:" + canonical_digest([device_id, client_request_id])


def _now() -> str:
    from runtime.util import iso, utc_now
    return iso(utc_now())


def _require_text(record: dict, key: str) -> None:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InputControlError(f"记录缺少 {key}")


def _require_int(record: dict, key: str, *, minimum: int = 0) -> None:
    value = record.get(key)
    if type(value) is not int or value < minimum:  # bool 被显式排除
        raise InputControlError(f"记录 {key} 必须为 ≥{minimum} 的整数")


def _require_time(record: dict, key: str, *, nullable: bool = False) -> None:
    value = record.get(key)
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        raise InputControlError(f"记录 {key} 必须为带时区时间字符串"
                                + ("或 null" if nullable else ""))


def validate_shell(record, expected_kind: str) -> None:
    if not isinstance(record, dict):
        raise InputControlError("记录必须是对象")
    if record.get("record_version") != RECORD_VERSION:
        raise InputControlError("record_version 不受支持")
    if record.get("kind") != expected_kind:
        raise InputControlError(f"kind 必须为 {expected_kind}")
    _require_text(record, "record_id")
    _require_text(record, "device_id")
    _require_int(record, "revision", minimum=1)
    _require_time(record, "created_at")
    _require_time(record, "updated_at")


def validate_owner(record: dict) -> None:
    validate_shell(record, "owner")
    owner_type = record.get("owner_type")
    if owner_type not in OWNER_TYPES:
        raise InputControlError("owner_type 不受支持")
    match = _OWNER_RE.match(str(record.get("record_id")))
    if not match or match.group("ref") != str(record.get("owner_ref")):
        raise InputControlError("owner 编号与 owner_type/owner_ref 不一致")
    for key in ("owner_ref", "ime_device_id", "payload_digest", "admitted_at"):
        _require_text(record, key)
    _require_int(record, "settings_revision", minimum=1)
    _require_int(record, "binding_revision", minimum=0)
    if record.get("revocation") not in REVOCATIONS:
        raise InputControlError("revocation 只允许 active/revoked")
    revoked_at = record.get("revoked_at_revision")
    if record["revocation"] == "revoked":
        _require_int(record, "revoked_at_revision", minimum=1)
    elif revoked_at is not None:
        raise InputControlError("active owner 的 revoked_at_revision 必须为 null")


def validate_effect(record: dict) -> None:
    validate_shell(record, "effect")
    owner_id = record.get("owner_id")
    slot_key = record.get("slot_key")
    if not isinstance(slot_key, list) or len(slot_key) != 3 \
            or not isinstance(slot_key[0], str) or type(slot_key[1]) is not int \
            or slot_key[1] < 0 or not isinstance(slot_key[2], str):
        raise InputControlError("slot_key 必须为 [字符串, ≥0 序号, effect_kind] 三元组")
    effect_kind = record.get("effect_kind")
    if effect_kind not in EFFECT_KINDS or slot_key[2] != effect_kind:
        raise InputControlError("effect_kind 不受支持或与 slot_key 不一致")
    if record.get("record_id") != record_id_for_effect(owner_id, slot_key):
        raise InputControlError("effect 编号与 [owner_id, slot_key] 摘要不一致")
    _require_text(record, "owner_id")
    _require_text(record, "payload_digest")
    if record.get("state") not in EFFECT_STATES:
        raise InputControlError("effect state 不受支持")
    if record.get("cancel_state") not in CANCEL_STATES:
        raise InputControlError("cancel_state 不受支持")
    if effect_kind in ("ime_create", "ime_deliver", "ime_start"):
        _require_text(record, "ime_task_id")
    elif record.get("ime_task_id") is not None:
        raise InputControlError("非 IME 动作 ime_task_id 必须为 null")
    _require_time(record, "prepared_at")
    for key in ("claimed_at", "finished_at"):
        _require_time(record, key, nullable=True)
    if record.get("typed_character_count") is not None:
        _require_int(record, "typed_character_count", minimum=0)
    receipt = record.get("receipt")
    if receipt is not None:
        if not isinstance(receipt, dict) or not isinstance(
                receipt.get("observations"), list):
            raise InputControlError("receipt 必须为 {observations: []} 只追加容器")
    if record["state"] in ("claimed", "completed", "unknown") \
            and record.get("claimed_at") is None:
        raise InputControlError("已认领/终态记录必须携带 claimed_at")
    if record["state"] in ("completed", "unknown") \
            and record.get("finished_at") is None:
        raise InputControlError("终态记录必须携带 finished_at")


def validate_guard(record: dict) -> None:
    validate_shell(record, "device_guard")
    if record.get("record_id") != record_id_for_guard(record["device_id"]):
        raise InputControlError("guard 编号必须为 guard:{device_id}")
    issues = record.get("issues")
    if not isinstance(issues, list):
        raise InputControlError("issues 必须为数组")
    seen = set()
    for issue in issues:
        if not isinstance(issue, dict):
            raise InputControlError("issue 必须为对象")
        for key in ("issue_id", "code", "state", "opened_at"):
            if not isinstance(issue.get(key), str) or not issue[key]:
                raise InputControlError(f"issue 缺少 {key}")
        if issue["code"] not in GUARD_CODES:
            raise InputControlError("issue code 不受支持")
        if issue["state"] not in GUARD_ISSUE_STATES:
            raise InputControlError("issue state 只允许 open/resolved")
        if issue["issue_id"] in seen:
            raise InputControlError("issue_id 重复")
        seen.add(issue["issue_id"])
        resolved_at = issue.get("resolved_at")
        if issue["state"] == "resolved":
            if not isinstance(resolved_at, str) or not resolved_at:
                raise InputControlError("resolved issue 缺少 resolved_at")
        elif resolved_at is not None or issue.get("resolution") is not None:
            raise InputControlError("open issue 不得携带 resolved_at/resolution")
    checkpoint = record.get("settings_checkpoint")
    if checkpoint is not None:
        if not isinstance(checkpoint, dict) \
                or type(checkpoint.get("revision")) is not int \
                or not isinstance(checkpoint.get("digest"), str) or not checkpoint["digest"]:
            raise InputControlError("settings_checkpoint 结构无效")
    binding_change = record.get("binding_change")
    if binding_change is not None and not isinstance(binding_change, dict):
        raise InputControlError("binding_change 必须为对象或 null")
    if not isinstance(record.get("review_requests"), dict):
        raise InputControlError("review_requests 必须为幂等账本对象")


def validate_settings_write(record: dict) -> None:
    validate_shell(record, "settings_write")
    _require_text(record, "client_request_id")
    if record.get("state") not in WRITE_STATES:
        raise InputControlError("settings_write state 不受支持")
    origin = (record.get("call_context") or {}).get("origin")
    if origin not in WRITE_ORIGINS:
        raise InputControlError("call_context.origin 不受支持")
    for key in ("operator_id", "role", "operation_id"):
        if not isinstance((record.get("call_context") or {}).get(key), str):
            raise InputControlError(f"call_context 缺少 {key}")
    for key in ("before_digest", "after_digest"):
        _require_text(record, key)
    for key in ("before_revision", "after_revision"):
        _require_int(record, key, minimum=0)
    if not isinstance(record.get("proposed_file"), dict):
        raise InputControlError("缺少完整拟提交文件 proposed_file")
    if record.get("original_response") is not None \
            and not isinstance(record["original_response"], dict):
        raise InputControlError("original_response 必须为对象或 null")
    failure = record.get("failure")
    if failure is not None and not isinstance(failure, str):
        raise InputControlError("failure 必须为字符串或 null")


VALIDATORS: dict[str, Callable[[dict], None]] = {
    "owner": validate_owner,
    "effect": validate_effect,
    "device_guard": validate_guard,
    "settings_write": validate_settings_write,
}


def deepcopy_request(value):
    return deepcopy(value)


class InputControl:
    """同一实例装配到执行器、驱动与 Fleet 挂载的 IME 仓储（§4.8.1）。

    单写入进程 + 每设备短锁；资源排他锁由 startup acquire_resource_locks 取得。
    """

    def __init__(self, store, db_path: Path | str):
        self._store = store
        self._db_path = Path(db_path)
        self._device_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._resource_lock_files: list = []
        # FIX-02：取消工作器提前唤醒（Runtime 装配时注入；关闭写入后通知）
        self._cancellation_notifier: Callable[[], None] | None = None

    def set_cancellation_notifier(self, notify: Callable[[], None] | None) -> None:
        """§4.8.6：关闭写入/对账完成后提前唤醒取消工作器（通知只是加速，
        工作器周期兜底扫描不依赖本通知）。"""
        self._cancellation_notifier = notify

    def _notify_cancellation_worker(self) -> None:
        notify = self._cancellation_notifier
        if notify is None:
            return
        try:
            notify()
        except Exception:
            pass  # 通知丢失由周期兜底扫描补偿

    # ---- R2-02：设置历史统一核对（正式 GET/写入/启动核对共同强制入口） ----
    def known_device_check(self, device_id: str, data_root):
        def known() -> bool:
            return known_device_fact(self._store, device_id, data_root)
        return known

    def assert_settings_history(self, device_id: str, stored: dict) -> None:
        """R2-02：checkpoint 历史核对——存在但回退/被替换的合法 JSON 同样拒绝。

        - 文件版本落后 checkpoint（版本回退）→ input_settings_invalid；
        - 同版本但语义摘要不同（文件被替换）→ input_settings_invalid；
        - 文件领先 checkpoint 不阻断：文件是事实源，checkpoint 由提交路径
          原子补齐（R2-02 前 legacy 半状态窗口按文件领先放行）。
        摘要比较剔除账本 original_response 注入差异；同时兼容旧检查点的
        全量摘要（含 original_response）。
        """
        from runtime import input_settings as iset
        guard = self._store.input_record_get(record_id_for_guard(device_id))
        checkpoint = guard.get("settings_checkpoint") \
            if isinstance(guard, dict) else None
        if not isinstance(checkpoint, dict):
            return
        file_revision = int(stored.get("revision") or 0)
        checkpoint_revision = int(checkpoint.get("revision") or 0)
        if file_revision < checkpoint_revision:
            raise iset.InputSettingsError(
                "input_settings_invalid",
                f"输入开关设置文件版本（r{file_revision}）落后于已核对检查点"
                f"（r{checkpoint_revision}）：疑似回退，按资料事故阻断；请从备份核对恢复")
        if file_revision == checkpoint_revision:
            normalized = canonical_digest(normalized_settings_for_checkpoint(stored))
            if normalized != checkpoint.get("digest") \
                    and canonical_digest(stored) != checkpoint.get("digest"):
                raise iset.InputSettingsError(
                    "input_settings_invalid",
                    "输入开关设置文件与已核对检查点同版本但内容不一致：疑似被替换，"
                    "按资料事故阻断；请从备份核对恢复")

    def load_settings_verified(self, device_id: str, *, data_root) -> dict:
        """R2-02 统一读取入口：known_check（已知设备文件丢失拒绝重建空白）
        + checkpoint 历史核对（回退/同版本不同内容=资料事故）。

        正式 GET、写入编排与启动核对都必须走本入口；直接调用
        input_settings.load_settings 只允许在低层工具（迁移器等）。
        """
        from runtime import input_settings as iset
        stored = iset.load_settings(
            device_id, data_root,
            known_check=self.known_device_check(device_id, data_root))
        self.assert_settings_history(device_id, stored)
        return stored

    # ---- 资源排他锁（§4.8.1：DB + 数据根；第二个实例必须启动失败） ----
    def acquire_resource_locks(self, data_roots: list[Path | str]) -> None:
        targets = {(self._db_path.resolve().parent / (self._db_path.name + ".input-owner.lock"))}
        for root in data_roots:
            root = Path(root)
            targets.add(root / ".input-owner.lock")
        for path in sorted(targets, key=str):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                for held in self._resource_lock_files:
                    held[1].close()
                self._resource_lock_files = []
                raise InputControlError(
                    f"输入控制资源锁被占用（另一实例在写同一资源）：{path}")
            self._resource_lock_files.append((path, handle))

    def release_resource_locks(self) -> None:
        for _path, handle in self._resource_lock_files:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self._resource_lock_files = []

    # ---- 设备短锁（§4.8.1：覆盖读取/认领持久化/关闭提交，不跨 await） ----
    @contextlib.contextmanager
    def device_lock(self, device_id: str) -> Iterator[None]:
        with self._locks_guard:
            lock = self._device_locks.setdefault(device_id, threading.Lock())
        with lock:
            yield

    # ---- 记录读取 ----
    def get_record(self, record_id: str, expected_kind: str | None = None) -> dict | None:
        record = self._store.input_record_get(record_id)
        if record is not None and expected_kind is not None:
            VALIDATORS[expected_kind](record)
        return record

    def records_for_device(self, device_id: str, kind: str | None = None) -> list[dict]:
        records = self._store.input_records_by_device(device_id, kind)
        validator = VALIDATORS.get(kind) if kind else None
        for record in records:
            if validator is not None:
                validator(record)
            else:
                validator_kind = record.get("kind")
                if validator_kind not in VALIDATORS:
                    raise InputControlError(f"未知记录 kind：{validator_kind}")
                VALIDATORS[validator_kind](record)
        return records

    # ---- owner（§4.8.4 admit） ----
    def build_owner(self, *, owner_type: str, owner_ref: str, device_id: str,
                    ime_device_id: str, settings_revision: int, binding_revision: int,
                    payload: dict) -> dict:
        record = {
            "record_version": RECORD_VERSION,
            "record_id": record_id_for_owner(owner_type, owner_ref),
            "device_id": device_id,
            "kind": "owner",
            "revision": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "owner_type": owner_type,
            "owner_ref": owner_ref,
            "ime_device_id": ime_device_id,
            "settings_revision": settings_revision,
            "binding_revision": binding_revision,
            "payload_digest": canonical_digest(payload),
            "admitted_at": _now(),
            "revocation": "active",
            "revoked_at_revision": None,
        }
        validate_owner(record)
        return record

    def admit_owner(self, owner: dict) -> tuple[dict, bool]:
        """同 owner_ref 同摘要=幂等返回原记录；不同摘要=冲突（P03 经由带任务事务）。"""
        stored, created = self._store.input_record_admit_owner(owner)
        validate_owner(stored)
        return stored, created

    def admit_owner_with_task(self, owner: dict, task: dict) -> tuple[dict, dict, bool]:
        owner, task, created = self._store.input_record_admit_owner_with_task(owner, task)
        validate_owner(owner)
        return owner, task, created

    def revoke_owner(self, record_id: str, *, expected_revision: int,
                     revoked_at_revision: int) -> dict:
        """撤销只能 active -> revoked；是否可用仍须核对当前撤销上界。"""

        def mutate(record: dict):
            if record["revocation"] != "active":
                raise InputControlError("owner 已撤销，不能重复撤销")
            record["revocation"] = "revoked"
            record["revoked_at_revision"] = revoked_at_revision

        record = self._store.input_record_cas(
            record_id, expected_revision, mutate)
        validate_owner(record)
        return record

    # ---- effect（§4.8.4 prepare/claim/finish/receipt） ----
    def build_effect(self, *, owner_id: str, device_id: str, slot_key: list,
                     payload: dict, ime_task_id: str | None = None) -> dict:
        effect_kind = slot_key[2]
        record = {
            "record_version": RECORD_VERSION,
            "record_id": record_id_for_effect(owner_id, slot_key),
            "device_id": device_id,
            "kind": "effect",
            "revision": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "owner_id": owner_id,
            "slot_key": list(slot_key),
            "effect_kind": effect_kind,
            "payload_digest": canonical_digest(payload),
            "state": "prepared",
            "prepared_at": _now(),
            "claimed_at": None,
            "finished_at": None,
            "receipt": None,
            "ime_task_id": ime_task_id,
            "cancel_state": "none",
            "typed_character_count": None,
        }
        validate_effect(record)
        return record

    def insert_effect(self, effect: dict) -> tuple[dict, bool]:
        stored = self._store.input_record_get(effect["record_id"])
        if stored is not None:
            validate_effect(stored)
            if stored["payload_digest"] != effect["payload_digest"]:
                raise InputControlError("同 slot 重复准备但参数摘要不同")
            return stored, False
        self._store.input_record_insert(effect)
        return effect, True

    def claim_effect(self, record_id: str, *, expected_revision: int) -> dict:
        """认领顺序点：prepared -> claimed 持久提交成功才允许下发（§4.8.4-4）。"""

        def mutate(record: dict):
            allowed = EFFECT_TRANSITIONS[record["state"]]
            if "claimed" not in allowed:
                raise InputControlError(
                    f"state {record['state']} 不允许认领（只允许 {'/'.join(sorted(allowed))}）")
            record["state"] = "claimed"
            record["claimed_at"] = _now()

        record = self._store.input_record_cas(record_id, expected_revision, mutate)
        validate_effect(record)
        return record

    def finish_effect(self, record_id: str, *, expected_revision: int,
                      state: str, typed_character_count: int | None = None,
                      cancel_state: str | None = None) -> dict:
        if state not in {"completed", "unknown"}:
            raise InputControlError("finish 只允许 completed/unknown（未下发用 not_started）")

        def mutate(record: dict):
            if state not in EFFECT_TRANSITIONS[record["state"]]:
                raise InputControlError(f"state {record['state']} 不允许迁移到 {state}")
            record["state"] = state
            record["finished_at"] = _now()
            if typed_character_count is not None:
                previous = record.get("typed_character_count")
                # 计数只进不退（§4.8.2：旧回包不能使已知输入进度倒退）
                if previous is None or typed_character_count > previous:
                    record["typed_character_count"] = typed_character_count
            if cancel_state is not None:
                if cancel_state not in CANCEL_STATES:
                    raise InputControlError("cancel_state 不受支持")
                record["cancel_state"] = cancel_state

        record = self._store.input_record_cas(record_id, expected_revision, mutate)
        validate_effect(record)
        return record

    def mark_effect_not_started(self, record_id: str, *, expected_revision: int,
                                cancel_state: str | None = None) -> dict:
        def mutate(record: dict):
            if "not_started" not in EFFECT_TRANSITIONS[record["state"]]:
                raise InputControlError(
                    f"state {record['state']} 不允许标记未开始（已认领即可能已动作）")
            record["state"] = "not_started"
            record["finished_at"] = _now()
            if cancel_state is not None:
                if cancel_state not in CANCEL_STATES:
                    raise InputControlError("cancel_state 不受支持")
                record["cancel_state"] = cancel_state

        record = self._store.input_record_cas(record_id, expected_revision, mutate)
        validate_effect(record)
        return record

    def set_cancel_state(self, record_id: str, *, expected_revision: int,
                         cancel_state: str) -> dict:
        """只改取消事实链（requested/confirmed/not_created），不动执行状态。"""

        def mutate(record: dict):
            if cancel_state not in CANCEL_STATES:
                raise InputControlError("cancel_state 不受支持")
            record["cancel_state"] = cancel_state

        record = self._store.input_record_cas(record_id, expected_revision, mutate)
        validate_effect(record)
        return record

    def append_receipt_observation(self, record_id: str, *, expected_revision: int,
                                   observation: dict) -> dict:
        """迟到回执只补全事实（只追加），不复活 owner、不降低撤销边界。"""

        def mutate(record: dict):
            receipt = record.get("receipt")
            if receipt is None:
                receipt = {"observations": []}
                record["receipt"] = receipt
            entry = dict(observation)
            entry["observed_at"] = _now()
            receipt["observations"].append(entry)

        record = self._store.input_record_cas(record_id, expected_revision, mutate)
        validate_effect(record)
        return record

    # ---- device_guard（§4.8.2/§4.8.7） ----
    def ensure_guard(self, device_id: str) -> dict:
        record_id = record_id_for_guard(device_id)
        record = self._store.input_record_get(record_id)
        if record is not None:
            validate_guard(record)
            return record
        record = {
            "record_version": RECORD_VERSION,
            "record_id": record_id,
            "device_id": device_id,
            "kind": "device_guard",
            "revision": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "issues": [],
            "settings_checkpoint": None,
            "binding_change": None,
            "review_requests": {},
        }
        validate_guard(record)
        self._store.input_record_insert(record)
        return record

    def open_guard_issue(self, device_id: str, *, code: str, owner_id: str | None = None,
                         effect_id: str | None = None, detail: dict | None = None,
                         issue_id: str | None = None) -> dict:
        guard = self.ensure_guard(device_id)
        # 幂等键只由问题内容决定（不得混入既有 issues 数量——那会让同一问题
        # 在重复上报时生成不同 ID，绕过"同一问题不重复创建"）。
        issue_id = issue_id or canonical_digest(
            [code, owner_id, effect_id, detail or {}])[:24]
        if any(issue["issue_id"] == issue_id for issue in guard["issues"]):
            return guard  # 幂等：同一问题原样返回，不递增版本

        def mutate(record: dict):
            if any(issue["issue_id"] == issue_id for issue in record["issues"]):
                return  # 幂等：同一问题不重复创建（§4.8.6-6）
            record["issues"].append({
                "issue_id": issue_id, "code": code,
                "owner_id": owner_id, "effect_id": effect_id,
                "state": "open", "opened_at": _now(),
                "resolved_at": None, "resolution": None,
                **({"detail": detail} if detail else {}),
            })

        record = self._store.input_record_cas(
            guard["record_id"], guard["revision"], mutate)
        validate_guard(record)
        return record

    def resolve_guard_issue(self, device_id: str, *, issue_id: str,
                            resolution: dict) -> dict:
        guard = self.ensure_guard(device_id)

        def mutate(record: dict):
            for issue in record["issues"]:
                if issue["issue_id"] != issue_id:
                    continue
                if issue["state"] == "resolved":
                    return  # 幂等：原样保留 resolved 与原审计
                issue["state"] = "resolved"
                issue["resolved_at"] = _now()
                issue["resolution"] = resolution
                return
            raise InputControlError(f"未找到问题 {issue_id}")

        record = self._store.input_record_cas(
            guard["record_id"], guard["revision"], mutate)
        validate_guard(record)
        return record

    def set_settings_checkpoint(self, device_id: str, *, revision: int,
                                digest: str) -> dict:
        guard = self.ensure_guard(device_id)

        def mutate(record: dict):
            record["settings_checkpoint"] = {"revision": revision, "digest": digest}

        record = self._store.input_record_cas(
            guard["record_id"], guard["revision"], mutate)
        validate_guard(record)
        return record

    # §4.8.3/FIX-04：换手机 binding_change 意图（阻断新输入直到流程走完）。
    BINDING_CHANGE_PHASES = ("prepared", "closed", "reconciling", "applied")

    def set_binding_change(self, device_id: str, *, expected_revision: int,
                           binding_change: dict | None) -> dict:
        """持久保存/推进 guard.binding_change 意图（phase 演进留痕）。

        binding_change 固定含 operation_id/old_ime_device_id/new_ime_device_id/
        phase；phase 只允许 prepared/closed/reconciling/applied。 None 清除
        （仅在 applied 收尾后由换绑流程本身使用）。
        """
        if binding_change is not None:
            if not isinstance(binding_change, dict):
                raise InputControlError("binding_change 必须为对象或 null")
            if binding_change.get("phase") not in self.BINDING_CHANGE_PHASES:
                raise InputControlError("binding_change.phase 不受支持")
            for key in ("operation_id", "old_ime_device_id", "new_ime_device_id"):
                if not isinstance(binding_change.get(key), str) or not binding_change[key]:
                    raise InputControlError(f"binding_change 缺少 {key}")
        guard = self.ensure_guard(device_id)

        def mutate(record: dict):
            record["binding_change"] = deepcopy_request(binding_change)

        record = self._store.input_record_cas(
            guard["record_id"], expected_revision, mutate)
        validate_guard(record)
        return record

    # §4.8.7：人工核对出口（零机械动作）
    PROTECTED_ISSUE_CODES = {"settings_commit_pending", "input_record_invalid",
                             "binding_change_pending"}

    def review_input_recovery(self, device_id: str, *, expected_revision: int,
                              issue_ids: list, resolution: dict,
                              client_request_id: str, operator_id: str,
                              call_context: dict | None = None) -> dict:
        """§4.8.7 人工核对出口（R2-22 原子化重写）。

        - 仅解除列明的 open 问题；提交/损坏/换绑类不可勾选清除；
        - 摘要覆盖请求内容（issue_ids 顺序/resolution/operator/调用上下文），
          不只 issue_ids+operator；expected_revision 只作首次申请的 CAS、
          **不计入摘要**（核对推进 revision 后刷新重发同内容仍返回原结果，
          §4.8.7 重放语义；复核条件闭合项 2）；
        - 安全前置：原执行仍在途（prepared/claimed 且取消未确认/未证实未创建）
          → input_recovery_busy，不释放；
        - 账本记录、全部 issue 解决与原响应在同一 guard 事务（单 CAS），
          同号重试先对账：recorded（中断遗留）保守补齐，不当完成。
        """
        if not issue_ids or len(set(issue_ids)) != len(issue_ids):
            raise InputControlError("issue_ids 必须为非空且不重复的列表")
        guard = self.ensure_guard(device_id)
        # 摘要不含 expected_revision：首次申请按 CAS 核对；核对推进 guard
        # revision 后，客户端刷新 revision 重发同内容仍须返回原结果（§4.8.7
        # "重放返回完全相同结果"；R2-22 冲突项=issue 顺序/resolution/操作者
        # 等内容变化）。call_context 由服务端注入，不受客户端控制。
        digest = canonical_digest({
            "issue_ids": [str(i) for i in issue_ids],
            "resolution": deepcopy_request(resolution),
            "operator_id": operator_id,
            "call_context": dict(call_context or {})})
        existing = guard.get("review_requests", {}).get(client_request_id)
        if existing is not None:
            if existing.get("digest") != digest:
                # 兼容第一轮遗留 recorded 账本（旧摘要只含 issue_ids+operator）：
                # 同号且这两项一致才允许按原证据保守补齐；其余一律冲突。
                legacy_digest = canonical_digest({"issue_ids": list(issue_ids),
                                                  "operator_id": operator_id})
                if not (existing.get("state") == "recorded"
                        and existing.get("digest") == legacy_digest):
                    error = InputControlError("同核对编号但内容不同：疑似重放/冲突")
                    error.reason_code = "request_conflict"
                    raise error
            return self._settle_review_request(
                device_id, client_request_id, existing, issue_ids, resolution)
        if guard["revision"] != expected_revision:
            error = InputControlError(
                f"恢复版本已变化（当前 r{guard['revision']}）：请刷新后按原编号核对")
            error.reason_code = "revision_conflict"
            raise error
        in_flight = self._in_flight_effect(device_id)
        if in_flight is not None:
            error = InputControlError(
                f"原执行仍在途（effect {in_flight['effect_kind']} "
                f"state={in_flight['state']} cancel={in_flight['cancel_state']}）："
                "先取消/对账，再执行人工核对")
            error.reason_code = "input_recovery_busy"
            raise error
        captured: dict = {}

        def mutate(record: dict):
            open_map = {i["issue_id"]: i for i in record["issues"]
                        if i["state"] == "open"}
            for issue_id in issue_ids:
                issue = open_map.get(issue_id)
                if issue is None:
                    err = InputControlError(f"问题 {issue_id} 不存在或已解决")
                    err.reason_code = "request_conflict"
                    raise err
                if issue["code"] in self.PROTECTED_ISSUE_CODES:
                    err = InputControlError(
                        f"问题 {issue_id}（{issue['code']}）不可由人工勾选清除："
                        "只能由提交对账、资料修复或换绑流程解决")
                    err.reason_code = "issue_not_reviewable"
                    raise err
            new_revision = record["revision"] + 1
            entry = {"digest": digest,
                     "issue_ids": [str(i) for i in issue_ids],
                     "operator_id": operator_id, "at": _now(),
                     "state": "completed",
                     "resolution": deepcopy_request(resolution),
                     "response": {"recovery_revision": new_revision,
                                  "issue_ids": [str(i) for i in issue_ids]}}
            record["review_requests"][client_request_id] = entry
            for issue_id in issue_ids:
                issue = open_map[issue_id]
                issue["state"] = "resolved"
                issue["resolved_at"] = _now()
                issue["resolution"] = deepcopy_request(resolution)
            captured["entry"] = entry

        record = self._store.input_record_cas(
            guard["record_id"], guard["revision"], mutate)
        validate_guard(record)
        entry = captured["entry"]
        return {"recovery_revision": entry["response"]["recovery_revision"],
                "issue_ids": list(entry["issue_ids"]),
                "review_request": entry}

    def _in_flight_effect(self, device_id: str) -> dict | None:
        """R2-22：仍在途的输入 effect（人工核对的安全前置：无真实占用）。"""
        for record in self._store.input_records_by_device(device_id, "effect"):
            validate_effect(record)
            if record["state"] in ("prepared", "claimed") \
                    and record.get("cancel_state") not in ("confirmed", "not_created"):
                return record
        return None

    def _settle_review_request(self, device_id: str, client_request_id: str,
                               existing: dict, issue_ids: list,
                               resolution: dict) -> dict:
        """同号重放对账：completed 且列明问题全部解决→原结果原样返回；
        recorded/部分未决→保守补齐（不可人工解决的问题保持保护）。"""
        guard = self.ensure_guard(device_id)
        states = {i["issue_id"]: i for i in guard["issues"]}
        unresolved = [issue_id for issue_id in issue_ids
                      if states.get(issue_id, {}).get("state") != "resolved"]
        if existing.get("state") == "completed" and not unresolved:
            response = existing.get("response") or {}
            return {"recovery_revision": int(response.get("recovery_revision")
                                            or guard["revision"]),
                    "issue_ids": list(issue_ids),
                    "review_request": existing}
        # 需要补齐：先安全前置，再逐项保守核对（不能为放行清除保护性问题）
        in_flight = self._in_flight_effect(device_id)
        if in_flight is not None:
            error = InputControlError(
                f"原执行仍在途（effect {in_flight['effect_kind']} "
                f"state={in_flight['state']} cancel={in_flight['cancel_state']}）："
                "先取消/对账，再执行人工核对")
            error.reason_code = "input_recovery_busy"
            raise error
        for issue_id in unresolved:
            issue = states.get(issue_id)
            if issue is not None and issue["code"] in self.PROTECTED_ISSUE_CODES:
                error = InputControlError(
                    f"遗留核对请求包含不可人工解决的问题 {issue_id}"
                    f"（{issue['code']}）：保持保护")
                error.reason_code = "issue_not_reviewable"
                raise error
        captured: dict = {}

        def mutate(record: dict):
            current = {i["issue_id"]: i for i in record["issues"]}
            for issue_id in issue_ids:
                issue = current.get(issue_id)
                if issue is None:
                    err = InputControlError(
                        f"问题 {issue_id} 已不存在，遗留核对记录保持待核对")
                    err.reason_code = "input_recovery_conflict"
                    raise err
                if issue["state"] == "open":
                    issue["state"] = "resolved"
                    issue["resolved_at"] = _now()
                    issue["resolution"] = deepcopy_request(resolution)
            new_revision = record["revision"] + 1
            updated = deepcopy_request(existing)
            updated["state"] = "completed"
            updated["issue_ids"] = [str(i) for i in issue_ids]
            updated["settlement"] = {"at": _now(),
                                     "resolution": deepcopy_request(resolution)}
            updated["response"] = {"recovery_revision": new_revision,
                                   "issue_ids": [str(i) for i in issue_ids]}
            record["review_requests"][client_request_id] = updated
            captured["entry"] = updated

        record = self._store.input_record_cas(
            guard["record_id"], guard["revision"], mutate)
        validate_guard(record)
        entry = captured["entry"]
        return {"recovery_revision": entry["response"]["recovery_revision"],
                "issue_ids": list(entry["issue_ids"]),
                "review_request": entry}

    def open_input_review(self, device_id: str, *, client_request_id: str,
                          issue_ids: list, operator_id: str) -> dict:
        """§4.8.7 人工核对幂等账本：同编号同内容=原结果，编号改内容=冲突。"""
        guard = self.ensure_guard(device_id)
        digest = canonical_digest({"issue_ids": issue_ids, "operator_id": operator_id})
        existing = guard["review_requests"].get(client_request_id)
        if existing is not None:
            if existing.get("digest") != digest:
                raise InputControlError("同核对编号但内容不同：疑似重放/冲突")
            return existing
        request = {"digest": digest, "issue_ids": list(issue_ids),
                   "operator_id": operator_id, "at": _now(),
                   "state": "recorded"}

        def mutate(record: dict):
            record["review_requests"][client_request_id] = deepcopy_request(request)

        record = self._store.input_record_cas(
            guard["record_id"], guard["revision"], mutate)
        validate_guard(record)
        return record["review_requests"][client_request_id]

    # ---- settings_write（§4.8.3 提交意图与对账材料） ----
    def build_settings_write(self, *, device_id: str, client_request_id: str,
                             call_context: dict, normalized_request: dict,
                             before: dict, after: dict,
                             proposed_file: dict,
                             before_file_digest: str | None = None) -> dict:
        record = {
            "record_version": RECORD_VERSION,
            "record_id": record_id_for_settings_write(device_id, client_request_id),
            "device_id": device_id,
            "kind": "settings_write",
            "revision": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "client_request_id": client_request_id,
            "call_context": dict(call_context),
            "normalized_request": deepcopy_request(normalized_request),
            "before_digest": canonical_digest(before),
            "after_digest": canonical_digest(after),
            "before_revision": int(before.get("revision") or 0),
            "after_revision": int(after.get("revision") or 0),
            "proposed_file": deepcopy_request(proposed_file),
            "original_response": None,
            "state": "prepared",
            "failure": None,
            "before_file_digest": before_file_digest,
        }
        validate_settings_write(record)
        return record

    def insert_settings_write(self, record: dict) -> tuple[dict, bool]:
        stored = self._store.input_record_get(record["record_id"])
        if stored is not None:
            validate_settings_write(stored)
            return stored, False
        self._store.input_record_insert(record)
        return record, True

    def mark_settings_write(self, record_id: str, *, expected_revision: int,
                            state: str, original_response: dict | None = None,
                            failure: str | None = None) -> dict:
        if state not in {"applied", "aborted"}:
            raise InputControlError("mark 只允许 applied/aborted")

        def mutate(record: dict):
            if record["state"] != "prepared":
                raise InputControlError("prepared 意图只能标记一次")
            record["state"] = state
            if original_response is not None:
                record["original_response"] = deepcopy_request(original_response)
            if failure is not None:
                record["failure"] = failure

        record = self._store.input_record_cas(record_id, expected_revision, mutate)
        validate_settings_write(record)
        return record


    # ---- §4.8.3 设置写入编排（P02）：意图 prepared → 落盘 → applied + checkpoint ----
    def _settings_intent_for_request(self, device_id: str,
                                     client_request_id: str) -> dict | None:
        for record in self._store.input_records_by_device(device_id, "settings_write"):
            validate_settings_write(record)
            if record["client_request_id"] == client_request_id:
                return record
        return None

    def has_prepared_intent(self, device_id: str) -> dict | None:
        for record in self._store.input_records_by_device(device_id, "settings_write"):
            validate_settings_write(record)
            if record["state"] == "prepared":
                return record
        return None

    def write_settings(self, device_id: str, *, data_root, request: dict,
                       observe=None, permission_denied: str | None = None,
                       call_context: dict | None = None,
                       calibration_ready: bool = True,
                       binding_state: str | None = None) -> dict:
        """设置写入唯一编排入口（§4.8.3；API/插件/代理不得各自实现）。

        FIX-04 两阶段（§4.8.3-5/6）：
        - 意图 prepared 先保存完整 proposed_file **和 original_response**——
          原响应在文件/DB 提交之前从拟提交文件构造，绝不事后补造；
        - 随后单次原子落盘（含完整原请求账本/原响应）并同步目录；
        - 同设备存在未对账 prepared 意图：编号不同=阻断新请求；编号相同=
          转崩溃对账（摘要比较）；prepared 意图存在即阻断新输入认领（P04）。
        - 提交失败分类（F04-2）：替换可证实未提交且 aborted 已保存才报
          write_failed；替换/同步或 DB 提交无法确认报 commit_unknown，
          意图保持 prepared，按原请求编号对账。
        """
        from runtime import input_settings as iset
        from runtime.input_settings import InputSettingsError

        client_request_id = str(request.get("client_request_id") or "").strip()
        if not client_request_id:
            raise InputControlError("设置写入缺少 client_request_id")
        with self.device_lock(device_id):
            pending = self.has_prepared_intent(device_id)
            if pending is not None:
                if pending["client_request_id"] != client_request_id:
                    error = InputControlError(
                        "同设备存在未对账的设置提交意图：先按原请求编号对账，不得越过提交")
                    error.reason_code = "input_settings_commit_pending"
                    raise error
                # §4.8.3-5：同编号重放先按文件摘要对账（不经 iset 账本重放遮蔽）
                return self._reconcile_settings_intent(
                    device_id, pending, data_root=data_root, observe=observe)
            historical = self._settings_intent_for_request(device_id, client_request_id)
            if historical is not None and historical["state"] == "aborted":
                # §4.8.3-7：aborted 请求重放只返回原失败，不自动再执行
                error = InputControlError(
                    f"原请求已失败（不自动再执行）：{historical.get('failure')}")
                error.reason_code = "input_settings_write_failed"
                raise error
            # R2-02：写入前先走统一历史核对入口（已知设备文件丢失/回退拒绝；
            # prepare_update 内的二次读取也携带 known_check，双保险防窗口）。
            self.load_settings_verified(device_id, data_root=data_root)
            try:
                prepared = iset.prepare_update(
                    device_id, data_root=data_root,
                    enabled=request.get("enabled"),
                    ime_device_id=request.get("ime_device_id"),
                    expected_revision=request.get("expected_revision"),
                    operator_id=request.get("operator_id"),
                    reason=request.get("reason"),
                    client_request_id=client_request_id,
                    calibration_ready=calibration_ready,
                    binding_state=binding_state,
                    permission_denied=permission_denied,
                    correlation_id=request.get("correlation_id"),
                    call_context=call_context,
                    known_check=self.known_device_check(device_id, data_root))
            except InputSettingsError:
                raise
            if prepared["kind"] == "replay":
                return {"settings": prepared["settings"], "replayed": True,
                        "outcome": "replayed"}
            stored = prepared["stored"]
            entry = stored["requests"][client_request_id]
            # §4.8.3-5：原响应在提交前从拟提交文件构造（拟提交视图内调用观察器，
            # 观察器读-己-写；构造失败退回持久字段简化视图）——不能等文件/DB
            # 已提交后才生成原响应，更不能伪造。
            original_response = None
            if observe is not None:
                try:
                    with iset.proposed_view(device_id, stored):
                        original_response = observe(device_id, stored)
                except Exception:
                    original_response = None
            if original_response is None:
                from runtime.input_settings import public_settings
                original_response = {**public_settings(stored),
                                     "effective": request.get("enabled") is True}
            entry["original_response"] = deepcopy_request(original_response)
            context = dict(call_context or {
                "origin": "human_put", "operator_id": request.get("operator_id"),
                "role": "operator", "operation_id": "input_settings_update"})
            intent_record = self.build_settings_write(
                device_id=device_id, client_request_id=client_request_id,
                call_context=context,
                normalized_request=request,
                before=entry["before"], after=entry["after"],
                proposed_file=stored,
                before_file_digest=prepared["before_file_digest"])
            # prepared 意图自插入起即携带完整 proposed_file+original_response：
            # 崩溃对账（_reconcile_settings_intent）用保存的原响应回放。
            intent_record["original_response"] = deepcopy_request(original_response)
            validate_settings_write(intent_record)
            intent, _created = self.insert_settings_write(intent_record)
            disk_path = iset.settings_path(device_id, data_root)
            try:
                iset.commit_update(device_id, prepared, data_root=data_root)
            except Exception as exc:
                # 分类（F04-2）：磁盘可证明仍在 before（或本就没有文件）才允许
                # aborted + write_failed；否则替换/同步无法确认 → commit_unknown。
                disk_digest = iset.file_digest(disk_path)
                before_digest = prepared.get("before_file_digest")
                provably_not_committed = (
                    (before_digest is None and disk_digest is None)
                    or (before_digest is not None and disk_digest == before_digest))
                if provably_not_committed:
                    try:
                        self.mark_settings_write(
                            intent["record_id"],
                            expected_revision=intent["revision"],
                            state="aborted", failure=str(exc))
                    except Exception as mark_exc:
                        raise InputControlCommitUnknown(
                            f"设置提交失败且失败意图无法持久化，按未知处理：{mark_exc}")
                    error = InputControlError(f"设置持久化失败（未提交）：{exc}")
                    error.reason_code = "input_settings_write_failed"
                    raise error
                raise InputControlCommitUnknown(
                    f"设置替换/同步结果无法确认（文件可能已提交），按原请求对账：{exc}")
            try:
                # R2-02：applied 与 settings_checkpoint 单事务提交（半状态封死）
                self._apply_settings_commit(
                    device_id, write_record_id=intent["record_id"],
                    write_expected_revision=intent["revision"],
                    original_response=original_response,
                    checkpoint_revision=int(stored["revision"]),
                    checkpoint_digest=canonical_digest(
                        normalized_settings_for_checkpoint(stored)))
            except Exception as exc:
                # 文件已提交、DB 提交无法确认：保持 prepared，重启/同编号对账补齐。
                raise InputControlCommitUnknown(
                    f"设置已落盘但提交意图/checkpoint 无法确认，按原请求对账：{exc}")
            # §4.8.6：撤销边界推进后提前唤醒取消工作器（丢通知由周期扫描兜底）
            self._notify_cancellation_worker()
            return {"settings": deepcopy(original_response), "replayed": False,
                    "outcome": "applied"}

    def _apply_settings_commit(self, device_id: str, *, write_record_id: str,
                               write_expected_revision: int,
                               original_response: dict | None,
                               checkpoint_revision: int,
                               checkpoint_digest: str) -> tuple[dict, dict]:
        """R2-02：settings_write→applied 与 guard.settings_checkpoint 的
        单事务统一收口——两者间任何故障都不会留下 applied-无-checkpoint。"""
        guard = self.ensure_guard(device_id)
        write_record, guard_record = self._store.input_record_apply_settings(
            write_record_id=write_record_id,
            write_expected_revision=write_expected_revision,
            original_response=original_response,
            guard_record_id=guard["record_id"],
            guard_expected_revision=int(guard["revision"]),
            checkpoint_revision=checkpoint_revision,
            checkpoint_digest=checkpoint_digest)
        validate_settings_write(write_record)
        validate_guard(guard_record)
        return write_record, guard_record

    def _reconcile_settings_intent(self, device_id: str, pending: dict, *,
                                   data_root, observe=None) -> dict:
        """§4.8.3-5/6：重启/重放时按文件摘要对账 prepared 意图。"""
        from runtime import input_settings as iset

        disk_path = iset.settings_path(device_id, data_root)
        disk = iset.file_digest(disk_path)
        proposed = pending["proposed_file"]
        after_digest = canonical_digest(proposed)
        # 文件可能在二次落盘时注入 ledger 的 original_response：语义比较时剔除
        def _matches_after() -> bool:
            if disk == after_digest:
                return True
            try:
                on_disk = json.loads(disk_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            # FIX-04：单次写入后文件自带的 ledger 已含 original_response——
            # 先按原样语义比较，再退回剔除 original_response 的旧对账路径。
            if canonical_digest(on_disk) == after_digest:
                return True
            normalized = deepcopy(on_disk)
            requests = normalized.get("requests") or {}
            entry = requests.get(pending["client_request_id"])
            if isinstance(entry, dict):
                entry.pop("original_response", None)
            return canonical_digest(normalized) == after_digest

        if _matches_after():
            original_response = pending["original_response"]
            if not isinstance(original_response, dict):
                from runtime.input_settings import public_settings
                original_response = {
                    **public_settings(pending["proposed_file"]),
                    "effective": bool(pending["proposed_file"].get("enabled"))}
            self._apply_settings_commit(
                device_id, write_record_id=pending["record_id"],
                write_expected_revision=pending["revision"],
                original_response=original_response,
                checkpoint_revision=int(pending["after_revision"]),
                checkpoint_digest=canonical_digest(
                    normalized_settings_for_checkpoint(pending["proposed_file"])))
            self._notify_cancellation_worker()
            return {"settings": deepcopy(original_response),
                    "replayed": True, "outcome": "applied"}
        if disk == pending.get("before_file_digest"):
            if pending["after_revision"] and \
                    (pending["proposed_file"].get("requests") or {}) \
                    .get(pending["client_request_id"], {}).get("enabled") is False:
                # 匹配 before 且原请求是关闭：完成这次已授权的关闭写入
                proposed = deepcopy_request(pending["proposed_file"])
                iset._atomic_write(iset.settings_path(device_id, data_root), proposed)
                self._apply_settings_commit(
                    device_id, write_record_id=pending["record_id"],
                    write_expected_revision=pending["revision"],
                    original_response=pending["original_response"],
                    checkpoint_revision=int(pending["after_revision"]),
                    checkpoint_digest=canonical_digest(
                        normalized_settings_for_checkpoint(proposed)))
                self._notify_cancellation_worker()
                return {"settings": deepcopy(pending["original_response"] or {}),
                        "replayed": True, "outcome": "applied"}
            # 匹配 before 且原请求是开启：禁止恢复程序代替操作者开启
            self.mark_settings_write(pending["record_id"],
                                     expected_revision=pending["revision"],
                                     state="aborted",
                                     failure="prepared-intent-before-state-open-request")
            return {"settings": None, "replayed": False, "outcome": "aborted"}
        error = InputControlError(
            "设置提交意图与磁盘文件都不匹配：保留阻断，走备份核对修复")
        error.reason_code = "input_settings_commit_unknown"
        raise error
