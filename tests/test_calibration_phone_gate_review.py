"""Independent calibration gate review with production protocol, no sockets."""
from __future__ import annotations

import asyncio
import io
import json
import socket
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from adapters.screen_ime.calibration import CalibrationCoordinator
from adapters.screen_ime.routes import (
    create_screen_ime_router,
    screen_ime_error_handler,
)
from adapters.screen_ime.service import ScreenImeApiError
from drivers import calibration_wizard as cw
from tests.test_calibration_wizard import Env


@pytest.fixture
def protocol_rig(tmp_path, monkeypatch):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Review tests must not access network or hardware")

    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def isolated_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            forbidden_network()
        return connect(sock, address)

    def isolated_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            forbidden_network()
        return connect_ex(sock, address)

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect", isolated_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", isolated_connect_ex)
    service = SimpleNamespace(calibrations=CalibrationCoordinator())
    app = FastAPI()
    app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)
    app.include_router(create_screen_ime_router(service, frame_source=object()))
    requests = []
    request_bodies = []
    with TestClient(app) as asgi:
        def in_process_urlopen(request, timeout):
            url = urlsplit(request.full_url)
            assert url.netloc == "calibration-review.invalid"
            assert url.path.startswith("/api/v1/calibrations")
            requests.append((request.get_method(), url.path))
            request_bodies.append(
                (url.path, json.loads(request.data) if request.data else None),
            )
            response = asgi.request(
                request.get_method(), url.path, content=request.data,
                headers=dict(request.header_items()),
            )
            if response.status_code >= 400:
                raise urllib.error.HTTPError(
                    request.full_url, response.status_code, response.reason_phrase,
                    response.headers, io.BytesIO(response.content),
                )
            return io.BytesIO(response.content)

        monkeypatch.setattr(urllib.request, "urlopen", in_process_urlopen)
        ime = cw.ImeCalibrationClient("http://calibration-review.invalid")
        env = Env(tmp_path, {"phone_arm_lead_s": 0.25})
        wizard = env.wizard
        wizard.ime = ime
        session = ime.create_session("review-phone")
        sid = session["sessionId"]
        ime._request("POST", f"/api/v1/calibrations/{sid}/client",
                     {"deviceId": "review-phone", "state": "open"})
        # Seed only this isolated wizard; do not start its phone watcher.
        wizard.state.update(
            stage=cw.STAGE_Z_READY, imeSessionId=sid, imeDeviceId="review-phone",
            daemonSession=env.daemon.session,
        )
        yield SimpleNamespace(
            wizard=wizard, daemon=env.daemon, ime=ime, sid=sid,
            requests=requests, request_bodies=request_bodies,
        )


def prepare(rig, prearmed, target=None):
    sample_id = "review-sample"
    pre = None
    if prearmed:
        rig.ime.arm(rig.sid, sample_id, 1, target)
        pre = {"sequence": 1, "armedAt": cw.time.time()}
    return sample_id, pre


def touch(rig, sample_id):
    return rig.ime._request(
        "POST", f"/api/v1/calibrations/{rig.sid}/touch",
        {"deviceId": "review-phone", "sampleId": sample_id, "sequence": 1,
         "screenX": 500, "screenY": 900, "screenWidth": 1080,
         "screenHeight": 2400, "orientation": "portrait"},
    )


def assert_no_motion(rig, sample_id, pre):
    motions = []

    def forbidden_motion():
        motions.append("TAP")
        raise AssertionError("Gate dispatched motion after prerequisites changed")

    with pytest.raises(cw.WizardError):
        rig.wizard._tap_with_report(sample_id, forbidden_motion, pre_armed=pre)
    assert motions == []
    assert rig.daemon.cmds == []


