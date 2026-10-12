"""Manual gesture safety tests. All devices, motion and frames are simulated."""
import asyncio
from copy import deepcopy
import hashlib
import json
import threading
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb
from drivers.real_camera import CameraMotionVerifier as LegacyCameraMotionVerifier
from scripts import motor_debug_server as debug
from tests.test_motor_debug_gesture_profiles import FakeDaemon
from tests.test_ui_targets import ready_bundle


@pytest.fixture()
def env(monkeypatch, tmp_path):
    daemon = FakeDaemon()
    context = {"context_id": "ctx", "device_id": "arm-01", "daemon_session": "sess-A",
               "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
               "binding_revision": 7, "frame_width": 1280, "frame_height": 720}
    cb.save_bundle(ready_bundle(), tmp_path)
    monkeypatch.setattr(debug, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "_BASIC_TICKETS", {})
    monkeypatch.setattr(debug, "_BASIC_ACTIVE", set())
    monkeypatch.setattr(debug, "daemon_for", lambda device: daemon)
    monkeypatch.setattr(debug, "require_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(debug, "camera_status", lambda device: {"has_frame": True, "seq": 100})
    monkeypatch.setattr(debug, "_reject_motion_during_auto_run", lambda *args: None)
    monkeypatch.setattr(debug, "device_config", lambda device: {"daemon_addr": "127.0.0.1:8795"})
    monkeypatch.setattr(debug, "log_op", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon, "mpos", lambda: {"x": 0, "y": 0, "z": 2, "state": "Idle"}, raising=False)
    return daemon, context, tmp_path


def point(x=300, y=300):
    return {"cameraX": x, "cameraY": y, "frameW": 1280, "frameH": 720}


def request(action="double_tap", **changes):
    state = debug.basic_gesture_state("arm-01", "ctx")
    return {"deviceId": "arm-01", "contextId": "ctx", "action": action,
            "operator": "tester", "confirmed": True, "requestId": state["requestId"],
            "binding": state["binding"], "frameSeq": 95,
            **({"point": point(), "intervalMs": 120} if action == "double_tap" else
               {"start": point(), "end": point(300, 450), "durationMs": 700}),
            **changes}


def confirmed():
    return "confirmed", {"motionConfirmed": True, "effectStatus": "manual_confirmation_required"}


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_single_call_dedup_frozen_calibration_and_bounded_result(env, monkeypatch, action):
    daemon, _, root = env
    before = cb.bundle_path("arm-01", root).read_bytes()
    run = AsyncMock(return_value=confirmed())
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    body = request(action)
    first = debug.test_basic_gesture("arm-01", body)
    second = debug.test_basic_gesture("arm-01", deepcopy(body))
    assert first == second and first["outcome"] == "confirmed"
    assert first["evidence"]["effectStatus"] == "manual_confirmation_required"
    run.assert_awaited_once()
    assert daemon.commands == []
    assert cb.bundle_path("arm-01", root).read_bytes() == before
    state = debug.basic_gesture_state("arm-01", "ctx")
    assert state["requestId"] != body["requestId"] and not state["blocked"]
    debug.test_basic_gesture("arm-01", request(action))
    assert run.await_count == 2
    with pytest.raises(ValueError, match="失效"):
        debug.test_basic_gesture("arm-01", body)
    assert run.await_count == 2
    assert len(list((root / "devices" / "arm-01").glob("basic_gesture_test*"))) == 1


@pytest.mark.parametrize("changes", [
    {"tapDepth": 7}, {"feed": 400}, {"hoverZ": 2}, {"device": "arm-01"},
    {"confirmed": False}, {"confirmed": 1}, {"operator": ""}, {"operator": 9},
    {"intervalMs": 49}, {"intervalMs": 501}, {"intervalMs": True},
    {"intervalMs": "120"}, {"intervalMs": float("nan")},
    {"frameSeq": True}, {"frameSeq": 0}, {"frameSeq": 101},
    {"frameSeq": -1}, {"action": "tap"}, {"deviceId": "arm-02"},
    {"point": {**point(), "source": "camera_gesture"}},
    {"point": {**point(), "cameraX": float("inf")}},
    {"point": {**point(), "cameraY": True}},
    {"point": {**point(), "frameW": 640}},
    {"point": {**point(), "frameH": "720"}},
    {"point": point(0, 100)}, {"point": point(810, 100)},
])
def test_invalid_requests_never_run_or_release(env, monkeypatch, changes):
    run = AsyncMock()
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    body = request(**changes)
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", body)
    run.assert_not_called()
    assert env[0].commands == []
    assert debug._basic_load("arm-01") is None


@pytest.mark.parametrize("duration", [149, 5001, True, "700"])
def test_swipe_duration_validation(env, monkeypatch, duration):
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", request("swipe", durationMs=duration))
    assert debug._basic_load("arm-01") is None


def test_swipe_equal_points_rejected(env):
    with pytest.raises(ValueError, match="重合"):
        debug.test_basic_gesture("arm-01", request("swipe", end=point()))


@pytest.mark.parametrize("field", ["intervalMs", "requestId", "binding", "operator", "frameSeq"])
def test_missing_keys(env, field):
    body = request()
    body.pop(field)
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", body)


@pytest.mark.parametrize("change", ["session", "geometry", "z", "frame", "binding", "not_ready"])
def test_stale_calibration_or_context_rejected(env, change):
    daemon, context, root = env
    body = request()
    bundle = cb.load_bundle("arm-01", root)
    if change == "session":
        daemon.session = "sess-B"
    elif change == "geometry":
        bundle["xy"]["residualRmsPx"] = .9
    elif change == "z":
        bundle["z"]["tapDepth"] = 4
    elif change == "frame":
        context["frame_width"] = 640
    elif change == "binding":
        context["binding_revision"] = 8
    else:
        bundle["status"] = "draft"
    cb.save_bundle(bundle, root)
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", body)
    assert daemon.commands == []


def test_binding_extra_and_boolean_identity_rejected(env):
    body = request()
    body["binding"]["unexpected"] = 1
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", body)
    body = request()
    body["binding"]["bindingRevision"] = True
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", body)


def test_same_id_different_body_rejected(env, monkeypatch):
    monkeypatch.setattr(debug, "_run_basic_gesture", AsyncMock(return_value=confirmed()))
    body = request()
    debug.test_basic_gesture("arm-01", body)
    body["intervalMs"] = 130
    with pytest.raises(ValueError, match="修改参数"):
        debug.test_basic_gesture("arm-01", body)


def test_unknown_survives_context_restart_and_geometry_change(env, monkeypatch):
    run = AsyncMock(return_value=("unknown", {"reason": "lost reply", "motionConfirmed": False}))
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    body = request()
    debug.test_basic_gesture("arm-01", body)
    debug._BASIC_TICKETS.clear()
    state = debug.basic_gesture_state("arm-01", "new-context")
    assert state["blocked"] and state["requestId"] is None
    bundle = cb.load_bundle("arm-01", env[2])
    bundle["z"]["tapDepth"] = 4
    cb.save_bundle(bundle, env[2])
    assert debug.basic_gesture_state("arm-01", "new-context")["blocked"]
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", request(requestId="new"))
    assert debug.test_basic_gesture("arm-01", body)["outcome"] == "unknown"
    run.assert_awaited_once()
    env[0].session = "sess-B"
    assert debug.basic_gesture_state("arm-01", "new-context")["blocked"]
    cb.save_bundle(ready_bundle(session="sess-B"), env[2])
    assert not debug.basic_gesture_state("arm-01", "new-context")["blocked"]


def test_persisted_pending_blocks_after_interruption(env, monkeypatch):
    body = request()

    async def interrupted(*args):
        assert debug._basic_load("arm-01")["outcome"] == "pending"
        assert debug.basic_gesture_state("arm-01", "ctx")["blocked"]
        assert debug.test_basic_gesture("arm-01", body)["outcome"] == "pending"
        raise KeyboardInterrupt("simulated abrupt termination")

    monkeypatch.setattr(debug, "_run_basic_gesture", interrupted)
    with pytest.raises(KeyboardInterrupt):
        debug.test_basic_gesture("arm-01", body)
    assert debug._basic_load("arm-01")["outcome"] == "pending"
    state = debug.basic_gesture_state("arm-01", "ctx")
    assert state["blocked"] and state["lastResult"]["outcome"] == "pending"
    assert state["lastResult"]["resultUnknown"]


def test_durable_pending_must_succeed_before_motion(env, monkeypatch):
    body = request()
    run = AsyncMock()
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    monkeypatch.setattr(debug, "_basic_save", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        debug.test_basic_gesture("arm-01", body)
    run.assert_not_called()
    assert not env[0].commands


def test_result_write_failure_preserves_pending(env, monkeypatch):
    body = request()
    save = debug._basic_save

    def fail_final(device, record):
        if record["outcome"] != "pending":
            raise OSError("disk full")
        save(device, record)

    monkeypatch.setattr(debug, "_basic_save", fail_final)
    monkeypatch.setattr(debug, "_run_basic_gesture", AsyncMock(return_value=confirmed()))
    with pytest.raises(OSError):
        debug.test_basic_gesture("arm-01", body)
    assert debug.basic_gesture_state("arm-01", "ctx")["blocked"]


@pytest.mark.parametrize("lease", [
    {"owner_type": "business", "lease_id": "business"},
    {"owner_type": "debug", "lease_id": "other"},
])
def test_no_foreign_lease_preemption(env, monkeypatch, lease):
    env[0].lease = lease
    run = AsyncMock()
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    with pytest.raises(ValueError, match="不能抢占"):
        debug.test_basic_gesture("arm-01", request())
    run.assert_not_called()
    assert not env[0].commands


def test_release_own_idle_only(env, monkeypatch):
    env[0].lease = {"owner_type": "debug", "lease_id": "own"}
    env[0].lease_id = "own"
    monkeypatch.setattr(debug, "_run_basic_gesture", AsyncMock(return_value=confirmed()))
    debug.test_basic_gesture("arm-01", request())
    assert env[0].commands == ["RELEASE"]


def test_busy_and_auto_run_reject_without_queue(env, monkeypatch):
    body = request()
    env[0].motion_active = True
    with pytest.raises(ValueError, match="不会排队"):
        debug.test_basic_gesture("arm-01", body)
    env[0].motion_active = False
    monkeypatch.setattr(debug, "_reject_motion_during_auto_run",
                        lambda *args: (_ for _ in ()).throw(ValueError("auto run")))
    with pytest.raises(ValueError, match="auto run"):
        debug.test_basic_gesture("arm-01", body)
    assert debug._basic_load("arm-01") is None


def test_nonblocking_lock(env):
    ready, release = threading.Event(), threading.Event()

    def hold():
        with env[0]._lock:
            ready.set()
            release.wait(3)

    worker = threading.Thread(target=hold)
    worker.start()
    ready.wait(3)
    try:
        with pytest.raises(ValueError, match="不会排队"):
            debug.test_basic_gesture("arm-01", request())
    finally:
        release.set()
        worker.join(3)


def test_expired_ticket_and_corrupt_record(env):
    body = request()
    debug._BASIC_TICKETS["arm-01"]["expires"] = 0
    with pytest.raises(ValueError, match="失效"):
        debug.test_basic_gesture("arm-01", body)
    debug._basic_record_path("arm-01").write_text("{broken")
    with pytest.raises(ValueError, match="损坏"):
        debug.basic_gesture_state("arm-01", "ctx")


class Camera:
    def __init__(self, fault=None):
        self.seq = 100
        self.fault = fault
        self.closed = False

    async def capture(self):
        self.seq += 1
        shade = 50 if self.seq % 2 else 150
        png = cv2.imencode(".png", np.full((720, 1280, 3), shade, np.uint8))[1].tobytes()
        return {"width": 640 if self.fault == "size" else 1280, "height": 720,
                "source_frame_seq": 100 if self.fault == "stale" else self.seq,
                "frozen": self.fault == "frozen", "image_bytes": png,
                "captured_at": "simulation", "sha256": hashlib.sha256(png).hexdigest()}

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("fault,expected", [
    (None, "confirmed"), ("ack_only", "unknown"), ("stale", "unknown"),
    ("frozen", "failed"), ("size", "failed"), ("lost", "unknown"),
    ("connect", "failed"), ("session", "unknown"),
])
def test_bridge_production_driver_verifier_live_gate_and_effect_split(env, monkeypatch, action, fault, expected):
    # These camera_gesture cases exercise legacy frame freshness, not probe tracking.
    monkeypatch.setattr(debug, "CameraMotionVerifier", LegacyCameraMotionVerifier)
    body = request(action)
    camera = Camera(fault)
    calls = []
    monkeypatch.setattr(debug, "_gesture_camera", lambda *args: camera)
    gate = AsyncMock(return_value=([], {}, None))
    monkeypatch.setattr(debug, "check_live_calibration", gate)

    class Arm:
        def __init__(self, *args, **kwargs):
            self.daemon_addr = kwargs["daemon_addr"]
            assert kwargs["owner_type"] == "debug"
            assert kwargs["owner_id"].startswith("basic-gesture:")
            self.lease = False
            self.changed = False

        async def connect(self):
            calls.append("TCP handshake")
            if fault == "connect":
                raise ValueError("connect failed")

        async def read_state(self):
            return {"session": "sess-B" if self.changed else "sess-A"}

        async def move(self, payload):
            calls.append((action, payload))
            assert self.motion_verifier._settle == .15
            gate_result = await self.live_calibration_gate()
            assert gate_result["checks"] == []  # 无阻断即放行（pose 可为空）
            self.lease = True
            await self.motion_verifier.before(action)
            assert (debug._basic_record_path("arm-01").parent /
                    "basic_gesture_evidence" / "before.png").exists()
            if fault == "lost":
                raise OSError("motion reply lost")
            proof = await self.motion_verifier.after(action)
            self.lease = False
            self.changed = fault == "session"
            return {"ok": True, "motion_verified": proof["verified"] and fault != "ack_only"}

        double_tap = swipe = move

        async def disconnect(self):
            calls.append("close own TCP")

    monkeypatch.setattr(debug, "DaemonArmDriver", Arm)
    binding, bundle = debug._basic_binding("arm-01", env[1], "sess-A")
    gesture = debug._basic_validate("arm-01", body, binding, bundle)
    outcome, evidence = asyncio.run(debug._run_basic_gesture("arm-01", body, gesture))
    assert outcome == expected, (outcome, evidence)
    assert evidence["motionConfirmed"] is (expected == "confirmed")
    assert evidence["effectStatus"] == "manual_confirmation_required"
    assert camera.closed and calls[-1] == "close own TCP"
    if fault != "connect":
        target = gesture if action == "swipe" else {
            "target_id": "gesture_point", "intervalMs": gesture["intervalMs"],
            "target_ref": {"source": "camera_vision", **gesture["point"]}}
        assert calls[1] == (action, target)
        gate.assert_awaited_once()
        assert gate.call_args.kwargs["camera"] is camera
    assert not env[0].commands


@pytest.mark.parametrize("value", ["", "../arm-01", "arm-01!", " arm-01", None, 1])
def test_strict_device_id_rejects_silent_fallback(env, value):
    with pytest.raises(ValueError):
        debug.strict_debug_device(value)


def test_unknown_device_rejected(env, monkeypatch):
    monkeypatch.setattr(debug, "device_config",
                        lambda *args: (_ for _ in ()).throw(ValueError("not registered")))
    with pytest.raises(ValueError, match="not registered"):
        debug.strict_debug_device("arm-99")


def review(body, **changes):
    return {"deviceId": "arm-01", "contextId": "ctx", "requestId": body["requestId"],
            "daemonSession": "sess-A", "operator": "reviewer", "reason": "Probe clear, device idle",
            "confirmed": True, **changes}


def test_review_unknown_preserves_original_and_allows_only_new_ticket(env, monkeypatch):
    body = request()
    run = AsyncMock(return_value=("unknown", {"reason": "lost reply", "motionConfirmed": False}))
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    original = debug.test_basic_gesture("arm-01", body)
    result = debug.review_basic_gesture("arm-01", review(body))
    assert result["outcome"] == original["outcome"] == "unknown"
    assert result["evidence"] == original["evidence"]
    assert result["review"]["operator"] == "reviewer"
    assert result["review"]["reason"] == "Probe clear, device idle"
    assert not debug.basic_gesture_state("arm-01", "ctx")["blocked"]
    assert debug.test_basic_gesture("arm-01", body)["outcome"] == "unknown"
    run.assert_awaited_once()
    fresh = request()
    assert fresh["requestId"] != body["requestId"]
    debug.test_basic_gesture("arm-01", fresh)
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", body)
    assert run.await_count == 2
    assert not env[0].commands


@pytest.mark.parametrize("change", ["active", "lease", "moving", "session", "offline", "wrong_id",
                                  "no_confirm", "no_reason", "extra"])
def test_review_must_be_explicit_idle_and_own_original(env, monkeypatch, change):
    body = request()
    monkeypatch.setattr(debug, "_run_basic_gesture",
                        AsyncMock(return_value=("unknown", {"motionConfirmed": False})))
    debug.test_basic_gesture("arm-01", body)
    check = review(body)
    if change == "active":
        debug._BASIC_ACTIVE.add("arm-01")
    elif change == "lease":
        env[0].lease = {"owner_type": "debug", "lease_id": "own"}
    elif change == "moving":
        monkeypatch.setattr(env[0], "mpos", lambda: {"state": "Run"})
    elif change == "session":
        env[0].session = "sess-B"
    elif change == "offline":
        monkeypatch.setattr(env[0], "status", lambda: {"connected": False, "session": "sess-A"})
    elif change == "wrong_id":
        check["requestId"] = "other"
    elif change == "no_confirm":
        check["confirmed"] = False
    elif change == "no_reason":
        check["reason"] = ""
    else:
        check["resume"] = True
    with pytest.raises(ValueError):
        debug.review_basic_gesture("arm-01", check)
    assert debug._basic_load("arm-01").get("review") is None
    assert not env[0].commands


def test_review_preserves_interrupted_pending(env, monkeypatch):
    body = request()

    async def interrupted(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(debug, "_run_basic_gesture", interrupted)
    with pytest.raises(KeyboardInterrupt):
        debug.test_basic_gesture("arm-01", body)
    result = debug.review_basic_gesture("arm-01", review(body))
    assert result["outcome"] == "pending"
    assert result["review"]
    assert not debug.basic_gesture_state("arm-01", "ctx")["blocked"]


def test_review_race_active_after_initial_check_is_rejected(env, monkeypatch):
    body = request()
    monkeypatch.setattr(debug, "_run_basic_gesture",
                        AsyncMock(return_value=("unknown", {"motionConfirmed": False})))
    debug.test_basic_gesture("arm-01", body)

    def race():
        debug._BASIC_ACTIVE.add("arm-01")
        return {"state": "Idle", "x": 0, "y": 0, "z": 2}

    monkeypatch.setattr(env[0], "mpos", race)
    with pytest.raises(ValueError):
        debug.review_basic_gesture("arm-01", review(body))
    assert debug._basic_load("arm-01").get("review") is None


@pytest.mark.parametrize("rejection", ["not_arrived", "lease", "boundary"])
def test_reconcile_unconsumed_ticket_proves_not_accepted_and_retires_late_post(env, monkeypatch, rejection):
    body = request()
    run = AsyncMock()
    monkeypatch.setattr(debug, "_run_basic_gesture", run)
    if rejection == "lease":
        env[0].lease = {"owner_type": "business", "lease_id": "foreign"}
    elif rejection == "boundary":
        body["point"] = point(0, 0)
    if rejection != "not_arrived":
        with pytest.raises(ValueError):
            debug.test_basic_gesture("arm-01", body)
    env[0].lease = None
    state = debug.basic_gesture_state("arm-01", "ctx", body["requestId"])
    assert state["lastResult"]["requestStatus"] == "not_accepted"
    assert state["lastResult"]["outcome"] == "failed" and not state["blocked"]
    assert state["requestId"] != body["requestId"]
    with pytest.raises(ValueError):
        debug.test_basic_gesture("arm-01", body)
    run.assert_not_called()
    assert not env[0].commands


def test_reconcile_accepted_unknown_never_turns_into_failed(env, monkeypatch):
    body = request()
    monkeypatch.setattr(debug, "_run_basic_gesture",
                        AsyncMock(return_value=("unknown", {"motionConfirmed": False})))
    debug.test_basic_gesture("arm-01", body)
    state = debug.basic_gesture_state("arm-01", "ctx", body["requestId"])
    assert state["lastResult"]["outcome"] == "unknown" and state["blocked"]


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("features", [True, False])
def test_real_driver_fake_daemon_end_to_end_no_hardware(env, monkeypatch, action, features):
    from tests.test_daemon_arm import FakeDaemon as ProtocolDaemon
    from drivers import real_stubs

    monkeypatch.setattr(debug, "CameraMotionVerifier", LegacyCameraMotionVerifier)
    monkeypatch.setattr(real_stubs, "require_confirmed_hardware", lambda: None)
    fake = ProtocolDaemon().start()
    fake.session = "sess-A"
    if not features:
        fake.motion_features = []
    camera = Camera()
    monkeypatch.setattr(debug, "device_config",
                        lambda device: {"daemon_addr": f"127.0.0.1:{fake.port}"})
    monkeypatch.setattr(debug, "_gesture_camera", lambda *args: camera)
    gate = AsyncMock(return_value=([], {}, None))
    monkeypatch.setattr(debug, "check_live_calibration", gate)
    before = cb.bundle_path("arm-01", env[2]).read_bytes()
    body = request(action)
    try:
        result = debug.test_basic_gesture("arm-01", body)
        assert result["outcome"] == ("confirmed" if features else "failed")
        if features:
            expected = "TAP2 " if action == "double_tap" else "SWIPE "
            assert sum(cmd.startswith(expected) for cmd in fake.commands) == 1
            assert not any(cmd.startswith("TAP ") for cmd in fake.commands)
            assert result["evidence"]["motionConfirmed"]
            assert result["evidence"]["effectStatus"] == "manual_confirmation_required"
            assert result["evidence"]["before"]["source_frame_seq"] < result["evidence"]["after"]["source_frame_seq"]
            if action == "double_tap":
                assert result["evidence"]["motion"]["target_id"] == "gesture_point"
            debug.test_basic_gesture("arm-01", body)
            assert sum(cmd.startswith(expected) for cmd in fake.commands) == 1
        else:
            assert fake.commands == []
        assert not any(cmd.startswith(("CONNECT", "DISCONNECT")) for _, cmd in fake.requests)
        assert not fake.lease and not fake.transactions
        assert camera.closed
        assert cb.bundle_path("arm-01", env[2]).read_bytes() == before
        gate.assert_awaited_once()
    finally:
        fake.stop()
