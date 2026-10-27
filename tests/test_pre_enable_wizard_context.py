"""校准屏幕采样不续用换框/换来源后的旧运行上下文。"""
import copy

import cv2
import numpy as np
import pytest

from drivers.calibration_wizard import PoseUnlocatable, WizardError
from tests.test_calibration_wizard import CAM_H, CAM_W, Env
from tests.test_layout_observation import offline_only  # noqa: F401


@pytest.mark.parametrize("fault", [
    "none", "unregistered", "draft_only", "needs_review",
    "content", "device", "version", "pointer_changed", "source_binding_changed",
    "source_and_other_reason",
])
def test_debug_wizard_region_reader_rejects_unselected_or_changed(tmp_path, monkeypatch, fault):
    from runtime.layout_region import canonical_digest
    from runtime.phone_region_context import RegionContextError

    monkeypatch.setenv("MOTOR_DEBUG_DATA_DIR", str(tmp_path))
    from scripts import motor_debug_server as debug

    record = {
        "device_id": "arm-01", "version": 1,
        "rect": {"x": .1, "y": .1, "width": .8, "height": .8},
    }
    if fault == "device":
        record["device_id"] = "arm-02"
    if fault == "version":
        record["version"] = 2
    record["sha256"] = canonical_digest(record)
    view = {
        "status": "verified", "revision": 1, "pending_draft": None,
        "selected": {"id": "arm-01", "version": 1, "sha256": record["sha256"]},
    }
    if fault in {"unregistered", "draft_only"}:
        view.update(selected=None, status="unregistered" if fault == "unregistered"
                    else "needs_review")
    if fault == "draft_only":
        view["pending_draft"] = {"id": "arm-01", "version": 1}
    if fault == "needs_review":
        view["status"] = "needs_review"
    if fault in {"source_binding_changed", "source_and_other_reason"}:
        view.update(status="needs_review", reasons=["source_binding_changed"])
        if fault == "source_and_other_reason":
            view["reasons"].append("phone_not_located")
    if fault == "content":
        record["rect"]["x"] = .2
    reads = []

    def proxy(method, path):
        assert method == "GET"
        reads.append(path)
        if path.endswith("/versions/1"):
            if fault == "pointer_changed":
                view["revision"] += 1
            return {"record": copy.deepcopy(record)}
        assert path == "/v1/devices/arm-01/phone-search-region"
        return copy.deepcopy(view)

    monkeypatch.setattr(debug, "_auto_proxy", proxy)
    if fault in {"none", "source_binding_changed"}:
        result = debug.wizard_search_region("arm-01")
        assert result["sha256"] == record["sha256"]
        assert result["_selection_revision"] == 1
    elif fault == "unregistered":
        assert debug.wizard_search_region("arm-01") is None
    else:
        with pytest.raises(RegionContextError):
            debug.wizard_search_region("arm-01")


@pytest.mark.parametrize("fault", ["none", "region", "binding", "generation", "dimensions"])
@pytest.mark.parametrize("when", ["capture", "next_sample", "reopen"])
def test_wizard_sampling_context_is_pinned(tmp_path, fault, when):
    facts = {
        "cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-01",
        "cameraBindingId": "installation", "sourceGeometryId": "geometry",
        "sourceGenerationId": "generation", "bindingRevision": 1,
        "daemonAddr": "127.0.0.1:8795", "frameWidth": CAM_W, "frameHeight": CAM_H,
    }
    region = {
        "rect": {"x": .05, "y": .05, "width": .55, "height": .90},
        "source_size": {"width": CAM_W, "height": CAM_H},
        "installation_binding": {
            "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
            "camera_binding_id": "installation",
        },
        "source_geometry_id": "geometry",
    }
    image = np.full((CAM_H, CAM_W, 3), 20, np.uint8)
    cv2.rectangle(image, (100, 80), (500, 460), (220, 220, 220), -1)
    cv2.rectangle(image, (700, 80), (920, 460), (220, 220, 220), -1)
    calls = []
    changed = []

    def change():
        if changed or fault == "none":
            return
        changed.append(fault)
        if fault == "region":
            region["rect"]["x"] += .01
        elif fault == "binding":
            facts["cameraBindingId"] = "new-installation"
        elif fault == "generation":
            facts["sourceGenerationId"] = "new-generation"
        elif fault == "dimensions":
            facts["frameWidth"] += 10

    def fetch():
        calls.append(True)
        if when == "capture":
            change()
        return cv2.imencode(".png", image)[1].tobytes(), len(calls)

    def make_env():
        return Env(tmp_path, wizard_extra={
            "camera_fetch": fetch,
            "binding_facts": lambda: copy.deepcopy(facts),
            "search_region_provider": lambda: copy.deepcopy(region),
        })

    env = make_env()
    try:
        if when != "capture":
            first = env.wizard._edge_quad()
            assert first["frameW"] == CAM_W
            if when == "reopen":
                # Reopening is not an operator cancel (cancel intentionally clears state).
                env = make_env()
            change()
        if fault == "none":
            result = env.wizard._edge_quad()
            assert np.max(np.abs(np.asarray(result["corners"]) - np.asarray(
                [[100, 80], [500, 80], [500, 460], [100, 460]]))) < 2
        else:
            with pytest.raises(WizardError) as raised:
                env.wizard._edge_quad()
            assert not isinstance(raised.value, PoseUnlocatable)
            assert "context" in str(raised.value), str(raised.value)
    finally:
        env.close()
