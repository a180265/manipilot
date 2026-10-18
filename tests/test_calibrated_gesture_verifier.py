"""Calibrated camera gestures use the same observation contract as clicks."""
import pytest

from drivers.probe_motion import ProbeMotionVerifier
from drivers.real_camera import CameraMotionVerifier
from tests.test_runtime_gesture_verifier_isolation import factory  # noqa: F401


@pytest.mark.parametrize("registered", [False, True])
def test_calibrated_factory_selects_camera_observation_without_probe_lookup(factory, monkeypatch, registered):
    manager, cfg = factory
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")

    def no_probe_lookup(*args, **kwargs):
        pytest.fail("校准摄像头模式无需读取探头照片")

    monkeypatch.setattr("drivers.probe_motion.profile_stale_for", no_probe_lookup)
    if registered:
        manager.register_device(cfg)
        arm = manager.driver("arm-01")
        camera = manager.camera_broker("arm-01")
    else:
        arm, camera, _, _, mode = manager._create_drivers(cfg)
        assert mode == "real"
    assert type(arm.motion_verifier) is CameraMotionVerifier
    assert arm.gesture_motion_verifier is arm.motion_verifier
    assert arm.gesture_motion_verifier.observation_only is True
    assert arm.gesture_motion_verifier.measurement_kind == "frame_change"
    assert arm.gesture_motion_verifier._camera is camera


def test_default_factory_keeps_registered_probe_verifier(factory, monkeypatch):
    manager, cfg = factory
    monkeypatch.delenv("ARMFLEET_RUNTIME_VISION_MODE", raising=False)
    arm, _, _, _, _ = manager._create_drivers(cfg)
    assert type(arm.gesture_motion_verifier) is ProbeMotionVerifier
    assert arm.gesture_motion_verifier is not arm.motion_verifier
