"""D4a：gesture-program.v1 规划器（规范第 6、22.9 节）。

纯函数测试：程序语义校验、canonical hash、坐标桥校验、SWIPE/PATH1 编译、
量化、时长/进给拒绝与公开摘要不含机械坐标。
"""
from __future__ import annotations

import copy
import math

import pytest

from drivers import gesture_program as gp
from tests.test_screen_gesture_driver import gesture_bundle


def program(**strokes_override):
    return {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": strokes_override.get("strokes", [stroke(0.85, 0.58, 2000)]),
    }


def stroke(y_start=0.85, y_end=0.58, duration_ms=2000, x=0.5, points=None):
    if points is None:
        points = [{"x": x, "y": y_start, "t_ms": 0},
                  {"x": x, "y": y_end, "t_ms": duration_ms}]
    return {"points": points, "hold_before_ms": 0, "hold_after_ms": 0,
            "gap_after_ms": 0}


@pytest.fixture()
def bundle():
    return gesture_bundle()


@pytest.fixture()
def frame(bundle):
    from drivers.screen_gestures import binding_snapshot
    return gp.build_coordinate_frame(
        binding=binding_snapshot(bundle),
        calibration_run_id="run-abc123",
        root_width_px=bundle["screen"]["screenWidth"],
        root_height_px=bundle["screen"]["screenHeight"],
        display_width_px=1080, display_height_px=2400,
        root_origin_display_px=[0, 80], density=3.0,
        source_client_instance_id="0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001")


def test_internal_frame_plans_scroll_without_android_metadata(bundle):
    internal = gp.frame_for_internal_gesture(bundle, None)
    assert gp.is_internal_coordinate_frame(internal)
    assert "display_width_px" not in internal
    assert "root_origin_display_px" not in internal
    assert gp.plan_program(program(), bundle, internal)["program"] == program()


def test_internal_frame_cannot_plan_system_edge(bundle):
    from runtime.gesture_validation import edge_swipe_candidates
    internal = gp.build_calibration_coordinate_frame(bundle)
    with pytest.raises(gp.GestureProgramError) as caught:
        edge_swipe_candidates(
            kind="back_left", system_gesture_insets={"left": 30},
            display_width_px=1080, display_height_px=2400, frame=internal,
            uncertainty_px=3, defaults={"system_back": {}, "scroll_self_test": {
                "candidates": [{"feed": 300}]}})
    assert caught.value.reason_code == "navigation_mode_unsupported"


# ---------- 程序语义校验 ----------

class TestValidateProgram:
    def test_legal_program_passes(self):
        gp.validate_program(program())

    def test_first_point_must_be_zero(self):
        bad = program(strokes=[stroke(points=[{"x": .5, "y": .8, "t_ms": 10},
                                               {"x": .5, "y": .6, "t_ms": 500}])])
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.validate_program(bad)
        assert "t_ms" in str(exc.value)

    def test_times_must_strictly_increase(self):
        bad = program(strokes=[stroke(points=[{"x": .5, "y": .8, "t_ms": 0},
                                               {"x": .5, "y": .7, "t_ms": 400},
                                               {"x": .5, "y": .6, "t_ms": 400}])])
        with pytest.raises(gp.GestureProgramError):
            gp.validate_program(bad)

    def test_last_stroke_gap_must_be_zero(self):
        bad = program(strokes=[{"points": [{"x": .5, "y": .8, "t_ms": 0}],
                                "hold_before_ms": 0, "hold_after_ms": 0,
                                "gap_after_ms": 100}])
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.validate_program(bad)
        assert "gap" in str(exc.value)

    def test_stroke_contact_budget(self):
        bad = program(strokes=[{"points": [{"x": .5, "y": .8, "t_ms": 0},
                                            {"x": .5, "y": .6, "t_ms": 5000}],
                                "hold_before_ms": 100, "hold_after_ms": 0,
                                "gap_after_ms": 0}])
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.validate_program(bad)
        assert "5000" in str(exc.value)

    def test_total_program_budget(self):
        s = lambda: {"points": [{"x": .5, "y": .8, "t_ms": 0},
                                 {"x": .5, "y": .6, "t_ms": 3000}],
                     "hold_before_ms": 500, "hold_after_ms": 500, "gap_after_ms": 500}
        strokes = [s(), s()]
        strokes[-1]["gap_after_ms"] = 0
        bad = program(strokes=strokes)  # (3000+500+500+500)+(3000+500+500+0) = 8500 > 8000
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.validate_program(bad)
        assert "8000" in str(exc.value)

    def test_non_finite_numbers_rejected(self):
        bad = program(strokes=[stroke(points=[{"x": math.nan, "y": .8, "t_ms": 0},
                                               {"x": .5, "y": .6, "t_ms": 500}])])
        with pytest.raises(gp.GestureProgramError):
            gp.validate_program(bad)

    def test_schema_violations_rejected(self):
        with pytest.raises(gp.GestureProgramError):
            gp.validate_program({"program_version": "gesture-program.v1",
                                 "coordinate_space": "calibration_root_normalized.v1",
                                 "strokes": [], "z": 1})

    def test_program_hash_is_canonical(self):
        first = program()
        second = program()
        assert gp.program_hash(first) == gp.program_hash(second)
        reordered = {k: first[k] for k in reversed(list(first))}
        assert gp.program_hash(reordered) == gp.program_hash(first)
        different = program(strokes=[stroke(0.85, 0.58, 2001)])
        assert gp.program_hash(different) != gp.program_hash(first)


