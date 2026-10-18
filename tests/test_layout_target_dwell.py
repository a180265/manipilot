"""Reviewed target contact timing must not leak to other targets or request input."""
from __future__ import annotations

import asyncio
import copy

import pytest

from runtime.layout_adaptation import AdaptationError, LayoutAdaptationService
from tests.layout_d2_helpers import load_contract
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401


def override(dwell):
    return {"state": "wechat_list", "kind": "target", "item_id": "wechat_chats_tab",
        "anchor_id": "wechat_chats_tab",
        "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05},
        "tapDwellMs": dwell, "source_evidence_ids": ["ev-1", "ev-2"],
        "reason": "人工核验此目标需要额外接触停留"}


@pytest.mark.parametrize("dwell", [0, 120, 2000])
def test_target_dwell_overrides_device_default_without_affecting_other_targets(target_rig, dwell):
    from drivers import calibration_bundle as cb
    rig = target_rig
    rig.bundle["z"]["tapDwellMs"] = 50
    cb.save_bundle(rig.bundle, rig.data)
    record = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    record["version"] = 3
    record["local_overrides"] = [override(dwell)]
    rig.select(record)
    asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab", "tapDwellMs": 999}))
    suffix = f" P{dwell / 1000:g}" if dwell else ""
    assert [cmd for cmd in motion(rig) if cmd.startswith("TAP ")][-1].endswith("F1500" + suffix)
    rig.source.image = rig.frame("chat_ime_hidden")
    asyncio.run(rig.arm.click({"target_id": "back_button"}))
    assert [cmd for cmd in motion(rig) if cmd.startswith("TAP ")][-1].endswith("F1500 P0.05")


@pytest.mark.parametrize("dwell", [0, 120, 2000])
def test_dwell_override_contract_and_adaptation_accept_milliseconds(dwell):
    load_contract().validate("layout", "override", override(dwell))
    LayoutAdaptationService()._check_overrides([override(dwell)])


@pytest.mark.parametrize("dwell", [-1, 2001, True, 120.5, "120", None])
def test_invalid_target_dwell_rejected_by_contract_and_adaptation(dwell):
    validator = load_contract()
    with pytest.raises(validator.ContractError):
        validator.validate("layout", "override", override(dwell))
    with pytest.raises(AdaptationError, match="停留"):
        LayoutAdaptationService()._check_overrides([override(dwell)])


def test_contact_dwell_cannot_override_region_or_live_ime_target():
    validator = load_contract()
    region = {**override(120), "kind": "region"}
    with pytest.raises(validator.ContractError):
        validator.validate("layout", "override", region)
    with pytest.raises(AdaptationError):
        LayoutAdaptationService()._check_overrides([region])
    with pytest.raises(AdaptationError, match="IME"):
        LayoutAdaptationService()._check_overrides([{**override(120), "item_id": "ime_delete_key"}])


def test_dwell_override_on_hover_target_is_rejected_before_motion(target_rig):
    from drivers.arm import DriverError
    rig = target_rig
    record = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    record["version"] = 3
    record["local_overrides"] = [{**override(120), "item_id": "hover_spot"}]
    rig.select(record)
    with pytest.raises(DriverError, match="接触停留"):
        asyncio.run(rig.arm.click({"target_id": "hover_spot"}))
    assert motion(rig) == []
