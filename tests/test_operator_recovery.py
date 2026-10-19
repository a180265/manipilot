from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from runtime.errors import ApiError
from runtime.operator_recovery import OperatorRecovery
from runtime.store import Store


def fixture(tmp_path, monkeypatch):
    store = Store(tmp_path / 'fleet.db')
    device = {'device_id': 'arm-01', 'status': 'paused', 'calibration_status': 'validated'}
    store.save_device(device)
    state = dict(connected=True, session='s1', communication_recovery_required=False,
                 recovery_id='f1', estop_locked=False, controller_state='Idle',
                 position={'x': 1, 'y': 2, 'z': 3, 'state': 'Idle'})
    arm = SimpleNamespace(data_root=tmp_path, recovery_state=AsyncMock(side_effect=lambda: dict(state)),
                          park=AsyncMock(return_value=dict(ok=True, park=True, arrived=True,
                                                          tap_executed=False, role='calibration_safe_park')),
                          recover_communication=AsyncMock(return_value={'recovered': True, 'session': 's1'}))
    lease = {}
    def acquire(device_id, task_id, **kwargs):
        lease.update(device_id=device_id, task_id=task_id, lease_id='l1')
        return dict(lease)
    runtime = SimpleNamespace(store=store, pre_enable_maintenance=False, estop=SimpleNamespace(active=False),
        devices=SimpleNamespace(get=lambda _: device, driver=lambda _: arm),
        leases=SimpleNamespace(get=lambda _: dict(lease) if lease else None,
                               acquire=acquire, release=lambda *_: lease.clear()),
        supervision=SimpleNamespace(stop=Mock()), _exec_tasks={}, audit=SimpleNamespace(record=Mock()))
    monkeypatch.setattr('runtime.operator_recovery.target_archive.load_runtime_calibration',
                        lambda *_: {'daemonSession': 's1', 'safePark': {'confirmed': True,
                            'daemonSession': 's1', 'parkX': 1, 'parkY': 2, 'parkZ': 3}})
    return OperatorRecovery(runtime), arm, state, device


def request(request_id='r1', **extra):
    body = {'contract_version': 'operator-recovery.v1', 'expected_session': 's1', 'recovery_id': 'f1',
            'confirmations': {'probe_clear': True, 'setup_unchanged': True, 'path_clear': True, 'arm_stopped': True}}
    body.update(extra)
    context = dict(operator_id='human', role='operator', reason='人工检查', correlation_id='c1',
                   client_request_id=request_id, confirmation_token='确认执行')
    return body, context


def run(service, operation='park', request_id='r1', **extra):
    return asyncio.run(service.execute('arm-01', operation, *request(request_id, **extra)))


def test_park_is_once_and_does_not_resume(tmp_path, monkeypatch):
    service, arm, _, device = fixture(tmp_path, monkeypatch)
    result = run(service)
    replay = run(service)
    assert result['park_confirmed'] is True
    assert replay['replayed'] is True
    assert arm.park.await_count == 1
    assert device['status'] == 'paused'
    assert device['operator_recovery_pending'] is False
    assert asyncio.run(service.view('arm-01', client_request_id='r1'))['requested_operation']['client_request_id'] == 'r1'


@pytest.mark.parametrize('change', ['missing_confirmation', 'wrong_session', 'estop', 'unreviewed', 'busy', 'lease', 'not_paused', 'calibration'])
def test_unsafe_park_never_reaches_driver(tmp_path, monkeypatch, change):
    service, arm, state, device = fixture(tmp_path, monkeypatch)
    body, context = request()
    if change == 'missing_confirmation': body['confirmations']['probe_clear'] = False
    if change == 'wrong_session': body['expected_session'] = 'old'
    if change == 'estop': service.rt.estop.active = True
    if change == 'not_paused': device['status'] = 'ready'
    if change == 'calibration': device['calibration_status'] = 'due'
    if change in {'unreviewed', 'busy'}:
        service.rt.store.save_task({'task_id': 't1', 'device_id': 'arm-01', 'status': 'unknown' if change == 'unreviewed' else 'running'})
    if change == 'lease': service.rt.leases.get = lambda _: {'task_id': 'other', 'lease_id': 'l2'}
    with pytest.raises(ApiError):
        asyncio.run(service.execute('arm-01', 'park', body, context))
    arm.park.assert_not_awaited()


def test_unknown_survives_restart_and_requires_review(tmp_path, monkeypatch):
    service, arm, _, device = fixture(tmp_path, monkeypatch)
    arm.park.side_effect = TimeoutError('lost receipt')
    result = run(service)
    assert result['status'] == 'unknown'
    assert device['operator_recovery_pending'] is True
    restarted = OperatorRecovery(service.rt)
    assert run(restarted)['status'] == 'unknown'
    with pytest.raises(ApiError): run(restarted, request_id='r2')
    reviewed = run(restarted, 'operator_recovery_review', 'review1', operation_request_id='r1')
    assert reviewed['park_confirmed'] is False
    assert restarted._get('r1')['status'] == 'unknown'
    assert restarted._get('r1')['reviewed_at']
    arm.park.side_effect = None
    assert run(restarted, request_id='r2')['park_confirmed'] is True
    assert arm.park.await_count == 2