# ---------- 坐标桥 ----------

class TestCoordinateFrame:
    def test_frame_hash_excludes_itself(self, frame):
        frame["collected_at"] = "2026-09-15T10:00:00Z"
        first = gp.coordinate_frame_hash(frame)
        frame["coordinate_frame_hash"] = "0" * 64
        # 哈希字段自身的取值不影响结果（排除自身），而非与某个固定值比较
        assert gp.coordinate_frame_hash(frame) == first

    def test_build_frame_hash_verifies(self, frame):
        gp.validate_coordinate_frame(frame, gesture_bundle())

    def test_rotation_unsupported(self, frame):
        frame["rotation"] = 1
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.validate_coordinate_frame(frame, gesture_bundle())
        assert exc.value.reason_code == "coordinate_frame_unsupported"

    def test_root_size_mismatch_is_binding_change(self, frame):
        frame["root_height_px"] = frame["root_height_px"] + 10
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.validate_coordinate_frame(frame, gesture_bundle())
        assert exc.value.reason_code == "gesture_binding_changed"

    def test_missing_frame(self, bundle):
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(), bundle, None)
        assert exc.value.reason_code == "coordinate_frame_missing"

    def test_tampered_hash_rejected(self, frame, bundle):
        frame["coordinate_frame_hash"] = "0" * 64
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.validate_coordinate_frame(frame, bundle)
        assert exc.value.reason_code == "gesture_binding_changed"

    def test_save_and_load_roundtrip(self, frame, tmp_path):
        gp.save_coordinate_frame("arm-01", frame, data_root=tmp_path)
        loaded = gp.load_coordinate_frame("arm-01", data_root=tmp_path)
        assert loaded == frame
        assert gp.load_coordinate_frame("arm-02", data_root=tmp_path) is None


# ---------- 规划与编译 ----------

