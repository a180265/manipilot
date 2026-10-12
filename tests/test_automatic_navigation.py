"""§17.1：微信列表识别、搜索与回顶（规范第 9、22.14 节）。

合成 ocr_boxes 与合成图覆盖：布局规则、双向找、回顶证据、未知停止、
轮次持久化预算（8 步 / 8 物理动作 / 90 秒 / 2 次探测）、close 语义、
顶部/桌面模板匹配（复用同一函数，min_anchors 参数化）。
"""
from __future__ import annotations

import copy
import json
import uuid

import numpy as np
import pytest

from adapters.uinav import automatic_navigation as an
from drivers.gesture_program import (
    GestureProgramError,
    validate_program,
)

W, H = 720, 1600
DEFAULTS = an.load_defaults()
ROI_N = {"x": 0.05, "y": 0.15, "width": 0.9, "height": 0.6}


def ocr_box(text, x, y, w, h, confidence=0.95):
    return {"text": text, "x": x, "y": y, "w": w, "h": h,
            "confidence": confidence}


def chinese_screen(title="微信", title_confidence=0.97):
    boxes = [ocr_box(title, 320, 100, 80, 52, title_confidence),
             ocr_box("9:41", 40, 40, 60, 30)]
    tabs = [("微信", 60, 60), ("通讯录", 210, 100), ("发现", 420, 60),
            ("我", 600, 40)]
    return boxes + [ocr_box(text, x, 1500, w, 44) for text, x, w in tabs]


def english_screen():
    boxes = [ocr_box("WeChat", 300, 100, 120, 52),
             ocr_box("9:41", 40, 40, 60, 30)]
    tabs = [("WeChat", 60, 90), ("Contacts", 190, 110), ("Discover", 390, 110),
            ("Me", 620, 40)]
    return boxes + [ocr_box(text, x, 1500, w, 44) for text, x, w in tabs]


# ---------- 1. 布局识别（§9.1） ----------