def test_park_unknown_can_recover_communication_without_clearing_unknown(tmp_path, monkeypatch):
    service, arm, state, device = fixture(tmp_path, monkeypatch)
    arm.park.side_effect = TimeoutError('lost')
    run(service)
    state.update(communication_recovery_required=True, estop_locked=True, position=None, controller_state=None)
    assert run(service, 'recover_communication', 'recover1')['status'] == 'confirmed'
    assert device['operator_recovery_pending'] is True
    assert not asyncio.run(service.view('arm-01'))['can_park']


def test_pending_written_before_effect_and_concurrent_rejected(tmp_path, monkeypatch):
    service, arm, _, _ = fixture(tmp_path, monkeypatch)
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        async def park(**kwargs):
            assert service._get('r1')['status'] == 'pending'
            started.set()
            await finish.wait()
            return {'ok': True, 'park': True, 'arrived': True, 'tap_executed': False, 'role': 'calibration_safe_park'}
        arm.park.side_effect = park
        task = asyncio.create_task(service.execute('arm-01', 'park', *request()))
        await started.wait()
        with pytest.raises(ApiError): await service.execute('arm-01', 'park', *request('r2'))
        finish.set()
        await task
    asyncio.run(scenario())
    assert arm.park.await_count == 1


def test_same_request_id_different_payload_conflicts(tmp_path, monkeypatch):
    service, arm, _, _ = fixture(tmp_path, monkeypatch)
    run(service)
    with pytest.raises(ApiError): run(service, expected_session='changed')
    assert arm.park.await_count == 1


def test_reviewed_unknown_lease_released_but_old_result_unchanged(tmp_path, monkeypatch):
    service, arm, _, _ = fixture(tmp_path, monkeypatch)
    service.rt.store.save_task({'task_id': 't1', 'device_id': 'arm-01', 'status': 'unknown', 'reviewed_at': '2026-10-19'})
    service.rt.leases.acquire('arm-01', 't1')
    assert run(service)['status'] == 'confirmed'
    assert service.rt.store.get_task('t1')['status'] == 'unknown'
    assert service.rt.leases.get('arm-01') is None


def test_execution_guard_after_motion_is_unknown(tmp_path, monkeypatch):
    service, arm, _, device = fixture(tmp_path, monkeypatch)
    async def park(**kwargs):
        assert kwargs['expected_session'] == 's1'
        device['binding_revision'] = 2
        kwargs['execution_guard']()
    arm.park.side_effect = park
    assert run(service)['status'] == 'unknown'
    assert device['operator_recovery_pending'] is True


def test_persistent_pending_guard_works_without_device_mirror(tmp_path, monkeypatch):
    from runtime.operator_recovery import pending_operator_recovery
    service, arm, _, device = fixture(tmp_path, monkeypatch)
    assert not pending_operator_recovery(service.rt.store, 'arm-01')
    arm.park.side_effect = TimeoutError('lost')
    run(service)
    device.pop('operator_recovery_pending')
    assert pending_operator_recovery(service.rt.store, 'arm-01')
    assert asyncio.run(service.view('arm-01'))['pending_operations'][0]['can_review'] is True
    run(service, 'operator_recovery_review', 'review', operation_request_id='r1')
    assert not pending_operator_recovery(service.rt.store, 'arm-01')


def test_review_different_session_requires_new_calibration(tmp_path, monkeypatch):
    service, arm, state, _ = fixture(tmp_path, monkeypatch)
    arm.park.side_effect = TimeoutError('lost')
    run(service)
    state['session'] = 's2'
    with pytest.raises(ApiError):
        run(service, 'operator_recovery_review', 'review', operation_request_id='r1', expected_session='s2')
    monkeypatch.setattr('runtime.operator_recovery.target_archive.load_runtime_calibration',
                        lambda *_: {'daemonSession': 's2', 'safePark': {'confirmed': True,
                            'daemonSession': 's2', 'parkX': 1, 'parkY': 2, 'parkZ': 3}})
    result = run(service, 'operator_recovery_review', 'review', operation_request_id='r1', expected_session='s2')
    assert result['status'] == 'confirmed' and result['park_confirmed'] is False
    assert service._get('r1')['status'] == 'unknown'


