"""系统返回（Back/Home）自检：边缘候选、22.13 决策表与恢复语义（规范第 10、22.13 节）。

复用 tests/test_gesture_validation_run.py 的 FakeCoordinator/make_trace 模式；
Back 用页面事件 + verify_page 双通道判定，Home 用桌面模板 verify_page。
"""
from __future__ import annotations

import asyncio

import pytest

from drivers import gesture_program as gprog
from runtime import gesture_validation as gv
from tests.test_gesture_validation_run import (
    FIXTURE_VERSION, FakeCoordinator, _frame, defaults, make_trace,
)
from tests.test_screen_gesture_driver import gesture_bundle

# 显示 1080x2400，root 1080x2320，root 原点 (0,80)：u = 1.0 + 1.66 = 2.66
INSETS = {"left": 20, "top": 0, "right": 20, "bottom": 30}


@pytest.fixture()
def bundle():
    return gesture_bundle()


class EdgeCoordinator(FakeCoordinator):
    """ready 回包携带 systemGestureInsets（第 10.1 节手机回报）。

    insets_placement="page" 为契约形态（Ready additionalProperties:false，
    insets 只在 PageMetadata/ready.page）；"top" 为旧夹具兼容形态。"""

    def __init__(self, insets=None, trace_factory=None, page_events=None,
                 insets_placement="top"):
        super().__init__(trace_factory, page_events)
        payload = {"fixtureVersion": FIXTURE_VERSION}
        if insets is not None:
            if insets_placement == "page":
                payload["page"] = {"systemGestureInsets": dict(insets)}
            else:
                payload["systemGestureInsets"] = dict(insets)
        self.ready_payload = payload


def edge_trace():
    """沿 back_left 首候选的完整 DOWN/MOVE/UP 轨迹（接触连续）。"""
    return make_trace([
        {"index": 0, "contactIndex": 0, "action": "down", "pointerId": 0,
         "tMs": 0, "xPx": 11.33, "yPx": 1120.0, "historical": False},
        {"index": 1, "contactIndex": 0, "action": "move", "pointerId": 0,
         "tMs": 180, "xPx": 119.0, "yPx": 1120.0, "historical": False},
        {"index": 2, "contactIndex": 0, "action": "up", "pointerId": 0,
         "tMs": 370, "xPx": 227.33, "yPx": 1120.0, "historical": False},
    ], end_reason="up")


def make_edge_runner(bundle, defaults, tmp_path, *, insets=INSETS,
                     trace_factory=None, page_events=None, verify_page=None,
                     budget_override=None, run_id="run-edge",
                     insets_placement="top"):
    coordinator = EdgeCoordinator(insets, trace_factory, page_events,
                                  insets_placement=insets_placement)
    executed: list[dict] = []
    runner = gv.GestureValidationRunner(
        defaults=defaults, bundle=bundle, coordinate_frame=_frame(bundle),
        device_id="arm-01", ime_device_id="ime-01", coordinator=coordinator,
        execute_swipe=lambda plan: executed.append(plan) or {
            "controller_status": "completed", "motion_verified": True,
            "ok": True, "position": {"z": -8.0, "state": "Idle"}},
        execute_double_tap=lambda p: {"controller_status": "completed",
                                      "motion_verified": True, "ok": True},
        capture_frame=lambda: {"frame_seq": 1, "png": b"png"},
        verify_page=verify_page, data_root=tmp_path, run_id=run_id,
        budget_override=budget_override)
    return runner, coordinator, executed


def candidates_for(bundle, defaults, kind, insets=INSETS, uncertainty_px=None,
                   frame=None, display=None):
    frame = frame if frame is not None else _frame(bundle)
    if display is None:
        display = (frame["display_width_px"], frame["display_height_px"])
    return gv.edge_swipe_candidates(
        kind=kind, system_gesture_insets=insets,
        display_width_px=display[0], display_height_px=display[1],
        frame=frame,
        uncertainty_px=(gv.calibration_uncertainty_px(bundle)
                        if uncertainty_px is None else uncertainty_px),
        defaults=defaults)


# ---------- 纯函数：边缘候选（第 10.1 节） ----------

