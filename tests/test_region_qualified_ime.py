"""Region target safety through the authenticated IME geometry fixture."""
import asyncio
from copy import deepcopy
import pytest
from drivers.arm import DriverError
from tests.test_r2_live_geometry_driver import live_env, ARM_DEVICE  # noqa: F401
from tests.test_region_qualified_clicks import region_driver


def test_live_geometry_keeps_true_bounds_and_region_click_uses_them(live_env, monkeypatch):
    async def scenario():
        env = live_env
        env.start_phone()
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, 'ime_candidate_bar')
        assert ref['bounds'] == {'left': 54, 'top': 2160, 'right': 1026, 'bottom': 2280}
        region_driver(env.driver, monkeypatch)
        result = await env.driver.click({'target_id': 'ime_candidate_bar', 'target_ref': ref})
        assert result['click_point']['safety']['source'] == 'live_ime_geometry'
        assert len(env.tap_commands()) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['planned_ref', 'consumed_snapshot'])
def test_ime_final_gate_binds_exact_target_bounds(live_env, monkeypatch, fault):
    async def scenario():
        env = live_env
        env.start_phone()
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, 'ime_candidate_bar')
        gate = env.runtime._geometry_gates[ref['nonce']]
        if fault == 'planned_ref':
            ref['bounds']['bottom'] += 10
        else:
            consume = env.geometry.consume_for_click
            def changed(*args, **kwargs):
                snapshot = deepcopy(consume(*args, **kwargs))
                target = next(item for item in snapshot['targets'] if item['id'] == 'ime_candidate_bar')
                target['bounds']['bottom'] += 10
                return snapshot
            monkeypatch.setattr(env.geometry, 'consume_for_click', changed)
        with pytest.raises(DriverError, match='范围|落点'):
            gate()
        assert env.tap_commands() == []
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['missing', 'too_small'])
def test_region_qualified_ime_requires_usable_bounds(live_env, monkeypatch, fault):
    async def scenario():
        env = live_env
        env.start_phone()
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, 'ime_candidate_bar')
        if fault == 'missing':
            ref.pop('bounds', None)
        else:
            ref['bounds'] = {'left': 539, 'top': 2219, 'right': 541, 'bottom': 2221}
        region_driver(env.driver, monkeypatch)
        with pytest.raises(DriverError, match='范围|安全区|边界'):
            await env.driver.click({'target_id': 'ime_candidate_bar', 'target_ref': ref})
        assert env.tap_commands() == []
    asyncio.run(scenario())
