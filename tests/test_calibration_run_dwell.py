"""Calibration and later execution preserve the same contact duration."""
import asyncio
import pytest

from drivers import calibration_bundle as cb
from drivers import calibration_wizard as cw
from tests.test_calibration_wizard import (
    auto_ready_env, finish_e_acceptance, wait_auto, wait_e_execution,
)
from tests.test_calibration_execution_model import failed_e_snapshot, resumable_e  # noqa: F401
from tests.test_daemon_arm import daemon_and_driver_ui, enable_ui_target  # noqa: F401


@pytest.mark.parametrize("dwell", [0, 120, 1000])
def test_auto_start_freezes_configured_tap_dwell(tmp_path, dwell):
    env = auto_ready_env(tmp_path)
    env.wizard.z_confirm_safe(tap_depth=3, tap_feed=1000, tap_dwell_ms=dwell)
    # An old UI/manual working copy cannot replace the actual confirmation snapshot.
    env.wizard.state["z"]["tap"]["dwellMs"] = 900
    try:
        view = env.wizard.auto_start(defer_input_acceptance=True)
        candidate = cb.load_candidate("arm-01", view["runId"], tmp_path)
        assert candidate["z"]["tapDwellMs"] == dwell
    finally:
        env.wizard.auto_stop()


@pytest.mark.parametrize("defer_input", [True, False])
def test_sampling_acceptance_and_promotion_use_frozen_dwell(tmp_path, defer_input):
    env = auto_ready_env(tmp_path)
    env.wizard.z_confirm_safe(tap_depth=3, tap_feed=1000, tap_dwell_ms=120)
    command_start = len(env.daemon.cmds)
    try:
        view = env.wizard.auto_start(defer_input_acceptance=defer_input)
        with env.wizard._lock:
            env.wizard.state["z"]["tap"]["dwellMs"] = 900
        if not defer_input:
            wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE", desc="E 验收")
            finish_e_acceptance(env, view["runId"])
        wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="固定停留参数提升")

        taps = [c for c in env.daemon.cmds[command_start:] if c.startswith("TAP ")]
        ordinary_taps = [c for c in taps if " P2" not in c]
        assert ordinary_taps and all(" P0.12" in c for c in ordinary_taps), taps
        candidate = cb.load_candidate("arm-01", view["runId"], tmp_path)
        assert {s["phase"] for s in candidate["samples"]} >= {
            "PHASE1_ARM", "PHASE2_VALIDATE", "INDEPENDENT_VALIDATE"}
        assert candidate["z"]["tapDwellMs"] == 120
        assert cb.load_bundle("arm-01", tmp_path)["z"]["tapDwellMs"] == 120
        assert env.wizard.state["z"]["tap"]["dwellMs"] == 120
    finally:
        env.wizard.auto_stop()


@pytest.mark.parametrize("dwell", [-1, 2001, None, "invalid", float("nan"), float("inf")])
def test_invalid_configured_dwell_rejects_run_before_motion(tmp_path, dwell):
    env = auto_ready_env(tmp_path)
    env.wizard.state["z"]["autoSafe"].pop("tapDwellMs", None)  # historical confirmation
    env.wizard.state["z"]["tap"] = {"dwellMs": dwell}
    commands = list(env.daemon.cmds)
    with pytest.raises(cw.WizardError, match="普通点击停留"):
        env.wizard.auto_start()
    assert env.daemon.cmds == commands
    assert env.wizard._auto_runner._auto["run"] is None


@pytest.mark.parametrize("dwell", [0, 50, 100, 1000])
def test_safe_confirmation_tests_and_saves_exact_dwell_once(tmp_path, dwell):
    env = auto_ready_env(tmp_path)
    before = len(env.daemon.cmds)
    result = env.wizard.z_confirm_safe(tap_depth=3, tap_feed=1500, tap_dwell_ms=dwell)
    taps = [c for c in env.daemon.cmds[before:] if c.startswith("TAP")]
    expected = "TAP 3.000 F1500" + (f" P{dwell / 1000:g}" if dwell else "")
    assert taps == [expected]
    assert result["tapDwellMs"] == dwell
    assert env.wizard.state["z"]["autoSafe"]["tapDwellMs"] == dwell
    assert env.wizard.state["z"]["tap"]["dwellMs"] == dwell
    assert cb.load_bundle("arm-01", tmp_path)["z"]["tapDwellMs"] == dwell


def test_omitted_confirmation_dwell_preserves_previously_saved_value(tmp_path):
    env = auto_ready_env(tmp_path)
    env.wizard.z_confirm_safe(tap_depth=3, tap_dwell_ms=50)
    start = len(env.daemon.cmds)
    result = env.wizard.z_confirm_safe(tap_depth=3)
    assert result["tapDwellMs"] == 50
    assert [line for line in env.daemon.cmds[start:] if line.startswith("TAP")] == [
        "TAP 3.000 F1500 P0.05"]