class TestEdgeSwipeCandidates:
    def test_back_left_order_and_intersection_center(self, bundle, defaults):
        frame = _frame(bundle)
        u = gv.calibration_uncertainty_px(bundle)
        assert u == pytest.approx(2.66)
        cands = candidates_for(bundle, defaults, "back_left",
                               insets={"left": 20, "top": 0, "right": 0,
                                       "bottom": 0})
        assert [c["id"] for c in cands] == ["BL1", "BL2", "BL3", "BL4",
                                            "BL5", "BL6"]
        # y 顺序 0.5→0.4→0.6 × 长度 0.2→0.3（display 高 2400、root 原点 80）
        ys = [c["start"][1] for c in cands]
        assert ys == pytest.approx([1120, 1120, 880, 880, 1360, 1360])
        # 起点取交集中心：display x∈[0,20] → 校准 [0,20] ∩ [u, root_w-u]
        center = (max(0.0, u) + 20.0) / 2
        assert all(c["start"][0] == pytest.approx(center) for c in cands)
        assert all(c["start"][0] <= 20.0 for c in cands)  # 不退到手势区外
        lengths = [c["end"][0] - c["start"][0] for c in cands]
        assert lengths == pytest.approx([216, 324] * 3)   # display 宽 0.2/0.3
        assert all(c["end"][0] <= frame["root_width_px"] - u for c in cands)
        assert all(c["feed"] == 2250 for c in cands)      # 已验证滑动 F

    def test_back_right_mirrors_left(self, bundle, defaults):
        u = gv.calibration_uncertainty_px(bundle)
        cands = candidates_for(bundle, defaults, "back_right",
                               insets={"left": 0, "top": 0, "right": 20,
                                       "bottom": 0})
        assert [c["id"] for c in cands] == [f"BR{i}" for i in range(1, 7)]
        center = (1060.0 + (1080.0 - u)) / 2  # display [1060,1080] 求交中心
        assert all(c["start"][0] == pytest.approx(center) for c in cands)
        lengths = [c["start"][0] - c["end"][0] for c in cands]
        assert lengths == pytest.approx([216, 324] * 3)

    def test_home_uses_bottom_inset(self, bundle, defaults):
        frame = _frame(bundle)
        u = gv.calibration_uncertainty_px(bundle)
        cands = candidates_for(bundle, defaults, "home")
        assert [c["id"] for c in cands] == [f"HM{i}" for i in range(1, 7)]
        # x 顺序 0.5→0.35→0.65 × 长度 0.18→0.28（display 宽 1080）
        xs = [c["start"][0] for c in cands]
        assert xs == pytest.approx([540, 540, 378, 378, 702, 702])
        center = (2370.0 - 80.0 + (frame["root_height_px"] - u)) / 2
        assert all(c["start"][1] == pytest.approx(center) for c in cands)
        lengths = [c["start"][1] - c["end"][1] for c in cands]
        assert lengths == pytest.approx([432, 672] * 3)   # display 高 0.18/0.28

    @pytest.mark.parametrize("insets", [
        {"left": 0, "top": 0, "right": 0, "bottom": 30},  # 对应 inset 为 0
        None,                                               # 未回报区域
        {"top": 0, "right": 0, "bottom": 30},               # 缺 left 键
    ])
    def test_missing_or_zero_inset_unsupported(self, bundle, defaults, insets):
        with pytest.raises(gprog.GestureProgramError) as exc:
            candidates_for(bundle, defaults, "back_left", insets=insets)
        assert exc.value.reason_code == "navigation_mode_unsupported"

    def test_narrow_intersection_unreachable(self, bundle, defaults):
        with pytest.raises(gprog.GestureProgramError) as exc:
            candidates_for(bundle, defaults, "back_left",
                           insets={"left": 20, "top": 0, "right": 0,
                                   "bottom": 0},
                           uncertainty_px=15.0)  # 交集 [15,20] 宽 5 < 2u+2
        assert exc.value.reason_code == "edge_gesture_unreachable"

    def test_endpoint_out_of_executable_skipped(self, bundle, defaults):
        # display 宽 1000、root 宽 300、u=10：L=0.3 终点越界，仅 L=0.2 可行
        frame = {"root_width_px": 300, "root_height_px": 600,
                 "root_origin_display_px": [0, 0]}
        cands = candidates_for(bundle, defaults, "back_left",
                               insets={"left": 40, "top": 0, "right": 0,
                                       "bottom": 0},
                               uncertainty_px=10.0, frame=frame,
                               display=(1000, 600))
        assert [c["length_frac"] for c in cands] == [0.2, 0.2, 0.2]
        assert all(c["start"][0] == pytest.approx(25.0) for c in cands)
        assert all(c["end"][0] == pytest.approx(225.0) for c in cands)

    def test_cross_axis_candidate_out_of_range_skipped(self, bundle, defaults):
        # display 高 850、root 高 500、u=10：y=0.6→510 越界被跳过
        frame = {"root_width_px": 300, "root_height_px": 500,
                 "root_origin_display_px": [0, 0]}
        cands = candidates_for(bundle, defaults, "back_left",
                               insets={"left": 40, "top": 0, "right": 0,
                                       "bottom": 0},
                               uncertainty_px=10.0, frame=frame,
                               display=(300, 850))
        assert [c["axis_frac"] for c in cands] == [0.5, 0.5, 0.4, 0.4]

    def test_build_edge_program_two_point_swipe(self, bundle, defaults):
        cands = candidates_for(bundle, defaults, "back_left",
                               insets={"left": 20, "top": 0, "right": 0,
                                       "bottom": 0})
        program, meta = gv.build_edge_program(cands[0], bundle, _frame(bundle))
        stroke = program["strokes"][0]["points"]
        assert program["program_version"] == "gesture-program.v1"
        assert stroke[0]["t_ms"] == 0 and stroke[1]["t_ms"] > 0
        assert 150 <= stroke[1]["t_ms"] <= 5000
        plan = gv.plan_scroll_trial(program, bundle, _frame(bundle))
        assert plan["execution_mode"] == "swipe"
        assert meta["candidate"]["id"] == "BL1"


