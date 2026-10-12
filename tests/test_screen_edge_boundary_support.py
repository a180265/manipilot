"""Localized boundary artifacts must not replace observed screen geometry."""

from pathlib import Path
import hashlib
import json

import cv2
import numpy as np
import pytest

from drivers.real_camera import RealCameraDriver, ScreenRoiNotFound
from drivers.screen_edge import ScreenEdgeError, detect_screen_quad, locate_screen_quad


CORNERS = np.array([[150, 100], [1150, 100], [1150, 560], [150, 560]], dtype=float)


def phone_with_tabs(tabs):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.fillConvexPoly(frame, CORNERS.astype(np.int32), (140, 140, 140))
    for left, top, right, bottom in tabs:
        cv2.rectangle(frame, (left, top), (right, bottom), (140, 140, 140), -1)
    return frame


@pytest.mark.parametrize("width,depth", [(18, 6), (24, 7), (32, 6), (38, 8)])
@pytest.mark.parametrize("jpeg", [False, True])
def test_single_frame_outer_tab_is_not_sufficient_evidence_to_relax_occlusion(width, depth, jpeg):
    frame = phone_with_tabs([(490, 100 - depth, 490 + width, 104)])
    if jpeg:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        assert ok
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(frame)
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, CORNERS.tolist(), require_fittable=True)


@pytest.mark.parametrize("tabs", [
    [(490, 93, 555, 104)],                         # broad exterior region
    [(490, 80, 510, 104)],                         # narrow but deep extension
    [(490, 93, 510, 104), (700, 93, 720, 104)],    # separated exterior patches
    [(160, 93, 180, 104)],                        # near a corner
    [(490, 93, 510, 104), (490, 557, 510, 567)],   # more than one side
])
def test_extended_or_ambiguous_boundary_remains_rejected(tabs):
    frame = phone_with_tabs(tabs)
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(frame)


@pytest.mark.parametrize("depth", [8, 15, 35, 70])
def test_inward_occlusion_cannot_be_relabelled_as_an_outer_artifact(depth):
    frame = phone_with_tabs([])
    cv2.rectangle(frame, (1150 - depth, 125), (1200, 535), (0, 0, 0), -1)
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(frame)
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, CORNERS.tolist(), require_fittable=True)


def test_diagonal_corner_excursion_combined_with_outer_tab_is_rejected():
    frame = phone_with_tabs([(490, 93, 514, 104)])
    cv2.fillConvexPoly(frame, np.int32([
        [150, 100], [146, 97], [150, 106], [158, 100],
    ]), (140, 140, 140))
    assert cv2.pointPolygonTest(CORNERS.astype(np.float32), (146.0, 97.0), True) == -5.0
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(frame)
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, CORNERS.tolist(), require_fittable=True)


def test_mostly_occluded_side_is_not_a_small_outer_tab_or_a_new_calibration():
    frame = phone_with_tabs([])
    cv2.rectangle(frame, (150, 100), (489, 105), (0, 0, 0), -1)
    cv2.rectangle(frame, (514, 100), (1150, 105), (0, 0, 0), -1)
    # Capture must fail before its wrong corners can become a self-consistent
    # same-frame calibration. An independent correct reference must also reject.
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(frame)
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, CORNERS.tolist(), require_fittable=True)


CAPTURES = (Path(__file__).resolve().parents[2] /
            "reports/wx-console-v1.10/live-registration-2026-10-08/arm-01")


@pytest.mark.parametrize("prefix,expected_quality", [
    ("01-wechat-list", "clear"), ("02-chat-ime-hidden", "clear"),
    ("03-keyboard-candidate", "abnormal"), ("04-ime-text", "abnormal"),
    ("06-phone-b-hidden", "abnormal"), ("07-phone-b-list", "abnormal"),
])
def test_local_saved_phone_states_preserve_strict_boundary_outcome(prefix, expected_quality):
    manifest_path = CAPTURES / f"{prefix}-draft.json"
    if not manifest_path.is_file():
        pytest.skip("Private local capture is not distributed with the test suite")
    manifest = json.loads(manifest_path.read_text())
    capture = manifest["captures"][0]
    source = CAPTURES / capture["file"]
    if not source.is_file():
        pytest.skip("Private local image is unavailable")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == capture["sha256"]
    frame = cv2.imread(str(source))
    assert frame is not None
    ok, png = cv2.imencode(".png", frame)
    assert ok
    if expected_quality == "clear":
        metrics = RealCameraDriver._analyze(png.tobytes(), require_phone=True)
        assert RealCameraDriver._classify(metrics, False) == expected_quality
    else:
        with pytest.raises(ScreenRoiNotFound):
            RealCameraDriver._analyze(png.tobytes(), require_phone=True)
