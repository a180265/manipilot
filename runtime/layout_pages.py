"""Legacy layout pages plus explicitly declared, bounded text-region pages."""
from __future__ import annotations

import re

LEGACY_STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty", "chat_ime_text")
MAX_PAGES = 12
TEXT_REGIONS = "text_regions.v1"
_OPTIONAL_STATE = re.compile(r"app_[a-z][a-z0-9_]{0,47}\Z")
_UI_STATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def valid_page_state(state):
    return isinstance(state, str) and (state in LEGACY_STATES
                                      or bool(_OPTIONAL_STATE.fullmatch(state)))


def validate_page_extension(page):
    state = page.get("state")
    if not valid_page_state(state):
        raise ValueError("页面状态无效")
    fields = ("observation_kind", "ui_state", "ui_state_display")
    if state in LEGACY_STATES:
        if any(key in page for key in fields):
            raise ValueError("已有页面的观察语义不能被覆盖")
        return
    if (page.get("observation_kind") != TEXT_REGIONS
            or not isinstance(page.get("ui_state"), str)
            or not _UI_STATE.fullmatch(page["ui_state"])
            or page["ui_state"] in {"conversation", "wechat_main"}
            or not isinstance(page.get("ui_state_display"), str)
            or not page["ui_state_display"].strip()
            or len(page["ui_state_display"]) > 512):
        raise ValueError("扩展页面必须声明版本化观察类型与独立页面名称")


def page_states(template):
    if not isinstance(template, dict):
        raise ValueError("布局模板无效")
    pages = template.get("pages") or []
    if isinstance(pages, dict):
        if any(not isinstance(page, dict) or page.get("state") != state
               for state, page in pages.items()):
            raise ValueError("布局页面索引无效")
        ordered = [state for state in LEGACY_STATES if state in pages]
        ordered.extend(state for state in pages if state not in LEGACY_STATES)
        pages = [pages[state] for state in ordered]
    if not isinstance(pages, list) or not all(isinstance(page, dict) for page in pages):
        raise ValueError("布局页面定义无效")
    states = tuple(page.get("state") for page in pages)
    if (not 4 <= len(states) <= MAX_PAGES or states[:4] != LEGACY_STATES
            or len(set(states)) != len(states)
            or not all(valid_page_state(state) for state in states)):
        raise ValueError("布局须保留原有四态顺序，扩展页面不得重复或超过上限")
    for page in pages:
        validate_page_extension(page)
    return states


def is_text_page(page):
    return page.get("observation_kind") == TEXT_REGIONS


def page_ui(page):
    if is_text_page(page):
        return page["ui_state"], page["ui_state_display"]
    return (("wechat_main", "微信主界面") if page["state"] == "wechat_list"
            else ("conversation", "聊天界面"))
