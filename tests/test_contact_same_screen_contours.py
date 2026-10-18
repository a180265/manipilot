"""Contact geometry must distinguish contour noise from another display."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from runtime import layout_region
from runtime.layout_geometry import LayoutGeometryError, locate_in_region


CORNERS = np.array([[100, 100], [800, 100], [800, 450], [100, 450]], dtype=float)
REGION = {"rect": {"x": 0, "y": 0, "width": 1, "height": 1}}


def scene(corners=CORNERS):
    frame = np.full((720, 1280, 3), 20, np.uint8)
    cv2.fillConvexPoly(frame, np.int32(corners), (180, 180, 180))
    return frame


def locate(frame):
    return locate_in_region(frame, REGION, CORNERS.tolist(),
                            contact_reference_corners=CORNERS.tolist())


def test_outer_contour_3_008px_from_four_measured_sides_is_the_same_display(monkeypatch):
    # The calibrated fitter still measures the real pixels. Model the generic
    # detector independently finding the outer contour of that same display.
    delta = 3.0083582 / np.sqrt(2)
    outer = np.asarray(CORNERS + delta * np.array(
        [[-1, -1], [1, -1], [1, 1], [-1, 1]]), dtype=np.float32)
    assert np.max(np.linalg.norm(outer - CORNERS, axis=1)) > 3
    monkeypatch.setattr(layout_region, "_screen_candidates",
                        lambda image, limit=None: [outer])
    pose = locate(scene())
    np.testing.assert_allclose(pose["corners"], CORNERS, atol=.1)


@pytest.mark.parametrize("brightness", [5, 180])
def test_a_real_separate_display_still_rejects_contact(brightness):
    frame = scene()
    cv2.rectangle(frame, (900, 140), (1200, 500), (brightness,) * 3, -1)
    with pytest.raises(LayoutGeometryError, match="multiple_screen_candidates"):
        locate(frame)


@pytest.mark.parametrize("offset", [(2, 0), (0, 2)])
def test_two_pixel_contact_boundary_still_measures_current_position(offset):
    pose = locate(scene(CORNERS + offset))
    np.testing.assert_allclose(pose["corners"], CORNERS + offset, atol=.1)


@pytest.mark.parametrize("offset", [(3, 0), (0, 3)])
def test_real_movement_beyond_two_pixels_still_rejects_contact(offset):
    with pytest.raises(LayoutGeometryError):
        locate(scene(CORNERS + offset))
