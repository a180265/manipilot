"""Automatic calibration qualifies READY without an E input acceptance step."""
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb, calibration_wizard as cw
from runtime.device_eligibility import INPUT_TASK_TYPES, compute_device_eligibility
from tests.test_calibration_bundle import _gate, _ready_bundle
from tests.test_calibration_wizard import Env, auto_ready_env, wait_auto


def motion_bundle():
    bundle = _ready_bundle()
    bundle["status"] = cb.MOTION_READY_STATUS
    bundle["acceptance"].update(debug=False, deferred={"debug": True, "wechat": True})
    bundle["z"].update(zSafe=2.0, deleteValidated=False)
    bundle["safePark"] = {"confirmed": True, "parkX": 0.0, "parkY": 0.0, "parkZ": 2.0}
    bundle["modelLocks"] = {
        key: {"state": cb.MODEL_LOCK_LOCKED, "version": 1}
        for key in ("cameraToScreen", "armAffine")
    }
    bundle["validation"] = {
        "passed": True, "runId": "run-mechanical", "tolerancePx": 10.0,
        "points": [{"targetId": f"validation-{index}", "passed": True,
                    "touchTargetErrorPx": 1.0} for index in range(1, 6)],
    }
    return bundle


def test_mechanical_scope_passes_without_claiming_input_success():
    bundle = motion_bundle()
    original = deepcopy(bundle)
    assert _gate(bundle) == []
    assert cb.input_acceptance_deferred(bundle) is True
    assert bundle == original


@pytest.mark.parametrize("scope", ["wechat_send", "require_input_acceptance"])
def test_input_and_send_scopes_no_longer_gated_by_e_acceptance(scope):
    """2026-10-03 规格 §4.6：输入/发送域不再由 E 验收决定——同一 motion_ready
    档案无论 scope 如何都过机械门；输入资格由「允许输入与发送」开关层负责。"""
    checks = _gate(motion_bundle(), **{scope: True})
    codes = {check["code"] for check in checks}
    assert "input_acceptance_deferred" not in codes
    assert "input_acceptance_missing" not in codes
    assert "delete_parameters_not_validated" not in codes


@pytest.mark.parametrize("field,value", [
    ("debug", False), ("debug", "false"), ("debug", None),
])
def test_deferred_marker_is_inert_for_mechanical_qualification(field, value):
    """§4.6：E 延后标志退出机械资格表达式——它的取值不再影响 ready/motion_ready。"""
    bundle = motion_bundle()
    bundle["acceptance"]["deferred"][field] = value
    assert _gate(bundle) == []


@pytest.mark.parametrize("corrupt", [
    # acceptance.debug / deleteValidated 已退役（§4.6），不再是机械证据；
    lambda b: b["z"].update(validated=False),
    lambda b: b["modelLocks"].clear(),
    lambda b: b["validation"]["points"].pop(),
    lambda b: b["validation"]["points"][0].update(passed=False),
    lambda b: b["validation"]["points"][0].update(touchTargetErrorPx=99),
    lambda b: b["validation"].update(tolerancePx=float("nan")),
    lambda b: b["safePark"].update(confirmed=False),
    lambda b: b["safePark"].update(parkZ=99),
])
def test_mechanical_qualification_rejects_incomplete_or_false_facts(corrupt):
    bundle = motion_bundle()
    corrupt(bundle)
    assert _gate(bundle)


@pytest.mark.parametrize("facts,code", [
    ({"daemon_session": "new-session"}, "daemon_session_changed"),
    ({"binding_revision": 8}, "camera_binding_revision_changed"),
    ({"estop_locked": True}, "motion_blocked"),
    ({"motion_result_unknown": True}, "motion_result_unknown"),
    ({"screen_edge_deviation_px": 20}, "screen_edge_changed"),
])
def test_deferred_mode_keeps_physical_safety_checks(facts, code):
    assert code in {item["code"] for item in _gate(motion_bundle(), **facts)}


def test_ready_with_deferred_marker_keeps_mechanical_gate_only():
    """§4.6：ready 只表示机械校准有效；E 延后标记不再产生任何拒绝项。"""
    bundle = _ready_bundle()
    bundle["acceptance"]["deferred"] = {"debug": True}
    assert _gate(bundle) == []
    codes = {item["code"] for item in _gate(bundle, require_input_acceptance=True)}
    assert "input_acceptance_deferred" not in codes
    assert "input_acceptance_missing" not in codes