def test_manual_parameter_save_revokes_different_automatic_confirmation(tmp_path):
    env = auto_ready_env(tmp_path)
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3, 1500, 50)
    env.wizard.z_save("tap")
    assert env.wizard.state["z"]["tap"]["dwellMs"] == 50
    assert env.wizard.state["z"]["autoSafe"]["validated"] is False
    before = list(env.daemon.cmds)
    with pytest.raises(cw.WizardError, match="尚未通过验证"):
        env.wizard.auto_start()
    assert env.daemon.cmds == before


@pytest.mark.parametrize("dwell", [-1, 2001, True, 1.5, "50", float("nan"), float("inf")])
def test_invalid_confirmation_dwell_has_no_contact_or_saved_parameter_change(tmp_path, dwell):
    env = auto_ready_env(tmp_path)
    before = list(env.daemon.cmds)
    saved = cb.load_bundle("arm-01", tmp_path)["z"]
    with pytest.raises(cw.WizardError, match="普通点击停留"):
        env.wizard.z_confirm_safe(tap_depth=3, tap_feed=1500, tap_dwell_ms=dwell)
    assert env.daemon.cmds == before
    assert cb.load_bundle("arm-01", tmp_path)["z"] == saved


def test_ready_dwell_change_requires_new_calibration_and_does_not_rewrite_qualification(tmp_path):
    env = auto_ready_env(tmp_path)
    try:
        env.wizard.auto_start()
        wait_auto(env, lambda v: v["state"] == "PROMOTED")
        saved = cb.load_bundle("arm-01", tmp_path)
        before = list(env.daemon.cmds)
        with pytest.raises(cw.WizardError, match="重新开始校准"):
            env.wizard.z_confirm_safe(tap_depth=3, tap_feed=1500, tap_dwell_ms=100)
        assert env.daemon.cmds == before
        assert cb.load_bundle("arm-01", tmp_path) == saved
    finally:
        env.wizard.auto_stop()


@pytest.mark.parametrize("stored_dwell", [None, 120])
def test_resumed_candidate_uses_its_dwell_and_legacy_defaults_to_zero(
        resumable_e, tmp_path, stored_dwell):
    env, run = resumable_e
    if stored_dwell is None:
        run["z"].pop("tapDwellMs", None)
    else:
        run["z"]["tapDwellMs"] = stored_dwell
    env.wizard.state["z"]["tap"] = {"depth": 3, "feed": 1000, "dwellMs": 900}
    cb.save_candidate(run, tmp_path)
    command_start = len(env.daemon.cmds)
    try:
        env.wizard.auto_resume_acceptance(run["runId"])
        for check in cw.DEBUG_ACCEPTANCE_CHECKS:
            wait_e_execution(env, run["runId"], check)
            env.wizard.auto_acceptance_record(run["runId"], "debug", check, True, "测试确认")
        wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="恢复候选停留参数")
        taps = [c for c in env.daemon.cmds[command_start:]
                if c.startswith("TAP ") and " P2" not in c]
        assert len(taps) == 1
        if stored_dwell is None:
            assert " P" not in taps[0]
        else:
            assert " P0.12" in taps[0]
        assert cb.load_bundle("arm-01", tmp_path)["z"]["tapDwellMs"] == (stored_dwell or 0)
    finally:
        env.wizard.auto_stop()


def test_invalid_restored_dwell_rejects_resume_before_motion(resumable_e, tmp_path):
    env, run = resumable_e
    run["z"]["tapDwellMs"] = 2001
    cb.save_candidate(run, tmp_path)
    commands = list(env.daemon.cmds)
    with pytest.raises(cw.WizardError, match="普通点击停留"):
        env.wizard.auto_resume_acceptance(run["runId"])
    assert env.daemon.cmds == commands


@pytest.mark.parametrize("dwell", [0, 50, 100])
def test_business_single_click_consumes_device_calibration_dwell(daemon_and_driver_ui, dwell, monkeypatch):
    daemon, driver, root = daemon_and_driver_ui
    monkeypatch.setattr(driver, "_input_switch_allowed", lambda _: True)
    bundle = cb.load_bundle("arm-01", root)
    bundle["z"]["tapDwellMs"] = dwell
    cb.save_bundle(bundle, root)
    enable_ui_target(root, "send_button", nx=.5, ny=.5)
    # arm.click 1.2.0：省略 tapDwellMs 时消费档案停留；显式提交时按本次调用值执行。
    asyncio.run(driver.click({"target_id": "send_button"}))
    assert [line for line in daemon.commands if line.startswith("TAP")] == [
        "TAP 3.000 F1500" + (f" P{dwell / 1000:g}" if dwell else "")]
    asyncio.run(driver.click({"target_id": "send_button", "tapDwellMs": 250}))
    assert [line for line in daemon.commands if line.startswith("TAP")][-1] == "TAP 3.000 F1500 P0.25"
