"""D4b：三层证据归约 gesture_verdict.v1（规范第 3 节）。

关键断言：controller completed 不等于成功；CANCEL 的两种含义；
contact 只在证据存在时判定；effect 需要指定验证器。
"""
from __future__ import annotations

import pytest

from drivers import gesture_verdict as gv


def trace(events, end_reason="up", dropped=0):
    return {"events": events, "endReason": end_reason,
            "droppedSampleCount": dropped}


def contact(action, t_ms, pointer=0, index=0):
    return {"index": index, "contactIndex": 0, "action": action,
            "pointerId": pointer, "tMs": t_ms, "xPx": 540, "yPx": 1000,
            "historical": False}


class TestEnums:
    def test_make_verdict_rejects_bad_controller(self):
        with pytest.raises(ValueError):
            gv.make_verdict(controller_status="fine", contact_status="continuous",
                            effect_status="verified")

    def test_make_verdict_rejects_bad_retry_policy(self):
        with pytest.raises(ValueError):
            gv.make_verdict(controller_status="completed",
                            contact_status="continuous", effect_status="verified",
                            retry_policy="just_retry")

    def test_make_verdict_returns_all_fields(self):
        verdict = gv.make_verdict(controller_status="completed",
                                  contact_status="continuous",
                                  effect_status="no_effect",
                                  reason_code="effect_not_observed",
                                  message="列表未滚动", evidence_refs=[{"e": 1}])
        assert verdict["controller_status"] == "completed"
        assert verdict["evidence_refs"] == [{"e": 1}]


class TestControllerStatus:
    def test_driver_swipe_success_maps_completed(self):
        result = {"ok": True, "swipe_executed": True, "motion_verified": True,
                  "outcome_hint": "success"}
        assert gv.controller_status_from_result(result) == "completed"

    def test_outcome_unknown_maps_unknown(self):
        assert gv.controller_status_from_result(
            {"ok": True, "motion_verified": False,
             "outcome_hint": "unknown"}) == "unknown"

    def test_empty_maps_not_started(self):
        assert gv.controller_status_from_result(None) == "not_started"
        assert gv.controller_status_from_result({}) == "not_started"

    def test_explicit_status_passthrough(self):
        assert gv.controller_status_from_result(
            {"controller_status": "not_started"}) == "not_started"


class TestContactStatus:
    def test_single_down_up_is_continuous(self):
        events = [contact("down", 0), contact("move", 100), contact("up", 300)]
        assert gv.contact_status_from_trace(trace(events)) == "continuous"

    def test_cancel_is_cancelled(self):
        events = [contact("down", 0), contact("cancel", 100)]
        assert gv.contact_status_from_trace(trace(events, "cancel")) == "cancelled"

    def test_overflow_is_broken(self):
        events = [contact("down", 0)]
        assert gv.contact_status_from_trace(
            trace(events, "overflow", dropped=5)) == "broken"

    def test_two_pointers_is_broken(self):
        events = [contact("down", 0), contact("down", 100, pointer=1, index=1)]
        assert gv.contact_status_from_trace(
            trace(events, "multi_touch")) == "broken"

    def test_no_events_not_observed(self):
        assert gv.contact_status_from_trace(trace([], "timeout")) == "not_observed"
        assert gv.contact_status_from_trace(None) == "not_observed"
        assert gv.contact_status_from_trace({}) == "not_observed"

    def test_double_tap_timeout_stays_unknown(self):
        events = [contact("down", 0), contact("up", 40)]
        assert gv.contact_status_from_trace(trace(events, "timeout")) == "unknown"

    def test_missing_up_is_broken(self):
        events = [contact("down", 0), contact("move", 100)]
        assert gv.contact_status_from_trace(trace(events)) == "broken"


