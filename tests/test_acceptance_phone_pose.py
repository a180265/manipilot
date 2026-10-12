"""Acceptance phone-position gates using fake devices and temporary files only."""
from copy import deepcopy
from itertools import repeat
import os
import socket
import subprocess
import urllib.request

import cv2
import pytest
import serial

from drivers import calibration_bundle as cb, calibration_wizard as cw, target_archive, ui_targets
from tests.test_calibration_wizard import (
    CAM_H, CAM_W, SCREEN_H, SCREEN_W, Env, FakeOcr, FakeOcrWord,
)


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Acceptance tests must not access hardware or services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(cv2, "VideoCapture", forbidden)
    monkeypatch.setattr(cw.time, "sleep", lambda _: None)


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path)
    state = env.wizard.state
    state.update(stage=cw.STAGE_DEBUG_ACCEPTANCE, imeDeviceId="acceptance-test",
                 daemonSession=env.daemon.session)
    state["screen"] = {
        "orientation": "portrait", "screenWidth": SCREEN_W, "screenHeight": SCREEN_H,
    }
    state["geometry"].update(
        edge={**deepcopy(env.quad), "frameSeq": 0},
        homography={
            "homography": [
                [SCREEN_W / CAM_W, 0, 0],
                [0, SCREEN_H / CAM_H, 0],
                [0, 0, 1],
            ],
            "frameW": CAM_W, "frameH": CAM_H,
        },
    )
    state["xy"]["fit"] = {
        "a": 10.0, "b": 0, "c": 100.0,
        "d": 0, "e": 8.0, "f": 200.0,
    }
    state["z"].update(
        hoverZ=5.0,
        tap={"depth": 3.0, "feed": 1500.0, "dwellMs": 0},
        delete={"depth": 3.0, "feed": 1500.0, "holdMs": 2200},
    )
    env.wizard._ocr_engine = FakeOcr([
        FakeOcrWord("统一校准测试", 440, 366),
    ])
    return env


@pytest.fixture
def candidate(env):
    run = {
        "runId": "acceptance-phone-pose", "deviceId": "arm-01",
        "phase": "E_ACCEPTANCE", "state": cw.AUTO_STATE_RUNNING,
        "edge": deepcopy(env.wizard.state["geometry"]["edge"]),
        "h": {"homography": deepcopy(
            env.wizard.state["geometry"]["homography"]["homography"])},
        "arm": {"robotToScreen": deepcopy(env.wizard.state["xy"]["fit"])},
        "binding": {"frameWidth": CAM_W, "frameHeight": CAM_H},
        "z": {"zSafe": 5.0}, "acceptance": {}, "samples": [],
    }
    env.wizard._auto["run"] = run
    return run


