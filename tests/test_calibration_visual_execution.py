"""自动验收必须执行摄像头看到的位置，触点回报只用于事后评分。"""
import pytest

from drivers import calibration_bundle as cb
from tests.test_calibration_wizard import CAM_W, SCREEN_W, auto_ready_env, wait_auto


@pytest.mark.parametrize("screen_shift,expected_state", [(6.0, "PROMOTED"), (16.0, "FAILED")])
def test_independent_validation_executes_camera_projection(tmp_path, screen_shift, expected_state):
    env = auto_ready_env(tmp_path)
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
        if expected_state == "FAILED":
            assert view["lastFailure"]["code"] == "independent_validation_failed"
            assert run["validationPoints"][0]["passed"] is False
    finally:
        env.wizard.auto_stop()
        thread = runner._auto.get("thread")
        if thread:
            thread.join(timeout=2)
