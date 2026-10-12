"""Isolated probes of shipped sampler callbacks, not full runtime wiring.

ARM_FLEET_SAMPLER_PLUGIN_SOURCE can select an installed plugin for a red run.
Only the callback construction block is compiled, unchanged, in memory: no
plugin imports, services, calibration files, evidence writes or hardware.
"""
import ast
import asyncio
import json
import math
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples/plugins/gesture-validation-adapter/plugin.py"
REGION = {"leftPx": 0, "topPx": 0, "widthPx": 100, "heightPx": 200}


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Sampler probes must not access services or hardware")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(os, "system", denied)


class SamplingClock:
    """Advance only the sampler's waits, never global asyncio."""

    def __init__(self):
        self.waits = asyncio.Queue()
        self.samplers = []
        self.timeouts = []

    def create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.samplers.append(task)
        return task

    async def wait_for(self, awaitable, timeout):
        self.timeouts.append(timeout)
        stopped = asyncio.create_task(awaitable)
        tick = asyncio.get_running_loop().create_future()
        self.waits.put_nowait(tick)
        try:
            done, _ = await asyncio.wait(
                (stopped, tick), return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                return stopped.result()
            raise asyncio.TimeoutError
        finally:
            tick.cancel()
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    async def next_wait(self):
        return await asyncio.wait_for(self.waits.get(), timeout=1)


def callbacks(ctx, clock):
    path = Path(os.environ.get("ARM_FLEET_SAMPLER_PLUGIN_SOURCE", SOURCE))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    plugin = next(node for node in tree.body
                  if isinstance(node, ast.ClassDef) and node.name == "Plugin")
    step = next(node for node in plugin.body
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "execute_step")

    def assigns(node, name):
        return isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets)

    start = next(i for i, node in enumerate(step.body)
                 if assigns(node, "scroll_frames"))
    end = next(i for i, node in enumerate(step.body) if assigns(node, "runner"))
    factory = ast.parse(
        "def build(ctx, defaults):\n"
        "    return execute_swipe, measure_scroll\n")
    helper = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_scroll_sample_interval_seconds")
    factory.body.insert(0, helper)
    factory.body[-1].body[:0] = step.body[start:end]
    interval = next(ast.literal_eval(node.value) for node in tree.body
                    if assigns(node, "SCROLL_FRAME_INTERVAL_SECONDS"))
    namespace = {
        "asyncio": SimpleNamespace(
            Event=asyncio.Event, TimeoutError=asyncio.TimeoutError,
            create_task=clock.create_task, wait_for=clock.wait_for,
            gather=asyncio.gather),
        "math": math,
        "launcher": None,
        "run_id": "run-isolated-sampler",
        "DOUBLE_TAP_INTERVAL_MS": 50,
        "SCROLL_FRAME_INTERVAL_SECONDS": interval,
        "SCROLL_FRAME_INTERVAL_MIN_SECONDS": 0.08,
        "SCROLL_FRAME_INTERVAL_MAX_SECONDS": 0.5,
        "SCROLL_POST_MOTION_RESERVE_FRAMES": 8,
        "SCROLL_FRAME_INTERVAL_RESERVE_FRAMES": 1,
    }
    exec(compile(ast.fix_missing_locations(factory), str(path), "exec"), namespace)
    defaults = json.loads((ROOT / "configs/gesture_defaults.json").read_text())
    assert defaults["scroll_observation"]["max_frame_chain"] == 30
    return namespace["build"](ctx, defaults)


