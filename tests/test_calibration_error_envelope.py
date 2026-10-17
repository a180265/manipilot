"""Independent tap errors reach target and screen-edge safety without hardware."""
from copy import deepcopy
from decimal import Decimal

import pytest

from drivers import calibration_bundle as cb, screen_gestures, screen_pose
from drivers.daemon_arm import DaemonArmDriver
from runtime import layout_geometry, layout_matching


def bundle():
    return {
        "schemaVersion": 3, "status": "ready", "deviceId": "arm-01",
        "daemonSession": "test", "cameraId": "cam-01",
        "cameraDeviceUniqueId": "camera", "bindingRevision": 1,
        "cameraBindingId": "camera-binding", "sourceGeometryId": "geometry",
        "z": {}, "safePark": {},
        "frame": {"width": 320, "height": 640},
        "screen": {"screenWidth": 320, "screenHeight": 640,
                   "edgeModel": {"corners": [[0, 0], [319, 0], [319, 639], [0, 639]]}},
        "xy": {"validated": True, "maxResidualPx": 1, "residualRmsPx": .1,
               "robotToScreen": {"a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0}},
        "camera": {"validated": True, "cameraToScreen": {
            "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "maxErrorPx": 2}},
    }


def independent(value=15):
    result = bundle()
    result["validation"] = {"passed": True, "points": [
        {"passed": True, "touchTargetErrorPx": 14, "cameraTouchGapPx": value}]}
    return result


def test_independent_error_dominates_good_training_fit():
    calibration = independent()
    assert cb.mapping_error_bound(calibration) == 17
    assert layout_geometry.mapping_error_bound(calibration) == 17
    assert calibration["xy"]["maxResidualPx"] == 1


def test_largest_point_or_summary_wins_without_double_counting_nominal_error():
    calibration = independent()
    calibration["camera"]["visualVerify"] = {
        "passed": True, "contactVsNominalMaxPx": 22, "contactVsCameraMaxPx": 3}
    assert cb.mapping_error_bound(calibration) == 22
    calibration["xy"]["validation"] = [{"passed": True, "cameraTouchGapPx": 25}]
    assert cb.mapping_error_bound(calibration) == 27
    calibration["camera"]["visualVerify"]["contactVsCameraMaxPx"] = 30
    assert cb.mapping_error_bound(calibration) == 32


@pytest.mark.parametrize("value", [None, True, -1, "15", float("nan"), float("inf"), 10**400])
@pytest.mark.parametrize("source", ["point", "summary", "training"])
def test_invalid_present_measurement_never_falls_back(value, source):
    calibration = independent()
    if source == "point":
        calibration["validation"]["points"][0]["cameraTouchGapPx"] = value
    elif source == "summary":
        calibration["camera"]["visualVerify"] = {"contactVsNominalMaxPx": value}
    else:
        calibration["xy"]["maxResidualPx"] = value
    assert cb.mapping_error_bound(calibration) is None
    assert cb.mapping_error_bound(calibration, allow_legacy_defaults=True) is None
    assert layout_geometry.mapping_error_bound(calibration) is None


def test_failed_independent_evidence_does_not_authorize_an_envelope():
    calibration = independent()
    calibration["validation"]["points"][0]["passed"] = False
    assert cb.mapping_error_bound(calibration) is None


def test_legacy_absent_evidence_and_physical_edge_defaults_are_preserved():
    calibration = bundle()
    assert cb.mapping_error_bound(calibration) == 3
    calibration["xy"]["maxResidualPx"] = 0
    assert cb.mapping_error_bound(calibration) == 2
    assert cb.mapping_error_bound(calibration, allow_legacy_defaults=True) == 3
    del calibration["xy"]["maxResidualPx"]
    assert cb.mapping_error_bound(calibration) is None
    assert cb.mapping_error_bound(calibration, allow_legacy_defaults=True) == 8


def test_error_sum_never_rounds_down_across_a_pixel_boundary():
    calibration = independent(2.0000000000000004)
    calibration["validation"]["points"][0]["touchTargetErrorPx"] = 0
    calibration["camera"]["cameraToScreen"]["maxErrorPx"] = 1.9999999999999998
    actual = cb.mapping_error_bound(calibration)
    assert Decimal(str(actual)) >= Decimal("2.0000000000000004") + Decimal("1.9999999999999998")
    assert actual > 4


def test_independent_error_rejects_small_target_but_keeps_large_target():
    calibration = independent()
    page = {"targets": [{"id": name, "mode": "tap", "anchor_id": "anchor"}
                        for name in ("small", "large")]}
    check = {"status": "passed", "reason_codes": [], "anchor_results": [
        {"id": "anchor", "status": "passed", "matched_rect": {},
         "localization_error_bound_px": 0}], "item_results": [
            {"kind": "target", "id": name, "status": "passed", "resolved_rect": {
                "x": .2, "y": .2, "width": .5, "height": height / 640}}
            for name, height in (("small", 20), ("large", 80))]}
    previous = layout_matching.apply_target_safety(deepcopy(check), page, (320, 640), 3)
    assert all(item["status"] == "passed" for item in previous["item_results"])
    result = layout_matching.apply_target_safety(
        check, page, (320, 640), layout_geometry.mapping_error_bound(calibration))
    assert result["item_results"][0]["reason_code"] == "safe_rect_empty"
    assert result["item_results"][1]["status"] == "passed"


@pytest.mark.parametrize("consumer", ["pose", "gesture", "path"])
def test_independent_error_reaches_each_screen_edge_consumer(consumer):
    calibration = independent()

    def resolve(x):
        if consumer == "pose":
            corners = calibration["screen"]["edgeModel"]["corners"]
            return screen_pose.physical_point(
                calibration, screen_pose.fit_pose(corners, corners), x, 320)
        if consumer == "gesture":
            return screen_gestures.resolve_point(
                {"source": "screen_normalized", "x": x / 320, "y": .5}, calibration)
        return DaemonArmDriver._validate_path1_waypoints(None, [
            {"mech_points": [{"robotX": x, "robotY": 320}]}], calibration)

    with pytest.raises(ValueError):
        resolve(10)
    resolve(30)
    calibration["validation"]["points"][0]["cameraTouchGapPx"] = float("nan")
    with pytest.raises(ValueError, match="校准最大误差无效"):
        resolve(30)


def test_error_measurement_does_not_grant_calibration_qualification():
    calibration = independent()
    calibration["status"] = "draft"
    assert layout_geometry.mapping_error_bound(calibration) is None
    assert not cb.automatic_calibration_verified(calibration)
