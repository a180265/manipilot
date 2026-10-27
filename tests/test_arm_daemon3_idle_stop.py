"""Failure cleanup must not put a proven-idle controller into feed hold."""
import json

import pytest

from scripts import arm_daemon3 as daemon
from tests.test_arm_daemon3_serial_framing_regression import rig  # noqa: F401
from tests.test_arm_daemon3_timeout_recovery import model_grbl_hold


def test_completed_motion_then_cleanup_stop_keeps_parser_available(rig):
    holder, wire, _clock = rig
    controller = model_grbl_hold(wire)
    holder.run(lambda arm: arm.idle())
    generation = holder.stop_generation

    result = json.loads(daemon.handle("STOP", 11))

    assert result["stopped"] is True
    assert result["already_idle"] is True
    assert holder.stop_generation == generation + 1
    assert controller["controller"] == "Idle"
    assert b"!" not in wire.writes and b"~" not in wire.writes
    holder.leases.claim(11, "business", "next-task")
    assert holder.run(lambda arm: arm.send("G91"), 11, motion=True) == ["ok"]
    assert holder.status()["communicationRecoveryRequired"] is False


@pytest.mark.parametrize("uncertain", ["no-proof", "pending-ack", "busy", "estop"])
def test_uncertain_or_emergency_stop_still_sends_hold_without_wait(rig, uncertain):
    holder, wire, _clock = rig
    holder.arm._idle_without_motion = uncertain != "no-proof"
    holder.arm._awaiting_ack = uncertain == "pending-ack"
    if uncertain == "busy":
        holder.cmd_lock.acquire()
    try:
        command = "ESTOP_LOCK operator" if uncertain == "estop" else "STOP"
        assert json.loads(daemon.handle(command, 11))["stopped"] is True
        assert wire.writes == [b"!"]
    finally:
        if uncertain == "busy":
            holder.cmd_lock.release()
