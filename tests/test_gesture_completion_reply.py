"""Gesture reply and debug observation boundaries, using fake TCP only."""
from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
import urllib.request

import cv2
import pytest
import serial

from drivers import calibration_bundle as cb, real_stubs, screen_gestures as sg
from drivers.arm import DriverError
from tests.test_daemon_arm import FakeDaemon, make_driver, wait_until
from tests.test_screen_gesture_driver import gesture_bundle


class EvidenceVerifier:
    measurement_kind = "registered_probe_trace"

    def __init__(self, *, verified=False, observation_only=None, reasons=()):
        if observation_only is not None:
            self.observation_only = observation_only
        self.verified = verified
        self.reasons = list(reasons)
        self.before_labels = []
        self.after_labels = []
        self.abort_calls = 0

    def readiness(self):
        return self.reasons

    async def before(self, label):
        self.before_labels.append(label)
        return {"evidence": "synthetic-frame"}

    async def after(self, label):
        self.after_labels.append(label)
        return {"verified": self.verified,
                "reason": None if self.verified else "old template not visible"}

    async def abort(self):
        self.abort_calls += 1


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    daemon = FakeDaemon(mpos=(-52, 49, 5)).start()
    address = ("127.0.0.1", daemon.port)
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def isolated_connect(sock, target):
        assert target == address, f"Non-fixture connection: {target}"
        return connect(sock, target)

    def isolated_connect_ex(sock, target):
        assert target == address, f"Non-fixture connection: {target}"
        return connect_ex(sock, target)

    def forbidden(*args, **kwargs):
        pytest.fail("Gesture completion tests must not access live hardware/services")

    monkeypatch.setattr(socket.socket, "connect", isolated_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", isolated_connect_ex)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(cv2, "VideoCapture", forbidden)
    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(serial, "serial_for_url", forbidden)
    monkeypatch.setattr(real_stubs, "require_confirmed_hardware", lambda: None)
    driver = None
    try:
        bundle = gesture_bundle()
        bundle["daemonAddr"] = f"{address[0]}:{address[1]}"
        bundle["bindingFingerprint"] = cb.binding_fingerprint(
            device_id="arm-01", camera_id=bundle["cameraId"],
            camera_device_unique_id=bundle["cameraDeviceUniqueId"],
            daemon_addr=bundle["daemonAddr"],
            frame_width=bundle["frame"]["width"], frame_height=bundle["frame"]["height"])
        cb.save_bundle(bundle, tmp_path)
        baseline = cb.bundle_path("arm-01", tmp_path).read_bytes()
        daemon.motion_features = ["tap2.v1", "swipe.v1"]
        driver = make_driver(daemon, tmp_path)
        asyncio.run(driver.connect())
        yield SimpleNamespace(daemon=daemon, driver=driver, bundle=bundle, root=tmp_path)
        assert cb.bundle_path("arm-01", tmp_path).read_bytes() == baseline
    finally:
        if driver is not None:
            asyncio.run(driver.disconnect())
        daemon.stop()
    assert not daemon.lease and not daemon.transactions
    assert not any(cmd.startswith(("CONNECT", "DISCONNECT")) for _, cmd in daemon.requests)


def request(rig, action, *, legacy=False):
    if legacy:
        point = {"source": "camera_vision", "cameraX": 650, "cameraY": 350,
                 "frameW": 1280, "frameH": 720}
        if action in {"click", "double_tap"}:
            return {"target_id": "gesture_point", "target_ref": point}
        return {"start": point, "end": {**point, "cameraX": 430}, "durationMs": 700}
    point = {"source": "screen_normalized", "x": .5, "y": .5}
    points = ({"point": point} if action == "double_tap" else
              {"start": point, "end": {**point, "y": .7}})
    return {**points, "binding": sg.binding_snapshot(rig.bundle)}


def execute(rig, action, *, legacy=False):
    return asyncio.run(getattr(rig.driver, action)(request(rig, action, legacy=legacy)))


def assert_completion(result, action, completed, *, verified=False):
    assert result["controller_completed"] is completed
    assert result["motion_verified"] is verified
    assert result["outcome_hint"] == ("success" if verified else "unknown")
    assert result["effect_status"] == "unverified"
    if action == "double_tap":
        assert result["tap_count"] == (2 if completed else 0)
    else:
        assert result["swipe_executed"] is completed


def assert_disconnected_without_replay(rig, action):
    assert not rig.driver.connected
    assert not rig.driver._transport.is_open()
    assert not rig.driver._motion_lock.locked()
    commands, requests = list(rig.daemon.commands), list(rig.daemon.requests)
    with pytest.raises(DriverError) as error:
        execute(rig, action)
    assert error.value.category == "driver_disconnected"
    assert rig.daemon.commands == commands
    assert rig.daemon.requests == requests
    wait_until(lambda: rig.daemon.lease is None and not rig.daemon.transactions)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("legacy", [False, True], ids=["normalized", "camera"])
@pytest.mark.parametrize("verified", [False, True])
def test_real_flat_reply_needs_no_macro_counters(rig, action, legacy, verified):
    replies = []

    def capture(line, original, _connection):
        if line.startswith(("Z ", "XY ", "TAP2 ", "SWIPE ")):
            replies.append(json.loads(original))
        return original

    rig.daemon.response_hook = capture
    if verified:
        rig.driver.gesture_motion_verifier = EvidenceVerifier(verified=True)
    result = execute(rig, action, legacy=legacy)
    assert_completion(result, action, True, verified=verified)
    assert all(set(reply) == {"x", "y", "z", "state"} for reply in replies)
    assert len(result["phases"]) == len(replies) == len(rig.daemon.commands)
    if action == "double_tap" and not legacy:
        assert rig.daemon.commands[-3:] == [
            "Z 5.000 F1500", "TAP2 3.000 F1500 I0.05", "Z -5.000 F1500"]
        assert result["phases"][-1]["phase"] == "double_tap_retract"
    else:
        assert rig.daemon.commands[-1].startswith("TAP2 " if action == "double_tap" else "SWIPE ")
    assert [cmd for _, cmd in rig.daemon.requests][-2:] == ["END_MOTION", "RELEASE"]


BAD_FIELDS = [
    {"state": "Hold:0"}, {"state": "Run"}, {"state": "Alarm"}, {"state": None},
    {"state": "Idle", "ok": False}, {"ok": "true"}, {"ok": 1},
    {"completed": False}, {"completed": "true"}, {"completed": 1},
    {"unknown": True}, {"unknown": "false"}, {"aborted": True}, {"abort": True},
    {"failed": True}, {"stopped": True}, {"cancelled": True},
    {"error": "controller abort"}, {"status": "failed"}, {"outcome": "unknown"},
    {"outcome_hint": "unknown"}, {"status": []}, {"outcome": {}}, {"status": None},
    {"x": None}, {"y": True}, {"z": "invalid"}, {"x": float("nan")},
    {"y": float("inf")}, {"z": float("-inf")},
]


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("fields", BAD_FIELDS)
def test_complete_but_invalid_macro_reply_is_unknown_not_completed(rig, action, fields):
    rig.driver.gesture_motion_verifier = EvidenceVerifier(verified=True)

    def fault(line, original, _connection):
        if line.startswith(("TAP2 ", "SWIPE ")):
            return json.dumps({**json.loads(original), **fields})
        return original

    rig.daemon.response_hook = fault
    with pytest.raises(DriverError) as error:
        execute(rig, action)
    assert error.value.category == "driver_disconnected"
    assert "结果未知" in error.value.message and "禁止重放" in error.value.message
    macro = "TAP2" if action == "double_tap" else "SWIPE"
    assert [cmd.split()[0] for cmd in rig.daemon.commands].count(macro) == 1
    assert rig.daemon.requests[-1][1] == rig.daemon.commands[-1]
    assert_disconnected_without_replay(rig, action)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("observe_only", [False, True])
@pytest.mark.parametrize("phase", ["Z", "XY", "macro"])
@pytest.mark.parametrize("fault", ["missing_state", "missing_xyz", "wrong_x", "wrong_y", "wrong_z"])
def test_every_gesture_step_checks_idle_and_all_endpoint_axes(
        rig, action, observe_only, phase, fault):
    rig.driver.gesture_motion_verifier = EvidenceVerifier(observation_only=observe_only,
                                                        verified=not observe_only)
    prefix = ("TAP2" if action == "double_tap" else "SWIPE") if phase == "macro" else phase
    faulted = []

    def reply(line, original, _connection):
        if line.split()[0] != prefix:
            return original
        value = json.loads(original)
        if fault == "missing_state":
            del value["state"]
        elif fault == "missing_xyz":
            del value["x"]
        else:
            value[fault[-1]] += 1
        faulted.append(line)
        return json.dumps(value)

    rig.daemon.response_hook = reply
    with pytest.raises(DriverError) as error:
        execute(rig, action)
    assert error.value.category == "driver_disconnected"
    assert "结果未知" in error.value.message
    assert len(faulted) == 1
    assert rig.daemon.commands[-1] == rig.daemon.requests[-1][1] == faulted[0]
    if phase != "macro":
        assert not any(cmd.startswith(("TAP2 ", "SWIPE ")) for cmd in rig.daemon.commands)
    assert_disconnected_without_replay(rig, action)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("reply", [None, "ok", "{}", "[]", '{"x": 0', "ERROR: aborted"])
def test_lost_or_non_position_reply_never_replays(rig, action, reply):
    rig.driver.gesture_motion_verifier = EvidenceVerifier(observation_only=True)
    rig.daemon.response_hook = lambda cmd, original, _: (
        reply if cmd.startswith(("TAP2 ", "SWIPE ")) else original)
    with pytest.raises(DriverError) as error:
        execute(rig, action)
    assert error.value.category == "driver_disconnected"
    assert "结果未知" in error.value.message
    assert_disconnected_without_replay(rig, action)


@pytest.mark.parametrize("observe_only", [False, True])
@pytest.mark.parametrize("fault", BAD_FIELDS + [
    None, "ok", "{}", "[]", '{"x": 0', "ERROR: aborted",
    "missing_state", "missing_xyz", "wrong_x", "wrong_y", "wrong_z",
])
def test_double_tap_final_retract_failure_is_unknown_and_never_replayed(rig, observe_only, fault):
    rig.driver.gesture_motion_verifier = EvidenceVerifier(
        observation_only=observe_only, verified=not observe_only)
    faulted = []

    def reply(line, original, _connection):
        if line != "Z -5.000 F1500" or not any(
                command.startswith("TAP2 ") for command in rig.daemon.commands):
            return original
        faulted.append(line)
        value = json.loads(original)
        if isinstance(fault, dict):
            return json.dumps({**value, **fault})
        if fault == "missing_state":
            del value["state"]
            return json.dumps(value)
        if fault == "missing_xyz":
            del value["z"]
            return json.dumps(value)
        if isinstance(fault, str) and fault.startswith("wrong_"):
            value[fault[-1]] += 1
            return json.dumps(value)
        return fault

    rig.daemon.response_hook = reply
    with pytest.raises(DriverError) as error:
        execute(rig, "double_tap")
    assert error.value.category == "driver_disconnected"
    assert "结果未知" in error.value.message
    assert faulted == ["Z -5.000 F1500"]
    assert rig.daemon.commands[-3:] == [
        "Z 5.000 F1500", "TAP2 3.000 F1500 I0.05", "Z -5.000 F1500"]
    assert len(rig.daemon.commands) == 6
    assert sum(command.startswith("TAP2 ") for command in rig.daemon.commands) == 1
    assert rig.daemon.requests[-1][1] == "Z -5.000 F1500"
    assert_disconnected_without_replay(rig, "double_tap")


def test_strict_unverified_double_tap_macro_without_stop_flag_is_not_complete(rig):
    verifier = EvidenceVerifier(observation_only=False, verified=True)
    rig.driver.gesture_motion_verifier = verifier

    async def after(label):
        verifier.after_labels.append(label)
        if label == "double_tap":
            return {"verified": False, "reason": "macro motion not independently confirmed"}
        return {"verified": True}

    verifier.after = after
    result = execute(rig, "double_tap")
    assert len(rig.daemon.commands) == 5
    assert rig.daemon.commands[-2:] == ["Z 5.000 F1500", "TAP2 3.000 F1500 I0.05"]
    assert sum(command.startswith("TAP2 ") for _, command in rig.daemon.requests) == 1
    assert not any(command == "Z -5.000 F1500" for _, command in rig.daemon.requests)
    assert verifier.before_labels == verifier.after_labels == [
        "hover_z", "hover_z", "travel_xy", "double_tap_approach", "double_tap"]
    assert all(phase["verification"]["verified"] for phase in result["phases"][:-1])
    assert result["phases"][-1]["phase"] == "double_tap"
    assert result["phases"][-1]["verification"]["verified"] is False
    assert "stop_required" not in result["phases"][-1]["verification"]
    assert result["position"]["z"] == rig.daemon.mpos[2] == -3
    assert_completion(result, "double_tap", False)
    assert not rig.driver._motion_lock.locked()


@pytest.mark.parametrize("verified", [False, True])
def test_double_tap_stop_required_after_final_retract_prevents_completion(rig, verified):
    verifier = EvidenceVerifier(observation_only=True, verified=True)
    rig.driver.gesture_motion_verifier = verifier

    async def after(label):
        if label == "double_tap_retract":
            return {"verified": verified, "stop_required": True, "reason": "camera binding changed"}
        return {"verified": True}

    verifier.after = after
    result = execute(rig, "double_tap")
    assert rig.daemon.commands[-3:] == [
        "Z 5.000 F1500", "TAP2 3.000 F1500 I0.05", "Z -5.000 F1500"]
    assert len(rig.daemon.commands) == 6
    assert result["phases"][-1]["phase"] == "double_tap_retract"
    assert_completion(result, "double_tap", False)
    assert not rig.driver._motion_lock.locked()


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_endpoint_uses_rounded_plan_without_rebasing_small_reply_drift(rig, action):
    def drift(line, original, _connection):
        if line.startswith(("Z ", "XY ", "TAP2 ", "SWIPE ")):
            value = json.loads(original)
            value["x"] += len(rig.daemon.commands) * .03
            return json.dumps(value)
        return original

    rig.daemon.response_hook = drift
    with pytest.raises(DriverError, match="终点") as error:
        execute(rig, action)
    assert error.value.category == "driver_disconnected"
    assert len(rig.daemon.commands) == 2
    assert all(cmd.startswith("Z ") for cmd in rig.daemon.commands)
    assert_disconnected_without_replay(rig, action)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_skipped_small_travel_is_not_mistaken_for_endpoint_error(rig, action):
    point = sg.resolve_point({"source": "screen_normalized", "x": .5, "y": .5}, rig.bundle)
    rig.daemon.mpos = [point["robotX"] + .04, point["robotY"] - .04, -7.96]
    result = execute(rig, action)
    if action == "double_tap":
        assert rig.daemon.commands == [
            "Z 5.000 F1500", "TAP2 3.000 F1500 I0.05", "Z -5.000 F1500"]
    else:
        assert len(rig.daemon.commands) == 1
        assert rig.daemon.commands[0].startswith("SWIPE ")
    assert_completion(result, action, True)
    assert result["position"] == dict(zip(("x", "y", "z"), rig.daemon.mpos))


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_endpoint_tolerance_uses_sent_rounding_not_unrounded_target(rig, action):
    # resolve_point rounds target XY; rounding the relative move adds this offset.
    rig.daemon.mpos[0] -= .00035
    initial_x = rig.daemon.mpos[0]

    def rounding_margin(line, original, _connection):
        if line.startswith(("Z ", "XY ", "TAP2 ", "SWIPE ")):
            value = json.loads(original)
            value["x"] -= .0498
            return json.dumps(value)
        return original

    rig.daemon.response_hook = rounding_margin
    result = execute(rig, action)
    assert_completion(result, action, True)
    target = sg.resolve_point({"source": "screen_normalized", "x": .5, "y": .5}, rig.bundle)
    travel = next(cmd.split() for cmd in rig.daemon.commands if cmd.startswith("XY "))
    expected_x = initial_x + float(travel[1])
    assert rig.daemon.mpos[0] == pytest.approx(expected_x)
    assert expected_x - target["robotX"] == pytest.approx(-.00035)
    assert result["position"]["x"] - expected_x == pytest.approx(-.0498)
    assert result["position"]["x"] - target["robotX"] == pytest.approx(-.05015)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("flag", [None, False, 1, "true"])
def test_observation_requires_exact_true_on_selected_verifier(rig, action, flag):
    rig.driver.gesture_motion_verifier = EvidenceVerifier(observation_only=flag)
    rig.driver.motion_verifier = EvidenceVerifier(observation_only=True)
    rig.driver.gesture_observation_only = True
    result = execute(rig, action)
    assert len(rig.daemon.commands) == 1
    assert_completion(result, action, False)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("observe_only", [False, True])
def test_missing_template_readiness_only_bypassed_in_debug(rig, action, observe_only):
    verifier = EvidenceVerifier(observation_only=observe_only, reasons=["old template missing"])
    rig.driver.gesture_motion_verifier = verifier
    if not observe_only:
        with pytest.raises(DriverError, match="old template missing"):
            execute(rig, action)
        assert not rig.daemon.commands and not verifier.before_labels
        return
    result = execute(rig, action)
    count = 6 if action == "double_tap" else 4
    assert len(rig.daemon.commands) == count
    assert len(verifier.before_labels) == len(verifier.after_labels) == count
    if action == "double_tap":
        assert verifier.after_labels[-3:] == [
            "double_tap_approach", "double_tap", "double_tap_retract"]
    assert verifier.abort_calls == 1
    assert_completion(result, action, True)
    assert all(not phase["verification"]["verified"] for phase in result["phases"])


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("phase", ["first", "macro"])
@pytest.mark.parametrize("verified", [False, True])
def test_stop_required_overrides_observation_and_completion(rig, action, phase, verified):
    verifier = EvidenceVerifier(observation_only=True)
    rig.driver.gesture_motion_verifier = verifier

    async def after(label):
        if phase == "first" or label == action:
            return {"verified": verified, "stop_required": True, "reason": "camera binding changed"}
        return {"verified": False, "reason": "old template missing"}

    verifier.after = after
    result = execute(rig, action)
    # 停车 z=5 → hoverZ=-8 跨 13mm，超 daemon 10mm 单段软限，必拆 2 段
    # hover Z；宏观段 stop_required 后观察模式仍执行预建 retract（安全
    # 回退，不重放 tap、结果保持未知），故 double_tap 完整序列为 6 条。
    assert len(rig.daemon.commands) == (
        1 if phase == "first" else 6 if action == "double_tap" else 4)
    if phase == "macro" and action == "double_tap":
        assert rig.daemon.commands[-3:] == [
            "Z 5.000 F1500", "TAP2 3.000 F1500 I0.05", "Z -5.000 F1500"]
    assert_completion(result, action, False)
    assert not rig.driver._motion_lock.locked()


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("stage", ["before", "after"])
def test_camera_exceptions_are_not_visibility_notes(rig, action, stage):
    verifier = EvidenceVerifier(observation_only=True)
    rig.driver.gesture_motion_verifier = verifier
    if stage == "before":
        original = verifier.before

        async def before(label):
            if label == action:
                raise DriverError("driver_uncalibrated", "stale camera frame")
            return await original(label)

        verifier.before = before
    else:
        verifier.after = AsyncMock(side_effect=OSError("camera transport lost"))
        result = execute(rig, action)
        assert_completion(result, action, False)
        assert result["phases"][-1]["verification"]["stop_required"] is True
        assert len(rig.daemon.commands) == 1
        return
    with pytest.raises(DriverError) as error:
        execute(rig, action)
    assert error.value.category == "driver_disconnected"
    assert "结果未知" in error.value.message
    assert not any(cmd.startswith(("TAP2 ", "SWIPE ")) for cmd in rig.daemon.commands)
    assert_disconnected_without_replay(rig, action)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("observe_only", [False, True])
def test_unclassified_exception_after_macro_cannot_claim_completion(rig, action, observe_only):
    verifier = EvidenceVerifier(observation_only=observe_only, verified=True)
    rig.driver.gesture_motion_verifier = verifier

    async def after(label):
        if label == action:
            raise OSError("camera transport lost")
        return {"verified": True}

    verifier.after = after
    result = execute(rig, action)
    # 观察模式下宏段验证异常→预建 retract 仍执行一次（结果保持未知），
    # 故 double_tap 多一条收尾 Z；非观察模式立即中止在 TAP2 上。
    assert len(rig.daemon.commands) == (
        6 if action == "double_tap" and observe_only
        else 5 if action == "double_tap" else 4)
    if action == "double_tap":
        assert rig.daemon.commands[-1] == (
            "Z -5.000 F1500" if observe_only else "TAP2 3.000 F1500 I0.05")
    # 未分类异常落在宏段验证上：不得声称完成（stop_required）。
    macro_phase = next(p for p in result["phases"] if p["phase"] == action)
    assert macro_phase["verification"]["stop_required"] is True
    assert_completion(result, action, False)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("change", ["session", "estop", "stop", "lease"])
def test_observation_does_not_bypass_context_between_steps(rig, action, change):
    verifier = EvidenceVerifier(observation_only=True)
    rig.driver.gesture_motion_verifier = verifier

    async def after(_label):
        if change == "session":
            rig.daemon.session = "replacement-session"
        elif change == "estop":
            rig.daemon.estop_locked = True
        elif change == "stop":
            rig.driver._stop_generation += 1
        else:
            rig.daemon.lease = None
        return {"verified": False}

    verifier.after = after
    with pytest.raises(DriverError) as error:
        execute(rig, action)
    assert error.value.category == "driver_disconnected"
    assert len(rig.daemon.commands) == 1
    assert_disconnected_without_replay(rig, action)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("gate", ["feature", "limit", "binding", "live_calibration"])
def test_observation_keeps_preflight_hard_gates(rig, action, gate):
    rig.driver.gesture_motion_verifier = EvidenceVerifier(observation_only=True)
    payload = request(rig, action)
    if gate == "feature":
        rig.daemon.motion_features = []
    elif gate == "limit":
        rig.daemon.mpos[0] = 1000
    elif gate == "binding":
        payload["binding"]["binding_revision"] += 1
    else:
        rig.driver.live_calibration_gate = AsyncMock(return_value=[
            {"code": "binding_changed", "message": "camera binding changed"}])
    with pytest.raises(DriverError):
        asyncio.run(getattr(rig.driver, action)(payload))
    assert not rig.daemon.commands


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("field", ["observation_only", "observe_only", "gesture_observation_only"])
def test_external_payload_cannot_enable_observation(rig, action, field):
    payload = {**request(rig, action), field: True}
    with pytest.raises(DriverError) as error:
        asyncio.run(getattr(rig.driver, action)(payload))
    assert error.value.category == "driver_gesture_invalid"
    assert not rig.daemon.commands


def test_single_click_never_inherits_gesture_observation(rig):
    verifier = EvidenceVerifier(observation_only=True)
    rig.driver.motion_verifier = verifier
    result = execute(rig, "click", legacy=True)
    assert len(rig.daemon.commands) == 1
    assert not result["motion_verified"] and result["outcome_hint"] == "unknown"
    assert "controller_completed" not in result


def test_single_click_keeps_legacy_position_only_reply_handling(rig):
    rig.driver.motion_verifier = EvidenceVerifier(verified=True)

    def reply(line, original, _connection):
        if line.startswith("TAP "):
            value = json.loads(original)
            value.update(state="Hold:0", ok=False, x=value["x"] + 1)
            return json.dumps(value)
        return original

    rig.daemon.response_hook = reply
    result = execute(rig, "click", legacy=True)
    assert result["motion_verified"] is True
    assert result["outcome_hint"] == "success"
    assert "controller_completed" not in result
    assert rig.daemon.commands[-1].startswith("TAP ")
