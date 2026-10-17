"""Registered daemon selectors must survive the real binding API lifecycle."""
import json

from fastapi.testclient import TestClient
import pytest

from tests.test_stage_c_device_menu import make_runtime, _op


def config():
    return {"device_id":"arm-id", "camera_id":"cam-id", "sim_calibrated":True,
            "daemon_addr":"127.0.0.1:8895", "serial_port":"/dev/cu.arm-id"}


def test_legacy_registration_resolves_a_real_daemon_identity_and_restart_is_stable(tmp_path):
    runtime, app = make_runtime(tmp_path, devices=[config()])
    with TestClient(app) as client:
        device = runtime.devices.get("arm-id")
        identity = device.get("daemon_binding_id")
        assert identity and identity.startswith("daemon-binding.v1:")
        assert device["daemon_resource_id"] == "daemon:arm-id"
        view = client.get("/v1/devices/arm-id/binding").json()
        assert view["daemon_binding_id"] == identity
        assert view["daemon_resource_id"] == "daemon:arm-id"
        assert device["calibration_status"] == "validated"
    restarted, app = make_runtime(tmp_path, devices=[config()])
    with TestClient(app):
        assert restarted.devices.get("arm-id")["daemon_binding_id"] == identity


def test_selector_is_persisted_by_binding_api_and_replayed_without_identity_drift(tmp_path):
    runtime, app = make_runtime(tmp_path, devices=[config()])
    with TestClient(app) as client:
        before = runtime.devices.get("arm-id")["daemon_binding_id"]
        body = {**_op("确认既有daemon资源",dangerous=True), "daemon_binding_id":"daemon:arm-id"}
        response = client.put("/v1/devices/arm-id/binding",json=body)
        assert response.status_code == 200, response.text
        saved = json.loads((tmp_path/'configs/devices.json').read_text())["devices"][0]
        assert saved["daemon_resource_id"] == "daemon:arm-id"
        assert saved["daemon_binding_id"] == before
        assert runtime.devices.get("arm-id")["daemon_binding_id"] == before
        replay = client.put("/v1/devices/arm-id/binding",json=body)
        assert replay.json() == response.json()
    restarted, app = make_runtime(tmp_path,devices=[saved])
    with TestClient(app):
        assert restarted.devices.get("arm-id")["daemon_binding_id"] == before


@pytest.mark.parametrize("field,value",[("daemon_addr","127.0.0.1:8896"),
                                         ("serial_port","/dev/cu.other")])
def test_actual_binding_change_invalidates_old_identity_and_calibration(tmp_path,field,value):
    runtime, app = make_runtime(tmp_path,devices=[config()])
    with TestClient(app) as client:
        old = runtime.devices.get("arm-id")["daemon_binding_id"]
        response = client.put('/v1/devices/arm-id/binding',
            json={**_op("更换真实连接",dangerous=True),field:value})
        assert response.status_code == 200,response.text
        assert runtime.devices.get("arm-id")["daemon_binding_id"] != old
        assert response.json()["calibration_status"] == "due"


def test_missing_arm_identity_never_becomes_camera_identity():
    from runtime.layout_capture import frame_from_record
    record = dict(evidence_id="e",camera_id="c",camera_unique_id="uvc",camera_binding_id="cam-binding",
        source_geometry_id="geo",frame_width=100,frame_height=100,source_generation_id="generation",
        source_frame_seq=1,source_capture_seq=1,source_clock_id="clock",source_captured_monotonic_ns=1,
        capture_uncertainty_ms=1,timing_provenance="source_monotonic")
    with pytest.raises(ValueError,match="机械臂"):
        frame_from_record(record,read_bytes=lambda _:b"image",arm_binding_id="")