@pytest.mark.parametrize("keyboard_active", [True, False])
def test_device_input_gate_blocks_when_switch_off_even_without_keyboard_requirement(
        tmp_path, keyboard_active):
    device = {
        "device_id": "arm-01", "status": "ready", "calibration_status": "validated",
        "device_mode": "ready_manual",
        "_settings_data_root": tmp_path,  # 无开关文件=缺省关（§4.1 首次安装）
        "ime_device_id": "phone", "enforce_input_gate": False,
    }
    ime = {"online": True, "keyboardActive": keyboard_active}
    ordinary = compute_device_eligibility(device, ime_status=ime)
    assert ordinary["can_accept_task"] is True
    input_view = compute_device_eligibility(
        device, ime_status=ime, require_input=True, require_keyboard_active=False)
    assert input_view["blocking_checks"] == ["input_switch_disabled"]
    assert not input_view["can_accept_task"]
    assert not input_view["can_manual_send"]
    assert not input_view["can_managed"]
    assert "wx_session_send" in INPUT_TASK_TYPES


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, {}])
def test_defer_mode_requires_literal_boolean_before_any_activity(tmp_path, value):
    env = Env(tmp_path)
    before = deepcopy(env.wizard.state)
    with pytest.raises(cw.WizardError, match="布尔"):
        env.wizard.auto_start(defer_input_acceptance=value)
    assert env.wizard.state == before
    assert env.daemon.tap_count == 0


def deferred_env(tmp_path):
    env = auto_ready_env(tmp_path)
    env.wizard.state["imeDeviceId"] = "ime-deferred-test"
    env.ime.sessions[env.wizard.state["imeSessionId"]]["deviceId"] = "ime-deferred-test"
    # Stale successful acceptance and delete confirmation must not be inherited.
    env.wizard.state["acceptance"]["debug"] = {
        check: {"ok": True, "executedAt": "old"} for check in cw.DEBUG_ACCEPTANCE_CHECKS}
    bundle = cb.load_bundle("arm-01", tmp_path)
    bundle["imeDeviceId"] = "ime-deferred-test"
    bundle["acceptance"]["debug"] = True
    bundle["z"]["deleteValidated"] = True
    cb.save_bundle(bundle, tmp_path)
    env.wizard.auto_start()
    view = wait_auto(env, lambda v: v["state"] not in cw.AUTO_ACTIVE_STATES)
    assert view["state"] == cw.AUTO_STATE_PROMOTED, view
    return env


def auto_gate(bundle, **scope):
    frame = bundle["frame"]
    return cb.validate_calibration_bundle(
        bundle, device_id=bundle["deviceId"], camera_id=bundle["cameraId"],
        camera_device_unique_id=bundle["cameraDeviceUniqueId"],
        binding_revision=bundle["bindingRevision"],
        daemon_addr=bundle["daemonAddr"], frame_width=frame["width"],
        frame_height=frame["height"], daemon_session=bundle["daemonSession"],
        **scope)


def test_default_auto_run_defers_input_acceptance_and_promotes_motion_ready(tmp_path):
    """2026-09-29 拍板：默认自动 run 延后输入验收，提升 motion_ready 机械资格。

    E 全过后由 finish() 置 ready；延后期间输入/发送域被门禁拒绝，
    且不带执行证据的 E 确认不受理。
    """
    env = deferred_env(tmp_path)
    bundle = cb.load_bundle("arm-01", tmp_path)
    run = cb.load_candidate("arm-01", bundle["autoCalibration"]["runId"], tmp_path)
    assert bundle["status"] == cb.MOTION_READY_STATUS
    assert cb.automatic_calibration_verified(bundle)
    assert bundle["acceptance"]["debug"] is False
    assert bundle["acceptance"]["deferred"]["debug"] is True
    assert bundle["z"]["deleteValidated"] is False
    assert env.wizard.state["acceptance"]["debug"] == {}
    assert env.wizard.state["acceptance"]["deferred"] == {"debug": True}
    assert env.wizard.state["stage"] == cw.STAGE_DEBUG_ACCEPTANCE
    assert env.ime.tasks_created == []
    assert len(run["pointPlan"]) == 9
    assert len(run["validation"]["points"]) == 5
    assert run["validation"]["passed"] is True
    phases = {event["phase"] for event in run["events"]}
    assert {"PHASE0_CAMERA", "FIRST_CLICK_GATE", "PHASE1_ARM",
            "PHASE2_VALIDATE", "INDEPENDENT_VALIDATE"} <= phases
    assert "E_ACCEPTANCE" not in phases
    assert run["lastPark"]
    assert env.wizard.auto_view()["qualificationStatus"] == "motion_ready"
    assert env.wizard._deferred_acceptance_bundle() is not None
    assert auto_gate(bundle) == []
    # §4.6：输入/发送 scope 不再由 E 验收拒绝；资格由「允许输入与发送」开关层负责
    codes_input = {item["code"] for item in auto_gate(bundle, require_input_acceptance=True)}
    codes_send = {item["code"] for item in auto_gate(bundle, wechat_send=True)}
    assert "input_acceptance_deferred" not in codes_input
    assert "input_acceptance_missing" not in codes_input
    assert "input_acceptance_deferred" not in codes_send
    with pytest.raises(cw.WizardError, match="验收未完成"):
        env.wizard.finish()
    # 无执行证据的 E 确认不受理：必须先真实执行本项验收
    with pytest.raises(cw.WizardError, match="E 验收"):
        env.wizard.acceptance_record("debug", "ime_input_tap", True)