class TestPlanProgram:
    def test_two_point_stroke_compiles_to_swipe(self, bundle, frame):
        plan = gp.plan_program(program(), bundle, frame)
        assert plan["execution_mode"] == "swipe"
        assert plan["calculated"] is True
        assert plan["binding"]["calibration_fingerprint"]
        assert plan["coordinate_frame_hash"] == frame["coordinate_frame_hash"]
        assert plan["program_hash"] == gp.program_hash(program())
        assert plan["plan_id"]
        stroke_plan = plan["strokes"][0]
        assert stroke_plan["duration_ms"] == 2000
        assert stroke_plan["feed"] == pytest.approx(
            stroke_plan["distance_mm"] / 2000 * 60000, rel=1e-3)
        assert stroke_plan["distance_mm"] == pytest.approx(40.4, abs=0.5)

    def test_public_summary_has_no_machine_coordinates(self, bundle, frame):
        plan = gp.plan_program(program(), bundle, frame)
        summary = gp.public_plan_summary(plan)
        dumped = repr(summary)
        assert "robotX" not in dumped and "robotY" not in dumped
        assert "mech_points" not in dumped
        assert summary["program_hash"] == plan["program_hash"]

    def test_fast_user_timing_rejected_with_feasible_range(self, bundle, frame):
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(strokes=[stroke(0.85, 0.58, 800)]), bundle, frame)
        assert exc.value.reason_code == "path_timing_unachievable"
        assert "808" in str(exc.value) or "809" in str(exc.value)

    def test_multi_point_requires_path_feature(self, bundle, frame):
        three = stroke(points=[{"x": .5, "y": .85, "t_ms": 0},
                               {"x": .5, "y": .7, "t_ms": 1000},
                               {"x": .5, "y": .58, "t_ms": 1800}])
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(strokes=[three]), bundle, frame,
                            limits={"path_feature_available": False})
        assert exc.value.reason_code == "path_feature_unavailable"

    def test_hold_requires_path_feature(self, bundle, frame):
        with_hold = {"points": [{"x": .5, "y": .85, "t_ms": 0},
                                 {"x": .5, "y": .58, "t_ms": 2000}],
                     "hold_before_ms": 200, "hold_after_ms": 0, "gap_after_ms": 0}
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(strokes=[with_hold]), bundle, frame)
        assert exc.value.reason_code == "path_feature_unavailable"

    def test_path1_compile_segments(self, bundle, frame):
        three = stroke(points=[{"x": .5, "y": .85, "t_ms": 0},
                               {"x": .5, "y": .7, "t_ms": 1000},
                               {"x": .5, "y": .58, "t_ms": 1800}])
        plan = gp.plan_program(program(strokes=[three]), bundle, frame,
                               limits={"path_feature_available": True})
        assert plan["execution_mode"] == "path1"
        stroke_plan = plan["strokes"][0]
        segments = stroke_plan["segments"]
        assert segments[0]["op"] == "down"
        assert segments[-1]["op"] == "up"
        moves = [s for s in segments if s["op"] == "move"]
        assert len(moves) == 2
        # dx/dy 由量化后的绝对机械点相减；两段 dy 同号且和≈总距离
        total_dy = sum(s["dy"] for s in moves)
        assert abs(total_dy) == pytest.approx(stroke_plan["distance_mm"], abs=1e-3)
        assert all(abs(s["dx"]) < 1e-6 for s in moves)
        assert all(100 <= s["feed"] <= 3000 for s in moves)

    def test_stay_point_becomes_hold(self, bundle, frame):
        stay = stroke(points=[{"x": .5, "y": .8, "t_ms": 0},
                              {"x": .5, "y": .8, "t_ms": 300},
                              {"x": .5, "y": .58, "t_ms": 1300}])
        plan = gp.plan_program(program(strokes=[stay]), bundle, frame,
                               limits={"path_feature_available": True})
        segments = plan["strokes"][0]["segments"]
        holds = [s for s in segments if s["op"] == "hold"]
        assert holds and holds[0]["ms"] == 300

    def test_short_segment_rejected(self, bundle, frame):
        quick = stroke(points=[{"x": .5, "y": .7, "t_ms": 0},
                               {"x": .52, "y": .69, "t_ms": 10}])
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(strokes=[quick]), bundle, frame,
                            limits={"path_feature_available": True})
        assert exc.value.reason_code == "path_timing_unachievable"

    def test_segment_cap_rejected(self, bundle, frame):
        points = [{"x": .5, "y": .85 - index * 0.004, "t_ms": index * 150}
                  for index in range(13)]
        capped = stroke(points=points)
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(strokes=[capped]), bundle, frame,
                            limits={"path_feature_available": True,
                                    "max_mech_segments": 8})
        assert exc.value.reason_code == "path_feature_unavailable"

    def test_hold_compiles_into_path1_segments(self, bundle, frame):
        """P1-4：hold_before/hold_after 必须编译成 hold 段，声明在真机才生效。"""
        held = stroke(0.7, 0.5, 1200)
        held["hold_before_ms"] = 300
        held["hold_after_ms"] = 500
        plan = gp.plan_program(program(strokes=[held]), bundle, frame,
                               limits={"path_feature_available": True})
        segments = plan["strokes"][0]["segments"]
        assert [seg["op"] for seg in segments] == \
            ["down", "hold", "move", "hold", "up"]
        assert segments[1]["ms"] == 300
        assert segments[-2]["ms"] == 500
        assert plan["strokes"][0]["duration_ms"] == 2000

    def test_generated_hold_over_daemon_cap_rejected_at_planning(self, bundle, frame):
        """R2 N7：量化重合点生成的 hold 段也要受 2000ms 上限（schema 只管声明型）。"""
        stalled = stroke(points=[{"x": .5, "y": .7, "t_ms": 0},
                                 {"x": .5, "y": .7, "t_ms": 2600},
                                 {"x": .5, "y": .5, "t_ms": 4200}])
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(strokes=[stalled]), bundle, frame,
                            limits={"path_feature_available": True})
        assert exc.value.reason_code == "path_timing_unachievable"

    def test_segment_cap_counts_total_including_down_and_up(self, bundle, frame):
        """P1-5：段数上限须按 daemon 口径（含首 down/末 up 的总数）在规划期拒绝。"""
        # 13 点折线 = 12 个 move + down + up = 总数 14；上限 12 时必须在规划期拒绝
        points = [{"x": .5, "y": .85 - index * 0.004, "t_ms": index * 150}
                  for index in range(13)]
        capped = stroke(points=points)
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(strokes=[capped]), bundle, frame,
                            limits={"path_feature_available": True,
                                    "max_mech_segments": 12})
        assert exc.value.reason_code == "path_feature_unavailable"

    def test_total_distance_limit(self, bundle, frame):
        # 150mm 上限：两条 ~90mm 对角线滑动
        long_stroke = lambda: {"points": [{"x": .05, "y": .99, "t_ms": 0},
                                           {"x": .95, "y": .56, "t_ms": 2000}],
                               "hold_before_ms": 0, "hold_after_ms": 0,
                               "gap_after_ms": 0}
        strokes = [long_stroke(), long_stroke()]
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(program(strokes=strokes), bundle, frame,
                            limits={"path_feature_available": True})
        assert "150" in str(exc.value)

    def test_program_and_plan_are_not_mutated(self, bundle, frame):
        source = program()
        frozen = copy.deepcopy(source)
        gp.plan_program(source, bundle, frame)
        assert source == frozen

    def test_evaluate_program_api_exists(self, bundle, frame):
        plan = gp.plan_program(program(), bundle, frame)
        verdict = gp.evaluate_program(plan, {"controller_status": "completed"},
                                     None, None, {"effect": "manual_only"})
        assert set(verdict) >= {"controller_status", "contact_status",
                                "effect_status", "reason_code", "message",
                                "evidence_refs", "retry_policy"}
        assert verdict["controller_status"] == "completed"
        assert verdict["contact_status"] == "not_observed"
        assert verdict["effect_status"] == "not_checked"
