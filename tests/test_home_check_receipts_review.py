"""Independent home-receipt regressions; temporary stores and fake drivers only."""
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import socket
import sys

import pytest

from drivers.arm import DriverError
from drivers.fake_arm import FakeArmDriver
from drivers.fake_camera import FakeCameraDriver
from drivers.fake_input import FakeInputDriver
from runtime.errors import ApiError
from tasks.plugin_api import make_step
from tests.helpers import (
    make_device_configs, make_runtime, op_context, probe_payload,
)
from tests.test_unknown_safety_hold import UnsafeRecoveryPlugin


@pytest.fixture(autouse=True)
def isolated_review(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    active = True

    def inside_root(path):
        resolved = Path(os.fsdecode(path)).resolve()
        return resolved == root or root in resolved.parents

    def denied(*args, **kwargs):
        raise AssertionError("Receipt review cannot access network or hardware")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    original_bind = socket.socket.bind

    def bind(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            return denied(connection, address)
        return original_bind(connection, address)

    monkeypatch.setattr(socket.socket, "bind", bind)
    import cv2
    import serial

    monkeypatch.setattr(cv2, "VideoCapture", denied)
    monkeypatch.setattr(serial, "Serial", denied)
    monkeypatch.setattr(serial, "serial_for_url", denied)

    def audit(event, args):
        if not active:
            return
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
            raise AssertionError("Receipt review cannot start processes")
        if event == "sqlite3.connect":
            assert args[0] == ":memory:" or inside_root(args[0]), (
                "Receipt review must use its temporary database"
            )
        if event == "open" and not isinstance(args[0], int):
            path = Path(os.fsdecode(args[0])).resolve()
            assert not str(path).startswith("/dev/") or path == Path("/dev/null")
            writing = (args[2] or 0) & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            )
            assert not writing or path == Path("/dev/null") or inside_root(path), (
                "Receipt review cannot write outside its temporary root"
            )
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"}:
            if not isinstance(args[0], int):
                assert inside_root(args[0]), "Receipt review cannot modify shared files"
        if event in {"os.rename", "os.link", "os.symlink"}:
            assert all(inside_root(path) for path in args[:2])

    sys.addaudithook(audit)
    try:
        yield
    finally:
        active = False


async def review_runtime(root, plugin=None):
    runtime = make_runtime(root, plugin=plugin)
    runtime.capabilities.bootstrap()
    # Do not start dispatch, background polling, or the daemon supervisor.
    await runtime.devices.register_all(make_device_configs())
    assert isinstance(runtime.devices.driver("arm-01"), FakeArmDriver)
    assert isinstance(runtime.devices.camera("arm-01"), FakeCameraDriver)
    assert isinstance(runtime.devices.input_driver("arm-01"), FakeInputDriver)
    runtime.devices.driver("arm-01").data_root = root / "data"
    return runtime


def assert_persisted_interlock(runtime):
    for device in (
        runtime.devices.get("arm-01"),
        runtime.store.get_device("arm-01"),
    ):
        assert device["home_ok"] is False
        assert device["status"] == "paused"
        assert device.get("pause_reason")
    with pytest.raises(ApiError):
        runtime.op_resume("arm-01", op_context())


async def assert_read_only_interlock(runtime, monkeypatch):
    writes = []
    arm = runtime.devices.driver("arm-01")

    async def home():
        writes.append("go_home")
        return {"ok": True}

    async def stop():
        writes.append("stop")
        return {"ok": True, "stopped": True}

    monkeypatch.setattr(arm, "go_home", home)
    monkeypatch.setattr(arm, "stop", stop)
    result = await runtime.op_home_check("arm-01", op_context())
    assert writes == [], "Unknown home receipts cannot authorize another driver call"
    assert result["detail"]["read_only"] is True
    assert result["home_ok"] is False
    assert_persisted_interlock(runtime)


async def inject_standalone_uncertainty(runtime, monkeypatch, receipt):
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def home():
        calls.append("go_home")
        if receipt == "cancelled":
            entered.set()
            await release.wait()
            return {"ok": True}
        if receipt == "exception":
            raise DriverError("driver_timeout", "Home receipt was lost")
        if receipt == "missing":
            return None
        return {"ok": True, "outcome_hint": "unknown"}

    monkeypatch.setattr(runtime.devices.driver("arm-01"), "go_home", home)
    if receipt == "cancelled":
        operation = asyncio.create_task(runtime.op_home_check("arm-01", op_context()))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        finally:
            release.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
    else:
        result = await runtime.op_home_check("arm-01", op_context())
        assert result["home_ok"] is False
        assert result["detail"]["motion_result_unknown"] is True
    assert calls == ["go_home"]


@pytest.mark.parametrize("revocation", ["lease_release", "lease_replacement", "session_change"])
@pytest.mark.parametrize("home_receipt", ["confirmed", "unknown"])
def test_finalization_home_rechecks_owner_after_read_state(
    tmp_path, monkeypatch, revocation, home_receipt,
):
    async def scenario():
        runtime = await review_runtime(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        operation = None
        try:
            task = runtime.submit_task(
                probe_payload([{}]), source="console", context={"operator_id": "review"},
            )
            lease = runtime.leases.acquire("arm-01", task["task_id"])
            task.update(status="executing", lease_id=lease["lease_id"])
            runtime.store.save_task(task)
            runtime.devices._set_status("arm-01", "busy", "Original simulated owner")
            device = runtime.devices.get("arm-01")
            device["daemon_session"] = "session-old"
            runtime.store.save_device(device)
            arm = runtime.devices.driver("arm-01")
            original_read = arm.read_state
            reads = []
            homes = []

            async def read_state():
                state = await original_read()
                reads.append("read")
                if len(reads) == 1:
                    entered.set()
                    await release.wait()
                    return {**state, "session": "session-old"}
                return {**state, "session": "session-new"}

            async def home():
                homes.append("go_home")
                if home_receipt == "unknown":
                    return {"ok": True, "outcome_hint": "unknown"}
                return {"ok": True}

            monkeypatch.setattr(arm, "read_state", read_state)
            monkeypatch.setattr(arm, "go_home", home)
            operation = asyncio.create_task(
                runtime.executor.finalize(task, "confirmed", skip_final_evidence=True),
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            replacement = None
            if revocation == "session_change":
                await runtime.devices._heartbeat_device("arm-01")
                assert runtime.devices.get("arm-01")["daemon_session"] == "session-new"
                assert runtime.leases.get("arm-01") is None
            else:
                runtime.leases.release("arm-01", lease["lease_id"])
                if revocation == "lease_replacement":
                    replacement = runtime.leases.acquire("arm-01", "replacement-owner")
                    device.update(
                        home_ok=False, home_checked_at=None,
                        status_reason="Replacement owner must remain untouched",
                    )
                    runtime.store.save_device(device)
            replacement_state = deepcopy(runtime.devices.get("arm-01"))
            release.set()
            result = await asyncio.wait_for(operation, timeout=2)
            assert homes == [], "Lease/session revoked during read_state: no go_home"
            assert result["status"] == "unknown"
            assert result["home_verified"] is False
            if replacement:
                assert runtime.leases.get("arm-01") == replacement
                assert runtime.devices.get("arm-01") == replacement_state
                assert runtime.store.get_device("arm-01") == replacement_state
        finally:
            release.set()
            if operation is not None:
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", ["unknown", "missing", "exception"])
def test_standalone_unknown_home_blocks_retry_and_resume(tmp_path, monkeypatch, receipt):
    async def scenario():
        runtime = await review_runtime(tmp_path)
        try:
            await inject_standalone_uncertainty(runtime, monkeypatch, receipt)
            assert_persisted_interlock(runtime)
            await assert_read_only_interlock(runtime, monkeypatch)
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_standalone_cancelled_home_persists_interlock_before_resume(tmp_path, monkeypatch):
    async def scenario():
        runtime = await review_runtime(tmp_path)
        try:
            await inject_standalone_uncertainty(runtime, monkeypatch, "cancelled")
            assert_persisted_interlock(runtime)
            await assert_read_only_interlock(runtime, monkeypatch)
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", ["unknown", "cancelled"])
def test_standalone_home_interlock_survives_restart_read_only(
    tmp_path, monkeypatch, receipt,
):
    async def scenario():
        original = await review_runtime(tmp_path)
        try:
            await inject_standalone_uncertainty(original, monkeypatch, receipt)
        finally:
            await original.shutdown()
        restarted = await review_runtime(tmp_path)
        try:
            assert_persisted_interlock(restarted)
            await assert_read_only_interlock(restarted, monkeypatch)
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("cursor", [1, 2], ids=["remaining_step", "all_steps_done"])
def test_legacy_unknown_recovery_result_is_held_immediately(tmp_path, cursor):
    async def scenario():
        plugin = UnsafeRecoveryPlugin()
        runtime = await review_runtime(tmp_path, plugin=plugin)
        try:
            task = runtime.submit_task(
                probe_payload([{"is_action": True}, {"is_action": True}]),
                source="console", context={"operator_id": "review"},
            )
            task.update(
                status="executing", step_cursor=cursor,
                plan=[
                    make_step(number, f"probe_{number}", f"Probe {number}",
                              is_action=True, required_capabilities=["arm.home"])
                    for number in (1, 2)
                ],
                step_log=[
                    {"step_no": number, "phase": "done", "is_action": True,
                     "outcome": "failed" if number == cursor else "success"}
                    for number in range(1, cursor + 1)
                ],
                recovery_attempted=True,
                recovery_result={
                    "recovered": False, "home_verified": False, "outcome": "unknown",
                },
            )
            assert not task.get("action_receipt")
            assert not task.get("finalization_phase")
            assert not task.get("motion_result_unknown")
            runtime.store.save_task(task)
            runtime.recover_tasks()
            # Assert synchronously, before a missing-lease executor can mask replay.
            recovered = runtime.store.get_task(task["task_id"])
            assert recovered["status"] == "unknown"
            assert recovered["home_verified"] is False
            assert runtime._exec_tasks == {}
            assert plugin.hooks == []
            assert plugin.step_executions == []
            assert runtime.devices.get("arm-01")["unknown_hold_task_id"] == task["task_id"]
            assert_persisted_interlock(runtime)
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_default_recovery_log_unknown_does_not_invent_mechanical_uncertainty(tmp_path):
    async def scenario():
        runtime = await review_runtime(tmp_path)
        try:
            task = runtime.submit_task(
                probe_payload([{"is_action": True}, {"is_action": True}]),
                source="console", context={"operator_id": "review"},
            )
            recovery = {"recovered": False, "message": "Default recovery requested stop"}
            task.update(
                status="executing", step_cursor=1, recovery_attempted=True,
                plan=[{"step_no": 1}, {"step_no": 2}],
                step_log=[{"step_no": 1, "phase": "done", "outcome": "failed"}],
                recovery_result=recovery,
                recovery_log=[{"outcome": "unknown", "detail": recovery}],
            )
            runtime.store.save_task(task)
            runtime.recover_tasks()
            assert runtime.store.get_task(task["task_id"])["status"] == "queued"
            assert runtime.devices.get("arm-01")["status"] == "ready"
            assert not runtime.devices.get("arm-01").get("unknown_hold_task_id")
            assert runtime._exec_tasks == {}
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_standalone_confirmed_home_does_not_create_unknown_interlock(tmp_path, monkeypatch):
    async def scenario():
        runtime = await review_runtime(tmp_path)
        calls = []

        async def home():
            calls.append("go_home")
            return {"ok": True}

        monkeypatch.setattr(runtime.devices.driver("arm-01"), "go_home", home)
        try:
            result = await runtime.op_home_check("arm-01", op_context())
            assert result["home_ok"] is True
            assert calls == ["go_home"]
            assert runtime.op_resume("arm-01", op_context())["status"] == "ready"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_standalone_not_started_home_rejection_allows_known_retry(tmp_path, monkeypatch):
    async def scenario():
        runtime = await review_runtime(tmp_path)
        calls = []

        async def home():
            calls.append("go_home")
            if len(calls) == 1:
                error = DriverError("driver_uncalibrated", "Rejected before driver motion")
                error.motion_not_started = True
                raise error
            return {"ok": True}

        monkeypatch.setattr(runtime.devices.driver("arm-01"), "go_home", home)
        try:
            rejected = await runtime.op_home_check("arm-01", op_context())
            assert rejected["home_ok"] is False
            assert not rejected["detail"].get("motion_result_unknown")
            confirmed = await runtime.op_home_check("arm-01", op_context())
            assert confirmed["home_ok"] is True
            assert calls == ["go_home", "go_home"]
            assert runtime.op_resume("arm-01", op_context())["status"] == "ready"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