class FakeContext:
    def __init__(self, *, suspended_capture=False, motion_error=None):
        self.suspended_capture = suspended_capture
        self.motion_error = motion_error
        self.motion_started = asyncio.Event()
        self.finish_motion = asyncio.Event()
        self.motion_ended = asyncio.Event()
        self.capture_entered = asyncio.Event()
        self.capture_exited = asyncio.Event()
        self.release_capture = asyncio.Event()
        self.motion_calls = 0
        self.motion_cancelled = False
        self.capture_cancelled = False
        self.frame_ids = []
        self.measurements = []
        self.motion_result = {"controller_status": "completed",
                              "motion_not_started": False}

    async def capture_evidence(self, label):
        self.capture_entered.set()
        try:
            if self.suspended_capture:
                await self.release_capture.wait()
            evidence_id = f"motion-{len(self.frame_ids) + 1}"
            self.frame_ids.append(evidence_id)
            return {"evidence_id": evidence_id}
        except asyncio.CancelledError:
            self.capture_cancelled = True
            raise
        finally:
            self.capture_exited.set()

    async def call_capability(self, capability_id, params):
        if capability_id == "arm.execute_gesture_program":
            self.motion_calls += 1
            self.motion_started.set()
            try:
                await self.finish_motion.wait()
                if self.motion_error is not None:
                    raise self.motion_error
                return self.motion_result
            except asyncio.CancelledError:
                self.motion_cancelled = True
                raise
            finally:
                self.motion_ended.set()
        assert capability_id == "camera.measure_scroll", capability_id
        self.measurements.append(params)
        return {"status": "displaced", "dy_px": -120}


async def reap(parent, clock):
    # Even a red run must leave no probe tasks behind.
    for task in (parent, *clock.samplers):
        if not task.done():
            task.cancel()
    await asyncio.gather(parent, *clock.samplers, return_exceptions=True)


def assert_no_probe_tasks(baseline):
    assert asyncio.all_tasks() - baseline == set()


@pytest.mark.parametrize("overflow", [False, True], ids=["exactly-30", "over-budget"])
def test_frame_budget_keeps_endpoints_and_never_replays_motion(overflow):
    async def run():
        baseline = asyncio.all_tasks()
        ctx, clock = FakeContext(), SamplingClock()
        execute, measure = callbacks(ctx, clock)
        parent = asyncio.create_task(execute({
            "plan_id": "one-motion",
            "estimated_duration_ms": 5000,
        }))
        try:
            await asyncio.wait_for(ctx.motion_started.wait(), timeout=1)
            for index in range(28):
                tick = await clock.next_wait()
                assert len(ctx.frame_ids) == index + 1
                if index < 27:
                    tick.set_result(None)

            assert not clock.samplers[0].done()
            assert not parent.done()
            assert clock.timeouts
            assert clock.timeouts[0] >= 5 / 27
            if overflow:
                # The 29th sampling instant exhausts the budget, not frame 28.
                tick.set_result(None)
                done, _ = await asyncio.wait(clock.samplers, timeout=0.1)
                assert len(ctx.frame_ids) == 28, "A 29th intermediate frame was captured"
                assert done == set(clock.samplers), "Budget did not stop sampling"
                assert not parent.done(), "Camera budget interrupted the started motion"
                assert not ctx.motion_cancelled

            ctx.finish_motion.set()
            assert await asyncio.wait_for(parent, timeout=1) is ctx.motion_result
            observation = await measure(
                {"evidence_id": "before"}, {"evidence_id": "after"}, REGION)
            if overflow:
                assert observation == {
                    "status": "unknown",
                    "reason_code": "camera_frame_budget_exhausted",
                }
                assert ctx.measurements == [], "No truncated or two-frame fallback"
            else:
                assert observation["status"] == "displaced"
                assert len(ctx.measurements) == 1
                request = ctx.measurements[0]
                assert request["evidence_ids"] == [
                    "before", *[f"motion-{i}" for i in range(1, 29)], "after"]
                assert len(request["evidence_ids"]) == 30
                assert request["before_evidence_id"] == "before"
                assert request["after_evidence_id"] == "after"
            assert ctx.motion_calls == 1
            assert all(task.done() for task in clock.samplers)
            assert_no_probe_tasks(baseline)
        finally:
            await reap(parent, clock)
        assert_no_probe_tasks(baseline)

    asyncio.run(run())


def test_five_second_motion_does_not_exhaust_frame_budget_early():
    async def run():
        baseline = asyncio.all_tasks()
        ctx, clock = FakeContext(), SamplingClock()
        execute, measure = callbacks(ctx, clock)
        parent = asyncio.create_task(execute({
            "plan_id": "full-duration-motion", "estimated_duration_ms": 5000,
        }))
        try:
            await asyncio.wait_for(ctx.motion_started.wait(), timeout=1)
            elapsed = 0.0
            for _ in range(26):
                tick = await clock.next_wait()
                interval = clock.timeouts[-1]
                elapsed += interval
                tick.set_result(None)
                await asyncio.sleep(0)
            assert elapsed >= 5.0, "Sampler budget ends before estimated motion"
            assert not clock.samplers[0].done()
            ctx.finish_motion.set()
            await asyncio.wait_for(parent, timeout=1)
            observation = await measure(
                {"evidence_id": "before"}, {"evidence_id": "after"}, REGION)
            assert observation["status"] == "displaced"
            assert ctx.motion_calls == 1
            assert_no_probe_tasks(baseline)
        finally:
            await reap(parent, clock)
    asyncio.run(run())


