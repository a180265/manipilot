"""OCR edge jitter is judged against the original feasible tap area."""
import asyncio
import pytest

from adapters.vision.ocr import OcrWord
from drivers.arm import DriverError
from tests.test_layout_observation import offline_only  # noqa: F401
from tests.test_layout_target_driver import motion, target_rig
from tests.test_layout_text_location import TextEngine
from tests.test_pre_enable_s3_driver import locate


@pytest.mark.parametrize("dx,dy", [(1.5, 1.5), (4, 3)])
def test_ocr_jitter_keeps_all_original_taps_inside_current_text(target_rig, dx, dy):
    rig = target_rig
    target = locate(rig)
    rig.camera.ocr_engine = TextEngine(words=[OcrWord("Ann", 12+dx, 20+dy, 40, 16, .99)])
    result = asyncio.run(rig.arm.click(target))
    assert result["ok"]
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


@pytest.mark.parametrize("dx,dy,width,height", [(0, 15, 40, 16), (35, 0, 40, 16), (0, 0, 8, 16)])
def test_original_tap_area_outside_current_text_is_rejected(target_rig, dx, dy, width, height):
    rig = target_rig
    target = locate(rig)
    rig.camera.ocr_engine = TextEngine(words=[OcrWord("Ann", 12+dx, 20+dy, width, height, .99)])
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))
