"""Calibration and later execution preserve the same contact duration."""
import pytest

from drivers import calibration_bundle as cb
from drivers import calibration_wizard as cw
from tests.test_calibration_wizard import (
    auto_ready_env, finish_e_acceptance, wait_auto, wait_e_execution,
)
from tests.test_calibration_execution_model import failed_e_snapshot, resumable_e  # noqa: F401


@pytest.mark.parametrize("dwell", [0, 120, 2000])
def test_auto_start_freezes_configured_tap_dwell(tmp_path, dwell):
    env = auto_ready_env(tmp_path)
    env.wizard.state["z"]["tap"] = {"depth": 3, "feed": 1000, "dwellMs": dwell}
    try:
        view = env.wizard.auto_start(defer_input_acceptance=True)
        candidate = cb.load_candidate("arm-01", view["runId"], tmp_path)
        assert candidate["z"]["tapDwellMs"] == dwell
    finally:
        env.wizard.auto_stop()


@pytest.mark.parametrize("defer_input", [True, False])
def test_sampling_acceptance_and_promotion_use_frozen_dwell(tmp_path, defer_input):
    env = auto_ready_env(tmp_path)
    env.wizard.state["z"]["tap"] = {"depth": 3, "feed": 1000, "dwellMs": 120}
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
    env.wizard.state["z"]["tap"] = {"dwellMs": dwell}
    commands = list(env.daemon.cmds)
    with pytest.raises(cw.WizardError, match="普通点击停留"):
        env.wizard.auto_start()
    assert env.daemon.cmds == commands
    assert env.wizard._auto_runner._auto["run"] is None


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
