"""An external page observation selects registered geometry without live anchors."""
import asyncio
import copy
import pytest
from runtime.layout_observation import LayoutObservationError
from tests.test_layout_observation import layout_rig, offline_only  # noqa: F401
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401


@pytest.mark.parametrize("mode", ["calibrated", "live"])
def test_current_bound_recalculates_target_without_rewriting_archived_box(target_rig, monkeypatch, mode):
    from drivers import calibration_bundle as cb
    rig = target_rig
    selected = rig.runtime.layout_observation.resolve("arm-01")
    archived = copy.deepcopy(selected.checks["wechat_list"])
    rig.bundle["xy"]["maxResidualPx"] = 4
    cb.save_bundle(rig.bundle, rig.data)
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", mode)

    assert selected.metadata(rig.bundle)["usable"] is True
    frame = asyncio.run(rig.camera._selected_layout_frame(observed_state="wechat_list"))
    current = next(i for i in frame.check["item_results"] if i["id"] == "wechat_chats_tab")
    old = next(i for i in archived["item_results"] if i["id"] == "wechat_chats_tab")
    assert current["status"] == "passed"
    assert current["safe_rect"]["x"] == pytest.approx(current["resolved_rect"]["x"] + 5 / 320)
    assert current["safe_rect"]["width"] < old["safe_rect"]["width"]
    assert selected.checks["wechat_list"] == archived


@pytest.mark.parametrize("mode", ["calibrated", "live"])
def test_new_bound_blocks_only_narrow_target_and_preserves_text_geometry(target_rig, monkeypatch, mode):
    from drivers import calibration_bundle as cb
    from drivers.arm import DriverError
    from runtime.layout_geometry import mapping_error_bound
    from runtime.layout_matching import compute_page_check
    from tests.test_layout_observation import png
    rig = target_rig
    template = copy.deepcopy(rig.parts["template_store"].get_template("offline-template", 2))
    template["version"] = 3
    page = next(p for p in template["pages"] if p["state"] == "wechat_list")
    page["targets"].append({
        "id": "wide_target", "anchor_id": "wechat_chats_tab", "mode": "tap",
        "relative_rect": {"dx": .3, "dy": .2, "width": .25, "height": .125}})
    rig.adaptation["page_checks"] = [
        compute_page_check(p, png(rig.screens[p["state"]]),
            {a["id"]: rig.assets[a["asset_id"]] for a in p["anchors"]},
            ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle))
        for p in template["pages"]]
    rig.template_version(template)
    rig.bundle["xy"]["maxResidualPx"] = 18
    cb.save_bundle(rig.bundle, rig.data)
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", mode)

    selected = rig.runtime.layout_observation.resolve("arm-01")
    assert selected.metadata(rig.bundle)["usable"] is True
    text_frame = asyncio.run(rig.camera._selected_text_frame())
    assert text_frame.localization_mode == "screen_text"
    frame = asyncio.run(rig.camera._selected_layout_frame(observed_state="wechat_list"))
    assert frame.evidence_profile()["regions"]["content"]
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab",
                                  "target_ref": {"layout_state": "wechat_list"}}))
    assert motion(rig) == []
    result = asyncio.run(rig.arm.click({"target_id": "wide_target",
                                       "target_ref": {"layout_state": "wechat_list"}}))
    assert result["target_source"] == "shared_layout"
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


@pytest.mark.parametrize("fault", ["asymmetric", "outside", "fractional-inset", "anchor-error"])
def test_archived_safe_box_still_requires_valid_historical_geometry(target_rig, fault):
    selected = target_rig.runtime.layout_observation.resolve("arm-01")
    check = selected.checks["wechat_list"]
    item = next(i for i in check["item_results"] if i["id"] == "wechat_chats_tab")
    if fault == "asymmetric":
        item["safe_rect"]["x"] += 1 / 320
    elif fault == "outside":
        item["safe_rect"]["x"] = -1 / 320
    elif fault == "fractional-inset":
        item["safe_rect"].update(x=item["resolved_rect"]["x"] + 2.5 / 320,
            y=item["resolved_rect"]["y"] + 2.5 / 640,
            width=item["resolved_rect"]["width"] - 5 / 320,
            height=item["resolved_rect"]["height"] - 5 / 640)
    else:
        check["anchor_results"][0]["localization_error_bound_px"] = 3
    with pytest.raises(LayoutObservationError, match="安全"):
        selected.metadata(target_rig.bundle)