class TestDetectChatListLayout:
    def test_chinese_layout_matched(self):
        result = an.detect_chat_list_layout(chinese_screen(), W, H,
                                            defaults=DEFAULTS)
        assert result["matched"] is True
        assert result["language"] == "zh"
        assert result["title"] == "微信"
        assert result["layout_version"] == "wechat-list-layout.v1"
        # top = 标题 bottom(152) + 0.03*1600(48)；bottom = 1500 - 0.04*1600(64)
        roi = result["roi"]
        assert roi["x"] == pytest.approx(0.05 * W, abs=0.01)
        assert roi["width"] == pytest.approx(0.90 * W, abs=0.01)
        assert roi["y"] == pytest.approx(152 + 0.03 * H, abs=0.01)
        assert roi["height"] == pytest.approx(1500 - 0.04 * H - (152 + 0.03 * H),
                                              abs=0.01)
        assert roi["height"] >= 0.45 * H
        assert [tab["text"] for tab in result["tabs"]] == \
            ["微信", "通讯录", "发现", "我"]
        assert all(tab["confidence"] >= 0.85 for tab in result["tabs"])

    def test_chinese_title_with_unread_suffix(self):
        result = an.detect_chat_list_layout(chinese_screen(title="微信(3)"),
                                            W, H, defaults=DEFAULTS)
        assert result["matched"] is True
        assert result["title"] == "微信"

    def test_english_layout_matched(self):
        result = an.detect_chat_list_layout(english_screen(), W, H,
                                            defaults=DEFAULTS)
        assert result["matched"] is True
        assert result["language"] == "en"
        assert result["title"] == "WeChat"
        assert [tab["text"] for tab in result["tabs"]] == \
            ["WeChat", "Contacts", "Discover", "Me"]

    def test_missing_tab_rejected(self):
        boxes = [box for box in chinese_screen() if box["text"] != "发现"]
        result = an.detect_chat_list_layout(boxes, W, H, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["reason_code"] == "navigation_layout_unsupported"
        assert "发现" in result["reason"]

    def test_low_confidence_tab_rejected(self):
        boxes = chinese_screen()
        for box in boxes:
            if box["text"] == "发现":
                box["confidence"] = 0.8
        result = an.detect_chat_list_layout(boxes, W, H, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["reason_code"] == "navigation_layout_unsupported"

    def test_overlapping_tabs_rejected(self):
        boxes = chinese_screen()
        for box in boxes:
            if box["text"] == "我":
                box["x"] = 460  # 与“发现”[420,480] 重叠
        result = an.detect_chat_list_layout(boxes, W, H, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["reason_code"] == "navigation_layout_unsupported"

    def test_title_missing_rejected(self):
        boxes = [box for box in chinese_screen() if box["y"] >= 200]
        result = an.detect_chat_list_layout(boxes, W, H, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["reason_code"] == "navigation_layout_unsupported"

    def test_title_with_unrelated_suffix_rejected(self):
        # “微信支付”不是带未读后缀的标题，剥掉括号后不等于“微信”。
        result = an.detect_chat_list_layout(chinese_screen(title="微信支付"),
                                            W, H, defaults=DEFAULTS)
        assert result["matched"] is False

    def test_mixed_language_rejected(self):
        boxes = [box for box in english_screen()]
        # 英文标题 + 中文标签：两种语言都无法同时满足标题与四标签。
        result = an.detect_chat_list_layout(boxes, W, H, defaults=DEFAULTS)
        assert result["matched"] is True  # 英文自洽
        mixed = [box for box in english_screen() if box["y"] < 200] + \
            [box for box in chinese_screen() if box["y"] > 1000]
        mixed_result = an.detect_chat_list_layout(mixed, W, H, defaults=DEFAULTS)
        assert mixed_result["matched"] is False
        assert mixed_result["reason_code"] == "navigation_layout_unsupported"

    def test_list_height_below_ratio_rejected_with_custom_defaults(self):
        defaults = copy.deepcopy(DEFAULTS)
        defaults["wechat_list_layout"]["min_list_height_ratio"] = 0.9
        result = an.detect_chat_list_layout(chinese_screen(), W, H,
                                            defaults=defaults)
        assert result["matched"] is False
        assert result["reason_code"] == "navigation_layout_unsupported"


# ---------- 2. 搜索步骤规划（§9.3 / §8.5） ----------

class TestPlanSearchStep:
    def test_search_next_direction(self):
        step = an.plan_search_step("search_next", ROI_N, validated_feed=4.0)
        program = step["program"]
        assert program["program_version"] == "gesture-program.v1"
        assert program["coordinate_space"] == "calibration_root_normalized.v1"
        points = program["strokes"][0]["points"]
        assert points[0]["x"] == pytest.approx(0.5)  # ROI 中心
        assert points[0]["t_ms"] == 0
        assert points[0]["y"] == pytest.approx(0.15 + 0.75 * 0.6)  # 0.60
        assert points[1]["y"] == pytest.approx(0.15 + 0.30 * 0.6)  # 0.33
        assert points[0]["y"] > points[1]["y"]  # 上滑
        validate_program(copy.deepcopy(program))
        assert step["plan"]["direction"] == "up"
        assert step["plan"]["length"] == pytest.approx(0.45 * 0.6)
        # 0.27 / 4.0 * 60000 = 4050ms，在 150-5000 窗口内（浮点 ceil 允许 1ms）。
        assert 4050 <= step["plan"]["duration_ms"] <= 4051
        assert step["plan"]["feed"] == 4.0

    def test_search_previous_direction(self):
        step = an.plan_search_step("search_previous", ROI_N, validated_feed=4.0)
        points = step["program"]["strokes"][0]["points"]
        assert points[0]["y"] == pytest.approx(0.33)
        assert points[1]["y"] == pytest.approx(0.60)
        assert points[1]["y"] > points[0]["y"]  # 下滑反向
        assert step["plan"]["direction"] == "down"

    def test_return_top_uses_downward_swipe(self):
        step = an.plan_search_step("return_top", ROI_N, validated_feed=4.0)
        assert step["plan"]["direction"] == "down"

    def test_duration_clamped_to_minimum(self):
        # 默认 F2250：0.27/2250*60000 ≈ 7ms，clamp 到 150ms 下限。
        step = an.plan_search_step("search_next", ROI_N)
        assert step["plan"]["duration_ms"] == 150

    def test_path_timing_unachievable(self):
        # 0.27/3.0*60000 = 5400ms 超过 5000ms 上限。
        with pytest.raises(GestureProgramError) as exc:
            an.plan_search_step("search_next", ROI_N, validated_feed=3.0)
        assert exc.value.reason_code == "path_timing_unachievable"

    def test_length_multiplier_capped_at_55_percent(self):
        step = an.plan_search_step("search_next", ROI_N, validated_feed=4.0,
                                   length_multiplier=2.0)
        # 0.45*0.6*2.0 = 0.54 超过 0.55*0.6 = 0.33 上限。
        assert step["plan"]["length"] == pytest.approx(0.55 * 0.6)
        points = step["program"]["strokes"][0]["points"]
        assert points[0]["y"] == pytest.approx(0.465 + 0.33 / 2)
        assert points[1]["y"] == pytest.approx(0.465 - 0.33 / 2)
        assert step["plan"]["duration_ms"] == 4950  # 0.33/4*60000

    def test_unknown_action_rejected(self):
        with pytest.raises(ValueError):
            an.plan_search_step("open_chat", ROI_N)


class TestAdjustSearchLength:
    def test_adjust_once_multiplies_1_2(self):
        base = an.plan_search_step("search_next", ROI_N, validated_feed=4.0)
        adjusted = an.adjust_search_length(base, ROI_N, defaults=DEFAULTS)
        assert adjusted is not None
        plan = adjusted["plan"]
        assert plan["adjusted"] is True
        assert plan["length_multiplier"] == pytest.approx(1.2)
        assert plan["length"] == pytest.approx(0.27 * 1.2)  # 0.324 < 0.33 上限
        assert plan["direction"] == "up"
        assert plan["feed"] == 4.0
        assert plan["duration_ms"] == 4860  # 0.324/4*60000
        validate_program(copy.deepcopy(adjusted["program"]))

    def test_adjust_only_once(self):
        base = an.plan_search_step("search_next", ROI_N, validated_feed=4.0)
        adjusted = an.adjust_search_length(base, ROI_N, defaults=DEFAULTS)
        assert adjusted is not None
        assert an.adjust_search_length(adjusted, ROI_N, defaults=DEFAULTS) is None

    def test_adjust_capped_at_55_percent_of_list(self):
        raw = {
            "program_version": "gesture-program.v1",
            "coordinate_space": "calibration_root_normalized.v1",
            "strokes": [{"points": [
                {"x": 0.5, "y": 0.2, "t_ms": 0},
                {"x": 0.5, "y": 0.7, "t_ms": 1000}],
                "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0}],
        }
        adjusted = an.adjust_search_length(raw, ROI_N, defaults=DEFAULTS)
        assert adjusted is not None
        plan = adjusted["plan"]
        assert plan["length"] == pytest.approx(0.55 * 0.6)  # min(0.6, 0.33)
        # 速度按旧程序反推：0.5/1000*60000 = 30；0.33/30*60000 ≈ 660ms。
        assert plan["feed"] == pytest.approx(30.0)
        assert 660 <= plan["duration_ms"] <= 661
        points = adjusted["program"]["strokes"][0]["points"]
        # 方向保持下滑：0.2->0.7 的中点 0.45，长度 0.33 -> 0.285 -> 0.615。
        assert points[0]["y"] == pytest.approx(0.45 - 0.165)
        assert points[1]["y"] == pytest.approx(0.45 + 0.165)
        assert points[1]["y"] > points[0]["y"]


# ---------- 3. 回顶边界探测状态机（§22.14） ----------

class TestBoundaryProbeTransition:
    def test_first_stationary_enters_probe(self):
        result = an.boundary_probe_transition("moving", stationary=True,
                                              probe_count=0)
        assert result == {"state": "stationary_once", "probe_allowed": True,
                          "terminal": False}

    def test_second_stationary_is_terminal(self):
        result = an.boundary_probe_transition("stationary_once", stationary=True,
                                              probe_count=1)
        assert result["state"] == "suspected_top"
        assert result["terminal"] is True
        assert result["probe_allowed"] is False

    def test_valid_scroll_returns_to_moving_without_new_probe(self):
        result = an.boundary_probe_transition("stationary_once", stationary=False,
                                              probe_count=1)
        assert result["state"] == "moving"
        assert result["probe_allowed"] is False  # 消耗的探测数不清零

    def test_moving_stays_moving_before_any_probe(self):
        result = an.boundary_probe_transition("moving", stationary=False,
                                              probe_count=0)
        assert result == {"state": "moving", "probe_allowed": True,
                          "terminal": False}

    def test_stationary_after_consumed_probe_keeps_moving(self):
        # 有效下滑后回到 moving，再静止不得再次进入边界探测。
        result = an.boundary_probe_transition("moving", stationary=True,
                                              probe_count=1)
        assert result == {"state": "moving", "probe_allowed": False,
                          "terminal": False}

    def test_terminal_states_absorb(self):
        for state in ("suspected_top", "stopped"):
            for stationary in (True, False):
                result = an.boundary_probe_transition(state,
                                                      stationary=stationary,
                                                      probe_count=2)
                assert result == {"state": state, "probe_allowed": False,
                                  "terminal": True}

    def test_unknown_state_rejected(self):
        with pytest.raises(ValueError):
            an.boundary_probe_transition("hovering", stationary=True,
                                         probe_count=0)


class TestStepOutcomeForReturnTop:
    @staticmethod
    def _scroll(status, reason_code=None):
        return {"status": status, "dy_px": None, "dx_px": None,
                "reason_code": reason_code}

    def test_first_stationary_is_boundary_probe_pending(self):
        probe = an.boundary_probe_transition("moving", stationary=True,
                                             probe_count=0)
        outcome = an.step_outcome_for_return_top(
            scroll_result=self._scroll("stationary"), probe_state=probe,
            defaults=DEFAULTS)
        # §3.2/§22.14 例外：探测步骤完成 = success，但不表示回顶。
        assert outcome["reason_code"] == "boundary_probe_pending"
        assert outcome["retry_policy"] == "business_boundary_probe"
        assert outcome["effect_status"] == "no_effect"
        assert outcome["top_verified"] is False
        assert outcome["plugin_outcome"] == "success"

    def test_second_stationary_is_top_not_confirmed(self):
        probe = an.boundary_probe_transition("stationary_once", stationary=True,
                                             probe_count=1)
        outcome = an.step_outcome_for_return_top(
            scroll_result=self._scroll("stationary"), probe_state=probe,
            defaults=DEFAULTS)
        assert outcome["reason_code"] == "top_not_confirmed"
        assert outcome["retry_policy"] == "none"
        assert outcome["plugin_outcome"] == "unknown"
        assert outcome["top_verified"] is False

    def test_stationary_after_probe_budget_exhausted(self):
        probe = an.boundary_probe_transition("moving", stationary=True,
                                             probe_count=1)
        outcome = an.step_outcome_for_return_top(
            scroll_result=self._scroll("stationary"), probe_state=probe,
            defaults=DEFAULTS)
        assert outcome["reason_code"] == "top_not_confirmed"
        assert outcome["plugin_outcome"] == "unknown"

    def test_displaced_is_verified_but_not_top(self):
        probe = an.boundary_probe_transition("moving", stationary=False,
                                             probe_count=0)
        outcome = an.step_outcome_for_return_top(
            scroll_result=self._scroll("displaced"), probe_state=probe,
            defaults=DEFAULTS)
        assert outcome["effect_status"] == "verified"
        assert outcome["top_verified"] is False
        assert outcome["plugin_outcome"] == "success"
        assert outcome["reason_code"] is None

    def test_unknown_scrolls_to_unknown_outcome(self):
        probe = an.boundary_probe_transition("moving", stationary=False,
                                             probe_count=0)
        outcome = an.step_outcome_for_return_top(
            scroll_result=self._scroll("unknown", "insufficient_features"),
            probe_state=probe, defaults=DEFAULTS)
        assert outcome["plugin_outcome"] == "unknown"
        assert outcome["reason_code"] == "insufficient_features"
        assert outcome["top_verified"] is False


# ---------- 4. 轮次管理（§22.14 全轮预算持久化） ----------

PROFILE = {"profile_id": "wx_chat_list_auto", "version": 1,
           "contract_version": "automatic-gesture-profile.v1"}
BINDING = {"device_id": "arm-fixture-01", "daemon_session": "fixture-session",
           "camera_id": "cam-fixture-01", "binding_revision": 1}
RESULT_OK = {"plugin_outcome": "success", "controller_status": "completed",
             "effect_status": "verified", "reason_code": None,
             "target_found": False, "top_verified": False}
RESULT_FOUND = dict(RESULT_OK, target_found=True)
RESULT_TOP = dict(RESULT_OK, top_verified=True)
RESULT_UNKNOWN = {"plugin_outcome": "unknown", "controller_status": "unknown",
                  "effect_status": "unknown", "reason_code": "controller_unknown",
                  "target_found": False, "top_verified": False}
RESULT_PROBE = dict(RESULT_OK, reason_code="boundary_probe_pending",
                    effect_status="no_effect")


@pytest.fixture()
def manager(tmp_path):
    return an.NavigationRunManager("arm-fixture-01", data_root=tmp_path)


def new_navigation_id():
    return str(uuid.uuid4())


def start(manager, navigation_id=None, *, action="search_next", now_ms=1000,
          target=None, session_ref=None):
    return manager.start_run(
        navigation_id=navigation_id or new_navigation_id(),
        profile_ref=PROFILE, binding=BINDING, action=action, target=target,
        session_ref=session_ref, now_ms=now_ms)


class TestNavigationRunManager:
    def test_start_run_persists_full_record(self, manager, tmp_path):
        navigation_id = new_navigation_id()
        run = start(manager, navigation_id, now_ms=1000)
        assert run["state"] == "active"
        assert run["navigation_id"] == navigation_id
        assert run["device_id"] == "arm-fixture-01"
        assert run["profile_ref"] == PROFILE
        assert run["binding"] == BINDING
        assert run["action"] == "search_next"
        assert run["target"] is None and run["session_ref"] is None
        assert run["started_at"] == 1000
        assert run["deadline_at"] == 1000 + 90000  # navigation_limits.timeout_ms
        assert run["last_step_index"] == 0
        assert run["steps"] == []
        assert run["physical_count"] == 0
        assert run["boundary_probe_count"] == 0
        # 原子写后可读回（§14.2 持久性）。
        path = (tmp_path / "devices" / "arm-fixture-01" / "navigation_runs"
                / f"{navigation_id}.json")
        assert json.loads(path.read_text(encoding="utf-8"))["navigation_id"] \
            == navigation_id
        assert manager.run_view(navigation_id)["state"] == "active"

    def test_run_view_missing_returns_none(self, manager):
        assert manager.run_view(new_navigation_id()) is None

    def test_invalid_navigation_id_rejected(self, manager):
        with pytest.raises(an.NavigationRunError):
            start(manager, "not-an-uuid")

    def test_session_ref_rules(self, manager):
        with pytest.raises(an.NavigationRunError):
            start(manager, action="back_to_list")  # 缺 session_ref
        with pytest.raises(an.NavigationRunError):
            start(manager, action="search_next",
                  session_ref={"session_id": "s1"})  # 其他动作禁止
        run = start(manager, action="back_to_list",
                    session_ref={"session_id": "s1"})
        assert run["action"] == "back_to_list"

    def test_first_step_must_be_one(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        with pytest.raises(an.NavigationStepConflict) as exc:
            manager.record_step(navigation_id, step_index=2,
                                request_hash="h", task_id="t",
                                submitted_physical=True, result_ref=RESULT_OK,
                                now_ms=2000)
        assert exc.value.reason_code == "navigation_step_conflict"
        with pytest.raises(an.NavigationStepConflict):
            manager.record_step(navigation_id, step_index=0,
                                request_hash="h", task_id="t",
                                submitted_physical=True, result_ref=RESULT_OK,
                                now_ms=2000)

    def test_steps_must_be_strictly_sequential(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        manager.record_step(navigation_id, step_index=1, request_hash="h1",
                            task_id="t1", submitted_physical=True,
                            result_ref=RESULT_OK, now_ms=2000)
        with pytest.raises(an.NavigationStepConflict):
            manager.record_step(navigation_id, step_index=3, request_hash="h3",
                                task_id="t3", submitted_physical=True,
                                result_ref=RESULT_OK, now_ms=3000)
        record = manager.record_step(navigation_id, step_index=2,
                                     request_hash="h2", task_id="t2",
                                     submitted_physical=True,
                                     result_ref=RESULT_OK, now_ms=3000)
        assert record["run_state"] == "active"
        assert manager.run_view(navigation_id)["last_step_index"] == 2

    def test_idempotent_step_resubmission(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        first = manager.record_step(navigation_id, step_index=1,
                                    request_hash="h1", task_id="t1",
                                    submitted_physical=True,
                                    result_ref=RESULT_OK, now_ms=2000)
        again = manager.record_step(navigation_id, step_index=1,
                                    request_hash="h1", task_id="t1",
                                    submitted_physical=True,
                                    result_ref=RESULT_OK, now_ms=2500)
        assert again["idempotent"] is True
        assert again["result_ref"] == RESULT_OK
        run = manager.run_view(navigation_id)
        assert len(run["steps"]) == 1
        assert run["physical_count"] == 1  # 不产生新动作
        assert run["state"] == "active"

    def test_same_step_different_task_or_hash_rejected(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        manager.record_step(navigation_id, step_index=1, request_hash="h1",
                            task_id="t1", submitted_physical=True,
                            result_ref=RESULT_OK, now_ms=2000)
        with pytest.raises(an.NavigationStepConflict):
            manager.record_step(navigation_id, step_index=1, request_hash="h1",
                                task_id="t-other", submitted_physical=True,
                                result_ref=RESULT_OK, now_ms=2100)
        with pytest.raises(an.NavigationStepConflict):
            manager.record_step(navigation_id, step_index=1,
                                request_hash="h-other", task_id="t1",
                                submitted_physical=True, result_ref=RESULT_OK,
                                now_ms=2200)

    def test_step_budget_exhausted_at_eight_steps(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id, now_ms=0)
        for index in range(1, 8):
            record = manager.record_step(navigation_id, step_index=index,
                                         request_hash=f"h{index}",
                                         task_id=f"t{index}",
                                         submitted_physical=True,
                                         result_ref=RESULT_OK,
                                         now_ms=index * 1000)
            assert record["run_state"] == "active"
        record = manager.record_step(navigation_id, step_index=8,
                                     request_hash="h8", task_id="t8",
                                     submitted_physical=True,
                                     result_ref=RESULT_OK, now_ms=8000)
        assert record["run_state"] == "failed"
        assert record["state_reason_code"] == "gesture_budget_exhausted"
        run = manager.run_view(navigation_id)
        assert run["physical_count"] == 8
        assert run["state"] == "failed"
        with pytest.raises(an.NavigationStepConflict):
            manager.record_step(navigation_id, step_index=9, request_hash="h9",
                                task_id="t9", submitted_physical=True,
                                result_ref=RESULT_OK, now_ms=9000)

    def test_navigation_limits_parameterized(self, tmp_path):
        defaults = copy.deepcopy(DEFAULTS)
        defaults["navigation_limits"]["max_steps"] = 2
        manager = an.NavigationRunManager("arm-limits", data_root=tmp_path,
                                          defaults=defaults)
        navigation_id = new_navigation_id()
        manager.start_run(navigation_id=navigation_id, profile_ref=PROFILE,
                          binding=BINDING, action="search_next", now_ms=0)
        manager.record_step(navigation_id, step_index=1, request_hash="h1",
                            task_id="t1", submitted_physical=True,
                            result_ref=RESULT_OK, now_ms=1000)
        record = manager.record_step(navigation_id, step_index=2,
                                     request_hash="h2", task_id="t2",
                                     submitted_physical=True,
                                     result_ref=RESULT_OK, now_ms=2000)
        assert record["run_state"] == "failed"

    def test_timeout_marks_run_failed(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id, now_ms=0)
        record = manager.record_step(navigation_id, step_index=1,
                                     request_hash="h1", task_id="t1",
                                     submitted_physical=True,
                                     result_ref=RESULT_OK, now_ms=89999)
        assert record["run_state"] == "active"
        record = manager.record_step(navigation_id, step_index=2,
                                     request_hash="h2", task_id="t2",
                                     submitted_physical=True,
                                     result_ref=RESULT_OK, now_ms=90000)
        assert record["run_state"] == "failed"
        assert record["state_reason_code"] == "gesture_budget_exhausted"

    def test_in_flight_step_blocks_next_and_close(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        record = manager.record_step(navigation_id, step_index=1,
                                     request_hash="h1", task_id="t1",
                                     submitted_physical=True,
                                     result_ref=None, now_ms=2000)
        assert record["run_state"] == "active"
        run = manager.run_view(navigation_id)
        assert run["active_task_id"] == "t1"
        with pytest.raises(an.NavigationStepConflict):
            manager.record_step(navigation_id, step_index=2, request_hash="h2",
                                task_id="t2", submitted_physical=True,
                                result_ref=RESULT_OK, now_ms=3000)
        with pytest.raises(an.NavigationStepConflict):
            manager.close_run(navigation_id, "caller_finished", now_ms=4000)

    def test_in_flight_step_completion_allows_next(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        manager.record_step(navigation_id, step_index=1, request_hash="h1",
                            task_id="t1", submitted_physical=True,
                            result_ref=None, now_ms=2000)
        record = manager.record_step(navigation_id, step_index=1,
                                     request_hash="h1", task_id="t1",
                                     submitted_physical=True,
                                     result_ref=RESULT_OK, now_ms=2600)
        assert record["idempotent"] is False
        run = manager.run_view(navigation_id)
        assert run["active_task_id"] is None
        assert run["physical_count"] == 1  # 完成不重复计数
        record = manager.record_step(navigation_id, step_index=2,
                                     request_hash="h2", task_id="t2",
                                     submitted_physical=True,
                                     result_ref=RESULT_OK, now_ms=3000)
        assert record["run_state"] == "active"

    def test_unknown_step_keeps_run_unknown(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        record = manager.record_step(navigation_id, step_index=1,
                                     request_hash="h1", task_id="t1",
                                     submitted_physical=True,
                                     result_ref=RESULT_UNKNOWN, now_ms=2000)
        assert record["run_state"] == "unknown"
        summary = manager.close_run(navigation_id, "caller_finished",
                                    now_ms=3000)
        assert summary["state"] == "unknown"  # close 不清除未知
        # 未知轮按原设备未知保护阻止新轮。
        with pytest.raises(an.NavigationRunError):
            start(manager, now_ms=4000)
        # 终态（unknown）轮不能再记录步骤。
        with pytest.raises(an.NavigationStepConflict):
            manager.record_step(navigation_id, step_index=2,
                                request_hash="h2", task_id="t2",
                                submitted_physical=True, result_ref=RESULT_OK,
                                now_ms=5000)

    def test_target_found_completes_run(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id, target={"visible_name": "张三"})
        record = manager.record_step(navigation_id, step_index=1,
                                     request_hash="h1", task_id="t1",
                                     submitted_physical=False,
                                     result_ref=RESULT_FOUND, now_ms=2000)
        assert record["run_state"] == "completed"

    def test_top_verified_completes_return_top_run(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id, action="return_top")
        record = manager.record_step(navigation_id, step_index=1,
                                     request_hash="h1", task_id="t1",
                                     submitted_physical=True,
                                     result_ref=RESULT_TOP, now_ms=2000)
        assert record["run_state"] == "completed"

    def test_boundary_probe_pending_stays_active_and_counts(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id, action="return_top")
        record = manager.record_step(navigation_id, step_index=1,
                                     request_hash="h1", task_id="t1",
                                     submitted_physical=True,
                                     result_ref=RESULT_PROBE, now_ms=2000)
        assert record["run_state"] == "active"  # 例外：success 但轮次未终态
        run = manager.run_view(navigation_id)
        assert run["boundary_probe_count"] == 1

    def test_close_caller_finished_marks_completed(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        manager.record_step(navigation_id, step_index=1, request_hash="h1",
                            task_id="t1", submitted_physical=True,
                            result_ref=RESULT_OK, now_ms=2000)
        summary = manager.close_run(navigation_id, "caller_finished",
                                    now_ms=3000)
        assert set(summary) == {"navigation_id", "device_id", "state",
                                "reason_code", "last_step_index",
                                "physical_count"}
        assert summary["state"] == "completed"
        assert summary["reason_code"] is None  # 不补造 target_found
        assert summary["last_step_index"] == 1
        assert summary["physical_count"] == 1
        # 重复 close 返回原轮摘要。
        assert manager.close_run(navigation_id, "caller_finished",
                                 now_ms=4000) == summary

    def test_close_caller_cancelled_marks_failed(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        summary = manager.close_run(navigation_id, "caller_cancelled",
                                    now_ms=3000)
        assert summary["state"] == "failed"
        assert summary["reason_code"] == "caller_cancelled"

    def test_close_invalid_reason_rejected(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        with pytest.raises(ValueError):
            manager.close_run(navigation_id, "target_found", now_ms=3000)

    def test_close_terminal_run_returns_original_summary(self, manager):
        navigation_id = new_navigation_id()
        start(manager, navigation_id, now_ms=0)
        for index in range(1, 9):
            manager.record_step(navigation_id, step_index=index,
                                request_hash=f"h{index}",
                                task_id=f"t{index}", submitted_physical=True,
                                result_ref=RESULT_OK, now_ms=index * 1000)
        summary = manager.close_run(navigation_id, "caller_finished",
                                    now_ms=20000)
        assert summary["state"] == "failed"
        assert summary["reason_code"] == "gesture_budget_exhausted"

    def test_single_active_run_per_device(self, manager):
        first = start(manager)
        with pytest.raises(an.NavigationRunError) as exc:
            start(manager)
        assert exc.value.reason_code == "navigation_run_conflict"
        manager.close_run(first["navigation_id"], "caller_finished",
                          now_ms=5000)
        second = start(manager, now_ms=6000)
        assert second["state"] == "active"

    def test_same_navigation_id_idempotent_and_conflict(self, manager):
        navigation_id = new_navigation_id()
        run = start(manager, navigation_id)
        assert start(manager, navigation_id)["started_at"] == run["started_at"]
        with pytest.raises(an.NavigationRunError):
            start(manager, navigation_id, action="search_previous")

    def test_records_visible_across_manager_instances(self, manager, tmp_path):
        navigation_id = new_navigation_id()
        start(manager, navigation_id)
        manager.record_step(navigation_id, step_index=1, request_hash="h1",
                            task_id="t1", submitted_physical=True,
                            result_ref=RESULT_OK, now_ms=2000)
        other = an.NavigationRunManager("arm-fixture-01", data_root=tmp_path)
        run = other.run_view(navigation_id)
        assert run is not None
        assert run["last_step_index"] == 1
        record = other.record_step(navigation_id, step_index=2,
                                   request_hash="h2", task_id="t2",
                                   submitted_physical=True,
                                   result_ref=RESULT_OK, now_ms=3000)
        assert record["run_state"] == "active"
        assert manager.run_view(navigation_id)["last_step_index"] == 2

    def test_record_step_missing_run_rejected(self, manager):
        with pytest.raises(an.NavigationRunError):
            manager.record_step(new_navigation_id(), step_index=1,
                                request_hash="h", task_id="t",
                                submitted_physical=True, result_ref=RESULT_OK,
                                now_ms=1000)


# ---------- 5. 顶部/桌面模板匹配（§9.4 / §22.8 / §22.15） ----------

FRAME_W, FRAME_H = 320, 640


def draw_blocks(size, blocks, offset=(0, 0), seed=11):
    """纯灰度背景 + 各锚点独立噪声块；同 seed 同块内容一致（可平移复现）。"""
    rng = np.random.default_rng(seed)
    frame = (60 + np.linspace(0, 8, size[1])[:, None]).astype(np.uint8) \
        .repeat(size[0], axis=1)
    for index, (x, y, w, h) in enumerate(blocks):
        block_rng = np.random.default_rng(seed * 1000 + index)
        block = block_rng.integers(0, 255, size=(h, w), dtype=np.uint8)
        dx, dy = offset
        frame[y + dy:y + dy + h, x + dx:x + dx + w] = block
    return frame


def registered_anchors(frame, blocks):
    return [{"left_px": x, "top_px": y, "width_px": w, "height_px": h,
             "template": frame[y:y + h, x:x + w].copy()}
            for x, y, w, h in blocks]


ANCHOR_BLOCKS = [(30, 80, 40, 40), (200, 140, 36, 50), (120, 300, 48, 36)]


class TestVerifyTopTemplate:
    def test_matching_shifted_frame(self):
        base = draw_blocks((FRAME_W, FRAME_H), ANCHOR_BLOCKS)
        anchors = registered_anchors(base, ANCHOR_BLOCKS)
        current = draw_blocks((FRAME_W, FRAME_H), ANCHOR_BLOCKS,
                              offset=(3, 5))
        result = an.verify_top_template(anchors, current, defaults=DEFAULTS)
        assert result["matched"] is True
        assert result["matched_anchors"] == 3
        for detail in result["anchors"]:
            assert detail["passed"] is True
            assert detail["correlation"] >= 0.92
            assert abs(detail["dx_px"]) <= 0.02 * FRAME_W
            assert abs(detail["dy_px"]) <= 0.02 * FRAME_H

    def test_launcher_reuse_requires_three_anchors(self):
        base = draw_blocks((FRAME_W, FRAME_H), ANCHOR_BLOCKS)
        anchors = registered_anchors(base, ANCHOR_BLOCKS)
        current = draw_blocks((FRAME_W, FRAME_H), ANCHOR_BLOCKS,
                              offset=(2, 4))
        assert an.verify_top_template(anchors, current, defaults=DEFAULTS,
                                      min_anchors=3)["matched"] is True
        # 只提供一个好锚点时，launcher 门槛（3）不满足。
        single = an.verify_top_template(anchors[:1], current,
                                        defaults=DEFAULTS, min_anchors=3)
        assert single["matched"] is False

    def test_large_offset_rejected(self):
        base = draw_blocks((FRAME_W, FRAME_H), ANCHOR_BLOCKS)
        anchors = registered_anchors(base, ANCHOR_BLOCKS)
        # 偏移 40px 超出 0.03*屏幕 搜索窗口（约 9/19px）。
        current = draw_blocks((FRAME_W, FRAME_H), ANCHOR_BLOCKS,
                              offset=(40, 40))
        result = an.verify_top_template(anchors, current, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["reason_code"] == "top_not_confirmed"

    def test_low_correlation_rejected(self):
        base = draw_blocks((FRAME_W, FRAME_H), ANCHOR_BLOCKS, seed=11)
        anchors = registered_anchors(base, ANCHOR_BLOCKS)
        different = draw_blocks((FRAME_W, FRAME_H), ANCHOR_BLOCKS, seed=77)
        result = an.verify_top_template(anchors, different, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["reason_code"] == "top_not_confirmed"
        assert all(detail["correlation"] is None
                   or detail["correlation"] < 0.92
                   for detail in result["anchors"])

    def test_small_anchor_rejected(self):
        blocks = [(50, 100, 12, 12)]  # < 24px
        base = draw_blocks((FRAME_W, FRAME_H), blocks)
        anchors = registered_anchors(base, blocks)
        current = draw_blocks((FRAME_W, FRAME_H), blocks)
        result = an.verify_top_template(anchors, current, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["anchors"][0]["reason"] == "anchor_too_small"

    def test_flat_anchor_rejected(self):
        frame = np.full((FRAME_H, FRAME_W), 128, dtype=np.uint8)
        flat = np.full((40, 40), 128, dtype=np.uint8)  # std = 0
        anchors = [{"left_px": 50, "top_px": 100, "width_px": 40,
                    "height_px": 40, "template": flat}]
        result = an.verify_top_template(anchors, frame, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["anchors"][0]["reason"] == "anchor_low_contrast"

    def test_ambiguous_duplicate_rejected(self):
        # 高帧（margin_y = 0.03*1280 ≈ 38px）：登记块下方 24px 处复制同一噪声块，
        # 两个候选都在搜索窗口内且互不重叠 -> 最佳与次优差 < 0.05，拒绝。
        w, h = 320, 1280
        blocks = [(100, 500, 24, 24)]

        def duplicated(frame):
            frame[524:548, 100:124] = frame[500:524, 100:124]
            return frame

        base = duplicated(draw_blocks((w, h), blocks))
        anchors = registered_anchors(base, blocks)
        current = duplicated(draw_blocks((w, h), blocks))
        result = an.verify_top_template(anchors, current, defaults=DEFAULTS)
        assert result["matched"] is False
        assert result["anchors"][0]["reason"] == "ambiguous_second_candidate"

    def test_empty_anchors_rejected(self):
        frame = np.full((FRAME_H, FRAME_W), 128, dtype=np.uint8)
        result = an.verify_top_template([], frame, defaults=DEFAULTS)
        assert result["matched"] is False
