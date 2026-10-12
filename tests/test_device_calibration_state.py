"""真实驱动加假 daemon：公有设备状态与校准门禁必须一致。"""
from __future__ import annotations

from contextlib import contextmanager
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from drivers import calibration_bundle as cb
from runtime.config import PlatformConfig
from runtime.devices import DeviceManager
from runtime.runtime import ArmFleetRuntime
from tests.test_daemon_arm import FakeDaemon
from tests.test_live_calibration_gate import ready_bundle


@contextmanager
def device_client(tmp_path, monkeypatch, *, saved_session=None, edit_bundle=None, device_updates=None):
    data_root = tmp_path / "data"
    monkeypatch.setattr(cb, "default_data_root", lambda: data_root)
    monkeypatch.setattr("drivers.real_stubs.require_confirmed_hardware", lambda: None)
    daemon = FakeDaemon().start()
    try:
        address = f"127.0.0.1:{daemon.port}"
        bundle = ready_bundle()
        bundle["daemonAddr"] = address
        bundle["daemonSession"] = saved_session or daemon.session
        bundle["bindingFingerprint"] = cb.binding_fingerprint(
            device_id="arm-01", camera_id="cam-01",
            camera_device_unique_id="uvc-01", daemon_addr=address,
            frame_width=64, frame_height=48,
        )
        if edit_bundle:
            edit_bundle(bundle)
        cb.save_bundle(bundle, data_root)
        archive = cb.bundle_path("arm-01", data_root)
        initial_bytes = archive.read_bytes()
        rng = np.random.default_rng(0)
        config = PlatformConfig({
            "hardware_mode": "real",
            "daemon_supervisor_enabled": False,
            "hotplug_discovery_enabled": False,
            "screen_ime_enabled": False,
        }, root=tmp_path)
        runtime = ArmFleetRuntime(config)
        app = create_app(runtime)
        def phone_frame():
            image = np.zeros((48, 64, 3), dtype=np.uint8)
            image[5:43, 5:59] = 115
            image[10:38, 12:50] = rng.integers(
                75, 190, size=(28, 38, 3), dtype=np.uint8)
            return image

        app.state.device_configs = [{
            "device_id": "arm-01",
            "driver_mode": "daemon",
            "binding_mode": "real",
            "daemon_addr": address,
            "camera_id": "cam-01",
            "camera_device_unique_id": "uvc-01",
            "binding_revision": 7,
            "expected_frame_width": 64,
            "expected_frame_height": 48,
            "camera_frame_provider": phone_frame,
            "data_root": data_root,
            **(device_updates or {}),
        }]
        with TestClient(app) as client:
            yield client, runtime, daemon, archive
        assert archive.read_bytes() == initial_bytes
        assert not daemon.commands
        assert not any(command == "CONNECT" for _connection, command in daemon.requests)
    finally:
        daemon.stop()


def test_daemon_driver_uses_configured_calibration_root(tmp_path, monkeypatch):
    monkeypatch.setattr("drivers.real_stubs.require_confirmed_hardware", lambda: None)
    root = tmp_path / "isolated-calibration"
    manager = DeviceManager(None, None, None, hardware_mode="real")

    arm, camera, _, _, mode = manager._create_drivers({
        "device_id": "arm-01",
        "driver_mode": "daemon",
        "camera_id": "cam-01",
        "camera_frame_provider": lambda: np.zeros((48, 64, 3), np.uint8),
        "data_root": root,
    })

    assert mode == "real"
    assert arm.data_root == root
    assert camera.data_root == root


def test_public_device_and_eligibility_report_old_session_as_recalibration_due(tmp_path, monkeypatch):
    with device_client(tmp_path, monkeypatch, saved_session="old-session") as (client, _runtime, _daemon, _archive):
        listed = client.get("/v1/devices").json()["devices"][0]
        detail = client.get("/v1/devices/arm-01").json()
        eligibility = client.get("/v1/devices/arm-01/eligibility?require_input=false").json()

        for device in (listed, detail):
            assert device["calibration_status"] == "due"
            assert device["calibration_status_display"] == "待重新校准"
            assert "会话" in device["calibration_status_reason"]
            assert "daemon_session_changed" in {check["code"] for check in device["calibration_checks"]}
            assert device["device_mode"] == "unmanaged"
        assert eligibility["calibration_status"] == "due"
        assert "calibration_due" in eligibility["blocking_checks"]
        assert eligibility["can_accept_task"] is False


def test_manual_validated_marker_cannot_override_current_session_check(tmp_path, monkeypatch):
    with device_client(tmp_path, monkeypatch, saved_session="old-session") as (client, _runtime, _daemon, _archive):
        response = client.post("/v1/devices/arm-01/calibration-status", json={
            "status": "validated", "reason": "历史人工校准标记",
        })

        assert response.status_code == 200
        device = response.json()
        assert device["calibration_status"] == "due"
        assert device["calibration_marked_status"] == "validated"
        assert device["calibration_marked_at"]
        assert device["calibration_validated_at"] is None
        assert device["eligibility"]["calibration_status"] == "due"


