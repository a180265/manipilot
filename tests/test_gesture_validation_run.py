"""D5：自检状态机、判据与有限调参（规范第 8、22.10 节）。

Fake 注入协调器/执行器/取帧；断言候选顺序、连续 3 次、预算、
复位竞态、unknown 不续跑、决策表、run 持久化与最终结构（22.16）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from runtime import gesture_validation as gv
from tests.test_screen_gesture_driver import gesture_bundle

ROOT_W, ROOT_H = 1080, 2320
REGION = {"leftPx": 0, "topPx": 250, "widthPx": 1080, "heightPx": 1900}
FIXTURE_VERSION = "fixture-list-v1"


def _frame(bundle):
    from drivers import gesture_program as gp
    from drivers import screen_gestures as sg
    return gp.build_coordinate_frame(
        binding=sg.binding_snapshot(bundle), calibration_run_id="run-test",
        root_width_px=ROOT_W, root_height_px=ROOT_H,
        display_width_px=1080, display_height_px=2400,
        root_origin_display_px=[0, 80], density=3.0,
        source_client_instance_id="0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001")


@pytest.fixture()
def bundle():
    return gesture_bundle()


@pytest.fixture()
def defaults():
    config = json.loads((Path(__file__).parents[1] / "configs"
                         / "gesture_defaults.json").read_text(encoding="utf-8"))
    return config


class FakeCoordinator:
    """可编程协调器替身：自动 ready、可注入轨迹/页面事件。"""

    def __init__(self, trace_factory=None, page_events=None):
        self.trace_factory = trace_factory
        self.page_events = page_events or []
        self.prepared: list[dict] = []
        self.executing: list[str] = []
        self.finished_trials: list[tuple[str, str]] = []
        self.session_id = "sess-fake"
        self.fail_prepare = None
        self.ready_payload = {
            "fixtureVersion": FIXTURE_VERSION, "fixtureRegion": REGION,
            "scrollY": 950, "contentHeightPx": 6400, "viewportHeightPx": 1900,
            "resetId": "9e2faf5b-4c7d-4a81-8f34-2f2f2e08c005",
        }

    async def create_session(self, **kwargs):
        return {"sessionId": self.session_id, "status": "active"}

    async def prepare_trial(self, session_id, trial_id, spec):
        if self.fail_prepare:
            raise gv.ScreenImeApiError(409, self.fail_prepare, "注入失败")
        self.prepared.append({"trialId": trial_id, **spec})
        return {"trialId": trial_id, "state": "prepared"}

    async def wait_ready(self, session_id, trial_id, timeout_ms):
        return {"ready": True, "readyPayload": dict(self.ready_payload)}

    async def mark_executing(self, session_id, trial_id):
        self.executing.append(trial_id)

    async def wait_evidence(self, session_id, trial_id, timeout_ms):
        if self.trace_factory is None:
            return {"ready": False, "trace": None, "pageEvents": []}
        return {"ready": True, "trace": self.trace_factory(trial_id),
                "pageEvents": list(self.page_events)}

    async def finish_trial(self, session_id, trial_id, state):
        self.finished_trials.append((trial_id, state))
        return {"trialId": trial_id, "state": state}

    async def finish_session(self, session_id, outcome):
        return {"sessionId": session_id, "outcome": outcome}


def make_trace(events, end_reason="up", scroll=None, dropped=0,
               recognition=None):
    return {"events": events, "endReason": end_reason,
            "droppedSampleCount": dropped,
            "scroll": scroll,
            "recognition": recognition,
            "coordinateSpace": "calibration_root_px.v1",
            "clockDomain": "android_uptime_ms"}


def scroll_evidence(before_y, after_y, *, row_h=192, content=6400,
                    viewport=1900, reset=1):
    def row(y):
        return int(y // row_h), int(y % row_h)
    row_b, off_b = row(before_y)
    row_a, off_a = row(after_y)
    return {
        "beforeY": before_y, "afterY": after_y, "minY": 0,
        "maxY": content - viewport, "contentHeightPx": content,
        "viewportHeightPx": viewport, "rowHeightPx": row_h,
        "firstVisibleRowBefore": row_b, "firstVisibleRowAfter": row_a,
        "rowOffsetBeforePx": off_b, "rowOffsetAfterPx": off_a,
        "settled": True, "settleWindowMs": 300,
        "resetCounterBefore": reset, "resetCounterAfter": reset,
        "boundaryBefore": "top" if before_y <= 1 else
        ("bottom" if before_y >= content - viewport - 1 else "neither"),
        "boundaryAfter": "top" if after_y <= 1 else
        ("bottom" if after_y >= content - viewport - 1 else "neither"),
    }


def good_trace_for(candidate, *, noise_px=1.0):
    """沿候选路径生成合格轨迹 + 正确位移的滚动证据（scroll_up：内容上移）。"""
    x = REGION["leftPx"] + candidate["x"] * REGION["widthPx"]
    y0 = REGION["topPx"] + candidate["y_start"] * REGION["heightPx"]
    y1 = REGION["topPx"] + candidate["y_end"] * REGION["heightPx"]
    events = [
        {"index": 0, "contactIndex": 0, "action": "down", "pointerId": 0,
         "tMs": 0, "xPx": x + noise_px, "yPx": y0 - noise_px, "historical": False},
        {"index": 1, "contactIndex": 0, "action": "move", "pointerId": 0,
         "tMs": 200, "xPx": x, "yPx": (y0 + y1) / 2, "historical": False},
        {"index": 2, "contactIndex": 0, "action": "up", "pointerId": 0,
         "tMs": 400, "xPx": x - noise_px, "yPx": y1 + noise_px, "historical": False},
    ]
    # 上滑：scrollY 增加（Android 原生语义——内容上移=看到更下方，真机定案）
    scroll = scroll_evidence(480, 1200)
    return make_trace(events, scroll=scroll)


def displaced_camera():
    return {"status": "displaced", "dy_px": -320.0}


def scroll_camera_io(camera_result=None):
    sequence = iter(range(1, 100))

    def capture():
        frame_seq = next(sequence)
        return {"frame_seq": frame_seq, "evidence_id": f"camera-{frame_seq}"}

    def measure(before, after, region):
        assert before["evidence_id"] != after["evidence_id"]
        assert region == REGION
        return displaced_camera() if camera_result is None else camera_result

    return {"capture_frame": capture, "measure_scroll": measure}


# ---------- 判据单测（§8.3） ----------

class TestEvaluateScrollTrial:
    def _plan(self, bundle, defaults):
        candidate = defaults["scroll_self_test"]["candidates"][0]
        program, _meta = gv.build_scroll_program(
            "scroll_up", candidate, REGION, bundle, _frame(bundle))
        return gv.plan_scroll_trial(program, bundle, _frame(bundle))

    def test_pass(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=good_trace_for(candidate), plan=plan, bundle=bundle,
            defaults=defaults, camera_result=displaced_camera())
        assert result["classification"] == "trial_passed"

    def test_insufficient_scroll_is_no_effect(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        trace = good_trace_for(candidate)
        trace["scroll"] = scroll_evidence(1100, 1200)  # 位移 8%
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=trace, plan=plan, bundle=bundle, defaults=defaults)
        assert result["classification"] == "no_effect"
        assert result["reason_code"] == "effect_not_observed"

    def test_unknown_camera_stops_before_phone_no_effect(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        trace = good_trace_for(candidate)
        trace["scroll"] = scroll_evidence(1100, 1200)
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=trace, plan=plan, bundle=bundle, defaults=defaults,
            camera_result={"status": "unknown",
                           "reason_code": "camera_frame_budget_exhausted"})
        assert result["classification"] == "unknown"
        assert result["effect_status"] == "unknown"
        assert result["contact_status"] == "continuous"

    def test_wrong_direction(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        trace = good_trace_for(candidate)
        trace["scroll"] = scroll_evidence(1200, 480)  # 方向相反（真机语义：上滑应 scrollY 增）
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=trace, plan=plan, bundle=bundle, defaults=defaults)
        assert result["classification"] == "direction_mismatch"

    def test_reset_counter_changed_fails(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        trace = good_trace_for(candidate)
        trace["scroll"] = scroll_evidence(480, 1200, reset=1)
        trace["scroll"]["resetCounterAfter"] = 2
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=trace, plan=plan, bundle=bundle, defaults=defaults)
        assert result["classification"] == "unknown"

    def test_no_down_is_no_contact(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=make_trace([], "timeout"), plan=plan, bundle=bundle,
            defaults=defaults)
        assert result["classification"] == "no_contact"

    def test_contact_broken(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        trace = good_trace_for(candidate)
        trace["endReason"] = "cancel"
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=trace, plan=plan, bundle=bundle, defaults=defaults)
        assert result["classification"] == "contact_broken"

    def test_mapping_mismatch(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        trace = good_trace_for(candidate)
        for event in trace["events"]:
            event["xPx"] += 260  # 整体偏离
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=trace, plan=plan, bundle=bundle, defaults=defaults)
        assert result["classification"] == "mapping_mismatch"

    def test_controller_unknown_is_unknown(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "unknown"},
            trace=good_trace_for(candidate), plan=plan, bundle=bundle,
            defaults=defaults)
        assert result["classification"] == "unknown"

    def test_row_inconsistency_rejected(self, bundle, defaults):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        trace = good_trace_for(candidate)
        trace["scroll"]["firstVisibleRowAfter"] += 3  # 行号与偏移不一致
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=trace, plan=plan, bundle=bundle, defaults=defaults)
        assert result["classification"] == "unknown"

    @pytest.mark.parametrize("camera_result,classification,reason", [
        (None, "unknown", "camera_displacement_unknown"),
        ({"status": "unknown", "dy_px": -320.0}, "unknown",
         "camera_displacement_unknown"),
        ({"status": "stationary", "dy_px": 0.0}, "no_effect",
         "camera_stationary"),
    ])
    def test_camera_evidence_required(self, bundle, defaults, camera_result,
                                      classification, reason):
        plan = self._plan(bundle, defaults)
        candidate = defaults["scroll_self_test"]["candidates"][0]
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=good_trace_for(candidate), plan=plan, bundle=bundle,
            defaults=defaults, camera_result=camera_result)
        assert result["classification"] == classification
        assert result["reason_code"] == reason


# ---------- 状态机 ----------

class TestValidationRun:
    def test_single_pass_needs_three_consecutive(self, bundle, defaults, tmp_path):
        counter = {"n": 0}
        executed = []

        def execute(plan):
            counter["n"] += 1
            executed.append(plan)
            return {"controller_status": "completed", "motion_verified": True,
                    "ok": True, "position": {"z": -8.0, "state": "Idle"}}

        def trace_factory(trial_id):
            candidate = defaults["scroll_self_test"]["candidates"][0]
            return good_trace_for(candidate)

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(trace_factory),
            execute_swipe=execute, execute_double_tap=execute,
            **scroll_camera_io(),
            data_root=tmp_path, run_id="run-3pass")
        result = asyncio.run(runner.run(["scroll_up"]))
        assert counter["n"] == 3  # 同候选连续 3 次
        assert result["state"] == "passed"
        assert result["action_results"][0]["gesture_id"] == "scroll_up"
        assert result["action_results"][0]["state"] == "valid"
        assert len(result["action_results"][0]["trial_ids"]) == 3
        action = result["action_results"][0]
        assert action["program"] == executed[-1]["program"]
        assert action["program_hash"] == executed[-1]["program_hash"]
        from runtime import automatic_gestures as ag
        published = ag.publish_run_results("arm-01", result, data_root=tmp_path,
                                           base_version=0)
        view = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                  bundle=bundle, frame=_frame(bundle),
                                  data_root=tmp_path)
        assert view["actions"]["search_next"]["usable"], view
        resolved = ag.resolve_navigation_action(
            "arm-01", {"profile_id": ag.NAVIGATION_PROFILE_ID,
                       "contract_version": ag.CONTRACT_VERSION,
                       "version": published["version"]}, view["binding"],
            "search_next", bundle=bundle, frame=_frame(bundle), data_root=tmp_path)
        assert resolved["program"] == executed[-1]["program"]

    @pytest.mark.parametrize("observation,run_state,reason", [
        ("missing", "unknown", "camera_displacement_unknown"),
        ("unknown", "unknown", "camera_displacement_unknown"),
        ("stationary", "failed", "camera_stationary"),
    ])
    def test_camera_does_not_certify_scroll(self, bundle, defaults, tmp_path,
                                            observation, run_state, reason):
        from runtime import automatic_gestures as ag

        coordinator = FakeCoordinator()
        coordinator.trace_factory = lambda _: good_trace_for(
            defaults["scroll_self_test"]["candidates"][
                min(len(coordinator.prepared) - 1, 1)])
        camera_io = scroll_camera_io(
            {"status": observation, "dy_px": 0.0})
        if observation == "missing":
            camera_io.pop("measure_scroll")
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=coordinator,
            execute_swipe=lambda _: {
                "controller_status": "completed", "motion_verified": True,
                "ok": True, "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda _: None,
            **camera_io, data_root=tmp_path, run_id=f"run-camera-{observation}")
        result = asyncio.run(runner.run(["scroll_up"]))
        assert result["state"] == run_state
        assert result["action_results"][0]["state"] == "unverified"
        assert result["action_results"][0]["reason_code"] == reason
        if run_state == "unknown":
            assert result["action_results"][0]["contact_status"] == "continuous"
            assert result["action_results"][0]["effect_status"] == "unknown"
        assert all(row["classification"] != "trial_passed"
                   for row in result["scroll_observations"])
        profile = ag.publish_run_results("arm-01", result, data_root=tmp_path)
        certificate = profile["actions"]["scroll_up"]
        assert certificate["state"] == "unverified"
        assert certificate["validated_at"] is None
        assert not ag.gestures_view(
            "arm-01", bundle=bundle, frame=_frame(bundle),
            data_root=tmp_path)["actions"]["scroll_up"]["usable"]

    @pytest.mark.parametrize("camera_mode", ["reported_unknown", "missing_measurement"])
    def test_camera_unknown_with_insufficient_phone_scroll_stops_after_one_motion(
            self, bundle, defaults, tmp_path, camera_mode):
        candidate = defaults["scroll_self_test"]["candidates"][0]
        coordinator = FakeCoordinator()

        def trace_factory(_trial_id):
            trace = good_trace_for(candidate)
            trace["scroll"] = scroll_evidence(1100, 1200)
            return trace

        coordinator.trace_factory = trace_factory
        executed = []

        def execute(plan):
            executed.append(plan)
            return {"controller_status": "completed", "motion_verified": True,
                    "ok": True, "position": {"z": -8.0, "state": "Idle"}}

        camera_io = scroll_camera_io(
            {"status": "unknown", "reason_code": "camera_frame_budget_exhausted"})
        if camera_mode == "missing_measurement":
            camera_io.pop("measure_scroll")
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=coordinator,
            execute_swipe=execute, execute_double_tap=lambda _: None,
            **camera_io, data_root=tmp_path,
            run_id=f"run-camera-{camera_mode}-low-scroll")

        result = asyncio.run(runner.run(["scroll_up", "scroll_down"]))

        assert result["state"] == "unknown"
        assert len(executed) == 1
        assert result["budget"]["physical_count"] == 1
        assert len(coordinator.prepared) == 1
        assert len(result["action_results"]) == 1
        assert result["action_results"][0]["state"] == "unverified"
        assert result["action_results"][0]["reason_code"] == "camera_displacement_unknown"
        assert result["scroll_observations"][0]["classification"] == "unknown"

    def test_failure_resets_streak_then_next_candidate(self, bundle, defaults,
                                                       tmp_path):
        attempts = {"n": 0}

        def execute(plan):
            attempts["n"] += 1
            return {"controller_status": "completed", "motion_verified": True,
                    "ok": True, "position": {"z": -8.0, "state": "Idle"}}

        def trace_factory(trial_id):
            attempts["trace"] = attempts.get("trace", 0) + 1
            candidate_index = 0 if attempts["trace"] <= 2 else 1
            candidate = defaults["scroll_self_test"]["candidates"][candidate_index]
            trace = good_trace_for(candidate)
            if attempts["trace"] == 2:  # 第二次试验失败（清零 S1 连续计数）
                trace["scroll"] = scroll_evidence(1200, 1100)
            return trace

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(trace_factory),
            execute_swipe=execute, execute_double_tap=execute,
            **scroll_camera_io(),
            data_root=tmp_path, run_id="run-streak")
        result = asyncio.run(runner.run(["scroll_up"]))
        # S1 两次（1过1败）→ 换 S2 连续 3 次通过
        assert attempts["n"] == 5
        assert result["state"] == "passed"
        assert result["action_results"][0]["state"] == "valid"

    def test_all_candidates_no_effect_action_unverified(self, bundle, defaults,
                                                        tmp_path):
        def execute(plan):
            return {"controller_status": "completed", "motion_verified": True,
                    "ok": True, "position": {"z": -8.0, "state": "Idle"}}

        def trace_factory(trial_id):
            candidate = defaults["scroll_self_test"]["candidates"][0]
            trace = good_trace_for(candidate)
            trace["scroll"] = scroll_evidence(1200, 1100)
            return trace

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(trace_factory),
            execute_swipe=execute, execute_double_tap=execute,
            **scroll_camera_io({"status": "stationary", "dy_px": 0.0}),
            data_root=tmp_path, run_id="run-noeffect")
        result = asyncio.run(runner.run(["scroll_up"]))
        assert result["action_results"][0]["state"] == "unverified"
        assert result["action_results"][0]["reason_code"] == "effect_not_observed"

    def test_budget_exhausted_before_action_returns_partial(self, bundle,
                                                             defaults, tmp_path):
        # 预算 4：scroll 连续 3 次 valid 后，double_tap 只试 1 次即耗尽未达成
        # → 有 valid 有未达成 → run=partial（自检插件层映射 failed）
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(
                lambda tid: good_trace_for(
                    defaults["scroll_self_test"]["candidates"][0])),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda p: {"controller_status": "completed",
                                          "motion_verified": True, "ok": True},
            **scroll_camera_io(),
            data_root=tmp_path, run_id="run-budget",
            budget_override={"max_physical_actions": 4, "timeout_ms": 600000})
        result = asyncio.run(runner.run(["scroll_up", "double_tap"]))
        assert result["state"] == "partial"
        assert result["action_results"][0]["state"] == "valid"
        assert result["budget"]["physical_count"] == 4
        assert result["budget"]["physical_limit"] == 4

    def test_unknown_stops_everything(self, bundle, defaults, tmp_path):
        executed = []

        def execute(plan):
            executed.append(plan)
            return {"controller_status": "unknown"}

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(lambda tid: good_trace_for(
                defaults["scroll_self_test"]["candidates"][0])),
            execute_swipe=execute,
            execute_double_tap=lambda p: {"controller_status": "completed"},
            capture_frame=lambda: {"frame_seq": 1, "png": b"png"},
            data_root=tmp_path, run_id="run-unknown")
        result = asyncio.run(runner.run(["scroll_up", "scroll_down"]))
        assert result["state"] == "unknown"
        assert len(executed) == 1  # 不再下一个候选/动作
        assert result["action_results"][0]["state"] == "unverified"

    def test_no_contact_ends_run(self, bundle, defaults, tmp_path):
        executed = []

        def execute(plan):
            executed.append(plan)
            return {"controller_status": "completed", "motion_verified": True,
                    "ok": True, "position": {"z": -8.0, "state": "Idle"}}

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(lambda tid: make_trace([], "timeout")),
            execute_swipe=execute,
            execute_double_tap=lambda p: {"controller_status": "completed"},
            **scroll_camera_io({"status": "stationary", "dy_px": 0.0}),
            data_root=tmp_path, run_id="run-nocontact")
        result = asyncio.run(runner.run(["scroll_up", "scroll_down"]))
        assert result["state"] == "failed"
        assert len(executed) == 1
        assert result["action_results"][0]["reason_code"] == "no_contact"

    def test_contact_broken_falls_to_s2_once(self, bundle, defaults, tmp_path):
        attempts = {"n": 0}

        def trace_factory(trial_id):
            attempts["n"] += 1
            candidate_index = 0 if attempts["n"] == 1 else 1
            candidate = defaults["scroll_self_test"]["candidates"][candidate_index]
            trace = good_trace_for(candidate)
            trace["endReason"] = "cancel"
            return trace

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(trace_factory),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda p: {"controller_status": "completed"},
            **scroll_camera_io({"status": "stationary", "dy_px": 0.0}),
            data_root=tmp_path, run_id="run-broken")
        result = asyncio.run(runner.run(["scroll_up"]))
        assert attempts["n"] == 2  # S1 断触 → 仅尝试 S2 一次
        assert result["action_results"][0]["state"] == "unverified"
        assert result["action_results"][0]["reason_code"] == "contact_broken"

    def test_prepare_failure_no_motion(self, bundle, defaults, tmp_path):
        coordinator = FakeCoordinator(lambda tid: good_trace_for(
            defaults["scroll_self_test"]["candidates"][0]))
        coordinator.fail_prepare = "trial_not_ready"
        executed = []
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01", coordinator=coordinator,
            execute_swipe=lambda plan: executed.append(plan) or {},
            execute_double_tap=lambda p: {},
            capture_frame=lambda: {"frame_seq": 1, "png": b"png"},
            data_root=tmp_path, run_id="run-prep")
        result = asyncio.run(runner.run(["scroll_up"]))
        assert not executed  # 物理未开始
        assert result["state"] == "failed"

    def test_run_json_persisted_with_transitions(self, bundle, defaults, tmp_path):
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(lambda tid: good_trace_for(
                defaults["scroll_self_test"]["candidates"][0])),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda p: {"controller_status": "completed"},
            **scroll_camera_io(),
            data_root=tmp_path, run_id="run-persist")
        result = asyncio.run(runner.run(["scroll_up"]))
        path = tmp_path / "devices" / "arm-01" / "gesture_validation_runs" \
            / "run-persist.json"
        assert path.exists()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["state"] == "passed"
        assert saved["run_id"] == "run-persist"
        assert saved["budget"]["physical_limit"] == 36
        assert saved["input_binding"]["device_id"] == "arm-01"
        states = [entry["state"] for entry in saved["transitions"]]
        assert states[0] == "queued"
        assert "executing" in states  # 物理动作前持久写 executing

    @pytest.mark.parametrize("mutation", [None, "interval", "point", "binding", "hash", "missing"])
    def test_double_tap_action(self, bundle, defaults, tmp_path, mutation):
        taps = {"n": 0}
        executed = []

        def execute(params):
            executed.append(params)
            return {"controller_status": "completed", "motion_verified": True, "ok": True}

        def trace_factory(trial_id):
            taps["n"] += 1
            return make_trace([
                {"index": 0, "contactIndex": 0, "action": "down", "pointerId": 0,
                 "tMs": 0, "xPx": 540, "yPx": 1200, "historical": False},
                {"index": 1, "contactIndex": 0, "action": "up", "pointerId": 0,
                 "tMs": 40, "xPx": 540, "yPx": 1200, "historical": False},
                {"index": 2, "contactIndex": 0, "action": "down", "pointerId": 0,
                 "tMs": 90, "xPx": 540, "yPx": 1200, "historical": False},
                {"index": 3, "contactIndex": 0, "action": "up", "pointerId": 0,
                 "tMs": 130, "xPx": 540, "yPx": 1200, "historical": False},
            ], end_reason="up",
                recognition={"kind": "double_tap", "recognized": True,
                             "firstUpMs": 40, "secondDownMs": 90,
                             "systemDoubleTapTimeoutMs": 300,
                             "systemDoubleTapSlopPx": 100})

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(trace_factory),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=execute,
            capture_frame=lambda: {"frame_seq": 1, "png": b"png"},
            data_root=tmp_path, run_id="run-tap")
        result = asyncio.run(runner.run(["double_tap"]))
        assert result["state"] == "passed"
        assert result["action_results"][0]["gesture_id"] == "double_tap"
        assert result["action_results"][0]["state"] == "valid"
        action = result["action_results"][0]
        assert "program" not in action
        assert action["validated_primitive"] == {"kind": "calibrated_double_tap", **executed[-1]}
        assert len(executed) == 3
        from runtime import automatic_gestures as ag
        profile = ag.publish_run_results("arm-01", result, data_root=tmp_path)
        entry = profile["actions"]["double_tap"]
        if mutation == "interval":
            entry["validated_primitive"]["interval_ms"] = 100
        elif mutation == "point":
            entry["validated_primitive"]["point"]["x"] = 0.1
        elif mutation == "binding":
            entry["validated_primitive"]["binding"]["device_id"] = "arm-02"
        elif mutation == "hash":
            entry["primitive_hash"] = "bad"
        elif mutation == "missing":
            entry.pop("validated_primitive")
        ag.profile_path("arm-01", tmp_path).write_text(json.dumps(profile))
        view = ag.gestures_view("arm-01", bundle=bundle, frame=_frame(bundle), data_root=tmp_path)
        assert view["actions"]["double_tap"]["usable"] is (mutation is None), view

    def test_program_change_restarts_consecutive_pass_count(self, bundle, defaults, tmp_path, monkeypatch):
        executed = []
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01", coordinator=FakeCoordinator(),
            execute_swipe=lambda _: None, execute_double_tap=lambda _: None,
            capture_frame=lambda: None, data_root=tmp_path, run_id="changing-program")

        async def trial(action, trial_id, program):
            executed.append(program)
            if len(executed) == 2:
                runner._region_override = {**REGION, "widthPx": 1060}
            return {"classification": "trial_passed", "reason_code": None,
                    "controller_status": "completed", "contact_status": "continuous"}

        monkeypatch.setattr(runner, "_execute_scroll_trial", trial)
        result = asyncio.run(runner.run(["scroll_up"]))
        assert len(executed) == 5
        assert executed[0] != executed[-1]
        assert executed[-3:] == [result["action_results"][0]["program"]] * 3

    def test_candidate_feed_over_limit_skipped(self, bundle, defaults, tmp_path):
        # 全部候选 F 超限（feed_limit_override=1000 < 候选最小 F1500）
        # → 动作 unverified 并记录跳过
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(lambda tid: good_trace_for(
                defaults["scroll_self_test"]["candidates"][0])),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda p: {"controller_status": "completed"},
            capture_frame=lambda: {"frame_seq": 1, "png": b"png"},
            data_root=tmp_path, run_id="run-skipfeed",
            feed_limit_override=1000)
        result = asyncio.run(runner.run(["scroll_up"]))
        assert result["action_results"][0]["state"] == "unverified"

class TestWaitingForPhone:
    def test_create_session_retries_until_test_page_open(self, bundle,
                                                         defaults, tmp_path):
        """校准完成自动触发的自检：手机还在校准页时应等待重试（§8.1 waiting_phone）。"""
        attempts = {"n": 0}

        class SlowPhoneCoordinator(FakeCoordinator):
            async def create_session(self, **kwargs):
                attempts["n"] += 1
                if attempts["n"] <= 3:
                    raise gv.ScreenImeApiError(
                        409, "client_not_foreground", "手机客户端未注册或已失效")
                return await super().create_session(**kwargs)

        sleeps = []
        clock_state = {"t": 0.0}

        async def fast_sleep(seconds):
            sleeps.append(seconds)
            clock_state["t"] += seconds

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=SlowPhoneCoordinator(
                lambda tid: good_trace_for(
                    defaults["scroll_self_test"]["candidates"][0])),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda p: {"controller_status": "completed"},
            **scroll_camera_io(),
            data_root=tmp_path, run_id="run-waitphone", sleep=fast_sleep,
            clock=lambda: clock_state["t"])
        result = asyncio.run(runner.run(["scroll_up"]))
        assert result["state"] == "passed"
        assert attempts["n"] == 4  # 3 次未就绪 + 第 4 次成功
        assert len(sleeps) == 3

    def test_waiting_phone_timeout_fails_cleanly(self, bundle, defaults,
                                                 tmp_path):
        class NeverReadyCoordinator(FakeCoordinator):
            async def create_session(self, **kwargs):
                raise gv.ScreenImeApiError(
                    409, "client_not_foreground", "手机客户端未注册或已失效")

        clock_state = {"t": 0.0}

        async def fast_sleep(seconds):
            clock_state["t"] += seconds

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=NeverReadyCoordinator(
                lambda tid: good_trace_for(
                    defaults["scroll_self_test"]["candidates"][0])),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda p: {"controller_status": "completed"},
            capture_frame=lambda: {"frame_seq": 1, "png": b"png"},
            data_root=tmp_path, run_id="run-waittimeout", sleep=fast_sleep,
            clock=lambda: clock_state["t"])
        result = asyncio.run(runner.run(["scroll_up"]))
        assert result["state"] == "failed"
        reason = (result.get("stop_reason")
                  or result["action_results"][0]["reason_code"])
        assert reason == "client_not_foreground"


# ---------- 会话泄漏防线与屏幕尺寸来源（评审 P1-1/P1-2） ----------

class RecordingCoordinator(FakeCoordinator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.finished_sessions: list[tuple[str, str]] = []

    async def finish_session(self, session_id, outcome):
        self.finished_sessions.append((session_id, outcome))
        return await super().finish_session(session_id, outcome)


class TestRunnerExceptionSessionCleanup:
    @pytest.mark.parametrize("action", ["scroll_up", "double_tap", "system_home"])
    def test_explicit_no_motion_refusal_is_failed_and_closes_session(
            self, bundle, defaults, tmp_path, action):
        coordinator = RecordingCoordinator()
        coordinator.ready_payload["systemGestureInsets"] = {
            "left": 20, "top": 0, "right": 20, "bottom": 30}
        calls = []

        def refuse(params):
            from runtime.errors import ApiError
            calls.append(params)
            error = ApiError("device_not_eligible", "revoked before driver")
            error.motion_not_started = True
            raise error

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01", coordinator=coordinator,
            execute_swipe=refuse, execute_double_tap=refuse,
            capture_frame=lambda: None, verify_page=lambda _: {"confirmed": True},
            data_root=tmp_path)
        result = asyncio.run(runner.run([action]))
        assert len(calls) == 1
        assert result["state"] == "failed"
        assert result["stop_reason"] == "device_not_eligible"
        assert result["budget"]["physical_count"] == 0
        assert result["action_results"][0]["controller_status"] == "not_started"
        assert result["action_results"][0]["state"] != "valid"
        assert coordinator.finished_sessions == [(coordinator.session_id, "failed")]
        assert json.loads(gv.run_path("arm-01", runner.run_id, tmp_path).read_text())["state"] == "failed"

    def test_unexpected_exception_still_finishes_session(self, bundle, defaults,
                                                         tmp_path):
        """P1-1：动作循环外的异常也必须终结协调器会话并落终态。

        否则单活动会话互斥会拒绝该设备后续全部自检（只能重启进程），
        run 文件也停留非终态。
        """
        coordinator = RecordingCoordinator()

        def boom(plan):
            raise RuntimeError("executor 超时取消")

        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=coordinator,
            execute_swipe=boom, execute_double_tap=boom,
            capture_frame=lambda: None, data_root=tmp_path)
        with pytest.raises(RuntimeError):
            asyncio.run(runner.run(["scroll_up"]))
        assert coordinator.finished_sessions == \
            [(coordinator.session_id, "unknown")]
        run_file = gv.run_path("arm-01", runner.run_id, tmp_path)
        state = json.loads(run_file.read_text(encoding="utf-8"))["state"]
        assert state == "unknown"
        assert state in gv.RUN_TERMINAL_STATES


class TestFrameSizeFromBundle:
    def test_scroll_expectations_use_bundle_screen_size(self, bundle, defaults):
        """P1-2：期望像素换算须用当前校准屏幕尺寸，而非 1080x2320 硬编码。"""
        from drivers import gesture_program as gp
        from drivers import screen_gestures as sg

        wide = gesture_bundle()
        wide["screen"]["screenWidth"] = 1440
        wide["screen"]["screenHeight"] = 3200
        frame = gp.build_coordinate_frame(
            binding=sg.binding_snapshot(wide), calibration_run_id="run-wide",
            root_width_px=1440, root_height_px=3200,
            display_width_px=1440, display_height_px=3200,
            root_origin_display_px=[0, 0], density=3.0,
            source_client_instance_id="0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001")
        candidate = defaults["scroll_self_test"]["candidates"][0]
        program, _meta = gv.build_scroll_program(
            "scroll_up", candidate, REGION, wide, frame)
        plan = gv.plan_scroll_trial(program, wide, frame)
        result = gv.evaluate_scroll_trial(
            controller_result={"controller_status": "completed",
                               "motion_verified": True, "ok": True,
                               "position": {"z": -8.0, "state": "Idle"}},
            trace=good_trace_for(candidate), plan=plan, bundle=wide,
            defaults=defaults, camera_result=displaced_camera())
        # 硬编码 1080x2320 时期望点会整体错比例 → mapping_mismatch 假失败
        assert result["classification"] == "trial_passed", result


# ---------- 卫生级修正（R1/R3 P2）：调参入口真实生效 ----------

class TestTunableJudgements:
    def _plan(self, bundle, defaults):
        candidate = defaults["scroll_self_test"]["candidates"][0]
        program, _meta = gv.build_scroll_program(
            "scroll_up", candidate, REGION, bundle, _frame(bundle))
        return gv.plan_scroll_trial(program, bundle, _frame(bundle))

    def test_path_deviation_percentile_is_configurable(self, bundle, defaults):
        """p95 写死 0.95 时配置项 path_deviation_percentile 是假入口。"""
        candidate = defaults["scroll_self_test"]["candidates"][0]
        x = REGION["leftPx"] + candidate["x"] * REGION["widthPx"]
        y0 = REGION["topPx"] + candidate["y_start"] * REGION["heightPx"]
        y1 = REGION["topPx"] + candidate["y_end"] * REGION["heightPx"]
        events = [{"index": 0, "contactIndex": 0, "action": "down", "pointerId": 0,
                   "tMs": 0, "xPx": x, "yPx": y0, "historical": False}]
        for index in range(1, 20):
            y = y0 + (y1 - y0) * index / 19
            # 第 19 个事件离路径 80px（远超容差）：20 个里占 5%
            out = 80.0 if index == 19 else 0.0
            events.append({"index": index, "contactIndex": 0, "action": "move",
                           "pointerId": 0, "tMs": index * 100,
                           "xPx": x + out, "yPx": y, "historical": False})
        events.append({"index": 20, "contactIndex": 0, "action": "up",
                       "pointerId": 0, "tMs": 2000, "xPx": x, "yPx": y1,
                       "historical": False})
        trace = make_trace(events, scroll=scroll_evidence(480, 1200))
        controller = {"controller_status": "completed", "motion_verified": True,
                      "ok": True, "position": {"z": -8.0, "state": "Idle"}}

        loose = json.loads(json.dumps(defaults))
        loose["scroll_self_test"]["pass_thresholds"]["path_deviation_percentile"] = 0.95
        result = gv.evaluate_scroll_trial(
            controller_result=controller, trace=trace, plan=self._plan(bundle, defaults),
            bundle=bundle, defaults=loose, camera_result=displaced_camera())
        assert result["classification"] == "trial_passed", result

        strict = json.loads(json.dumps(defaults))
        strict["scroll_self_test"]["pass_thresholds"]["path_deviation_percentile"] = 0.99
        result = gv.evaluate_scroll_trial(
            controller_result=controller, trace=trace, plan=self._plan(bundle, defaults),
            bundle=bundle, defaults=strict)
        assert result["classification"] == "mapping_mismatch"

    def test_all_candidates_skipped_reports_real_reason(self, bundle, defaults,
                                                        tmp_path):
        """候选全部跳过时 reason 应携带真实跳过原因，不是预算耗尽。"""
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(lambda tid: good_trace_for(
                defaults["scroll_self_test"]["candidates"][0])),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda p: {"controller_status": "completed"},
            capture_frame=lambda: {"frame_seq": 1, "png": b"png"},
            data_root=tmp_path, run_id="run-skipreason",
            feed_limit_override=100)
        result = asyncio.run(runner.run(["scroll_up"]))
        assert result["action_results"][0]["state"] == "unverified"
        assert "candidate_skipped" in (result["action_results"][0]["reason_code"]
                                       or ""), result["action_results"][0]

    def test_all_valid_run_is_passed_even_when_budget_just_exhausted(
            self, bundle, defaults, tmp_path):
        """全 valid 的 run 不因预算恰在最后一次判定后耗尽而降级 partial。"""
        runner = gv.GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
            device_id="arm-01", ime_device_id="ime-01",
            coordinator=FakeCoordinator(lambda tid: good_trace_for(
                defaults["scroll_self_test"]["candidates"][0])),
            execute_swipe=lambda plan: {"controller_status": "completed",
                                        "motion_verified": True, "ok": True,
                                        "position": {"z": -8.0, "state": "Idle"}},
            execute_double_tap=lambda p: {"controller_status": "completed"},
            **scroll_camera_io(),
            data_root=tmp_path, run_id="run-justbudget",
            budget_override={"max_physical_actions": 3})
        result = asyncio.run(runner.run(["scroll_up"]))
        assert result["state"] == "passed", result
