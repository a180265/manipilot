"""A calibration run cannot combine samples after the phone has moved."""
import pytest

from drivers.calibration_auto_runner import AutoRunner
from drivers.calibration_wizard import AutoRunError, CalibrationWizard, WizardError


SAVED = [[30, 20], [270, 20], [270, 220], [30, 220]]


def wizard_with_edge(live):
    wizard = object.__new__(CalibrationWizard)
    wizard._edge_detector = object()
    wizard._edge_quad = lambda: {"corners": live, "frameSeq": 2}
    wizard._auto_runner = AutoRunner(wizard)  # Auto_CS#8：runner 自治，宿主仅提供 host 面
    return wizard


def test_current_phone_position_allows_same_run():
    wizard = wizard_with_edge([[x + 1, y + 1] for x, y in SAVED])
    run = {"edge": {"corners": SAVED, "frameSeq": 1}}
    wizard._auto_runner._auto_require_stable_phone(run)
    assert run["edge"]["frameSeq"] == 2


@pytest.mark.parametrize("live,tolerated", [
    ([[x + 6, y] for x, y in SAVED], False),   # 四角齐动 6px：采样复核超限
    ([[30, 20], [281, 20], [270, 220], [30, 220]], True),  # 单角 11px：检测噪声（09-29 定案剔角容忍）
])
def test_phone_shift_or_contour_change_h01_semantics(live, tolerated):
    """H01（2026-10-05 用户确认，第四轮入口§3.2；第五轮§9.2 改判）：
    采样姿态复核超限=仅提示+留痕，不硬拒、不停车强制重做。
    安全语义保留：超限时门返回 None（未核实）不作担保；单角噪声按
    2026-09-29 真机定案由鲁棒拟合剔角容忍，返回可用四角。"""
    wizard = wizard_with_edge(live)
    run = {"edge": {"corners": SAVED, "frameSeq": 1}, "events": []}
    result = wizard._auto_runner._auto_require_stable_phone(run, after_travel=True)
    skips = [e for e in run["events"] if e.get("event") == "pose_gate_skipped"]
    if tolerated:
        assert result is not None and result.get("corners"), "单角噪声容忍后返回可用四角"
        assert run["edge"]["frameSeq"] == 2
        assert not skips
    else:
        assert result is None, "超限时不得返回可用四角（未核实不作担保）"
        assert run["edge"]["frameSeq"] == 1, "超限时基准帧不推进"
        assert skips and skips[-1]["detail"].get("reason"), "必须留痕告警"


def test_missing_baseline_aborts_but_unlocatable_live_warns():
    """基准缺失仍硬拒（H01 不放宽独立前置）；采样期定位不到手机改为
    H01 告警+None（R4-08-D：定位无法复核如实提示，不编造也不作担保）。"""
    wizard = wizard_with_edge(SAVED)
    with pytest.raises(AutoRunError):
        wizard._auto_runner._auto_require_stable_phone({"edge": None})
    wizard._edge_quad = lambda: (_ for _ in ()).throw(
        WizardError("screen_edge_not_detected"))
    run = {"edge": {"corners": SAVED, "frameSeq": 1}, "events": []}
    result = wizard._auto_runner._auto_require_stable_phone(run)
    assert result is None
    skips = [e for e in run["events"] if e.get("event") == "pose_gate_skipped"]
    assert skips and "screen_edge_not_detected" in str(skips[-1]["detail"].get("reason"))


def test_stale_stream_cannot_vouch_stability(monkeypatch):
    """H01 改判：陈旧推流→告警+None；安全语义保留=陈旧帧不得为稳定性
    作担保（返回 None、基准帧号不推进）。"""
    wizard = wizard_with_edge(SAVED)
    monkeypatch.setattr("drivers.calibration_wizard.time.sleep", lambda _: None)
    run = {"edge": {"corners": SAVED, "frameSeq": 2}, "events": []}
    result = wizard._auto_runner._auto_require_stable_phone(run, after_travel=True)
    assert result is None, "陈旧流不得返回可用四角"
    assert run["edge"]["frameSeq"] == 2
    skips = [e for e in run["events"] if e.get("event") == "pose_gate_skipped"]
    assert skips and "推流帧没有推进" in str(skips[-1]["detail"].get("reason"))
