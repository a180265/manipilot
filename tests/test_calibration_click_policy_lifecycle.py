"""Automatic calibration preserves the device's explicitly selected click policy."""
from copy import deepcopy
import socket

import pytest

from drivers import calibration_bundle as cb, calibration_wizard as cw
from tests.test_calibration_execution_model import failed_e_snapshot, resumable_e  # noqa: F401
from tests.test_calibration_wizard import (
    auto_ready_env, finish_e_acceptance, wait_auto, wait_e_execution,
)


POLICY = {"mode": "screen_interior", "preferredInsetsPx": {
    "left": 0, "top": 150, "right": 0, "bottom": 0}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("policy lifecycle tests must not access hardware or network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def save_policy(root, value):
    bundle = cb.load_bundle("arm-01", root)
    if value is None:
        bundle["xy"].pop("clickPointPolicy", None)
    else:
        bundle["xy"]["clickPointPolicy"] = deepcopy(value)
    cb.save_bundle(bundle, root)
    return bundle


@pytest.mark.parametrize("deferred", [True, False])
@pytest.mark.parametrize("enabled", [True, False])
def test_real_auto_start_and_promotion_preserve_policy(tmp_path, deferred, enabled):
    env = auto_ready_env(tmp_path)
    expected = POLICY if enabled else None
    save_policy(tmp_path, expected)
    try:
        started = env.wizard.auto_start(defer_input_acceptance=deferred)
        candidate = cb.load_candidate("arm-01", started["runId"], tmp_path)
        assert candidate["clickPointPolicy"] == expected
        if not deferred:
            view = wait_auto(env, lambda value: value["phase"] == "E_ACCEPTANCE"
                             or value["state"] not in cw.AUTO_ACTIVE_STATES)
            assert view["phase"] == "E_ACCEPTANCE", view
            finish_e_acceptance(env, view["runId"])
        view = wait_auto(env, lambda value: value["state"] not in cw.AUTO_ACTIVE_STATES)
        assert view["state"] == cw.AUTO_STATE_PROMOTED, view
        env.wizard._auto_runner._auto["thread"].join(timeout=2)
        saved = cb.load_bundle("arm-01", tmp_path)
        assert saved["xy"].get("clickPointPolicy") == expected
        assert ("clickPointPolicy" in saved["xy"]) is enabled
        # Session rebind retains the preference while requiring a new session review.
        env.wizard._mark_session_rebind("policy-lifecycle-test")
        env.wizard.park_clear()
        rebound = cb.load_bundle("arm-01", tmp_path)
        assert env.wizard.state["sessionRebind"]["required"] is True
        if deferred:
            assert rebound["status"] == "draft"
        assert rebound["xy"].get("clickPointPolicy") == expected
    finally:
        env.wizard.auto_stop()


@pytest.mark.parametrize("value", [{}, {**POLICY, "mode": "invalid"}])
def test_invalid_policy_rejects_auto_start_before_motion(tmp_path, value):
    env = auto_ready_env(tmp_path)
    save_policy(tmp_path, value)
    commands = list(env.daemon.cmds)
    with pytest.raises(cw.WizardError, match="选点策略"):
        env.wizard.auto_start()
    assert env.daemon.cmds == commands
    assert env.wizard._auto_runner._auto["run"] is None


@pytest.mark.parametrize("change", ["add", "remove", "alter"])
def test_policy_change_stops_next_point_before_motion(tmp_path, monkeypatch, change):
    env = auto_ready_env(tmp_path)
    save_policy(tmp_path, None if change == "add" else POLICY)
    runner = env.wizard._auto_runner
    monkeypatch.setattr(runner, "_auto_spawn", lambda *args, **kwargs: None)
    started = env.wizard.auto_start(defer_input_acceptance=True)
    run = cb.load_candidate("arm-01", started["runId"], tmp_path)
    new = deepcopy(POLICY)
    if change == "alter":
        new["preferredInsetsPx"]["top"] += 1
    save_policy(tmp_path, None if change == "remove" else new)
    commands = list(env.daemon.cmds)
    with pytest.raises(cw.AutoRunError, match="选点策略"):
        runner._auto_guard(run)
    assert env.daemon.cmds == commands


@pytest.mark.parametrize("enabled", [True, False])
def test_legacy_candidate_resume_only_allows_absent_policy(resumable_e, tmp_path, enabled):
    env, run = resumable_e
    run.pop("clickPointPolicy", None)
    base = save_policy(tmp_path, POLICY if enabled else None)
    # Old candidates did hash xy, but that alone is not a frozen policy snapshot.
    from drivers.calibration_auto_runner import AUTO_PROMOTE_GUARD_KEYS
    run["baseGeometryHash"] = cb.profile_hash(base, keys=AUTO_PROMOTE_GUARD_KEYS)
    cb.save_candidate(run, tmp_path)
    commands = list(env.daemon.cmds)
    if enabled:
        with pytest.raises(cw.AutoRunError, match="选点策略"):
            env.wizard.auto_resume_acceptance(run["runId"])
        assert env.daemon.cmds == commands
    else:
        try:
            env.wizard.auto_resume_acceptance(run["runId"])
            for check in cw.DEBUG_ACCEPTANCE_CHECKS:
                wait_e_execution(env, run["runId"], check)
                env.wizard.auto_acceptance_record(run["runId"], "debug", check, True, "测试确认")
            view = wait_auto(env, lambda value: value["state"] not in cw.AUTO_ACTIVE_STATES)
            assert view["state"] == cw.AUTO_STATE_PROMOTED, view
            assert "clickPointPolicy" not in cb.load_bundle("arm-01", tmp_path)["xy"]
        finally:
            env.wizard.auto_stop()
