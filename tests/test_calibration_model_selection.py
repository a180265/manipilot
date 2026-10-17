"""Calibration chooses complexity using only held-out training samples."""
import json
from pathlib import Path

import pytest

from adapters.screen_ime import geometry
from drivers.calibration_auto_runner import AUTO_CONFIG_DEFAULTS, AutoRunError, AutoRunner


def noisy_grid(noise=(0, 1, -1, 0, 0, 1, -1, 0, 0)):
    return [
        {"robotX": x, "robotY": y,
         "screenX": 10 * x + 100 + noise[index], "screenY": 10 * y + 100}
        for index, (x, y) in enumerate(
            (x, y) for y in (-1, 0, 1) for x in (-1, 0, 1))
    ]


def run_for(pairs):
    return {
        "config": dict(AUTO_CONFIG_DEFAULTS), "iteration": 1,
        "arm": None, "armHistory": [], "locks": {"armAffine": {}},
        "samples": [
            {"phase": "PHASE1_ARM", "eligible": True, "targetId": str(index),
             "effectiveRobotXY": [p["robotX"], p["robotY"]],
             "actualTouch": [p["screenX"], p["screenY"]]}
            for index, p in enumerate(pairs)
        ],
    }


def test_auto_calibration_does_not_choose_quadratic_just_for_lower_training_error():
    pairs = noisy_grid()
    assert geometry.fit_quadratic(pairs)["residualRms"] < geometry.fit_affine(pairs)["residualRms"]
    run = run_for(pairs)

    AutoRunner(None)._auto_update_arm_candidate(run)

    assert "qx2" not in run["arm"]["robotToScreen"]
    selection = run["arm"]["modelSelection"]
    assert selection["selectedModel"] == "affine"
    assert selection["quadratic"]["leaveOneOut"]["maxErrorPx"] > selection["affine"]["leaveOneOut"]["maxErrorPx"]


def test_equal_worst_leave_one_out_error_uses_rms_to_keep_affine():
    pairs = noisy_grid((0, 0, 0, 0, 3, 0, 0, 0, 0))
    selected = geometry.fit_calibration_model(pairs)["modelSelection"]
    affine = selected["affine"]["leaveOneOut"]
    quadratic = selected["quadratic"]["leaveOneOut"]
    assert affine["maxErrorPx"] == pytest.approx(3)
    assert quadratic["maxErrorPx"] == pytest.approx(3)
    assert quadratic["rmsPx"] > affine["rmsPx"]
    assert selected["selectedModel"] == "affine"


@pytest.mark.parametrize("count", [3, 5, 8])
def test_fewer_than_nine_points_keeps_affine(count):
    pairs = [noisy_grid()[i] for i in (0, 2, 8, 6, 4, 1, 3, 5)][:count]
    selected = geometry.fit_calibration_model(pairs)
    assert "qx2" not in selected
    assert selected["modelSelection"]["reason"] == "insufficient_samples_for_quadratic_validation"


def test_exact_affine_does_not_add_unnecessary_quadratic_terms():
    selected = geometry.fit_calibration_model(noisy_grid((0,) * 9))
    assert selected["modelSelection"]["selectedModel"] == "affine"
    assert "qx2" not in selected


@pytest.mark.parametrize("points,reason", [
    ([(i, 0) for i in range(8)] + [(0, 1)], "affine_leave_one_out_degenerate"),
    ([(i, j) for j in (0, 1) for i in range(5)][:9], "quadratic_validation_degenerate"),
    ([(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (0, 2)] + [(0, 0)] * 3,
     "quadratic_validation_degenerate"),
])
def test_degenerate_full_or_held_out_quadratic_fit_keeps_valid_affine(points, reason):
    pairs = [{"robotX": x, "robotY": y, "screenX": 10 * x, "screenY": 10 * y}
             for x, y in points]
    selected = geometry.fit_calibration_model(pairs)
    assert "qx2" not in selected
    assert selected["modelSelection"]["reason"] == reason
    assert selected["residualRms"] < 1e-6


@pytest.mark.parametrize("pairs", [
    [{"robotX": i, "robotY": i, "screenX": i, "screenY": i} for i in range(9)],
    [{"robotX": 0, "robotY": 0, "screenX": 0, "screenY": 0}],
])
def test_unusable_affine_baseline_still_rejects_calibration(pairs):
    with pytest.raises(geometry.CalibrationMathError):
        geometry.fit_calibration_model(pairs)


@pytest.mark.parametrize("limit", ["arm_rms_target_px", "arm_max_error_px"])
def test_candidate_selection_excludes_independent_acceptance_and_preserves_quality_gate(limit):
    run = run_for(noisy_grid())
    run["samples"].append({
        "phase": "INDEPENDENT_VALIDATE", "eligible": True, "targetId": "holdout",
        "effectiveRobotXY": [0.3, 0.7], "actualTouch": [9000, 9000],
    })
    run["config"][limit] = 0.1
    with pytest.raises(AutoRunError) as error:
        AutoRunner(None)._auto_update_arm_candidate(run)
    assert error.value.code == "arm_quality_below_target"
    assert run["arm"] is None
    assert run["armFitSelection"]["sampleCount"] == 9
    assert run["armFitSelection"]["selectedModel"] == "affine"


@pytest.mark.parametrize("case_index,expected", [(0, "affine"), (1, "quadratic")])
def test_recorded_nine_point_training_replay(case_index, expected):
    cases = json.loads((Path(__file__).parent / "fixtures" /
                        "calibration_model_selection_real9.json").read_text())
    selected = geometry.fit_calibration_model(cases[case_index]["samples"])
    diagnostics = selected["modelSelection"]
    assert diagnostics["sampleCount"] == 9
    assert diagnostics["selectedModel"] == expected
    assert (diagnostics["quadratic"]["training"]["rmsPx"]
            < diagnostics["affine"]["training"]["rmsPx"])
    if expected == "affine":
        assert diagnostics["quadratic"]["leaveOneOut"]["maxErrorPx"] > 8
    else:
        assert diagnostics["quadratic"]["leaveOneOut"]["maxErrorPx"] < 2.1
