"""Quality diagnostics use synthetic frames only; no hardware or services."""
import asyncio
import json
import socket
import subprocess
from copy import deepcopy

import cv2
import numpy as np
import pytest

from drivers.camera_broker import CameraFrameBroker
from drivers import FakeArmDriver, FakeCameraDriver, FakeInputDriver
from drivers.real_camera import RealCameraDriver
from runtime.audit import AuditLog
from runtime.devices import DeviceManager
from runtime.events import EventBus
from runtime.store import Store


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("hardware, network and subprocess I/O are forbidden")

    monkeypatch.setattr(cv2, "VideoCapture", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


class Frames:
    stream_alive = True

    def __init__(self):
        self.image = np.full((48, 64, 3), 120, np.uint8)
        self.error = None

    def __call__(self):
        if self.error:
            raise self.error
        return self.image


@pytest.mark.parametrize("via_broker", [False, True])
def test_unlocatable_frame_has_safe_diagnostic_without_changing_quality(via_broker):
    driver = RealCameraDriver("cam-test", frame_provider=Frames())
    checker = CameraFrameBroker(driver, camera_id="cam-test") if via_broker else driver

    result = asyncio.run(checker.check_quality())

    assert result["quality"] == "abnormal"
    assert result["quality_display"] == "画面异常"
    assert result.get("reason_code") == "screen_roi_not_found"
    assert result.get("note") == "画面中无法定位手机屏幕"
    assert result.get("metrics") == {}
    json.dumps(result, allow_nan=False)


def test_capture_failure_keeps_broker_exception_and_uses_same_safe_diagnostic():
    source = Frames()
    source.error = RuntimeError("https://user:secret@camera.invalid/private/path")
    driver = RealCameraDriver("cam-test", frame_provider=source)
    broker = CameraFrameBroker(driver, camera_id="cam-test")
    events = []
    broker.subscribe(events.append)

    direct = asyncio.run(driver.check_quality())
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(broker.check_quality())

    assert direct["quality"] == "abnormal"
    assert direct["quality_display"] == "画面异常"
    assert direct.get("reason_code") == "camera_capture_failed"
    assert direct.get("note") == "摄像头取流失败，当前画面不可用"
    assert raised.value.reason_code == direct["reason_code"]
    assert str(raised.value) == direct["note"]
    assert raised.value.diagnostics == {
        key: direct[key] for key in ("reason_code", "note", "metrics")
    }
    assert broker.unavailable is True
    assert events[-1].status == "camera_unavailable"
    assert events[-1].reason_code == "camera_capture_failed"
    assert events[-1].detail == direct["note"]
    assert broker.last_error == direct["note"]
    assert "secret" not in json.dumps(direct)


@pytest.mark.parametrize("via_broker", [False, True])
@pytest.mark.parametrize(
    "message,code,note",
    [
        ("secret=/private/token", "camera_quality_check_failed",
         "画面质量检查异常，原因尚未确认"),
        ("画面与校准绑定不符", "camera_calibration_binding_mismatch", "画面与校准绑定不符"),
        ("校准缺少屏幕区域", "camera_calibration_screen_missing", "校准缺少屏幕区域"),
        ("校准屏幕区域无效", "camera_quality_geometry_invalid", "校准屏幕区域无效"),
    ],
)
def test_quality_exceptions_are_allowlisted(monkeypatch, via_broker, message, code, note):
    driver = RealCameraDriver("cam-test", frame_provider=Frames())

    def fail_geometry(frame):
        raise RuntimeError(message)

    monkeypatch.setattr(driver, "_quality_geometry", fail_geometry)
    checker = CameraFrameBroker(driver, camera_id="cam-test") if via_broker else driver
    result = asyncio.run(checker.check_quality())
    assert result["quality"] == "abnormal"
    assert result["quality_display"] == "画面异常"
    assert result.get("reason_code") == code
    assert result.get("note") == note
    assert result.get("metrics") == {}
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("via_broker", [False, True])
def test_decode_failure_is_not_misreported_as_missing_screen(monkeypatch, via_broker):
    driver = RealCameraDriver("cam-test", frame_provider=Frames())
    monkeypatch.setattr(driver, "capture_sync", lambda: {
        "image": b"not-an-image", "frozen": False,
    })
    checker = CameraFrameBroker(driver, camera_id="cam-test") if via_broker else driver
    result = asyncio.run(checker.check_quality())
    assert result["quality"] == "abnormal"
    assert result.get("reason_code") == "camera_frame_decode_failed"
    assert result.get("note") == "画面解码失败"


@pytest.mark.parametrize("via_broker", [False, True])
def test_recovery_clears_old_reason_and_keeps_current_metrics(via_broker):
    async def scenario():
        source = Frames()
        driver = RealCameraDriver("cam-test", frame_provider=source)
        checker = CameraFrameBroker(driver, camera_id="cam-test") if via_broker else driver
        failed = await checker.check_quality()
        assert failed.get("reason_code") == "screen_roi_not_found"
        source.image = np.full((240, 320, 3), 65, np.uint8)
        source.image[30:210, 95:225] = np.random.default_rng(23).integers(
            90, 230, (180, 130, 3), dtype=np.uint8)
        if via_broker:
            await checker.capture()
        recovered = await checker.check_quality()
        assert recovered["quality"] == "clear"
        assert recovered["quality_display"] == "清晰"
        assert recovered.get("reason_code", "missing") is None
        assert recovered.get("note", "missing") is None
        assert set(recovered["metrics"]) == {"brightness", "texture", "blur_variance"}
        assert recovered["metrics"]["brightness"] > 90
        json.dumps(recovered, allow_nan=False)

    asyncio.run(scenario())


class ReportCamera(FakeCameraDriver):
    def __init__(self):
        super().__init__("cam-test")
        self.report = {
            "quality": "abnormal", "quality_display": "画面异常",
            "reason_code": "screen_roi_not_found",
            "note": "do not forward /private/token",
            "metrics": {},
        }

    async def check_quality(self):
        return deepcopy(self.report)


@pytest.fixture
def managed_camera(tmp_path, monkeypatch):
    store = Store(tmp_path / "diagnostics.db")
    events = EventBus()
    manager = DeviceManager(store, AuditLog(store, events), events, config_root=tmp_path)
    camera = ReportCamera()
    monkeypatch.setattr(manager, "_create_drivers", lambda cfg: (
        FakeArmDriver("arm-test"), camera, FakeInputDriver("arm-test"), "test", "simulation",
    ))
    cfg = {"device_id": "arm-test", "camera_id": "cam-test", "driver_mode": "simulation"}
    manager.register_device(cfg)
    try:
        yield manager, camera, store, events, cfg
    finally:
        store._conn.close()


async def poll_once(manager, monkeypatch):
    original_sleep = asyncio.sleep
    completed = asyncio.Event()

    async def end_iteration(delay):
        if delay == 10:
            completed.set()
            await original_sleep(3600)
        else:
            await original_sleep(delay)

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "sleep", end_iteration)
        manager._running = True
        task = asyncio.create_task(manager._camera_loop())
        try:
            await asyncio.wait_for(completed.wait(), 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            manager._running = False


def device_events(events):
    return [e["data"] for e in events.recent_events(500) if e["event_type"] == "device.updated"]


def test_selfcheck_exposes_safe_diagnostics_in_all_device_views(managed_camera):
    manager, camera, store, events, cfg = managed_camera
    asyncio.run(manager.register_and_selfcheck(cfg, replacing=True))
    expected = {
        "camera_quality_reason_code": "screen_roi_not_found",
        "camera_quality_note": "画面中无法定位手机屏幕",
        "camera_quality_metrics": {},
    }
    for view in (manager.get("arm-test"), manager.all()[0],
                 manager.binding_view("arm-test"), store.get_device("arm-test"),
                 device_events(events)[-1]):
        assert {key: view.get(key) for key in expected} == expected
        assert view["camera_quality"] == "abnormal"
        assert view["camera_quality_display"] == "画面异常"
        assert "/private/token" not in json.dumps(view)
    assert manager.get("arm-test")["status"] == "degraded"


def test_reason_changes_emit_events_and_recovery_does_not_restore_mode(
        managed_camera, monkeypatch):
    manager, camera, store, events, cfg = managed_camera

    async def scenario():
        device = manager.get("arm-test")
        device.update(status="paused", status_display="暂停", camera_quality="clear",
                      device_mode="managed", calibration_status="validated", home_ok=False)
        await poll_once(manager, monkeypatch)
        assert device["device_mode"] == "unmanaged"
        assert len(store.all_alerts()) == 1
        assert device_events(events)[-1].get("camera_quality_reason_code") == "screen_roi_not_found"
        event_count = len(device_events(events))
        camera.report.update(reason_code="camera_quality_check_failed", note="secret")
        await poll_once(manager, monkeypatch)
        assert len(device_events(events)) == event_count + 1
        assert device_events(events)[-1].get("camera_quality_reason_code") == "camera_quality_check_failed"
        assert len(store.all_alerts()) == 1
        await poll_once(manager, monkeypatch)
        assert len(device_events(events)) == event_count + 1
        camera.report.update(quality="clear", quality_display="清晰",
                             metrics={"brightness": 120.126, "texture": 40.005,
                                      "blur_variance": 200.123, "path": "/private/token"})
        await poll_once(manager, monkeypatch)
        assert device["camera_quality"] == "clear"
        assert device.get("camera_quality_reason_code", "missing") is None
        assert device.get("camera_quality_note", "missing") is None
        assert device.get("camera_quality_metrics") == {
            "brightness": 120.13, "texture": 40.01, "blur_variance": 200.12,
        }
        assert device["status"] == "paused"
        assert device["home_ok"] is False
        assert device["calibration_status"] == "validated"
        assert device["device_mode"] == "unmanaged"
        assert len(store.all_alerts()) == 1
        recovered_event = deepcopy(device_events(events)[-1])
        camera.report["metrics"]["brightness"] = 125
        await poll_once(manager, monkeypatch)
        assert len(device_events(events)) == event_count + 2
        assert device.get("camera_quality_metrics", {}).get("brightness") == 125
        assert device_events(events)[-1] == recovered_event

    asyncio.run(scenario())


def test_stream_failure_only_updates_diagnostics_and_preserves_existing_safety_fields(
        managed_camera, monkeypatch):
    manager, camera, store, events, cfg = managed_camera
    source = Frames()
    source.error = RuntimeError("secret=/private/camera")
    manager._cameras["arm-test"] = RealCameraDriver("cam-test", frame_provider=source)

    async def scenario():
        device = manager.get("arm-test")
        device.update(status="paused", status_display="暂停", camera_quality="clear",
                      camera_quality_display="清晰", device_mode="unmanaged",
                      home_ok=False, last_camera_check_at="previous-check")
        keys = ("camera_quality", "camera_quality_display", "device_mode", "status",
                "home_ok", "calibration_status", "last_camera_check_at", "current_ui_state")
        before = {key: device.get(key) for key in keys}
        await poll_once(manager, monkeypatch)
        assert {key: device.get(key) for key in keys} == before
        assert device.get("camera_quality_reason_code") == "camera_capture_failed"
        assert device.get("camera_quality_note") == "摄像头取流失败，当前画面不可用"
        assert device.get("camera_quality_metrics") == {}
        assert len(device_events(events)) == 1
        assert not store.all_alerts()
        await poll_once(manager, monkeypatch)
        assert len(device_events(events)) == 1
        assert "secret" not in json.dumps(device_events(events))

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "secret", 10 ** 400])
def test_metrics_are_bounded_to_finite_numeric_allowlist(managed_camera, monkeypatch, value):
    manager, camera, store, events, cfg = managed_camera
    camera.report["metrics"] = {"brightness": value, "texture": 12.344,
                                "blur_variance": 91, "token": "secret"}
    asyncio.run(poll_once(manager, monkeypatch))
    device = manager.all()[0]
    assert device.get("camera_quality_metrics") == {"texture": 12.34, "blur_variance": 91}
    json.dumps(device, allow_nan=False)
    assert "secret" not in json.dumps(device_events(events))


