"""Offline target safety checks; no runtime, storage or hardware is started."""
from __future__ import annotations

import copy
import math
from decimal import Decimal

import pytest

from runtime import layout_geometry, layout_matching
from tests.test_crop_matching import _page_and_frame, _png


def scene():
    page, frame, assets = _page_and_frame(320, 640)
    page["targets"] = [{
        "id": "wechat_chats_tab", "anchor_id": "wechat_chats_tab", "mode": "tap",
        "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05},
    }]
    return page, frame, assets


def target(check):
    return next(item for item in check["item_results"] if item["kind"] == "target")


def test_matched_tap_without_mapping_bound_is_not_passed():
    page, frame, assets = scene()
    check = layout_matching.compute_page_check(page, _png(frame), assets, ["a", "b"])
    item = target(check)
    assert item["status"] == "failed"
    assert item["safe_rect"] is None
    assert item["reason_code"] == "mapping_error_unknown"
    assert check["status"] == "failed"
    assert all(a["status"] == "passed" for a in check["anchor_results"])
    assert all(r["status"] == "passed" for r in check["item_results"] if r["kind"] == "region")


@pytest.mark.parametrize("bound,inset", [(0, 2), (2.01, 3), (6, 6)])
def test_matched_tap_uses_current_pixel_error_bound(bound, inset):
    page, frame, assets = scene()
    check = layout_matching.compute_page_check(
        page, _png(frame), assets, ["a", "b"], mapping_error_bound_px=bound)
    item = target(check)
    assert item["status"] == "passed"
    box = item["resolved_rect"]
    assert item["safe_rect"] == pytest.approx({
        "x": box["x"] + inset / 320, "y": box["y"] + inset / 640,
        "width": box["width"] - 2 * inset / 320,
        "height": box["height"] - 2 * inset / 640,
    })


@pytest.mark.parametrize("bound", [None, -1, True, float("nan"), float("inf"), "1", 10**400])
def test_unknown_or_invalid_bound_never_becomes_zero(bound):
    page, frame, assets = scene()
    check = layout_matching.compute_page_check(
        page, _png(frame), assets, ["a", "b"], mapping_error_bound_px=bound)
    assert target(check)["reason_code"] == "mapping_error_unknown"
    assert target(check)["safe_rect"] is None


def test_empty_safe_rect_fails_only_that_target():
    page, frame, assets = scene()
    check = layout_matching.compute_page_check(
        page, _png(frame), assets, ["a", "b"], mapping_error_bound_px=16)
    assert target(check)["reason_code"] == "safe_rect_empty"
    assert target(check)["safe_rect"] is None
    assert all(r["status"] == "passed" for r in check["item_results"] if r["kind"] == "region")


def test_hover_and_observation_regions_do_not_get_click_safety():
    page, frame, assets = scene()
    page["targets"][0]["mode"] = "hover_only"
    check = layout_matching.compute_page_check(page, _png(frame), assets, ["a", "b"])
    assert check["status"] == "passed"
    assert all(item["safe_rect"] is None for item in check["item_results"])


def test_merged_frames_recompute_safety_with_interframe_drift():
    page, frame, assets = scene()
    checks = []
    for dx in (0, 2):
        moved = frame.copy()
        moved[96:128, 64:96] = 150
        moved[96:128, 64 + dx:96 + dx] = frame[96:128, 64:96]
        checks.append(layout_matching.compute_page_check(
            page, _png(moved), assets, ["a", "b"], mapping_error_bound_px=1))
    merged = layout_geometry.merge_page_checks(
        checks, page, (320, 640), mapping_error_bound_px=1)
    error = merged["anchor_results"][0]["localization_error_bound_px"]
    assert error == pytest.approx(4)
    assert target(merged)["safe_rect"]["x"] == pytest.approx(.2 + 5 / 320)
    assert target(merged)["safe_rect"]["width"] == pytest.approx(.1 - 10 / 320)


def test_maximum_calibration_residuals_are_used_not_rms_or_default():
    bundle = {
        "schemaVersion": 3, "status": "ready",
        "xy": {"validated": True, "maxResidualPx": 1.2, "residualRmsPx": .1},
        "camera": {"validated": True, "reprojectionRmsPx": .1,
                   "cameraToScreen": {"maxErrorPx": 2.3}},
    }
    assert layout_geometry.mapping_error_bound(bundle) == pytest.approx(3.5)
    for keys in (("xy", "maxResidualPx"), ("camera", "cameraToScreen", "maxErrorPx")):
        incomplete = copy.deepcopy(bundle)
        node = incomplete
        for key in keys[:-1]:
            node = node[key]
        node.pop(keys[-1])
        assert layout_geometry.mapping_error_bound(incomplete) is None
    for component in ("xy", "camera"):
        invalid = copy.deepcopy(bundle)
        invalid[component]["validated"] = False
        assert layout_geometry.mapping_error_bound(invalid) is None
    for bound in (True, -1, float("nan"), float("inf"), 10**400):
        invalid = copy.deepcopy(bundle)
        invalid["xy"]["maxResidualPx"] = bound
        assert layout_geometry.mapping_error_bound(invalid) is None


def test_displacement_error_is_not_rounded_down_before_safety():
    page, frame, assets = scene()
    # The reference is subpixel; the best discrete candidate is 0.004px away.
    page["anchors"][0]["reference_rect"]["x"] += .004 / 320
    page["anchors"][0]["reference_rect"]["width"] -= .004 / 320
    check = layout_matching.compute_page_check(
        page, _png(frame), assets, ["a", "b"], mapping_error_bound_px=2)
    error = check["anchor_results"][0]["localization_error_bound_px"]
    assert error > 0
    assert target(check)["safe_rect"]["x"] == pytest.approx(.2 + 3 / 320)


def test_error_sum_preserves_excess_above_integer_boundary():
    values = (2.0000000000000004, 1.9999999999999998)
    total = layout_matching.sum_error_bounds(*values)
    assert Decimal(str(total)) >= sum(Decimal(str(value)) for value in values)
    assert math.ceil(total) == 5
    page, frame, assets = scene()
    page["anchors"][0]["reference_rect"]["x"] += 2 / 320
    page["targets"][0]["relative_rect"].update(width=10 / 320, height=10 / 640)
    check = layout_matching.compute_page_check(
        page, _png(frame), assets, ["a", "b"],
        mapping_error_bound_px=1.9999999999999984)
    assert all(anchor["status"] == "passed" for anchor in check["anchor_results"])
    assert target(check)["reason_code"] == "safe_rect_empty"
    assert target(check)["safe_rect"] is None


@pytest.mark.parametrize("distance", [3, 4])
@pytest.mark.parametrize("direction", [(1, 0), (-1, 0), (0, 1), (0, -1)])
def test_two_frame_drift_boundary_is_direction_independent(distance, direction):
    page, frame, assets = scene()
    dx, dy = (distance * axis for axis in direction)
    second = frame.copy()
    second[96:128, 64:96] = 150
    second[96 + dy:128 + dy, 64 + dx:96 + dx] = frame[96:128, 64:96]
    checks = [layout_matching.compute_page_check(
        page, _png(image), assets, ["a", "b"], mapping_error_bound_px=1)
        for image in (frame, second)]
    assert all(check["status"] == "passed" for check in checks)
    merged = layout_geometry.merge_page_checks(
        checks, page, (320, 640), mapping_error_bound_px=1)
    assert (merged["status"] == "passed") is (distance == 3)
    assert (target(merged)["safe_rect"] is not None) is (distance == 3)
