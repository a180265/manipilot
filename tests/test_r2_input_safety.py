"""第二轮修复 A 包红测：R2-02（设置历史事故统一入口）+ R2-22（人工核对原子提交）。

映射（COVERAGE.md 同步）：
- test_r2_02_get_put_rejects_known_device_missing_file
    F04/R2-02：已知设备设置文件丢失 → 正式 GET 给出 input_settings_invalid，
    不得按空白 r0 重建；expected_revision=0 开启不得成功（版本重置）。
- test_r2_02_rollback_file_does_not_resurrect_old_owner
    R2-02：文件回退到旧 r1 → GET 503；旧 owner 不得复活（check_input_owner 拒绝）。
- test_r2_02_restart_checks_history_before_dispatch
    R2-02：同根重建 Runtime（重启）后先核对历史再派发——回退文件阻断旧任务。
- test_r2_02_applied_and_checkpoint_atomic
    R2-02：applied 与 checkpoint 必须同一 Store 事务；applied/checkpoint 之间故障
    不得留下 applied-无-checkpoint 半状态。
- test_r2_02_same_request_replay_reconciles
    R2-02：同编号重放按原请求对账（prepared 意图补 applied），不重置版本。
- test_r2_02_new_device_still_defaults_closed
    合法成功路径：真正无历史的新设备 GET=r0 关闭、开启正常。
- test_r2_22_replay_does_not_fake_success_on_recorded_entry
    R2-22：账本 recorded（中断遗留）+ 部分 issue 仍 open → 同号重试必须保守
    补齐解决，不得把 recorded 当完成返回假成功。
- test_r2_22_review_is_single_revision_atomic
    R2-22：核对记录与全部 issue 解决在同一 guard revision（单事务）。
- test_r2_22_digest_covers_full_request
    R2-22：摘要覆盖完整请求（resolution/expected_revision/调用上下文），
    不只 issue_ids/operator。
- test_r2_22_busy_when_effect_in_flight
    R2-22：原执行仍在途（claimed 且取消未确认）→ input_recovery_busy 不释放。

隔离：临时 config/db/data_root、fake 手机状态 provider、不连现场端口。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from fastapi.testclient import TestClient

from api.app import create_app
from runtime import input_settings as iset
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from runtime.store import Store
from tests.helpers import make_device_configs


@pytest.fixture()
def env(tmp_path):
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
    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.device_configs = make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=7,
        daemon_addr="127.0.0.1:8795", expected_frame_width=64,
        expected_frame_height=48)
    settings_root = tmp_path / "data"
    runtime.devices.settings_data_root = lambda device_id: settings_root
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-1", "online": True, "keyboardActive": True}]
    with TestClient(app) as c:
        runtime.devices._devices["arm-01"].update({
            "status": "online", "calibration_status": "validated",
            "ime_online": True, "ime_device_id": "k40-1",
            "ime_keyboard_active": True, "daemon_session": "sess-1"})
        yield {"client": c, "runtime": runtime, "root": settings_root,
               "tmp_path": tmp_path}


def op(**extra):
    base = {"contract_version": "device-input-settings.v1",
            "operator_id": "操作员甲", "role": "operator", "reason": "R2 测试",
            "correlation_id": "corr", "client_request_id": "req-1"}
    base.update(extra)
    return base


def _enable(env, crid="enable-1"):
    r = env["client"].put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id=crid), "enabled": True,
        "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r.status_code == 200, r.text
    return r


def _close(env, crid="close-1", revision=1):
    r = env["client"].put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id=crid), "enabled": False,
        "expected_revision": revision})
    assert r.status_code == 200, r.text
    return r


def _settings_file(env):
    return env["root"] / "devices" / "arm-01" / "input_settings.json"


def _admit_owner(runtime, task_id="t1", settings_revision=1):
    control = runtime.input_control
    binding_revision = int(runtime.devices.binding_view("arm-01")
                           .get("binding_revision") or 0)
    owner = control.build_owner(
        owner_type="fleet_task", owner_ref=task_id, device_id="arm-01",
        ime_device_id="k40-1", settings_revision=settings_revision,
        binding_revision=binding_revision, payload={"task_id": task_id})
    stored, _created = control.admit_owner(owner)
    return stored


# ---------------------------------------------------------------- R2-02

def test_r2_02_get_put_rejects_known_device_missing_file(env):
    _enable(env)
    _close(env)
    _settings_file(env).unlink()
    # GET：已知设备文件丢失 → input_settings_invalid，不得按空白 r0 重建
    r = env["client"].get("/v1/devices/arm-01/input-settings")
    assert r.status_code == 503, f"GET 应 503，实际 {r.status_code}: {r.text}"
    assert r.json()["error"]["key"] == "input_settings_invalid"
    # 版本不得重置：expected_revision=0 开启必须被拒
    r2 = env["client"].put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id="rebuild-attempt"), "enabled": True,
        "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r2.status_code != 200, f"文件丢失后 expected_revision=0 开启不得成功：{r2.text}"
    assert r2.json()["error"]["key"] == "input_settings_invalid"


def test_r2_02_rollback_file_does_not_resurrect_old_owner(env):
    _enable(env)
    owner = _admit_owner(env["runtime"], settings_revision=1)
    # 旧 owner 在 r1 时本来有效
    assert env["runtime"].check_input_owner(
        {"task_id": "t1", "device_id": "arm-01"}) is None
    _close(env)
    # 关闭后 owner 失效（撤销边界 r2）
    blocked = env["runtime"].check_input_owner({"task_id": "t1", "device_id": "arm-01"})
    assert blocked is not None and "revoked" in blocked["category"]
    # 回退攻击：把文件恢复成旧 r1 内容
    file = _settings_file(env)
    current = iset.load_settings("arm-01", data_root=env["root"])
    current["revision"] = 1
    current["enabled"] = True
    current["revoked_through_revision"] = 0
    current["ime_device_id"] = "k40-1"
    current["updated_at"] = owner["created_at"]
    import json as _json
    file.write_text(_json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    # GET 必须按资料回退事故拒绝，不得把 r1 当当前事实
    r = env["client"].get("/v1/devices/arm-01/input-settings")
    assert r.status_code == 503, f"回退文件 GET 应 503，实际 {r.status_code}: {r.text}"
    # 旧 owner 不得借回退文件复活
    resurrected = env["runtime"].check_input_owner({"task_id": "t1", "device_id": "arm-01"})
    assert resurrected is not None, "回退文件使旧 owner 复活（R2-02 核心反例）"


def test_r2_02_restart_checks_history_before_dispatch(env):
    _enable(env)
    _admit_owner(env["runtime"], task_id="t9", settings_revision=1)
    _close(env)
    file = _settings_file(env)
    import json as _json
    rolled = _json.loads(file.read_text(encoding="utf-8"))
    rolled["revision"] = 1
    rolled["enabled"] = True
    rolled["revoked_through_revision"] = 0
    rolled["ime_device_id"] = "k40-1"
    file.write_text(_json.dumps(rolled, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同根重建 Runtime（重启语义：同一 fleet.db + 同一 data_root + 同一设备登记）
    config = PlatformConfig({
        "db_path": str(env["tmp_path"] / "fleet.db"),
        "evidence_dir": str(env["tmp_path"] / "evidence"),
        "plugin_dir": str(env["tmp_path"] / "plugins"),
        "hardware_mode": "real",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
    }, root=env["tmp_path"])
    rebooted = ArmFleetRuntime(config)
    import asyncio as _asyncio
    _asyncio.run(rebooted.devices.register_all(make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=7,
        daemon_addr="127.0.0.1:8795", expected_frame_width=64,
        expected_frame_height=48)))
    rebooted.devices._devices["arm-01"].update({
        "status": "online", "calibration_status": "validated",
        "ime_online": True, "ime_device_id": "k40-1",
        "ime_keyboard_active": True, "daemon_session": "sess-1"})
    rebooted.devices.settings_data_root = lambda device_id: env["root"]
    # 反证（防假通过）：回退文件上旧任务本来会被放行——先断言无回退检测时放行，
    # 再要求核对后拒绝。此处直接断言正确行为：必须拒绝。
    blocked = rebooted.check_input_owner({"task_id": "t9", "device_id": "arm-01"})
    assert blocked is not None, "重启后未先核对历史即放行旧任务（R2-02：启动先核对再派发）"


def test_r2_02_applied_and_checkpoint_atomic(env, monkeypatch):
    from runtime.input_control import InputControl, InputControlCommitUnknown
    _enable(env)
    store = env["runtime"].store
    has_atomic = hasattr(Store, "input_record_apply_settings")
    if not has_atomic:
        # 旧实现：applied 与 checkpoint 是两次独立事务。注入 checkpoint 故障，
        # 证明会出现 applied-无-checkpoint 半状态（R2-02 目标断言）。
        def _boom(*a, **k):
            raise RuntimeError("injected checkpoint failure")
        monkeypatch.setattr(InputControl, "set_settings_checkpoint", _boom)
        with pytest.raises(Exception) as exc_info:
            env["client"].put("/v1/devices/arm-01/input-settings", json={
                **op(client_request_id="close-crash"), "enabled": False,
                "expected_revision": 1})
        assert "input_settings_commit_unknown" in str(exc_info.value) or \
            getattr(exc_info.value, "status_code", None) == 503
        from runtime.input_control import record_id_for_settings_write
        rec = store.input_record_get(record_id_for_settings_write("arm-01", "close-crash"))
        assert rec is not None and rec["state"] == "prepared", \
            "applied 已持久化而 checkpoint 缺失：半状态（R2-02 反例实锤）"
        pytest.fail("R2-02：applied 与 checkpoint 分离提交存在半状态窗口（旧实现）")
    # 新实现：单事务调用故障 → commit_unknown 且记录保持 prepared
    def _boom2(*a, **k):
        raise RuntimeError("injected atomic apply failure")
    monkeypatch.setattr(Store, "input_record_apply_settings", _boom2)
    r = env["client"].put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id="close-crash"), "enabled": False,
        "expected_revision": 1})
    assert r.status_code == 503, f"应 503 commit_unknown，实际 {r.status_code}: {r.text}"
    assert r.json()["error"]["key"] == "input_settings_commit_unknown"
    from runtime.input_control import record_id_for_settings_write
    rec = store.input_record_get(record_id_for_settings_write("arm-01", "close-crash"))
    assert rec is not None and rec["state"] == "prepared"
    # 恢复后同编号重放：按原请求对账为 applied，版本不重置
    monkeypatch.undo()
    r = env["client"].put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id="close-crash"), "enabled": False,
        "expected_revision": 1})
    assert r.status_code == 200, r.text
    assert r.json()["replayed"] is True
    rec = store.input_record_get(record_id_for_settings_write("arm-01", "close-crash"))
    assert rec["state"] == "applied"
    guard = store.input_record_get("guard:arm-01")
    assert guard["settings_checkpoint"]["revision"] == rec["after_revision"]


def test_r2_02_new_device_still_defaults_closed(env):
    # 真正无历史的新设备：GET=r0 关闭，开启正常（不得被历史检查误伤）
    r = env["client"].get("/v1/devices/arm-01/input-settings")
    assert r.status_code == 200 and r.json()["revision"] == 0 \
        and r.json()["enabled"] is False
    _enable(env)
    got = env["client"].get("/v1/devices/arm-01/input-settings").json()
    assert got["enabled"] is True and got["revision"] == 1


# ---------------------------------------------------------------- R2-22

def _seed_issue(runtime, code, detail=None):
    guard = runtime.input_control.open_guard_issue(
        "arm-01", code=code, detail=detail or {})
    return guard


def _review_body(crid, issue_ids, revision, **extra):
    body = {"contract_version": "input-recovery-review.v1",
            "expected_recovery_revision": revision,
            "ime_device_id": "k40-1",
            "issue_ids": issue_ids,
            "candidate_removed": True, "editor_checked": True,
            "operator_id": "操作员甲", "role": "operator",
            "reason": "人工已核对", "correlation_id": "corr",
            "client_request_id": crid}
    body.update(extra)
    return body


def test_r2_22_replay_does_not_fake_success_on_recorded_entry(env):
    runtime = env["runtime"]
    control = runtime.input_control
    i1 = _seed_issue(runtime, "input_effect_unknown")["issues"][0]["issue_id"]
    guard = _seed_issue(runtime, "legacy_input_review_required")
    i2 = guard["issues"][-1]["issue_id"]
    from runtime.input_control import canonical_digest
    # 模拟第一轮实现中断遗留：账本已 recorded、i1 已解决、i2 仍 open
    legacy_entry = {"digest": canonical_digest({"issue_ids": [i1, i2],
                                                "operator_id": "操作员甲"}),
                    "issue_ids": [i1, i2], "operator_id": "操作员甲",
                    "at": "2026-10-04T00:00:00Z", "state": "recorded"}

    def mutate(record):
        record["review_requests"]["crash-1"] = deepcopy(legacy_entry)

    control._store.input_record_cas("guard:arm-01", guard["revision"], mutate)
    control.resolve_guard_issue("arm-01", issue_id=i1,
                                resolution={"candidate_removed": True,
                                            "editor_checked": True})
    revision = control.ensure_guard("arm-01")["revision"]
    r = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                           json=_review_body("crash-1", [i1, i2], revision))
    assert r.status_code == 200, r.text
    after = control.ensure_guard("arm-01")
    states = {i["issue_id"]: i["state"] for i in after["issues"]}
    assert states[i2] == "resolved", \
        f"同号重试返回成功但 {i2} 仍 open（recorded 被当完成）"
    assert after["review_requests"]["crash-1"]["state"] == "completed"
    assert r.json()["input_recovery_required"] is False


def test_r2_22_review_is_single_revision_atomic(env):
    runtime = env["runtime"]
    control = runtime.input_control
    i1 = _seed_issue(runtime, "input_effect_unknown")["issues"][0]["issue_id"]
    i2 = _seed_issue(runtime, "legacy_input_review_required")["issues"][-1]["issue_id"]
    before = control.ensure_guard("arm-01")["revision"]
    r = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                           json=_review_body("review-1", [i1, i2], before))
    assert r.status_code == 200, r.text
    after = control.ensure_guard("arm-01")
    assert after["revision"] == before + 1, \
        f"核对必须单事务（revision +1），实际推进 {after['revision'] - before}"
    entry = after["review_requests"]["review-1"]
    assert entry["state"] == "completed"
    assert entry.get("response"), "账本必须保存原响应"
    assert entry.get("resolution"), "账本必须保存解决结果"
    states = {i["issue_id"]: i["state"] for i in after["issues"]}
    assert states[i1] == "resolved" and states[i2] == "resolved"
    # 重放（同号同内容：expected_recovery_revision 也必须与原请求一致）返回
    # 原结果且不再推进版本；换新 revision 重发=新内容 → 冲突（见 digest 用例）
    r2 = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                            json=_review_body("review-1", [i1, i2], before))
    assert r2.status_code == 200
    assert control.ensure_guard("arm-01")["revision"] == after["revision"]


def test_r2_22_digest_covers_full_request(env):
    runtime = env["runtime"]
    control = runtime.input_control
    i1 = _seed_issue(runtime, "input_effect_unknown")["issues"][0]["issue_id"]
    before = control.ensure_guard("arm-01")["revision"]
    r = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                           json=_review_body("digest-1", [i1], before,
                                             reason="理由A"))
    assert r.status_code == 200, r.text
    # 同号但请求内容不同（reason/resolution 变化）→ request_conflict
    r2 = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                            json=_review_body("digest-1", [i1], before + 1,
                                              reason="理由B"))
    assert r2.status_code == 409, f"同号不同内容应 409，实际 {r2.status_code}: {r2.text}"
    assert r2.json()["error"]["key"] == "request_conflict"


def test_r2_22_busy_when_effect_in_flight(env):
    runtime = env["runtime"]
    control = runtime.input_control
    i1 = _seed_issue(runtime, "legacy_input_review_required")["issues"][0]["issue_id"]
    owner = _admit_owner(runtime, task_id="t-busy")
    effect = control.build_effect(
        owner_id=owner["record_id"], device_id="arm-01",
        slot_key=["ime:t-busy", 0, "ime_deliver"], payload={},
        ime_task_id="ime-t-busy")
    stored, _created = control.insert_effect(effect)
    control.claim_effect(stored["record_id"], expected_revision=1)
    revision = control.ensure_guard("arm-01")["revision"]
    r = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                           json=_review_body("busy-1", [i1], revision))
    assert r.status_code == 409, f"原执行在途应 409，实际 {r.status_code}: {r.text}"
    assert r.json()["error"]["key"] == "input_recovery_busy"
    # 取消确认后同一请求放行（安全前置满足）
    control.set_cancel_state(stored["record_id"], expected_revision=2,
                             cancel_state="confirmed")
    r2 = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                            json=_review_body("busy-1", [i1], revision))
    assert r2.status_code == 200, r2.text


# ---------------------------------------------------------------- R2-01
# 真实 Runtime 认领回调 + 真实 DaemonArmDriver + fake transport（§4.3）。

@pytest.fixture()
def claim_env(tmp_path, monkeypatch):
    """真实 Runtime（注册设备+输入设置）+ 真实驱动（fake daemon）。"""
    import asyncio
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_daemon_arm import FakeDaemon, make_driver, write_device_files, \
        write_ready_bundle, enable_ui_target
    from tests.helpers import make_device_configs as _mdc

    write_device_files(tmp_path)
    write_ready_bundle(tmp_path)
    enable_ui_target(tmp_path, "ime_candidate_bar", nx=0.5, ny=0.5)
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    asyncio.run(runtime.devices.register_all(_mdc(
        1, camera_device_unique_id="uvc-01", binding_revision=3,
        daemon_addr="127.0.0.1:8795", expected_frame_width=64,
        expected_frame_height=48)))
    runtime.devices._devices["arm-01"].update({
        "status": "online", "calibration_status": "validated"})
    data_root = tmp_path / "data"
    runtime.devices.settings_data_root = lambda device_id: data_root
    runtime.devices.input_binding = lambda device_id: {"ime_device_id": "k40-test"}
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-test", "online": True, "keyboardActive": True}]
    # 驱动入口的准们检查放行（admission 已过）；R2-01 验证的是认领边界重验
    import drivers.daemon_arm as da_mod
    monkeypatch.setattr(da_mod.DaemonArmDriver, "_input_switch_allowed",
                        lambda self, calib: True)
    daemon = FakeDaemon().start()
    driver = make_driver(daemon, tmp_path)
    asyncio.run(driver.connect())
    # 真实写入口开启 + 真实 owner
    outcome = runtime.input_control.write_settings(
        "arm-01", data_root=data_root,
        request={"enabled": True, "ime_device_id": "k40-test",
                 "expected_revision": 0, "operator_id": "操作员",
                 "reason": "R2-01", "client_request_id": "claim-enable-1",
                 "correlation_id": "c"},
        calibration_ready=True, binding_state=None,
        call_context={"origin": "human_put", "operator_id": "操作员",
                      "role": "operator", "operation_id": "input_settings_update"})
    assert outcome["outcome"] == "applied"
    control = runtime.input_control
    owner = control.build_owner(
        owner_type="fleet_task", owner_ref="t-claim", device_id="arm-01",
        ime_device_id="k40-test", settings_revision=1,
        binding_revision=int(runtime.devices.binding_view("arm-01")["binding_revision"]),
        payload={"task_id": "t-claim"})
    control.admit_owner(owner)
    try:
        yield {"runtime": runtime, "daemon": daemon, "driver": driver,
               "data_root": data_root, "tmp_path": tmp_path}
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def _prepare_effect(env, kind="candidate_commit", task_id=None):
    """唯一 task_id（每测独立 effect slot）；返回 (params, effect_record_id)。"""
    runtime = env["runtime"]
    task_id = task_id or f"t-{uuid.uuid4().hex[:8]}"
    control = runtime.input_control
    owner = control.build_owner(
        owner_type="fleet_task", owner_ref=task_id, device_id="arm-01",
        ime_device_id="k40-test", settings_revision=1,
        binding_revision=int(runtime.devices.binding_view("arm-01")["binding_revision"]),
        payload={"task_id": task_id})
    control.admit_owner(owner)
    task = {"task_id": task_id, "device_id": "arm-01", "step_cursor": 0,
            "status": "running"}
    params = {"target_id": "ime_candidate_bar"}
    runtime._prepare_input_effect(task, task, kind, params)
    return params, task_id


def test_r2_01_close_wins_before_final_claim_zero_contact(claim_env):
    """真实链路：prepared 后、接触认领前经真实写入口关闭 → 接触 0、走位保留。"""
    env = claim_env
    runtime, daemon, driver = env["runtime"], env["daemon"], env["driver"]
    params, task_id = _prepare_effect(env)
    # 最终回调前经真实写入口关闭（撤销边界推进）
    outcome = runtime.input_control.write_settings(
        "arm-01", data_root=env["data_root"],
        request={"enabled": False, "ime_device_id": None,
                 "expected_revision": 1, "operator_id": "操作员",
                 "reason": "关闭先赢", "client_request_id": "claim-close-1",
                 "correlation_id": "c"},
        calibration_ready=False, binding_state=None,
        call_context={"origin": "human_put", "operator_id": "操作员",
                      "role": "operator", "operation_id": "input_settings_update"})
    assert outcome["outcome"] == "applied"
    from drivers.arm import DriverError
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click(params))
    assert "input_switch_disabled" in str(exc.value.category) or \
        "input_authorization_revoked" in str(exc.value.category)
    taps = [c for c in daemon.commands if c.startswith("TAP ")]
    assert taps == [], "关闭先赢时接触命令必须零下发"
    assert any(c.startswith("XY ") or c.startswith("Z ")
               for c in daemon.commands), "已执行走位事实必须保留"
    # effect 不成为已交付
    record = runtime.store.input_record_get(
        task_effect_record_id(runtime, task_id))
    assert record is not None and record["state"] == "prepared"


def task_effect_record_id(runtime, task_id):
    from runtime.input_control import record_id_for_owner
    owner_id = record_id_for_owner("fleet_task", task_id)
    records = [r for r in runtime.store.input_records_by_device("arm-01", "effect")
               if r["owner_id"] == owner_id]
    assert records, "effect 未持久化"
    return records[0]["record_id"]


def test_r2_01_happy_path_single_contact_and_double_claim_refused(claim_env):
    env = claim_env
    runtime, daemon, driver = env["runtime"], env["daemon"], env["driver"]
    params, task_id = _prepare_effect(env)
    callback = params["input_claim"]["callback"]  # click 会 pop input_claim
    result = asyncio.run(driver.click(params))
    assert result["ok"] is True
    taps = [c for c in daemon.commands if c.startswith("TAP ")]
    assert len(taps) == 1, "合法输入恰一次接触"
    record = runtime.store.input_record_get(
        task_effect_record_id(runtime, task_id))
    assert record["state"] == "claimed"
    # 同一 effect 二次认领拒绝（认领先赢原在途至多一次）
    from drivers.arm import DriverError
    with pytest.raises(DriverError):
        callback()


def test_r2_01_guard_cancel_estop_digest_block_final_claim(claim_env):
    """guard 问题/任务取消/急停/payload 摘要变化任一先赢 → 接触 0。"""
    env = claim_env
    runtime, daemon, driver = env["runtime"], env["daemon"], env["driver"]
    from drivers.arm import DriverError

    def taps():
        return [c for c in daemon.commands if c.startswith("TAP ")]

    def clear_guard_issues():
        guard = runtime.input_control.ensure_guard("arm-01")
        runtime.store.input_record_cas(
            guard["record_id"], guard["revision"],
            lambda record: record.update({"issues": []}))

    # a) guard 问题先赢
    params, _tid = _prepare_effect(env)
    runtime.input_control.open_guard_issue(
        "arm-01", code="input_record_invalid", detail={"why": "r2-01"})
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dict(params)))
    assert taps() == []
    clear_guard_issues()  # 测试装置清理：后续子场景需要干净 guard
    # b) 任务取消先赢
    params, task_id = _prepare_effect(env)
    task_row = runtime.store.get_task(task_id)
    assert task_row is not None, "任务进度应已持久化"
    task_row["status"] = "cancelled"
    runtime.store.save_task(task_row)
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dict(params)))
    assert taps() == []
    # c) 急停先赢
    params, _tid = _prepare_effect(env)
    runtime.store.kv_set("estop_active", "1")
    try:
        with pytest.raises(DriverError):
            asyncio.run(driver.click(dict(params)))
        assert taps() == []
    finally:
        runtime.store.kv_set("estop_active", "0")
    # d) payload 摘要在认领前被改 → 拒绝
    params, _tid = _prepare_effect(env)
    params["late_injection"] = {"tamper": True}
    with pytest.raises(DriverError):
        asyncio.run(driver.click(params))
    assert taps() == []


def test_r2_01_phone_offline_blocks_final_claim(claim_env):
    """运行时资格丢失（手机离线）在最终认领重验时拒绝。"""
    env = claim_env
    runtime, daemon, driver = env["runtime"], env["daemon"], env["driver"]
    params, _tid = _prepare_effect(env)
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-test", "online": False, "keyboardActive": False}]
    from drivers.arm import DriverError
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dict(params)))
    assert not [c for c in daemon.commands if c.startswith("TAP ")], \
        "手机离线时接触必须零下发"


# ---------------------------------------------------------------- R2-03

@pytest.fixture()
def geometry_env(tmp_path):
    """真实 ASGI app（几何路由+设置路由同 app）+ A/B 两台唯一绑定手机。"""
    from fastapi.testclient import TestClient
    from api.app import create_app
    from tests.helpers import make_device_configs as _mdc

    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "real",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    configs = _mdc(2, camera_device_unique_id="uvc-01", binding_revision=7,
                   daemon_addr="127.0.0.1:8795", expected_frame_width=64,
                   expected_frame_height=48)
    for index, cfg in enumerate(configs):
        cfg["daemon_addr"] = f"127.0.0.1:{8795 + index}"
        cfg["camera_device_unique_id"] = f"uvc-{index + 1:02d}"
        cfg["ime_device_id"] = "k40-b" if index == 0 else "k40-a"
    data_root = tmp_path / "data"
    runtime.devices.settings_data_root = lambda device_id: data_root
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-a", "online": True, "keyboardActive": True},
        {"deviceId": "k40-b", "online": True, "keyboardActive": True}]
    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.device_configs = configs
    client = TestClient(app)
    # R2-21：测试凭据（每机独立令牌，走真实验证器）。
    # 修前无此接口：几何入口本就不要求凭据（R2-21 否定的旧语义），
    # header 被忽略 → 修前红测命中 401/403 目标断言而非 fixture 错误。
    if hasattr(runtime, "register_phone_credential"):
        runtime.register_phone_credential("k40-a", "tok-a-00000000000000a")
        runtime.register_phone_credential("k40-b", "tok-b-00000000000000b")
    with client:
        for device_id, ime in (("arm-01", "k40-b"), ("arm-02", "k40-a")):
            runtime.devices._devices[device_id].update({
                "status": "online", "calibration_status": "validated",
                "ime_device_id": ime})
        yield {"client": client, "runtime": runtime, "root": data_root}


def _geometry_handshake(client, phone, token=None):
    headers = {"x-ime-phone-token": token} if token else {}
    return client.post(f"/api/v1/devices/{phone}/geometry/sessions", json={
        "contract_version": "ime-geometry.v1", "imeGeometryVersion": 1,
        "client_instance_id": str(uuid.uuid4())}, headers=headers)


def test_r2_03_close_completes_while_geometry_poll_waiting(geometry_env):
    """同 app 内：真实 poll 长轮询等待期间关闭请求必须及时完成（不阻塞事件循环）。"""
    import threading
    import time as _time
    client, runtime = geometry_env["client"], geometry_env["runtime"]
    r = _geometry_handshake(client, "k40-b", token="tok-b-00000000000000b")
    assert r.status_code == 200, r.text
    session_id = r.json()["runtime_session_id"]
    # 开启开关（供关闭对比）
    rr = client.put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id="poll-enable"), "enabled": True,
        "expected_revision": 0, "ime_device_id": "k40-b"})
    assert rr.status_code == 200, rr.text
    results = {}

    def poller():
        started = _time.monotonic()
        resp = client.get(
            f"/api/v1/devices/k40-b/geometry/requests/next?waitSeconds=5"
            f"&runtime_session_id={session_id}",
            headers={"x-ime-phone-token": "tok-b-00000000000000b"})
        results["poll"] = (resp.status_code, _time.monotonic() - started)

    thread = threading.Thread(target=poller)
    thread.start()
    _time.sleep(0.4)  # 让 poll 进入等待
    started = _time.monotonic()
    close = client.put("/v1/devices/arm-01/input-settings", json={
        **op(client_request_id="poll-close"), "enabled": False,
        "expected_revision": 1})
    close_elapsed = _time.monotonic() - started
    assert close.status_code == 200, close.text
    assert close_elapsed < 3.0, \
        f"关闭请求被几何长轮询阻塞 {close_elapsed:.1f}s（R2-03 事件循环被占）"
    thread.join(timeout=10)
    assert "poll" in results and results["poll"][0] in (204, 200)
    # 关闭先赢：poll 期间关闭已提交；原在途输入最后认领失败（G1 断言前置）
    assert runtime.input_switch_allowed("arm-01") is False


def test_r2_03_poll_cancellation_cleans_promptly(geometry_env):
    """取消的 poll 等待立即退出，不遗留等待者/线程。"""
    import asyncio
    geometry = geometry_env["runtime"].ime_geometry
    r = _geometry_handshake(geometry_env["client"], "k40-b", token="tok-b-00000000000000b")
    session_id = r.json()["runtime_session_id"]

    async def scenario():
        task = asyncio.create_task(geometry.poll_next_async(
            "k40-b", session_id, 20, credentials="tok-b-00000000000000b"))
        await asyncio.sleep(0.2)
        task.cancel()
        started = asyncio.get_running_loop().time()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return asyncio.get_running_loop().time() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.0, f"取消后 {elapsed:.1f}s 才退出（应立即）"


# ---------------------------------------------------------------- R2-21

def test_r2_21_missing_credentials_rejected_all_entries(geometry_env):
    """无凭据：三个几何入口一律 401 ime_geometry_unauthenticated（会话/nonce 禁止）。"""
    client, runtime = geometry_env["client"], geometry_env["runtime"]
    r = _geometry_handshake(client, "k40-b", token=None)
    assert (r.status_code, r.json()["error"]) == \
        (401, "ime_geometry_unauthenticated"), r.text
    # report / poll 同样拒绝
    rr = client.post("/api/v1/devices/k40-b/geometry", json={"any": 1})
    assert rr.status_code == 401
    rp = client.get("/api/v1/devices/k40-b/geometry/requests/next")
    assert rp.status_code == 401
    # 撤销后原令牌也拒绝（凭据可撤销）
    runtime.revoke_phone_credential("k40-b")
    r2 = _geometry_handshake(client, "k40-b", token="tok-b-00000000000000b")
    assert r2.status_code == 401
    runtime.register_phone_credential("k40-b", "tok-b-00000000000000b2")
    assert _geometry_handshake(client, "k40-b", token="tok-b-00000000000000b2").status_code == 200


def test_r2_21_phone_a_cannot_access_b_session(geometry_env):
    """A 的有效凭据访问 B 的 handshake/report/poll → 403；B 会话原样不变。"""
    client, runtime = geometry_env["client"], geometry_env["runtime"]
    b = _geometry_handshake(client, "k40-b", token="tok-b-00000000000000b")
    assert b.status_code == 200
    session_id = b.json()["runtime_session_id"]
    # A 的凭据访问 B 路径
    r1 = _geometry_handshake(client, "k40-b", token="tok-a-00000000000000a")
    assert (r1.status_code, r1.json()["error"]) == (403, "ime_geometry_forbidden")
    r2 = client.post("/api/v1/devices/k40-b/geometry", json={
        "contract_version": "ime-geometry.v1"},
        headers={"x-ime-phone-token": "tok-a-00000000000000a"})
    assert r2.status_code == 403
    r3 = client.get("/api/v1/devices/k40-b/geometry/requests/next"
                    f"?runtime_session_id={session_id}",
                    headers={"x-ime-phone-token": "tok-a-00000000000000a"})
    assert r3.status_code == 403
    # B 会话原样：B 仍可正常领取 nonce
    nonce = runtime.ime_geometry.create_nonce_request("k40-b")
    got = client.get("/api/v1/devices/k40-b/geometry/requests/next"
                     f"?runtime_session_id={session_id}",
                     headers={"x-ime-phone-token": "tok-b-00000000000000b"})
    assert got.status_code == 200 and got.json()["request_nonce"] == nonce


def test_r2_21_b_legitimate_chain_completes(geometry_env):
    """B 凭据走完整合法链：握手→nonce 派发→领取→快照上报接受。"""
    client, runtime = geometry_env["client"], geometry_env["runtime"]
    b = _geometry_handshake(client, "k40-b", token="tok-b-00000000000000b")
    assert b.status_code == 200
    session_id = b.json()["runtime_session_id"]
    runtime.ime_geometry.create_nonce_request("k40-b")
    poll = client.get("/api/v1/devices/k40-b/geometry/requests/next"
                      f"?runtime_session_id={session_id}",
                      headers={"x-ime-phone-token": "tok-b-00000000000000b"})
    assert poll.status_code == 200
    nonce = poll.json()["request_nonce"]
    body = {
        "contract_version": "ime-geometry.v1",
        "runtime_session_id": session_id,
        "view_instance_id": str(uuid.uuid4()),
        "seq": 1, "captured_uptime_ms": 1000,
        "display": {"display_id": 0, "width_px": 1080, "height_px": 2400,
                    "rotation": 0, "density": 2.75},
        "layout_version": "v1", "keyboard_active": True,
        "geometry_valid": True,
        "keyboard_bounds": None, "current_task_id": None,
        "reason": "snapshot", "request_nonce": nonce,
        "targets": [
            {"id": "ime_candidate_bar", "visible": True, "enabled": True,
             "clickable": True, "bounds": {"left": 54, "top": 2160,
                                           "right": 1026, "bottom": 2280}},
            {"id": "ime_delete_key", "visible": True, "enabled": True,
             "clickable": True, "bounds": {"left": 960, "top": 1960,
                                           "right": 1050, "bottom": 2050}},
        ],
    }
    report = client.post("/api/v1/devices/k40-b/geometry", json=body,
                         headers={"x-ime-phone-token": "tok-b-00000000000000b"})
    assert report.status_code == 200, report.text
    assert report.json()["accepted_seq"] == 1


def test_r2_21_unbound_and_multi_bound_rejected(geometry_env):
    """零绑定/多绑定手机凭据 → 403 forbidden（凭据本身有效）。"""
    client, runtime = geometry_env["client"], geometry_env["runtime"]
    runtime.register_phone_credential("k40-ghost", "tok-ghost-0000000000g")
    r = _geometry_handshake(client, "k40-ghost", token="tok-ghost-0000000000g")
    assert (r.status_code, r.json()["error"]) == (403, "ime_geometry_forbidden")


def test_r2_22_revision_refresh_replay_returns_original(env):
    """显式钉死（复核条件 2）：expected_revision 不计入核对摘要——
    首次 CAS 后，客户端刷新 revision 重发同内容必须返回原结果。"""
    runtime = env["runtime"]
    control = runtime.input_control
    i1 = _seed_issue(runtime, "input_effect_unknown")["issues"][0]["issue_id"]
    before = control.ensure_guard("arm-01")["revision"]
    r1 = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                            json=_review_body("rev-refresh", [i1], before))
    assert r1.status_code == 200
    first = r1.json()
    mid = control.ensure_guard("arm-01")["revision"]
    assert mid == before + 1
    # 刷新 revision（>=mid 的任意当前值）重发同内容：原结果原样
    r2 = env["client"].post("/v1/devices/arm-01/input-recovery/review",
                            json=_review_body("rev-refresh", [i1], mid + 5))
    assert r2.status_code == 200, r2.text
    assert r2.json()["recovery_revision"] == first["recovery_revision"]
    assert control.ensure_guard("arm-01")["revision"] == mid
