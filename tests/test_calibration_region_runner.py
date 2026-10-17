"""Region calibration follows the actual armed rectangle through qualification."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.screen_ime import geometry
from drivers import calibration_bundle as cb, calibration_wizard as cw
from drivers.calibration_auto_runner import AutoRunner, AUTO_VALIDATION_POINTS
from tests.test_calibration_model_selection import run_for
from tests.test_calibration_execution_model import failed_e_snapshot, resumable_e  # noqa: F401
from tests.test_calibration_wizard import auto_ready_env, auto_report, wait_auto
from tests.test_calibration_wizard_pose_safety import (
    automatic_run, bound_env, isolated_io,  # noqa: F401
)


def recorded_run(index=0):
    cases = json.loads((Path(__file__).parent / "fixtures" /
                        "calibration_provisional_real9.json").read_text())
    run = run_for(cases[index]["samples"])
    run.update(validationCriterion=cb.REGION_VALIDATION_CRITERION,
               screen={"screenWidth": 1080, "screenHeight": 2320},
               validation={"passed": False})
    for i, sample in enumerate(run["samples"]):
        u, v = (.1, .5, .9)[i % 3], (.1, .5, .9)[i // 3]
        sample.update(u=u, v=v, targetRect=cw.CalibrationWizard._target_rect(None, u, v))
    return run


@pytest.mark.parametrize("index", (0, 1))
def test_recorded_training_uses_each_target_margin_not_fixed_pixel_gate(index):
    run = recorded_run(index)
    AutoRunner(None)._auto_update_arm_candidate(run)
    assert run["arm"]["modelSelection"]["selectedModel"] == "affine"
    assert run["arm"]["residualRmsPx"] > 3
    assert run["arm"]["fitGate"]["trainingTargetsPassed"] is False
    assert run["arm"]["fitGate"]["requiresIndependentValidation"] is True
    assert run["locks"]["armAffine"]["state"] == cb.MODEL_LOCK_CANDIDATE
    assert run["validation"]["passed"] is False


@pytest.mark.parametrize("fault", ("missing", "short", "nan", "inf", "count", "summary", "small_target"))
def test_region_fit_rejects_incomplete_or_outside_leave_one_out(monkeypatch, fault):
    run = recorded_run()
    pairs = [{"robotX": s["effectiveRobotXY"][0], "robotY": s["effectiveRobotXY"][1],
              "screenX": s["actualTouch"][0], "screenY": s["actualTouch"][1]}
             for s in run["samples"]]
    fit = geometry.fit_calibration_model(pairs)
    loo = fit["modelSelection"]["affine"]["leaveOneOut"]
    if fault == "missing":
        del fit["modelSelection"]["affine"]["leaveOneOut"]
    elif fault == "short":
        loo["residualsPx"].pop()
    elif fault in ("nan", "inf"):
        loo["residualsPx"][0] = float(fault)
    elif fault == "count":
        fit["modelSelection"]["sampleCount"] = 8
    elif fault == "summary":
        loo["maxErrorPx"] = 0
    else:
        run["samples"][0]["targetRect"].update(widthU=.01, heightV=.01)
    monkeypatch.setattr(geometry, "fit_calibration_model", lambda _: fit)
    with pytest.raises(cw.AutoRunError, match="区域候选"):
        AutoRunner(None)._auto_update_arm_candidate(run)
    assert run["arm"] is None


@pytest.mark.parametrize("width,expected", ((.12, "PASS"), (.04, "SOFT_FAIL")))
def test_actual_click_scores_thirty_pixels_by_armed_target(bound_env, monkeypatch, width, expected):
    env = bound_env
    run = automatic_run(env)
    run["validationCriterion"] = cb.REGION_VALIDATION_CRITERION
    run["h"] = {"homography": [[1080 / 960, 0, 0], [0, 2400 / 540, 0], [0, 0, 1]]}
    rectangle = {"u": .5, "v": .5, "widthU": width, "heightV": .12}
    monkeypatch.setattr(env.wizard, "_target_rect", lambda *_: deepcopy(rectangle))
    auto_report(env.ime, env.daemon, noise=(30, 0))
    sample = env.wizard._auto_runner._auto_click_point(
        run, "region-center", .5, .5, "PHASE2_VALIDATE", use_camera=True)
    assert sample["errorLevel"] == expected
    assert sample["touchTargetErrorPx"] == pytest.approx(30, abs=.02)
    assert sample["targetRect"] == rectangle
    assert sample["regionEvidence"]["targetRect"] == rectangle
    assert sample["regionEvidence"]["passed"] is (expected == "PASS")
    assert env.daemon.tap_count == 1


def region_point(target_id, u, v, delta=30, width=None):
    screen = {"screenWidth": 1080, "screenHeight": 2320}
    rect = cw.CalibrationWizard._target_rect(None, u, v)
    if width is not None:
        rect["widthU"] = width
    center = [u * 1080, v * 2320]
    evidence = cb.calibration_region_evidence(rect, screen, [center[0] + delta, center[1]], center)
    return {"targetId": target_id, "u": u, "v": v, "passed": True,
            "eligible": True, "errorLevel": "PASS", "classification": "ok",
            "regionEvidence": evidence, "touchTargetErrorPx": evidence["touchTargetErrorPx"],
            "cameraTouchGapPx": evidence["cameraTouchGapPx"], "cameraProjectionErrorPx": 0}


LEGACY_VALIDATION_POINTS = [
    ("validation-1", .05, .05), ("validation-2", .95, .05),
    ("validation-3", .05, .95), ("validation-4", .95, .95),
    ("validation-5", .50, .65),
]


def validation_run(*, legacy_plan=False):
    run = recorded_run()
    plan = LEGACY_VALIDATION_POINTS if legacy_plan else AUTO_VALIDATION_POINTS
    run.update(runId="region-acceptance", deviceId="arm-01", binding={},
               pointPlan=[{"id": "center", "u": .5, "v": .5}],
               validationPlan=[{"id": n, "u": u, "v": v} for n, u, v in plan],
               phase2={"center": region_point("center", .5, .5)},
               locks={name: {"state": cb.MODEL_LOCK_LOCKED}
                      for name in ("armAffine", "cameraToScreen")})
    return run


@pytest.mark.parametrize("legacy_plan", (False, True), ids=("ordinary-region", "frozen-old-corners"))
def test_phase2_and_independent_keep_full_region_evidence(tmp_path, monkeypatch, legacy_plan):
    run = validation_run(legacy_plan=legacy_plan)
    frozen_plan = deepcopy(run["validationPlan"])
    run["phase2"] = {}
    runner = AutoRunner(SimpleNamespace(data_root=tmp_path))
    monkeypatch.setattr(runner, "_auto_click_point", lambda _run, name, u, v, *_args, **_kwargs:
                        region_point(name, u, v, delta=20))
    assert runner._auto_phase2_validate(run) == []
    assert runner._auto_check_convergence(run) is True
    runner._auto_validate_independent(run)
    assert runner._auto_region_validation_complete(run) is True
    assert run["validationPlan"] == frozen_plan
    assert [(p["targetId"], p["u"], p["v"]) for p in run["validationPoints"]] == [
        (p["id"], p["u"], p["v"]) for p in frozen_plan]
    assert all(p["regionEvidence"]["targetRect"]["widthU"] == pytest.approx(.06 if legacy_plan else .12)
               for p in run["validationPoints"][:4])
    assert all(p["touchTargetErrorPx"] > 10 for p in run["validationPoints"])


@pytest.mark.parametrize("fault", ("missing", "outside", "aim_outside", "wrong_center"))
def test_region_convergence_never_trusts_pass_label(fault):
    run = validation_run()
    point = run["phase2"]["center"]
    if fault == "missing":
        del point["regionEvidence"]
    elif fault == "wrong_center":
        point["u"] = .6
    else:
        point["regionEvidence"]["actualTouch" if fault == "outside" else "cameraPredictedScreen"] = [999, 999]
    assert AutoRunner(None)._auto_check_convergence(run) is False


def test_outside_independent_point_cannot_promote_even_with_pass_label(tmp_path, monkeypatch):
    run = validation_run(legacy_plan=True)
    # A persisted old candidate keeps its small corner targets after the default changes.
    cb.save_candidate(run, tmp_path)
    run = cb.load_candidate("arm-01", run["runId"], tmp_path)
    runner = AutoRunner(SimpleNamespace(data_root=tmp_path, _read_binding_facts=lambda: {}))
    monkeypatch.setattr(runner, "_auto_click_point", lambda _run, name, u, v, *_a, **_k:
                        region_point(name, u, v, delta=30))
    # Near-corner safe half width is 29.16 px, so 30 px is outside despite "PASS".
    with pytest.raises(cw.AutoRunError) as exc:
        runner._auto_validate_independent(run)
    assert exc.value.code == "independent_validation_failed"
    run["validation"]["passed"] = True  # An aggregate flag is insufficient.
    with pytest.raises(cw.AutoRunError) as exc:
        runner._auto_promote_candidate(run)
    assert exc.value.code == "promote_validation_missing"
    assert cb.load_bundle("arm-01", tmp_path) is None


def test_new_region_evidence_cannot_replace_frozen_old_validation_plan(tmp_path, monkeypatch):
    run = validation_run(legacy_plan=True)
    runner = AutoRunner(SimpleNamespace(data_root=tmp_path))
    monkeypatch.setattr(runner, "_auto_click_point", lambda _run, name, u, v, *_a, **_k:
                        region_point(name, u, v, delta=20))
    runner._auto_validate_independent(run)
    assert runner._auto_region_validation_complete(run) is True
    # Results with the same IDs at other coordinates cannot certify the old plan.
    new_points = [region_point(name, u, v, delta=20) for name, u, v in [
        ("validation-1", .20, .10), ("validation-2", .80, .10),
        ("validation-3", .20, .90), ("validation-4", .80, .90),
        ("validation-5", .50, .65),
    ]]
    run["validationPoints"] = new_points
    run["validation"]["points"] = deepcopy(new_points)
    assert runner._auto_region_validation_complete(run) is False


def test_new_run_freezes_region_criterion_and_promotes_full_evidence(tmp_path):
    env = auto_ready_env(tmp_path)
    try:
        view = env.wizard.auto_start(defer_input_acceptance=True)
        assert view["validationCriterion"] == cb.REGION_VALIDATION_CRITERION
        done = wait_auto(env, lambda value: value["state"] not in cw.AUTO_ACTIVE_STATES)
        assert done["state"] == "PROMOTED", done
        candidate = cb.load_candidate("arm-01", done["runId"], tmp_path)
        assert [(p["id"], p["u"], p["v"]) for p in candidate["validationPlan"]] == [
            ("validation-1", .20, .10), ("validation-2", .80, .10),
            ("validation-3", .20, .90), ("validation-4", .80, .90),
            ("validation-5", .50, .65),
        ]
        saved = cb.load_bundle("arm-01", tmp_path)
        assert saved["validation"]["criterion"] == cb.REGION_VALIDATION_CRITERION
        assert "tolerancePx" not in saved["validation"]
        assert saved["xy"]["modelSelection"]["sampleCount"] == 9
        assert all(sample["targetRect"] for sample in saved["xy"]["samples"])
        assert cb.automatic_calibration_verified(saved) is True
    finally:
        env.wizard.auto_stop()


@pytest.mark.parametrize("criterion", (None, "", "unknown-region"))
def test_unknown_criterion_rejects_before_contact(bound_env, criterion):
    env = bound_env
    run = automatic_run(env)
    run["validationCriterion"] = criterion
    with pytest.raises(cw.AutoRunError, match="验收标准无效"):
        env.wizard._auto_runner._auto_click_point(run, "center", .5, .5, "PHASE1_ARM", use_camera=False)
    assert env.daemon.cmds == []


@pytest.mark.parametrize("fault", ("missing", "outside", "phase2_missing"))
def test_region_resume_rejects_missing_or_outside_evidence(resumable_e, fault):
    env, run = resumable_e
    point = run["validationPoints"][0]
    if fault == "missing":
        del point["regionEvidence"]
    elif fault == "outside":
        point["regionEvidence"]["actualTouch"] = [999, 999]
    else:
        run["phase2"].clear()
    run["validation"]["points"] = deepcopy(run["validationPoints"])
    commands = list(env.daemon.cmds)
    with pytest.raises(cw.WizardError, match="区域复验或独立验证"):
        env.wizard._auto_runner._auto_acceptance_resume_guard(run)
    assert env.daemon.cmds == commands
