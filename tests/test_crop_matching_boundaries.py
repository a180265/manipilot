"""Independent crop counterexamples retained as permanent regressions."""
import cv2
import numpy as np
import pytest

from runtime.layout_matching import (
    ImageDecodeError, _anchor_result, _pixel_rect, decode_image, match_anchor,
)
from runtime.layout_region import RegionError, _decode_jpeg, _detect_in_rect
from tests.test_crop_matching import _png, _scene


FULL = {"x": 0, "y": 0, "width": 1, "height": 1}


def _anchor(reference, search):
    return {"id": "probe", "reference_rect": reference, "search_region": search}


def test_mixed_bright_and_dark_screens_are_ambiguous():
    frame = np.full((600, 900, 3), 100, np.uint8)
    cv2.rectangle(frame, (100, 100), (340, 480), (230,) * 3, -1)
    cv2.rectangle(frame, (600, 180), (650, 260), (0,) * 3, -1)
    outcome = _detect_in_rect(
        frame, {"left": 30, "top": 30, "right": 870, "bottom": 550})
    assert outcome["status"] == "needs_review"
    assert outcome["reason_codes"] == ["multiple_screen_candidates"]


@pytest.mark.parametrize("search", [{}, [], False, None])
def test_invalid_search_never_expands_to_full_screen(search):
    frame = np.full((100, 100, 3), 150, np.uint8)
    asset = np.random.default_rng(11).integers(35, 230, (20, 20, 3), dtype=np.uint8)
    frame[60:80, 60:80] = asset
    ref = {"x": .6, "y": .6, "width": .2, "height": .2}
    assert _anchor_result(_anchor(ref, search), frame, _png(asset))["status"] == "failed"


@pytest.mark.parametrize("width,height,rect", [
    (100, 100, {"x": .205, "y": .305, "width": .1, "height": .1}),
    (101, 203, {"x": .2, "y": .15, "width": .1, "height": .05}),
    (333, 777, {"x": .21, "y": .17, "width": .09, "height": .035}),
])
def test_fractional_reference_crop_round_trips(width, height, rect):
    frame = np.full((height, width, 3), 150, np.uint8)
    left, top, right, bottom = _pixel_rect(rect, width, height)
    asset = np.random.default_rng(1).integers(
        35, 230, (bottom - top, right - left, 3), dtype=np.uint8)
    frame[top:bottom, left:right] = asset
    assert match_anchor(frame, asset, rect)["scores"][0] >= .999
    outcome = _anchor_result(_anchor(rect, rect), frame, _png(asset))
    assert outcome["status"] == "passed"
    assert outcome["matched_rect"]["x"] == left / width
    assert outcome["matched_rect"]["width"] == (right - left) / width


def test_threshold_is_applied_before_rounding():
    rng = np.random.default_rng(0)
    asset = rng.integers(40, 220, (24, 24, 3), dtype=np.uint8)
    frame = np.clip(asset.astype(float) + 25.76580983905842 * rng.normal(
        0, 1, asset.shape), 0, 255).astype(np.uint8)
    raw = float(cv2.matchTemplate(frame, asset, cv2.TM_CCOEFF_NORMED)[0, 0])
    assert .89995 <= raw < .9
    outcome = _anchor_result(_anchor(FULL, FULL), frame, _png(asset))
    assert outcome["status"] == "failed"
    assert outcome["matched_rect"] is None


def test_margin_is_applied_before_rounding():
    rng = np.random.default_rng(0)
    asset = rng.integers(40, 220, (24, 24, 3), dtype=np.uint8)
    frame = np.full((24, 72, 3), 150, np.uint8)
    for left, scale in [(0, 21.02334208027969), (48, 33.48450056424099)]:
        frame[:, left:left + 24] = np.clip(
            asset.astype(float) + scale * rng.normal(0, 1, asset.shape),
            0, 255).astype(np.uint8)
    scores = cv2.matchTemplate(frame, asset, cv2.TM_CCOEFF_NORMED)
    assert scores[0, 0] >= .9 and 0 < scores[0, 0] - scores[0, 48] < .08
    ref = {"x": 0, "y": 0, "width": 1 / 3, "height": 1}
    outcome = _anchor_result(_anchor(ref, FULL), frame, _png(asset))
    assert outcome["status"] == "failed"
    assert outcome["reason_code"] == "anchor_ambiguous"


def test_live_crop_needs_texture_not_just_high_correlation():
    values = np.random.default_rng(123).integers(35, 230, (24, 24), dtype=np.uint8)
    asset = cv2.cvtColor(values, cv2.COLOR_GRAY2BGR)
    frame = np.clip(128 + (asset.astype(float) - 128) * .02, 0, 255).astype(np.uint8)
    assert _anchor_result(_anchor(FULL, FULL), frame, _png(asset))["status"] == "failed"


@pytest.mark.parametrize("decoder,error", [
    (decode_image, ImageDecodeError), (_decode_jpeg, RegionError),
])
def test_empty_evidence_has_a_declared_error(decoder, error):
    with pytest.raises(error):
        decoder(b"")


@pytest.mark.parametrize("side", ["right", "left", "top", "bottom"])
@pytest.mark.parametrize("reach", [15, 40, 100])
def test_crop_cannot_hide_a_connected_protrusion(side, reach):
    frame = _scene(False)
    boxes = {
        "right": ((330, 220), (340 + reach, 240)),
        "left": ((100 - reach, 220), (110, 240)),
        "top": ((210, 100 - reach), (230, 110)),
        "bottom": ((210, 470), (230, 480 + reach)),
    }
    cv2.rectangle(frame, *boxes[side], (200,) * 3, -1)
    result = _detect_in_rect(
        frame, {"left": 85, "top": 85, "right": 355, "bottom": 495})
    assert result["status"] == "needs_review"
