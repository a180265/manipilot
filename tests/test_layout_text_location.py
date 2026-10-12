"""Selected-layout text location uses a real crop and calibrated camera output."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中")


from adapters.vision.ocr import OcrEngineError, OcrResult, OcrWord
from drivers import screen_proof
from tests.test_layout_observation import Engine, layout_rig, offline_only  # noqa: F401


class TextEngine(Engine):
    def __init__(self, *, words=None, fault=None, after=None):
        super().__init__()
        self.words = words if words is not None else [OcrWord("Ann", 12, 20, 40, 16, .99)]
        self.fault, self.after = fault, after
        self.images = []

    def recognize(self, image, *, region):
        patch = screen_proof.decode(image)
        height, width = patch.shape[:2]
        self.images.append(patch.copy())
        self.seen.append((region, width, height))
        if self.after:
            self.after()
        words = copy.deepcopy(self.words)
        if self.fault == "outside":
            words[0].x = -1
        if self.fault == "size":
            width += 1
        return OcrResult(
            text="\n".join(w.text for w in words), words=words, confidence=.99,
            truncated=self.fault == "truncated", engine="offline-fake",
            engine_version="1", width=width, height=height)


@pytest.mark.parametrize("region", ["messages", "account"])
@pytest.mark.parametrize("dx,dy", [(0, 0), (7, 5)])
def test_selected_text_uses_content_crop_and_returns_calibrated_camera_coordinates(
        layout_rig, region, dx, dy):
    rig = layout_rig
    image = rig.frame("wechat_list")
    rig.source.image = cv2.warpAffine(
        image, np.float32([[1, 0, dx], [0, 1, dy]]), (1280, 720),
        borderValue=(20, 20, 20))
    engine = TextEngine()
    rig.camera.ocr_engine = engine
    result = asyncio.run(rig.camera.locate_text("Ann", region=region, exact=True))
    assert engine.seen == [(region, 288, 260)]
    assert np.array_equal(engine.images[0], rig.screens["wechat_list"][180:440, 16:304])
    # Fixture's independent H is camera->screen = translation(-100, -50).
    assert result["bbox"] == pytest.approx([128, 250, 168, 266], abs=.1)
    assert (result["cameraX"], result["cameraY"]) == pytest.approx([148, 258], abs=.1)
    assert (result["frameW"], result["frameH"]) == (1280, 720)
    assert result["coordinateVersion"] == "calibrated_camera.v1"
    assert result["frameSeq"] and result["frameSha256"]
    assert result["template_ref"]["version"] == 1


def test_selected_text_roi_filters_in_camera_domain_before_ambiguity(layout_rig):
    rig = layout_rig
    rig.source.image = rig.frame("wechat_list")
    rig.camera.ocr_engine = TextEngine(words=[
        OcrWord("Ann", 12, 20, 40, 16, .99), OcrWord("Ann", 12, 120, 40, 16, .99)])
    roi = {"x1": 120, "y1": 245, "x2": 175, "y2": 270, "frameW": 1280, "frameH": 720}
    found = asyncio.run(rig.camera.locate_text("Ann", exact=True, roi=roi))
    assert found["found"] and found["cameraY"] == pytest.approx(258)
    with pytest.raises(OcrEngineError, match="多个位置"):
        asyncio.run(rig.camera.locate_text("Ann", exact=True))
    miss = asyncio.run(rig.camera.locate_text("Ann", exact=True, roi={**roi, "x2": 140}))
    assert miss["found"] is False and "cameraX" not in miss
    with pytest.raises(OcrEngineError):
        asyncio.run(rig.camera.locate_text("Ann", exact=True, roi={**roi, "frameW": 320}))


@pytest.mark.parametrize("fault", ["outside", "size", "truncated"])
def test_selected_text_rejects_invalid_crop_ocr(layout_rig, fault):
    rig = layout_rig
    rig.source.image = rig.frame("wechat_list")
    rig.camera.ocr_engine = TextEngine(fault=fault)
    with pytest.raises((OcrEngineError, ValueError)):
        asyncio.run(rig.camera.locate_text("Ann", exact=True))


def test_selected_text_never_restores_failed_content_region_from_legacy(layout_rig):
    rig = layout_rig
    changed = copy.deepcopy(rig.adaptation)
    changed["version"] = 2
    check = changed["page_checks"][0]
    check.update(status="failed", reason_codes=["item_outside_screen"])
    item = next(r for r in check["item_results"] if r["id"] == "content")
    item.update(status="failed", resolved_rect=None, reason_code="item_outside_screen")
    rig.select(changed)
    rig.source.image = rig.frame("wechat_list")
    engine = TextEngine()
    rig.camera.ocr_engine = engine
    with pytest.raises((ValueError, OcrEngineError)):
        asyncio.run(rig.camera.locate_text("Ann", region="account", exact=True))
    assert engine.seen == []


def test_selected_text_rechecks_binding_after_ocr(layout_rig):
    rig = layout_rig
    rig.source.image = rig.frame("wechat_list")
    changed = copy.deepcopy(rig.adaptation)
    changed["version"] = 2
    rig.camera.ocr_engine = TextEngine(after=lambda: rig.select(changed))
    with pytest.raises(ValueError):
        asyncio.run(rig.camera.locate_text("Ann", exact=True))


def test_full_means_rectified_phone_not_camera_background(layout_rig):
    rig = layout_rig
    rig.source.image = rig.frame("wechat_list")
    engine = TextEngine(words=[])
    rig.camera.ocr_engine = engine
    result = asyncio.run(rig.camera.locate_text("Ann", region="full", exact=True))
    assert engine.seen == [("messages", 320, 640)]
    assert result["found"] is False and "cameraX" not in result
    assert result["coordinateVersion"] == "calibrated_camera.v1"


@pytest.mark.parametrize("shared", [False, True])
def test_full_scope_uses_an_actual_apple_vision_region(layout_rig, monkeypatch, tmp_path, shared):
    from adapters.vision.ocr import AppleVisionOcrEngine

    rig = layout_rig
    rig.source.image = rig.frame("wechat_list")
    if not shared:
        rig.camera.attach_layout_provider(None)
    calls = []

    def runner(argv, *, input, **kwargs):
        image = screen_proof.decode(input)
        calls.append((argv[1], image.shape[:2]))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "ok": True, "text": "", "words": [], "confidence": .99,
            "width": image.shape[1], "height": image.shape[0],
        }).encode())

    engine = AppleVisionOcrEngine(runner=runner, binary_path=tmp_path / "unused")
    monkeypatch.setattr(engine, "ensure_binary", lambda: tmp_path / "unused")
    rig.camera.ocr_engine = engine
    result = asyncio.run(rig.camera.locate_text("Ann", region="full", exact=True))
    assert result["found"] is False
    assert calls == [("messages", (640, 320) if shared else (720, 1280))]


@needs_wx_example
def test_quick_recovery_preserves_ocr_coordinate_domain_and_source():
    from tests.test_wx_manual_sessions import load_plugin

    located = {"found": True, "cameraX": 148, "cameraY": 258, "frameSeq": 55,
               "frameW": 1280, "frameH": 720, "frameSha256": "a" * 64,
               "coordinateVersion": "calibrated_camera.v1", "confidence": .99,
               "text": "Ann", "bbox": [128, 250, 168, 266],
               "template_ref": {"id": "test", "version": 1, "sha256": "b" * 64}}
    state = {}

    async def capability(name, params):
        if name == "camera.identify_ui_state":
            return {"ui_state": "wechat_main", "confidence": .99}
        assert name == "camera.locate_text"
        return located

    async def observed(*args):
        pass

    async def evidence(*args):
        return {"evidence_id": "before"}

    ctx = SimpleNamespace(
        task={"payload": {"target": {"display_title": "Ann"}}},
        call_capability=capability, update_observed_ui_state=observed,
        capture_evidence=evidence, state_set=lambda k, v: state.update({k: v}))
    result = asyncio.run(load_plugin()._step_locate_target_row(ctx))
    assert result["outcome"] == "success"
    assert state["mark_read"]["ocr_target"] == located
    assert state["mark_read"]["ocr_target"] is not located


def test_contact_location_refuses_conversation_page(layout_rig):
    rig = layout_rig
    rig.source.image = rig.frame("chat_ime_hidden")
    engine = TextEngine()
    rig.camera.ocr_engine = engine
    with pytest.raises(OcrEngineError):
        asyncio.run(rig.camera.locate_text("Ann", region="account", exact=True))
    assert engine.seen == []


@pytest.mark.parametrize("returning_top", [False, True])
@needs_wx_example
def test_legacy_navigation_keeps_top_anchor_distinct_from_contact_crop(returning_top):
    from tests.test_wx_manual_sessions import load_plugin

    calls = []
    roi = {"x1": 110, "y1": 60, "x2": 400, "y2": 100, "frameW": 1280, "frameH": 720}

    async def capability(name, params):
        if name == "camera.identify_ui_state":
            return {"ui_state": "wechat_main", "confidence": .99}
        assert name == "camera.locate_text"
        calls.append(params)
        return {"found": False, "frameSeq": 1, "frameW": 1280, "frameH": 720}

    ctx = SimpleNamespace(call_capability=capability, device_context={"binding_mode": "real"})
    navigation = {"max_swipes": 0, "top_max_swipes": 0,
                  "top_anchor_region": roi, "list_region": roi}
    asyncio.run(load_plugin()._scan_with_scroll(
        ctx, navigation, "Ann", returning_top=returning_top))
    assert len(calls) == 1 and calls[0]["roi"] == roi
    assert calls[0]["region"] == ("full" if returning_top else "account")


@needs_wx_example
def test_automatic_contact_search_does_not_expand_to_page_chrome():
    from tests.test_wx_manual_sessions import load_plugin

    calls = []

    async def capability(name, params):
        assert name == "camera.locate_text"
        calls.append(params)
        return {"found": False}

    result = asyncio.run(load_plugin()._auto_nav_locate_target(
        SimpleNamespace(call_capability=capability), {"visible_name": "Ann"}))
    assert result["found"] is False
    assert calls[0]["region"] == "account"
