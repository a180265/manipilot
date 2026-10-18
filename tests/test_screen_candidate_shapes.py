"""Background reflections must not become independent display candidates."""
import cv2
import numpy as np
import pytest

from runtime.layout_geometry import LayoutGeometryError, locate_in_region
from runtime.layout_region import _screen_candidates


def scene():
    image = np.full((720, 1280, 3), 20, np.uint8)
    saved = [[100, 80], [1150, 80], [1150, 550], [100, 550]]
    cv2.fillConvexPoly(image, np.int32(saved), (180, 180, 180))
    return image, saved


@pytest.mark.parametrize("brightness", [80, 220])
def test_irregular_background_patch_is_not_a_second_screen(brightness):
    image, saved = scene()
    # Five-sided support reflection, matching the live false-positive shape.
    patch = np.int32([[380, 565], [467, 565], [491, 602], [455, 620], [385, 622]])
    cv2.fillConvexPoly(image, patch, (brightness,) * 3)
    assert len(_screen_candidates(image)) == 1
    pose = locate_in_region(image, {"rect": {"x": 0, "y": 0, "width": 1, "height": 1}}, saved)
    assert pose["movementPx"] < 2


@pytest.mark.parametrize("brightness", [5, 80, 220])
def test_small_independent_real_screen_still_blocks(brightness):
    image, saved = scene()
    cv2.rectangle(image, (400, 580), (450, 680), (brightness,) * 3, -1)
    assert len(_screen_candidates(image)) == 2
    with pytest.raises(LayoutGeometryError, match="multiple_screen_candidates"):
        locate_in_region(image, {"rect": {"x": 0, "y": 0, "width": 1, "height": 1}}, saved)
