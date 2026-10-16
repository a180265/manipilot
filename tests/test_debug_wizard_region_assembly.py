"""Actual debug wizard factory, region reader and geometry; fake IO only."""
import copy

import cv2
import numpy as np
import pytest

from drivers.calibration_wizard import PoseUnlocatable, WizardError
from drivers import screen_edge
from runtime.layout_region import canonical_digest
from tests.test_calibration_wizard import FakeDaemon, FakeIme, CAM_W, CAM_H
from tests.test_layout_observation import offline_only  # noqa: F401


@pytest.mark.parametrize("case", [
    "first", "two_inside", "tight", "gradient", "gradient_outside",
    "changed_region", "changed_source",
])
def test_debug_factory_reads_selected_region_and_bounds_detection(tmp_path, monkeypatch, case):
    monkeypatch.setenv("MOTOR_DEBUG_DATA_DIR", str(tmp_path))
    from scripts import motor_debug_server as debug

    monkeypatch.setattr(debug, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "INSTALLATION_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(debug, "WIZARDS", {})
    daemon = FakeDaemon()
    monkeypatch.setattr(debug, "daemon_for", lambda _: daemon)
    monkeypatch.setattr(debug, "IME_CLIENT", FakeIme())
    monkeypatch.setattr(debug, "_auto_calibration_workspace", lambda: {})
    facts = {
        "cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-01",
        "cameraBindingId": "installation", "sourceGeometryId": "geometry",
        "sourceGenerationId": "generation", "bindingRevision": 1,
        "daemonAddr": "127.0.0.1:8795", "daemonSession": "sess-A",
        "frameWidth": CAM_W, "frameHeight": CAM_H,
    }
    monkeypatch.setattr(debug, "calibration_binding_facts",
                        lambda _: copy.deepcopy(facts))
    region = {
        "device_id": "arm-01", "version": 1,
        "source_size": {"width": CAM_W, "height": CAM_H},
        "source_geometry_id": "geometry",
        "installation_binding": {
            "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
            "camera_binding_id": "installation",
        },
        "rect": {"x": 50 / CAM_W, "y": 30 / CAM_H,
                 "width": 550 / CAM_W, "height": 480 / CAM_H},
    }
    if case == "tight":
        region["rect"]["width"] = 300 / CAM_W
    region["sha256"] = canonical_digest(region)
    selected = {
        "status": "verified", "revision": 1, "pending_draft": None,
        "selected": {"id": "arm-01", "version": 1, "sha256": region["sha256"]},
    }
    reads = []

    def proxy(method, path):
        assert method == "GET"
        reads.append(path)
        if path.endswith("/versions/1"):
            return {"record": copy.deepcopy(region)}
        assert path == "/v1/devices/arm-01/phone-search-region"
        return copy.deepcopy(selected)

    monkeypatch.setattr(debug, "_auto_proxy", proxy)
    frame = np.full((CAM_H, CAM_W, 3), 20, np.uint8)
    expected = [[100, 80], [500, 80], [500, 460], [100, 460]]
    if case != "gradient_outside":
        if case == "two_inside":
            cv2.rectangle(frame, (90, 80), (260, 450), (220, 220, 220), -1)
            cv2.rectangle(frame, (330, 80), (510, 450), (220, 220, 220), -1)
        else:
            cv2.rectangle(frame, (100, 80), (500, 460), (220, 220, 220), -1)
    cv2.rectangle(frame, (700, 80), (920, 460), (220, 220, 220), -1)
    captures = []

    def capture(_):
        captures.append(True)
        if case == "changed_region":
            selected["revision"] += 1
        if case == "changed_source":
            facts["sourceGenerationId"] = "replacement"
        return cv2.imencode(".png", frame)[1].tobytes(), len(captures)

    monkeypatch.setattr(debug, "fetch_camera_frame", capture)
    gradient_calls = []
    if case.startswith("gradient"):
        def free_failure(*args, **kwargs):
            raise screen_edge.ScreenEdgeError("injected threshold detection failure")

        gradient = screen_edge.locate_quad_by_gradient_lines

        def observe_gradient(image, corners, **kwargs):
            gradient_calls.append((image.shape, np.asarray(corners)))
            assert image.shape[:2] == (480, 550)
            return gradient(image, corners, **kwargs)

        monkeypatch.setattr(screen_edge, "locate_screen_quad", free_failure)
        monkeypatch.setattr(screen_edge, "locate_quad_by_gradient_lines", observe_gradient)
    wizard = debug.get_wizard("arm-01")
    assert debug.get_wizard("arm-01") is wizard
    assert not (tmp_path / "devices/arm-01/calibration_bundle.json").exists()
    anchor = expected if case.startswith("gradient") else None
    if case in {"first", "gradient"}:
        result = wizard._edge_quad(expected_corners=anchor)
        assert (result["frameW"], result["frameH"]) == (CAM_W, CAM_H)
        assert np.max(np.abs(np.asarray(result["corners"]) - expected)) < 2
    else:
        with pytest.raises(WizardError) as raised:
            wizard._edge_quad(expected_corners=anchor)
        assert not isinstance(raised.value, PoseUnlocatable)
        if case.startswith("changed_"):
            assert "context" in str(raised.value)
    if case.startswith("gradient"):
        assert len(gradient_calls) == 1
        assert np.array_equal(gradient_calls[0][1], np.asarray(expected) - [50, 30])
    # 成功=单帧；失败=3 轮曝光摇摆重试各取一帧（业主 2026-10-16 语义）
    assert reads and (captures == [True] if case in {"first", "gradient"}
                      else len(captures) == 3)
    assert daemon.cmds == []