class TestEvaluateGestureProgram:
    def test_controller_completed_alone_is_not_success(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None, None,
            {"effect": "scroll_displacement", "min_displacement_px": 100})
        assert verdict["controller_status"] == "completed"
        assert verdict["contact_status"] == "not_observed"
        assert verdict["effect_status"] == "unknown"

    def test_scroll_displacement_verified(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"status": "displaced", "dy_px": -300, "dx_px": 2},
            {"effect": "scroll_displacement", "min_displacement_px": 100})
        assert verdict["effect_status"] == "verified"

    def test_scroll_displacement_below_threshold_no_effect(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"status": "displaced", "dy_px": -20},
            {"effect": "scroll_displacement", "min_displacement_px": 100})
        assert verdict["effect_status"] == "no_effect"

    def test_scroll_unknown_status_is_never_verified(self):
        """测量模块判 unknown（如 ocr_rows_not_consistent）时 dy 带数值也不放行。"""
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"status": "unknown", "dy_px": -300,
             "reason_code": "ocr_rows_not_consistent"},
            {"effect": "scroll_displacement", "min_displacement_px": 100})
        assert verdict["effect_status"] == "unknown"

    def test_scroll_missing_status_is_unknown(self):
        """缺 status 的观测不构成证据（fail-closed）。"""
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"dy_px": -300},
            {"effect": "scroll_displacement", "min_displacement_px": 100})
        assert verdict["effect_status"] == "unknown"

    def test_scroll_missing_policy_minimum_is_unknown(self):
        """policy 漏配 min_displacement_px 时不判 verified（缺省 fail-closed）。"""
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"status": "displaced", "dy_px": -300},
            {"effect": "scroll_displacement"})
        assert verdict["effect_status"] == "unknown"

    def test_controller_unknown_downgrades_no_effect_to_unknown(self):
        """controller unknown 时 no_effect 对称降级：视觉没动但控制结果未知，
        不得当"确定没效果"走 failed 重试路径重放真机动作。"""
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "unknown"}, None,
            {"status": "stationary", "dy_px": 0.2},
            {"effect": "scroll_displacement", "min_displacement_px": 100})
        assert verdict["effect_status"] == "unknown"
        assert verdict["reason_code"] == "effect_unknown"

    def test_scroll_stationary_is_no_effect(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"status": "stationary", "dy_px": 0.2},
            {"effect": "scroll_displacement", "min_displacement_px": 100})
        assert verdict["effect_status"] == "no_effect"

    def test_scroll_wrong_direction_is_no_effect(self):
        """expected_sign 相反：位移真实但方向与意图不符，不当 verified。"""
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"status": "displaced", "dy_px": 300},
            {"effect": "scroll_displacement", "min_displacement_px": 100,
                       "expected_sign": -1})
        assert verdict["effect_status"] == "no_effect"

    def test_scroll_expected_sign_match_verified(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"status": "displaced", "dy_px": -300},
            {"effect": "scroll_displacement", "min_displacement_px": 100,
                       "expected_sign": -1})
        assert verdict["effect_status"] == "verified"

    def test_manual_only_is_not_checked(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"},
            trace([contact("down", 0), contact("up", 200)]), None,
            {"effect": "manual_only"})
        assert verdict["effect_status"] == "not_checked"
        assert verdict["reason_code"] == "manual_review_required"
        assert verdict["contact_status"] == "continuous"

    def test_controller_not_completed_downgrades_effect(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "unknown"}, None,
            {"status": "displaced", "dy_px": -300},
            {"effect": "scroll_displacement", "min_displacement_px": 100})
        assert verdict["effect_status"] == "unknown"

    def test_page_transition_confirmed(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"confirmed": True}, {"effect": "page_transition"})
        assert verdict["effect_status"] == "verified"

    def test_page_transition_unconfirmed_is_unknown(self):
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, None,
            {"confirmed": False}, {"effect": "page_transition"})
        assert verdict["effect_status"] == "unknown"
        assert verdict["reason_code"] == "effect_unknown"

    def test_system_back_cancel_with_page_evidence_can_verify(self):
        """系统返回会终止触摸事件：CANCEL + 明确提交与目标页证据仍可判效果成立。"""
        cancelled = trace([contact("down", 0), contact("cancel", 120)], "cancel")
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"}, cancelled,
            {"confirmed": True}, {"effect": "page_transition"})
        assert verdict["contact_status"] == "cancelled"
        assert verdict["effect_status"] == "verified"

    def test_fixture_trace_uses_contact(self):
        events = [contact("down", 0), contact("up", 300)]
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"},
            trace(events), None, {"effect": "fixture_trace"})
        assert verdict["effect_status"] == "verified"
        broken = trace([contact("down", 0)], "overflow", dropped=3)
        verdict = gv.evaluate_gesture_program(
            {"plan_id": "p"}, {"controller_status": "completed"},
            broken, None, {"effect": "fixture_trace"})
        assert verdict["effect_status"] == "no_effect"

    def test_unknown_verifier_kind_rejected(self):
        with pytest.raises(ValueError):
            gv.evaluate_gesture_program(
                {"plan_id": "p"}, {"controller_status": "completed"}, None, None,
                {"effect": "magic"})
