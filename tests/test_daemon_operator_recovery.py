"""人工通信复核只走假 daemon，不打开生产串口。"""
import asyncio
import json

import pytest

from drivers.arm import DriverError
from tests.test_daemon_arm import daemon_and_driver  # noqa: F401
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401
from tests.test_safe_park_capability import save_park


def fault_reply(daemon):
    daemon.estop_locked = True

    def hook(command, response, _):
        if command == "STATUS":
            state = json.loads(response)
            state.update(communicationRecoveryRequired=True,
                         communicationRecoveryId="fault-1",
                         communicationRecoveryReason="串口回包超时")
            return json.dumps(state)
        return response

    daemon.response_hook = hook


def test_heartbeat_retains_session_without_querying_faulted_serial(daemon_and_driver):
    daemon, driver = daemon_and_driver
    fault_reply(daemon)
    daemon.requests.clear()
    state = asyncio.run(driver.read_state())
    assert state["connected"] is True
    assert state["session"] == daemon.session
    assert state["communication_recovery_required"] is True
    assert state["position"] is None
    assert all(command == "STATUS" for _, command in daemon.requests)


def test_closed_motion_tcp_does_not_discard_preserved_serial_session(daemon_and_driver):
    daemon, driver = daemon_and_driver
    fault_reply(daemon)
    driver._transport.close()
    driver._connected = False
    state = asyncio.run(driver.read_state())
    assert state["connected"] is True
    assert state["session"] == daemon.session
    assert state["recovery_id"] == "fault-1"
    assert not any(command == "CONNECT" for _, command in daemon.requests)


def test_heartbeat_reattaches_tcp_after_external_fault_review(daemon_and_driver):
    daemon, driver = daemon_and_driver
    driver._transport.close()
    driver._connected = False
    state = asyncio.run(driver.read_state())
    assert state["connected"] is True
    assert state["session"] == daemon.session
    assert driver.connected
    assert not any(command == "CONNECT" for _, command in daemon.requests)


def test_heartbeat_failed_tcp_reattach_keeps_verified_session(daemon_and_driver, monkeypatch):
    daemon, driver = daemon_and_driver
    driver._transport.close()
    driver._connected = False

    async def failed():
        driver._daemon_session = None
        raise DriverError("driver_connect_failed", "暂时繁忙")

    monkeypatch.setattr(driver, "connect", failed)
    for _ in range(2):
        state = asyncio.run(driver.read_state())
        assert state["session"] == daemon.session
        assert driver._daemon_session == daemon.session


def test_operator_park_requires_confirmed_session(target_rig):
    save_park(target_rig)
    with pytest.raises(DriverError, match="会话"):
        asyncio.run(target_rig.arm.park(expected_session="old"))
    assert not motion(target_rig)


def test_operator_park_rechecks_execution_authority_between_steps(target_rig):
    save_park(target_rig)

    def guard():
        if motion(target_rig):
            raise DriverError("driver_stopped", "用户停止")

    with pytest.raises(DriverError):
        asyncio.run(target_rig.arm.park(expected_session="sess-A", execution_guard=guard))
    assert len(motion(target_rig)) == 1


def test_recovery_rejects_stale_identity_without_write(daemon_and_driver):
    daemon, driver = daemon_and_driver
    fault_reply(daemon)
    with pytest.raises(DriverError, match="会话|故障"):
        asyncio.run(driver.recover_communication("old-session", "operator", "fault-1"))
    assert not any(command.startswith("RECOVER_") for _, command in daemon.requests)


def test_recovery_does_not_clear_independent_local_estop(daemon_and_driver):
    daemon, driver = daemon_and_driver
    fault_reply(daemon)
    driver._estop_latched = True
    with pytest.raises(DriverError, match="急停"):
        asyncio.run(driver.recover_communication(daemon.session, "operator", "fault-1"))
    assert driver._estop_latched
    assert not any(command.startswith("RECOVER_") for _, command in daemon.requests)


def test_recovery_reports_matching_receipt_and_never_replays_motion(daemon_and_driver):
    daemon, driver = daemon_and_driver
    fault_reply(daemon)
    previous = daemon.response_hook
    recovered = False

    def hook(command, response, conn):
        nonlocal recovered
        if command.startswith("RECOVER_COMMUNICATION "):
            recovered = True
            daemon.estop_locked = False
            return json.dumps({"recovered": True, "session": daemon.session,
                               "recovery_id": "fault-1", "recovered_by": "operator",
                               "estop_locked": False, "outcome_hint": "unknown",
                               "position": {"x": 2, "y": 2, "z": 5, "state": "Idle"}})
        return response if recovered else previous(command, response, conn)

    daemon.response_hook = hook
    result = asyncio.run(driver.recover_communication(daemon.session, "operator", "fault-1"))
    assert result["recovered"] is True
    assert not daemon.commands
    assert not any(command in {"CONNECT", "RESUME"} or command.startswith("RESET_ESTOP")
                   for _, command in daemon.requests)
