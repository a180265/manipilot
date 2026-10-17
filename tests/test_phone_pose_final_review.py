"""Independent pose review: memory transports, synthetic frames and temp files."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import socket
import subprocess
import threading
import urllib.request

import cv2
import numpy as np
import pytest
import serial

from drivers import calibration_bundle as cb, calibration_wizard as cw
from drivers import screen_gestures as sg, screen_pose
from drivers.arm import DriverError
from drivers.daemon_arm import DaemonArmDriver
from runtime import calibration_gate
from tests.test_calibration_wizard import Env, SCREEN_H, SCREEN_W, auto_report
from tests.test_daemon_arm import (
    CALIB_SESSION, PATH1_FEATURE, FakeDaemon, _path1_plan, enable_ui_target,
)
from tests.test_live_calibration_gate import FakeCamera, ready_bundle
from tests.test_screen_gesture_driver import gesture_bundle, point
from tests.test_screen_wake_driver import validated_bundle, wake_frame


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise RuntimeError("Final pose review forbids hardware, network and processes")

    for method in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, method, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(cv2, "VideoCapture", forbidden)
    monkeypatch.setattr(calibration_gate, "EDGE_CONSENSUS_INTERVAL_S", 0)
    monkeypatch.setattr(cw.time, "sleep", lambda _seconds: None)


class MemoryDaemon(FakeDaemon):
    """Reuse only the protocol model; never construct its TCP listener."""

    def __init__(self):
        self.lock = threading.Lock()
        self.device_id = "arm-01"
        self.mpos = [-52.0, 49.0, -8.0]
        self.serial_connected = True
        self.session = CALIB_SESSION
        self.estop_locked = False
        self.motion_features = [
            "tap2.v1", "swipe.v1", "tap2.short-lift.v1", "tap2.feed-3000.v1",
        ]
        self.lease = None
        self.transactions = {}
        self.commands = []
        self.requests = []


class MemoryTransport:
    def __init__(self, daemon):
        self.daemon = daemon
        self.opened = True
        self.on_request = None

    def is_open(self):
        return self.opened

    def close(self):
        self.opened = False

    def cmd(self, line, *, timeout=None):
        assert self.opened
        assert line.split()[0] not in {
            "CONNECT", "DISCONNECT", "STOP", "ESTOP_LOCK", "RESET_ESTOP", "$H",
        }
        self.daemon.requests.append((1, line))
        if self.on_request is not None:
            self.on_request(line)
        return self.daemon._handle(line, 1)


class Phone:
    def __init__(self, bundle, daemon):
        self.bundle = bundle
        self.daemon = daemon
        self.offset = (0.0, 0.0)
        self.calls = 0
        self.before_sample = None
        self.after_sample = None

    def corners(self):
        dx, dy = self.offset
        return [[x + dx, y + dy]
                for x, y in self.bundle["screen"]["edgeModel"]["corners"]]

    def sample_pose(self):
        self.calls += 1
        if self.before_sample:
            self.before_sample()
        try:
            pose = screen_pose.fit_pose(
                self.bundle["screen"]["edgeModel"]["corners"], self.corners())
        except ValueError as exc:
            return [{"code": "screen_pose_unavailable", "message": str(exc)}], None
        if self.after_sample:
            self.after_sample()
        return [], pose

    async def live_gate(self, **_kwargs):
        checks, pose = self.sample_pose()
        return {"checks": checks, "pose": pose}

    async def wake_gate(self):
        checks, pose = self.sample_pose()
        if checks:
            return checks, {}, None
        image_bundle = deepcopy(self.bundle)
        image_bundle["screen"]["edgeModel"]["corners"] = self.corners()
        frame = wake_frame(
            image_bundle, sequence=self.calls,
            lit=any(cmd.startswith("TAP2 ") for cmd in self.daemon.commands))
        frame["_screen_pose"] = pose
        frame["source_received_at"] = frame["source_captured_at"]
        return [], frame, None


@pytest.fixture
def memory_rig(tmp_path):
    bundle = validated_bundle(gesture_bundle())
    cb.save_bundle(bundle, tmp_path)
    daemon = MemoryDaemon()
    transport = MemoryTransport(daemon)
    driver = DaemonArmDriver("arm-01", data_root=tmp_path)
    driver._transport = transport
    driver._connected = True
    driver._daemon_session = daemon.session
    phone = Phone(bundle, daemon)
    driver.live_calibration_gate = phone.live_gate
    driver.screen_wake_gate = phone.wake_gate
    yield daemon, driver, bundle, phone, transport
    transport.close()


ACTIONS = ("click", "double_tap", "swipe_up", "swipe_down", "wake")


def request_for(action, bundle):
    binding = sg.binding_snapshot(bundle)
    if action == "click":
        resolved = sg.resolve_point(point(.5, .65), bundle)
        return {
            "target_id": "conversation_item",
            "target_ref": {
                "source": "camera_ocr",
                "coordinateVersion": "calibrated_camera.v1",
                "cameraX": resolved["cameraX"], "cameraY": resolved["cameraY"],
                "frameW": bundle["frame"]["width"],
                "frameH": bundle["frame"]["height"],
                "frameSeq": 42, "confidence": .99,
            },
        }
    if action == "double_tap":
        return {"point": point(.5, .65), "binding": binding}
    if action.startswith("swipe_"):
        start, end = (.7, .3) if action == "swipe_up" else (.3, .7)
        return {
            "start": point(.5, start), "end": point(.5, end),
            "durationMs": 1200, "binding": binding,
        }
    return {"binding": binding, "confirmed": True}


def run_action(action, driver, request):
    method = "wake_screen" if action == "wake" else (
        "swipe" if action.startswith("swipe_") else action)
    return asyncio.run(getattr(driver, method)(request))


def contact_commands(daemon):
    return [cmd for cmd in daemon.commands
            if cmd.startswith(("TAP ", "TAP2 ", "SWIPE ", "PATH1 "))]


@pytest.mark.parametrize("action", ACTIONS)
def test_stable_shift_is_compensated_once_without_changing_bundle(memory_rig, action):
    daemon, driver, bundle, phone, _transport = memory_rig
    phone.offset = (5.0, 3.0)
    original = cb.bundle_path("arm-01", driver.data_root).read_bytes()
    request = request_for(action, bundle)
    result = run_action(action, driver, request)
    normalized = point(.5, .65) if not action.startswith("swipe_") else request["end"]
    resolved = sg.resolve_point(normalized, bundle)
    expected = screen_pose.physical_point(
        bundle, screen_pose.fit_pose(
            bundle["screen"]["edgeModel"]["corners"], phone.corners()),
        resolved["cameraX"], resolved["cameraY"])
    assert daemon.mpos[:2] == pytest.approx(expected, abs=.002)
    assert len(contact_commands(daemon)) == 1
    assert phone.calls >= 2
    assert result.get("controller_completed", result["ok"]) is True
    assert cb.bundle_path("arm-01", driver.data_root).read_bytes() == original


@pytest.mark.parametrize("offset", [(1.0, 0.0), (5.0, 3.0)])
def test_raw_live_camera_point_is_never_translated_twice(memory_rig, offset):
    daemon, driver, bundle, phone, _transport = memory_rig
    phone.offset = offset
    request = request_for("click", bundle)
    target = request["target_ref"]
    target.pop("coordinateVersion")
    target["cameraX"] += offset[0]
    target["cameraY"] += offset[1]
    if np.hypot(*offset) > 2:
        with pytest.raises(DriverError, match="观测时姿态"):
            run_action("click", driver, request)
        assert daemon.commands == []
    else:
        run_action("click", driver, request)
        pose = screen_pose.fit_pose(
            bundle["screen"]["edgeModel"]["corners"], phone.corners())
        expected = screen_pose.physical_point(
            bundle, pose, target["cameraX"], target["cameraY"], already_live=True)
        assert daemon.mpos[:2] == pytest.approx(expected, abs=.002)


@pytest.mark.parametrize("action", ACTIONS)
def test_large_initial_move_never_travels_or_contacts(memory_rig, action):
    daemon, driver, bundle, phone, _transport = memory_rig
    phone.offset = (50.0, 0.0)
    with pytest.raises(DriverError):
        run_action(action, driver, request_for(action, bundle))
    assert daemon.commands == []


@pytest.mark.parametrize("action", ACTIONS)
def test_move_seen_before_contact_blocks_touch(memory_rig, action):
    daemon, driver, bundle, phone, _transport = memory_rig

    def move_after_travel():
        if daemon.commands:
            phone.offset = (50.0, 0.0)

    phone.before_sample = move_after_travel
    if action == "wake":
        result = run_action(action, driver, request_for(action, bundle))
        assert result["controller_completed"] is False
        assert result["outcome_hint"] == "unknown"
    else:
        with pytest.raises(DriverError):
            run_action(action, driver, request_for(action, bundle))
    assert daemon.commands
    assert not contact_commands(daemon)


@pytest.mark.parametrize("action", ACTIONS)
def test_drift_during_last_status_wait_must_block_contact(memory_rig, action):
    daemon, driver, bundle, phone, transport = memory_rig
    injected = []
    last_samples = []

    def record_sample():
        last_samples.append(len(daemon.commands))

    def status_wait(line):
        if line != "STATUS" or injected:
            return
        if action == "wake":
            at_contact = (daemon.commands
                          and daemon.commands[-1].startswith("Z 5.000"))
        else:
            at_contact = phone.calls == 2
        if at_contact:
            injected.append(phone.calls)
            phone.offset = (50.0, 0.0)

    phone.after_sample = record_sample
    transport.on_request = status_wait
    error = None
    try:
        run_action(action, driver, request_for(action, bundle))
    except DriverError as exc:
        error = exc
    assert injected, "The test must reach the STATUS wait after the final pose check"
    assert last_samples
    assert not contact_commands(daemon), (
        f"Phone shifted 50px during final STATUS wait; contact was sent: "
        f"{contact_commands(daemon)!r}; result={error!r}")


@pytest.mark.parametrize("action", ("click", "swipe_up", "swipe_down"))
def test_drift_during_lease_mpos_must_block_initial_travel(memory_rig, action):
    daemon, driver, bundle, phone, transport = memory_rig
    injected = []

    def mpos_wait(line):
        if line == "MPOS" and not injected:
            injected.append(True)
            phone.offset = (50.0, 0.0)

    transport.on_request = mpos_wait
    with pytest.raises(DriverError):
        run_action(action, driver, request_for(action, bundle))
    assert injected
    assert not contact_commands(daemon)
    assert not daemon.commands, (
        f"Phone moved during leased MPOS; stale travel was still sent: "
        f"{daemon.commands!r}")


@pytest.mark.parametrize("action", ("swipe_up", "swipe_down", "path1"))
@pytest.mark.parametrize("phone_moves", (False, True), ids=("stable", "moved"))
def test_post_hover_status_wait_must_guard_xy_and_contact(
        memory_rig, action, phone_moves):
    daemon, driver, bundle, phone, transport = memory_rig
    daemon.mpos[2] = -12.0
    hover_command = "Z 4.000 F1500"
    observed_wait = []
    if action == "path1":
        daemon.motion_features.append(PATH1_FEATURE)
        request = _path1_plan(driver.data_root)
    else:
        request = request_for(action, bundle)

    def status_wait(line):
        if (line == "STATUS" and not observed_wait
                and daemon.commands
                and daemon.commands[-1] == hover_command):
            observed_wait.append(list(daemon.commands))
            if phone_moves:
                phone.offset = (20.0, 0.0)

    transport.on_request = status_wait
    result = None
    error = None
    try:
        if action == "path1":
            result = asyncio.run(driver.execute_gesture_program(request))
        else:
            result = run_action(action, driver, request)
    except DriverError as exc:
        error = exc

    assert observed_wait == [[hover_command]], (
        "The scenario must reach the first STATUS wait after raising Z, "
        "before any XY travel or contact")
    contacts = contact_commands(daemon)
    xy_commands = [cmd for cmd in daemon.commands if cmd.startswith("XY ")]
    if phone_moves:
        assert contacts == [], (
            f"Phone shifted 20px during post-hover STATUS; contact was sent: "
            f"{contacts!r}; rejection={error!r}")
        assert xy_commands == [], (
            f"Phone shifted 20px during post-hover STATUS; stale XY was sent: "
            f"{xy_commands!r}; rejection={error!r}")
        assert error is not None, "Unsafe phone pose must reject the action"
    else:
        assert error is None, error
        assert result["controller_completed"] is True
        assert len(xy_commands) == 1
        assert len(contacts) == 1


@pytest.mark.parametrize("boundary", ("lease_mpos", "post_hover_status"))
@pytest.mark.parametrize("phone_moves", (False, True), ids=("stable", "moved"))
def test_move_target_rechecks_pose_after_blocking_waits(
        memory_rig, boundary, phone_moves):
    daemon, driver, bundle, phone, transport = memory_rig
    daemon.mpos[2] = -12.
    phone.offset = (5., 3.)
    camera_x, camera_y = 650., 350.
    enable_ui_target(
        driver.data_root, "ime_delete_key",
        nx=camera_x / bundle["frame"]["width"],
        ny=camera_y / bundle["frame"]["height"], target_mode="hover_only")
    expected = screen_pose.physical_point(
        bundle, screen_pose.fit_pose(
            bundle["screen"]["edgeModel"]["corners"], phone.corners()),
        camera_x, camera_y)
    injected = []

    def wait(line):
        at_boundary = (
            line == "MPOS" if boundary == "lease_mpos" else (
                line == "STATUS" and daemon.commands
                and daemon.commands[-1] == "Z 4.000 F1500"))
        if at_boundary and not injected:
            injected.append(True)
            if phone_moves:
                phone.offset = (20., 0.)

    transport.on_request = wait
    if phone_moves:
        with pytest.raises(DriverError):
            asyncio.run(driver.move_target("ime_delete_key"))
        assert not any(cmd.startswith("XY ") for cmd in daemon.commands)
        if boundary == "lease_mpos":
            assert daemon.commands == []
    else:
        result = asyncio.run(driver.move_target("ime_delete_key"))
        assert result["ok"] is True
        assert result["tap_executed"] is False
        assert daemon.mpos[:2] == pytest.approx(expected, abs=.002)
    assert injected, "The test must reach the selected blocking response"
    assert contact_commands(daemon) == []


class SequenceCamera(FakeCamera):
    def __init__(self, *, fault=None, fault_at=2):
        super().__init__()
        self.calls = 0
        self.fault = fault
        self.fault_at = fault_at

    async def capture(self):
        self.calls += 1
        frame = await super().capture()
        now = datetime.now(timezone.utc).isoformat()
        frame.update(source_frame_seq=self.calls,
                     source_captured_at=now, source_received_at=now)
        if self.calls == self.fault_at:
            if self.fault == "repeated":
                frame["source_frame_seq"] -= 1
            elif self.fault == "stale":
                frame["source_captured_at"] = (
                    datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            elif self.fault == "legacy":
                frame["source_legacy_mode"] = True
            elif self.fault == "frozen":
                frame["frozen"] = True
            elif self.fault in {"camera_id", "device_id", "device_unique_id"}:
                frame[self.fault] = "replacement"
            elif self.fault == "binding_revision":
                frame["binding_revision"] += 1
        return frame


def pose_consensus(monkeypatch, camera, *, live_corners=None):
    bundle = ready_bundle()
    monkeypatch.setattr(
        calibration_gate, "locate_screen_quad",
        lambda *_args, **_kwargs: {
            "corners": live_corners or bundle["screen"]["edgeModel"]["corners"]})
    return asyncio.run(calibration_gate.check_live_calibration(
        device_id=bundle["deviceId"], camera_id=bundle["cameraId"],
        daemon_addr=bundle["daemonAddr"], daemon_session=bundle["daemonSession"],
        estop_locked=False, camera=camera, bundle=bundle, require_pose=True))


def test_pose_consensus_returns_latest_of_four_fresh_frames(monkeypatch):
    camera = SequenceCamera()
    checks, frame, _deviation = pose_consensus(monkeypatch, camera)
    assert checks == []
    assert camera.calls == frame["source_frame_seq"] == 4
    assert frame["_screen_pose"]["movementPx"] == pytest.approx(0)


@pytest.mark.parametrize("fault", ("repeated", "stale", "legacy", "frozen"))
@pytest.mark.parametrize("sample", (2, 3, 4))
def test_pose_consensus_rejects_one_bad_source_frame(monkeypatch, fault, sample):
    checks, _frame, _deviation = pose_consensus(
        monkeypatch, SequenceCamera(fault=fault, fault_at=sample))
    assert checks


@pytest.mark.parametrize("fault", (
    "camera_id", "device_id", "device_unique_id", "binding_revision",
))
@pytest.mark.parametrize("sample", (1, 2, 3, 4))
def test_pose_consensus_must_reject_temporary_identity_mismatch(
        monkeypatch, fault, sample):
    checks, _frame, _deviation = pose_consensus(
        monkeypatch, SequenceCamera(fault=fault, fault_at=sample))
    assert checks, f"Consensus accepted sample {sample} from mismatched {fault}"


@pytest.mark.parametrize("live", [
    [[54, 4], [109, 4], [109, 43], [54, 43]],
    [[4, 4], [70, 4], [59, 43], [4, 43]],
])
def test_large_or_nonrigid_pose_cannot_be_downgraded_to_advisory(monkeypatch, live):
    checks, frame, _deviation = pose_consensus(
        monkeypatch, SequenceCamera(), live_corners=live)
    assert checks
    assert "_screen_pose" not in frame


@pytest.mark.parametrize(("metric", "amount", "allowed"), [
    ("movement", 44.99, True), ("movement", 45.01, False),
    ("residual", 1.99, True), ("residual", 2.01, False),
    ("scale", 1.0099, True), ("scale", 1.0101, False),
])
def test_default_pose_safety_boundaries_are_not_relaxed(metric, amount, allowed):
    saved = [[100.0, 100.0], [500.0, 100.0], [500.0, 500.0], [100.0, 500.0]]
    if metric == "movement":
        live = [[x + amount, y] for x, y in saved]
    elif metric == "residual":
        live = [[x + (amount if index % 2 == 0 else -amount), y]
                for index, (x, y) in enumerate(saved)]
    else:
        live = [[300 + amount * (x - 300), 300 + amount * (y - 300)]
                for x, y in saved]
    if allowed:
        screen_pose.fit_pose(saved, live)
    else:
        with pytest.raises(ValueError):
            screen_pose.fit_pose(saved, live)


@pytest.fixture
def calibration_env(tmp_path):
    env = Env(tmp_path, {"phone_arm_lead_s": 0})
    sid = env.ime.create_session("pose-review-phone")["sessionId"]
    env.ime.client(sid, "open")
    env.ime.phone_lag = 0
    env.wizard.state.update(
        stage=cw.STAGE_Z_READY, imeSessionId=sid, imeDeviceId="pose-review-phone",
        daemonSession=env.daemon.session,
        screen={"orientation": "portrait",
                "screenWidth": SCREEN_W, "screenHeight": SCREEN_H})
    env.wizard.state["geometry"]["edge"] = {**deepcopy(env.quad), "frameSeq": 0}
    auto_report(env.ime, env.daemon)
    return env


@pytest.mark.parametrize("kind", ("tap", "delete"))
@pytest.mark.parametrize("pose_fault", ("moved", "unlocatable"))
def test_manual_calibration_must_not_touch_without_safe_phone_pose(
        calibration_env, kind, pose_fault):
    env = calibration_env
    if pose_fault == "moved":
        env.quad["corners"] = [[x + 20, y] for x, y in env.quad["corners"]]
    else:
        env.wizard._edge_detector = lambda _jpg: None
    error = None
    try:
        env.wizard.z_tap_test(
            kind, depth=3, feed=1500, dwell_ms=0 if kind == "tap" else 2200)
    except (cw.WizardError, cw.AutoRunError) as exc:
        error = exc
    assert env.daemon.tap_count == 0, (
        f"Manual {kind} touched with {pose_fault} phone pose; rejection={error!r}")


def auto_run_for(env):
    return {
        "runId": "pose-review-run", "state": cw.AUTO_STATE_RUNNING,
        "deviceId": "arm-01", "daemonSession": env.daemon.session,
        "phoneSessionId": env.wizard.state["imeSessionId"],
        "screen": deepcopy(env.wizard.state["screen"]),
        "binding": env.wizard._read_binding_facts(),
        "runtime": {"profileFingerprint": "pose-review"},
        "modelVersion": 1,
        "arm": {"robotToScreen": {
            "a": 10.0, "b": 0.0, "c": 100.0,
            "d": 0.0, "e": 8.0, "f": 200.0,
        }},
        "z": {"zSafe": 5.0, "tapDepth": 3.0, "tapFeed": 1500.0},
        "config": deepcopy(env.wizard._auto_cfg),
        "locks": {"cameraToScreen": {"state": cb.MODEL_LOCK_LOCKED}},
        "samples": [],
        "edge": {**deepcopy(env.wizard.state["geometry"]["edge"]), "frameSeq": 0},
    }


def test_automatic_calibration_drift_during_arming_wait_warns_and_rejects_sample(
        calibration_env, monkeypatch):
    """H01（2026-10-05 确认）+ 2026-10-03 测试更正：布防等待期手机漂移不再
    点击前硬拒——按压执行、超限 pose_gate_skipped 留痕；采样有效性由回报
    后门兜底：姿态无法核实 → AutoRunError，样本不进学习。"""
    env = calibration_env
    env.wizard._phone_arm_lead_s = .25
    env.wizard._auto_runner._auto["run"] = auto_run_for(env)

    def phone_moves(_seconds):
        env.quad["corners"] = [[x + 6, y] for x, y in env.quad["corners"]]

    monkeypatch.setattr(cw.time, "sleep", phone_moves)
    with pytest.raises(cw.AutoRunError, match="姿态无法核实"):
        env.wizard._tap_with_report(
            "moving-before-press", lambda: env.wizard._daemon_tap(3, 1500, 0))
    run = env.wizard._auto_runner._auto["run"]
    # H01 取舍：位移超限不拦停按压（人工测试兜底），但必须留痕可审计
    assert env.daemon.tap_count == 1
    skips = [e for e in run.get("events", [])
             if e.get("event") == "pose_gate_skipped"]
    assert skips and all(
        e["detail"]["measuredDisplacementPx"] == 6.0
        and e["detail"]["thresholdPx"] == 4.0 for e in skips), run.get("events")
    # 漂移触点不得进入学习（原阻断语义的现行载体）
    assert not run.get("samples")


@pytest.mark.parametrize("move_during_report", (False, True), ids=("stable", "moved"))
def test_automatic_calibration_sample_eligibility_requires_one_phone_pose(
        calibration_env, move_during_report):
    env = calibration_env
    run = auto_run_for(env)
    env.wizard._auto_runner._auto["run"] = run
    env.wizard._target_detector = env.green_from_armed()
    original = env.daemon.on_tap

    def moved_while_touch_was_reported(line):
        if move_during_report:
            env.quad["corners"] = [[x + 6, y] for x, y in env.quad["corners"]]
        original(line)

    env.daemon.on_tap = moved_while_touch_was_reported
    sample = None
    try:
        sample = env.wizard._auto_runner._auto_click_point(
            run, "pose-review-center", .5, .5, "PHASE1_ARM", use_camera=False)
    except (cw.WizardError, cw.AutoRunError):
        if not move_during_report:
            raise
    assert env.daemon.tap_count == 1
    assert len(run["samples"]) == 1
    if not move_during_report:
        assert sample["eligible"] is True
        assert sample["actualTouch"] == pytest.approx([540.0, 1200.0])
        assert sample["touchTargetErrorPx"] == 0
    else:
        with pytest.raises(ValueError):
            screen_pose.fit_pose(run["edge"]["corners"], env.quad["corners"],
                                 max_movement=4)
        assert not any(saved["eligible"] for saved in run["samples"]), (
            f"A sample from a changed phone pose was eligible for learning: {sample!r}")
