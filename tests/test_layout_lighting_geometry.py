"""Lighting fallback measures boundaries without weakening contact/crop guards."""
import cv2
import numpy as np
import pytest

from drivers.screen_edge import ScreenEdgeError, locate_screen_quad
from runtime.layout_geometry import LayoutGeometryError, locate_in_region

CORNERS = np.array([[150, 100], [1150, 100], [1150, 560], [150, 560]], float)
REGION = {'rect': {'x': .03, 'y': .03, 'width': .94, 'height': .94}}


def reflected_phone():
    image = np.full((720, 1280, 3), 220, np.uint8)
    cv2.fillConvexPoly(image, CORNERS.astype(np.int32), (100, 100, 100))
    cv2.rectangle(image, (450, 0), (750, 125), (100, 100, 100), -1)
    return image


def test_reflection_connected_to_desk_uses_measured_edges_within_crop():
    image = reflected_phone()
    with pytest.raises(ScreenEdgeError, match='无法定位'):
        locate_screen_quad(image, CORNERS.tolist(), tolerance_px=32)
    pose = locate_in_region(image, REGION, CORNERS.tolist())
    np.testing.assert_allclose(pose['corners'], CORNERS, atol=.3)
    assert pose['movementPx'] < .3


def test_lighting_fallback_does_not_continue_contact_reference(monkeypatch):
    from drivers import screen_edge
    def absent(*args, **kwargs):
        raise ScreenEdgeError('画面中无法定位校准手机屏幕')
    monkeypatch.setattr(screen_edge, 'locate_screen_quad', absent)
    def forbidden(*args, **kwargs):
        pytest.fail('Contact must preserve the existing reference checks')
    monkeypatch.setattr(screen_edge, 'locate_quad_by_gradient_lines', forbidden)
    with pytest.raises(LayoutGeometryError):
        locate_in_region(reflected_phone(), REGION, CORNERS.tolist(),
                         contact_reference_corners=CORNERS.tolist())


def test_lighting_fallback_does_not_invent_edges_on_blank_frame():
    with pytest.raises(LayoutGeometryError):
        locate_in_region(np.full((720, 1280, 3), 160, np.uint8), REGION, CORNERS.tolist())


def test_lighting_fallback_still_rejects_crop_cutting_phone():
    tight = {'rect': {'x': 150 / 1280, 'y': 100 / 720,
                      'width': 1000 / 1280, 'height': 460 / 720}}
    with pytest.raises(LayoutGeometryError):
        locate_in_region(reflected_phone(), tight, CORNERS.tolist())


def test_gradient_iteration_cannot_extend_search_bound(monkeypatch):
    from drivers import screen_edge
    monkeypatch.setattr(screen_edge, 'locate_screen_quad',
                        lambda *a, **k: (_ for _ in ()).throw(ScreenEdgeError('画面中无法定位校准手机屏幕')))
    monkeypatch.setattr(screen_edge, 'locate_quad_by_gradient_lines',
                        lambda *a, **k: {'corners': (CORNERS + [40, 0] - [38, 21]).tolist()})
    with pytest.raises(LayoutGeometryError):
        locate_in_region(reflected_phone(), REGION, CORNERS.tolist())
