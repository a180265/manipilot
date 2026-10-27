"""New calibration authorizes reuse of exact selected layout assets, never old geometry."""
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb
from runtime.layout_observation import LayoutObservationError
from tests.test_layout_observation import LayoutRig, offline_only  # noqa: F401


@pytest.fixture
def rig(tmp_path, monkeypatch):
    item = LayoutRig(tmp_path, monkeypatch)
    yield item
    item.close()


def changed_bundle(rig):
    bundle = deepcopy(rig.bundle)
    bundle.update(imeDeviceId="offline-ime", cameraDeviceUniqueId="usb-new-port", cameraBindingId="bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
                  sourceGeometryId="d" * 64, coordinateFrameHash="e" * 64)
    bundle["screen"]["edgeModel"]["corners"] = [[700, 50], [1019, 50], [1019, 689], [700, 689]]
    bundle["camera"]["cameraToScreen"]["homography"] = [1, 0, -700, 0, 1, -50, 0, 0, 1]
    device = rig.runtime.devices.get("arm-01")
    device.update(camera_device_unique_id=bundle["cameraDeviceUniqueId"],
                  camera_binding_id=bundle["cameraBindingId"], source_geometry_id=bundle["sourceGeometryId"])
    cb.save_bundle(bundle, rig.data)
    return bundle


def test_new_calibration_reuses_exact_layout_with_new_source_and_region(rig):
    from runtime.layout_recalibration import record_completed_calibration
    bundle = changed_bundle(rig)
    immutable_region = deepcopy(rig.parts["region_store"].get_version("arm-01", 1))
    immutable_adaptation = deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 1))
    with pytest.raises(LayoutObservationError):
        rig.runtime.layout_observation.resolve("arm-01").verify_saved_binding(bundle)
    receipt = record_completed_calibration(bundle, rig.data, source="manual_finish")
    assert receipt["adaptation_ref"]["sha256"] == immutable_adaptation["sha256"]
    selected = rig.runtime.layout_observation.resolve("arm-01")
    _, width, height = selected.verify_saved_binding(bundle)
    assert (width, height) == (320, 640)
    assert selected.region["installation_binding"]["camera_device_unique_id"] == "usb-new-port"
    assert selected.region["pixel_rect"]["left"] > 600
    assert selected.region["pixel_rect"]["right"] > 1019
    assert rig.parts["region_store"].get_version("arm-01", 1) == immutable_region
    assert rig.parts["ops_svc"].resolve_adaptation("arm-01", 1) == immutable_adaptation
    assert selected.template == rig.template


@pytest.mark.parametrize("fault", ["draft", "changed_calibration", "new_adaptation", "wrong_device", "corrupt"])
def test_receipt_does_not_authorize_other_calibration_or_assets(rig, fault):
    from runtime.layout_recalibration import record_completed_calibration, receipt_path
    bundle = changed_bundle(rig)
    record_completed_calibration(bundle, rig.data, source="auto_promote")
    if fault == "draft":
        bundle["status"] = "draft"
        cb.save_bundle(bundle, rig.data)
    elif fault == "changed_calibration":
        bundle["cameraBindingId"] = "cccccccc-3333-4333-8333-cccccccccccc"
        cb.save_bundle(bundle, rig.data)
    elif fault == "new_adaptation":
        adaptation = deepcopy(rig.adaptation)
        adaptation["version"] = 2
        rig.select(adaptation)
    elif fault == "wrong_device":
        bundle["deviceId"] = "arm-02"
    else:
        receipt_path("arm-01", rig.data).write_text('{"broken":true}')
    with pytest.raises((LayoutObservationError, ValueError)):
        rig.runtime.layout_observation.resolve("arm-01").verify_saved_binding(bundle)


def test_region_context_uses_the_same_new_calibration_projection(rig):
    from runtime.layout_recalibration import record_completed_calibration
    from runtime.phone_region_context import load_region_context, context_matches_bundle
    bundle = changed_bundle(rig)
    record_completed_calibration(bundle, rig.data, source="manual_finish")
    context = load_region_context(rig.parts["region_store"], "arm-01")
    context_matches_bundle(context, bundle)
    assert context["region"]["pixel_rect"]["left"] > 600
    assert context["region"]["rect"]["x"] > 0.5
    assert context["pointer"]["selected_sha256"] == rig.region["sha256"]
    # Public refs must continue to name the immutable archive, not a derived copy.
    assert rig.parts["region_svc"].get("arm-01")["selected"]["sha256"] == rig.region["sha256"]