def test_ready_auto_bundle_rejects_new_daemon_session(tmp_path):
    env = deferred_env(tmp_path)
    env.daemon.session = "changed"
    bundle = cb.load_bundle("arm-01", tmp_path)
    assert "daemon_session_changed" in {
        item["code"] for item in cb.validate_calibration_bundle(
            bundle, device_id=bundle["deviceId"], camera_id=bundle["cameraId"],
            camera_device_unique_id=bundle["cameraDeviceUniqueId"],
            binding_revision=bundle["bindingRevision"],
            daemon_addr=bundle["daemonAddr"],
            frame_width=bundle["frame"]["width"],
            frame_height=bundle["frame"]["height"],
            daemon_session="changed")}


def test_ready_auto_bundle_survives_reload_without_input_acceptance(tmp_path):
    env = deferred_env(tmp_path)
    bundle = cb.load_bundle("arm-01", tmp_path)
    env.wizard._save_state()
    restored = cw.CalibrationWizard(
        device="arm-01", daemon=env.daemon, ime=env.ime, data_root=tmp_path,
        binding_facts=env.wizard._binding_facts)
    # motion_ready + 输入验收延后：重启后停在 E 验收阶段，延后标记不丢
    assert restored.state["stage"] == cw.STAGE_DEBUG_ACCEPTANCE
    assert restored.state["acceptance"]["deferred"] == {"debug": True}
    assert restored._deferred_acceptance_bundle() is not None
    assert cb.load_bundle("arm-01", tmp_path) == bundle


@pytest.mark.parametrize("confirmation", ["z", "park"])
def test_deferred_mode_cannot_skip_motion_preconditions(tmp_path, confirmation):
    env = auto_ready_env(tmp_path)
    if confirmation == "z":
        env.wizard.state["z"]["autoSafe"]["validated"] = False
    else:
        env.wizard.state["safePark"]["confirmed"] = False
    taps = env.daemon.tap_count
    with pytest.raises(cw.WizardError):
        env.wizard.auto_start(defer_input_acceptance=True)
    assert env.daemon.tap_count == taps
    assert not env.wizard.auto_run_active()


def test_deferred_mode_cancellation_never_promotes(tmp_path, monkeypatch):
    env = auto_ready_env(tmp_path)
    original = cb.load_bundle("arm-01", tmp_path)

    def cancel(_run):
        raise cw._AutoCancelled()

    monkeypatch.setattr(env.wizard._auto_runner, "_auto_validate_independent", cancel)
    env.wizard.auto_start(defer_input_acceptance=True)
    view = wait_auto(env, lambda v: v["state"] not in cw.AUTO_ACTIVE_STATES)
    assert view["state"] == cw.AUTO_STATE_STOPPED
    assert cb.load_bundle("arm-01", tmp_path) == original
    assert env.ime.tasks_created == []


def test_deferred_independent_failure_does_not_promote(tmp_path, monkeypatch):
    env = auto_ready_env(tmp_path)
    original = cb.load_bundle("arm-01", tmp_path)

    def fail(_run):
        raise cw.AutoRunError("independent_validation_failed", "test validation failure")

    monkeypatch.setattr(env.wizard._auto_runner, "_auto_validate_independent", fail)
    env.wizard.auto_start(defer_input_acceptance=True)
    view = wait_auto(env, lambda v: v["state"] not in cw.AUTO_ACTIVE_STATES)
    assert view["state"] == cw.AUTO_STATE_FAILED
    assert cb.load_bundle("arm-01", tmp_path) == original
    assert env.ime.tasks_created == []


def test_promote_tail_invokes_post_promote_hook_once(tmp_path):
    """A5（2026-10-01 拍板）：PROMOTE 尾部调用宿主注入的平台同步钩子
    （恰好一次、带设备号）；失败 run 不调用。"""
    calls: list[tuple] = []
    env = auto_ready_env(tmp_path)
    env.wizard.post_promote_hook = lambda device, run_id: calls.append((device, run_id))
    env.wizard.state["imeDeviceId"] = "ime-hook-test"
    env.ime.sessions[env.wizard.state["imeSessionId"]]["deviceId"] = "ime-hook-test"
    bundle = cb.load_bundle("arm-01", tmp_path)
    bundle["imeDeviceId"] = "ime-hook-test"
    cb.save_bundle(bundle, tmp_path)
    env.wizard.auto_start()
    wait_auto(env, lambda v: v["state"] not in cw.AUTO_ACTIVE_STATES)
    assert [c[0] for c in calls] == ["arm-01"] and all(c[1] for c in calls), calls
