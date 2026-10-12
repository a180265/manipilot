"""Runtime verifier wiring only: temporary data, no camera reads or hardware IO."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drivers import daemon_arm, real_stubs
from drivers.grbl import EmulatedGrblController
from drivers.probe_motion import ProbeMotionVerifier
from drivers.real_camera import CameraMotionVerifier
from runtime.screen_gestures import gesture_metadata
from tests.helpers import make_device_configs
from tests.test_screen_gesture_metadata import runtime_fixture
from tests.test_stage_a_devices import make_manager


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    manager.hardware_mode = "real"
    monkeypatch.setattr(real_stubs, "require_confirmed_hardware", lambda: None)
    driver_class = daemon_arm.DaemonArmDriver
    monkeypatch.setattr(daemon_arm, "DaemonArmDriver",
                        lambda *args, **kwargs: driver_class(*args, **{"data_root": tmp_path, **kwargs}))
    monkeypatch.setattr(daemon_arm.DaemonTCPTransport, "open",
                        lambda *_: pytest.fail("Unexpected daemon connection"))
    cfg = make_device_configs(
        1, driver_mode="daemon", daemon_addr="127.0.0.1:1", data_root=tmp_path,
        camera_frame_provider=lambda: pytest.fail("Unexpected camera capture"),
    )[0]
    return manager, cfg


def test_daemon_factory_preserves_click_verifier_and_adds_separate_probe(factory, tmp_path):
    manager, cfg = factory
    arm, camera, _, _, mode = manager._create_drivers(cfg)

    # Exact type matters: ProbeMotionVerifier subclasses CameraMotionVerifier.
    assert type(arm.motion_verifier) is CameraMotionVerifier
    assert type(arm.gesture_motion_verifier) is ProbeMotionVerifier
    assert arm.motion_verifier is not arm.gesture_motion_verifier
    assert arm.motion_verifier._camera is camera
    assert arm.gesture_motion_verifier._camera is camera
    assert arm.gesture_motion_verifier.device_id == "arm-01"
    assert arm.gesture_motion_verifier.data_root == arm.data_root == tmp_path
    assert callable(arm.live_calibration_gate)
    assert mode == "real"


def test_registration_wires_both_verifiers_to_the_same_device_broker(factory):
    manager, cfg = factory
    manager.register_device(cfg)
    arm = manager.driver("arm-01")
    broker = manager.camera_broker("arm-01")

    assert type(arm.motion_verifier) is CameraMotionVerifier
    assert type(arm.gesture_motion_verifier) is ProbeMotionVerifier
    assert arm.motion_verifier._camera is broker
    assert arm.gesture_motion_verifier._camera is broker
    assert broker._driver is manager.camera("arm-01")
    assert manager.camera_broker("arm-01") is broker


def test_direct_serial_factory_keeps_camera_verifier_without_probe(factory):
    manager, cfg = factory
    arm, camera, _, _, mode = manager._create_drivers({
        **cfg, "driver_mode": "real", "transport_factory": EmulatedGrblController,
    })

    assert type(arm.motion_verifier) is CameraMotionVerifier
    assert arm.motion_verifier._camera is camera
    assert getattr(arm, "gesture_motion_verifier", None) is None
    assert mode == "real"


@pytest.mark.parametrize("reasons", [[], ["probe registration missing"]])
def test_metadata_uses_gesture_readiness_without_consulting_click_verifier(tmp_path, reasons):
    runtime, _, _, _ = runtime_fixture(tmp_path)
    driver = runtime.devices.driver("arm-01")
    click = CameraMotionVerifier(None)
    click.readiness = Mock(side_effect=AssertionError("Click verifier must not gate gestures"))
    probe = ProbeMotionVerifier(None, device_id="arm-01", data_root=tmp_path)
    probe.readiness = Mock(return_value=reasons)
    driver.motion_verifier = click
    driver.gesture_motion_verifier = probe

    result = asyncio.run(gesture_metadata(runtime, "arm-01"))

    assert result["usable"] is (not reasons)
    assert result["blocking_reasons"] == reasons
    assert all(action["usable"] is (not reasons) for action in result["actions"].values())
    probe.readiness.assert_called_once_with()
    click.readiness.assert_not_called()
    assert driver.motion_verifier is click
    assert driver.gesture_motion_verifier is probe


def test_metadata_checks_selected_gesture_verifier_kind(tmp_path):
    runtime, _, _, _ = runtime_fixture(tmp_path)
    driver = runtime.devices.driver("arm-01")
    driver.motion_verifier = SimpleNamespace(measurement_kind="registered_probe_trace")
    driver.gesture_motion_verifier = CameraMotionVerifier(None)

    result = asyncio.run(gesture_metadata(runtime, "arm-01"))

    assert not result["usable"]
    assert any("画面变化" in reason for reason in result["blocking_reasons"])
    assert not any(action["usable"] for action in result["actions"].values())


@pytest.mark.parametrize("gesture_slot", ["absent", "none"])
def test_metadata_retains_fallback_for_drivers_without_gesture_verifier(tmp_path, gesture_slot):
    runtime, _, _, _ = runtime_fixture(tmp_path)
    driver = runtime.devices.driver("arm-01")
    fallback = SimpleNamespace(
        measurement_kind="registered_probe_trace", readiness=Mock(return_value=["fallback not ready"]))
    driver.motion_verifier = fallback
    if gesture_slot == "none":
        driver.gesture_motion_verifier = None

    result = asyncio.run(gesture_metadata(runtime, "arm-01"))

    assert result["blocking_reasons"] == ["fallback not ready"]
    assert not any(action["usable"] for action in result["actions"].values())
    fallback.readiness.assert_called_once_with()
