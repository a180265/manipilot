"""边缘标靶保持中心，遮挡复核回停车位且不更换几何基准。"""
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb
from drivers.calibration_wizard import CalibrationWizard, WizardError
from tests.test_calibration_wizard import auto_ready_env, wait_auto


@pytest.mark.parametrize("u,v", [(.05, .05), (.95, .95), (.5, .5)])
def test_edge_target_keeps_requested_center(u, v):
    target = CalibrationWizard._target_rect(None, u, v)
    assert (target["u"], target["v"]) == (u, v)
    assert 0 <= u - target["widthU"] / 2 <= u + target["widthU"] / 2 <= 1
    assert 0 <= v - target["heightV"] / 2 <= v + target["heightV"] / 2 <= 1


@pytest.mark.parametrize("u,v", [(0, .5), (1, .5), (.5, float("nan"))])
def test_target_rejects_invalid_center(u, v):
    with pytest.raises(WizardError):
        CalibrationWizard._target_rect(None, u, v)


@pytest.mark.parametrize("recovery", ["fresh", "unverified", "stale"])
def test_unverified_touch_pose_rechecks_original_geometry_from_park(tmp_path, monkeypatch, recovery):
    recovers = recovery == "fresh"
    env = auto_ready_env(tmp_path)
    runner = env.wizard._auto_runner
    original_check = runner._auto_require_stable_phone
    original_park = runner._auto_safe_park
    state = {"injected": False, "parked": False, "corners": None, "tap_count": None}

    def check(run, **kwargs):
        if run["phase"] == "PHASE1_ARM" and not state["injected"]:
            state["injected"] = True
            state["corners"] = deepcopy(run["edge"]["corners"])
            state["tap_count"] = env.daemon.tap_count
        if state["injected"] and not state["parked"]:
            return None  # 笔杆遮挡：动作前warn，回报后必须移开核实。
        if state["injected"] and state["parked"] and not state.get("rechecked"):
            state["rechecked"] = True
            assert type(kwargs.get("after_frame_seq")) is int
            if recovery == "unverified":
                return None
            if recovery == "stale":
                original_quad = env.wizard._edge_quad
                def stale_quad(**_):
                    quad = original_quad()
                    quad["frameSeq"] = kwargs["after_frame_seq"]
                    return quad
                monkeypatch.setattr(env.wizard, "_edge_quad", stale_quad)
                try:
                    return original_check(run, **kwargs)
                finally:
                    monkeypatch.setattr(env.wizard, "_edge_quad", original_quad)
        return original_check(run, **kwargs)

    def park(run, **kwargs):
        if state["injected"] and not state["parked"]:
            assert run["edge"]["corners"] == state["corners"]
            assert env.daemon.tap_count == state["tap_count"] + 1
            state["parked"] = True
        return original_park(run, **kwargs)

    monkeypatch.setattr(runner, "_auto_require_stable_phone", check)
    monkeypatch.setattr(runner, "_auto_safe_park", park)
    try:
        started = env.wizard.auto_start(defer_input_acceptance=True)
        view = wait_auto(env, lambda v: v["state"] in {"PROMOTED", "FAILED", "ABORT"})
        assert state["injected"] and state["parked"]
        assert view["state"] == ("PROMOTED" if recovers else "FAILED")
        run = cb.load_candidate("arm-01", started["runId"], tmp_path)
        assert run["edge"]["corners"] == state["corners"]
        events = [e for e in run["events"] if e["event"] == "pose_rechecked_from_park"]
        assert events[0]["detail"]["verified"] is recovers
        if recovers:
            recovered = next(s for s in run["samples"] if s["phase"] == "PHASE1_ARM")
            assert recovered.get("movedAfterTouch") is True
            assert recovered.get("tapCameraPixel") is None
        if not recovers:
            assert view["lastFailure"]["code"] == "camera_measurement_error"
            assert not any(s.get("eligible") for s in run["samples"]
                           if s["phase"] == "PHASE1_ARM")
    finally:
        env.wizard.auto_stop()
        thread = runner._auto.get("thread")
        if thread:
            thread.join(timeout=2)