@pytest.mark.parametrize("prearmed", [False, True])
@pytest.mark.parametrize("with_target", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
def test_real_protocol_accepts_one_matching_touch(
        protocol_rig, monkeypatch, prearmed, with_target, automatic):
    rig = protocol_rig
    target = (
        {"u": 0.5, "v": 0.5, "widthU": 0.1, "heightV": 0.1}
        if with_target else None
    )
    sample_id, pre = prepare(rig, prearmed, target)
    monkeypatch.setattr(cw.time, "sleep", lambda seconds: None)
    if automatic:
        rig.wizard._auto_runner._auto["run"] = {
            "runId": "review-auto", "state": cw.AUTO_STATE_RUNNING,
            "phoneSessionId": rig.sid,
            "daemonSession": rig.daemon.session,
            "binding": rig.wizard._read_binding_facts(),
            "edge": rig.wizard._edge_quad(),
        }
    motions = []

    def report_motion():
        motions.append("TAP")
        touch(rig, sample_id)

    result = rig.wizard._tap_with_report(
        sample_id, report_motion, pre_armed=pre, target=target,
    )
    assert motions == ["TAP"]
    assert result["screenX"] == 500
    assert result["screenY"] == 900
    final = rig.ime.get_session(rig.sid)
    assert final["armed"] is None
    assert len(final["samples"]) == 1
    assert final["samples"][0].get("target") == target
    assert sum(path.endswith("/arm") for _, path in rig.requests) == 1
    assert sum(path.endswith("/touch") for _, path in rig.requests) == 1


@pytest.mark.parametrize("prearmed", [False, True])
@pytest.mark.parametrize("change", [
    "closed", "completed", "cancelled", "disarmed", "consumed", "unreachable",
])
def test_real_protocol_changes_during_lead_reject_before_motion(
        protocol_rig, monkeypatch, prearmed, change):
    rig = protocol_rig
    sample_id, pre = prepare(rig, prearmed)

    def change_during_lead(seconds):
        if change == "closed":
            rig.ime._request(
                "POST", f"/api/v1/calibrations/{rig.sid}/client",
                {"deviceId": "review-phone", "state": "closed"},
            )
        elif change == "completed":
            rig.ime.complete(rig.sid)
        elif change == "cancelled":
            rig.ime.cancel(rig.sid)
        elif change == "disarmed":
            rig.ime.disarm(rig.sid, sample_id)
        elif change == "consumed":
            touch(rig, sample_id)
        elif change == "unreachable":
            def unavailable(*args, **kwargs):
                raise OSError("Simulated lost IME connection")
            monkeypatch.setattr(urllib.request, "urlopen", unavailable)

    monkeypatch.setattr(cw.time, "sleep", change_during_lead)
    assert_no_motion(rig, sample_id, pre)


@pytest.mark.parametrize("prearmed", [False, True])
def test_changed_ime_session_cannot_authorize_original_operation(
        protocol_rig, monkeypatch, prearmed):
    rig = protocol_rig
    sample_id, pre = prepare(rig, prearmed)
    replacement = {}

    def replace_session_during_lead(seconds):
        rig.ime.cancel(rig.sid)
        session = rig.ime.create_session("review-phone")
        replacement["sid"] = session["sessionId"]
        rig.ime._request(
            "POST", f"/api/v1/calibrations/{replacement['sid']}/client",
            {"deviceId": "review-phone", "state": "open"},
        )
        # The real protocol scopes sampleId and sequence to a session.
        rig.ime.arm(replacement["sid"], sample_id, 1)
        rig.wizard.state["imeSessionId"] = replacement["sid"]

    monkeypatch.setattr(cw.time, "sleep", replace_session_during_lead)
    assert_no_motion(rig, sample_id, pre)
    assert rig.ime.get_session(replacement["sid"])["armed"] == {
        "sampleId": sample_id, "sequence": 1,
    }


@pytest.mark.parametrize("prearmed", [False, True])
@pytest.mark.parametrize("same_sample", [False, True])
def test_replacement_arming_survives_rejected_operation_cleanup(
        protocol_rig, monkeypatch, prearmed, same_sample):
    rig = protocol_rig
    sample_id, pre = prepare(rig, prearmed)
    next_sample = sample_id if same_sample else "replacement-sample"

    def replace_arming_during_lead(seconds):
        rig.ime.disarm(rig.sid, sample_id)
        rig.ime.arm(rig.sid, next_sample, 2)

    monkeypatch.setattr(cw.time, "sleep", replace_arming_during_lead)
    assert_no_motion(rig, sample_id, pre)
    assert rig.ime.get_session(rig.sid)["armed"] == {
        "sampleId": next_sample, "sequence": 2,
    }, "Cleanup for the old sample/sequence cleared its replacement"


@pytest.mark.parametrize("prearmed", [False, True])
def test_active_auto_cancel_during_lead_rejects_before_motion(
        protocol_rig, monkeypatch, prearmed):
    rig = protocol_rig
    sample_id, pre = prepare(rig, prearmed)
    rig.wizard._auto_runner._auto["run"] = {
        "runId": "review-auto", "state": cw.AUTO_STATE_RUNNING,
        "phoneSessionId": rig.sid,
    }

    def cancel_during_lead(seconds):
        rig.wizard._auto_runner._auto["cancel"].set()

    monkeypatch.setattr(cw.time, "sleep", cancel_during_lead)
    motions = []

    def forbidden_motion():
        motions.append("TAP")
        raise AssertionError("Gate dispatched TAP after automatic run cancellation")

    with pytest.raises((cw.WizardError, cw._AutoCancelled)):
        rig.wizard._tap_with_report(sample_id, forbidden_motion, pre_armed=pre)
    assert motions == []


@pytest.mark.parametrize("prearmed", [False, True])
@pytest.mark.parametrize("change", ["removed", "replaced", "stopped"])
def test_changed_auto_run_cannot_authorize_original_operation(
        protocol_rig, monkeypatch, prearmed, change):
    rig = protocol_rig
    sample_id, pre = prepare(rig, prearmed)
    run = {
        "runId": "review-auto", "state": cw.AUTO_STATE_RUNNING,
        "phoneSessionId": rig.sid,
    }
    rig.wizard._auto_runner._auto["run"] = run

    def change_during_lead(seconds):
        if change == "removed":
            rig.wizard._auto_runner._auto["run"] = None
        elif change == "replaced":
            rig.wizard._auto_runner._auto["run"] = dict(run)
        else:
            run["state"] = cw.AUTO_STATE_STOPPED

    monkeypatch.setattr(cw.time, "sleep", change_during_lead)
    assert_no_motion(rig, sample_id, pre)
    assert rig.ime.get_session(rig.sid)["armed"] is None


@pytest.mark.parametrize("sample_id,sequence", [
    ("another-sample", 2), ("review-sample", 1), ("another-sample", 1),
])
def test_disarm_requires_both_sample_and_sequence_when_supplied(
        protocol_rig, sample_id, sequence):
    rig = protocol_rig
    rig.ime.arm(rig.sid, "review-sample", 2)
    before = rig.ime.get_session(rig.sid)
    with pytest.raises(cw.ImeError) as error:
        rig.ime.disarm(rig.sid, sample_id, sequence)
    assert error.value.status == 409
    assert error.value.code == "sample_not_armed"
    assert rig.ime.get_session(rig.sid) == before
    assert rig.daemon.cmds == []


@pytest.mark.parametrize("armed", [False, True], ids=["empty", "armed"])
@pytest.mark.parametrize("sequence", [
    pytest.param(True, id="bool-true"),
    pytest.param(False, id="bool-false"),
    pytest.param(1.0, id="integer-float"),
    pytest.param(1.5, id="fraction"),
    pytest.param("1", id="numeric-string"),
    pytest.param("", id="empty-string"),
    pytest.param([], id="array"),
    pytest.param({}, id="object"),
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative"),
])
def test_disarm_rejects_invalid_sequence_without_changing_session(
        protocol_rig, armed, sequence):
    rig = protocol_rig
    if armed:
        rig.ime.arm(rig.sid, "review-sample", 1)
    before = rig.ime.get_session(rig.sid)
    with pytest.raises(cw.ImeError) as error:
        rig.ime.disarm(rig.sid, "review-sample", sequence)
    assert error.value.status == 400
    assert error.value.code == "invalid_sequence"
    assert rig.ime.get_session(rig.sid) == before
    assert rig.daemon.cmds == []


@pytest.mark.parametrize("mode", ["legacy", "none", "exact"])
def test_disarm_legacy_and_exact_calls_are_idempotent_and_keep_samples(
        protocol_rig, mode):
    rig = protocol_rig
    rig.ime.arm(rig.sid, "reported-sample", 1)
    history = touch(rig, "reported-sample")["samples"]
    rig.ime.arm(rig.sid, "pending-sample", 7)

    def disarm():
        if mode == "legacy":
            return rig.ime.disarm(rig.sid, "pending-sample")
        if mode == "none":
            return rig.ime.disarm(rig.sid, "pending-sample", None)
        return rig.ime.disarm(rig.sid, "pending-sample", 7)

    first = disarm()
    assert first["armed"] is None
    assert first["samples"] == history
    assert first["status"] == "active"
    assert first["clientState"] == "open"
    assert disarm() == first
    expected_body = {"sampleId": "pending-sample"}
    if mode == "exact":
        expected_body["sequence"] = 7
    bodies = [body for path, body in rig.request_bodies if path.endswith("/disarm")]
    assert bodies == [expected_body, expected_body]
    assert rig.daemon.cmds == []


def test_queued_stale_disarm_checks_replacement_under_coordinator_lock():
    async def scenario():
        coordinator = CalibrationCoordinator()
        sid = (await coordinator.create("review-phone"))["sessionId"]
        await coordinator.client(sid, "review-phone", "open")
        await coordinator.arm(sid, "review-sample", 1, None, None, None, None)
        async with coordinator._lock:
            release = asyncio.create_task(coordinator.disarm(sid, "review-sample", 1))
            replace = asyncio.create_task(
                coordinator.arm(sid, "review-sample", 2, None, None, None, None),
            )
            stale = asyncio.create_task(coordinator.disarm(sid, "review-sample", 1))
            # All operations queue while the original pair still exists.
            await asyncio.sleep(0)
            assert not any(task.done() for task in (release, replace, stale))
        results = await asyncio.gather(release, replace, stale, return_exceptions=True)
        assert results[0]["armed"] is None
        assert results[1]["armed"] == {"sampleId": "review-sample", "sequence": 2}
        assert isinstance(results[2], ScreenImeApiError)
        assert results[2].code == "sample_not_armed"
        assert (await coordinator.get(sid))["armed"] == results[1]["armed"]

    asyncio.run(scenario())


@pytest.mark.parametrize("prearmed", [False, True])
@pytest.mark.parametrize("outcome", ["move_error", "page_closed", "timeout"])
def test_every_tap_cleanup_uses_original_sequence_without_motion_replay(
        protocol_rig, monkeypatch, prearmed, outcome):
    rig = protocol_rig
    clock = SimpleNamespace(now=1000.0)

    def advance(seconds):
        clock.now += seconds

    monkeypatch.setattr(
        cw, "time", SimpleNamespace(time=lambda: clock.now, sleep=advance),
    )
    sample_id, pre = prepare(rig, prearmed)
    motions = []

    def move():
        motions.append("TAP")
        if outcome == "page_closed":
            rig.ime._request(
                "POST", f"/api/v1/calibrations/{rig.sid}/client",
                {"deviceId": "review-phone", "state": "closed"},
            )
        else:
            rig.ime.disarm(rig.sid, sample_id, 1)
            rig.ime.arm(rig.sid, sample_id, 2)
            if outcome == "move_error":
                raise RuntimeError("Simulated lost motion acknowledgement")

    error_type = {
        "move_error": RuntimeError,
        "page_closed": cw.WizardError,
        "timeout": cw.TouchReportTimeout,
    }[outcome]
    with pytest.raises(error_type):
        rig.wizard._tap_with_report(sample_id, move, pre_armed=pre, timeout=0.5)
    assert motions == ["TAP"]
    final = rig.ime.get_session(rig.sid)
    if outcome == "page_closed":
        assert final["armed"] is None
        assert final["clientState"] == "closed"
    else:
        assert final["armed"] == {"sampleId": sample_id, "sequence": 2}
    bodies = [body for path, body in rig.request_bodies if path.endswith("/disarm")]
    assert bodies
    assert all(body == {"sampleId": sample_id, "sequence": 1} for body in bodies)
    assert len(bodies) == (1 if outcome == "page_closed" else 2)


def test_outer_auto_disarm_preserves_replacement_after_inner_gate_cleanup(
        protocol_rig, monkeypatch):
    rig = protocol_rig
    sample_id, pre = prepare(rig, prearmed=True)
    armed = {"sessionId": rig.sid, "sampleId": sample_id, **pre}
    replacement = {}

    def replace_during_lead(seconds):
        rig.ime.disarm(rig.sid, sample_id, 1)
        rig.ime.arm(rig.sid, sample_id, 2)
        replacement.update(rig.ime.get_session(rig.sid))

    monkeypatch.setattr(cw.time, "sleep", replace_during_lead)
    try:
        assert_no_motion(rig, sample_id, pre)
    finally:
        # Reproduce the outer finally after _tap_with_report already cleaned up.
        rig.wizard._auto_runner._auto_disarm(armed)

    assert rig.ime.get_session(rig.sid) == replacement
    assert replacement["armed"] == {"sampleId": sample_id, "sequence": 2}
    bodies = [body for path, body in rig.request_bodies if path.endswith("/disarm")]
    assert bodies == [{"sampleId": sample_id, "sequence": 1}] * 3
    assert sum(path.endswith("/arm") for _, path in rig.requests) == 2
    assert rig.daemon.cmds == []


def test_disarm_preview_preserves_same_sample_with_new_sequence(protocol_rig):
    rig = protocol_rig
    sample_id, pre = prepare(rig, prearmed=True)
    rig.wizard.state["xy"]["preview"] = {"sampleId": sample_id, **pre}
    rig.ime.disarm(rig.sid, sample_id, 1)
    target = {"u": 0.7, "v": 0.3, "widthU": 0.1, "heightV": 0.1}
    rig.ime.arm(rig.sid, sample_id, 2, target)
    replacement = rig.ime.get_session(rig.sid)

    rig.wizard._disarm_preview()

    assert rig.wizard.state["xy"]["preview"] is None
    assert rig.ime.get_session(rig.sid) == replacement
    assert replacement["armed"] == {
        "sampleId": sample_id, "sequence": 2, "target": target,
    }
    bodies = [body for path, body in rig.request_bodies if path.endswith("/disarm")]
    assert bodies == [{"sampleId": sample_id, "sequence": 1}] * 2
    request_count = len(rig.requests)
    rig.wizard._disarm_preview()
    assert len(rig.requests) == request_count
    assert rig.daemon.cmds == []