# ---------- Back 自检（第 10.2、22.13 节） ----------

class TestBackValidationRun:
    def test_committed_with_parent_confirm_three_passes(self, bundle, defaults,
                                                        tmp_path):
        page_events = [{"kind": "back_committed", "committed": True,
                        "pageId": "gesture-child",
                        "callbackSource": "on_back_invoked"}]
        kinds: list[str] = []

        def verify(kind):
            kinds.append(kind)
            return {"confirmed": True}

        runner, coordinator, executed = make_edge_runner(
            bundle, defaults, tmp_path, trace_factory=lambda tid: edge_trace(),
            page_events=page_events, verify_page=verify, run_id="run-back-ok")
        result = asyncio.run(runner.run(["system_back_left"]))
        assert result["state"] == "passed"
        action = result["action_results"][0]
        assert action["gesture_id"] == "system_back_left"
        assert action["state"] == "valid"
        assert action["validation_status"] == "three_passes"
        assert len(action["trial_ids"]) == 3
        assert len(executed) == 3                       # 同候选连续 3 次
        assert action["program"] == executed[-1]["program"]
        assert action["program_hash"] == executed[-1]["program_hash"]
        assert kinds == ["back"] * 3
        first = coordinator.prepared[0]
        assert first["kind"] == "prepare_back"
        assert first["payload"] == {"parentId": "gesture-parent",
                                    "childId": "gesture-child"}
        assert first["coordinateFrame"]["coordinate_frame_hash"] == \
            _frame(bundle)["coordinate_frame_hash"]
        assert {state for _tid, state in coordinator.finished_trials} == \
            {"passed"}

    def test_cancelled_only_is_unknown_single_motion(self, bundle, defaults,
                                                     tmp_path):
        page_events = [{"kind": "back_cancelled", "pageId": "gesture-child",
                        "callbackSource": "on_back_animation"}]
        runner, coordinator, executed = make_edge_runner(
            bundle, defaults, tmp_path, trace_factory=lambda tid: edge_trace(),
            page_events=page_events, verify_page=None, run_id="run-back-unk")
        result = asyncio.run(runner.run(["system_back_left"]))
        assert result["state"] == "unknown"            # 不凭取消换候选
        assert len(executed) == 1
        action = result["action_results"][0]
        assert action["state"] == "unverified"
        assert action["reason_code"] == "back_cancelled"

    def test_full_trace_without_commit_moves_to_next_candidate(
            self, bundle, defaults, tmp_path):
        # 完整轨迹 + 无提交 + verify_page confirmed=False → no_effect；
        # 全部候选 no_effect → 动作 unverified，继续下一动作（第 22.10 节）
        runner, coordinator, executed = make_edge_runner(
            bundle, defaults, tmp_path, trace_factory=lambda tid: edge_trace(),
            page_events=[], verify_page=lambda kind: {"confirmed": False},
            run_id="run-back-noeff")
        result = asyncio.run(runner.run(["system_back_left", "system_home"]))
        back, home = result["action_results"]
        assert back["state"] == "unverified"
        assert back["reason_code"] == "effect_not_observed"
        assert home["gesture_id"] == "system_home"     # run 继续下一动作
        assert home["state"] == "unverified"
        assert len(executed) == 12                     # 6 Back + 6 Home 候选
        assert result["budget"]["physical_count"] == 12
        assert result["state"] == "failed"             # 无一 valid

    def test_no_down_is_no_contact_and_stops_run(self, bundle, defaults,
                                                 tmp_path):
        runner, coordinator, executed = make_edge_runner(
            bundle, defaults, tmp_path,
            trace_factory=lambda tid: make_trace([], "timeout"),
            page_events=[], verify_page=lambda kind: {"confirmed": False},
            run_id="run-back-nocontact")
        result = asyncio.run(runner.run(["system_back_left"]))
        assert result["state"] == "failed"
        assert len(executed) == 1
        assert result["action_results"][0]["reason_code"] == "no_contact"

    def test_ready_without_insets_unsupported_no_motion(self, bundle, defaults,
                                                        tmp_path):
        runner, coordinator, executed = make_edge_runner(
            bundle, defaults, tmp_path, insets=None,
            trace_factory=lambda tid: edge_trace(), page_events=[],
            run_id="run-back-noinset")
        result = asyncio.run(runner.run(["system_back_left"]))
        assert result["action_results"][0]["reason_code"] == \
            "navigation_mode_unsupported"
        assert executed == []
        assert result["budget"]["physical_count"] == 0
        assert result["state"] == "failed"


