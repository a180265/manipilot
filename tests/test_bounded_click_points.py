"""Bounded click alternatives through real layout, OCR and daemon boundaries."""
from __future__ import annotations

import asyncio
import copy
import time

import numpy as np
import cv2
import pytest

from adapters.vision.ocr import OcrWord
from drivers.arm import DriverError
from drivers import calibration_bundle as cb, screen_pose
from drivers.click_points import ocr_point
from runtime.layout_geometry import mapping_error_bound
from runtime.layout_matching import compute_page_check
from tests.test_layout_observation import offline_only  # noqa: F401
from tests.test_layout_target_driver import motion, png, target_rig  # noqa: F401
from tests.test_layout_text_location import TextEngine
from tests.test_pre_enable_s3_driver import FAULTS, locate


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_shared_target_uses_bounded_alternative_and_one_contact(target_rig, attempt):
    rig = target_rig
    rig.source.image = rig.frame("chat_ime_hidden")
    frame = asyncio.run(rig.camera._selected_layout_frame())
    centre = frame.target("back_button")
    chosen = frame.target("back_button", click_attempt=attempt)
    ref = copy.deepcopy(frame.selected.references)
    target = {"target_id": "back_button", "target_ref": ref, "click_attempt": attempt}
    before = copy.deepcopy(target)
    result = asyncio.run(rig.arm.click(target))
    assert target == before
    assert result["click_attempt"] == attempt
    assert result["click_retry_supported"] is True
    assert result["click_point"] == chosen["click_point"]
    assert result["click_point"]["screen"][1] == pytest.approx(32)
    displacement = result["click_point"]["screen"][0] - 32
    if attempt == 0:
        assert chosen["nx"] == centre["nx"]
        assert chosen["ny"] == centre["ny"]
        assert displacement == pytest.approx(0)
    elif attempt == 1:
        assert -12 <= displacement <= -2
    else:
        assert 2 <= displacement <= 12
    frame.verify_target_point("back_button", {
        "robotX": result["resolved_robot_xy"][0],
        "robotY": result["resolved_robot_xy"][1]}, rig.bundle)
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_trusted_ocr_uses_original_bbox_and_preserves_location_credentials(target_rig, attempt):
    rig = target_rig
    target = locate(rig)
    target["click_attempt"] = attempt
    original = copy.deepcopy(target)
    assert original["target_ref"]["cameraPolygon"] == [[128, 250], [168, 250], [168, 266], [128, 266]]
    result = asyncio.run(rig.arm.click(target))
    assert target == original
    assert result["click_retry_supported"] is True
    assert result["click_attempt"] == attempt
    point = result["click_point"]
    assert point["screen"][1] == pytest.approx(208)
    assert 28 < point["screen"][0] < 68
    assert 250 < point["camera"][1] < 266
    if attempt == 0:
        assert point["camera"] == pytest.approx([148, 258])
        assert result["resolved_robot_xy"] == pytest.approx([-5.2, 1])
    elif attempt == 1:
        assert -12 <= point["screen"][0] - 48 <= -2
    else:
        assert 2 <= point["screen"][0] - 48 <= 12
    assert "Ann" not in str(point)
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


@pytest.mark.parametrize("target_id", ["send_button", "ime_candidate_bar", "ime_delete_key",
                                     "mark_read_row", "wechat_chats_tab", "gesture_point"])
def test_forbidden_target_cannot_request_alternative(target_rig, target_id):
    rig = target_rig
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({"target_id": target_id, "click_attempt": 1}))
    assert motion(rig) == []


@pytest.mark.parametrize("attempt", [-1, 3, True, "1", 1.0, None])
def test_invalid_attempt_is_rejected_before_motion(target_rig, attempt):
    rig = target_rig
    rig.source.image = rig.frame("chat_ime_hidden")
    with pytest.raises(DriverError, match="click_attempt"):
        asyncio.run(rig.arm.click({"target_id": "back_button", "click_attempt": attempt}))
    assert motion(rig) == []


def test_tiny_shared_safe_rect_preserves_centre_but_refuses_alternatives(target_rig):
    rig = target_rig
    template = copy.deepcopy(rig.parts["ops_svc"].resolve_template("offline-template", 2))
    template["version"] = 3
    page = template["pages"][1]
    page["targets"][0]["relative_rect"].update(width=7 / 320, height=7 / 640)
    rig.adaptation["page_checks"][1] = compute_page_check(
        page, png(rig.screens["chat_ime_hidden"]),
        {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]},
        ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle))
    rig.template_version(template)
    rig.source.image = rig.frame("chat_ime_hidden")
    result = asyncio.run(rig.arm.click({"target_id": "back_button"}))
    assert result["click_retry_supported"] is False
    assert result["click_point"]["screen"] == pytest.approx([19.5, 19.5])
    before = motion(rig)
    for attempt in (1, 2):
        with pytest.raises(DriverError, match="安全区"):
            asyncio.run(rig.arm.click({"target_id": "back_button", "click_attempt": attempt}))
        assert motion(rig) == before


