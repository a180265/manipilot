"""Approach-only geometry continuation needs an explicit trusted clean reference."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from drivers.screen_edge import ScreenEdgeError, locate_screen_quad, quad_deviation_px


CORNERS = np.array([[150, 100], [1150, 100], [1150, 560], [150, 560]], dtype=float)


def phone(corners=CORNERS):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.fillConvexPoly(frame, np.rint(corners).astype(np.int32), (140, 140, 140))
    return frame


def clean_and_bridged():
    clean = phone()
    # The support already exists outside the phone, separated by a real gap.
    cv2.rectangle(clean, (490, 70), (530, 94), (140, 140, 140), -1)
    current = clean.copy()
    cv2.rectangle(current, (506, 94), (514, 104), (140, 140, 140), -1)
    reference = locate_screen_quad(clean, CORNERS.tolist())["corners"]
    return clean, current, reference


def locate(frame, reference):
    return locate_screen_quad(frame, CORNERS.tolist(), require_fittable=True,
                              contact_reference_corners=reference)


def test_new_bridge_to_existing_support_uses_current_four_observed_sides():
    _, current, reference = clean_and_bridged()
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(current, CORNERS.tolist(), require_fittable=True)
    located = locate(current, reference)
    np.testing.assert_allclose(located["corners"], CORNERS, atol=.1)
    assert reference == CORNERS.tolist()


@pytest.mark.parametrize("offset", [(1, 0), (0, 1), (2, 0)])
def test_reference_is_a_stability_guard_and_does_not_replace_observation(offset):
    # Sub-contact-guard movement remains measured instead of returning reference.
    current = phone(CORNERS + offset)
    located = locate(current, CORNERS.tolist())
    np.testing.assert_allclose(located["corners"], CORNERS + offset, atol=.1)


@pytest.mark.parametrize("offset", [(3, 0), (0, 3), (7, 4)])
def test_every_candidate_must_match_reference_even_without_an_exterior_patch(offset):
    with pytest.raises(ScreenEdgeError):
        locate(phone(CORNERS + offset), CORNERS.tolist())


@pytest.mark.parametrize("depth", [8, 15, 35])
def test_reference_does_not_accept_an_inward_occluder_as_the_side(depth):
    current = phone()
    cv2.rectangle(current, (1150 - depth, 125), (1200, 535), (0, 0, 0), -1)
    with pytest.raises(ScreenEdgeError):
        locate(current, CORNERS.tolist())


def test_inner_boundary_within_existing_two_pixel_contact_noise_remains_bounded():
    current = phone()
    cv2.rectangle(current, (1149, 125), (1200, 535), (0, 0, 0), -1)
    measured = locate(current, CORNERS.tolist())["corners"]
    # A visually indistinguishable 2px boundary change is the existing contact
    # guard's tolerance, not permission to infer an unobserved remote side.
    assert quad_deviation_px(measured, CORNERS.tolist()) <= 2.0
    assert measured != CORNERS.tolist()


def test_mostly_occluded_side_with_small_old_strip_still_rejects():
    current = phone()
    cv2.rectangle(current, (150, 100), (489, 106), (0, 0, 0), -1)
    cv2.rectangle(current, (514, 100), (1150, 106), (0, 0, 0), -1)
    with pytest.raises(ScreenEdgeError):
        locate(current, CORNERS.tolist())


def test_a_completely_missing_side_still_rejects():
    current = phone()
    cv2.rectangle(current, (1110, 80), (1200, 580), (0, 0, 0), -1)
    with pytest.raises(ScreenEdgeError):
        locate(current, CORNERS.tolist())


@pytest.mark.parametrize("length", [20, 40, 80])
def test_four_sparse_gradient_fragments_do_not_become_a_screen(length):
    current = np.zeros_like(phone())
    for start, end in [((650 - length // 2, 100), (650 + length // 2, 100)),
                       ((650 - length // 2, 560), (650 + length // 2, 560)),
                       ((150, 330 - length // 2), (150, 330 + length // 2)),
                       ((1150, 330 - length // 2), (1150, 330 + length // 2))]:
        cv2.line(current, start, end, (140, 140, 140), 2)
    with pytest.raises(ScreenEdgeError):
        locate(current, CORNERS.tolist())


@pytest.mark.parametrize("reference", [[], [[1, 2]], [[0, 0]] * 4,
    [[150, 100], [1150, 560], [1150, 100], [150, 560]],
    [[150, 100], [float("nan"), 100], [1150, 560], [150, 560]],
    [[-5, 100], [1150, 100], [1150, 560], [-5, 560]], "invalid"])
def test_invalid_reference_rejects(reference):
    with pytest.raises(ScreenEdgeError):
        locate(phone(), reference)


def test_contact_reference_requires_saved_expected_corners():
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(phone(), contact_reference_corners=CORNERS.tolist())


def test_contact_reference_rejects_small_phone_search():
    small = np.array([[20, 20], [40, 20], [40, 60], [20, 60]], dtype=float)
    frame = np.zeros((80, 80, 3), dtype=np.uint8)
    cv2.fillConvexPoly(frame, small.astype(np.int32), (140, 140, 140))
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(frame, small.tolist(), contact_reference_corners=small.tolist())


def test_same_invalid_shape_cannot_supply_a_clean_reference():
    _, current, _ = clean_and_bridged()
    # Qualification is the original strict call; callers cannot manufacture a
    # clean reference by running the temporal branch on another invalid frame.
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(current, CORNERS.tolist())


CAPTURES = Path(os.environ.get("SCREEN_EDGE_CONTACT_CAPTURE_DIR", str(
    Path(__file__).resolve().parents[2] / ".local/calibration-final-20261018/unlocked-live")))


def test_private_real_approach_frame_keeps_its_observed_boundary():
    hashes = {"before-scroll": "b229387ee78ae5e8ba660dd06e54a4a61beb9a4f6c7089cd9cc2663616610c51",
              "swipe-aborted": "b38494f40db97180e59e4b9620319d6ae4850031ef25668c091aa35d90887d46"}
    images = {}
    for name, digest in hashes.items():
        path = CAPTURES / f"{name}.jpg"
        if not path.is_file():
            pytest.skip("Private camera captures are not distributed")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        images[name] = cv2.imread(str(path))
    saved = [[117.41506884990497, 68.66530292238075],
             [1154.9679704687856, 81.51984327497621],
             [1159.6646903058124, 549.0876549593853],
             [102.38217626221636, 539.1266738255891]]
    clean = locate_screen_quad(images["before-scroll"], saved,
                               require_fittable=True)["corners"]
    with pytest.raises(ScreenEdgeError):
        locate_screen_quad(images["swipe-aborted"], saved, require_fittable=True)
    current = locate_screen_quad(images["swipe-aborted"], saved,
        require_fittable=True, contact_reference_corners=clean)["corners"]
    assert quad_deviation_px(clean, current) <= 2.0
    assert quad_deviation_px(saved, current) <= 2.0
