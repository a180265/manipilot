"""Hidden keyboards stop heartbeats; focus-only admission must keep input guards."""
import asyncio
from copy import deepcopy

import pytest

from runtime.device_eligibility import compute_device_eligibility
from runtime.errors import ApiError
from tests.helpers import PROBE_MANIFEST, make_runtime, make_device_configs, enable_input_switch, probe_payload


def _runtime(tmp_path, bootstrap):
    async def build():
        manifest = deepcopy(PROBE_MANIFEST)
        manifest['task_types'][0].update(task_type='declared_input', requires_input=True,
            requires_keyboard_active=False, focuses_input_before_typing=bootstrap)
        runtime = make_runtime(tmp_path, manifest=manifest)
        await runtime.devices.register_all(make_device_configs(1))
        enable_input_switch(runtime, tmp_path)
        runtime.devices.get('arm-01').update(enforce_input_gate=True, ime_device_id='phone')
        runtime.devices._ime_status = lambda device: {'online': False, 'keyboardActive': False}
        runtime.devices.input_binding = lambda device_id: {'ime_device_id': 'phone'}
        return runtime
    return asyncio.run(build())


@pytest.mark.parametrize('bootstrap', [False, True])
def test_hidden_keyboard_can_be_focused_only_by_declared_task(tmp_path, bootstrap):
    runtime = _runtime(tmp_path, bootstrap)
    payload = probe_payload([{'is_action': False}]);payload['task_type']='declared_input'
    try:
        if not bootstrap:
            with pytest.raises(ApiError, match='ime_offline'):
                runtime.submit_task(payload, source='console', context={'operator_id':'test','reason':'focus'})
        else:
            task = runtime.submit_task(payload, source='console', context={'operator_id':'test','reason':'focus'})
            assert task['status'] == 'queued'
            lease=runtime.leases.acquire('arm-01',task['task_id'])
            task.update(lease_id=lease['lease_id'],status='executing')
            runtime.store.save_task(task)
            done=asyncio.run(runtime.executor.execute(task['task_id']))
            assert done['status'] == 'confirmed', done.get('result_message')
            # Actual input readiness remains strict regardless of the task declaration.
            assert 'ime_offline' in runtime.devices.eligibility('arm-01',require_input=True)['blocking_checks']
    finally:
        asyncio.run(runtime.shutdown())


def test_bootstrap_still_requires_binding_and_authorization(tmp_path):
    device={'device_id':'arm-01','status':'ready','calibration_status':'validated',
            '_settings_data_root':tmp_path}
    result=compute_device_eligibility(device,require_input=True,require_keyboard_active=False,
                                     require_ime_ready=False)
    assert set(result['blocking_checks']) == {'input_switch_disabled','ime_not_bound'}
