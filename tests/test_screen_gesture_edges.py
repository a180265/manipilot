"""Read-only edge regressions using geometry from the reported debug requests."""
from copy import deepcopy

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb, screen_gestures as sg
from drivers.target_archive import resolve_target
from tests.test_screen_gesture_driver import gesture_bundle


@pytest.fixture
def edge_bundle():
    bundle = gesture_bundle()
    bundle["screen"]["edgeModel"]["corners"] = [
        [137, 142], [1155, 121], [1161, 588], [157, 595],
    ]
    bundle["screen"]["edgeModel"]["tolerancePx"] = 8.0
    bundle["camera"]["cameraToScreen"] = {
        "homography": [
            [-0.012871977236070717, 2.220430745399773, -251.87325261227292],
            [-2.249963037195894, 0.009906832663950194, 2602.6037796221212],
            [-0.00003334726118351913, 0.000007039823325054326, 1],
        ],
        "maxErrorPx": 2.96,
    }
    bundle["xy"]["maxResidualPx"] = 2.552
    bundle["xy"]["robotToScreen"] = {
        "a": -15.530072, "b": -0.117338, "c": 758.317502,
        "d": -0.229816, "e": 15.464716, "f": 1515.577774,
        "qx2": 0.001161, "qxy": -0.000209, "qy2": 0.000574,
        "px2": -0.000557, "pxy": 0.000205, "py2": -0.00027,
    }
    return bundle


def camera_point(x, y):
    return {"cameraX": x, "cameraY": y, "frameW": 1280, "frameH": 720}


@pytest.mark.parametrize("start,end", [
    ((146.80309686634348, 402.3375822368421), (533.6951393698062, 393.6520461824792)),
    ((149.69756319252076, 433.0050802891274), (533.6951393698062, 393.6520461824792)),
    ((145.4, 440.6), (533.6951393698062, 393.6520461824792)),
])
def test_reported_edge_point_uses_same_coordinates_as_calibrated_click(edge_bundle, start, end):
    before = deepcopy(edge_bundle)
    contour = np.asarray(edge_bundle["screen"]["edgeModel"]["corners"], dtype=np.float32)
    assert -8 < cv2.pointPolygonTest(contour, start, True) < 0
    points = {
        "start": sg.point_from_camera(camera_point(*start), edge_bundle),
        "end": sg.point_from_camera(camera_point(*end), edge_bundle),
    }
    plan = sg.plan_gesture("swipe", points, edge_bundle)
    mapped = sg.resolve_point({"source": "screen_normalized", **points["start"]}, edge_bundle)
    click = resolve_target(
        {"nx": start[0] / 1280, "ny": start[1] / 720},
        edge_bundle, sg.calibrated_profile(edge_bundle),
    )
    assert (mapped["robotX"], mapped["robotY"]) == (click["robotX"], click["robotY"])
    assert plan["points"]["start"] == pytest.approx(camera_point(*start))
    assert plan["start"] == points["start"]
    assert edge_bundle == before


def test_larger_reported_contour_disagreement_remains_rejected(edge_bundle):
    with pytest.raises(ValueError, match="轮廓及其容差"):
        sg.point_from_camera(camera_point(137.15492988227146, 398.2628819684903), edge_bundle)


@pytest.mark.parametrize("x,y", [(.008, .5), (.992, .5), (.5, .008), (.5, .992)])
def test_all_four_edges_can_swipe_inward_within_recorded_contour_tolerance(x, y):
    bundle = gesture_bundle()
    bundle["screen"]["edgeModel"]["corners"] = [
        [111, 111], [1189, 111], [1189, 589], [111, 589],
    ]
    points = {"start": {"x": x, "y": y}, "end": {"x": .5, "y": .5}}
    plan = sg.plan_gesture("swipe", points, bundle)
    assert plan["start"] == points["start"]
    assert plan["end"] == points["end"]


@pytest.mark.parametrize("tolerance", [0, 0.1, 1])
def test_smaller_recorded_tolerance_is_not_expanded(edge_bundle, tolerance):
    edge_bundle["screen"]["edgeModel"]["tolerancePx"] = tolerance
    with pytest.raises(ValueError, match="轮廓及其容差"):
        sg.point_from_camera(camera_point(146.80309686634348, 402.3375822368421), edge_bundle)


def test_missing_tolerance_does_not_enable_an_extra_margin(edge_bundle):
    del edge_bundle["screen"]["edgeModel"]["tolerancePx"]
    with pytest.raises(ValueError, match="轮廓及其容差"):
        sg.point_from_camera(camera_point(146.80309686634348, 402.3375822368421), edge_bundle)


@pytest.mark.parametrize("tolerance", [-1, 8.01, 1000, True, float("nan"), float("inf")])
def test_invalid_or_excessive_tolerance_cannot_expand_edge_domain(edge_bundle, tolerance):
    edge_bundle["screen"]["edgeModel"]["tolerancePx"] = tolerance
    with pytest.raises(ValueError, match="轮廓容差"):
        sg.resolve_point({"source": "screen_normalized", "x": .5, "y": .5}, edge_bundle)


@pytest.mark.parametrize("x,y", [(0, .5), (1, .5), (.5, 0), (.5, 1), (-.001, .5), (.5, 1.001)])
def test_contour_tolerance_does_not_relax_calibrated_touch_bounds(edge_bundle, x, y):
    before = deepcopy(edge_bundle)
    with pytest.raises(ValueError, match="屏幕"):
        sg.resolve_point({"source": "screen_normalized", "x": x, "y": y}, edge_bundle)
    assert edge_bundle == before


def test_interior_of_swipe_cannot_exit_contour_tolerance():
    bundle = gesture_bundle()
    bundle["screen"].update(screenWidth=1000, screenHeight=1000)
    bundle["camera"]["cameraToScreen"]["homography"] = [[1, 0, 0], [0, 2, 0], [0, 0, 1]]
    bundle["screen"]["edgeModel"]["corners"] = [[0, 30], [1100, 30], [1100, 700], [0, 700]]
    # Endpoints are inside the contour; mid-stroke y=25 is inside the screen
    # but projects to camera y=12.5, outside the recorded 8px contour tolerance.
    bundle["xy"]["robotToScreen"] = {
        "a": 10, "b": 0, "c": 0, "d": -5.5, "e": 20, "f": 162.5,
        "qx2": 0, "qxy": 0, "qy2": 0, "px2": .055, "pxy": 0, "py2": 0,
    }
    start = sg.resolve_point({"source": "screen_normalized", "x": .1, "y": .113}, bundle)
    end = sg.resolve_point({"source": "screen_normalized", "x": .9, "y": .113}, bundle)
    assert cb.screen_from_robot(bundle["xy"]["robotToScreen"], 50, 0)[1] == 25
    with pytest.raises(ValueError, match="轮廓及其容差"):
        sg.validate_swipe_path(start, end, bundle)
