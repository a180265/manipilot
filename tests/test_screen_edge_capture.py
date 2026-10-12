"""Capture/runtime corner consistency, without cameras or hardware services."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from drivers.screen_edge import ScreenEdgeError, detect_screen_quad, locate_screen_quad
from drivers.screen_pose import fit_pose


CORNERS = np.array(
    [[150, 85], [1155, 130], [1180, 580], [125, 545]], dtype=float)


def _phone_frame(*, radius=30, probe=False, brightness=80, background=0):
    mask = np.zeros((461, 1001), dtype=np.uint8)
    cv2.rectangle(mask, (radius, 0), (1000 - radius, 460), 255, -1)
    cv2.rectangle(mask, (0, radius), (1000, 460 - radius), 255, -1)
    for x in (radius, 1000 - radius):
        for y in (radius, 460 - radius):
            cv2.circle(mask, (x, y), radius, 255, -1)
    mapping = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [1000, 0], [1000, 460], [0, 460]]),
        CORNERS.astype(np.float32))
    mask = cv2.warpPerspective(mask, mapping, (1280, 720))
    gray = np.rint(
        background + mask.astype(float) / 255 * (brightness - background)
    ).astype(np.uint8)
    frame = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if probe:
        cv2.circle(frame, (835, 320), 42, (background,) * 3, -1)
        cv2.rectangle(frame, (825, 320), (850, 680), (background,) * 3, -1)
    return frame


@pytest.mark.parametrize("radius,probe", [(0, False), (30, False), (30, True)])
def test_capture_uses_observed_intersections_and_strict_same_frame_pose(radius, probe):
    frame = _phone_frame(radius=radius, probe=probe)
    captured = detect_screen_quad(frame)
    located = locate_screen_quad(
        frame, captured["corners"], require_fittable=True)
    np.testing.assert_allclose(captured["corners"], CORNERS, atol=1.5)
    np.testing.assert_allclose(located["corners"], captured["corners"], atol=1.5)
    pose = fit_pose(captured["corners"], located["corners"])
    assert pose["movementPx"] <= 2
    assert pose["fitErrorPx"] <= 2


@pytest.mark.skipif(
    not Path("/tmp/wx-phone-current.jpg").is_file(),
    reason="Optional read-only camera snapshot is unavailable")
def test_read_only_camera_snapshot_capture_strict_same_frame_pose():
    frame = cv2.imread("/tmp/wx-phone-current.jpg")
    assert frame is not None
    captured = detect_screen_quad(frame)
    located = locate_screen_quad(
        frame, captured["corners"], require_fittable=True)
    np.testing.assert_allclose(located["corners"], captured["corners"], atol=1.5)
    assert fit_pose(captured["corners"], located["corners"])["movementPx"] <= 2


@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("brightness,background", [(24, 0), (0, 220), (80, 0)])
def test_fresh_capture_keeps_canonical_corners_across_display_polarity(
        probe, brightness, background):
    captured = detect_screen_quad(_phone_frame(brightness=200))
    located = locate_screen_quad(
        _phone_frame(probe=probe, brightness=brightness, background=background),
        captured["corners"], require_fittable=True)
    np.testing.assert_allclose(located["corners"], CORNERS, atol=1.5)
    assert fit_pose(captured["corners"], located["corners"])["movementPx"] <= 2


@pytest.mark.parametrize("depth", [15, 35, 70])
def test_capture_rejects_a_missing_side_instead_of_using_the_occluder_edge(depth):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(frame, (150, 100), (1150, 560), (80, 80, 80), -1)
    cv2.rectangle(frame, (1150 - depth, 125), (1200, 535), (0, 0, 0), -1)
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(frame)


def test_legacy_inscribed_corners_are_not_silently_reused_as_a_definition():
    legacy = [[151, 104], [1138, 132], [1176, 562], [145, 543]]
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(_phone_frame(), legacy, require_fittable=True)


@pytest.mark.parametrize("gap", [2, 3, 4])
def test_capture_does_not_merge_a_nearby_stand_into_the_phone(gap):
    frame = np.full((720, 1280, 3), 8, dtype=np.uint8)
    cv2.rectangle(frame, (150, 100), (1150, 560), (40, 40, 40), -1)
    cv2.rectangle(frame, (220, 560 + gap), (240, 719), (40, 40, 40), -1)

    captured = detect_screen_quad(frame)
    located = locate_screen_quad(
        frame, captured["corners"], require_fittable=True)

    expected = [[150, 100], [1150, 100], [1150, 560], [150, 560]]
    np.testing.assert_allclose(captured["corners"], expected, atol=1.5)
    np.testing.assert_allclose(located["corners"], expected, atol=1.5)
    assert fit_pose(captured["corners"], located["corners"])["movementPx"] <= 2


@pytest.mark.parametrize("gap", [2, 3, 4])
def test_capture_does_not_replace_phone_bottom_with_wide_stand(gap):
    frame = np.full((720, 1280, 3), 8, dtype=np.uint8)
    cv2.rectangle(frame, (150, 100), (1150, 560), (40, 40, 40), -1)
    cv2.rectangle(frame, (150, 560 + gap), (1150, 610), (40, 40, 40), -1)
    expected = [[150, 100], [1150, 100], [1150, 560], [150, 560]]

    captured = detect_screen_quad(frame)
    located = locate_screen_quad(
        frame, captured["corners"], require_fittable=True)

    np.testing.assert_allclose(captured["corners"], expected, atol=1.5)
    np.testing.assert_allclose(located["corners"], expected, atol=1.5)


def test_initial_capture_rejects_competing_environment_rectangle():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(frame, (80, 130), (550, 590), (80, 80, 80), -1)
    cv2.rectangle(frame, (680, 130), (1170, 590), (80, 80, 80), -1)

    with pytest.raises(ScreenEdgeError, match="多个可能"):
        detect_screen_quad(frame)


def test_initial_capture_rejects_larger_environment_rectangle():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(frame, (150, 100), (610, 560), (80, 80, 80), -1)
    cv2.rectangle(frame, (680, 80), (1180, 650), (80, 80, 80), -1)

    with pytest.raises(ScreenEdgeError, match="多个可能"):
        detect_screen_quad(frame)


def test_independent_bright_rectangle_requires_phone_identity():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(frame, (150, 100), (1150, 560), (80, 80, 80), -1)
    cv2.rectangle(frame, (350, 580), (750, 710), (80, 80, 80), -1)

    with pytest.raises(ScreenEdgeError, match="多个可能"):
        detect_screen_quad(frame)


def test_compressed_narrow_gap_cannot_become_a_wrong_baseline():
    frame = np.full((720, 1280, 3), 8, dtype=np.uint8)
    cv2.rectangle(frame, (150, 100), (1150, 560), (40, 40, 40), -1)
    cv2.rectangle(frame, (150, 562), (1150, 610), (40, 40, 40), -1)
    compressed = cv2.imdecode(cv2.imencode(
        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 50])[1], cv2.IMREAD_COLOR)

    with pytest.raises(ScreenEdgeError, match="去噪前后不一致"):
        detect_screen_quad(compressed)


@pytest.mark.parametrize("size", [(64, 48), (96, 72), (128, 96)])
@pytest.mark.parametrize("border", [2, 4, 8, 10])
def test_bound_small_dark_phone_uses_observed_edges_not_bright_border(size, border):
    width, height = size
    frame = np.full((height, width, 3), 30, dtype=np.uint8)
    frame[border:height - border, border:width - border] = 220
    left, top = width // 4, height // 4
    right, bottom = width * 3 // 4 - 1, height * 3 // 4 - 1
    frame[top:bottom + 1, left:right + 1] = 0
    saved = [[left, top], [right, top], [right, bottom], [left, bottom]]

    located = locate_screen_quad(frame, saved, require_fittable=True)

    np.testing.assert_allclose(located["corners"], saved, atol=.1)
    pose = fit_pose(saved, located["corners"])
    assert pose["movementPx"] <= .1
    assert pose["fitErrorPx"] <= .1


def test_small_phone_blur_halo_does_not_hide_a_scale_change():
    frame = np.full((48, 64, 3), 30, dtype=np.uint8)
    frame[4:44, 4:60] = 220
    frame[11:37, 15:49] = 0
    saved = [[16, 12], [47, 12], [47, 35], [16, 35]]

    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, saved, require_fittable=True)
