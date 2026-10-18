"""Optional reviewed text pages preserve four-page behavior and device contact settings."""
from __future__ import annotations

import asyncio
import copy
import hashlib

import cv2
import numpy as np
import pytest

from adapters.vision.ocr import OcrResult, OcrWord
from runtime.layout_geometry import mapping_error_bound
from runtime.layout_matching import compute_page_check
from runtime.layout_pages import LEGACY_STATES, page_states
from tests.test_layout_observation import layout_rig, offline_only, png  # noqa: F401
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401


class TextEngine:
    def recognize(self, image, *, region):
        from drivers.screen_proof import decode
        height, width = decode(image).shape[:2]
        return OcrResult(text="Contact", words=[OcrWord("Contact", 10, 5, 60, 20, .99)],
                         confidence=.99, truncated=False, width=width, height=height,
                         engine="offline", engine_version="1")


def add_text_page(rig):
    template = copy.deepcopy(rig.template)
    template["version"] = 2
    page = copy.deepcopy(template["pages"][2])
    page.update(state="app_search", observation_kind="text_regions.v1",
                ui_state="wechat_search", ui_state_display="微信搜索页")
    screen = np.full((640, 320, 3), 240, np.uint8)
    for index, anchor in enumerate(page["anchors"]):
        tile = cv2.resize(np.random.default_rng(100 + index).integers(
            25, 220, (8, 8, 3), dtype=np.uint8), (32, 32), interpolation=cv2.INTER_NEAREST)
        left = 16 + index * 40
        screen[16:48, left:left + 32] = tile
        asset_id = "search-" + anchor["id"]
        anchor["asset_id"] = asset_id
        data = png(tile)
        rig.assets[asset_id] = data
        rig.parts["template_store"].save_asset("offline-template", asset_id, data)
        template["assets"].append({"id": asset_id, "relative_path": f"assets/{asset_id}.png",
            "sha256": hashlib.sha256(data).hexdigest(), "media_type": "image/png",
            "width": 32, "height": 32})
    page["regions"] = [region for region in page["regions"]
                       if region["purpose"] in {"input", "content"}]
    template["pages"].append(page)
    rig.screens[page["state"]] = screen
    rig.adaptation["page_checks"].append(compute_page_check(
        page, png(screen), {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]},
        ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle)))
    rig.adaptation["color_samples"].append({"state": "app_search", "foreground_rgb": [1, 2, 3],
        "background_rgb": [4, 5, 6], "empty_input_asset_ref": None,
        "evidence_ids": ["ev-1", "ev-2"]})
    rig.template_version(template)
    rig.source.image = rig.frame("app_search")
    rig.camera.ocr_engine = TextEngine()
    return template


def test_optional_page_has_explicit_ui_and_same_frame_reviewed_text_geometry(layout_rig):
    rig = layout_rig
    template = add_text_page(rig)
    assert page_states(template) == (*LEGACY_STATES, "app_search")
    result = asyncio.run(rig.camera.observe_regions())
    assert result["ui"]["ui_state"] == "wechat_search"
    assert result["observation_kind"] == "text_regions.v1"
    assert result["text_region_coordinate_version"] == "calibrated_screen.v1"
    assert result["text_region_frame_size"] == {"width": 320, "height": 640}
    assert set(result["text_regions"]) == {"input", "content"}
    content = result["text_regions"]["content"]
    assert content["screen_bbox"] == pytest.approx([16, 180, 304, 440])
    assert content["bbox"] == pytest.approx([116, 230, 404, 490])
    assert content["truncated"] is False
    assert content["words"][0]["x"] == pytest.approx(26)
    assert content["words"][0]["camera_bbox"] == pytest.approx([126, 235, 186, 255])
    assert result["input_empty"] is False
    assert result["components"] == []


def test_optional_page_locate_uses_reviewed_content_and_immutable_location(layout_rig):
    rig = layout_rig
    add_text_page(rig)
    result = asyncio.run(rig.camera.locate_text(text="Contact", region="messages", exact=True))
    assert result["found"] is True
    assert result["locationId"]
    assert len(result["cameraPolygon"]) == 4


@pytest.mark.parametrize("fault", ["missing_semantics", "legacy_override", "duplicate", "missing_prefix"])
def test_optional_page_requires_opt_in_unique_states_and_original_prefix(layout_rig, fault):
    template = add_text_page(layout_rig)
    if fault == "missing_semantics":
        del template["pages"][-1]["observation_kind"]
    elif fault == "legacy_override":
        template["pages"][0]["ui_state"] = "wechat_search"
    elif fault == "duplicate":
        template["pages"].append(copy.deepcopy(template["pages"][-1]))
    else:
        template["pages"].pop(0)
    with pytest.raises(ValueError):
        page_states(template)


