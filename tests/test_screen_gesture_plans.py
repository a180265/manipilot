"""Gesture calculation is read-only and shares production geometry."""
from copy import deepcopy

import pytest

from drivers import screen_gestures as sg
from tests.test_screen_gesture_driver import gesture_bundle


def test_ready_calibration_generates_all_directions_without_mutation():
    bundle = gesture_bundle()
    before = deepcopy(bundle)
    plans = sg.default_plans(bundle)
    assert [p["id"] for p in plans] == [
        "double_tap_center", "swipe_up", "swipe_down", "swipe_left", "swipe_right", "bottom_up"]
    assert all(p["calculated"] and p["effectStatus"] == "unverified" for p in plans)
    up, down = plans[1:3]
    assert up["start"] == down["end"] and up["end"] == down["start"]
    assert up["start"]["y"] > up["end"]["y"]
    assert up["points"]["start"]["cameraX"] < up["points"]["end"]["cameraX"]
    assert up["durationMs"] >= 1300
    assert plans[0]["repeatLiftMm"] == 3 and plans[0]["intervalMs"] == 50
    assert bundle == before


def test_camera_point_and_screen_point_have_identical_plans():
    bundle = gesture_bundle()
    point = {"x": .28, "y": .63}
    planned = sg.plan_gesture("double_tap", {"point": point}, bundle)
    converted = sg.point_from_camera(planned["points"]["point"], bundle)
    assert converted == pytest.approx(point)
    assert sg.plan_gesture("double_tap", {"point": converted}, bundle)["point"] == pytest.approx(point)


def test_bottom_candidate_is_generated_inside_current_measured_edge():
    bundle = gesture_bundle()
    bundle["screen"]["edgeModel"]["corners"][0][0] = 122
    bundle["screen"]["edgeModel"]["corners"][3][0] = 122
    with pytest.raises(ValueError):
        sg.resolve_point({"source": "screen_normalized", "x": .5, "y": .99}, bundle)
    bottom = sg.default_plans(bundle)[-1]
    assert bottom["calculated"]
    assert .95 < bottom["start"]["y"] < .99
    sg.plan_gesture("swipe", {"start": bottom["start"], "end": bottom["end"]}, bundle)


@pytest.mark.parametrize("change", ["draft", "missing_h", "bad_z", "bad_feed"])
def test_bad_calibration_never_produces_executable_plan(change):
    bundle = gesture_bundle()
    if change == "draft":
        bundle["status"] = "draft"
    elif change == "missing_h":
        bundle["camera"]["cameraToScreen"] = {}
    elif change == "bad_z":
        bundle["z"]["tapDepth"] = 0
    else:
        bundle["z"]["tapFeed"] = 10  # 低于 F30 下限（F300 已解禁）
    with pytest.raises(ValueError):
        sg.plan_gesture("double_tap", {"point": {"x": .5, "y": .5}}, bundle)


@pytest.mark.parametrize("points", [
    {"start": {"x": .5, "y": .5}, "end": {"x": .5, "y": .5}},
    {"start": {"x": -.1, "y": .5}, "end": {"x": .5, "y": .5}},
    {"start": {"x": True, "y": .5}, "end": {"x": .5, "y": .5}},
    {"start": {"x": .5, "y": .5}, "end": {"x": .5, "y": .8}, "feed": 1},
])
def test_arbitrary_bad_path_does_not_snap_or_override(points):
    with pytest.raises(ValueError):
        sg.plan_gesture("swipe", points, gesture_bundle())


def test_wrong_camera_dimensions_are_not_silently_rescaled():
    with pytest.raises(ValueError):
        sg.point_from_camera(
            {"cameraX": 640, "cameraY": 360, "frameW": 1920, "frameH": 1080},
            gesture_bundle())


def test_short_swipe_is_calculated_within_minimum_feed_and_duration():
    plan = sg.plan_gesture("swipe", {
        "start": {"x": .5, "y": .5}, "end": {"x": .5, "y": .50326}}, gesture_bundle())
    assert 150 <= plan["durationMs"] < 600
    assert 100 <= plan["distanceMm"] / plan["durationMs"] * 60000 <= 3000


@pytest.mark.parametrize("distance", [-1, 0, .1, float("inf")])
def test_impossible_swipe_timing_is_rejected(distance):
    with pytest.raises(ValueError):
        sg.automatic_swipe_duration(distance)
