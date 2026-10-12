"""Recheck phone arming and controller identity after the phone polling delay."""
from __future__ import annotations

import pytest

from drivers import calibration_wizard as cw
from tests.test_calibration_wizard import Env, drive_to_z_ready


@pytest.mark.parametrize("prearmed", [False, True])
@pytest.mark.parametrize("change", [
    "closed", "finished", "disarmed", "sample_changed", "sequence_changed",
    "daemon_changed", "estop",
])
def test_phone_delay_changes_reject_before_motion(tmp_path, monkeypatch, prearmed, change):
    env = Env(tmp_path)
    drive_to_z_ready(env)
    wizard = env.wizard
    sid = wizard.state["imeSessionId"]
    sample_id = "final-phone-gate"
    armed = None
    if prearmed:
        env.ime.arm(sid, sample_id, 1)
        armed = {"sequence": 1, "armedAt": cw.time.time()}

    def change_during_lead(_seconds):
        session = env.ime.sessions[sid]
        if change == "closed":
            session["clientState"] = "closed"
        elif change == "finished":
            session["status"] = "completed"
        elif change == "disarmed":
            session["armed"] = None
        elif change == "sample_changed":
            session["armed"]["sampleId"] = "another-sample"
        elif change == "sequence_changed":
            session["armed"]["sequence"] += 1
        elif change == "daemon_changed":
            env.daemon.session = "new-session"
        elif change == "estop":
            env.daemon.estop_locked = True

    motions = []

    def forbidden_motion():
        motions.append("tap")
        raise AssertionError("Calibration moved after its prerequisites changed")

    monkeypatch.setattr(cw.time, "sleep", change_during_lead)
    with pytest.raises(cw.WizardError):
        wizard._tap_with_report(sample_id, forbidden_motion, pre_armed=armed)
    assert motions == []
    if change == "sample_changed":
        assert env.ime.sessions[sid]["armed"]["sampleId"] == "another-sample"


@pytest.mark.parametrize("prearmed", [False, True])
def test_unchanged_phone_and_controller_allow_one_calibration_touch(
        tmp_path, prearmed):
    env = Env(tmp_path, {"phone_arm_lead_s": 0})
    drive_to_z_ready(env)
    wizard = env.wizard
    sid = wizard.state["imeSessionId"]
    sample_id = "valid-final-phone-gate"
    armed = None
    if prearmed:
        env.ime.arm(sid, sample_id, 1)
        armed = {"sequence": 1, "armedAt": cw.time.time()}
    motions = []

    def report_motion():
        motions.append("tap")
        env.ime.report(sid, sample_id, 500, 900)

    result = wizard._tap_with_report(sample_id, report_motion, pre_armed=armed)
    assert motions == ["tap"]
    assert result["screenX"] == 500
    assert result["screenY"] == 900


def test_phone_moving_during_arm_delay_never_reaches_calibration_press(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    drive_to_z_ready(env)
    wizard = env.wizard
    baseline = [point[:] for point in env.quad["corners"]]
    # 会话锚定（2026-10-01 A3-W）：candidate 必须携带当前 daemon 会话，
    # 否则在姿态门之前就会按 session 变化失效；本测试针对的是姿态漂移门。
    run = {"state": cw.AUTO_STATE_RUNNING, "daemonSession": env.daemon.session,
           "phoneSessionId": wizard.state["imeSessionId"],
           "binding": dict(wizard._read_binding_facts()),
           # S1（第六轮）契约：edge 必须携带与绑定一致的帧尺寸，否则在姿态
           # 门之前就按 binding_frame_mismatch 硬拒。本测试针对姿态漂移门，
           # 补齐字段以到达目标门（绑定一致性由 r6_s1 套件单独断言）。
           "edge": {"corners": baseline,
                    "frameW": env.quad["frameW"], "frameH": env.quad["frameH"]}}
    wizard._auto["run"] = run
    sid = wizard.state["imeSessionId"]
    motions = []

    def move_phone(_seconds):
        env.quad["corners"] = [[x + 7, y] for x, y in baseline]

    monkeypatch.setattr(cw.time, "sleep", move_phone)
    # 测试更正（§4.5，2026-10-03）：姿态门按 2026-09-29 拍板改为尽力而为
    #（暗屏/轮廓不作为强制条件，失败记 pose_gate_skipped 事件并放行）——
    # 手机位移不再在点击前硬拒；采样有效性由触点回报门兜底：位移后点击
    # 拿不到回报 → touch_report_timeout、布防解除，位移触点进不了学习。
    with pytest.raises(cw.TouchReportTimeout):
        wizard._tap_with_report(
            "position-drift-after-arming", lambda: motions.append("press"))
    assert env.ime.sessions[sid]["armed"] is None
    skips = [e for e in run.get("events", [])
             if e.get("event") == "pose_gate_skipped"]
    assert skips, "姿态门放行必须留痕（pose_gate_skipped 事件），不得静默"
