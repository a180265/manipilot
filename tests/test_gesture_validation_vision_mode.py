"""视觉优先模式单元测试：无页面会话，臂滑+相机光流判定。"""
import asyncio
import json
import sys
from pathlib import Path as _Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime.gesture_validation import GestureValidationRunner


class FakeCoordinator:
    """记录调用的假协调器——vision_only 下不应有任何页面调用。"""

    def __init__(self):
        self.calls = []

    async def create_session(self, **kwargs):
        self.calls.append(("create_session", kwargs))
        return {"sessionId": "s-1"}

    async def prepare_trial(self, *a, **k):
        self.calls.append(("prepare_trial", a, k))

    async def wait_ready(self, *a, **k):
        self.calls.append(("wait_ready", a, k))
        return {"ready": True, "readyPayload": {"fixtureRegion": {"x": 0, "y": 0, "w": 100, "h": 100}}}

    async def mark_executing(self, *a, **k):
        self.calls.append(("mark_executing", a, k))

    async def wait_evidence(self, *a, **k):
        self.calls.append(("wait_evidence", a, k))
        return {"trace": {"events": [
            {"xPx": 100, "yPx": 100, "tMs": 0}, {"xPx": 100, "yPx": 200, "tMs": 100}]}}

    async def finish_trial(self, *a, **k):
        self.calls.append(("finish_trial", a, k))


DEFAULTS = None  # 惰性从 configs/gesture_defaults.json 加载（见 make_runner）


def make_runner(vision_only, actions, frames, strokes, run_id, data_root):
    global DEFAULTS
    if DEFAULTS is None:
        cfg = json.loads((_Path(__file__).parents[1] / "configs" /
                           "gesture_defaults.json").read_text())
        DEFAULTS = {**cfg, "command_timing": cfg["command_timing"]}
    trials = {"i": 0}

    async def execute_swipe(plan):
        trials["i"] += 1
        strokes.append(json.loads(json.dumps((plan or {}).get("program") or {})))
        return {"controller_status": "completed", "motion_verified": True,
                "ok": True, "position": {"x": 0, "y": 0, "z": 0, "state": "Idle"}}

    idx = {"i": 0}

    async def capture_frame():
        i = idx["i"]
        idx["i"] += 1
        return frames[min(i, len(frames) - 1)]

    async def measure_scroll(before, after, region):
        return {"status": "displaced", "dy_px": -40.0, "dx_px": 1.0,
                "inlier_ratio": 0.9, "valid_points": 50}

    return GestureValidationRunner(
        defaults=DEFAULTS, bundle=BUNDLE, coordinate_frame=FRAME,
        device_id="arm-01", ime_device_id="k40-1",
        coordinator=FakeCoordinator(), execute_swipe=execute_swipe,
        execute_double_tap=execute_swipe, capture_frame=capture_frame,
        measure_scroll=measure_scroll, vision_only=vision_only,
        data_root=data_root, run_id=run_id)


from tests.test_gesture_validation_run import _frame as build_frame
from tests.test_screen_gesture_driver import gesture_bundle as real_bundle

BUNDLE = real_bundle()
FRAME = build_frame(BUNDLE)


def test_vision_only_skips_page_and_verifies_by_camera(tmp_path):
    async def run():
        frames = [{"evidence_id": f"evd-{i}", "sha256": f"h{i}"} for i in range(40)]
        strokes = []
        runner = make_runner(True, ["scroll_up"], frames, strokes,
                             run_id="run-vision-case1", data_root=tmp_path)
        result = await runner.run(["scroll_up"])
        assert result["state"] == "passed", (result["state"], result.get("stop_reason"),
                                             result.get("action_results"))
        actions = result["action_results"]
        assert actions[0]["state"] == "valid"
        assert actions[0]["reason_code"] is None  # 完成动作无失败原因
        assert actions[0]["validation_status"] == "three_passes"
        # 三连过全部来自相机视觉判定（无软件回报）；取证链必须真实落盘
        observations = runner._run.get("scroll_observations") or []
        assert len(observations) == 3, observations
        for obs in observations:
            assert obs["camera_observation"]["status"] == "displaced"
            assert obs["before_evidence_id"] and obs["after_evidence_id"]
            assert obs["before_evidence_id"] != obs["after_evidence_id"]
            assert obs["classification"] == "trial_passed"
        # 页面协调器零调用；物理预算=复位滑+被测滑都计数（3 trial=6 次）
        assert runner.coordinator.calls == []
        assert runner._run["budget"]["physical_count"] == 6
        return len(strokes)
    n = asyncio.run(run())
    print(f"vision_only: {n} 个程序计划执行（复位+被测交替）")


def test_vision_only_rejects_mixed_action_sets(tmp_path):
    """纵深防线：runner 不信任调用方——vision 模式只接受纯滑动集。"""
    async def run():
        frames = [{"evidence_id": f"evd-{i}", "sha256": f"h{i}"} for i in range(40)]
        runner = make_runner(True, ["scroll_up", "double_tap"], frames, [],
                             run_id="run-vision-case3", data_root=tmp_path)
        result = await runner.run(["scroll_up", "double_tap"])
        assert result["state"] == "failed", result["state"]
        assert result["stop_reason"] == "vision_requires_scroll_only"
        assert result["action_results"] == []
        assert runner.coordinator.calls == []
    asyncio.run(run())


def test_page_mode_still_uses_coordinator(tmp_path):
    async def run():
        frames = [{"evidence_id": f"evd-{i}", "sha256": f"h{i}"} for i in range(40)]
        strokes = []
        runner = make_runner(False, ["scroll_up"], frames, strokes,
                             run_id="run-vision-case2", data_root=tmp_path)
        result = await runner.run(["scroll_up"])
        assert runner.coordinator.calls, "页面模式必须有协调器调用"
        assert result["state"] in ("passed", "failed", "partial", "unknown")
        return len(strokes)
    asyncio.run(run())