@pytest.mark.parametrize("mode", ["calibrated", "live"])
def test_archived_box_cannot_authorize_click_without_current_error_bound(target_rig, monkeypatch, mode):
    from drivers import calibration_bundle as cb
    from drivers.arm import DriverError
    target_rig.bundle["xy"].pop("maxResidualPx")
    cb.save_bundle(target_rig.bundle, target_rig.data)
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", mode)
    with pytest.raises(DriverError):
        asyncio.run(target_rig.arm.click({"target_id": "wechat_chats_tab",
                                         "target_ref": {"layout_state": "wechat_list"}}))
    assert motion(target_rig) == []


def test_calibrated_click_uses_local_rect_even_with_older_published_check(target_rig, monkeypatch):
    import copy
    rig = target_rig
    record = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    record["version"] = 3
    record["local_overrides"] = [{
        "state": "wechat_list", "kind": "target", "item_id": "wechat_chats_tab",
        "anchor_id": "wechat_chats_tab",
        "relative_rect": {"dx": .2, "dy": 0, "width": .1, "height": .05},
        "source_evidence_ids": ["ev-1", "ev-2"], "reason": "本台按钮范围"}]
    rig.select(record)
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    frame = asyncio.run(rig.camera._selected_layout_frame(observed_state="wechat_list"))
    assert frame.target("wechat_chats_tab")["click_point"]["screen"] == pytest.approx([96, 32])


def test_calibrated_click_rejects_override_outside_screen(target_rig, monkeypatch):
    import copy
    rig = target_rig
    record = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    record["version"] = 3
    record["local_overrides"] = [{
        "state": "wechat_list", "kind": "target", "item_id": "wechat_chats_tab",
        "anchor_id": "wechat_chats_tab",
        "relative_rect": {"dx": .98, "dy": 0, "width": .1, "height": .05},
        "source_evidence_ids": ["ev-1", "ev-2"], "reason": "越界范围应拒绝"}]
    rig.select(record)
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    from drivers.arm import DriverError
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab",
                                  "target_ref": {"layout_state": "wechat_list"}}))
    assert motion(rig) == []


def test_explicit_observed_state_uses_registered_rects(layout_rig, monkeypatch):
    rig = layout_rig
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "calibrated")
    import runtime.layout_observation as observation
    def unmatched(*args, **kwargs):
        raise AssertionError("registered page geometry must not ask live template matcher")
    monkeypatch.setattr(observation, "compute_page_check", unmatched)
    frame = asyncio.run(rig.camera._selected_layout_frame(observed_state="wechat_list"))
    assert frame.state == "wechat_list"
    assert frame.check["item_results"]
    with pytest.raises(LayoutObservationError, match="未登记"):
        asyncio.run(rig.camera._selected_layout_frame(observed_state="unknown_state"))
    monkeypatch.setenv("ARMFLEET_RUNTIME_VISION_MODE", "live")
    with pytest.raises(AssertionError, match="must not ask"):
        asyncio.run(rig.camera._selected_layout_frame(observed_state="wechat_list"))


def test_existing_platform_region_capability_is_upgraded_for_state_hint():
    from copy import deepcopy
    from runtime.capabilities import CapabilityRegistry
    class Store:
        def __init__(self):
            self.rows = {"camera.observe_regions": {"capability_id": "camera.observe_regions",
                         "source": "platform", "version": "1.0.0",
                         "input_schema": {"type": "object", "additionalProperties": False}}}
        def get_capability(self, key):
            return deepcopy(self.rows.get(key))
        def save_capability(self, row):
            self.rows[row["capability_id"]] = deepcopy(row)
    store = Store()
    CapabilityRegistry(store).bootstrap()
    saved = store.get_capability("camera.observe_regions")
    assert saved["version"] == "1.1.0"
    assert "layout_state" in saved["input_schema"]["properties"]


def test_raw_plugin_capture_pins_layout_before_acquiring_evidence():
    from copy import deepcopy
    from types import SimpleNamespace
    from runtime.plugin_capabilities import PluginCapabilityMixin
    class Runtime(PluginCapabilityMixin):
        def __init__(self):
            self.saved = {"task_id": "raw-task", "device_id": "arm-01"}
            self.store = SimpleNamespace(get_task=lambda key: deepcopy(self.saved))
            self.evidence = {"raw-1": {"evidence_id": "raw-1"}}
            self.layout_observation = SimpleNamespace(execution_binding=lambda key: {"selected_version": 15})
        def _save_task_progress(self, task):
            self.saved = deepcopy(task)
        async def plugin_invoke_capability(self, plugin_id, task, capability_id, params):
            assert task["layout_execution_binding"] == {"selected_version": 15}
            assert self.saved["layout_execution_binding"] == task["layout_execution_binding"]
            assert capability_id == "camera.capture"
            return {"evidence_id": "raw-1"}
    runtime = Runtime()
    task = deepcopy(runtime.saved)
    assert asyncio.run(runtime.plugin_capture_evidence("plugin", task, "raw")) == {"evidence_id": "raw-1"}
