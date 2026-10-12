"""定位凭据只能在原相机、原上下文与原时限内消费。"""
import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from adapters.vision.ocr import OcrEngineError, OcrWord
from drivers.arm import DriverError
from drivers.real_camera import RealCameraDriver
from drivers import calibration_bundle as cb
from tests.test_camera_text_location import ocr_result
from tests.test_layout_observation import offline_only  # noqa: F401
from tests.test_layout_target_driver import motion, target_rig
from tests.test_pre_enable_s3_driver import locate


class Source:
    def __init__(self):
        self.last_frame_seq = 0
        self.last_source_generation_id = "generation-1"
        self.last_device_unique_id = "camera-1"

    def __call__(self):
        self.last_frame_seq += 1
        self.last_captured_at = self.last_received_at = datetime.now(timezone.utc).isoformat()
        return np.zeros((480, 640, 3), dtype=np.uint8)


@pytest.mark.parametrize("fault", ["none", "generation", "physical_camera", "binding_during_ocr"])
def test_legacy_locator_also_rejects_changed_source(fault):
    source = Source()
    context = {"revision": 1}
    camera = RealCameraDriver("cam-test", frame_provider=source, ocr_engine=SimpleNamespace(
        recognize=lambda *_a, **_k: ocr_result(OcrWord("Ann", 100, 200, 40, 20, .99))))
    camera.attach_target_context_provider(lambda: dict(context))
    located = asyncio.run(camera.locate_text("Ann", exact=True))
    if fault == "generation":
        source.last_source_generation_id = "generation-2"
    elif fault == "physical_camera":
        source.last_device_unique_id = "camera-2"
    elif fault == "binding_during_ocr":
        def recognize(*_a, **_k):
            context["revision"] += 1
            return ocr_result(OcrWord("Ann", 100, 200, 40, 20, .99))
        camera.ocr_engine = SimpleNamespace(recognize=recognize)
    if fault == "none":
        check = asyncio.run(camera.verify_text_target(located))
        check()
    else:
        with pytest.raises((ValueError, OcrEngineError)):
            asyncio.run(camera.verify_text_target(located))


def test_locate_does_not_return_coordinates_after_ocr_context_change():
    source = Source()
    context = {"revision": 1}

    def recognize(*_a, **_k):
        context["revision"] += 1
        return ocr_result(OcrWord("Ann", 100, 200, 40, 20, .99))

    camera = RealCameraDriver("cam-test", frame_provider=source,
                              ocr_engine=SimpleNamespace(recognize=recognize))
    camera.attach_target_context_provider(lambda: dict(context))
    with pytest.raises(OcrEngineError):
        asyncio.run(camera.locate_text("Ann", exact=True))
    assert camera._text_locations == {}


@pytest.mark.parametrize("field", [
    "locationId", "cameraX", "bbox", "frameSeq", "frameSha256", "text",
    "sourceCapturedAt", "sourceReceivedAt", "lost_proof",
])
def test_changed_or_lost_location_proof_never_moves(target_rig, field):
    target = locate(target_rig)
    ref = target["target_ref"]
    if field == "lost_proof":
        target_rig.camera._text_locations.clear()
    elif field == "bbox":
        ref[field][0] += 1
    elif isinstance(ref[field], (int, float)):
        ref[field] += 1
    else:
        ref[field] = str(ref[field]) + "-changed"
    with pytest.raises(DriverError):
        asyncio.run(target_rig.arm.click(target))
    assert motion(target_rig) == []


@pytest.mark.parametrize("phase", ["capture", "ocr"])
def test_locate_pins_calibration_before_capture(target_rig, phase):
    rig = target_rig
    # Exercise the legacy branch too: selecting no shared layout must not reload
    # a new calibration halfway through processing the original frame.
    rig.camera._layout_provider = None
    original_capture = rig.camera._capture_for_operation

    def change():
        changed = copy.deepcopy(rig.bundle)
        changed["camera"]["cameraToScreen"]["maxErrorPx"] = 0.6
        cb.save_bundle(changed, rig.data)

    async def capture():
        frame = await original_capture()
        if phase == "capture":
            change()
        return frame

    def recognize(*_a, **_k):
        if phase == "ocr":
            change()
        return replace(ocr_result(OcrWord("Ann", 132, 250, 40, 20, .99)),
                       width=1280, height=720)

    rig.camera._capture_for_operation = capture
    rig.camera.ocr_engine = SimpleNamespace(recognize=recognize)
    with pytest.raises(OcrEngineError, match="校准已变化"):
        asyncio.run(rig.camera.locate_text("Ann", exact=True))
    assert rig.camera._text_locations == {}
