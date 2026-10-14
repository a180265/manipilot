"""R6-S1（执行单§3）：H01 边界两修复+文案+终局重建。

- test_r6_s1_binding_frame_mismatch_hard_rejects —— 帧尺寸/绑定变化=绑定级
  硬拒（binding_frame_mismatch），不得被 H01 宽捕获化成告警；零后续动作。
- test_r6_s1_pose_unverified_wording —— 位移测不到时显示"姿态未核实"，
  类别为"采样姿态未核实提示"，不得称已测超限。
- test_r6_s1_lastRun_restore_from_persistent_candidate —— 新进程实例从最新
  持久 candidate 恢复终局摘要（含 poseWarnings 中文字段；旧记录兼容）。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from drivers import calibration_bundle as cb
from drivers.calibration_auto_runner import AutoRunner
from drivers.calibration_wizard import AutoRunError, CalibrationWizard
from tests.test_auto_calibration_phone_pose import SAVED, wizard_with_edge


def test_r6_s1_binding_frame_mismatch_hard_rejects():
    """合法基准+仅帧尺寸变化：硬拒（AutoRunError），不是 pose_gate_skipped。"""
    wizard = wizard_with_edge(SAVED)
    run = {"edge": {"corners": SAVED, "frameSeq": 1, "frameW": 640, "frameH": 480},
           "binding": {"frameWidth": 999, "frameHeight": 480},
           "events": []}
    with pytest.raises(AutoRunError) as error:
        wizard._auto_runner._auto_require_stable_phone(run)
    assert "binding_frame_mismatch" in str(error.value) or \
        getattr(error.value, "code", "") == "binding_frame_mismatch", str(error.value)
    # 硬拒不是告警：不得产生 pose_gate_skipped（绑定异常不属于 H01 采样姿态）
    assert not [e for e in run["events"]
                if e.get("event") == "pose_gate_skipped"], run["events"]
    assert error.value.park is True
    assert error.value.classification == "camera_measurement_error"


def test_r6_s1_pose_unverified_wording():
    """位移不可测：类别=采样姿态未核实提示；摘要 measuredDisplay=姿态未核实。"""
    from drivers.screen_edge import ScreenEdgeError
    wizard = wizard_with_edge(SAVED)

    def boom():
        raise ScreenEdgeError("edge_detect_failed", "当前帧无法定位手机四角")
    wizard._edge_quad = boom
    run = {"edge": {"corners": SAVED, "frameSeq": 1}, "events": []}
    assert wizard._auto_runner._auto_require_stable_phone(run) is None
    detail = [e for e in run["events"]
              if e.get("event") == "pose_gate_skipped"][-1]["detail"]
    assert detail.get("measuredDisplacementPx") is None
    assert detail.get("categoryDisplay") == "采样姿态未核实提示", detail
    summary = wizard._auto_runner._auto_run_summary(run)
    w = summary["poseWarnings"][-1]
    assert w["measuredDisplay"] == "姿态未核实", w
    # 可测得时仍为超限提示（不因文案修改变含糊）
    wizard2 = wizard_with_edge([[x + 30, y + 5] for x, y in SAVED])
    run2 = {"edge": {"corners": SAVED, "frameSeq": 1}, "events": []}
    wizard2._auto_runner._auto_require_stable_phone(run2)
    d2 = [e for e in run2["events"]
          if e.get("event") == "pose_gate_skipped"][-1]["detail"]
    assert d2.get("categoryDisplay") == "采样姿态超限提示", d2


def _wizard_for_view(tmp_path, device="arm-01"):
    wizard = object.__new__(CalibrationWizard)
    wizard.device = device
    wizard.data_root = Path(tmp_path)
    wizard._lock = threading.RLock()
    wizard._auto_runner = AutoRunner(wizard)
    wizard.state = {}
    return wizard


def test_r6_s1_lastRun_restore_from_persistent_candidate(tmp_path):
    """进程重建：auto_view 从最新持久 candidate 恢复终局摘要+poseWarnings。"""
    wizard = _wizard_for_view(tmp_path)
    run = {"runId": "run-r6-1", "deviceId": "arm-01", "state": "STOPPED",
           "phase": "DONE", "iteration": 1, "createdAt": "2026-10-06T00:00:00Z",
           "events": [
               {"ts": "2026-10-06T00:01:00Z", "phase": "SAMPLE",
                "event": "pose_gate_skipped",
                "detail": {"category": "pose_sampling_warning",
                           "categoryDisplay": "采样姿态超限提示",
                           "stage": "sampling", "stageDisplay": "采样姿态复核",
                           "reason": "采样位移超限",
                           "measuredDisplacementPx": 6.1, "thresholdPx": 4.0}},
               {"ts": "2026-10-06T00:02:00Z", "phase": "SAMPLE",
                "event": "pose_gate_skipped",
                "detail": {"reason": "旧版留痕仅 reason"}}]}
    cb.save_candidate(run, tmp_path)
    view = wizard.auto_view()
    assert view.get("lastRun"), "新实例必须恢复终局摘要"
    warnings = view["lastRun"].get("poseWarnings")
    assert warnings and len(warnings) == 2, warnings
    assert warnings[0]["categoryDisplay"] == "采样姿态超限提示"
    assert warnings[0]["measuredDisplay"] == "6.1px"
    assert warnings[1]["categoryDisplay"] == "未知"      # 旧记录兼容
    assert warnings[1]["measuredDisplay"] == "姿态未核实"
    # 恢复只尝试一次：再次读取不重复加载（lastRun 引用稳定）
    again = wizard.auto_view()
    assert again["lastRun"] is view["lastRun"]


def test_r6_s1_no_candidates_no_restore(tmp_path):
    """无持久 candidate 时保持 IDLE，不抛错。"""
    wizard = _wizard_for_view(tmp_path)
    view = wizard.auto_view()
    assert view.get("state") == "IDLE"
    assert view.get("lastRun") is None