def test_device_target_depth_is_used_without_changing_other_target_defaults(target_rig):
    rig = target_rig
    baseline = asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    baseline_tap = [cmd for cmd in motion(rig) if cmd.startswith("TAP ")][-1]
    record = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    record["version"] = 3
    record["local_overrides"] = [{"state": "wechat_list", "kind": "target",
        "item_id": "wechat_chats_tab", "anchor_id": "wechat_chats_tab",
        "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05},
        "tapDepth": 10, "source_evidence_ids": ["ev-1", "ev-2"],
        "reason": "reviewed phone edge contact"}]
    rig.select(record)
    result = asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert baseline["target_source"] == result["target_source"] == "shared_layout"
    assert [cmd for cmd in motion(rig) if cmd.startswith("TAP ")][-1].split()[1] == "10.000"
    rig.source.image = rig.frame("chat_ime_hidden")
    asyncio.run(rig.arm.click({"target_id": "back_button"}))
    assert [cmd for cmd in motion(rig) if cmd.startswith("TAP ")][-1] == baseline_tap


def test_optional_declared_page_is_required_before_adaptation_publish(tmp_path, monkeypatch):
    import uuid
    from runtime.layout_adaptation import AdaptationError
    from tests.layout_r2_helpers import (LayoutBundle, FakeCaptureProvider, FakeCalibrationProvider,
        FakeClock, DEFAULT_BINDING, DEFAULT_GEOMETRY_ID, build_page_frames, build_template_assets)
    from tests.test_layout_adaptation import _start, _fill, _write
    frames = build_page_frames(build_template_assets())
    frames["app_search"] = frames["chat_ime_empty"].copy()
    bundle = LayoutBundle(tmp_path / "rt", clock=FakeClock(), capture_provider=FakeCaptureProvider(
        frames, binding=DEFAULT_BINDING, source_geometry_id=DEFAULT_GEOMETRY_ID),
        calibration_provider=FakeCalibrationProvider())
    try:
        svc = bundle.adaptation_svc
        template = copy.deepcopy(svc._resolve_template("tpl-1", 1))
        page = copy.deepcopy(template["pages"]["chat_ime_empty"])
        page.update(state="app_search", observation_kind="text_regions.v1",
                    ui_state="wechat_search", ui_state_display="微信搜索页")
        page["regions"] = [region for region in page["regions"]
                           if region["purpose"] in {"input", "content"}]
        template["pages"]["app_search"] = page
        monkeypatch.setattr(svc, "_resolve_template", lambda *args: template)
        sid = _start(svc)["session_id"]
        revision = _fill(svc, "arm-01", sid)
        with pytest.raises(AdaptationError, match="app_search"):
            svc.preview("arm-01", {"capture_session_id": sid})
        written = _write(svc, "arm-01", sid, revision, "app_search")
        assert written["check"]["status"] == "passed"
        preview = svc.preview("arm-01", {"capture_session_id": sid})
        result = svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
            "session_id": sid, "preview_id": preview["preview_id"],
            "principal_id": "operator", "expected_revision": 0,
            "accepted_items": preview["accepted_items"]})
        assert result["status"] == "verified"
        assert [check["state"] for check in result["record"]["page_checks"]] == [
            *LEGACY_STATES, "app_search"]
        assert len(result["record"]["color_samples"]) == 5
    finally:
        bundle.close()


def test_optional_page_and_contact_depth_schema_are_closed(layout_rig):
    from tests.layout_d2_helpers import load_contract
    validator = load_contract()
    page = add_text_page(layout_rig)["pages"][-1]
    validator.validate("layout", "page", page)
    invalid = copy.deepcopy(page)
    del invalid["ui_state"]
    with pytest.raises(validator.ContractError):
        validator.validate("layout", "page", invalid)
    override = {"state": "app_search", "kind": "target", "item_id": "search_button",
        "anchor_id": page["anchors"][0]["id"],
        "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05},
        "tapDepth": 10, "source_evidence_ids": ["ev-1", "ev-2"], "reason": "reviewed"}
    validator.validate("layout", "override", override)
    for invalid in ({**override, "tapDepth": 10.1}, {**override, "kind": "region"}):
        with pytest.raises(validator.ContractError):
            validator.validate("layout", "override", invalid)
