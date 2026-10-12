"""输入控制记录层单元测试（2026-10-03 规格 §4.8 P01：I38/I39）。

覆盖：单写入（同编号幂等/异摘要冲突）、CAS 版本条件与状态机、四类记录
严格校验、设备短锁串行化、旧任务副本不覆写记录（save_task 不能碰
input_records）。
"""
from __future__ import annotations

import sys
import threading

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime.input_control import (
    EFFECT_KINDS, GUARD_CODES, InputControl, InputControlError,
    InputRecordConflict, canonical_digest, record_id_for_effect,
    record_id_for_guard, record_id_for_owner, record_id_for_settings_write,
)
from runtime.store import Store


@pytest.fixture()
def control(tmp_path):
    store = Store(tmp_path / "fleet.db")
    return InputControl(store, tmp_path / "fleet.db"), store


def _owner(device_id="arm-01", owner_ref="task-1", payload=None, **extra):
    base = dict(owner_type="fleet_task", owner_ref=owner_ref, device_id=device_id,
                ime_device_id="k40-1", settings_revision=3, binding_revision=7,
                payload=payload or {"text": "hi"})
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# I38：单写入与接纳幂等
# ---------------------------------------------------------------------------

def test_owner_admit_is_idempotent_for_same_payload_and_conflicts_on_different(control):
    ctrl, store = control
    owner = ctrl.build_owner(**_owner())
    stored, created = ctrl.admit_owner(owner)
    assert created is True and stored["revocation"] == "active"
    replay, created2 = ctrl.admit_owner(owner)
    assert created2 is False and replay["record_id"] == owner["record_id"]
    assert store.input_record_get(owner["record_id"])["revision"] == 1
    impostor = ctrl.build_owner(**_owner(payload={"text": "different"}))
    with pytest.raises(InputRecordConflict):
        ctrl.admit_owner(impostor)


def test_owner_admit_with_task_creates_both_in_one_transaction(control):
    ctrl, store = control
    owner = ctrl.build_owner(**_owner())
    task = {"task_id": "t-1", "idempotency_key": "k-1", "status": "queued",
            "payload_hash": "hash-1"}
    _, task, created = ctrl.admit_owner_with_task(owner, task)
    assert created is True
    replay_owner, replay_task, created2 = ctrl.admit_owner_with_task(
        owner, {**task, "status": "executing"})  # 重放只取旧任务，不覆盖原状态
    assert created2 is False
    assert replay_task["status"] == "queued"
    assert store.get_task("t-1")["status"] == "queued"
    assert store.input_record_get(owner["record_id"])["revision"] == 1
    # 幂等键相同内容不同 → 明确冲突，不写半条
    bad = {**task, "task_id": "t-2", "payload_hash": "hash-2"}
    with pytest.raises(Exception):
        ctrl.admit_owner_with_task(ctrl.build_owner(**_owner(owner_ref="task-2")), bad)
    assert store.input_record_get(record_id_for_owner("fleet_task", "task-2")) is None


def test_owner_revocation_only_active_to_revoked(control):
    ctrl, store = control
    owner, _ = ctrl.admit_owner(ctrl.build_owner(**_owner()))
    revoked = ctrl.revoke_owner(
        owner["record_id"], expected_revision=1, revoked_at_revision=5)
    assert revoked["revocation"] == "revoked"
    assert revoked["revoked_at_revision"] == 5
    with pytest.raises(InputControlError):
        ctrl.revoke_owner(owner["record_id"], expected_revision=2,
                          revoked_at_revision=6)
    assert store.input_record_get(owner["record_id"])["revision"] == 2


# ---------------------------------------------------------------------------
# effect：prepare/claim/finish 状态机 + CAS
# ---------------------------------------------------------------------------

def _effect(ctrl, owner, kind="text_type", seq=0):
    slot_key = ["1", seq, kind]
    ime_task_id = f"{owner['owner_ref']}-ime" if kind.startswith("ime_") else None
    effect, created = ctrl.insert_effect(ctrl.build_effect(
        owner_id=owner["record_id"], device_id=owner["device_id"],
        slot_key=slot_key, payload={"text": "hi"}, ime_task_id=ime_task_id))
    return effect, created


