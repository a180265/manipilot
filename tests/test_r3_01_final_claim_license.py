"""R3-01 红测：IME typing_claim/deliver 最终认领必须在设备短锁内统一复核许可。

映射（acceptance-matrix.json 同步）：
- R3-01-A 正常认领各一次+同号重试拒绝（回归护栏）
- R3-01-B1 关闭写入在初检与认领之间先赢 → 零新认领（竞态钉在 insert_effect 处，
  竞争者走真实 write_settings 生产入口）
- R3-01-B2 deliver 同窗口同断言
- R3-01-B3 反向顺序（认领先赢）→ 保留已认领事实+取消对账可达
- R3-01-C1..C7 逐项拒绝：open guard/prepared 设置意图/设置回退/任务取消/急停/
  换手机绑定/运行资格（手机离线）
- R3-01-D 关闭后安全 ack/cancel 仍可达；其他设备不受牵连
- R3-01-E 经真实 ScreenImeService（生产钩子）typing_claim/deliver 拒绝分支映射 409

隔离：临时 config/db/data_root、fake 手机状态 provider、不连现场端口。
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime import input_settings as iset
from runtime.config import PlatformConfig
from runtime.input_control import InputControlError
from runtime.runtime import ArmFleetRuntime
from runtime.store import Store

IME = "k40-1"
OTHER_IME = "k40-9"


def make_runtime(tmp_path):
    from tests.helpers import make_device_configs
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "real",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    asyncio.run(runtime.devices.register_all(make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=7,
        daemon_addr="127.0.0.1:8795", expected_frame_width=64,
        expected_frame_height=48)))
    settings_root = tmp_path / "data"
    runtime.devices.settings_data_root = lambda device_id: settings_root
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": IME, "online": True, "keyboardActive": True,
         "inputControlCompatible": True},
        {"deviceId": OTHER_IME, "online": True, "keyboardActive": True,
         "inputControlCompatible": True}]
    runtime.devices._devices["arm-01"].update({
        "status": "ready", "calibration_status": "validated",
        "ime_online": True, "ime_device_id": IME,
        "ime_keyboard_active": True, "daemon_session": "sess-r3"})
    runtime.devices.input_binding = lambda device_id: {"ime_device_id": IME}
    return runtime, settings_root


@pytest.fixture()
def env(tmp_path):
    runtime, root = make_runtime(tmp_path)
    return {"runtime": runtime, "root": root, "tmp_path": tmp_path}


def _write_settings(runtime, root, *, enabled, ime=IME, expected_revision,
                    crid):
    """真实生产写入入口（app.py:888 同一路径，不经 HTTP 是为了避开线程约束）。"""
    return runtime.input_control.write_settings(
        "arm-01", data_root=root,
        request={"contract_version": "device-input-settings.v1",
                 "operator_id": "操作员甲", "role": "operator",
                 "reason": "R3-01 测试", "correlation_id": "corr-r3",
                 "client_request_id": crid,
                 "enabled": enabled, "expected_revision": expected_revision,
                 "ime_device_id": ime if enabled else None})


def _enable(env, crid="enable-1"):
    return _write_settings(env["runtime"], env["root"], enabled=True,
                           expected_revision=0, crid=crid)


def _close(env, crid="close-1", revision=1):
    return _write_settings(env["runtime"], env["root"], enabled=False,
                           expected_revision=revision, crid=crid)


def _admit_owner(runtime, task_id="t-r3-1", settings_revision=1,
                 ime=IME, with_task=False, task_status="queued"):
    control = runtime.input_control
    binding_revision = int(runtime.devices.binding_view("arm-01")
                           .get("binding_revision") or 0)
    owner = control.build_owner(
        owner_type="fleet_task", owner_ref=task_id, device_id="arm-01",
        ime_device_id=ime, settings_revision=settings_revision,
        binding_revision=binding_revision, payload={"task_id": task_id})
    if with_task:
        task = {"task_id": task_id, "device_id": "arm-01",
                "task_type": "chat_send", "status": task_status,
                "status_display": task_status, "cancel_requested": False,
                "stop_result_unknown": False, "payload": {"task_id": task_id},
                "idempotency_key": "idem-" + task_id,
                "created_at": "2026-10-03T00:00:00+00:00"}
        runtime.store.create_task(task)
    stored, _created = control.admit_owner(owner)
    return stored


def _assert_not_claimed(runtime, owner, ime_task_id, kind):
    from runtime.input_control import record_id_for_effect
    rec = runtime.store.input_record_get(record_id_for_effect(
        owner["record_id"], ["ime:" + ime_task_id, 0, kind]))
    assert rec is None or rec["state"] not in ("claimed", "completed", "unknown"), \
        f"{kind} 不应被认领，实际 {rec and rec['state']}（R3-01 目标断言）"
    return rec


def _pin_close_between_precheck_and_claim(runtime, root, close_revision=1):
    """把真实关闭写入钉在初检之后、认领之前（insert_effect 处，未持设备锁）。

    竞争者走生产 write_settings 入口，只编排时序，不替换任何被测逻辑。
    """
    control = runtime.input_control
    original_insert = control.insert_effect
    state = {"closed": False}

    def insert_then_competing_close(effect):
        result = original_insert(effect)
        if not state["closed"]:
            state["closed"] = True
            _write_settings(runtime, root, enabled=False,
                            expected_revision=close_revision,
                            crid="close-race")
        return result

    control.insert_effect = insert_then_competing_close
    return state


# ---------------------------------------------------------------- A 正常链

def test_r3_01_a_normal_claim_once_and_retry_rejected(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    hooks = runtime.ime_control_hooks("arm-01")
    task_id = "t-r3-1"
    claimed = hooks["typing_claim"](task_id + "-ime", IME)
    assert claimed["state"] == "claimed"
    # 同号重试：不许二次开始许可
    with pytest.raises(InputControlError) as exc:
        hooks["typing_claim"](task_id + "-ime", IME)
    assert getattr(exc.value, "hook_code", "") == "input_start_already_claimed"
    # deliver 正常一次
    hooks["deliver"](task_id + "-ime", IME)
    from runtime.input_control import record_id_for_effect
    rec = runtime.store.input_record_get(record_id_for_effect(
        owner["record_id"], ["ime:" + task_id + "-ime", 0, "ime_deliver"]))
    assert rec is not None and rec["state"] == "claimed"


# ---------------------------------------------------------------- B 竞态

def test_r3_01_b1_close_wins_zero_new_typing_claim(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    _pin_close_between_precheck_and_claim(runtime, root)
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError):
        hooks["typing_claim"]("t-r3-1-ime", IME)
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_start")


def test_r3_01_b2_close_wins_zero_new_deliver(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    _pin_close_between_precheck_and_claim(runtime, root)
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError):
        hooks["deliver"]("t-r3-1-ime", IME)
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_deliver")


def test_r3_01_b3_claim_first_keeps_claimed_fact_and_cancel_reachable(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    hooks = runtime.ime_control_hooks("arm-01")
    claimed = hooks["typing_claim"]("t-r3-1-ime", IME)
    assert claimed["state"] == "claimed"
    _close(env)  # 关闭后赢：已认领事实保留
    from runtime.input_control import record_id_for_effect
    rec = runtime.store.input_record_get(record_id_for_effect(
        owner["record_id"], ["ime:t-r3-1-ime", 0, "ime_start"]))
    assert rec is not None and rec["state"] == "claimed", \
        "已认领事实不得被关闭抹掉"
    # 取消对账可达：取消检查给出取消理由，cancel 落 requested
    assert hooks["cancel_pending_check"]("t-r3-1-ime", IME) is True
    hooks["cancel"]("t-r3-1-ime", IME, delivered=False)


# ---------------------------------------------------------------- C 逐项拒绝

def test_r3_01_c1_open_guard_rejects(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    runtime.input_control.open_guard_issue(
        "arm-01", code="editor_review_required", owner_id=owner["record_id"],
        detail={"ime_task_id": "t-r3-1-ime"})
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError):
        hooks["typing_claim"]("t-r3-1-ime", IME)
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_start")


def test_r3_01_c2_prepared_settings_intent_rejects(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    intent = runtime.input_control.build_settings_write(
        device_id="arm-01", client_request_id="pending-crq",
        call_context={"origin": "human_put", "operator_id": "操作员甲",
                      "role": "operator", "operation_id": "input_settings_update"},
        normalized_request={"enabled": False},
        before={"revision": 1}, after={"revision": 2},
        proposed_file={"revision": 2})
    runtime.input_control.insert_settings_write(intent)
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError):
        hooks["typing_claim"]("t-r3-1-ime", IME)
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_start")


def test_r3_01_c3_settings_rollback_rejects(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    # 同版本内容替换（checkpoint r1 已核对，文件被改动）：updated_at 变化
    # 使语义摘要不同；许可字段保持 r1 有效，以隔离"历史核对"这一道闸。
    file = root / "devices" / "arm-01" / "input_settings.json"
    rolled = json.loads(file.read_text(encoding="utf-8"))
    assert int(rolled["revision"]) == 1
    rolled["updated_at"] = "2026-01-01T00:00:00.000Z"
    file.write_text(json.dumps(rolled, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError) as exc:
        hooks["typing_claim"]("t-r3-1-ime", IME)
    assert getattr(exc.value, "hook_code", "") == "input_settings_invalid"
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_start")


def test_r3_01_c4_cancelled_task_rejects(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime, with_task=True, task_status="cancelled")
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError):
        hooks["typing_claim"]("t-r3-1-ime", IME)
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_start")


def test_r3_01_c5_estop_rejects(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    runtime.estop.trigger("操作员甲", "R3-01 红测急停")
    assert runtime.estop.active
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError):
        hooks["typing_claim"]("t-r3-1-ime", IME)
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_start")


def test_r3_01_c6_phone_rebound_rejects(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime, ime=IME)
    # 换手机：开启状态下重绑到另一台（生产写入入口），设备运行时绑定同步
    _write_settings(runtime, root, enabled=True, ime=OTHER_IME,
                    expected_revision=1, crid="rebind-1")
    runtime.devices._devices["arm-01"]["ime_device_id"] = OTHER_IME
    runtime.devices.input_binding = lambda device_id: {"ime_device_id": OTHER_IME}
    hooks = runtime.ime_control_hooks("arm-01")
    # 原手机冒认：拒绝
    with pytest.raises(InputControlError):
        hooks["typing_claim"]("t-r3-1-ime", IME)
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_start")


def test_r3_01_c7_eligibility_phone_offline_rejects(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner = _admit_owner(runtime)
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": IME, "online": False, "keyboardActive": False,
         "inputControlCompatible": True}]
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError):
        hooks["typing_claim"]("t-r3-1-ime", IME)
    _assert_not_claimed(runtime, owner, "t-r3-1-ime", "ime_start")


# ---------------------------------------------------------------- D 安全对账

def test_r3_01_d_after_close_safe_cancel_and_ack_reachable(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    _admit_owner(runtime)
    hooks = runtime.ime_control_hooks("arm-01")
    hooks["typing_claim"]("t-r3-1-ime", IME)
    _close(env)
    # 关闭后原手机安全对账不要求输入开关开启
    hooks["cancel"]("t-r3-1-ime", IME, delivered=True)
    hooks["ack"]("t-r3-1-ime", IME, typed_character_count=0)
    # 无计数=未知输入 → guard 打开（原语义保留）
    from runtime.input_control import record_id_for_guard
    guard = runtime.store.input_record_get(record_id_for_guard("arm-01")) or {}
    codes = {i["code"] for i in guard.get("issues", []) if i["state"] == "open"}
    assert "editor_review_required" in codes


def test_r3_01_d2_other_device_navigation_unaffected(env):
    runtime, root = env["runtime"], env["root"]
    _enable(env)
    owner_a = _admit_owner(runtime, task_id="t-a")
    # guard 打开（t-a 的编辑框复核问题）：t-a 新输入被拒
    runtime.input_control.open_guard_issue(
        "arm-01", code="editor_review_required", owner_id=owner_a["record_id"],
        detail={"ime_task_id": "t-a-ime"})
    hooks = runtime.ime_control_hooks("arm-01")
    with pytest.raises(InputControlError):
        hooks["typing_claim"]("t-a-ime", IME)
    # 另一 owner 的取消/ack 安全对账不被 guard 牵连
    _admit_owner(runtime, task_id="t-b")
    hooks["cancel"]("t-b-ime", IME, delivered=False)
    hooks["ack"]("t-b-ime", IME, typed_character_count=0)


# ---------------------------------------------------------------- E service 层

def _make_service(runtime, tmp_path):
    from adapters.screen_ime.service import ScreenImeService
    db = tmp_path / "ime.db"
    service = ScreenImeService(db, control_hooks=runtime.ime_control_hooks("arm-01"))
    # 手机先经真实心跳注册（create 依 IME 仓储 devices 表判在线）
    asyncio.run(service.tasks.heartbeat(IME, {
        "appVersion": "r3-test", "keyboardActive": True}))
    return service


def test_r3_01_e_service_typing_claim_rejected_maps_409(env):
    from adapters.screen_ime.service import ScreenImeApiError
    runtime, root, tmp_path = env["runtime"], env["root"], env["tmp_path"]
    _enable(env)
    _admit_owner(runtime)
    service = _make_service(runtime, tmp_path)
    task, _created = asyncio.run(service.tasks.create({
        "taskId": "t-r3-1-ime", "deviceId": IME,
        "targetUser": "客户甲", "content": "您好"}))
    assert task["status"] == "pending"
    asyncio.run(service.tasks.update_status("t-r3-1-ime", {
        "deviceId": IME, "status": "displayed"}))
    _close(env)
    with pytest.raises(ScreenImeApiError) as exc:
        asyncio.run(service.tasks.update_status("t-r3-1-ime", {
            "deviceId": IME, "status": "typing"}))
    assert exc.value.status == 409
    assert exc.value.code != "invalid_transition", \
        "409 必须来自输入许可拒绝，不是状态机转移错误" 
    # deliver 分支：长轮询不返回正文
    got = asyncio.run(service.tasks.next_task(IME, wait_seconds=0.2))
    assert got is None, "关闭后长轮询不得返回待交付正文"


def test_r3_01_e2_service_typing_claim_guard_rejected(env):
    from adapters.screen_ime.service import ScreenImeApiError
    runtime, root, tmp_path = env["runtime"], env["root"], env["tmp_path"]
    _enable(env)
    owner = _admit_owner(runtime)
    service = _make_service(runtime, tmp_path)
    asyncio.run(service.tasks.create({
        "taskId": "t-r3-1-ime", "deviceId": IME,
        "targetUser": "客户甲", "content": "您好"}))
    asyncio.run(service.tasks.update_status("t-r3-1-ime", {
        "deviceId": IME, "status": "displayed"}))
    runtime.input_control.open_guard_issue(
        "arm-01", code="editor_review_required", owner_id=owner["record_id"],
        detail={"ime_task_id": "t-r3-1-ime"})
    with pytest.raises(ScreenImeApiError) as exc:
        asyncio.run(service.tasks.update_status("t-r3-1-ime", {
            "deviceId": IME, "status": "typing"}))
    assert exc.value.status == 409
    assert exc.value.code != "invalid_transition", \
        "409 必须来自输入许可拒绝，不是状态机转移错误" 