def test_uncompensatable_phone_blocks_even_pre_ocr_parking(env):
    env.wizard.state["safePark"] = {
        "confirmed": True, "parkX": 100.0, "parkY": 50.0, "parkZ": 5.0,
    }
    env.quad["corners"] = [[x + 20, y] for x, y in env.quad["corners"]]

    with pytest.raises((cw.WizardError, cw.AutoRunError), match="手机"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert env.daemon.cmds == []
    assert env.daemon.tap_count == 0
    assert env.ime.tasks_created == []


@pytest.mark.parametrize("target", ["manual_input", "manual_delete", "legacy_delete", "ui_delete"])
def test_static_acceptance_target_follows_small_phone_shift_once(env, target):
    baseline = deepcopy(env.wizard.state["geometry"])
    fit = deepcopy(env.wizard.state["xy"]["fit"])
    env.quad["corners"] = [[x + 6, y + 2] for x, y in env.quad["corners"]]
    check = "ime_input_tap" if target == "manual_input" else "ime_delete_clear"
    coordinates = {"nx": 0.5, "ny": 0.7}
    if target == "legacy_delete":
        target_archive.save_target(
            "arm-01", "ime_delete_key", {"nx": 0.5, "ny": 0.7}, env.wizard.data_root,
        )
        coordinates = {}
    elif target == "ui_delete":
        cb.save_bundle(cb.new_bundle("arm-01"), env.wizard.data_root)
        ui_targets.register_draft(
            "arm-01", "ime_delete_key", nx=0.5, ny=0.7,
            data_root=env.wizard.data_root, registration_window={"source": "manual"},
        )
        coordinates = {}

    result = env.wizard.acceptance_execute("debug", check, **coordinates)

    assert result["executed"] is True
    assert (env.daemon.x, env.daemon.y) == pytest.approx((44.675, 186.111), abs=0.002)
    assert env.daemon.tap_count == 1
    assert env.wizard.state["geometry"] == baseline
    assert env.wizard.state["xy"]["fit"] == fit


def test_live_ocr_camera_coordinate_is_not_compensated_twice(env):
    env.quad["corners"] = [[x + 6, y + 2] for x, y in env.quad["corners"]]
    env.wizard._ocr_engine = FakeOcr([
        FakeOcrWord("统一校准测试", 446, 368),
    ])

    result = env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert result["targetSource"] == "camera_ocr"
    assert result["ocrTarget"]["cameraX"] == 486
    assert result["ocrTarget"]["cameraY"] == 380
    assert (env.daemon.x, env.daemon.y) == pytest.approx((44.675, 186.111), abs=0.002)
    assert env.daemon.tap_count == 1


def test_phone_move_while_waiting_for_ime_cancels_without_target_travel(env, monkeypatch):
    wait = env.ime.wait_task_displayed

    def moved(*args, **kwargs):
        result = wait(*args, **kwargs)
        env.quad["corners"] = [[x + 6, y] for x, y in env.quad["corners"]]
        return result

    monkeypatch.setattr(env.ime, "wait_task_displayed", moved)

    with pytest.raises((cw.WizardError, cw.AutoRunError), match="手机"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert not any(cmd.startswith(("XY ", "Z ", "TAP ")) for cmd in env.daemon.cmds)
    assert env.ime.cancelled_tasks == [env.ime.tasks_created[0]["taskId"]]
    assert not env.wizard.state["acceptance"]["debug"]["ime_input_tap"].get("executedAt")


def test_reused_detector_corner_list_cannot_move_acceptance_reference(env, monkeypatch):
    wait = env.ime.wait_task_displayed

    def moved_in_place(*args, **kwargs):
        result = wait(*args, **kwargs)
        for corner in env.quad["corners"]:
            corner[0] += 6
        return result

    monkeypatch.setattr(env.ime, "wait_task_displayed", moved_in_place)
    with pytest.raises(cw.WizardError, match="手机"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert not any(cmd.startswith(("Z ", "XY ", "TAP ")) for cmd in env.daemon.cmds)
    assert env.ime.cancelled_tasks == [env.ime.tasks_created[0]["taskId"]]


@pytest.mark.parametrize("boundary", [
    "park_lease", "park_position", "target_position", "after_z", "after_xy",
])
def test_acceptance_rechecks_phone_at_each_motion_boundary(env, monkeypatch, boundary):
    if boundary.startswith("park"):
        env.wizard.state["safePark"] = {
            "confirmed": True, "parkX": 100.0, "parkY": 50.0, "parkZ": 5.0,
        }
    if boundary in {"target_position", "after_z"}:
        env.daemon.z = 0.0

    def shift():
        env.quad["corners"] = [[x + 6, y] for x, y in env.quad["corners"]]

    if boundary == "park_lease":
        original = env.daemon.ensure_debug_lease

        def lease():
            original()
            shift()

        monkeypatch.setattr(env.daemon, "ensure_debug_lease", lease)
    elif boundary.endswith("position"):
        original = env.daemon.mpos
        reads = []

        def position():
            result = original()
            if not reads:
                shift()
            reads.append(True)
            return result

        monkeypatch.setattr(env.daemon, "mpos", position)
    else:
        original = env.daemon.cmd

        def moved(line):
            result = original(line)
            if line.startswith("Z " if boundary == "after_z" else "XY "):
                shift()
            return result

        monkeypatch.setattr(env.daemon, "cmd", moved)

    with pytest.raises((cw.WizardError, cw.AutoRunError), match="手机"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert env.daemon.tap_count == 0
    if boundary == "after_z":
        assert any(cmd.startswith("Z ") for cmd in env.daemon.cmds)
        assert not any(cmd.startswith("XY ") for cmd in env.daemon.cmds)
    elif boundary == "after_xy":
        assert any(cmd.startswith("XY ") for cmd in env.daemon.cmds)
    else:
        assert not any(cmd.startswith(("XY ", "Z ")) for cmd in env.daemon.cmds)
    if env.ime.tasks_created:
        assert env.ime.cancelled_tasks == [env.ime.tasks_created[0]["taskId"]]


@pytest.mark.parametrize("issue", ["wrong_size", "old_frame", "invalid_sequence"])
def test_live_ocr_must_match_frame_contract_before_target_motion(env, monkeypatch, issue):
    recognize = env.wizard._ocr_engine.recognize

    def invalid_ocr(*args, **kwargs):
        result = recognize(*args, **kwargs)
        if issue == "wrong_size":
            result.width = CAM_W * 2
        return result

    monkeypatch.setattr(env.wizard._ocr_engine, "recognize", invalid_ocr)
    if issue == "old_frame":
        env.frame_seq = iter([1, 2, 2, 3, 4, 5, 6])
    elif issue == "invalid_sequence":
        env.frame_seq = iter([1, 2, -1, 3, 4, 5, 6])

    with pytest.raises(cw.WizardError, match="帧"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert not any(cmd.startswith(("Z ", "XY ", "TAP ")) for cmd in env.daemon.cmds)
    assert env.ime.cancelled_tasks == [env.ime.tasks_created[0]["taskId"]]


@pytest.mark.parametrize("change", ["ended", "replaced"])
def test_auto_acceptance_cannot_switch_run_while_waiting(env, candidate, monkeypatch, change):
    wait = env.ime.wait_task_displayed

    def changed_run(*args, **kwargs):
        result = wait(*args, **kwargs)
        env.wizard._auto["run"] = None if change == "ended" else deepcopy(candidate)
        return result

    monkeypatch.setattr(env.ime, "wait_task_displayed", changed_run)
    with pytest.raises((cw.WizardError, cw.AutoRunError), match="运行"):
        env.wizard.auto_acceptance_execute(candidate["runId"], "ime_input_tap")

    assert not any(cmd.startswith(("Z ", "XY ", "TAP ")) for cmd in env.daemon.cmds)
    assert env.ime.cancelled_tasks == [env.ime.tasks_created[0]["taskId"]]


@pytest.mark.parametrize("issue", [
    "no_camera", "no_detector", "no_baseline", "no_phone", "bad_corners",
    "wrong_frame_size", "wrong_baseline_size", "frozen", "scale", "nonrigid",
])
def test_unlocatable_phone_fails_closed_before_parking(env, issue):
    env.wizard.state["safePark"] = {
        "confirmed": True, "parkX": 100.0, "parkY": 50.0, "parkZ": 5.0,
    }
    if issue == "no_camera":
        env.wizard._camera_fetch = None
    elif issue == "no_detector":
        env.wizard._edge_detector = None
    elif issue == "no_baseline":
        env.wizard.state["geometry"]["edge"] = None
    elif issue == "no_phone":
        env.wizard._edge_detector = lambda _: None
    elif issue == "bad_corners":
        env.quad["corners"] = [[float("nan"), 5]] * 4
    elif issue == "wrong_frame_size":
        env.quad["frameW"] = CAM_W * 2
    elif issue == "wrong_baseline_size":
        env.wizard.state["geometry"]["edge"]["frameW"] = CAM_W * 2
    elif issue == "frozen":
        env.frame_seq = repeat(0)
    elif issue == "scale":
        env.quad["corners"] = [[480 + (x - 480) * 1.02, 270 + (y - 270) * 1.02]
                               for x, y in env.quad["corners"]]
    elif issue == "nonrigid":
        env.quad["corners"][1][0] += 12

    with pytest.raises((cw.WizardError, cw.AutoRunError), match="手机"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert env.daemon.cmds == []
    assert env.ime.tasks_created == []


def test_frozen_stream_after_ocr_never_reuses_ocr_frame_as_motion_permission(
        env, monkeypatch):
    recognize = env.wizard._ocr_engine.recognize

    def freeze(*args, **kwargs):
        env.frame_seq = repeat(3)
        return recognize(*args, **kwargs)

    monkeypatch.setattr(env.wizard._ocr_engine, "recognize", freeze)
    with pytest.raises(cw.WizardError, match="帧没有推进"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert not any(cmd.startswith(("Z ", "XY ", "TAP ")) for cmd in env.daemon.cmds)
    assert env.ime.cancelled_tasks == [env.ime.tasks_created[0]["taskId"]]


def test_auto_candidate_does_not_accept_manual_compensation_range(env, candidate):
    preserved = deepcopy({key: candidate[key] for key in ("h", "arm", "samples")})
    baseline = deepcopy(candidate["edge"])
    env.quad["corners"] = [[x + 6, y] for x, y in env.quad["corners"]]
    with pytest.raises(cw.AutoRunError) as error:
        env.wizard.auto_acceptance_execute(candidate["runId"], "ime_input_tap")

    assert error.value.code == "camera_measurement_error"
    assert error.value.park is False
    assert {key: candidate[key] for key in preserved} == preserved
    assert candidate["edge"] == baseline
    assert env.daemon.cmds == [] and env.ime.tasks_created == []


def test_auto_acceptance_uses_candidate_pose_and_keeps_model_and_baseline(env, candidate):
    env.wizard.state["geometry"]["edge"]["corners"] = [[0, 0]] * 4
    env.wizard.state["xy"]["fit"]["c"] = 900.0
    preserved = deepcopy({key: candidate[key] for key in ("h", "arm", "samples")})
    corners = deepcopy(candidate["edge"]["corners"])

    result = env.wizard.auto_acceptance_execute(candidate["runId"], "ime_input_tap")

    assert result["executed"]
    assert (env.daemon.x, env.daemon.y) == pytest.approx((44, 185), abs=0.002)
    assert env.daemon.tap_count == 1
    assert {key: candidate[key] for key in preserved} == preserved
    assert candidate["edge"]["corners"] == corners
    assert candidate["edge"]["frameSeq"] > 0