@pytest.mark.parametrize("previous_status", ["paused", "quarantined", "ready"])
@pytest.mark.parametrize("camera_state", ["clear", "abnormal", "open_failed"])
def test_startup_disconnected_daemon_preserves_holds_without_restoring_execution(
        managed_camera, monkeypatch, tmp_path, previous_status, camera_state):
    manager, camera, store, events, cfg = managed_camera
    previous = dict(manager.get("arm-test"))
    previous.update(status=previous_status, device_mode="unmanaged",
                    pause_reason="operator hold" if previous_status == "paused" else None,
                    quarantine_reason="operator isolation" if previous_status == "quarantined" else None)
    store.save_device(previous)

    class DisconnectedDaemon(FakeArmDriver):
        def __init__(self):
            super().__init__("arm-test")
            self.data_root = tmp_path / "data"

        async def connect(self):
            raise RuntimeError("fake daemon is disconnected")

    daemon = DisconnectedDaemon()
    restarted = DeviceManager(store, AuditLog(store, events), events,
                              hardware_mode="real", config_root=tmp_path)
    monkeypatch.setattr(restarted, "_create_drivers", lambda config: (
        daemon, camera, FakeInputDriver("arm-test"), "fake daemon", "real",
    ))
    camera.report.update(quality=camera_state, quality_display={
        "clear": "清晰", "abnormal": "画面异常", "open_failed": "未知",
    }[camera_state])
    if camera_state == "open_failed":
        async def failed_open():
            raise RuntimeError("fake camera is disconnected")
        monkeypatch.setattr(camera, "open", failed_open)
    cfg = {**cfg, "driver_mode": "daemon", "calibration_status": "due"}

    result = asyncio.run(restarted.register_and_selfcheck(cfg))

    expected_status = previous_status if previous_status != "ready" else "offline"
    assert result["status"] == expected_status
    assert result["status_display"] == {
        "paused": "暂停", "quarantined": "隔离", "offline": "离线",
    }[expected_status]
    assert result["device_mode"] == "unmanaged"
    assert result["calibration_status"] == "due"
    assert not result.get("daemon_session")
    assert daemon.connected is False
    assert "机械臂连接失败" in result["status_reason"]
    assert result["pause_reason"] == previous["pause_reason"]
    assert result["quarantine_reason"] == previous["quarantine_reason"]
    assert store.get_device("arm-test")["status"] == expected_status
    assert not restarted.eligibility("arm-test")["can_accept_task"]
    if camera_state != "open_failed":
        assert result["camera_quality"] == camera_state
    emitted_statuses = [event["status"] for event in device_events(events)]
    assert emitted_statuses and set(emitted_statuses) == {expected_status}
    assert any("机械臂连接失败" in a["message"] for a in store.all_alerts())


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("change", ["device", "camera", "version"])
def test_old_attachment_cannot_publish_diagnostics_after_rebind(
        managed_camera, monkeypatch, fail, change):
    manager, camera, store, events, cfg = managed_camera

    async def replaced_during_quality_check():
        if change == "device":
            manager._devices["arm-test"] = dict(manager.get("arm-test"))
        elif change == "camera":
            manager._cameras["arm-test"] = ReportCamera()
        else:
            manager._lifecycle_versions["arm-test"] = (
                manager._lifecycle_versions.get("arm-test", 0) + 1)
        if fail:
            raise RuntimeError("secret=/private/stale-camera")
        return deepcopy(camera.report)

    monkeypatch.setattr(camera, "check_quality", replaced_during_quality_check)
    asyncio.run(poll_once(manager, monkeypatch))
    assert manager.get("arm-test")["camera_quality"] == "unknown"
    assert manager.get("arm-test").get("camera_quality_reason_code") is None
    assert not device_events(events)
    assert not store.all_alerts()


