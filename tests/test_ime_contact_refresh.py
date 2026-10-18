"""IME geometry may age during travel; renew only evidence before one contact.

Uses the real runtime/driver/geometry protocol and a TCP fake controller. No
production camera, daemon, IME, or mechanical arm is accessed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import uuid

import pytest

from drivers.arm import DriverError
from tests.test_r2_live_geometry_driver import (
    ARM_DEVICE, IME_DEVICE, _click_params, live_env,  # noqa: F401
)


async def _plan(env):
    ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, "ime_candidate_bar")
    gate = env.runtime._geometry_gates[ref["nonce"]]
    return ref, gate


async def _click(env, ref, gate):
    return await env.driver.click(dict(
        _click_params(ref, "ime_candidate_bar"),
        input_claim={"geometry_gate": gate}))


def test_slow_travel_refreshes_unchanged_geometry_before_one_tap(live_env, monkeypatch):
    import runtime.ime_geometry as geom_mod

    async def scenario():
        env = live_env
        offset = [0.0]
        real_now = geom_mod._now
        monkeypatch.setattr(geom_mod, "_now", lambda: real_now() + offset[0])
        waits = []
        original_wait = env.geometry.wait_checked_snapshot
        transport = env.driver._transport

        def checked_wait(*args, **kwargs):
            # The phone must be able to answer without holding transport I/O.
            assert not transport._lock.locked()
            waits.append(args[1])
            return original_wait(*args, **kwargs)

        monkeypatch.setattr(env.geometry, "wait_checked_snapshot", checked_wait)
        env.start_phone()
        ref, gate = await _plan(env)
        original_ref = deepcopy(ref)

        def slow_xy(command, reply, _conn):
            if command.startswith("XY "):
                offset[0] += 1.5
            return reply

        env.daemon.response_hook = slow_xy
        guards = env.driver._layout_motion_guards

        def slow_guards(*args, **kwargs):
            contact, command = guards(*args, **kwargs)

            async def slow_contact():
                if contact is not None:
                    await contact()
                if any(cmd.startswith("XY ") for cmd in env.daemon.commands):
                    offset[0] += 1.5

            return slow_contact, command

        monkeypatch.setattr(env.driver, "_layout_motion_guards", slow_guards)
        await _click(env, ref, gate)
        assert len(env.tap_commands()) == 1
        assert len([cmd for cmd in env.daemon.commands if cmd.startswith("XY ")]) == 1
        assert len(waits) == 2 and waits[0] != waits[1]
        assert ref == original_ref, "evidence refresh must not change planned coordinates"
        assert env.geometry._nonces[waits[1]]["click_consumed"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["bounds", "view", "task", "display", "session", "ref"])
def test_contact_refresh_rejects_changed_plan_with_zero_taps(live_env, changed):
    async def scenario():
        env = live_env
        env.start_phone()
        ref, gate = await _plan(env)

        def change_after_xy(command, reply, _conn):
            if command.startswith("XY "):
                if changed == "bounds":
                    env._snapshot_kwargs["candidate_bounds"] = (64, 2160, 1026, 2280)
                elif changed == "view":
                    env._view = str(uuid.uuid4())
                elif changed == "task":
                    env._snapshot_kwargs["current_task_id"] = "different-task"
                elif changed == "display":
                    env._snapshot_kwargs["display"] = dict(ref["display"], rotation=1)
                elif changed == "session":
                    env.session = env.geometry.handshake(IME_DEVICE, {
                        "contract_version": "ime-geometry.v1", "imeGeometryVersion": 1,
                        "client_instance_id": str(uuid.uuid4())},
                        credentials=env.phone_credentials)
                else:
                    ref["cameraX"] += 1
            return reply

        env.daemon.response_hook = change_after_xy
        with pytest.raises(DriverError):
            await _click(env, ref, gate)
        assert env.tap_commands() == []
        assert len([cmd for cmd in env.daemon.commands if cmd.startswith("XY ")]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("age", [1.1, 2.1])
def test_refresh_does_not_relax_final_snapshot_or_nonce_ttl(live_env, monkeypatch, age):
    import runtime.ime_geometry as geom_mod

    async def scenario():
        env = live_env
        real_now = geom_mod._now
        offset = [0.0]
        monkeypatch.setattr(geom_mod, "_now", lambda: real_now() + offset[0])
        env.start_phone()
        ref, gate = await _plan(env)
        original_cmd = env.driver._json_cmd

        def delayed_transport(command, **kwargs):
            if command.startswith("TAP"):
                offset[0] += age
            return original_cmd(command, **kwargs)

        monkeypatch.setattr(env.driver, "_json_cmd", delayed_transport)
        with pytest.raises(DriverError, match="snapshot_stale|nonce_expired"):
            await _click(env, ref, gate)
        assert env.tap_commands() == []

    asyncio.run(scenario())


def test_missing_contact_snapshot_cannot_use_planning_nonce(live_env, monkeypatch):
    async def scenario():
        env = live_env
        env.start_phone()
        ref, gate = await _plan(env)
        monkeypatch.setattr(env.geometry, "wait_checked_snapshot",
                            lambda *_args, **_kwargs: (False, "snapshot_missing", None))
        with pytest.raises(DriverError, match="snapshot_missing"):
            await _click(env, ref, gate)
        assert env.tap_commands() == []
        assert not env.geometry._nonces[ref["nonce"]].get("click_consumed")

    asyncio.run(scenario())


def test_contact_gate_replay_cannot_request_another_nonce(live_env, monkeypatch):
    async def scenario():
        env = live_env
        env.start_phone()
        ref, gate = await _plan(env)
        await _click(env, ref, gate)
        nonce_count = len(env.geometry._nonces)
        with pytest.raises(DriverError, match="nonce"):
            await _click(env, ref, gate)
        assert len(env.tap_commands()) == 1
        assert len(env.geometry._nonces) == nonce_count

    asyncio.run(scenario())