def test_tiny_original_ocr_bbox_allows_only_original_centre(target_rig):
    rig = target_rig
    rig.camera.ocr_engine = TextEngine(words=[OcrWord("Ann", 12, 20, 8, 6, .99)])
    located = asyncio.run(rig.camera.locate_text("Ann", region="account", exact=True))
    target = {"target_id": "conversation_item", "target_ref": {"source": "camera_ocr", **located}}
    original = copy.deepcopy(target)
    result = asyncio.run(rig.arm.click(target))
    assert result["click_retry_supported"] is False
    assert result["click_point"]["camera"] == pytest.approx([132, 253])
    before = motion(rig)
    for attempt in (1, 2):
        with pytest.raises(DriverError, match="不支持替代"):
            asyncio.run(rig.arm.click({**target, "click_attempt": attempt}))
        assert motion(rig) == before
    assert target == original


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("fault", ["adaptation", "region", "binding", "text"])
def test_alternative_still_rechecks_original_target_after_travel(target_rig, attempt, fault):
    rig = target_rig
    target = locate(rig)
    target["click_attempt"] = attempt
    original = copy.deepcopy(target)

    def after(line):
        if line.startswith("XY "):
            if fault == "text":
                rig.camera.ocr_engine = TextEngine(words=[])
            else:
                FAULTS[fault](rig)

    rig.transport.after_send = after
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert target == original
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


def test_alternative_cannot_refresh_an_expired_original_ocr_location(target_rig):
    rig = target_rig
    target = locate(rig)
    time.sleep(2.1)
    with pytest.raises(DriverError, match="过期"):
        asyncio.run(rig.arm.click({**target, "click_attempt": 1}))
    assert motion(rig) == []


def test_alternative_keeps_final_transport_gate(target_rig):
    rig = target_rig
    target = locate(rig)

    def before(line):
        if line.startswith("TAP "):
            FAULTS["binding"](rig)

    rig.transport.before_send = before
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({**target, "click_attempt": 2}))
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


@pytest.mark.parametrize("error", [None, -1, float("nan"), float("inf"), True])
def test_unknown_error_has_no_alternative_even_with_large_bbox(error):
    args = ("conversation_item", [10, 10, 110, 40], [60, 25], np.eye(3), error, [320, 640])
    camera, screen, supported, _ = ocr_point(*args)
    assert not supported
    assert camera.tolist() == screen.tolist() == [60, 25]
    for attempt in (1, 2):
        with pytest.raises(ValueError, match="校准最大误差未知"):
            ocr_point(*args, attempt=attempt)


@pytest.mark.parametrize("attempt", [1, 2])
def test_projective_ocr_alternatives_stay_inside_actual_inset_edges(attempt):
    matrix = np.array([[1., .4, 100], [.2, 1., 100], [.002, .001, 1.]])
    point, screen, supported, safety = ocr_point(
        "conversation_item", [0, 0, 100, 30], [50, 15], matrix, 1, [320, 640], attempt)
    assert supported
    assert 0 < point[0] < 100 and 0 < point[1] < 30
    polygon = safety["screen_polygon"]
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        distance = ((x2 - x1) * (screen[1] - y1)
                    - (y2 - y1) * (screen[0] - x1)) / np.hypot(x2 - x1, y2 - y1)
        assert distance > 3
    centre = np.array([156 / 1.115, 125 / 1.115])
    assert 2 <= np.linalg.norm(screen - centre) <= 12.00000001


@pytest.mark.parametrize("old_version", ["1.0.0", "1.1.0"])
def test_click_schema_upgrade_is_persisted_for_existing_installations(tmp_path, old_version):
    from runtime.capabilities import CapabilityRegistry
    from runtime.store import Store

    store = Store(tmp_path / "click-capabilities.sqlite3")
    try:
        store.save_capability({"capability_id": "arm.click", "source": "platform", "version": old_version})
        registry = CapabilityRegistry(store)
        registry.bootstrap()
        registry.bootstrap()
        saved = store.get_capability("arm.click")
        assert saved["version"] == "1.2.0"
        assert saved["input_schema"]["properties"]["tapDwellMs"]["minimum"] == 0
        assert saved["input_schema"]["properties"]["tapDwellMs"]["maximum"] == 5000
        schema = saved["input_schema"]["properties"]["click_attempt"]
        assert schema["enum"] == [0, 1, 2] and schema["default"] == 0
    finally:
        store._conn.close()


def test_alternative_is_only_a_single_click_option(target_rig):
    rig = target_rig
    rig.source.image = rig.frame("chat_ime_hidden")
    with pytest.raises(DriverError, match="arm.click"):
        asyncio.run(rig.arm.double_tap({"target_id": "back_button", "click_attempt": 1}))
    assert motion(rig) == []