def test_unknown_checker_failure_and_cancellation_keep_exception_control_flow(
        managed_camera, monkeypatch):
    manager, camera, store, events, cfg = managed_camera

    async def broken_check():
        raise RuntimeError("secret=/private/failure")

    monkeypatch.setattr(camera, "check_quality", broken_check)
    asyncio.run(poll_once(manager, monkeypatch))
    assert manager.get("arm-test")["camera_quality"] == "unknown"
    assert manager.get("arm-test").get("camera_quality_reason_code") == "camera_quality_check_failed"
    assert "secret" not in json.dumps(device_events(events))

    async def cancelled_check():
        raise asyncio.CancelledError()

    monkeypatch.setattr(camera, "check_quality", cancelled_check)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager.camera_broker("arm-test").check_quality())


def test_stream_recovery_clears_diagnostic_without_reopening_camera(
        managed_camera, monkeypatch):
    manager, camera, store, events, cfg = managed_camera
    source = Frames()
    source.error = RuntimeError("secret")
    driver = RealCameraDriver("cam-test", frame_provider=source)
    manager._cameras["arm-test"] = driver

    async def scenario():
        await poll_once(manager, monkeypatch)
        assert manager.get("arm-test").get("camera_quality_reason_code") == "camera_capture_failed"
        source.error = None
        source.image = np.full((240, 320, 3), 65, np.uint8)
        source.image[30:210, 95:225] = np.random.default_rng(23).integers(
            90, 230, (180, 130, 3), dtype=np.uint8)
        await poll_once(manager, monkeypatch)
        device = manager.get("arm-test")
        assert device["camera_quality"] == "clear"
        assert device.get("camera_quality_reason_code", "missing") is None
        assert device.get("camera_quality_note", "missing") is None
        assert device["camera_quality_metrics"]["brightness"] > 90
        assert not manager.camera_broker("arm-test").unavailable
        assert manager.camera_broker("arm-test").last_error == ""

    asyncio.run(scenario())


