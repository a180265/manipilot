"""Exact/ROI OCR uses parsed fake results and in-memory images, never hardware."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from adapters.vision.ocr import OcrEngineError, OcrResult, OcrWord, find_text_bbox
from drivers.real_camera import RealCameraDriver
from tasks.plugin_api import StepContext
from tests.helpers import PROBE_MANIFEST, make_device_configs, make_runtime


def ocr_result(*words):
    return OcrResult(
        text="\n".join(word.text for word in words), words=list(words),
        confidence=.99, truncated=False, engine="fake", engine_version="1",
        width=640, height=480,
    )


def test_exact_matching_does_not_accept_substring_but_keeps_default_compatible():
    result = ocr_result(OcrWord("Annabelle", 100, 200, 90, 20, .99))
    assert find_text_bbox(result, "Ann") is not None
    assert find_text_bbox(result, "Ann", exact=True) is None


def test_exact_matching_joins_words_and_deduplicates_same_location():
    result = ocr_result(
        OcrWord("Mary", 100, 200, 40, 20, .99),
        OcrWord("Ann", 144, 200, 30, 20, .98),
    )
    located = find_text_bbox(result, " mary ann ", exact=True)
    assert located["bbox"] == [100, 200, 174, 220]
    assert find_text_bbox(ocr_result(result.words[0]), "Mary", exact=True)["cameraX"] == 120


def test_exact_matching_rejects_distinct_duplicate_contact_locations():
    result = ocr_result(
        OcrWord("Ann", 100, 200, 40, 20, .99),
        OcrWord("Ann", 100, 300, 40, 20, .90),
    )
    assert find_text_bbox(result, "Ann")["cameraY"] == 210
    with pytest.raises(OcrEngineError) as exc:
        find_text_bbox(result, "Ann", exact=True)
    assert exc.value.reason == "ocr_target_ambiguous"


ROI = {"x1": 80, "y1": 180, "x2": 250, "y2": 240, "frameW": 640, "frameH": 480}


def camera_with_ocr(result, *, frame_seq=None):
    engine = SimpleNamespace(recognize=lambda image, **kwargs: result)
    def frame_provider():
        return np.zeros((480, 640, 3), dtype=np.uint8)
    frame_provider.last_frame_seq = frame_seq
    return RealCameraDriver(
        "arm-01", frame_provider=frame_provider, ocr_engine=engine,
    )


def test_exact_miss_preserves_source_frame_metadata_without_coordinates():
    async def scenario():
        camera = camera_with_ocr(ocr_result(), frame_seq=77)
        frame = await camera.capture()
        missed = await camera.locate_text("Ann", exact=True, roi=ROI)
        assert missed["found"] is False
        assert missed["frameSeq"] == 77
        assert missed["frameW"] == 640 and missed["frameH"] == 480
        assert missed["frameSha256"] == frame["sha256"]
        assert not {"bbox", "cameraX", "cameraY"} & missed.keys()

    asyncio.run(scenario())


def test_legacy_miss_still_raises_not_found_error():
    with pytest.raises(OcrEngineError, match="未找到文字") as exc:
        asyncio.run(camera_with_ocr(ocr_result()).locate_text("Ann"))
    assert exc.value.reason == "ocr_text_not_found"


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("has_match", [False, True])
def test_truncated_ocr_is_an_observation_error_even_with_a_matching_word(exact, has_match):
    result = ocr_result(*([OcrWord("Ann", 100, 200, 40, 20, .99)] if has_match else []))
    result.truncated = True
    with pytest.raises(OcrEngineError) as exc:
        asyncio.run(camera_with_ocr(result).locate_text("Ann", exact=exact, roi=ROI))
    assert exc.value.reason == "ocr_result_truncated"
    assert "未找到" not in str(exc.value)


def test_camera_filters_roi_before_resolving_duplicate_contacts():
    result = ocr_result(
        OcrWord("Ann", 100, 200, 40, 20, .99),
        OcrWord("Ann", 100, 300, 40, 20, .99),
    )
    camera = camera_with_ocr(result)
    located = asyncio.run(camera.locate_text("Ann", exact=True, roi=ROI))
    assert located["bbox"] == [100, 200, 140, 220]
    assert located["frameW"] == 640 and located["frameH"] == 480
    assert len(result.words) == 2


def test_roi_does_not_accept_partially_outside_words_or_low_confidence():
    result = ocr_result(
        OcrWord("Ann", 60, 200, 40, 20, .99),
        OcrWord("Ann", 100, 210, 40, 20, .70),
    )
    located = asyncio.run(camera_with_ocr(result).locate_text("Ann", exact=True, roi=ROI))
    assert located["found"] is False


@pytest.mark.parametrize("change", [
    {"x1": -1}, {"x2": 700}, {"x1": 250}, {"y2": float("nan")},
    {"frameW": 1280}, {"frameH": 720}, {"frameW": None},
])
def test_invalid_roi_or_frame_mismatch_never_produces_coordinates(change):
    with pytest.raises(OcrEngineError) as exc:
        asyncio.run(camera_with_ocr(ocr_result(
            OcrWord("Ann", 100, 200, 40, 20, .99),
        )).locate_text("Ann", exact=True, roi={**ROI, **change}))
    assert exc.value.reason == "ocr_roi_invalid"


def test_runtime_passes_exact_roi_and_confidence_to_camera(tmp_path):
    async def scenario():
        manifest = {**PROBE_MANIFEST, "capabilities": [*PROBE_MANIFEST["capabilities"], "camera.locate_text"]}
        runtime = make_runtime(tmp_path, manifest=manifest)
        await runtime.startup(device_configs=make_device_configs())
        try:
            camera = camera_with_ocr(ocr_result(
                OcrWord("Annabelle", 100, 200, 60, 20, .99),
                OcrWord("Ann", 100, 300, 40, 20, .99),
                OcrWord("Ann", 170, 200, 40, 20, .80),
            ))
            runtime.devices._cameras["arm-01"] = camera
            task = {"task_id": "ocr-options", "device_id": "arm-01",
                    "plugin_id": manifest["plugin_id"], "status": "executing",
                    "lease_id": runtime.leases.acquire("arm-01", "ocr-options")["lease_id"]}
            runtime.store.save_task(task)
            ctx = StepContext(runtime, task, manifest)
            params = {"text": "Ann", "region": "account", "exact": True,
                      "roi": ROI, "min_confidence": .9}
            missed = await ctx.call_capability("camera.locate_text", params)
            assert missed["found"] is False
            assert missed["frameW"] == 640 and missed["frameH"] == 480
            assert "frameSeq" in missed and missed["frameSha256"]
            located = await ctx.call_capability(
                "camera.locate_text", {**params, "min_confidence": .75})
            assert located["bbox"] == [170, 200, 210, 220]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_fake_camera_accepts_navigation_options_and_returns_inside_roi():
    from drivers.fake_camera import FakeCameraDriver

    camera = FakeCameraDriver("arm-01", width=640, height=480)
    located = asyncio.run(camera.locate_text("Ann", exact=True, roi=ROI))
    assert ROI["x1"] <= located["cameraX"] <= ROI["x2"]
    assert ROI["y1"] <= located["cameraY"] <= ROI["y2"]
    assert located["frameW"] == 640 and located["frameH"] == 480
