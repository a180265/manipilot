"""Explicit preparation retires stale work without physical actions."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from runtime.errors import ApiError
from runtime.run_preparation import prepare_run
from tests.helpers import make_device_configs, make_runtime, op_context, probe_payload
from tests.test_stage_e_api import build_app


def test_prepare_retires_old_work_without_motion_or_replay(tmp_path):
    async def scenario():
        rt = make_runtime(tmp_path)
        await rt.startup(device_configs=make_device_configs(2))
        try:
            rt.devices.pause('arm-01', '旧任务未知')
            old = rt.submit_task(probe_payload([]), source='console', context={'operator_id': 'test'})
            queued = rt.submit_task(probe_payload([]), source='console', context={'operator_id': 'test'})
            old.update(status='unknown', status_display='结果未知', result={'outcome': 'unknown'})
            rt.store.save_task(old)
            rt.devices.hold_unknown('arm-01', old['task_id'], '旧任务未知')
            arm = rt.devices.driver('arm-01')
            before_actions = list(arm.action_log)
            before_calibration = rt.devices.get('arm-01')['calibration_status']
            calls = []
            async def hook(runtime, device_id, context):
                assert runtime.devices.get(device_id)['status'] == 'paused'
                calls.append(device_id)
            context = op_context()
            result = await prepare_run(rt, 'arm-01', {'confirmed': True}, context, before_resume=hook)
            assert result['status'] == 'ready'
            assert result['eligibility']['can_accept_task']
            assert result['discarded_task_count'] == result['reviewed_task_count'] == 1
            assert rt.get_task(queued['task_id'])['status'] == 'cancelled'
            reviewed = rt.get_task(old['task_id'])
            assert reviewed['status'] == 'unknown' and reviewed['result'] == {'outcome': 'unknown'}
            assert reviewed['reviewed_at']
            assert rt.devices.get('arm-01')['unknown_hold_task_ids'] == []
            assert rt.devices.get('arm-01')['calibration_status'] == before_calibration
            assert rt.devices.get('arm-02')['status'] == 'ready'
            assert not any(row.get('task_id') == old['task_id'] for row in rt.takeovers())
            assert arm.action_log == before_actions
            rt.devices.pause('arm-01', '后来新暂停')
            again = await prepare_run(rt, 'arm-01', {'confirmed': True}, context, before_resume=hook)
            assert again['status'] == 'paused' and calls == ['arm-01']
            assert not again['eligibility']['can_accept_task']
        finally:
            await rt.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['hook', 'connection', 'calibration', 'pause', 'camera'])
def test_prepare_failure_retains_pause_and_unknown(tmp_path, fault):
    async def scenario():
        rt = make_runtime(tmp_path)
        await rt.startup(device_configs=make_device_configs())
        try:
            rt.devices.pause('arm-01', '旧暂停')
            old = rt.submit_task(probe_payload([]), source='console', context={'operator_id': 'test'})
            old.update(status='unknown')
            rt.store.save_task(old)
            rt.devices.hold_unknown('arm-01', old['task_id'], '旧暂停')
            actions = list(rt.devices.driver('arm-01').action_log)
            async def hook(runtime, device_id, context):
                if fault == 'hook':
                    raise ApiError('state_transition_invalid', '业务清理失败')
                if fault == 'connection':
                    rt.devices.driver(device_id)._connected = False
                if fault == 'calibration':
                    rt.devices.set_calibration_status(device_id, 'due')
                if fault == 'pause':
                    rt.devices.pause(device_id, '操作员另外暂停')
                if fault == 'camera':
                    rt.devices.get(device_id)['camera_quality'] = 'waiting'
            with pytest.raises(ApiError):
                await prepare_run(rt, 'arm-01', {'confirmed': True}, op_context(), before_resume=hook)
            assert rt.devices.get('arm-01')['status'] == 'paused'
            assert not rt.get_task(old['task_id']).get('reviewed_at')
            assert rt.devices.driver('arm-01').action_log == actions
        finally:
            await rt.shutdown()
    asyncio.run(scenario())


def test_prepare_api_requires_explicit_operator_and_has_default_reason(tmp_path):
    app, rt = build_app(tmp_path, n_devices=1)
    with TestClient(app) as client:
        context = op_context()
        context.pop('reason')
        url = '/v1/devices/arm-01/prepare-run'
        assert client.post(url, json=context).status_code == 400
        assert client.post(url, json={**context, 'confirmed': True, 'role': 'readonly'}).status_code == 403
        assert client.post(url, json={**context, 'confirmed': True, 'role': 'wx'}).status_code == 403
        response = client.post(url, json={**context, 'confirmed': True})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'ready'
        view = client.get('/v1/devices/arm-01').json()
        assert view['task_eligibility']['can_accept_task']
        assert 'can_manual_send' in view['eligibility']


@pytest.mark.parametrize('fault', ['estop', 'lease', 'action_in_queue'])
def test_prepare_does_not_clear_active_faults(tmp_path, fault):
    async def scenario():
        rt = make_runtime(tmp_path)
        await rt.startup(device_configs=make_device_configs())
        try:
            rt.devices.pause('arm-01', '暂停')
            task = rt.submit_task(probe_payload([]), source='console', context={'operator_id': 'test'})
            if fault == 'estop':
                rt.devices.get('arm-01')['estop_latched'] = True
            elif fault == 'lease':
                rt.leases.acquire('arm-01', task['task_id'])
            else:
                task['step_log'] = [{'is_action': True}]
                rt.store.save_task(task)
            with pytest.raises(ApiError):
                await prepare_run(rt, 'arm-01', {'confirmed': True}, op_context())
            assert rt.get_task(task['task_id'])['status'] == 'queued'
            assert rt.devices.get('arm-01')['status'] == 'paused'
        finally:
            await rt.shutdown()
    asyncio.run(scenario())