def test_short_plan_with_slow_capability_retains_camera_chain():
    async def run():
        baseline = asyncio.all_tasks()
        ctx, clock = FakeContext(), SamplingClock()
        execute, measure = callbacks(ctx, clock)
        parent = asyncio.create_task(execute({
            "plan_id": "slow-return", "estimated_duration_ms": 1700,
        }))
        try:
            await asyncio.wait_for(ctx.motion_started.wait(), timeout=1)
            elapsed = 0.0
            while True:
                tick = await clock.next_wait()
                interval = clock.timeouts[-1]
                if elapsed + interval >= 5.3:
                    break
                tick.set_result(None)
                elapsed += interval
                await asyncio.sleep(0)
            assert len(ctx.frame_ids) <= 28
            assert len(ctx.frame_ids) >= 20
            assert not clock.samplers[0].done()
            ctx.finish_motion.set()
            assert await asyncio.wait_for(parent, timeout=1) is ctx.motion_result
            observation = await measure(
                {"evidence_id": "before"}, {"evidence_id": "after"}, REGION)
            assert observation["status"] == "displaced"
            assert len(ctx.measurements) == 1
            assert ctx.measurements[0]["evidence_ids"] == [
                "before", *ctx.frame_ids, "after"]
            assert ctx.motion_calls == 1
            assert_no_probe_tasks(baseline)
        finally:
            await reap(parent, clock)
    asyncio.run(run())


class StartedMotionError(RuntimeError):
    motion_not_started = False
    reason_code = "motion_outcome_unknown"


@pytest.mark.parametrize("outcome", ["cancelled", "completed", "failed"])
def test_suspended_capture_is_reaped_without_masking_started_motion(outcome):
    async def run():
        baseline = asyncio.all_tasks()
        error = StartedMotionError("Motion started; its outcome is unknown")
        ctx = FakeContext(suspended_capture=True,
                          motion_error=error if outcome == "failed" else None)
        clock = SamplingClock()
        execute, _ = callbacks(ctx, clock)
        parent = asyncio.create_task(execute({
            "plan_id": "one-motion",
            "estimated_duration_ms": 5000,
        }))
        try:
            await asyncio.wait_for(ctx.motion_started.wait(), timeout=1)
            await asyncio.wait_for(ctx.capture_entered.wait(), timeout=1)
            if outcome == "cancelled":
                parent.cancel("operator cancelled after motion started")
            else:
                ctx.finish_motion.set()
            # wait(), unlike wait_for(), cannot rescue a broken cleanup by
            # injecting a second cancellation into the parent/sampler.
            done, _ = await asyncio.wait((parent,), timeout=0.1)
            assert parent in done, "Cleanup is still awaiting a suspended capture"
            if outcome == "cancelled":
                with pytest.raises(asyncio.CancelledError) as caught:
                    await parent
                assert caught.value.args == ("operator cancelled after motion started",)
                assert not getattr(caught.value, "motion_not_started", False)
                assert ctx.motion_cancelled
            elif outcome == "failed":
                with pytest.raises(StartedMotionError) as caught:
                    await parent
                assert caught.value is error
                assert caught.value.motion_not_started is False
                assert not ctx.motion_cancelled
            else:
                assert await parent is ctx.motion_result
                assert not ctx.motion_cancelled
            assert ctx.motion_ended.is_set()
            assert ctx.motion_calls == 1
            assert ctx.capture_cancelled
            assert ctx.capture_exited.is_set()
            assert ctx.frame_ids == []
            assert ctx.measurements == []
            assert not ctx.release_capture.is_set()
            assert all(task.done() for task in clock.samplers)
            assert_no_probe_tasks(baseline)
        finally:
            await reap(parent, clock)
        assert_no_probe_tasks(baseline)

    asyncio.run(run())
