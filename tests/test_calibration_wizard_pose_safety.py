"""Calibration pose regressions with bound memory devices and fresh fake frames."""
from copy import deepcopy
import os
import socket
import subprocess
import urllib.request

import cv2
import numpy as np
import pytest
import serial

from drivers import calibration_bundle as cb, calibration_wizard as cw
from tests.test_calibration_phone_gate_review import protocol_rig, prepare, touch
from tests.test_calibration_wizard import Env, SCREEN_H, SCREEN_W, auto_report


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Pose regressions forbid hardware, network and processes")

    for name in ("connect", "connect_ex", "bind", "listen"):
        original = getattr(socket.socket, name)

        def isolated(sock, *args, _original=original, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                forbidden()
            return _original(sock, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, isolated)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(cv2, "VideoCapture", forbidden)


@pytest.fixture
def bound_env(tmp_path, monkeypatch):
    env = Env(tmp_path, {"phone_arm_lead_s": 0})
    sid = env.ime.create_session("pose-safety-phone")["sessionId"]
    env.ime.client(sid, "open")
    env.ime.phone_lag = 0
    env.wizard.state.update(
        stage=cw.STAGE_Z_READY, imeSessionId=sid, imeDeviceId="pose-safety-phone",
        daemonSession=env.daemon.session,
        binding=deepcopy(env.wizard._read_binding_facts()),
        screen={"orientation": "portrait",
                "screenWidth": SCREEN_W, "screenHeight": SCREEN_H},
    )
    env.wizard.state["geometry"]["edge"] = {**deepcopy(env.quad), "frameSeq": 0}
    auto_report(env.ime, env.daemon)
    monkeypatch.setattr(cw.time, "sleep", lambda _seconds: None)
    return env


def manual_touch(env, kind):
    if kind == "confirm":
        return env.wizard.z_confirm_safe(tap_depth=3, tap_feed=1500)
    return env.wizard.z_tap_test(
        kind, depth=3, feed=1500, dwell_ms=2200 if kind == "delete" else 0)


def shift_phone(env, distance=6):
    env.quad["corners"] = [[x + distance, y] for x, y in env.quad["corners"]]


@pytest.mark.parametrize("kind", ("tap", "delete", "confirm"))
def test_bound_manual_z_rejects_moved_phone_before_contact(bound_env, kind):
    env = bound_env
    shift_phone(env, 20)
    with pytest.raises((cw.WizardError, cw.AutoRunError), match="手机"):
        manual_touch(env, kind)
    assert env.daemon.tap_count == 0
    assert env.daemon.cmds == []


@pytest.mark.parametrize("kind", ("tap", "delete", "confirm"))
def test_bound_manual_z_accepts_one_stable_contact(bound_env, kind):
    env = bound_env
    result = manual_touch(env, kind)
    assert env.daemon.tap_count == 1
    if kind == "confirm":
        assert result["validated"] is True
        assert env.wizard.state["z"]["autoSafe"]["report"]["screenWidth"] == SCREEN_W
    else:
        assert result["trial"]["ok"] is True


@pytest.mark.parametrize("fault", ("unlocatable", "bright_contour_anomaly"))
@pytest.mark.parametrize("after_contact", (False, True), ids=("before-contact", "after-report"))
def test_confirm_with_anchor_never_adds_hidden_wake_contacts(
        bound_env, monkeypatch, fault, after_contact):
    """The confirmation's repeated pose checks must not consume its touch arming."""
    env = bound_env
    env.wizard.state["z"]["tapAnchor"] = {"daemonSession": env.daemon.session}
    locate = env.wizard._edge_quad

    def observed_quad(**kwargs):
        if after_contact and env.daemon.tap_count == 0:
            return locate(**kwargs)
        if fault == "unlocatable":
            raise cw.PoseUnlocatable("screen_edge_not_detected：轮廓不可见")
        quad = deepcopy(locate(**kwargs))
        quad["corners"][1][0] += 40  # Bright frame, non-rigid contour error, not darkness.
        return quad

    monkeypatch.setattr(env.wizard, "_edge_quad", observed_quad)
    result = env.wizard.z_confirm_safe(tap_depth=3, tap_feed=1500)
    assert env.daemon.tap_count == 1
    assert result["validated"] is True
    session = env.ime.get_session(env.wizard.state["imeSessionId"])
    assert len(session["samples"]) == 1
    assert session["samples"][0]["sampleId"] == result["report"]["sampleId"]
    assert session["armed"] is None


def test_readonly_pose_check_with_anchor_never_wakes_on_bright_contour_error(bound_env):
    env = bound_env
    env.wizard.state["z"]["tapAnchor"] = {"daemonSession": env.daemon.session}
    env.quad["corners"][1][0] += 40
    env.wizard._manual_z_phone_pose()
    assert env.daemon.tap_count == 0
    assert env.daemon.cmds == []


@pytest.mark.parametrize("brightness", (0, 255), ids=("dark", "bright"))
def test_region_localization_failure_has_no_hidden_contact(bound_env, monkeypatch, brightness):
    env = bound_env
    width, height = env.quad["frameW"], env.quad["frameH"]
    _, jpg = cv2.imencode(".jpg", np.full((height, width, 3), brightness, dtype=np.uint8))
    monkeypatch.setattr(env.wizard, "_camera_fetch", lambda: (jpg.tobytes(), next(env.frame_seq)))
    monkeypatch.setattr(env.wizard, "_search_region_provider", lambda: {
        "source_size": {"width": width, "height": height}})

    def no_contour(*_args, **_kwargs):
        raise cw.ScreenEdgeError("轮廓异常")

    monkeypatch.setattr(env.wizard, "_gradient_edge_quad", no_contour)
    monkeypatch.setattr("drivers.screen_edge.locate_screen_quad", no_contour)
    monkeypatch.setattr("runtime.phone_region_context.locate_quad_in_region", no_contour)
    with pytest.raises(cw.WizardError, match="若手机暗屏，请先点亮"):
        env.wizard._edge_quad()
    assert env.daemon.tap_count == 0
    assert env.daemon.cmds == []


@pytest.mark.parametrize("kind", ("tap", "delete", "confirm"))
def test_manual_z_without_binding_never_contacts(bound_env, kind):
    env = bound_env
    env.wizard.state["binding"] = None
    with pytest.raises(cw.WizardError, match="绑定"):
        manual_touch(env, kind)
    assert env.daemon.tap_count == 0
    assert env.daemon.cmds == []


@pytest.mark.parametrize("kind", ("tap", "delete", "confirm"))
@pytest.mark.parametrize("fault", ("unlocatable", "frozen", "dimensions", "nonrigid"))
def test_manual_z_requires_fresh_formal_four_corner_evidence(bound_env, kind, fault):
    env = bound_env
    if fault == "unlocatable":
        env.wizard.state["geometry"]["edge"] = None
        env.wizard._edge_detector = lambda _jpg: None
    elif fault == "frozen":
        env.wizard._camera_fetch = lambda: (b"jpg", 0)
    elif fault == "dimensions":
        env.quad["frameW"] += 1
    else:
        env.quad["corners"][1][0] += 20
    if fault == "unlocatable" and kind == "confirm":
        # 用户拍板 2026-09-29（固定产品决定，见
        # test_calibration_wizard.py::test_z_confirm_pose_reference_absent_
        # does_not_block_operator_tap）：操作员监督下的 zSafe 确认点击不被
        # "姿态定位不可得"拦截——缺席留痕（poseReference），以触点回报裁决。
        # 容忍只覆盖"定位不到"（PoseUnlocatable 族）；frozen/dimensions/
        # nonrigid 属质量门失败，confirm 也照常拒绝（下方分支）。
        result = manual_touch(env, kind)
        record = env.wizard.state["z"]["autoSafe"]
        assert result["validated"] is True
        assert "姿态参考缺席" in (record.get("poseReference") or "")
        assert env.daemon.tap_count == 1
        return
    with pytest.raises((cw.WizardError, cw.AutoRunError)):
        manual_touch(env, kind)
    assert env.daemon.tap_count == 0
    assert env.daemon.cmds == []


@pytest.mark.parametrize("kind", ("tap", "delete", "confirm"))
def test_manual_z_rechecks_after_lease_wait(bound_env, kind):
    env = bound_env
    acquire = env.daemon.ensure_debug_lease

    def wait_for_lease():
        shift_phone(env)
        return acquire()

    env.daemon.ensure_debug_lease = wait_for_lease
    with pytest.raises(cw.WizardError, match="手机位置变化"):
        manual_touch(env, kind)
    assert env.daemon.tap_count == 0
    assert not any(command.startswith(("TAP", "Z ", "XY ")) for command in env.daemon.cmds)


@pytest.mark.parametrize("kind", ("tap", "confirm"))
def test_manual_z_rechecks_after_phone_arming_wait(bound_env, monkeypatch, kind):
    env = bound_env
    env.wizard._phone_arm_lead_s = .25
    monkeypatch.setattr(cw.time, "sleep", lambda _seconds: shift_phone(env))
    with pytest.raises(cw.WizardError, match="手机位置变化"):
        manual_touch(env, kind)
    assert env.daemon.tap_count == 0
    assert env.ime.sessions[env.wizard.state["imeSessionId"]]["armed"] is None


@pytest.mark.parametrize("kind", ("tap", "delete", "confirm"))
def test_manual_z_cannot_confirm_touch_after_phone_moves(bound_env, kind):
    env = bound_env
    report = env.daemon.on_tap

    def shifted_report(line):
        shift_phone(env)
        report(line)

    env.daemon.on_tap = shifted_report
    with pytest.raises(cw.WizardError, match="手机位置变化"):
        manual_touch(env, kind)
    assert env.daemon.tap_count == 1
    assert not any(trial["ok"] for trial in env.wizard.state["z"]["trials"])
    assert not (env.wizard.state["z"].get("autoSafe") or {}).get("validated")
    assert env.wizard.state["z"].get("tapAnchor") is None


def test_failed_reconfirmation_does_not_inherit_previous_confirmation(bound_env):
    env = bound_env
    assert manual_touch(env, "confirm")["validated"] is True
    shift_phone(env)
    with pytest.raises(cw.WizardError, match="手机位置变化"):
        manual_touch(env, "confirm")
    assert env.daemon.tap_count == 1
    assert env.wizard.state["z"]["autoSafe"]["validated"] is False


@pytest.mark.parametrize("kind", ("tap", "delete"))
def test_manual_z_save_does_not_inherit_touch_from_old_phone_pose(bound_env, kind):
    env = bound_env
    env.wizard.capture_hover_z()
    manual_touch(env, kind)
    shift_phone(env)
    with pytest.raises(cw.WizardError, match="手机位置变化"):
        env.wizard.z_save(kind)
    assert env.wizard.state["z"].get(kind) is None
    assert env.daemon.tap_count == 1


@pytest.mark.parametrize("kind", ("tap", "delete"))
def test_manual_z_save_accepts_stable_phone_pose(bound_env, kind):
    env = bound_env
    env.wizard.capture_hover_z()
    manual_touch(env, kind)
    saved = env.wizard.z_save(kind)
    assert saved["saved"] == kind
    assert env.wizard.state["z"][kind] is not None
    assert env.daemon.tap_count == 1


@pytest.mark.parametrize("kind", ("tap", "delete"))
def test_manual_z_save_rejects_legacy_touch_without_pose_evidence(bound_env, kind):
    env = bound_env
    env.wizard.capture_hover_z()
    manual_touch(env, kind)
    env.wizard.state["z"].pop("phonePose")
    with pytest.raises(cw.WizardError, match="姿态基准"):
        env.wizard.z_save(kind)
    assert env.wizard.state["z"].get(kind) is None
    assert env.daemon.tap_count == 1


def automatic_run(env):
    run = {
        "runId": "pose-safety-auto", "state": cw.AUTO_STATE_RUNNING,
        "deviceId": env.wizard.device, "daemonSession": env.daemon.session,
        "phoneSessionId": env.wizard.state["imeSessionId"],
        "screen": deepcopy(env.wizard.state["screen"]),
        "binding": deepcopy(env.wizard.state["binding"]),
        "runtime": {"profileFingerprint": "pose-safety"}, "modelVersion": 1,
        "arm": {"robotToScreen": {
            "a": 10.0, "b": 0.0, "c": 100.0,
            "d": 0.0, "e": 8.0, "f": 200.0,
        }},
        "z": {"zSafe": 5.0, "tapDepth": 3.0, "tapFeed": 1500.0},
        "config": deepcopy(env.wizard._auto_cfg),
        "locks": {"cameraToScreen": {"state": cb.MODEL_LOCK_LOCKED}},
        "samples": [], "edge": deepcopy(env.wizard.state["geometry"]["edge"]),
    }
    env.wizard._auto_runner._auto["run"] = run
    env.wizard._target_detector = env.green_from_armed()
    return run


@pytest.mark.parametrize("fault", ("detector_missing", "malformed", "frozen", "unbound"))
def test_automatic_precheck_requires_pose_and_binding_before_first_motion(bound_env, fault):
    env = bound_env
    run = automatic_run(env)
    if fault == "detector_missing":
        env.wizard._edge_detector = None
    elif fault == "malformed":
        env.quad["corners"] = [[10, 5]] * 4
    elif fault == "frozen":
        env.wizard._camera_fetch = lambda: (b"jpg", 0)
    else:
        env.wizard._binding_facts = None
        env.wizard.state["binding"] = None
        run["binding"] = {}
    with pytest.raises(cw.AutoRunError) as error:
        env.wizard._auto_runner._auto_phase_precheck(run)
    assert error.value.park is False
    assert env.daemon.cmds == []


def test_automatic_precheck_accepts_bound_fresh_formal_phone_pose(bound_env):
    env = bound_env
    run = automatic_run(env)
    env.wizard._auto_runner._auto_phase_precheck(run)
    assert run["edge"]["frameSeq"] > 0
    assert run["edge"]["corners"] == env.quad["corners"]
    assert env.daemon.cmds == []


@pytest.mark.parametrize("anchor_matches", (False, True))
def test_auto_precheck_never_wakes_at_current_position_when_localization_fails(
        bound_env, monkeypatch, anchor_matches):
    env = bound_env
    run = automatic_run(env)
    env.daemon.x, env.daemon.y = -100, -100  # A historic touch anchor is not current XY.
    env.wizard.state["z"]["tapAnchor"] = {
        "daemonSession": env.daemon.session if anchor_matches else "old-session",
        "robotX": 0, "robotY": 0,
    }

    def unavailable(**_kwargs):
        raise cw.PoseUnlocatable("screen_edge_not_detected：轮廓不可见")

    monkeypatch.setattr(env.wizard, "_edge_quad", unavailable)
    with pytest.raises(cw.AutoRunError) as error:
        env.wizard._auto_runner._auto_phase_precheck(run)
    assert error.value.park is False
    assert env.daemon.tap_count == 0
    assert env.daemon.cmds == []


@pytest.mark.parametrize("moved", (False, True), ids=("stable", "moved"))
def test_phase1_real_sampling_does_not_learn_or_replay_changed_pose(bound_env, moved):
    env = bound_env
    run = automatic_run(env)
    previous_model = deepcopy(run["arm"])
    report = env.daemon.on_tap

    def shifted_report(line):
        if moved:
            shift_phone(env)
        report(line)

    env.daemon.on_tap = shifted_report
    point = {"id": "pose-safety-center", "u": .5, "v": .5}
    if moved:
        with pytest.raises(cw.AutoRunError) as error:
            env.wizard._auto_runner._auto_phase1_point(run, point)
        assert error.value.code == "camera_measurement_error"
        assert len(run["samples"]) == 1
        assert run["samples"][0]["eligible"] is False
        with pytest.raises(cw.AutoRunError) as learning:
            env.wizard._auto_runner._auto_update_arm_candidate(run)
        assert learning.value.code == "arm_samples_insufficient"
        assert run["arm"] == previous_model
    else:
        sample = env.wizard._auto_runner._auto_phase1_point(run, point)
        assert sample["eligible"] is True
        assert sample["actualTouch"] == pytest.approx([540, 1200])
    assert env.daemon.tap_count == 1
    assert len(run["samples"]) == 1


def test_automatic_sample_cannot_rebase_reference_while_reporting(bound_env):
    env = bound_env
    run = automatic_run(env)
    report = env.daemon.on_tap

    def replace_reference(line):
        shift_phone(env, 20)
        run["edge"] = {**deepcopy(env.quad), "frameSeq": 0}
        report(line)

    env.daemon.on_tap = replace_reference
    with pytest.raises((cw.WizardError, cw.AutoRunError)):
        env.wizard._auto_runner._auto_click_point(
            run, "replaced-reference", .5, .5, "PHASE1_ARM", use_camera=False)
    assert env.daemon.tap_count == 1
    assert len(run["samples"]) == 1
    assert run["samples"][0]["eligible"] is False


@pytest.mark.parametrize("fault", (
    "corners_mutated", "run_replaced", "run_stopped", "binding_changed",
    "phone_session_changed", "daemon_session_changed",
))
def test_automatic_report_rechecks_run_reference_and_binding(bound_env, fault):
    env = bound_env
    run = automatic_run(env)
    report = env.daemon.on_tap

    def change_during_report(line):
        if fault == "corners_mutated":
            shift_phone(env, 20)
            for saved, live in zip(run["edge"]["corners"], env.quad["corners"]):
                saved[:] = live
        elif fault == "run_replaced":
            env.wizard._auto_runner._auto["run"] = {**run}
        elif fault == "run_stopped":
            run["state"] = cw.AUTO_STATE_STOPPED
        elif fault == "binding_changed":
            run["binding"]["bindingRevision"] += 1
        elif fault == "phone_session_changed":
            env.wizard.state["imeSessionId"] = "replacement-session"
        else:
            env.daemon.session = "replacement-controller"
        report(line)

    env.daemon.on_tap = change_during_report
    with pytest.raises((cw.WizardError, cw.AutoRunError)):
        env.wizard._auto_runner._auto_click_point(
            run, "report-identity", .5, .5, "PHASE1_ARM", use_camera=False)
    assert env.daemon.tap_count == 1
    assert len(run["samples"]) == 1
    assert run["samples"][0]["eligible"] is False


@pytest.mark.parametrize("prearmed", (False, True))
@pytest.mark.parametrize("with_target", (False, True))
def test_complete_auto_run_accepts_one_real_protocol_touch(
        protocol_rig, monkeypatch, prearmed, with_target):
    rig = protocol_rig
    wizard = rig.wizard
    binding = wizard._read_binding_facts()
    wizard.state["binding"] = deepcopy(binding)
    wizard._auto_runner._auto["run"] = {
        "runId": "bound-protocol-auto", "state": cw.AUTO_STATE_RUNNING,
        "phoneSessionId": rig.sid, "daemonSession": rig.daemon.session,
        "binding": deepcopy(binding), "edge": wizard._edge_quad(),
    }
    target = {"u": .5, "v": .5, "widthU": .1, "heightV": .1} if with_target else None
    sample_id, pre = prepare(rig, prearmed, target)
    monkeypatch.setattr(cw.time, "sleep", lambda _seconds: None)
    motions = []

    def report_motion():
        motions.append("TAP")
        touch(rig, sample_id)

    result = wizard._tap_with_report(
        sample_id, report_motion, pre_armed=pre, target=target)
    assert motions == ["TAP"]
    assert (result["screenX"], result["screenY"]) == (500, 900)
    session = rig.ime.get_session(rig.sid)
    assert session["armed"] is None
    assert len(session["samples"]) == 1
    assert session["samples"][0].get("target") == target