def test_actual_observation_uses_new_projection_and_keeps_page_assets(rig):
    import asyncio
    import numpy as np
    from runtime.layout_recalibration import record_completed_calibration
    bundle = changed_bundle(rig)
    record_completed_calibration(bundle, rig.data, source="manual_finish")
    image = np.full((720, 1280, 3), 20, np.uint8)
    image[50:690, 700:1020] = rig.screens["chat_ime_empty"]
    rig.source.image = image
    rig.source.last_device_unique_id = bundle["cameraDeviceUniqueId"]
    rig.source.last_camera_binding_id = bundle["cameraBindingId"]
    rig.source.last_source_geometry_id = bundle["sourceGeometryId"]
    observed = asyncio.run(rig.camera.observe_regions())
    assert observed["ui"]["ui_state"] == "conversation"
    assert observed["input_empty"] is True
    assert observed["components"][0]["x"] == 924
    assert observed["adaptation_ref"]["version"] == 1
    rig.source.last_device_unique_id = "uvc-01"
    with pytest.raises(ValueError):
        asyncio.run(rig.camera.observe_regions())


def test_resolved_layout_rejects_calibration_replaced_during_operation(rig):
    from runtime.layout_recalibration import record_completed_calibration
    bundle = changed_bundle(rig)
    record_completed_calibration(bundle, rig.data, source="manual_finish")
    selected = rig.runtime.layout_observation.resolve("arm-01")
    changed = deepcopy(bundle)
    changed["camera"]["cameraToScreen"]["homography"][2] -= 1
    with pytest.raises(LayoutObservationError, match="新校准已变化"):
        selected.verify_saved_binding(changed)


def test_reuse_failure_is_visible_without_failing_saved_calibration(rig):
    from drivers.calibration_wizard import CalibrationWizard
    bundle = changed_bundle(rig)
    bundle["imeDeviceId"] = "a-different-phone"
    wizard = object.__new__(CalibrationWizard)
    wizard.device, wizard.data_root, wizard.state = "arm-01", rig.data, {}
    result = wizard._record_layout_recalibration(bundle, source="manual_finish")
    assert result["status"] == "warning"
    assert "新校准已保存" in result["message"]
    assert "新校准手机与已选布局不符" in result["message"]
    assert wizard.state["layoutReuse"] == result


def test_completed_reuse_status_is_visible_to_operator(rig):
    from drivers.calibration_wizard import CalibrationWizard
    bundle = changed_bundle(rig)
    wizard = object.__new__(CalibrationWizard)
    wizard.device, wizard.data_root, wizard.state = "arm-01", rig.data, {}
    result = wizard._record_layout_recalibration(bundle, source="manual_finish")
    assert result["status"] == "ready"
    assert result["receiptSha256"]
    assert wizard.state["layoutReuse"] == result


def test_custom_layout_database_keeps_calibration_root_separate(tmp_path, monkeypatch):
    from drivers.calibration_wizard import CalibrationWizard
    from runtime.layout_recalibration import receipt_path
    from runtime.phone_region_context import load_region_context, context_matches_bundle
    layout_db = tmp_path / "separate-layout-store" / "custom.sqlite"
    item = LayoutRig(tmp_path, monkeypatch, layout_db_path=layout_db)
    try:
        bundle = changed_bundle(item)
        wizard = object.__new__(CalibrationWizard)
        wizard.device, wizard.data_root, wizard.state = "arm-01", item.data, {}
        wizard.layout_db_path = layout_db
        assert wizard._record_layout_recalibration(bundle, source="manual_finish")["status"] == "ready"
        assert receipt_path("arm-01", item.data).exists()
        assert not (item.data / "layout.db").exists()
        item.runtime.layout_observation.resolve("arm-01").verify_saved_binding(bundle)
        context = load_region_context(item.parts["region_store"], "arm-01")
        context_matches_bundle(context, bundle)
        assert context["region"]["pixel_rect"]["left"] > 600
    finally:
        item.close()