def test_open_failure_does_not_leak_through_device_status_reason(managed_camera, monkeypatch):
    manager, camera, store, events, cfg = managed_camera

    async def fail_open():
        raise RuntimeError("secret=/private/camera-config")

    monkeypatch.setattr(camera, "open", fail_open)
    result = asyncio.run(manager.register_and_selfcheck(cfg, replacing=True))
    assert result["status"] == "degraded"
    assert result.get("camera_quality_reason_code") == "camera_capture_failed"
    assert "secret" not in json.dumps(result)
    assert "/private" not in json.dumps(events.recent_events(500))


@pytest.mark.parametrize("via_broker", [False, True])
def test_low_detail_frame_is_not_misreported_as_check_exception(via_broker):
    source = Frames()
    source.image = np.full((240, 320, 3), 210, np.uint8)
    source.image[30:210, 30:290] = 128
    driver = RealCameraDriver("cam-test", frame_provider=source)
    checker = CameraFrameBroker(driver, camera_id="cam-test") if via_broker else driver
    result = asyncio.run(checker.check_quality())
    assert result["quality"] == "abnormal"
    assert result["quality_display"] == "画面异常"
    assert result.get("reason_code") == "camera_detail_insufficient"
    assert result.get("note") == "画面细节不足，清晰度未达到要求"
    assert result["metrics"]["blur_variance"] == 0