def test_effect_prepare_claim_finish_happy_path(control):
    ctrl, store = control
    owner, _ = ctrl.admit_owner(ctrl.build_owner(**_owner()))
    effect, created = _effect(ctrl, owner)
    assert created is True
    # 同 slot 同参数幂等
    again, created2 = _effect(ctrl, owner)
    assert created2 is False and again["record_id"] == effect["record_id"]
    claimed = ctrl.claim_effect(effect["record_id"], expected_revision=1)
    assert claimed["state"] == "claimed" and claimed["claimed_at"]
    # prepared -> completed 直跳不允许：用另一条全新 prepared effect 验证
    fresh, _ = _effect(ctrl, owner, seq=1)
    with pytest.raises(InputControlError):
        ctrl.finish_effect(fresh["record_id"], expected_revision=1,
                           state="completed")
    done = ctrl.finish_effect(effect["record_id"], expected_revision=2,
                              state="completed", typed_character_count=2)
    assert done["state"] == "completed" and done["finished_at"]
    # 终态不可再演化；迟到回执只追加观察，不复活
    with pytest.raises((InputControlError, InputRecordConflict)):
        ctrl.claim_effect(effect["record_id"], expected_revision=3)
    noted = ctrl.append_receipt_observation(
        effect["record_id"], expected_revision=3,
        observation={"source": "ime", "event": "inserted", "typedCharacterCount": 2})
    assert noted["receipt"]["observations"][-1]["event"] == "inserted"
    # 字符计数只进不退
    noted2 = ctrl.append_receipt_observation(
        effect["record_id"], expected_revision=4,
        observation={"source": "ime", "event": "progress", "typedCharacterCount": 1})
    assert noted2["typed_character_count"] == 2


def test_effect_not_started_only_from_prepared_and_record_id_is_digest(control):
    ctrl, _ = control
    owner, _ = ctrl.admit_owner(ctrl.build_owner(**_owner()))
    effect, _ = _effect(ctrl, owner, kind="ime_create", seq=0)
    assert effect["record_id"] == record_id_for_effect(
        owner["record_id"], ["1", 0, "ime_create"])
    assert effect["ime_task_id"] is None or effect["ime_task_id"]
    not_started = ctrl.mark_effect_not_started(
        effect["record_id"], expected_revision=1)
    assert not_started["state"] == "not_started"
    owner2, _ = ctrl.admit_owner(ctrl.build_owner(**_owner(owner_ref="task-2")))
    click, _ = _effect(ctrl, owner2, kind="ime_create", seq=0)
    # claimed→not_started 仅限驱动证实未下发（§4.8.4-5 可靠 not_started）
    claimed = ctrl.claim_effect(click["record_id"], expected_revision=1)
    reliable = ctrl.mark_effect_not_started(claimed["record_id"],
                                            expected_revision=2)
    assert reliable["state"] == "not_started"


def test_same_slot_different_payload_is_rejected(control):
    ctrl, _ = control
    owner, _ = ctrl.admit_owner(ctrl.build_owner(**_owner()))
    ctrl.insert_effect(ctrl.build_effect(
        owner_id=owner["record_id"], device_id="arm-01",
        slot_key=["1", 0, "text_type"], payload={"text": "a"}))
    with pytest.raises(InputControlError):
        ctrl.insert_effect(ctrl.build_effect(
            owner_id=owner["record_id"], device_id="arm-01",
            slot_key=["1", 0, "text_type"], payload={"text": "b"}))


# ---------------------------------------------------------------------------
# device_guard：issues/checkpoint/review 账本
# ---------------------------------------------------------------------------

def test_guard_issue_lifecycle_and_review_ledger(control):
    ctrl, store = control
    guard = ctrl.ensure_guard("arm-01")
    assert guard["issues"] == []
    issue = ctrl.open_guard_issue(
        "arm-01", code="ime_cancel_pending", owner_id="owner:task:t-1",
        detail={"taskId": "t-1-ime"})
    again = ctrl.open_guard_issue(
        "arm-01", code="ime_cancel_pending", owner_id="owner:task:t-1",
        detail={"taskId": "t-1-ime"})
    assert again["revision"] == issue["revision"], "同一问题幂等不重复创建"
    resolved = ctrl.resolve_guard_issue(
        "arm-01", issue_id=issue["issues"][0]["issue_id"],
        resolution={"fact": "人工核对候选已移除"})
    assert resolved["issues"][0]["state"] == "resolved"
    ctrl.set_settings_checkpoint("arm-01", revision=4, digest="d" * 64)
    stored = store.input_record_get(record_id_for_guard("arm-01"))
    assert stored["settings_checkpoint"] == {"revision": 4, "digest": "d" * 64}
    request = ctrl.open_input_review(
        "arm-01", client_request_id="review-1", issue_ids=[issue["issues"][0]["issue_id"]],
        operator_id="操作员")
    replay = ctrl.open_input_review(
        "arm-01", client_request_id="review-1",
        issue_ids=[issue["issues"][0]["issue_id"]], operator_id="操作员")
    assert replay["digest"] == request["digest"]
    with pytest.raises(InputControlError):
        ctrl.open_input_review("arm-01", client_request_id="review-1",
                               issue_ids=["other"], operator_id="操作员")


# ---------------------------------------------------------------------------
# settings_write：prepared → applied/aborted 单次标记
# ---------------------------------------------------------------------------

def test_settings_write_intent_lifecycle(control):
    ctrl, store = control
    record = ctrl.build_settings_write(
        device_id="arm-01", client_request_id="req-1",
        call_context={"origin": "human_put", "operator_id": "op", "role": "operator",
                      "operation_id": "input_settings_update"},
        normalized_request={"enabled": True},
        before={"revision": 0, "enabled": False},
        after={"revision": 1, "enabled": True},
        proposed_file={"contract_version": "device-input-settings.v1", "revision": 1})
    stored, created = ctrl.insert_settings_write(record)
    assert created is True
    replay, created2 = ctrl.insert_settings_write(record)
    assert created2 is False
    applied = ctrl.mark_settings_write(
        record["record_id"], expected_revision=1, state="applied",
        original_response={"settings": {}, "replayed": False})
    assert applied["state"] == "applied"
    with pytest.raises(InputControlError):
        ctrl.mark_settings_write(record["record_id"], expected_revision=2,
                                 state="aborted")


