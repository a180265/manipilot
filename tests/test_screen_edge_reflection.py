"""Photometric separation of adjoining reflections, without inferred corners."""
import cv2
import numpy as np
import pytest

from drivers.screen_edge import ScreenEdgeError, detect_screen_quad, locate_screen_quad
from drivers.screen_pose import fit_pose


CORNERS = np.float32([[150, 100], [1150, 100], [1150, 560], [150, 560]])


def reflected_phone(*, reflection=130, inverted=False, occluded=False):
    frame = np.zeros((720, 1280, 3), np.uint8)
    cv2.fillConvexPoly(frame, CORNERS.astype(np.int32), (220,) * 3)
    # Dim adjoining material survives ordinary foreground/background Otsu.
    cv2.rectangle(frame, (480, 93), (504, 105), (reflection,) * 3, -1)
    # Ordinary display content supplies the foreground's separate intensity mode.
    for x in range(300, 1050, 125):
        cv2.rectangle(frame, (x, 270), (x + 65, 490), (175,) * 3, -1)
    # Vignetting dims one true side: a high threshold can invent an inner edge.
    frame[100:561, 1100:1151] = np.linspace(220, 175, 51, dtype=np.uint8)[None, :, None]
    if occluded:
        frame[100:561, 1100:1151] = 220
        cv2.rectangle(frame, (1135, 125), (1190, 535), (0,) * 3, -1)
    return cv2.bitwise_not(frame) if inverted else frame


@pytest.mark.parametrize('inverted', [False, True])
@pytest.mark.parametrize('jpeg', [False, True])
def test_dim_adjoining_reflection_uses_four_observed_screen_sides(inverted, jpeg):
    frame = reflected_phone(inverted=inverted)
    if jpeg:
        frame = cv2.imdecode(cv2.imencode('.jpg', frame,
            [cv2.IMWRITE_JPEG_QUALITY, 90])[1], cv2.IMREAD_COLOR)
    quad = locate_screen_quad(frame, CORNERS.tolist(), require_fittable=True)
    np.testing.assert_allclose(quad['corners'], CORNERS, atol=1.5)
    assert fit_pose(CORNERS, quad['corners'])['fitErrorPx'] < 1.5


@pytest.mark.parametrize('reflection', [130, 220])
def test_missing_side_with_adjoining_reflection_still_rejected(reflection):
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(reflected_phone(reflection=reflection, occluded=True),
                           CORNERS.tolist(), require_fittable=True)


def test_same_brightness_tab_cannot_be_deleted_as_reflection():
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(reflected_phone(reflection=220),
                           CORNERS.tolist(), require_fittable=True)


@pytest.mark.parametrize('inverted', [False, True])
@pytest.mark.parametrize('jpeg', [False, True])
def test_unanchored_reflection_detection_measures_all_four_sides(inverted, jpeg):
    frame = reflected_phone(inverted=inverted)
    if jpeg:
        frame = cv2.imdecode(cv2.imencode('.jpg', frame,
            [cv2.IMWRITE_JPEG_QUALITY, 90])[1], cv2.IMREAD_COLOR)
    quad = locate_screen_quad(frame)
    np.testing.assert_allclose(quad['corners'], CORNERS, atol=1.5)
    assert fit_pose(CORNERS, quad['corners'])['fitErrorPx'] < 1.5


@pytest.mark.parametrize('reflection', [130, 220])
def test_unanchored_reflection_does_not_hide_a_missing_side(reflection):
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(reflected_phone(reflection=reflection, occluded=True))


def test_unanchored_same_brightness_tab_remains_rejected():
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(reflected_phone(reflection=220))


def test_unanchored_reflection_does_not_choose_between_two_screens():
    frame = np.zeros((720, 1280, 3), np.uint8)
    phone = cv2.resize(reflected_phone(), (576, 324))
    frame[150:474, 20:596] = phone
    frame[150:474, 670:1246] = phone
    with pytest.raises(ScreenEdgeError, match='多个可能'):
        detect_screen_quad(frame)


def test_unanchored_first_threshold_cannot_hide_a_second_reflected_screen():
    frame = np.zeros((720, 2560, 3), np.uint8)
    cv2.rectangle(frame, (150, 100), (1150, 560), (175,) * 3, -1)
    frame[:, 1280:] = reflected_phone()
    with pytest.raises(ScreenEdgeError, match='多个可能'):
        detect_screen_quad(frame)


@pytest.mark.parametrize('inverted', [False, True])
def test_observation_region_finds_reflected_screen_without_saved_corners(inverted):
    from runtime.phone_region_context import locate_quad_in_region

    region = {'rect': {'x': .05, 'y': .04, 'width': .9, 'height': .85}}
    quad = locate_quad_in_region(reflected_phone(inverted=inverted), region, None)
    np.testing.assert_allclose(quad['corners'], CORNERS, atol=1.5)
