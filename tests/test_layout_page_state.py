"""Shared chat chrome must not be treated as a four-page score competition."""
from __future__ import annotations

import asyncio
import copy

import pytest

from adapters.vision.ocr import OcrWord
from tests.test_layout_observation import Engine, layout_rig, offline_only  # noqa: F401


class InputEngine(Engine):
    def __init__(self, text="", confidence=.99):
        super().__init__()
        self.text, self.confidence = text, confidence

    def recognize(self, image, *, region):
        result = super().recognize(image, region=region)
        if region == "input":
            result.text, result.confidence = self.text, self.confidence
            result.words = [OcrWord(self.text, 10, 5, 24, 12, self.confidence)] if self.text else []
        return result


def shared_keyboard_pages(rig):
    template = copy.deepcopy(rig.template)
    template["version"] = 2
    pages = {page["state"]: page for page in template["pages"]}
    empty, text = pages["chat_ime_empty"], pages["chat_ime_text"]
    for index in range(3):
        text["anchors"][index] = copy.deepcopy(empty["anchors"][index])
        left = 16 + index * 40
        rig.screens["chat_ime_text"][16:48, left:left + 32] = (
            rig.screens["chat_ime_empty"][16:48, left:left + 32])
    rig.screens["chat_ime_text"][532:548, 40:80] = 30
    rig.template_version(template)


def test_same_header_keyboard_anchors_can_discriminate_text_by_visual_input(layout_rig):
    rig = layout_rig
    shared_keyboard_pages(rig)
    rig.source.image = rig.frame("chat_ime_text")
    rig.camera.ocr_engine = InputEngine("test")
    result = asyncio.run(rig.camera.observe_regions())
    assert result["ui"]["ui_state"] == "conversation"
    assert result["input_text"] == "test"
    assert result["input_empty"] is False
    assert sum(region == "input" for region, _, _ in rig.camera.ocr_engine.seen) == 1
    assert asyncio.run(rig.camera._selected_layout_frame()).state == "chat_ime_text"


def test_same_header_keyboard_anchors_keep_empty_state(layout_rig):
    rig = layout_rig
    shared_keyboard_pages(rig)
    rig.source.image = rig.frame("chat_ime_empty")
    rig.camera.ocr_engine = InputEngine()
    assert asyncio.run(rig.camera._selected_layout_frame()).state == "chat_ime_empty"
    assert asyncio.run(rig.camera.observe_regions())["input_empty"] is True


@pytest.mark.parametrize("text,confidence", [("", .99), ("test", .4)])
def test_input_difference_without_reliable_text_is_unknown(layout_rig, text, confidence):
    rig = layout_rig
    shared_keyboard_pages(rig)
    rig.source.image = rig.frame("chat_ime_text")
    rig.camera.ocr_engine = InputEngine(text, confidence)
    with pytest.raises(ValueError):
        asyncio.run(rig.camera._selected_layout_frame())


def test_text_ocr_on_visually_empty_input_does_not_select_text_page(layout_rig):
    rig = layout_rig
    shared_keyboard_pages(rig)
    rig.source.image = rig.frame("chat_ime_empty")
    rig.camera.ocr_engine = InputEngine("test")
    with pytest.raises(ValueError):
        asyncio.run(rig.camera._selected_layout_frame())


@pytest.mark.parametrize("other", ["wechat_list", "chat_ime_hidden"])
def test_missing_text_feature_does_not_fall_back_to_another_page(layout_rig, other):
    rig = layout_rig
    shared_keyboard_pages(rig)
    template = copy.deepcopy(rig.parts["ops_svc"].resolve_template("offline-template", 2))
    template.update(version=3, parent_version=2)
    pages = {page["state"]: page for page in template["pages"]}
    for index, original in enumerate(pages[other]["anchors"]):
        replacement = copy.deepcopy(pages["chat_ime_empty"]["anchors"][index])
        replacement["id"] = original["id"]
        pages[other]["anchors"][index] = replacement
    rig.template_version(template)
    # Keyboard is visibly present and contains text, but has no send feature.
    rig.screens["chat_ime_empty"][532:548, 40:80] = 30
    rig.source.image = rig.frame("chat_ime_empty")
    rig.camera.ocr_engine = InputEngine("test")
    with pytest.raises(ValueError, match="回退"):
        asyncio.run(rig.camera._selected_layout_frame())
