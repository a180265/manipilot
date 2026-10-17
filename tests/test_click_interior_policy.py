"""Opt-in point selection with real target credentials and no physical devices."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import numpy as np
import pytest

from drivers import calibration_bundle as cb
from drivers.arm import DriverError
from drivers.click_points import ocr_point, policy_from_bundle, rectangle_point
from tests.test_calibration_error_envelope import bundle
from tests.test_layout_observation import offline_only  # noqa: F401
from tests.test_layout_target_driver import motion, target_rig  # noqa: F401
from tests.test_pre_enable_s3_driver import locate


def policy(**insets):
    return {"mode": "screen_interior", "preferredInsetsPx": {
        "left": 0, "top": 0, "right": 0, "bottom": 0, **insets}}


def enable(rig, value):
    rig.bundle["xy"]["clickPointPolicy"] = value
    # 保存会刷新 updatedAt；后续最终守卫必须使用同一份已落盘快照。
    rig.bundle = cb.save_bundle(rig.bundle, rig.data)


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_rectangle_selection_and_retries_stay_in_the_same_intersection(attempt):
    point, supported, details = rectangle_point(
        "back_button", {"x": .1, "y": .05, "width": .2, "height": .15},
        1000, 1000, attempt, policy=policy(top=160))
    assert supported
    assert 100 < point[0] < 300 and 160 < point[1] < 200
    assert details["originalCenter"] == [200, 125]
    assert details["aim"] == point.tolist()
    assert details["selectionOffset"] == pytest.approx(point - [200, 125])
    assert details["max_offset_px"] <= 12
    assert details["preferenceSatisfied"] is True
    assert point == pytest.approx([200 + (0, -12, 12)[attempt], 180])


@pytest.mark.parametrize("insets", [
    {"left": 220}, {"right": 800}, {"top": 220}, {"bottom": 800},
    {"left": 220, "top": 220}, {"right": 800, "bottom": 800},
])
def test_each_edge_and_corners_choose_inside_original_target(insets):
    point, _, _ = rectangle_point(
        "input_box", {"x": .1, "y": .1, "width": .2, "height": .2},
        1000, 1000, policy=policy(**insets))
    assert np.all(point > 100) and np.all(point < 300)
    assert insets.get("left", 0) <= point[0] <= 1000 - insets.get("right", 0)
    assert insets.get("top", 0) <= point[1] <= 1000 - insets.get("bottom", 0)


def test_safe_center_is_preserved_and_small_intersection_allows_only_one_point():
    original, supported, _ = rectangle_point(
        "input_box", {"x": .2, "y": .2, "width": .2, "height": .2},
        1000, 1000, policy=policy(top=200))
    assert original == pytest.approx([300, 300]) and supported
    args = ("input_box", {"x": .2, "y": .2, "width": .005, "height": .005}, 1000, 1000)
    point, supported, _ = rectangle_point(*args, policy=policy(top=203))
    assert not supported and 203 < point[1] < 205
    with pytest.raises(ValueError, match="替代落点"):
        rectangle_point(*args, 1, policy=policy(top=203))


@pytest.mark.parametrize("top", [200, 201])
def test_empty_or_line_preference_uses_interior_of_the_same_target(top):
    point, _, safety = rectangle_point(
        "back_button", {"x": .1, "y": .05, "width": .2, "height": .15},
        1000, 1000, policy=policy(top=top))
    assert 100 < point[0] < 300 and 125 < point[1] < 200
    assert safety["preferenceSatisfied"] is False
    assert safety["preferenceReason"] == "no_positive_area_intersection"
    assert safety["safe_rect"] == {"x": .1, "y": .05, "width": .2, "height": .15}


@pytest.mark.parametrize("attempt", [0, 1, 2])
@pytest.mark.parametrize("insets,axis,direction", [
    ({"top": 150}, 1, 1), ({"left": 150}, 0, 1),
    ({"right": 950}, 0, -1), ({"bottom": 950}, 1, -1),
    ({"left": 150, "top": 150}, 1, 1),
])
def test_disjoint_edge_preference_never_escapes_real_control(attempt, insets, axis, direction):
    chosen, _, safety = rectangle_point(
        "back_button", {"x": .05, "y": .05, "width": .08, "height": .08},
        1000, 1000, attempt, policy=policy(**insets))
    assert np.all(chosen > 50) and np.all(chosen < 130)
    assert direction * (chosen[axis] - 90) > 0
    assert safety["preferenceSatisfied"] is False


@pytest.mark.parametrize("reverse", [False, True])
def test_disjoint_ocr_preference_preserves_perspective_and_measured_inset(reverse):
    polygon = np.array([[20, 30], [220, 40], [200, 120], [10, 100]], dtype=float)
    if reverse:
        polygon = polygon[::-1]
    for attempt in (0, 1, 2):
        camera, chosen, _, safety = ocr_point(
            "conversation_item", [0, 0, 1000, 1000], polygon.mean(axis=0), np.eye(3), 10,
            [1000, 1000], attempt, camera_polygon=polygon, policy=policy(top=150))
        assert camera == pytest.approx(chosen)
        assert safety["preferenceSatisfied"] is False
        for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
            edge, delta = b - a, chosen - a
            assert abs(edge[0] * delta[1] - edge[1] * delta[0]) / np.linalg.norm(edge) > 12


@pytest.mark.parametrize("value", [None, {}, False, {"mode": "unknown"},
    policy(top=-1), policy(top=True), policy(top="40"), policy(top=float("nan")),
    policy(top=float("inf")), policy(top=10**400), policy(top=640),
    policy(left=160, right=160), {**policy(), "unknown": 1}])
def test_invalid_present_policy_is_not_treated_as_disabled(value):
    calibration = bundle()
    calibration["xy"]["clickPointPolicy"] = value
    with pytest.raises(ValueError, match="策略无效"):
        policy_from_bundle(calibration)
    del calibration["xy"]["clickPointPolicy"]
    assert policy_from_bundle(calibration) is None


@pytest.mark.parametrize("attempt", [0, 1, 2])
@pytest.mark.parametrize("reverse", [False, True])
def test_ocr_clips_true_perspective_polygon_not_expanded_bbox(attempt, reverse):
    polygon = np.array([[100, 100], [300, 130], [285, 230], [85, 200]], dtype=float)
    if reverse:
        polygon = polygon[::-1]
    camera, chosen, supported, details = ocr_point(
        "conversation_item", [0, 0, 1000, 1000], [192.5, 165], np.eye(3), 10,
        [1000, 1000], attempt, camera_polygon=polygon, policy=policy(top=180))
    assert supported and np.allclose(camera, chosen)
    assert chosen[1] >= 180
    for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
        edge, delta = b - a, chosen - a
        assert abs(edge[0] * delta[1] - edge[1] * delta[0]) / np.linalg.norm(edge) > 12
    assert details["selectionOffset"] == pytest.approx(chosen - [192.5, 165])


@pytest.mark.parametrize("error", [None, -1, True, float("nan")])
def test_policy_never_falls_back_on_unknown_ocr_error(error):
    with pytest.raises(ValueError, match="最大误差未知"):
        ocr_point("conversation_item", [10, 10, 110, 50], [60, 30], np.eye(3), error,
                  [320, 640], camera_polygon=[[10, 10], [110, 10], [110, 50], [10, 50]],
                  policy=policy())


def test_ocr_policy_requires_original_polygon_and_never_expands_a_narrow_word():
    args = ("conversation_item", [0, 0, 320, 640], [60, 30], np.eye(3), 5, [320, 640])
    with pytest.raises(ValueError, match="四角"):
        ocr_point(*args, policy=policy())
    with pytest.raises(ValueError, match="安全区"):
        ocr_point(*args, camera_polygon=[[10, 25], [110, 25], [110, 35], [10, 35]],
                  policy=policy())


@pytest.mark.parametrize("version", [2, 3])
def test_policy_mutations_change_motion_identity_without_changing_no_policy_identity(version):
    calibration = bundle()
    calibration["schemaVersion"] = version
    original = cb.motion_calibration_fingerprint(calibration)
    calibration["xy"]["clickPointPolicy"] = policy(top=100)
    enabled = cb.motion_calibration_fingerprint(calibration)
    calibration["xy"]["clickPointPolicy"]["preferredInsetsPx"]["top"] = 110
    assert cb.motion_calibration_fingerprint(calibration) not in (original, enabled)
    del calibration["xy"]["clickPointPolicy"]
    assert cb.motion_calibration_fingerprint(calibration) == original


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_real_shared_driver_moves_single_aim_inside_preference(target_rig, attempt):
    rig = target_rig
    enable(rig, policy(top=36))
    rig.source.image = rig.frame("chat_ime_hidden")
    result = asyncio.run(rig.arm.click({"target_id": "back_button", "click_attempt": attempt}))
    choice = result["click_point"]
    assert choice["originalCenter"] == pytest.approx([32, 32])
    assert choice["aim"] == choice["screen"]
    assert 36 < choice["screen"][1] < 48
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_real_ocr_driver_preserves_original_credentials_while_selecting_aim(target_rig, attempt):
    rig = target_rig
    enable(rig, policy(top=210))
    target = locate(rig)
    target["click_attempt"] = attempt
    original = deepcopy(target)
    result = asyncio.run(rig.arm.click(target))
    assert target == original
    choice = result["click_point"]
    assert choice["originalCenter"] == pytest.approx([48, 208])
    assert 210 <= choice["aim"][1] < 216
    assert choice["selectionOffset"] == pytest.approx(np.array(choice["aim"]) - [48, 208])
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


@pytest.mark.parametrize("kind", ["shared", "ocr"])
def test_driver_disjoint_preference_still_uses_real_target_and_one_contact(target_rig, kind):
    rig = target_rig
    enable(rig, policy(top=220))
    if kind == "shared":
        rig.source.image = rig.frame("chat_ime_hidden")
        target = {"target_id": "back_button"}
    else:
        target = locate(rig)
    result = asyncio.run(rig.arm.click(target))
    choice = result["click_point"]
    assert choice["safety"]["preferenceSatisfied"] is False
    assert choice["screen"][1] < 220
    if kind == "shared":
        assert 16 < choice["screen"][0] < 48 and 32 < choice["screen"][1] < 48
    else:
        assert 28 < choice["screen"][0] < 68 and 208 < choice["screen"][1] < 216
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


def test_disjoint_preference_keeps_send_single_contact_without_alternatives(target_rig, monkeypatch):
    rig = target_rig
    enable(rig, policy(bottom=630))
    rig.screens["chat_ime_text"][532:548, 40:80] = 30
    rig.source.image = rig.frame("chat_ime_text")
    monkeypatch.setattr(rig.arm, "_input_switch_allowed", lambda _: True)
    result = asyncio.run(rig.arm.click({"target_id": "send_button"}))
    assert result["click_point"]["safety"]["preferenceSatisfied"] is False
    assert result["click_retry_supported"] is False
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1
    before = motion(rig)
    with pytest.raises(DriverError, match="禁止替代"):
        asyncio.run(rig.arm.click({"target_id": "send_button", "click_attempt": 1}))
    assert motion(rig) == before


def test_disjoint_preference_cannot_rescue_ocr_smaller_than_calibration_error(target_rig):
    from adapters.vision.ocr import OcrWord
    from tests.test_layout_text_location import TextEngine

    rig = target_rig
    enable(rig, policy(top=220))
    rig.camera.ocr_engine = TextEngine(words=[OcrWord("Ann", 12, 20, 8, 6, .99)])
    located = asyncio.run(rig.camera.locate_text("Ann", region="account", exact=True))
    with pytest.raises(DriverError, match="安全区") as caught:
        asyncio.run(rig.arm.click({"target_id": "conversation_item", "target_ref": {
            "source": "camera_ocr", **located}}))
    assert caught.value.motion_not_started is True
    assert motion(rig) == []


@pytest.mark.parametrize("kind", ["shared", "ocr"])
def test_disjoint_preference_still_checks_final_position_against_real_target(
        target_rig, monkeypatch, kind):
    from tests.test_region_qualified_clicks import region_driver

    rig = target_rig
    region_driver(rig.arm, monkeypatch)
    load = cb.load_bundle

    def region_with_preference(*args, **kwargs):
        calibration = load(*args, **kwargs)
        calibration["xy"]["clickPointPolicy"] = policy(top=220)
        return calibration

    monkeypatch.setattr(cb, "load_bundle", region_with_preference)
    if kind == "shared":
        rig.source.image = rig.frame("chat_ime_hidden")
        target = {"target_id": "back_button"}
    else:
        target = locate(rig)

    def after_travel(line):
        if line.startswith("XY "):
            rig.transport.position["x"] += 20

    rig.transport.after_send = after_travel
    with pytest.raises(DriverError, match="安全范围|安全区"):
        asyncio.run(rig.arm.click(target))
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


def test_existing_preference_intersection_remains_in_final_position_guard(target_rig, monkeypatch):
    rig = target_rig
    # 明确跨过保存时间戳边界，避免同秒运行掩盖内存/落盘快照不一致。
    saved_at = "2026-10-19T00:00:01"
    monkeypatch.setattr(cb, "now_iso", lambda: saved_at)
    enable(rig, policy(top=36))
    assert rig.bundle["updatedAt"] == saved_at
    rig.source.image = rig.frame("chat_ime_hidden")
    frame = asyncio.run(rig.camera._selected_layout_frame())
    # (32, 32) is in the real safe box but below the existing preference intersection.
    with pytest.raises(ValueError, match="内部偏好区"):
        frame.verify_target_point("back_button", {"robotX": -6.8, "robotY": -21}, rig.bundle)


@pytest.mark.parametrize("value", [None, policy(top=-1), policy(top=640)])
def test_real_driver_rejects_invalid_enabled_policy_before_motion(target_rig, value):
    rig = target_rig
    enable(rig, value)
    rig.source.image = rig.frame("chat_ime_hidden")
    with pytest.raises(DriverError) as caught:
        asyncio.run(rig.arm.click({"target_id": "back_button"}))
    assert caught.value.motion_not_started is True
    assert motion(rig) == []


@pytest.mark.parametrize("kind", ["shared", "ocr"])
def test_changing_policy_after_planning_revokes_old_target_before_tap(target_rig, kind):
    rig = target_rig
    enable(rig, policy())
    if kind == "shared":
        rig.source.image = rig.frame("chat_ime_hidden")
        target = {"target_id": "back_button"}
    else:
        target = locate(rig)

    def after(line):
        if line.startswith("XY "):
            enable(rig, policy(top=1))

    rig.transport.after_send = after
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


def test_policy_changes_revoke_old_layout_and_ocr_proof(target_rig):
    rig = target_rig
    enable(rig, policy())
    frame = asyncio.run(rig.camera._selected_layout_frame())
    first_binding = frame.profile["binding"]
    target = locate(rig)
    enable(rig, policy(top=1))
    with pytest.raises(ValueError, match="校准"):
        frame.ensure_current(rig.bundle)
    with pytest.raises(DriverError, match="校准"):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []
    new_frame = asyncio.run(rig.camera._selected_layout_frame())
    assert new_frame.profile["binding"] != first_binding
