"""R4-08-D/E 验证：采样姿态超限按 H01 仅告警+留痕（不中断/不作废/不强制
重做）；无法取得结论记录"姿态未核实"；普通动作接触门不因 H01 削弱。

阶段阈值表见 POSE-POLICY.md。基线来源：第四轮修前快照同代码行为 +
_pose_gate_skip 现有留痕通道（事件+Phase2/触点回报门不变）。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from tests.test_auto_calibration_phone_pose import SAVED, wizard_with_edge


def test_r4_08_d_overshift_warns_and_continues_run():
    """有合法初始基准，注入采样位移超限：仅告警（pose_gate_skipped），
    run 不中断、edge 基准推进（样本不作废）、无强制人工门。"""
    wizard = wizard_with_edge([[x + 30, y + 5] for x, y in SAVED])  # 超 4px
    run = {"edge": {"corners": SAVED, "frameSeq": 1}, "events": []}
    wizard._auto_runner._auto_require_stable_phone(run)  # H01：不得抛 AutoRunError
    # 仅告警：样本不作废（edge 原样保留）、run 不中断、无强制人工门
    assert run["edge"]["corners"] == SAVED and run["edge"]["frameSeq"] == 1
    assert not any(e.get("event") == "operator_confirm_required"
                   for e in run["events"]), run["events"]
    skips = [e for e in run["events"] if e.get("event") == "pose_gate_skipped"]
    assert skips, "姿态门放行必须留痕（pose_gate_skipped 事件）"


def test_r4_08_d2_overshift_warning_records_measurable_details():
    """告警/事件携带可测位移与阈值；无法测得的值不得编造（None 保留）。"""
    wizard = wizard_with_edge([[x + 30, y + 5] for x, y in SAVED])
    run = {"edge": {"corners": SAVED, "frameSeq": 1}, "events": []}
    wizard._auto_runner._auto_require_stable_phone(run)
    skips = [e for e in run["events"] if e.get("event") == "pose_gate_skipped"]
    assert skips, "必须有告警留痕"
    detail = skips[-1].get("detail") or {}
    assert detail.get("reason"), detail  # 可测得的 reason 必须记录


def test_r4_08_e2_ordinary_action_gate_unaffected():
    """普通动作接触门（2px）不受 H01 削弱：位移>2px 的 live 快照仍拒绝。"""
    from drivers.screen_pose import fit_pose_robust
    base = [[100.0, 100.0], [300.0, 100.0], [300.0, 300.0], [100.0, 300.0]]
    moved = [[100.0 + 30.0, 100.0], [300.0 + 30.0, 100.0],
             [300.0 + 30.0, 300.0], [100.0 + 30.0, 300.0]]
    with pytest.raises(ValueError):
        fit_pose_robust(base, moved, max_movement=2.0)


def test_r4_08_d3_warning_structured_fields_and_summary():
    """P4（第五轮§9.2）：告警带分类/阶段/可测位移/阈值；run 结束摘要含
    poseWarnings 中文展示；旧记录（仅 reason）兼容显示未知不编造。"""
    wizard = wizard_with_edge([[x + 30, y + 5] for x, y in SAVED])
    run = {"edge": {"corners": SAVED, "frameSeq": 1}, "events": []}
    wizard._auto_runner._auto_require_stable_phone(run)
    skips = [e for e in run["events"] if e.get("event") == "pose_gate_skipped"]
    assert skips, "必须有告警留痕"
    detail = skips[-1]["detail"]
    assert detail.get("category") == "pose_sampling_warning"
    assert detail.get("stage") in ("sampling", "post_travel")
    assert detail.get("stageDisplay"), detail
    # 位移可测得（基准 vs 当前四角最大欧氏距离 ≈30.4px）且阈值 4px
    assert isinstance(detail.get("measuredDisplacementPx"), float), detail
    assert 29.0 < detail["measuredDisplacementPx"] < 32.0, detail
    assert detail.get("thresholdPx") == 4.0
    # 结束摘要（_auto_run_summary）携带 poseWarnings，进行中/结束后同源
    summary = wizard._auto_runner._auto_run_summary(run)
    ws = summary.get("poseWarnings")
    assert ws, "结束摘要必须含姿态告警"
    w = ws[-1]
    assert w["categoryDisplay"] == "采样姿态超限提示"
    assert w["measuredPx"] == detail["measuredDisplacementPx"]
    assert w["measuredDisplay"].endswith("px") and w["thresholdDisplay"] == "4.0px"


def test_r4_08_d4_unmeasurable_warning_keeps_null():
    """无法测得位移（当前四角拿不到）时明确空值，不编造。"""
    from drivers.screen_edge import ScreenEdgeError
    wizard = wizard_with_edge(SAVED)

    def boom():
        raise ScreenEdgeError("edge_detect_failed", "当前帧无法定位手机四角")
    wizard._edge_quad = boom
    run = {"edge": {"corners": SAVED, "frameSeq": 1}, "events": []}
    wizard._auto_runner._auto_require_stable_phone(run)
    skips = [e for e in run["events"] if e.get("event") == "pose_gate_skipped"]
    assert skips, run["events"]
    detail = skips[-1]["detail"]
    assert detail.get("measuredDisplacementPx") is None, detail
    assert detail.get("thresholdPx") == 4.0  # 阈值仍可给出
    summary = wizard._auto_runner._auto_run_summary(run)
    assert summary["poseWarnings"][-1]["measuredDisplay"] == "姿态未核实"  # S1/L42 文案


def test_r4_08_d5_legacy_events_compat_display():
    """旧记录（第四轮：事件仅 reason）兼容：未知/未测得，不编造。"""
    wizard = wizard_with_edge(SAVED)
    run = {"events": [{"ts": "2026-10-05T00:00:00Z", "phase": "SAMPLE",
                       "event": "pose_gate_skipped", "detail": {"reason": "旧版留痕"}}]}
    summary = wizard._auto_runner._auto_run_summary(run)
    w = summary["poseWarnings"][-1]
    assert w["reason"] == "旧版留痕"
    assert w["categoryDisplay"] == "未知" and w["stageDisplay"] == "未知"
    assert w["measuredDisplay"] == "姿态未核实" and w["thresholdDisplay"] == "未知"  # S1/L42
