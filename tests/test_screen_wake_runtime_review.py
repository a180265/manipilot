"""Independent runtime review; temporary roots and local fake daemon only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import socket
import threading

import pytest

from drivers import calibration_bundle as cb
from drivers.daemon_arm import DaemonArmDriver
from drivers.wake_screen import resolve_wake_target
from runtime.calibration_gate import check_live_calibration
from runtime.errors import ApiError
from runtime.util import iso, utc_now
from tests.helpers import make_device_configs, make_runtime, op_context, wait_terminal
from tests.test_daemon_arm import FakeDaemon
from tests.test_screen_gesture_driver import gesture_bundle
from tests.test_screen_wake_contract import validator, wake_request
from tests.test_screen_wake_driver import validated_bundle, wake_frame
from tests.test_screen_wake_runtime import PLUGIN_DIR


# Reaching later checkpoints includes several real, multi-frame CV gates.
CHECKPOINT_TIMEOUT_S = 10


@pytest.fixture(autouse=True)
def only_review_fake_connections(monkeypatch):
    allowed_ports = set()
    original_start = FakeDaemon.start
    original_connect = socket.socket.connect
    monkeypatch.setattr("runtime.calibration_gate.EDGE_CONSENSUS_INTERVAL_S", 0)

    def start(daemon):
        allowed_ports.add(daemon.port)
        return original_start(daemon)

    def connect(connection, address):
        assert (
            isinstance(address, tuple)
            and address[0] == "127.0.0.1"
            and address[1] in allowed_ports
        ), f"Review cannot contact live endpoints: {address!r}"
        return original_connect(connection, address)

    monkeypatch.setattr(FakeDaemon, "start", start)
    monkeypatch.setattr(socket.socket, "connect", connect)


@asynccontextmanager
async def real_driver_runtime(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.capabilities.bootstrap()
    await runtime.devices.register_all(make_device_configs())
    daemon = FakeDaemon(mpos=(-52, 49, -8)).start()
    root = tmp_path / "data"
    driver = DaemonArmDriver(
        "arm-01", daemon_addr=f"127.0.0.1:{daemon.port}", data_root=root)
    bundle = validated_bundle(gesture_bundle())
    bundle["daemonAddr"] = driver.daemon_addr
    bundle["bindingFingerprint"] = cb.binding_fingerprint(
        device_id="arm-01", camera_id=bundle["cameraId"],
        camera_device_unique_id=bundle["cameraDeviceUniqueId"],
        daemon_addr=driver.daemon_addr,
        frame_width=bundle["frame"]["width"], frame_height=bundle["frame"]["height"])
    cb.save_bundle(bundle, root)
    binding = resolve_wake_target(bundle)["binding"]

    class Frames:
        sequence = 0

        def capture_sync(self):
            self.sequence += 1
            frame = wake_frame(
                bundle, sequence=self.sequence,
                lit=any(cmd.startswith("TAP2 ") for cmd in daemon.commands))
            return {
                **frame, "image": frame["image_bytes"],
                "captured_at": frame["source_captured_at"],
                "source_received_at": frame["source_captured_at"],
                "sha256": hashlib.sha256(frame["image_bytes"]).hexdigest(),
            }

    try:
        await driver.connect()
        runtime.devices._arms["arm-01"] = driver
        runtime.devices._cameras["arm-01"] = Frames()
        runtime.devices.get("arm-01").update(
            binding_mode="real", daemon_addr=driver.daemon_addr,
            daemon_session=binding["daemon_session"],
            camera_id=binding["camera_id"],
            camera_device_unique_id=binding["camera_device_unique_id"],
            camera_binding_revision=binding["binding_revision"],
            expected_frame_width=binding["frame_width"],
            expected_frame_height=binding["frame_height"],
            camera_quality="waiting", camera_frame_age_ms=0)

        async def gate():
            state = await driver.read_state()
            return await check_live_calibration(
                device_id="arm-01", camera_id=binding["camera_id"],
                daemon_addr=driver.daemon_addr, daemon_session=state["session"],
                estop_locked=bool(
                    state.get("estop_latched") or state.get("daemon_estop_locked")),
                camera=runtime.devices.camera_broker("arm-01"),
                bundle=cb.load_bundle("arm-01", root), screen_wake=True,
                require_pose=True)

        driver.screen_wake_gate = gate
        runtime.plugins.install(PLUGIN_DIR, "reviewer", "isolated review")
        runtime.plugins.activate("screen-wake-adapter", "reviewer", "isolated review")
        request = wake_request(payload={"binding": binding, "confirmed": True})
        yield runtime, request, daemon, driver, cb.bundle_path("arm-01", root)
    finally:
        for task in runtime._exec_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*runtime._exec_tasks.values(), return_exceptions=True)
        await driver.disconnect()
        daemon.stop()


@pytest.mark.parametrize(
    "boundary", ["preflight", "before_double_tap", "controller_preflight",
                 "after_double_tap"])
@pytest.mark.parametrize(
    "revocation", ["cancel", "request_expiry", "lease_expiry", "lease_replacement"])
def test_review_runtime_revocation_during_driver_await_prevents_motion(
        tmp_path, monkeypatch, revocation, boundary):
    async def scenario():
        async with real_driver_runtime(tmp_path) as (
                runtime, request, daemon, driver, bundle_path):
            entered, resume = asyncio.Event(), asyncio.Event()
            original_gate = driver.screen_wake_gate
            original_call = driver._call
            baseline = bundle_path.read_bytes()
            refresh_calls = 0

            async def held_gate():
                at_boundary = boundary == "preflight" or (
                    boundary == "before_double_tap"
                    and any(cmd.startswith("Z ") for cmd in daemon.commands)) or (
                    boundary == "after_double_tap"
                    and any(cmd.startswith("TAP2 ") for cmd in daemon.commands))
                if at_boundary and not entered.is_set():
                    entered.set()
                    await resume.wait()
                return await original_gate()

            async def held_call(func, *args, **kwargs):
                nonlocal refresh_calls
                result = await original_call(func, *args, **kwargs)
                if func == driver._refresh_session:
                    refresh_calls += 1
                    # The second refresh follows the frame gate, just before XY.
                    if boundary == "controller_preflight" and refresh_calls == 2:
                        entered.set()
                        await resume.wait()
                return result

            driver.screen_wake_gate = held_gate
            monkeypatch.setattr(driver, "_call", held_call)
            task = runtime.submit_task(request, source="console", context={})
            await runtime._dispatch_once()
            try:
                await asyncio.wait_for(entered.wait(), CHECKPOINT_TIMEOUT_S)
                commands_before = list(daemon.commands)
                if boundary == "after_double_tap":
                    assert commands_before[-1].startswith("TAP2 ")
                else:
                    assert not any(cmd.startswith("TAP2 ") for cmd in commands_before)
                if boundary in {"preflight", "controller_preflight"}:
                    assert not commands_before
                else:
                    assert any(cmd.startswith("XY ") for cmd in commands_before)
                    assert any(cmd.startswith("Z ") for cmd in commands_before)
                if revocation == "cancel":
                    await runtime.cancel_task(task["task_id"], op_context())
                elif revocation == "request_expiry":
                    after_expiry = utc_now() + timedelta(seconds=61)
                    monkeypatch.setattr("runtime.screen_wake.utc_now", lambda: after_expiry)
                elif revocation == "lease_expiry":
                    lease = runtime.leases.get("arm-01")
                    lease["expires_at"] = iso(utc_now() - timedelta(seconds=1))
                    runtime.store.save_lease(lease)
                else:
                    lease = runtime.leases.get("arm-01")
                    runtime.leases.release("arm-01", lease["lease_id"])
                    replacement = runtime.leases.acquire("arm-01", "replacement-task")
                resume.set()
                result = await wait_terminal(runtime, task["task_id"], timeout=10)
                await asyncio.wait_for(runtime._exec_tasks[task["task_id"]], 2)
                if revocation == "lease_replacement":
                    assert runtime.leases.get("arm-01")["lease_id"] == replacement["lease_id"]
                    assert runtime.devices.get("arm-01")["status"] == "busy"
                assert bundle_path.read_bytes() == baseline
                commands_after = daemon.commands[len(commands_before):]
                forbidden = commands_after if boundary != "before_double_tap" else [
                    cmd for cmd in commands_after if cmd.startswith("TAP2 ")]
                assert not forbidden, (
                    f"{revocation} at {boundary}: commands before={commands_before!r}, "
                    f"after={commands_after!r}; task ended as {result['status']}"
                )
                assert result["status"] in {"failed", "cancelled", "expired", "unknown"}
                if commands_before:
                    assert result["status"] == "unknown"
                    assert result["motion_not_started"] is False
                    assert result["effect_status"] == "unverified"
                else:
                    assert result["motion_not_started"] is True
            finally:
                resume.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["preflight", "before_double_tap"])
@pytest.mark.parametrize("change", [
    "paused", "quarantined", "binding_disabled", "camera_revision",
    "camera_identity", "daemon_session", "device_capability", "camera_frozen",
    "home_unverified", "stop_unknown",
])
def test_review_runtime_guard_rechecks_dynamic_safety(tmp_path, change, boundary):
    async def scenario():
        async with real_driver_runtime(tmp_path) as (
                runtime, request, daemon, driver, bundle_path):
            entered, resume = asyncio.Event(), asyncio.Event()
            original_gate = driver.screen_wake_gate
            baseline = bundle_path.read_bytes()

            async def held_gate():
                at_boundary = boundary == "preflight" or any(
                    cmd.startswith("Z ") for cmd in daemon.commands)
                if at_boundary and not entered.is_set():
                    entered.set()
                    await resume.wait()
                return await original_gate()

            driver.screen_wake_gate = held_gate
            task = runtime.submit_task(request, source="console", context={})
            await runtime._dispatch_once()
            try:
                await asyncio.wait_for(entered.wait(), CHECKPOINT_TIMEOUT_S)
                commands_before = list(daemon.commands)
                assert bool(commands_before) is (boundary == "before_double_tap")
                assert not any(cmd.startswith("TAP2 ") for cmd in commands_before)
                device = runtime.devices.get("arm-01")
                changes = {
                    "binding_disabled": ("binding_enabled", False),
                    "camera_revision": (
                        "camera_binding_revision", device["camera_binding_revision"] + 1),
                    "camera_identity": ("camera_device_unique_id", "replacement-camera"),
                    "daemon_session": ("daemon_session", "replacement-session"),
                    "camera_frozen": ("camera_frozen", True),
                    "home_unverified": ("home_ok", False),
                }
                if change == "paused":
                    runtime.devices.pause("arm-01", "review pause")
                elif change == "quarantined":
                    runtime.devices.quarantine("arm-01", "review quarantine")
                elif change == "device_capability":
                    device["capabilities"].remove("arm.wake_screen")
                elif change == "stop_unknown":
                    stored = runtime.get_task(task["task_id"])
                    stored["stop_result_unknown"] = True
                    runtime.store.save_task(stored)
                else:
                    key, value = changes[change]
                    device[key] = value
                resume.set()
                result = await wait_terminal(runtime, task["task_id"], timeout=10)
                await asyncio.wait_for(runtime._exec_tasks[task["task_id"]], 2)
                assert daemon.commands == commands_before, (
                    f"{change} at {boundary}: {daemon.commands!r}, result={result['status']}")
                assert result["status"] == (
                    "unknown" if commands_before or change == "stop_unknown" else "failed")
                assert result["motion_not_started"] is (not commands_before)
                assert result["effect_status"] == "unverified"
                assert result["home_verified"] is False
                assert runtime.leases.get("arm-01") is None
                assert bundle_path.read_bytes() == baseline
                assert device["calibration_status"] == "validated"
                if change in {"paused", "quarantined"}:
                    assert device["status"] == change
                elif change == "device_capability":
                    assert "arm.wake_screen" not in device["capabilities"]
                elif change in changes:
                    key, value = changes[change]
                    assert device[key] == value
            finally:
                resume.set()

    asyncio.run(scenario())


def test_review_plugin_deactivation_cannot_change_permissions_during_wake(tmp_path):
    async def scenario():
        async with real_driver_runtime(tmp_path) as (runtime, request, daemon, _, _):
            task = runtime.submit_task(request, source="console", context={})
            with pytest.raises(ApiError) as error:
                runtime.plugins.deactivate("screen-wake-adapter", "reviewer", "review")
            assert error.value.key == "plugin_tasks_running"
            assert runtime.plugins.get("screen-wake-adapter")["status"] == "activated"
            assert not daemon.commands
            await runtime._dispatch_once()
            result = await wait_terminal(runtime, task["task_id"], timeout=10)
            await asyncio.wait_for(runtime._exec_tasks[task["task_id"]], 2)
            assert result["status"] == "confirmed"
            assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("queued_command", ["XY ", "TAP2 "], ids=["first_motion", "tap2"])
@pytest.mark.parametrize("revocation", [
    "cancel", "request_expiry", "lease_expiry", "lease_release", "lease_replacement",
])
def test_review_worker_queue_revocation_prevents_transport_send(
        tmp_path, monkeypatch, queued_command, revocation):
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        async with real_driver_runtime(tmp_path) as (
                runtime, request, daemon, driver, bundle_path):
            entered, release = asyncio.Event(), threading.Event()
            original_call = driver._call
            blocker = None
            baseline = bundle_path.read_bytes()

            def occupy_worker():
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(10), "Review worker queue was not released"

            async def queued_call(func, *args, **kwargs):
                nonlocal blocker
                if (blocker is None and args and isinstance(args[0], str)
                        and args[0].startswith(queued_command)):
                    # The real _call queues immediately behind this occupied worker.
                    blocker = loop.run_in_executor(None, occupy_worker)
                return await original_call(func, *args, **kwargs)

            monkeypatch.setattr(driver, "_call", queued_call)
            task = runtime.submit_task(request, source="console", context={})
            await runtime._dispatch_once()
            try:
                await asyncio.wait_for(entered.wait(), CHECKPOINT_TIMEOUT_S)
                assert blocker is not None and not blocker.done()
                commands_before = list(daemon.commands)
                assert not any(command.startswith(queued_command)
                               for _, command in daemon.requests)
                if queued_command == "XY ":
                    assert not commands_before
                else:
                    assert any(command.startswith("XY ") for command in commands_before)
                    assert any(command.startswith("Z ") for command in commands_before)
                if revocation == "cancel":
                    await runtime.cancel_task(task["task_id"], op_context())
                elif revocation == "request_expiry":
                    after_expiry = utc_now() + timedelta(seconds=61)
                    monkeypatch.setattr("runtime.screen_wake.utc_now", lambda: after_expiry)
                elif revocation == "lease_expiry":
                    lease = runtime.leases.get("arm-01")
                    lease["expires_at"] = iso(utc_now() - timedelta(seconds=1))
                    runtime.store.save_lease(lease)
                else:
                    lease = runtime.leases.get("arm-01")
                    runtime.leases.release("arm-01", lease["lease_id"])
                    if revocation == "lease_replacement":
                        replacement = runtime.leases.acquire("arm-01", "replacement-task")
                release.set()
                result = await wait_terminal(runtime, task["task_id"], timeout=10)
                await asyncio.wait_for(runtime._exec_tasks[task["task_id"]], 2)
                assert daemon.commands == commands_before
                assert not any(command.startswith(queued_command)
                               for _, command in daemon.requests), daemon.requests
                assert not any(command.startswith("TAP2 ") for _, command in daemon.requests)
                assert result["status"] in {"failed", "cancelled", "expired", "unknown"}
                assert result["effect_status"] == "unverified"
                if commands_before:
                    assert result["status"] == "unknown"
                    assert result["motion_not_started"] is False
                else:
                    assert result["motion_not_started"] is True
                    assert result["status"] in {"failed", "cancelled", "expired"}, result
                    if revocation == "cancel":
                        assert result["status"] == "cancelled"
                        assert result["cancellation_handled"] is True
                        assert result.get("stop_attempt_count", 0) == 0
                        assert not any(command == "STOP" for _, command in daemon.requests)
                if revocation == "lease_replacement":
                    assert runtime.leases.get("arm-01")["lease_id"] == replacement["lease_id"]
                    assert runtime.devices.get("arm-01")["status"] == "busy"
                assert bundle_path.read_bytes() == baseline
                assert runtime.submit_task(request, source="console", context={})["status"] == result["status"]
                await runtime._dispatch_once()
                assert daemon.commands == commands_before
            finally:
                release.set()
                if blocker is not None:
                    await asyncio.wait_for(blocker, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("blocker", ["estop", "stop_unknown"])
def test_review_zero_motion_cancel_does_not_exempt_estop_or_unknown_stop(
        tmp_path, blocker):
    async def scenario():
        async with real_driver_runtime(tmp_path) as (
                runtime, request, daemon, driver, bundle_path):
            entered, resume = asyncio.Event(), asyncio.Event()
            original_gate = driver.screen_wake_gate
            baseline = bundle_path.read_bytes()

            async def held_gate():
                if not entered.is_set():
                    entered.set()
                    await resume.wait()
                return await original_gate()

            driver.screen_wake_gate = held_gate
            task = runtime.submit_task(request, source="console", context={})
            await runtime._dispatch_once()
            try:
                await asyncio.wait_for(entered.wait(), CHECKPOINT_TIMEOUT_S)
                assert not daemon.commands
                await runtime.cancel_task(task["task_id"], op_context())
                if blocker == "estop":
                    runtime.store.kv_set("estop_active", "1")
                else:
                    stored = runtime.get_task(task["task_id"])
                    stored["stop_result_unknown"] = True
                    runtime.store.save_task(stored)
                resume.set()
                result = await wait_terminal(runtime, task["task_id"], timeout=10)
                await asyncio.wait_for(runtime._exec_tasks[task["task_id"]], 2)
                assert result["status"] == "unknown"
                assert result["motion_not_started"] is True
                assert result["effect_status"] == "unverified"
                assert result.get("stop_attempt_count") == 1
                assert result["cancel_result"]["stopped"] is True
                assert sum(command == "STOP" for _, command in daemon.requests) == 1
                assert not daemon.commands
                assert runtime.devices.get("arm-01")["status"] == "paused"
                assert bundle_path.read_bytes() == baseline
                if blocker == "estop":
                    assert runtime.estop.active
                else:
                    assert result["stop_result_unknown"] is True
            finally:
                resume.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["after_xy", "after_double_tap"])
def test_review_executor_interruption_recovers_unknown_without_more_motion(
        tmp_path, boundary):
    async def scenario():
        async with real_driver_runtime(tmp_path) as (
                runtime, request, daemon, driver, bundle_path):
            entered, resume = asyncio.Event(), asyncio.Event()
            original_gate = driver.screen_wake_gate
            baseline = bundle_path.read_bytes()
            command_prefix = "XY " if boundary == "after_xy" else "TAP2 "

            async def held_gate():
                if any(cmd.startswith(command_prefix) for cmd in daemon.commands):
                    entered.set()
                    await resume.wait()
                return await original_gate()

            driver.screen_wake_gate = held_gate
            task = runtime.submit_task(request, source="console", context={})
            await runtime._dispatch_once()
            try:
                await asyncio.wait_for(entered.wait(), CHECKPOINT_TIMEOUT_S)
                commands_before = list(daemon.commands)
                executor = runtime._exec_tasks[task["task_id"]]
                executor.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(executor, 2)
                assert daemon.commands == commands_before
                assert not driver.connected
                runtime.recover_tasks()
                result = runtime.get_task(task["task_id"])
                assert result["status"] == "unknown"
                assert result["home_verified"] is False
                assert runtime.devices.get("arm-01")["status"] == "paused"
                assert runtime.devices.get("arm-01")["calibration_status"] == "validated"
                assert runtime.devices.get("arm-01")["home_ok"] is False
                assert bundle_path.read_bytes() == baseline
                assert runtime.submit_task(request, source="console", context={})["status"] == "unknown"
                await runtime._dispatch_once()
                assert daemon.commands == commands_before
            finally:
                resume.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("lost_command", ["TAP2 ", "END_MOTION", "RELEASE"])
def test_review_lost_motion_or_cleanup_reply_is_unknown_without_replay(
        tmp_path, lost_command):
    async def scenario():
        async with real_driver_runtime(tmp_path) as (
                runtime, request, daemon, driver, bundle_path):
            baseline = bundle_path.read_bytes()
            daemon.response_hook = lambda command, reply, _: (
                None if command.startswith(lost_command) else reply)
            task = runtime.submit_task(request, source="console", context={})
            await runtime._dispatch_once()
            result = await wait_terminal(runtime, task["task_id"], timeout=10)
            await asyncio.wait_for(runtime._exec_tasks[task["task_id"]], 2)
            assert result["status"] == "unknown"
            assert result["motion_not_started"] is False
            assert result["effect_status"] == "unverified"
            assert result["home_verified"] is False
            assert result["action_call_count"] == 1
            assert runtime.devices.get("arm-01")["status"] == "paused"
            assert runtime.devices.get("arm-01")["calibration_status"] == "validated"
            assert runtime.leases.get("arm-01") is None
            assert bundle_path.read_bytes() == baseline
            commands = list(daemon.commands)
            assert len([cmd for cmd in commands if cmd.startswith("TAP2 ")]) == 1
            if lost_command == "TAP2 ":
                assert commands[-1].startswith("TAP2 ")
            assert not any(cmd.startswith(("SWIPE", "PATH1", "$H")) for cmd in commands)
            assert runtime.submit_task(request, source="console", context={})["status"] == "unknown"
            runtime.recover_tasks()
            await runtime._dispatch_once()
            assert daemon.commands == commands

    asyncio.run(scenario())


def test_review_real_driver_positive_control_keeps_evidence_and_does_not_replay(tmp_path):
    async def scenario():
        async with real_driver_runtime(tmp_path) as (
                runtime, request, daemon, driver, bundle_path):
            device = runtime.devices.get("arm-01")
            protected = {key: deepcopy(device.get(key)) for key in (
                "calibration_status", "home_ok", "current_ui_state", "daemon_session")}
            baseline = bundle_path.read_bytes()
            task = runtime.submit_task(request, source="console", context={})
            await runtime._dispatch_once()
            result = await wait_terminal(runtime, task["task_id"], timeout=10)
            await asyncio.wait_for(runtime._exec_tasks[task["task_id"]], 2)
            assert result["status"] == "confirmed"
            assert result["effect_status"] == "awake"
            assert result["controller_completed"] is True and result["tap_count"] == 2
            assert result["home_verified"] is False
            assert result["action_call_count"] == 1
            assert runtime.leases.get("arm-01") is None
            assert device["status"] == "ready"
            assert {key: device.get(key) for key in protected} == protected
            assert bundle_path.read_bytes() == baseline
            detail = next(entry["detail"] for entry in result["step_log"]
                          if entry["phase"] == "done" and entry["action"] == "wake_screen")
            validator("resultDetail").validate(detail)
            before, after = detail["before_evidence"], detail["after_evidence"]
            assert before["evidence_id"] != after["evidence_id"]
            assert before["sha256"] != after["sha256"]
            for reference in (before, after):
                record = runtime.evidence.get(reference["evidence_id"])
                assert record["task_id"] == task["task_id"]
                assert record["device_id"] == "arm-01"
                assert record["sha256"] == reference["sha256"]
            observed = driver.action_log[-1]["result"]
            assert observed["motion_verified"] is False
            assert all(phase["verification"]["verified"] is False
                       for phase in observed["phases"])
            commands = list(daemon.commands)
            assert len([cmd for cmd in commands if cmd.startswith("TAP2 ")]) == 1
            assert not any(cmd.startswith(("SWIPE", "PATH1", "$H")) for cmd in commands)
            assert runtime.submit_task(request, source="console", context={})["status"] == "confirmed"
            await runtime._dispatch_once()
            assert daemon.commands == commands

    asyncio.run(scenario())


@pytest.mark.parametrize("current_fault", [
    "none", "motion_unknown", "stop_unknown", "paused", "quarantined",
    "returning_home", "device_estop", "global_estop",
    "awaiting_device", "validating", "executing",
    "lease", "expired_lease", "binding", "daemon_session", "daemon_estop", "daemon_lease",
])
def test_review_history_is_preserved_but_current_safety_still_gates_real_driver(
        tmp_path, current_fault):
    from runtime.screen_wake import wake_metadata

    async def scenario():
        async with real_driver_runtime(tmp_path) as (
                runtime, request, daemon, _, bundle_path):
            history = []
            for index in range(23):
                previous = deepcopy(request)
                previous.update(
                    task_id=f"old-self-test-{index}", idempotency_key=f"old-key-{index}",
                    task_type="gesture_self_test", plugin_id="gesture-validation-adapter",
                    status="unknown", reviewed_at=None,
                    created_at=iso(utc_now() - timedelta(days=6)),
                    finished_at=iso(utc_now() - timedelta(days=5)),
                    action_call_count=[0, 1, None][index % 3],
                )
                previous["payload"]["binding"]["daemon_session"] = "old-session"
                runtime.store.save_task(previous)
                history.append(deepcopy(runtime.get_task(previous["task_id"])))
            baseline = bundle_path.read_bytes()
            device = runtime.devices.get("arm-01")
            if current_fault in {"motion_unknown", "stop_unknown"}:
                device[{"motion_unknown": "motion_result_unknown",
                        "stop_unknown": "stop_result_unknown"}[current_fault]] = True
            elif current_fault in {"paused", "quarantined", "returning_home"}:
                device["status"] = current_fault
            elif current_fault == "device_estop":
                device["estop_latched"] = True
            elif current_fault == "global_estop":
                runtime.store.kv_set("estop_active", "1")
            elif current_fault in {"awaiting_device", "validating", "executing"}:
                active = deepcopy(history[0])
                active.update(task_id="current-work", idempotency_key="current-key",
                              status=current_fault, finished_at=None)
                runtime.store.save_task(active)
            elif current_fault in {"lease", "expired_lease"}:
                lease = runtime.leases.acquire("arm-01", "current-work")
                if current_fault == "expired_lease":
                    lease["expires_at"] = iso(utc_now() - timedelta(seconds=1))
                    runtime.store.save_lease(lease)
            elif current_fault == "binding":
                device["camera_binding_revision"] += 1
            elif current_fault == "daemon_session":
                daemon.session = "changed-session"
            elif current_fault == "daemon_estop":
                daemon.estop_locked = True
            elif current_fault == "daemon_lease":
                daemon.lease = {
                    "conn_id": -1, "lease_id": "external-controller-lease",
                    "owner_type": "review", "owner_id": "other-owner",
                    "task_id": "other-work", "expires_at": utc_now().timestamp() + 300,
                }
            before_metadata = deepcopy(device)
            metadata = await wake_metadata(runtime, "arm-01")
            assert metadata["usable"] is (current_fault == "none"), metadata
            assert runtime.devices.get("arm-01") == before_metadata
            assert not daemon.commands
            if current_fault == "none" or current_fault.startswith("daemon_"):
                task = runtime.submit_task(request, source="console", context={})
                await runtime._dispatch_once()
                result = await wait_terminal(runtime, task["task_id"], timeout=10)
                await asyncio.wait_for(runtime._exec_tasks[task["task_id"]], 2)
                if current_fault == "none":
                    assert result["status"] == "confirmed", result
                    assert sum(cmd.startswith("TAP2 ") for cmd in daemon.commands) == 1
                    commands = list(daemon.commands)
                    runtime.recover_tasks()
                    assert runtime.submit_task(request, source="console", context={})["status"] == "confirmed"
                    await runtime._dispatch_once()
                    assert daemon.commands == commands
                else:
                    assert result["status"] == "failed", result
                    assert result["motion_not_started"] is True
                    assert not daemon.commands
                    if current_fault == "daemon_lease":
                        assert daemon.lease["lease_id"] == "external-controller-lease"
            else:
                with pytest.raises(ApiError):
                    runtime.submit_task(request, source="console", context={})
                assert not daemon.commands
            assert bundle_path.read_bytes() == baseline
            for previous in history:
                assert runtime.get_task(previous["task_id"]) == previous

    asyncio.run(scenario())
