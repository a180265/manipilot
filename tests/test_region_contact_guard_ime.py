"""Real driver + authenticated geometry + FakeDaemon; no physical device IO."""
import asyncio
import json

import pytest

from drivers.arm import DriverError
from drivers import calibration_bundle as cb
from tests.test_r2_live_geometry_driver import live_env, ARM_DEVICE  # noqa: F401
from tests.test_region_qualified_clicks import region_driver


@pytest.mark.parametrize('region,drift,blocked', [(True, 4, True), (True, -4, True),
                                                (True, .1, False), (False, 4, False)])
def test_ime_final_contact_checks_mpos_before_consuming_nonce(
        live_env, monkeypatch, region, drift, blocked):
    async def scenario():
        env = live_env
        env.start_phone()
        if region:
            region_driver(env.driver, monkeypatch)
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, 'ime_delete_key')
        gate = env.runtime._geometry_gates[ref['nonce']]
        consumed = []
        consume = env.geometry.consume_for_click

        def counted(*args, **kwargs):
            consumed.append(args[1])
            return consume(*args, **kwargs)

        monkeypatch.setattr(env.geometry, 'consume_for_click', counted)

        def after_travel(command, reply, _conn):
            if command.startswith('XY '):
                env.daemon.mpos[0] += drift
                return env.daemon._mpos_json()
            return reply

        env.daemon.response_hook = after_travel
        target = {'target_id': 'ime_delete_key', 'target_ref': ref,
                  'input_claim': {'geometry_gate': gate}}
        if blocked:
            with pytest.raises(DriverError, match='安全范围|安全区'):
                await env.driver.click(target)
            assert env.tap_commands() == []
            assert consumed == []
        else:
            await env.driver.click(target)
            assert len(env.tap_commands()) == 1
            assert consumed == [ref['nonce']]
    asyncio.run(scenario())


def test_region_calibration_change_after_travel_blocks_contact_and_nonce(live_env, monkeypatch):
    async def scenario():
        env = live_env
        env.start_phone()
        region_driver(env.driver, monkeypatch)
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, 'ime_delete_key')
        gate = env.runtime._geometry_gates[ref['nonce']]
        consumed = []
        consume = env.geometry.consume_for_click

        def counted(*args, **kwargs):
            consumed.append(args[1])
            return consume(*args, **kwargs)

        monkeypatch.setattr(env.geometry, 'consume_for_click', counted)

        def after_travel(command, reply, _conn):
            if command.startswith('XY '):
                path = cb.bundle_path(ARM_DEVICE, env.driver.data_root)
                bundle = json.loads(path.read_text())
                bundle['z']['tapDepth'] += .5
                path.write_text(json.dumps(bundle))
            return reply

        env.daemon.response_hook = after_travel
        with pytest.raises(DriverError, match='校准已变化'):
            await env.driver.click({'target_id': 'ime_delete_key', 'target_ref': ref,
                                    'input_claim': {'geometry_gate': gate}})
        assert env.tap_commands() == []
        assert consumed == []
    asyncio.run(scenario())
