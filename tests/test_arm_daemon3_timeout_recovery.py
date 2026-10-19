"""Timeout retains coordinates but requires explicit, synchronized human recovery."""
import json

import pytest

from scripts import arm_daemon3 as daemon
from tests.test_arm_daemon3_serial_framing_regression import rig  # noqa: F401

POSITION = b"<Idle|MPos:1,2,3>\n"


def fail_timeout(holder, command="MPOS"):
    if command != "MPOS":
        holder.leases.claim(11, "debug", "test")
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    status = holder.status()
    return f"RECOVER_COMMUNICATION operator {status['session']} {status['communicationRecoveryId']}"


def install_responses(wire, *, report=True, ack=True, position=POSITION):
    write = wire.write

    def respond(data):
        result = write(data)
        if data == b"$G\n":
            if report:
                wire.fragments.append(b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\n")
            if ack:
                wire.fragments.append(b"ok\n")
        if data == b"?":
            wire.fragments.append(position)
        return result

    wire.write = respond


def test_read_timeout_recovery_keeps_session_and_never_moves(rig):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    original_session = holder.arm.session
    install_responses(wire)

    result = json.loads(daemon.handle(command, 11))

    assert result["recovered"] is True
    assert result["session"] == original_session == holder.arm.session
    assert result["outcome_hint"] == "unknown"
    assert result["position"] == {"x": 1, "y": 2, "z": 3, "state": "Idle"}
    assert holder.status()["communicationRecoveryRequired"] is False
    assert not holder.estop_locked and not wire.closed
    assert set(wire.writes) <= {b"?", b"!", b"$G\n"}
    assert holder.leases.snapshot() is None


@pytest.mark.parametrize("partial", [b"", b"o"])
def test_late_ack_and_partial_line_are_consumed_before_sync_query(rig, partial):
    holder, wire, _clock = rig
    wire.fragments.append(partial)
    command = fail_timeout(holder, "Z 1")
    assert wire.writes == [b"G91\n", b"!"]
    wire.fragments.append(b"k\n" if partial else b"ok\n")
    install_responses(wire)

    assert json.loads(daemon.handle(command, 11))["recovered"]
    assert wire.writes[2:4] == [b"?", b"$G\n"]
    assert not any(item.startswith(b"G1") or item == b"~" for item in wire.writes)


def test_permanently_lost_ordinary_ack_can_resync_without_motion(rig):
    holder, wire, _clock = rig
    command = fail_timeout(holder, "Z 1")
    session = holder.arm.session
    install_responses(wire)
    assert json.loads(daemon.handle(command, 11))["recovered"]
    assert holder.arm.session == session and not wire.closed
    assert wire.writes[:4] == [b"G91\n", b"!", b"?", b"$G\n"]
    assert set(wire.writes[2:]) <= {b"$G\n", b"?"}


@pytest.mark.parametrize("report,ack", [(False, True), (True, False)])
def test_plain_old_ok_or_report_without_ack_cannot_establish_boundary(rig, report, ack):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    install_responses(wire, report=report, ack=ack)
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    assert holder.status()["communicationRecoveryRequired"]
    assert holder.estop_locked and not wire.closed


@pytest.mark.parametrize("state", [b"Hold:0", b"Hold:1", b"Run", b"Alarm", b"Jog"])
def test_recovery_does_not_resume_buffered_motion(rig, state):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    install_responses(wire, position=POSITION.replace(b"Idle", state))
    with pytest.raises(RuntimeError, match="未处于空闲"):
        daemon.handle(command, 11)
    assert holder.estop_locked and holder.status()["communicationRecoveryRequired"]
    assert b"~" not in wire.writes


@pytest.mark.parametrize("command", ["RESET_ESTOP operator", "RESUME", "Z 1", "XY 1 1", "TAP 1", "CLAIM debug other"])
def test_all_motion_and_generic_unlock_remain_blocked_after_timeout(rig, command):
    holder, wire, _clock = rig
    fail_timeout(holder)
    before = list(wire.writes)
    with pytest.raises(ValueError):
        daemon.handle(command, 12)
    assert wire.writes == before
    assert holder.estop_locked


def test_mpos_and_connect_do_not_clear_fault(rig):
    holder, wire, _clock = rig
    fail_timeout(holder)
    fault_id = holder.status()["communicationRecoveryId"]
    install_responses(wire)
    assert json.loads(daemon.handle("MPOS", 11))["state"] == "Idle"
    assert json.loads(daemon.handle("CONNECT", 11))["communicationRecoveryRequired"]
    assert holder.status()["communicationRecoveryId"] == fault_id
    assert holder.estop_locked


@pytest.mark.parametrize("change", ["session", "fault", "stop", "estop"])
def test_stale_recovery_authorization_cannot_unlock(rig, change):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    if change == "session":
        command = command.replace("framing-test-session", "stale-session")
    elif change == "fault":
        command += "-stale"
    else:
        daemon.handle("STOP" if change == "stop" else "ESTOP_LOCK newer", 12)
    before = list(wire.writes)
    with pytest.raises(ValueError):
        daemon.handle(command, 11)
    assert wire.writes == before and holder.estop_locked


def test_emergency_stop_during_recovery_wins(rig):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    install_responses(wire)
    write = wire.write

    def stop_during_query(data):
        result = write(data)
        if data == b"$G\n":
            daemon.handle("ESTOP_LOCK emergency", 12)
        return result

    wire.write = stop_during_query
    with pytest.raises(RuntimeError, match="发生停止"):
        daemon.handle(command, 11)
    assert holder.estop_locked and holder.status()["communicationRecoveryRequired"]


def test_preexisting_emergency_latch_survives_communication_recovery(rig):
    holder, wire, _clock = rig
    holder.estop_locked = True
    command = fail_timeout(holder)
    install_responses(wire)
    result = json.loads(daemon.handle(command, 11))
    assert result["recovered"] and result["estop_locked"]
    assert holder.estop_locked


@pytest.mark.parametrize("failure", ["read", "write", "reset"])
def test_real_transport_errors_and_reset_still_revoke_physical_session(rig, failure):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    if failure == "read":
        def fail_read(_size):
            raise daemon.serial.SerialException("USB device disappeared")
        wire.read = fail_read
    elif failure == "write":
        def fail_write(_data):
            raise OSError("USB device disappeared")
        wire.write = fail_write
    else:
        wire.fragments.append(b"Grbl 1.3b ['$' for help]\n")
    with pytest.raises(RuntimeError, match="会话已重置"):
        daemon.handle(command, 11)
    assert holder.arm is None and wire.closed
    assert holder.status()["communicationRecoveryRequired"] is False


def test_changing_idle_coordinates_cannot_pass_recovery(rig):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    install_responses(wire)
    write = wire.write
    queries = 0

    def move_between_queries(data):
        nonlocal queries
        result = write(data)
        if data == b"?":
            queries += 1
            wire.fragments[-1] = f"<Idle|MPos:{queries},2,3>\n".encode()
        return result

    wire.write = move_between_queries
    with pytest.raises(RuntimeError, match="位置仍在变化"):
        daemon.handle(command, 11)
    assert holder.estop_locked and holder.status()["communicationRecoveryRequired"]


def test_timed_out_partial_position_is_completed_without_losing_serial_boundary(rig):
    holder, wire, _clock = rig
    wire.fragments.append(b"<Idle|MPos:9,9,9")
    command = fail_timeout(holder)
    wire.fragments.append(b">\n")
    install_responses(wire)
    result = json.loads(daemon.handle(command, 11))
    assert result["position"] == {"x": 1, "y": 2, "z": 3, "state": "Idle"}
    assert not wire.closed


def test_idle_wait_timeout_keeps_session_and_locks_without_replay(rig):
    holder, wire, _clock = rig
    install_responses(wire, position=POSITION.replace(b"Idle", b"Run"))
    holder.leases.claim(11, "debug", "test")
    with pytest.raises(RuntimeError, match="超时"):
        holder.run(lambda arm: arm.idle(timeout=0.5), 11, motion=True)
    assert holder.arm is not None and not wire.closed
    assert holder.status()["communicationRecoveryRequired"] and holder.estop_locked
    assert set(wire.writes) == {b"?", b"!"}


def test_stop_write_failure_after_silence_is_a_real_disconnect(rig):
    holder, wire, _clock = rig
    write = wire.write

    def fail_stop(data):
        if data == b"!":
            raise OSError("USB gone")
        return write(data)

    wire.write = fail_stop
    with pytest.raises(RuntimeError, match="串口会话已撤销"):
        daemon.handle("MPOS", 11)
    assert holder.arm is None and wire.closed
    assert holder.estop_locked


def test_diagnostic_mpos_accounts_for_late_ack_before_recovery(rig):
    holder, wire, _clock = rig
    command = fail_timeout(holder, "Z 1")
    wire.fragments.append(b"ok\n")
    install_responses(wire)
    assert json.loads(daemon.handle("MPOS", 12))["state"] == "Idle"
    assert holder.arm._awaiting_ack is False
    assert holder.status()["communicationRecoveryRequired"]
    assert json.loads(daemon.handle(command, 11))["recovered"]


def test_retry_waits_for_previous_sync_ack_before_sending_another_query(rig):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    install_responses(wire, ack=False)
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    assert wire.writes.count(b"$G\n") == 1
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    assert wire.writes.count(b"$G\n") == 1
    # The first query's delayed ACK is consumed before a new tagged query.
    wire.fragments.append(b"ok\n")
    write = wire.write

    def add_ack(data):
        result = write(data)
        if data == b"$G\n":
            wire.fragments.append(b"ok\n")
        return result

    wire.write = add_ack
    assert json.loads(daemon.handle(command, 11))["recovered"]
    assert wire.writes.count(b"$G\n") == 2


def test_serial_error_followed_by_silence_is_not_downgraded_to_timeout(rig):
    holder, wire, _clock = rig
    read = wire.read
    failed = False

    def fail_then_empty(size):
        nonlocal failed
        if not failed:
            failed = True
            raise daemon.serial.SerialException("USB read failed")
        return read(size)

    wire.read = fail_then_empty
    with pytest.raises(RuntimeError, match="串口异常，会话已重置"):
        daemon.handle("MPOS", 11)
    assert holder.arm is None and wire.closed


def test_lost_motion_segment_ack_stays_held_without_completing_old_macro(rig):
    holder, wire, _clock = rig
    controller = model_grbl_hold(wire)
    controller["ack"] = False
    wire.fragments.append(b"ok\n")  # G91 accepted; the G1 ACK never arrives.
    command = fail_timeout(holder, "Z 1")
    assert controller["controller"] == "Hold:0"
    with pytest.raises(RuntimeError, match="需调试处理"):
        daemon.handle(command, 11)
    assert wire.writes == [b"G91\n", b"G1 Z1.000 F30\n", b"!", b"?"]
    assert wire.writes.count(b"G1 Z1.000 F30\n") == 1
    assert b"G90\n" not in wire.writes and b"~" not in wire.writes
    assert holder.estop_locked and not wire.closed


@pytest.mark.parametrize("old_ack", [b"ok\n", b"error:20\n"])
def test_old_ordinary_reply_before_gc_cannot_finish_parser_probe(rig, old_ack):
    holder, wire, _clock = rig
    command = fail_timeout(holder, "Z 1")
    wire.fragments.append(old_ack)
    install_responses(wire, report=False)
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    assert holder.arm._awaiting_ack
    assert holder.arm._pending_response_kind == "parser_probe"
    assert not getattr(holder.arm, "_parser_probe_report_seen", False)
    assert holder.estop_locked


def test_mpos_old_ack_does_not_clear_missing_parser_report(rig):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    install_responses(wire, report=False, ack=False)
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    wire.fragments.append(b"ok\n")
    assert json.loads(daemon.handle("MPOS", 12))["state"] == "Idle"
    assert holder.arm._awaiting_ack
    assert not holder.arm._parser_probe_report_seen
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    assert wire.writes.count(b"$G\n") == 1


def test_mpos_tracks_old_probe_report_and_retry_waits_for_its_ack(rig):
    holder, wire, _clock = rig
    command = fail_timeout(holder)
    install_responses(wire, report=False, ack=False)
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    wire.fragments.append(b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\n")
    assert json.loads(daemon.handle("MPOS", 12))["state"] == "Idle"
    assert holder.arm._awaiting_ack and holder.arm._parser_probe_report_seen
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    assert wire.writes.count(b"$G\n") == 1
    wire.fragments.append(b"ok\n")
    install_responses(wire)
    assert json.loads(daemon.handle(command, 11))["recovered"]
    assert wire.writes.count(b"$G\n") == 2


def test_incomplete_old_line_is_not_discarded_to_force_recovery(rig):
    holder, wire, _clock = rig
    wire.fragments.append(b"o")
    command = fail_timeout(holder, "Z 1")
    install_responses(wire)
    with pytest.raises(RuntimeError, match="超时"):
        daemon.handle(command, 11)
    assert holder.arm._awaiting_ack and holder.estop_locked
    assert not getattr(holder.arm, "_parser_probe_report_seen", False)


def model_grbl_hold(wire):
    """Model the upstream GRBL rule: ! turns even Idle into persistent Hold."""
    state = {"controller": "Idle", "respond": True, "ack": True}
    write = wire.write

    def respond(data):
        result = write(data)
        if data == b"!":
            state["controller"] = "Hold:0"
        if not state["respond"]:
            return result
        if data == b"?":
            wire.fragments.append(f"<{state['controller']}|MPos:1,2,3>\n".encode())
        elif data == b"$G\n" and state["controller"] == "Idle":
            wire.fragments.extend([b"[GC:G1 G54 G17 G21 G90 G94 M5 M9 T0 F30 S0]\n", b"ok\n"])
        elif data.startswith(b"G") and state["controller"] == "Idle":
            if data.startswith(b"G1 "):
                state["controller"] = "Run"
            if state["ack"]:
                wire.fragments.append(b"ok\n")
        return result

    wire.write = respond
    return state


def test_proven_idle_query_timeout_can_recover_on_controller_that_holds_on_bang(rig):
    holder, wire, _clock = rig
    controller = model_grbl_hold(wire)
    holder.run(lambda arm: arm.idle())
    assert holder.arm._idle_without_motion
    controller["respond"] = False
    command = fail_timeout(holder)
    assert b"!" not in wire.writes
    assert holder.estop_locked and holder.status()["communicationRecoveryRequired"]
    controller["respond"] = True
    assert json.loads(daemon.handle(command, 11))["recovered"]
    assert controller["controller"] == "Idle"
    assert holder.arm._idle_without_motion and not holder.estop_locked
    assert b"~" not in wire.writes and b"!" not in wire.writes


def test_single_mpos_does_not_grant_quiescent_proof(rig):
    holder, wire, _clock = rig
    controller = model_grbl_hold(wire)
    daemon.handle("MPOS", 11)
    assert not getattr(holder.arm, "_idle_without_motion", False)
    controller["respond"] = False
    command = fail_timeout(holder)
    assert wire.writes[-1] == b"!"
    controller["respond"] = True
    with pytest.raises(RuntimeError, match="需调试处理"):
        daemon.handle(command, 11)
    assert controller["controller"] == "Hold:0"
    assert b"$G\n" not in wire.writes and b"~" not in wire.writes


@pytest.mark.parametrize("line", ["G91", "G1 Z1 F30", "G90"])
def test_every_normal_gcode_invalidates_quiescent_proof_before_write(rig, line):
    holder, wire, _clock = rig
    controller = model_grbl_hold(wire)
    holder.run(lambda arm: arm.idle())
    controller["ack"] = False
    holder.leases.claim(11, "debug", "test")
    with pytest.raises(RuntimeError, match="超时"):
        holder.run(lambda arm: arm.send(line), 11, motion=True)
    assert not holder.arm._idle_without_motion
    assert controller["controller"] == "Hold:0"
    assert wire.writes[-1] == b"!"


def test_acknowledged_macro_requires_idle_barrier_to_reestablish_proof(rig):
    holder, wire, _clock = rig
    controller = model_grbl_hold(wire)
    holder.run(lambda arm: arm.idle())
    holder.leases.claim(11, "debug", "test")
    holder.run(lambda arm: arm.send("G91"), 11, motion=True)
    assert not holder.arm._idle_without_motion
    holder.run(lambda arm: arm.mpos())
    assert not holder.arm._idle_without_motion
    holder.run(lambda arm: arm.idle(), 11, motion=True)
    assert holder.arm._idle_without_motion
    controller["respond"] = False
    fail_timeout(holder)
    assert b"!" not in wire.writes


def test_idle_without_matching_ack_does_not_reestablish_proof(rig):
    holder, wire, _clock = rig
    model_grbl_hold(wire)
    holder.arm._awaiting_ack = True
    holder.arm._pending_response_kind = "command"
    holder.run(lambda arm: arm.idle())
    assert not getattr(holder.arm, "_idle_without_motion", False)


@pytest.mark.parametrize("state,expected", [("Idle", True), ("Hold:0", False)])
def test_connection_handshake_grants_proof_only_for_stable_idle(rig, state, expected):
    holder, wire, _clock = rig
    controller = model_grbl_hold(wire)
    controller["controller"] = state
    holder.arm._startup_banner_count = 0
    holder.arm._initializing_connection = True
    holder.arm._idle_without_motion = False
    holder.arm._confirm_connection()
    assert holder.arm._idle_without_motion is expected


def test_resume_invalidates_quiescent_proof_even_when_position_returns_idle(rig):
    holder, wire, _clock = rig
    model_grbl_hold(wire)
    holder.run(lambda arm: arm.idle())
    holder.leases.claim(11, "debug", "test")
    holder.run(lambda arm: arm.resume(), 11, motion=True)
    assert b"~" in wire.writes
    assert not holder.arm._idle_without_motion


@pytest.mark.parametrize("state", ["Run", "Hold:0", "Jog", "Alarm"])
def test_observed_nonidle_revokes_prior_idle_proof_before_query_timeout(rig, state):
    holder, wire, _clock = rig
    controller = model_grbl_hold(wire)
    holder.run(lambda arm: arm.idle())
    assert holder.arm._idle_without_motion
    controller["controller"] = state
    assert json.loads(daemon.handle("MPOS", 11))["state"] == state
    assert not holder.arm._idle_without_motion
    # A single later Idle cannot undo the revocation.
    controller["controller"] = "Idle"
    daemon.handle("MPOS", 11)
    assert not holder.arm._idle_without_motion
    controller["respond"] = False
    fail_timeout(holder)
    assert wire.writes[-1] == b"!"
    assert controller["controller"] == "Hold:0"