# ---------------------------------------------------------------------------
# 校验负例与记录不可覆写（I39）
# ---------------------------------------------------------------------------

def test_validation_rejects_unknown_kind_bad_digest_and_boolean_revision(control):
    ctrl, store = control
    owner, _ = ctrl.admit_owner(ctrl.build_owner(**_owner()))
    good = ctrl.build_effect(owner_id=owner["record_id"], device_id="arm-01",
                             slot_key=["1", 0, "text_type"], payload={"t": 1})
    bad = {**good, "record_id": "effect:deadbeef"}
    store.input_record_insert(bad)
    with pytest.raises(InputControlError):
        ctrl.get_record(bad["record_id"], expected_kind="effect")
    tampered = {**owner, "revision": 1, "revocation": True}
    from runtime.input_control import validate_owner
    with pytest.raises(InputControlError):
        validate_owner(tampered)
    bad_slot = {**good, "slot_key": ["1", -1, "text_type"]}
    from runtime.input_control import validate_effect
    with pytest.raises(InputControlError):
        validate_effect(bad_slot)
    bad_kind = {**good, "effect_kind": "not_a_kind", "slot_key": ["1", 0, "not_a_kind"]}
    with pytest.raises(InputControlError):
        validate_effect(bad_kind)


def test_old_task_copy_cannot_overwrite_input_records(tmp_path, control):
    """I39：save_task（整份覆盖路径）不得改写 input_records。"""
    ctrl, store = control
    owner, _ = ctrl.admit_owner(ctrl.build_owner(**_owner()))
    effect, _ = _effect(ctrl, owner)
    claimed = ctrl.claim_effect(effect["record_id"], expected_revision=1)
    # 任务旧副本被整份重存（迟到回调/进度写路径）——记录必须原样保留
    store.save_task({"task_id": "t-1", "idempotency_key": "k-1",
                     "status": "queued", "payload_hash": "h"})
    after = store.input_record_get(claimed["record_id"])
    assert after["state"] == "claimed" and after["revision"] == 2
    assert store.input_record_get(owner["record_id"])["revocation"] == "active"


def test_device_short_lock_serializes_claim_order(control):
    """后进入短锁的读者必须看到前者已持久提交的认领（顺序点可见）。"""
    import time as _time
    ctrl, _ = control
    owner, _ = ctrl.admit_owner(ctrl.build_owner(**_owner()))
    effect, _ = _effect(ctrl, owner)
    started = threading.Event()
    observed: list[str] = []

    def claimer():
        with ctrl.device_lock("arm-01"):
            ctrl.claim_effect(effect["record_id"], expected_revision=1)
            started.set()
            _time.sleep(0.3)  # 持锁期间：第二个读者不得读到未提交中间态

    def reader():
        started.wait(timeout=2)
        with ctrl.device_lock("arm-01"):
            record = ctrl.get_record(effect["record_id"], expected_kind="effect")
            observed.append(record["state"])

    first = threading.Thread(target=claimer)
    second = threading.Thread(target=reader)
    first.start(); second.start()
    first.join(timeout=3); second.join(timeout=3)
    assert observed == ["claimed"], observed


def test_record_ids_use_canonical_digest_of_arrays():
    assert record_id_for_effect("o", ["s", 0, "text_type"]).startswith("effect:")
    assert canonical_digest({"b": 1, "a": 2}) == canonical_digest({"a": 2, "b": 1})
    assert record_id_for_settings_write("arm-01", "r1").startswith("write:")


def test_resource_lock_excludes_second_instance(tmp_path):
    """§4.8.1：第二个实例必须启动失败，不能自动变成无锁模式。"""
    store = Store(tmp_path / "fleet.db")
    first = InputControl(store, tmp_path / "fleet.db")
    first.acquire_resource_locks([tmp_path / "data"])
    try:
        second = InputControl(Store(tmp_path / "fleet2.db"), tmp_path / "fleet.db")
        with pytest.raises(InputControlError, match="资源锁被占用"):
            second.acquire_resource_locks([tmp_path / "data"])
        # 不同数据根互不阻塞
        third = InputControl(Store(tmp_path / "fleet3.db"), tmp_path / "fleet3.db")
        third.acquire_resource_locks([tmp_path / "other-root"])
        third.release_resource_locks()
    finally:
        first.release_resource_locks()
    # 释放后可再次取得（重启场景）
    again = InputControl(Store(tmp_path / "fleet4.db"), tmp_path / "fleet.db")
    again.acquire_resource_locks([tmp_path / "data"])
    again.release_resource_locks()