# ---------- Home 自检（第 10.3 节） ----------

class TestHomeValidationRun:
    def test_without_verifier_skipped_no_motion(self, bundle, defaults,
                                                tmp_path):
        runner, coordinator, executed = make_edge_runner(
            bundle, defaults, tmp_path, trace_factory=lambda tid: edge_trace(),
            page_events=[], verify_page=None, run_id="run-home-skip")
        result = asyncio.run(runner.run(["system_home"]))
        action = result["action_results"][0]
        assert action["gesture_id"] == "system_home"
        assert action["state"] == "unverified"
        assert action["reason_code"] == "launcher_template_missing"
        assert executed == []                          # 无物理动作
        assert coordinator.prepared == []
        assert result["budget"]["physical_count"] == 0
        assert result["state"] == "failed"

    def test_confirmed_desktop_three_passes_same_candidate(self, bundle,
                                                           defaults, tmp_path):
        kinds: list[str] = []

        async def verify(kind):                        # 异步验证器同样支持
            kinds.append(kind)
            return {"confirmed": True}

        runner, coordinator, executed = make_edge_runner(
            bundle, defaults, tmp_path, trace_factory=lambda tid: edge_trace(),
            page_events=[], verify_page=verify, run_id="run-home-ok")
        result = asyncio.run(runner.run(["system_home"]))
        assert result["state"] == "passed"
        action = result["action_results"][0]
        assert action["state"] == "valid"
        assert action["validation_status"] == "three_passes"
        assert len(action["trial_ids"]) == 3
        assert len(executed) == 3
        assert kinds == ["home"] * 3
        assert coordinator.prepared[0]["kind"] == "prepare_home"
        # 同一候选（HM1）补足 3 次，不再枚举其他候选
        assert {plan["program_hash"] for plan in executed} == \
            {executed[0]["program_hash"]}


def test_ready_insets_read_from_contract_page_metadata(bundle, defaults, tmp_path):
    """P1-3（2026-09-30 审查）：契约 Ready 为 additionalProperties:false，
    systemGestureInsets 只在 PageMetadata（ready.page）里——runner 必须从
    page 域读取，合规手机不再 navigation_mode_unsupported。"""
    page_events = [{"kind": "back_committed", "committed": True,
                    "pageId": "gesture-child",
                    "callbackSource": "on_back_invoked"}]

    def verify(kind):
        return {"confirmed": True}

    runner, coordinator, executed = make_edge_runner(
        bundle, defaults, tmp_path, trace_factory=lambda tid: edge_trace(),
        page_events=page_events, verify_page=verify,
        run_id="run-edge-page-insets", insets_placement="page")
    result = asyncio.run(runner.run(["system_back_left"]))
    assert result["state"] == "passed", (result["state"], result.get("stop_reason"),
                                         result.get("action_results"))
    assert len(executed) == 3, "back 自检应执行边缘滑动"