@pytest.mark.parametrize("attempt", [1, 2])
def test_rotated_perspective_text_uses_original_four_corners_not_camera_aabb(target_rig, attempt):
    rig = target_rig
    camera_warp = np.array([[.985, -.17, 66], [.17, .985, -38], [.0001, 0, 1.]])
    # Include a real two-pixel screen border so rotation/interpolation does not
    # manufacture unsupported pixels at the rectification crop's outer edge.
    rig.source.image[48:692, 98:422] = cv2.copyMakeBorder(
        rig.screens["wechat_list"], 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=(240, 240, 240))
    rig.source.image = cv2.warpPerspective(rig.source.image, camera_warp, (1280, 720),
                                          borderValue=(20, 20, 20))
    old_h = np.asarray(rig.bundle["camera"]["cameraToScreen"]["homography"]).reshape(3, 3)
    rig.bundle["camera"]["cameraToScreen"]["homography"] = (old_h @ np.linalg.inv(camera_warp)).ravel().tolist()
    corners = np.array([[98, 48], [421, 48], [421, 691], [98, 691]])
    corners = np.column_stack((corners, np.ones(4))) @ camera_warp.T
    corners = (corners[:, :2] / corners[:, 2, None]).tolist()
    rig.bundle["screen"]["edgeModel"]["corners"] = corners
    cb.save_bundle(rig.bundle, rig.data)
    rig.pose = screen_pose.fit_pose(corners, corners)
    rig.camera.ocr_engine = TextEngine(words=[OcrWord("Ann", 12, 20, 80, 8, .99)])
    located = asyncio.run(rig.camera.locate_text("Ann", region="account", exact=True))
    assert located.get("cameraPolygon") is not None
    point = asyncio.run(rig.arm.click({"target_id": "conversation_item", "click_attempt": attempt,
                                      "target_ref": {"source": "camera_ocr", **located}}))["click_point"]
    # The original rectified word occupies x=[28,108], y=[200,208]. The camera
    # axis-aligned bounding box is much taller and cannot authorize a wider target.
    assert 28 < point["screen"][0] < 108
    assert point["screen"][1] == pytest.approx(204, abs=.1)
    assert np.asarray(point["safety"]["screen_polygon"]) == pytest.approx(
        np.array([[28, 200], [108, 200], [108, 208], [28, 208]]), abs=.1)


def test_original_polygon_cannot_be_replaced_or_removed(target_rig):
    rig = target_rig
    target = locate(rig)
    assert "cameraPolygon" in target["target_ref"]
    for mode in ("changed", "removed"):
        modified = copy.deepcopy(target)
        if mode == "changed":
            modified["target_ref"]["cameraPolygon"][0][0] += 5
        else:
            del modified["target_ref"]["cameraPolygon"]
        with pytest.raises(DriverError, match="改写"):
            asyncio.run(rig.arm.click({**modified, "click_attempt": 1}))
        assert motion(rig) == []


def test_legacy_shared_location_never_uses_expanded_camera_bbox_for_alternatives():
    args = ("conversation_item", [10, 10, 110, 50], [60, 30], np.eye(3), 1, [320, 640])
    camera, _, supported, _ = ocr_point(*args, require_polygon=True)
    assert camera.tolist() == [60, 30] and not supported
    for attempt in (1, 2):
        with pytest.raises(ValueError, match="缺少原始四角"):
            ocr_point(*args, attempt=attempt, require_polygon=True)


@pytest.mark.parametrize("fault", [None, [[1, 2]], [[float("nan"), 0]] * 4])
def test_original_polygon_rejects_malformed_credentials_before_motion(target_rig, fault):
    target = locate(target_rig)
    target["target_ref"]["cameraPolygon"] = fault
    with pytest.raises(DriverError, match="改写"):
        asyncio.run(target_rig.arm.click({**target, "click_attempt": 1}))
    assert motion(target_rig) == []


def test_polygon_cannot_be_injected_into_legacy_camera_credential(target_rig):
    rig = target_rig
    rig.camera.attach_layout_provider(None)

    async def no_layout():
        return None

    rig.arm.attach_layout_provider(no_layout)
    rig.camera.ocr_engine = TextEngine(words=[OcrWord("Ann", 128, 250, 40, 16, .99)])
    located = asyncio.run(rig.camera.locate_text("Ann", region="full", exact=True))
    assert located["found"]
    assert "cameraPolygon" not in located
    target = {"target_id": "conversation_item", "target_ref": {
        "source": "camera_ocr", **located,
        "cameraPolygon": [[1, 1], [100, 1], [100, 100], [1, 100]]}}
    with pytest.raises(DriverError, match="四角被改写"):
        asyncio.run(rig.arm.click({**target, "click_attempt": 1}))
    assert motion(rig) == []
