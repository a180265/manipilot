"""Partial publication preserves unrelated verified items; all IO is temporary."""
from __future__ import annotations

import asyncio
import copy

import pytest

from runtime.layout_geometry import mapping_error_bound
from runtime.layout_matching import compute_page_check
from tests.test_layout_observation import layout_rig, offline_only, png  # noqa: F401


def add_targets(rig):
    template = copy.deepcopy(rig.template)
    template["version"] = 2
    for page in template["pages"]:
        page["targets"] = [{
            "id": page["anchors"][0]["id"],
            "anchor_id": page["anchors"][0]["id"], "mode": "tap",
            "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05},
        }]
        if page["state"] == "wechat_list":
            page["targets"].append({
                "id": "too_small", "anchor_id": page["anchors"][0]["id"], "mode": "tap",
                "relative_rect": {"dx": .3, "dy": 0, "width": 4 / 320, "height": 4 / 640},
            })
    rig.adaptation["page_checks"] = [
        compute_page_check(page, png(rig.screens[page["state"]]),
            {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]},
            ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle))
        for page in template["pages"]]
    rig.template_version(template)


def test_failed_target_does_not_hide_verified_page_or_regions(layout_rig):
    rig = layout_rig
    add_targets(rig)
    rig.source.image = rig.frame("wechat_list")
    frame = asyncio.run(rig.camera._selected_layout_frame())
    assert frame.state == "wechat_list"
    assert "unread" in frame.profile["regions"]


def test_failed_keyboard_page_does_not_block_list_observation(layout_rig):
    rig = layout_rig
    changed = copy.deepcopy(rig.adaptation)
    changed["version"] = 2
    page = changed["page_checks"][-1]
    page.update(status="failed", reason_codes=["anchor_missing"])
    page["anchor_results"][0].update(
        status="failed", matched_rect=None, best_score=None, runner_up_score=None,
        candidate_count=0, localization_error_bound_px=None, reason_code="anchor_missing")
    for item in page["item_results"]:
        item.update(status="failed", resolved_rect=None, safe_rect=None, reason_code="anchor_missing")
    rig.select(changed)
    rig.source.image = rig.frame("wechat_list")
    assert asyncio.run(rig.camera.observe_regions())["ui"]["ui_state"] == "wechat_main"
    rig.source.image = rig.frame("chat_ime_text")
    with pytest.raises(ValueError):
        asyncio.run(rig.camera._selected_layout_frame())


def test_failed_region_is_not_reenabled_by_live_match(layout_rig):
    rig = layout_rig
    changed = copy.deepcopy(rig.adaptation)
    changed["version"] = 2
    page = changed["page_checks"][0]
    page.update(status="failed", reason_codes=["item_outside_screen"])
    item = next(r for r in page["item_results"] if r["id"] == "unread")
    item.update(status="failed", resolved_rect=None, reason_code="item_outside_screen")
    rig.select(changed)
    rig.source.image = rig.frame("wechat_list")
    frame = asyncio.run(rig.camera._selected_layout_frame())
    assert "unread" not in frame.profile["regions"]


def test_failed_target_with_executable_safety_remains_archive_corruption(layout_rig):
    rig = layout_rig
    add_targets(rig)
    selected = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    selected["version"] = 3
    item = next(r for r in selected["page_checks"][0]["item_results"] if r["id"] == "too_small")
    item["safe_rect"] = {"x": .1, "y": .1, "width": .1, "height": .1}
    rig.select(selected)
    rig.source.image = rig.frame("wechat_list")
    with pytest.raises(ValueError):
        asyncio.run(rig.camera._selected_layout_frame())
