"""Camera-only assignment preserves arm/input sessions and other workstations."""
import asyncio
from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient

from tests.test_stage_c_device_menu import make_runtime, _op


def test_camera_mapping_preserves_arm_and_input_and_requires_manual_calibration(tmp_path):
    runtime, app = make_runtime(tmp_path, devices=[
        {"device_id": "arm-01", "camera_id": "cam-01", "sim_calibrated": True},
        {"device_id": "arm-02", "camera_id": "cam-02", "sim_calibrated": True},
    ])
    with TestClient(app) as client:
        manager = runtime.devices
        old_arm, old_input = manager.driver('arm-01'), manager.input_driver('arm-01')
        old_camera, other_camera = manager.camera('arm-01'), manager.camera('arm-02')
        old_broker = manager.camera_broker('arm-01')
        manager._devices['arm-01']['daemon_session'] = 'keep-session'
        other = deepcopy(manager._devices['arm-02'])
        response = client.put('/v1/devices/arm-01/binding', json={
            **_op('相机独立对应', dangerous=True), 'camera_binding_id': 'camera:cam-09'})
        assert response.status_code == 200, response.text
        assert manager.driver('arm-01') is old_arm
        assert manager.input_driver('arm-01') is old_input
        assert manager.camera('arm-01') is not old_camera
        assert manager.camera('arm-01').device_id == 'cam-09'
        assert manager.camera_broker('arm-01') is not old_broker
        device = manager._devices['arm-01']
        assert device['daemon_session'] == 'keep-session'
        assert device['status'] == 'paused'
        assert device['device_mode'] == 'unmanaged'
        assert device['calibration_status'] == 'due'
        assert manager.camera('arm-02') is other_camera
        assert manager._devices['arm-02'] == other
        stored = json.loads((tmp_path/'configs/devices.json').read_text())['devices']
        assert next(x for x in stored if x['device_id'] == 'arm-01')['camera_id'] == 'cam-09'


def test_camera_catalog_includes_unassigned_channels(tmp_path):
    runtime, app = make_runtime(tmp_path, devices=[{'device_id':'arm-01','camera_id':'cam-01'}])
    with TestClient(app) as client:
        options = client.get('/v1/devices/arm-01/binding/options').json()['camera_options']
        assert {f'camera:cam-{i:02d}' for i in range(1,11)} <= {x['resource_id'] for x in options}


def test_camera_mapping_storage_failure_leaves_old_objects_and_mapping(tmp_path, monkeypatch):
    runtime, app = make_runtime(tmp_path, devices=[{'device_id':'arm-01','camera_id':'cam-01'}])
    with TestClient(app) as client:
        manager=runtime.devices
        before=deepcopy(manager._devices['arm-01'])
        camera=manager.camera('arm-01')
        configs=deepcopy(manager._registered_configs)
        def fail(_device_id, _changes, **_kwargs): raise OSError('disk full')
        manager.set_config_persistence(fail)
        response=client.put('/v1/devices/arm-01/binding',json={
            **_op('写入失败',dangerous=True),'camera_binding_id':'camera:cam-09'})
        assert response.status_code >= 400
        assert manager._devices['arm-01'] == before
        assert manager.camera('arm-01') is camera
        assert manager._registered_configs == configs


@pytest.mark.parametrize('blocker', ['busy', 'lease', 'installation', 'old_channel', 'new_channel'])
def test_camera_mapping_rejects_active_work_without_touching_other_channel(tmp_path, blocker):
    from contextlib import nullcontext
    from scripts.camera_binding_v2 import installation_lock
    runtime, app = make_runtime(tmp_path, devices=[{'device_id':'arm-01','camera_id':'cam-01'}])
    with TestClient(app) as client:
        manager=runtime.devices
        if blocker == 'busy': manager._devices['arm-01']['status']='busy'
        if blocker == 'lease': runtime.leases.acquire('arm-01','active-task')
        before=deepcopy(manager._devices['arm-01'])
        lock_id={'installation':'arm-01','old_channel':'camera-cam-01','new_channel':'camera-cam-09'}.get(blocker)
        guard=(installation_lock(tmp_path/'data',lock_id,exclusive=False) if lock_id else nullcontext())
        with guard:
            response=client.put('/v1/devices/arm-01/binding',json={
                **_op('活动动作保护'+blocker,dangerous=True),'camera_binding_id':'camera:cam-09'})
        assert response.status_code == 409, response.text
        assert manager._devices['arm-01'] == before


def test_camera_mapping_real_arm_gates_use_replaced_camera_without_reconnect(tmp_path,monkeypatch):
    from tests.test_device_calibration_state import device_client
    calls=[]
    async def gate(**kwargs):
        calls.append(kwargs)
        return [],{},None
    monkeypatch.setattr('runtime.calibration_gate.check_live_calibration',gate)
    with device_client(tmp_path,monkeypatch) as (client,runtime,daemon,archive):
        manager=runtime.devices
        # Fixture-only function/Path providers are not serializable config fields.
        manager.set_config_persistence(lambda *_a, **_k: None)
        arm=manager.driver('arm-01')
        session=manager._devices['arm-01']['daemon_session']
        response=client.put('/v1/devices/arm-01/binding',json={
            **_op('真实驱动换相机',dangerous=True),'camera_binding_id':'camera:cam-09'})
        assert response.status_code == 200,response.text
        assert manager.driver('arm-01') is arm
        assert manager._devices['arm-01']['daemon_session'] == session
        asyncio.run(arm.live_calibration_gate())
        assert calls[-1]['camera_id'] == 'cam-09'
        assert calls[-1]['camera'] is manager.camera('arm-01')
        asyncio.run(arm.screen_wake_gate())
        assert calls[-1]['camera_id'] == 'cam-09'
        assert calls[-1]['camera'] is manager.camera_broker('arm-01')
        assert arm.motion_verifier._camera is manager.camera_broker('arm-01')


