"""默认九点校准覆盖边缘，旧十六点规则计划仍可独立完成校准。"""
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb
from drivers.calibration_wizard import AutoRunError, WizardError
from tests.test_calibration_wizard import auto_ready_env, wait_auto


def grid_plan(axis):
    return [{"id": f"custom-{row * len(axis) + col + 1}", "u": u, "v": v}
            for row, v in enumerate(axis) for col, u in enumerate(axis)]


def test_default_plan_has_nine_training_and_five_independent_screen_region_points(tmp_path):
    env = auto_ready_env(tmp_path)
    plan = env.wizard.state_view()["autoPlan"]

    assert len(plan["points"]) == 9
    assert {(p["u"], p["v"]) for p in plan["points"]} == {
        (u, v) for v in (0.10, 0.50, 0.90) for u in (0.10, 0.50, 0.90)
    }
    assert {(p["u"], p["v"]) for p in plan["validation"]}.isdisjoint(
        {(p["u"], p["v"]) for p in plan["points"]})
    assert {(p["u"], p["v"]) for p in plan["validation"]} == {
        (0.20, 0.10), (0.80, 0.10), (0.20, 0.90), (0.80, 0.90), (0.50, 0.65),
    }


@pytest.mark.parametrize("axis", [(0.10, 0.50, 0.90), (0.15, 0.38, 0.62, 0.85)])
def test_custom_grid_completes_using_its_own_shape(tmp_path, axis):
    env = auto_ready_env(tmp_path)
    plan = grid_plan(axis)
    env.wizard.auto_start(point_plan=plan)
    view = wait_auto(env, lambda v: v["state"] in {"PROMOTED", "FAILED", "ABORT"})

    assert view["state"] == "PROMOTED", view.get("lastFailure")
    run = cb.load_candidate("arm-01", view["runId"], tmp_path)
    assert run["pointPlan"] == plan
    assert run["h"]["quality"]["pointCount"] == len(plan)
    assert len(run["phase2"]) == len(plan)
    assert len(run["validationPoints"]) == 5
    assert all(point["passed"] for point in run["validationPoints"])


@pytest.mark.parametrize("invalid", ["wrong-count", "duplicate-id", "wrong-order", "not-grid"])
def test_invalid_custom_plan_rejected_before_motion(tmp_path, invalid):
    env = auto_ready_env(tmp_path)
    plan = grid_plan((0.10, 0.50, 0.90))
    if invalid == "wrong-count":
        plan.pop()
    elif invalid == "duplicate-id":
        plan[-1]["id"] = plan[0]["id"]
    elif invalid == "wrong-order":
        plan[0], plan[1] = plan[1], plan[0]
    else:
        plan[-1]["u"] = 0.80
    taps = env.daemon.tap_count

    with pytest.raises(WizardError, match="标定点"):
        env.wizard.auto_start(point_plan=plan)

    assert env.daemon.tap_count == taps
    assert env.wizard.auto_view()["activeRunId"] is None


@pytest.mark.parametrize("best_effort", [False, True])
def test_parking_rechecks_session_after_lease_wait(tmp_path, monkeypatch, best_effort):
    env = auto_ready_env(tmp_path)
    state = env.wizard.state
    run = {
        "daemonSession": env.daemon.session, "phoneSessionId": state["imeSessionId"],
        "binding": dict(env.wizard._read_binding_facts()),
        "safePark": deepcopy(state["safePark"]), "z": deepcopy(state["z"]["autoSafe"]),
        "events": [],
    }
    env.daemon.x = 20.0
    env.daemon.cmds.clear()

    def reconnect_during_lease():
        env.daemon.session = "new-coordinate-session"

    monkeypatch.setattr(env.daemon, "ensure_debug_lease", reconnect_during_lease)
    if best_effort:
        env.wizard._auto_runner._auto_safe_park(run, best_effort=True)
    else:
        with pytest.raises(AutoRunError):
            env.wizard._auto_runner._auto_safe_park(run)

    assert not any(command.startswith("XY ") for command in env.daemon.cmds)
    assert run["lastPark"] is None


def test_post_touch_parking_does_not_record_parking_tip_as_contact(tmp_path, monkeypatch):
    env = auto_ready_env(tmp_path)
    original_report = env.wizard._tap_with_report
    tip_reads = []

    def report_after_parking(*args, **kwargs):
        return {**original_report(*args, **kwargs), "movedAfterTouch": True}

    def parking_tip():
        tip_reads.append(True)
        return 100.0, 200.0

    monkeypatch.setattr(env.wizard, "_tap_with_report", report_after_parking)
    monkeypatch.setattr(env.wizard, "_detect_tip", parking_tip)
    env.wizard.auto_start()
    view = wait_auto(env, lambda v: v["state"] in {"PROMOTED", "FAILED", "ABORT"})

    assert view["state"] == "PROMOTED", view.get("lastFailure")
    run = cb.load_candidate("arm-01", view["runId"], tmp_path)
    assert run["samples"] and all(sample["movedAfterTouch"] for sample in run["samples"])
    assert all(sample["tapCameraPixel"] is None and sample["cameraTapErrorPx"] is None
               for sample in run["samples"])
    assert not tip_reads
    assert all(sample["touchTargetErrorPx"] <= run["config"]["target_tolerance_px"]
               for sample in run["samples"])