def test_source_device_selector_change_and_device_slot_change_revoke_old_identity():
    from runtime.arm_binding import normalize_daemon_binding
    old = normalize_daemon_binding(config())
    for field,value in [("daemon_resource_id","daemon:other"),("device_id","arm-other")]:
        changed = normalize_daemon_binding({**old,field:value})
        assert changed["daemon_binding_id"] != old["daemon_binding_id"]
    cleared = normalize_daemon_binding({**old,"daemon_addr":""})
    assert "daemon_binding_id" not in cleared


def test_real_region_and_draft_capture_share_registered_arm_identity(tmp_path):
    import uuid
    from runtime.arm_binding import normalize_daemon_binding
    from tests.layout_d2_helpers import AppEnv,FrameBridgeFake,phone_scene,write_v3_bundle

    with AppEnv(tmp_path) as env:
        # AppEnv uses a synthetic explicit arm identity for older tests. Restore
        # actual registration-derived identity to exercise production providers.
        cfg = next(c for c in env.runtime.devices._registered_configs if c["device_id"] == "arm-01")
        identity = normalize_daemon_binding(cfg)["daemon_binding_id"]
        env.runtime.devices._devices["arm-01"]["daemon_binding_id"] = identity
        bridge = FrameBridgeFake(env.runtime)
        first = bridge.push(phone_scene())
        bridge.push(phone_scene())
        write_v3_bundle(tmp_path)
        headers = {**env.auth(),"Content-Type":"application/json"}
        preview = env.client.post('/v1/devices/arm-01/phone-search-region/preview',json={
            "contract_version":"phone-search-region-preview.v1","correlation_id":str(uuid.uuid4()),
            "rect":{"x":.2,"y":.15,"width":.65,"height":.8},
            "source_evidence_id":first["evidence_id"]},headers=headers)
        assert preview.status_code == 200,preview.text
        assert preview.json()["source_binding"]["installation_binding"]["arm_binding_id"] == identity
        created = env.client.post('/v1/layout-template-drafts',json={
            "client_request_id":str(uuid.uuid4()),"device_id":"arm-01","base_template_ref":None,
            "contract_version":"ui-layout-edit.v1","correlation_id":str(uuid.uuid4())},headers=headers)
        assert created.status_code == 200,created.text
        draft_id = created.json()["draft_id"]
        draft = env.client.get(f'/v1/layout-template-drafts/{draft_id}',headers={
            **env.auth(),"X-Layout-Contract-Version":"ui-layout-edit.v1",
            "X-Correlation-Id":str(uuid.uuid4())})
        assert draft.status_code == 200,draft.text
        assert draft.json()["draft"]["binding_snapshot"]["installation_binding"]["arm_binding_id"] == identity
        capture = env.client.post('/v1/devices/arm-01/layout-captures',json={
            "client_request_id":str(uuid.uuid4()),"owner_kind":"draft","owner_id":draft_id,
            "expected_owner_revision":1,"page_state":"wechat_list","controlled_test_confirmed":True,
            "contract_version":"layout-capture.v1","correlation_id":str(uuid.uuid4())},headers=headers)
        assert capture.status_code == 200,capture.text
        for row in capture.json()["capture"]["evidence"]:
            assert row["source_binding"]["installation_binding"]["arm_binding_id"] == identity

        # A changed actual endpoint cannot publish a preview from the old arm.
        changed = normalize_daemon_binding({**cfg,"daemon_addr":"127.0.0.1:8899"})
        env.runtime.devices._devices["arm-01"]["daemon_binding_id"] = changed["daemon_binding_id"]
        stale = env.client.put('/v1/devices/arm-01/phone-search-region',json={
            "contract_version":"phone-search-region-write.v1","correlation_id":str(uuid.uuid4()),
            "client_request_id":str(uuid.uuid4()),"expected_revision":0,
            "preview_id":preview.json()["preview_id"],"region_digest":preview.json()["region_digest"],
            "publish":True},headers=headers)
        assert stale.status_code == 409,stale.text
        assert stale.json()["code"] == "layout_binding_changed"
