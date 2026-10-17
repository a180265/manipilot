"""The production app mounts calibration metadata under the same phone policy."""
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs
from tests.test_gesture_coordinate_frame import LEGACY_SESSION, _hello, _page
from tests.test_r3_17_pairing_routes import TRUST
from tests.test_v110_repair_ime_geometry import snapshot_body


@pytest.mark.parametrize("paired_deployment", [False, True])
@pytest.mark.parametrize("deferred_runtime", [False, True])
def test_legacy_metadata_uses_existing_deployment_identity_policy(
        tmp_path, monkeypatch, paired_deployment, deferred_runtime):
    runtime = ArmFleetRuntime(PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "real", "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False, "screen_ime_enabled": True,
    }, root=tmp_path))
    runtime.ime_pairing_trust = dict(TRUST) if paired_deployment else None
    runtime.register_phone_credential("ime-01", "test-registered-phone")
    if deferred_runtime:
        monkeypatch.setattr("runtime.config.load_platform_config", lambda: runtime.config)
        app = create_app()
        app.state.runtime_factory = lambda: runtime
    else:
        app = create_app(runtime)
    app.state.device_configs = make_device_configs(1)
    hello = _hello(_page("legacy-calibration", legacy=LEGACY_SESSION))
    with TestClient(app) as client:
        heartbeat = client.post("/api/v1/devices/ime-01/heartbeat",
                                json={"keyboardActive": True})
        assert heartbeat.status_code == (401 if paired_deployment else 200)
        path = "/api/v1/devices/ime-01/gesture-client"
        response = client.post(path, json=hello)
        if paired_deployment:
            assert response.status_code == 401
            invalid = client.post(path, json=hello,
                                  headers={"x-ime-phone-token": "invalid"})
            assert invalid.status_code == 401
            other = client.post("/api/v1/devices/other/gesture-client", json=hello,
                                headers={"x-ime-phone-token": "test-registered-phone"})
            assert other.status_code == 403
            response = client.post(path, json=hello,
                                   headers={"x-ime-phone-token": "test-registered-phone"})
        assert response.status_code == 200, response.text
        metadata = client.get("/api/v1/gesture-protocol/legacy-metadata",
                              params={"deviceId": "ime-01", "sessionId": LEGACY_SESSION})
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["page"]["rootOriginDisplayPx"] == [0, 80]
        assert metadata.json()["sessionId"] == LEGACY_SESSION
        # The same deployment choice must reach live IME geometry as well.
        runtime.devices._devices["arm-01"]["ime_device_id"] = "ime-01"
        headers = ({"x-ime-phone-token": "test-registered-phone"}
                   if paired_deployment else {})
        geometry_path = "/api/v1/devices/ime-01/geometry"
        handshake = {"contract_version": "ime-geometry.v1", "imeGeometryVersion": 1,
                     "client_instance_id": hello["clientInstanceId"]}
        if paired_deployment:
            assert client.post(geometry_path + "/sessions", json=handshake).status_code == 401
        session = client.post(geometry_path + "/sessions", json=handshake, headers=headers)
        assert session.status_code == 200, session.text
        session_id = session.json()["runtime_session_id"]
        nonce = runtime.ime_geometry.create_nonce_request("ime-01")
        poll = client.get(geometry_path + "/requests/next",
                          params={"runtime_session_id": session_id}, headers=headers)
        assert poll.status_code == 200, poll.text
        assert poll.json()["request_nonce"] == nonce
        snapshot = snapshot_body(session_id, 1, nonce)
        report = client.post(geometry_path, json=snapshot, headers=headers)
        assert report.status_code == 200, report.text
        gate = runtime._ime_geometry_final_gate(
            device_id="arm-01", ime_device="ime-01", target_id="ime_candidate_bar",
            nonce=nonce, expected_task=snapshot["current_task_id"],
            expected_session=session_id, expected_display=snapshot["display"])
        gate()
        from drivers.arm import DriverError
        with pytest.raises(DriverError, match="nonce"):
            gate()

        # A phone that is no longer uniquely bound cannot replace the session.
        runtime.devices._devices["arm-01"]["ime_device_id"] = "other"
        rejected = client.post(geometry_path + "/sessions", json=handshake, headers=headers)
        assert rejected.status_code == 403, rejected.text


@pytest.mark.parametrize("content", [None, "invalid json", "{}", "[]"])
def test_explicit_bad_pairing_trust_refuses_startup(tmp_path, monkeypatch, content):
    trust_path = tmp_path / "trust.json"
    if content is not None:
        trust_path.write_text(content)
    monkeypatch.setenv("ARMFLEET_IME_PAIRING_TRUST_FILE", str(trust_path))
    with pytest.raises(ValueError, match="配对信任"):
        ArmFleetRuntime._load_ime_pairing_trust()