def test_heartbeat_does_not_restore_invalid_archive_or_emit_false_restoration(tmp_path, monkeypatch):
    with device_client(
        tmp_path, monkeypatch, edit_bundle=lambda bundle: bundle["z"].update(validated=False),
    ) as (client, _runtime, _daemon, _archive):
        device = client.get("/v1/devices/arm-01").json()
        assert device["calibration_status"] == "due"
        previous_heartbeat = device["last_heartbeat_at"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.05)
            device = client.get("/v1/devices/arm-01").json()
            if device["last_heartbeat_at"] != previous_heartbeat:
                break
        else:
            pytest.fail("假 daemon 心跳未按时更新")

        audits = client.get("/v1/audit-events").json()["events"]
        assert not [event for event in audits if event["action"] == "calibration_restored"]
        assert device["calibration_status"] == "due"
        assert "z_parameters_not_validated" in {check["code"] for check in device["calibration_checks"]}


def test_matching_archive_retains_validated_status_and_task_eligibility(tmp_path, monkeypatch):
    with device_client(tmp_path, monkeypatch) as (client, _runtime, _daemon, _archive):
        device = client.get("/v1/devices/arm-01").json()
        eligibility = client.get("/v1/devices/arm-01/eligibility?require_input=false").json()

        assert device["calibration_status"] == "validated"
        assert device["calibration_status_display"] == "已校准"
        assert device["calibration_checks"] == []
        assert device["calibration_status_reason"] is None
        assert eligibility["can_accept_task"] is True


@pytest.mark.parametrize(("updates", "code"), [
    ({"camera_id": "cam-02"}, "camera_binding_changed"),
    ({"camera_device_unique_id": "uvc-02"}, "camera_unique_id_changed"),
    ({"binding_revision": 8}, "camera_binding_revision_changed"),
    ({"expected_frame_width": 128}, "camera_resolution_changed"),
])
def test_changed_binding_cannot_inherit_archive_validated_status(tmp_path, monkeypatch, updates, code):
    with device_client(tmp_path, monkeypatch, device_updates=updates) as (client, _runtime, _daemon, _archive):
        device = client.get("/v1/devices").json()["devices"][0]
        assert device["calibration_status"] == "due"
        assert device["calibration_status_display"] == "待重新校准"
        assert code in {check["code"] for check in device["calibration_checks"]}
        eligibility = client.get("/v1/devices/arm-01/eligibility?require_input=false").json()
        assert eligibility["can_accept_task"] is False


def test_device_events_and_eligibility_expose_same_calibration_reason(tmp_path, monkeypatch):
    with device_client(tmp_path, monkeypatch, saved_session="old-session") as (client, runtime, _daemon, _archive):
        device = client.get("/v1/devices/arm-01").json()
        eligibility = client.get("/v1/devices/arm-01/eligibility?require_input=false").json()
        event = next(event["data"] for event in reversed(runtime.events.recent_events())
                     if event["event_type"] == "device.updated")

        assert event["calibration_status"] == "due"
        assert event["calibration_status_display"] == "待重新校准"
        assert event["calibration_status_reason"] == device["calibration_status_reason"]
        assert eligibility["calibration_checks"] == device["calibration_checks"]
        assert eligibility["calibration_status_reason"] == device["calibration_status_reason"]


def test_heartbeat_restores_motion_ready_archive(tmp_path, monkeypatch):
    """A2（2026-10-01 backlog）：motion_ready 是合法可运行态（机械资格完整、
    输入延后）——心跳故障撤销资格后的自动恢复不得只认字面 "ready"，
    否则默认 PROMOTE（motion_ready）的设备每次断线都被降级为待校准。"""
    import time as _time
    from drivers import calibration_bundle as cb

    def force_motion_ready(bundle):
        # 完整 motion_ready 形态（9-29 拍板）：机械资格全链路 + 输入延后标记
        bundle["status"] = cb.MOTION_READY_STATUS
        bundle["modelLocks"] = {key: {"state": cb.MODEL_LOCK_LOCKED, "version": 1}
                                for key in ("cameraToScreen", "armAffine")}
        bundle["validation"] = {
            "passed": True, "runId": "run-motion-ready", "tolerancePx": 10.0,
            "points": [{"targetId": f"validation-{index}", "passed": True,
                        "touchTargetErrorPx": 1.0} for index in range(1, 6)]}
        bundle["z"]["zSafe"] = bundle["z"]["hoverZ"]
        bundle["z"]["deleteValidated"] = False
        bundle["safePark"] = {"confirmed": True, "parkX": 0.0, "parkY": 0.0,
                              "parkZ": bundle["z"]["zSafe"]}
        bundle["acceptance"] = {"debug": False, "wechat": False, "checks": [],
                                "deferred": {"debug": True}}

    with device_client(tmp_path, monkeypatch, edit_bundle=force_motion_ready) as (
            client, runtime, _daemon, _archive):
        # 模拟一次心跳故障曾撤销运行资格（marked_status 仍 validated=非人工重标）
        device_view = runtime.devices._devices["arm-01"]
        device_view["calibration_status"] = "due"
        device_view["calibration_marked_status"] = "validated"
        # 等心跳循环走 restore 分支（默认 1s 周期，留 3 倍余量）
        deadline = _time.monotonic() + 6
        status = None
        while _time.monotonic() < deadline:
            status = client.get("/v1/devices/arm-01").json()["calibration_status"]
            if status == "validated":
                break
            _time.sleep(0.3)
        assert status == "validated", "motion_ready 档案应在心跳恢复分支转回已校准"
        # 恢复必须发生在外科层级（存储状态被心跳写回 validated），
        # 而不是读接口的即时重算掩盖了 due。
        stored = runtime.devices._devices["arm-01"]["calibration_status"]
        assert stored == "validated", f"存储状态仍为 {stored}：恢复分支未执行"
