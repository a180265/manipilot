import pytest
from types import SimpleNamespace

from drivers import calibration_bundle, screen_gestures
from runtime.devices import DeviceManager
from runtime.device_eligibility import compute_device_eligibility
from tests.test_deferred_input_acceptance import motion_bundle


def test_motion_ready_can_calculate_binding_without_input_acceptance():
    bundle = motion_bundle()
    binding = screen_gestures.binding_snapshot(bundle)
    assert binding["device_id"] == bundle["deviceId"]
    assert bundle["acceptance"]["debug"] is False
    assert bundle["acceptance"]["deferred"]["debug"] is True


@pytest.mark.parametrize("change", [
    lambda b: b["validation"].update(passed=False),
    lambda b: b["validation"]["points"].pop(),
    # deleteValidated 已退役（2026-10-03 规格 §4.6）：不再是机械证据要求
    lambda b: b["safePark"].update(confirmed=False),
])
def test_motion_ready_still_requires_complete_mechanical_evidence(change):
    bundle = motion_bundle()
    change(bundle)
    with pytest.raises(ValueError):
        screen_gestures.binding_snapshot(bundle)


@pytest.mark.parametrize("changed", [
    {}, {"daemonSession": "other-session"}, {"cameraId": "other-camera"},
    {"deviceId": "other-arm"}, {"validation": {"passed": False}},
])
def test_motion_binding_routes_sampling_but_never_grants_input(monkeypatch, tmp_path, changed):
    bundle = motion_bundle()
    bundle["imeDeviceId"] = "calibrated-phone"
    device = {
        "device_id": bundle["deviceId"], "binding_mode": "real",
        "camera_id": bundle["cameraId"], "daemon_session": bundle["daemonSession"],
        "camera_device_unique_id": bundle["cameraDeviceUniqueId"],
        "camera_binding_revision": bundle["bindingRevision"],
        "daemon_addr": bundle["daemonAddr"],
        "expected_frame_width": bundle["frame"]["width"],
        "expected_frame_height": bundle["frame"]["height"],
        "_settings_data_root": tmp_path,
        "ime_device_id": "old-placeholder", "status": "ready",
        "calibration_status": "validated", "device_mode": "ready_manual",
    }
    manager = SimpleNamespace(get=lambda _: device,
                              driver=lambda _: SimpleNamespace(data_root=None))
    bundle.update(changed)
    monkeypatch.setattr(calibration_bundle, "load_bundle", lambda *args: bundle)
    result = DeviceManager.input_binding(manager, device["device_id"])
    if changed:
        assert result["ime_device_id"] is None
        assert result["ime_binding_source"] == "calibration_invalid"
    else:
        assert result["ime_device_id"] == "calibrated-phone"
        assert result["ime_binding_source"] == "calibration_bundle"
        eligibility = compute_device_eligibility(
            {**device, **result}, require_input=True,
            ime_status={"online": True, "keyboardActive": True})
        # A-开关（2026-10-03 规格 §4.6）：真档案身份不授予输入——资格由
        # 操作者开关决定；开关缺省关=统一阻断码。
        assert eligibility["blocking_checks"] == ["input_switch_disabled"]
        assert not eligibility["can_manual_send"]
        assert not eligibility["can_managed"]
