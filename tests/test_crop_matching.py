"""Offline regressions for ROI detection and screen-domain anchor crops."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from runtime.layout_matching import compute_page_check, match_anchor
from runtime.layout_region import _detect_in_rect


def _scene(second=True):
    image = np.full((600, 900, 3), 18, np.uint8)
    cv2.rectangle(image, (100, 100), (340, 480), (200, 200, 200), -1)
    if second:
        cv2.rectangle(image, (580, 90), (820, 470), (230, 230, 230), -1)
    return image


def test_roi_ignores_outside_phone_and_returns_source_coordinates():
    result = _detect_in_rect(
        _scene(), {"left": 60, "top": 60, "right": 390, "bottom": 530})
    assert result["status"] == "verified", result
    points = np.array([[p["x"], p["y"]] for p in result["detected_quad"]])
    np.testing.assert_allclose(
        points, [[100, 100], [340, 100], [340, 480], [100, 480]], atol=2)


def test_roi_containing_two_phones_is_not_verified():
    result = _detect_in_rect(
        _scene(), {"left": 40, "top": 40, "right": 860, "bottom": 550})
    assert result["status"] == "needs_review"
    assert result["detected_quad"] is None


def test_large_roi_locks_onto_global_fit_result():
    """多候选不再否决（业主 2026-10-16 拍板：人工框直接采信）：大框内大小
    两块屏，判定=全裁图严格拟合的裁决结果（与下游校准/姿态门同一检测器），
    不再用低阶轮廓启发式做更严的第二意见。"""
    frame = np.full((1000, 1600, 3), 18, np.uint8)
    cv2.rectangle(frame, (100, 100), (550, 900), (200, 200, 200), -1)
    cv2.rectangle(frame, (1000, 300), (1100, 480), (230, 230, 230), -1)
    result = _detect_in_rect(
        frame, {"left": 0, "top": 0, "right": 1600, "bottom": 1000})
    assert result["status"] == "verified", result
    points = np.array([[p["x"], p["y"]] for p in result["detected_quad"]])
    np.testing.assert_allclose(
        points, [[100, 100], [550, 100], [550, 900], [100, 900]], atol=2)


@pytest.mark.parametrize("rect", [
    {"left": 110, "top": 60, "right": 390, "bottom": 530},
    {"left": 60, "top": 60, "right": 330, "bottom": 530},
    {"left": 98, "top": 98, "right": 342, "bottom": 482},
    {"left": 0, "top": 0, "right": 50, "bottom": 50},
    {"left": 400, "top": 10, "right": 400, "bottom": 20},
    {"left": -20, "top": 50, "right": 390, "bottom": 530},
    {"left": 0, "top": 0, "right": 950, "bottom": 600},
])
def test_roi_truncation_empty_or_invalid_never_verified(rect):
    result = _detect_in_rect(_scene(False), rect)
    assert result["status"] == "needs_review"
    assert result["detected_quad"] is None


def _tile(seed, size=32):
    cells = np.random.default_rng(seed).integers(
        35, 230, (8, 8, 3), dtype=np.uint8)
    return cv2.resize(cells, (size, size), interpolation=cv2.INTER_NEAREST)


def _png(image):
    ok, data = cv2.imencode(".png", image)
    assert ok
    return data.tobytes()


def _page_and_frame(width, height):
    frame = np.full((height, width, 3), 150, np.uint8)
    anchors, assets = [], {}
    for name, seed, x, y in (
        ("wechat_chats_tab", 42, .2, .15),
        ("list_marker", 66, .6, .15),
    ):
        reference = {"x": x, "y": y, "width": .1, "height": .05}
        anchors.append({
            "id": name, "asset_id": name, "required": True,
            "reference_rect": reference,
            "search_region": {"x": x - .05, "y": y - .04,
                              "width": .2, "height": .13},
        })
        asset = _tile(seed)
        assets[name] = _png(asset)
        w, h = round(.1 * width), round(.05 * height)
        tile = cv2.resize(asset, (w, h), interpolation=(
            cv2.INTER_AREA if w < 32 and h < 32 else cv2.INTER_LINEAR))
        px, py = round(x * width), round(y * height)
        frame[py:py + h, px:px + w] = tile
    page = {
        "state": "wechat_list", "anchors": anchors, "targets": [],
        "regions": [
            {"id": "unread", "anchor_id": "wechat_chats_tab",
             "relative_rect": {"dx": 0, "dy": .15, "width": .1, "height": .04}},
            {"id": "content", "anchor_id": "wechat_chats_tab",
             "relative_rect": {"dx": 0, "dy": .25, "width": .6, "height": .4}},
        ],
    }
    return page, frame, assets


@pytest.mark.parametrize("size", [(320, 640), (480, 960), (240, 480), (360, 800)])
def test_screen_resolution_and_nonzero_search_crop_keep_normalized_coordinates(size):
    page, frame, assets = _page_and_frame(*size)
    check = compute_page_check(page, _png(frame), assets, ["first", "second"])
    assert check["status"] == "passed", check
    for anchor, result in zip(page["anchors"], check["anchor_results"]):
        assert result["status"] == "passed"
        for key, value in anchor["reference_rect"].items():
            assert result["matched_rect"][key] == pytest.approx(value, abs=.003)
    content = next(i for i in check["item_results"] if i["id"] == "content")
    assert content["resolved_rect"]["x"] == pytest.approx(.2, abs=.003)
    assert content["resolved_rect"]["y"] == pytest.approx(.4, abs=.003)


def test_scaled_duplicate_still_rejected_as_ambiguous():
    page, frame, assets = _page_and_frame(480, 960)
    anchor = page["anchors"][0]
    anchor["search_region"] = {"x": 0, "y": 0, "width": .5, "height": .3}
    copy = frame[144:192, 96:144].copy()
    frame[30:78, 10:58] = copy
    check = compute_page_check(page, _png(frame), assets, ["first", "second"])
    assert check["status"] == "failed"
    assert check["anchor_results"][0]["reason_code"] == "anchor_ambiguous"


def test_feature_outside_search_crop_cannot_satisfy_anchor():
    frame = np.full((100, 200, 3), 150, np.uint8)
    tile = _tile(1, 20)
    frame[40:60, 140:160] = tile
    result = match_anchor(frame, tile, {"x": 0, "y": 0, "width": .5, "height": 1})
    assert result["matched_rect"] is None


def test_constant_tight_crop_cannot_report_perfect_match():
    frame = np.full((100, 100, 3), 150, np.uint8)
    tile = np.full((20, 20, 3), 150, np.uint8)
    result = match_anchor(frame, tile, {"x": .3, "y": .4, "width": .2, "height": .2})
    assert result == {"scores": [], "matched_rect": None}


@pytest.mark.parametrize("rect", [
    {"x": 1, "y": 0, "width": .1, "height": .1},
    {"x": -.1, "y": 0, "width": .3, "height": .3},
    {"x": .8, "y": 0, "width": .4, "height": .3},
    {"x": 0, "y": 0, "width": 0, "height": .1},
    {"x": float("nan"), "y": 0, "width": .1, "height": .1},
])
def test_invalid_search_region_fails_without_clamping_or_exception(rect):
    result = match_anchor(np.full((100, 100, 3), 150, np.uint8), _tile(1), rect)
    assert result == {"scores": [], "matched_rect": None}


@pytest.fixture
def region_service(tmp_path):
    from types import SimpleNamespace
    from runtime.evidence import EvidenceStore
    from runtime.layout_region import LayoutRegionService, LayoutRegionStore
    from tests.layout_d2_helpers import FrameBridgeFake

    rows = {}
    memory_store = SimpleNamespace(
        save_evidence=lambda record: rows.update({record["evidence_id"]: record}),
        get_evidence=rows.get,
    )
    evidence = EvidenceStore(memory_store, tmp_path / "evidence")
    bridge = FrameBridgeFake(
        SimpleNamespace(evidence=evidence), width=900, height=600)
    store = LayoutRegionStore(tmp_path / "region.db")
    svc = LayoutRegionService(store)
    svc.configure(
        evidence_get=evidence.get, frame_reader=evidence.read_source_frame_bytes,
        latest_frames=lambda device, count: bridge.archived[-count:][::-1],
        arm_binding=lambda device: "arm-bind-01", device_idle=lambda device: True,
    )
    try:
        yield svc, bridge
    finally:
        store.close()


def _preview_request(evidence_id):
    return {"source_evidence_id": evidence_id,
            "rect": {"x": 60 / 900, "y": .1, "width": 330 / 900,
                     "height": 470 / 600}}


def test_region_preview_metadata_all_describe_detection_frame(region_service):
    svc, bridge = region_service
    old = bridge.push(_scene())
    current = bridge.push(_scene())
    view = svc.preview("arm-01", _preview_request(old["evidence_id"]), "operator")
    assert view["status"] == "verified", view
    binding = view["source_binding"]
    assert binding["evidence_id"] == current["evidence_id"]
    assert binding["frame_seq"] == current["source_frame_seq"]
    assert binding["source_captured_at"] == current["captured_at"]
    assert binding["server_received_at"] == current["received_at"]


def test_region_preview_rejects_reencoded_or_replaced_source(region_service):
    from runtime.layout_region import RegionError

    svc, bridge = region_service
    old = bridge.push(_scene())
    bridge.push(_scene())
    svc._env["frame_reader"] = lambda evidence_id: _png(_scene())
    with pytest.raises(RegionError) as error:
        svc.preview("arm-01", _preview_request(old["evidence_id"]), "operator")
    assert error.value.code == "archive_invalid"


def test_publish_trusts_operator_rect_without_detection_gate(region_service):
    """检测门已撤（业主 2026-10-16）：发布不再被检测状态拦截——即使保存时
    当前帧里手机已消失也照常发布。真正的门在使用时：下游校准/姿态门用
    detect_screen_quad 实际定位，框里没手机会在使用时大声失败。"""
    import uuid

    svc, bridge = region_service
    old = bridge.push(_scene())
    bridge.push(_scene())
    view = svc.preview("arm-01", _preview_request(old["evidence_id"]), "operator")
    assert view["status"] == "verified"
    changed = _scene()
    changed[60:530, 60:390] = 18
    bridge.push(changed)
    result = svc.save("arm-01", {
        "client_request_id": str(uuid.uuid4()), "expected_revision": 0,
        "publish": True, "preview_id": view["preview_id"],
        "region_digest": view["region_digest"],
    }, "operator")
    assert result["status"] == "verified", result
    pointer = svc._store.get_pointer("arm-01")
    assert pointer is not None and pointer["selected_version"] >= 1