def test_api_routes_and_definite_preflight_rejection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.app import create_app
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime
    from tests.helpers import make_device_configs
    runtime = ArmFleetRuntime(PlatformConfig({
        'db_path': str(tmp_path / 'real.db'), 'evidence_dir': str(tmp_path / 'evidence'),
        'plugin_dir': str(tmp_path / 'plugins'), 'hardware_mode': 'simulation',
    }, root=tmp_path))
    app = create_app(runtime)
    app.state.device_configs = make_device_configs(1)
    with TestClient(app) as client:
        device = runtime.devices.get('arm-01')
        runtime.devices.pause('arm-01', '人工核查')
        device['calibration_status'] = 'validated'
        arm = runtime.devices.driver('arm-01')
        arm.data_root = tmp_path
        arm.recovery_state = AsyncMock(return_value=dict(connected=True, session='s1',
            communication_recovery_required=False, controller_state='Idle',
            position={'x': 1, 'y': 2, 'z': 3}))
        arm.park = AsyncMock(return_value=dict(ok=True, park=True, arrived=True,
            tap_executed=False, role='calibration_safe_park'))
        monkeypatch.setattr('runtime.operator_recovery.target_archive.load_runtime_calibration',
                            lambda *_: {'daemonSession': 's1', 'safePark': {'confirmed': True,
                                'daemonSession': 's1', 'parkX': 1, 'parkY': 2, 'parkZ': 3}})
        body, context = request()
        payload = {**body, **context}
        response = client.post('/v1/devices/arm-01/park', json={**payload, 'expected_session': 'old'})
        assert response.status_code == 409
        assert response.json()['not_submitted'] is True
        arm.park.assert_not_awaited()
        response = client.post('/v1/devices/arm-01/park', json=payload)
        assert response.status_code == 200 and response.json()['park_confirmed'] is True
        response = client.get('/v1/devices/arm-01/operator-recovery?client_request_id=r1')
        assert response.json()['requested_operation']['client_request_id'] == 'r1'
        response = client.post('/v1/devices/arm-01/park', json={**payload, 'expected_session': 'old'})
        assert response.status_code == 409
        assert 'not_submitted' not in response.json()
        assert arm.park.await_count == 1


from tests.test_layout_target_driver import target_rig  # noqa: E402,F401


@pytest.mark.parametrize("run_background", [False, True])
def test_real_driver_tcp_parks_from_paused_runtime_with_no_business_task(target_rig, run_background):
    """Service → production driver → TCP fake daemon, real calibration bundle."""
    from drivers import calibration_bundle as cb
    from drivers.daemon_arm import DaemonArmDriver
    from tests.test_daemon_arm import FakeDaemon
    rig = target_rig
    daemon = FakeDaemon(mpos=(0, 0, 2))
    daemon.session = daemon.next_session = 'sess-A'
    daemon.start()
    arm = DaemonArmDriver('arm-01', daemon_addr=f'127.0.0.1:{daemon.port}', data_root=rig.data)
    original_gate = rig.arm.live_calibration_gate
    async def live_gate(**kwargs):
        if run_background:
            lease_before = rig.runtime.leases.get('arm-01')
            assert lease_before['task_id'] == 'operator-recovery:r1'
            assert rig.runtime.store.get_task(lease_before['task_id']) is None
            await rig.runtime._dispatch_once()
            await rig.runtime.devices._heartbeat_device('arm-01')
            assert rig.runtime.leases.get('arm-01') == lease_before
            assert rig.runtime.devices.get('arm-01')['status'] == 'paused'
        return await original_gate(**kwargs)
    arm.live_calibration_gate = live_gate
    arm.motion_verifier = rig.arm.motion_verifier
    rig.bundle['safePark'] = dict(confirmed=True, daemonSession='sess-A', parkX=1., parkY=2., parkZ=3.)
    cb.save_bundle(rig.bundle, rig.data)
    rig.runtime.devices._arms['arm-01'] = arm
    rig.runtime.config._data['pre_enable_maintenance'] = False
    rig.runtime.devices.pause('arm-01', '人工核查')
    rig.runtime.devices.get('arm-01')['calibration_status'] = 'validated'
    service = OperatorRecovery(rig.runtime)
    body, context = request(expected_session='sess-A')
    try:
        asyncio.run(arm.connect())
        before = asyncio.run(service.view('arm-01'))
        assert before['can_park'], before
        result = asyncio.run(service.execute('arm-01', 'park', body, context))
        assert result['park_confirmed'] is True, result
        assert result['receipt']['position'] == {'x': 1., 'y': 2., 'z': 3.}
        assert rig.runtime.devices.get('arm-01')['status'] == 'paused'
        assert rig.runtime.leases.get('arm-01') is None
        assert daemon.commands == ['XY 1.000 2.000 F5000', 'Z 1.000 F1500']
        assert not any(command.startswith(('TAP', 'CONNECT', 'RESUME')) for _, command in daemon.requests)
        replay = asyncio.run(service.execute('arm-01', 'park', body, context))
        assert replay['replayed'] is True and len(daemon.commands) == 2
    finally:
        asyncio.run(arm.disconnect())
        daemon.stop()