def test_usb_rebind_refreshes_live_identity_then_accepts_only_new_manual_calibration(tmp_path):
    from types import SimpleNamespace
    from datetime import datetime,timezone
    from tests.test_calibration_status_refresh import _manager
    from drivers import calibration_bundle as cb
    manager,device,bundle=_manager(tmp_path)
    device['status']='paused'
    source={'camera_id':device['camera_id'],'device_id':'arm-01','device_unique_id':'uvc-new-port',
            'camera_binding_id':'new-binding','source_geometry_id':'new-geometry',
            'received_at':datetime.now(timezone.utc).isoformat(),'binding_revision':8,'legacy_mode':False}
    camera=SimpleNamespace(last_source_info=lambda:source)
    manager._cameras['arm-01']=camera
    result=manager.get('arm-01')
    assert result['camera_device_unique_id']=='uvc-new-port'
    assert result['camera_binding_id']=='new-binding'
    assert result['calibration_status']=='due'
    # Stand-in for the user's completed calibration, with current capture facts.
    bundle['cameraDeviceUniqueId']='uvc-new-port'
    bundle['bindingRevision']=8
    bundle['bindingFingerprint']=cb.binding_fingerprint(
        device_id='arm-01',camera_id=bundle['cameraId'],camera_device_unique_id='uvc-new-port',
        daemon_addr=bundle['daemonAddr'],frame_width=bundle['frame']['width'],frame_height=bundle['frame']['height'])
    cb.save_bundle(bundle,tmp_path)
    result=manager.get('arm-01')
    assert result['calibration_status']=='validated',result['calibration_checks']
    assert result['status']=='paused' and result['device_mode']=='unmanaged'


@pytest.mark.parametrize('broken',['stale','future','wrong_device','wrong_channel','missing_binding'])
def test_live_camera_metadata_does_not_adopt_unproven_identity(tmp_path,broken):
    from types import SimpleNamespace
    from datetime import datetime,timezone,timedelta
    from tests.test_calibration_status_refresh import _manager
    manager,device,bundle=_manager(tmp_path)
    source={'camera_id':device['camera_id'],'device_id':'arm-01','device_unique_id':'uvc-other',
            'camera_binding_id':'other-binding','source_geometry_id':'other-geometry',
            'received_at':datetime.now(timezone.utc).isoformat(),'binding_revision':7,'legacy_mode':False}
    if broken=='stale':source['received_at']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    if broken=='future':source['received_at']=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
    if broken=='wrong_device':source['device_id']='arm-02'
    if broken=='wrong_channel':source['camera_id']='cam-02'
    if broken=='missing_binding':source['camera_binding_id']=None
    manager._cameras['arm-01']=SimpleNamespace(last_source_info=lambda:source)
    manager.get('arm-01')
    assert device['camera_device_unique_id']==bundle['cameraDeviceUniqueId']


def test_multichannel_driver_accepts_fresh_usb_identity_without_changing_channel_contract():
    from runtime.devices import DeviceManager
    driver=DeviceManager._make_real_camera({
        'device_id':'arm-01','camera_id':'cam-09','camera_mode':'multi_channel',
        'camera_device_unique_id':'retired-usb-id','expected_frame_width':1280,'expected_frame_height':720,
        'camera_frame_url':'http://127.0.0.1:8790/cameras/cam-09/latest.jpg'})
    source=driver._frame_provider
    assert source.expected_device_unique_id is None
    assert source.expected_camera_id=='cam-09' and source.expected_device_id=='arm-01'
    assert source.expected_width is None and source.expected_height is None


@pytest.mark.parametrize('activity', ['busy', 'returning_home', 'recovering', 'lease'])
def test_live_identity_refresh_waits_until_task_snapshot_is_released(tmp_path, activity):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from tests.test_calibration_status_refresh import _manager
    from runtime.layout_observation import BINDING_KEYS
    manager, device, bundle = _manager(tmp_path)
    device['camera_mode'] = 'multi_channel'
    device['status'] = activity if activity != 'lease' else 'ready'
    held = {'task_id': 'active-task'} if activity == 'lease' else None
    manager._leases = SimpleNamespace(get=lambda _: held)
    source = {'camera_id': device['camera_id'], 'device_id': 'arm-01',
              'device_unique_id': 'replacement-usb', 'camera_binding_id': 'replacement-binding',
              'source_geometry_id': 'replacement-geometry', 'binding_revision': bundle['bindingRevision'],
              'frame_width': 1280, 'frame_height': 720, 'legacy_mode': False,
              'received_at': datetime.now(timezone.utc).isoformat()}
    camera = SimpleNamespace(last_source_info=lambda: source)
    manager._cameras['arm-01'] = camera
    snapshot = {key: device.get(key) for key in BINDING_KEYS}
    size = device['expected_frame_width'], device['expected_frame_height']
    manager.get('arm-01')
    assert {key: device.get(key) for key in BINDING_KEYS} == snapshot
    assert (device['expected_frame_width'], device['expected_frame_height']) == size
    device['status'] = 'ready'
    held = None
    result = manager.get('arm-01')
    assert result['camera_device_unique_id'] == 'replacement-usb'
    assert result['camera_binding_id'] == 'replacement-binding'
    assert (result['expected_frame_width'], result['expected_frame_height']) == (1280, 720)
    assert result['calibration_status'] == 'due'
