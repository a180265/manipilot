"""自动验收必须执行摄像头看到的位置，触点回报只用于事后评分。"""
import pytest

from drivers import calibration_bundle as cb
from tests.test_calibration_wizard import (
    CAM_W, SCREEN_W, auto_ready_env, wait_auto, legacy_auto_calibration,  # noqa: F401
)


@pytest.mark.parametrize("region_mode,screen_shift,expected_state", [
    (False, 6.0, "PROMOTED"), (False, 16.0, "FAILED"),
    (True, 6.0, "PROMOTED"), (True, 16.0, "PROMOTED"),
], ids=("legacy-small", "legacy-over-limit", "region-small", "region-inside-target"))
def test_independent_validation_executes_camera_projection(
        tmp_path, legacy_auto_calibration, region_mode, screen_shift, expected_state):
    env = auto_ready_env(tmp_path)
    if not region_mode:
        legacy_auto_calibration(env)
    runner = env.wizard._auto_runner

    def candidates():
        payload = env.real_candidates()
        run = runner._auto.get("run") or {}
        if run.get("phase") == "INDEPENDENT_VALIDATE":
            payload["items"] = [dict(item, x=item["x"] + screen_shift * CAM_W / SCREEN_W)
                                for item in payload["items"]]
        return payload

    env.candidates_hook = candidates
    try:
        started = env.wizard.auto_start(defer_input_acceptance=True)
        view = wait_auto(env, lambda v: v["state"] in {"PROMOTED", "FAILED", "ABORT"},
                         desc="视觉独立验收终态")
        assert view["state"] == expected_state
        run = cb.load_candidate("arm-01", started["runId"], tmp_path)
        sample = next(s for s in run["samples"] if s["phase"] == "INDEPENDENT_VALIDATE")
        assert sample["actualTouch"][0] - sample["targetScreen"][0] == pytest.approx(screen_shift, abs=.1)
        assert sample["cameraTouchGapPx"] < .1
        if region_mode:
            assert run["validationCriterion"] == cb.REGION_VALIDATION_CRITERION
            assert all(cb.calibration_region_point_passed(p, run["screen"])
                       for p in run["validationPoints"])
        else:
            assert "validationCriterion" not in run
        if expected_state == "FAILED":
            assert view["lastFailure"]["code"] == "independent_validation_failed"
            assert run["validationPoints"][0]["passed"] is False
    finally:
        env.wizard.auto_stop()
        thread = runner._auto.get("thread")
        if thread:
            thread.join(timeout=2)
