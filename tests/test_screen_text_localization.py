"""Exact text geometry is independent of page recognition, never of its binding."""
from __future__ import annotations

import asyncio
import copy
import time

import pytest

from adapters.vision.ocr import OcrEngineError, OcrResult, OcrWord
from drivers import screen_proof
from drivers.arm import DriverError
from tests.test_layout_observation import layout_rig, offline_only  # noqa: F401
from tests.test_layout_target_driver import motion, target_rig  # noqa: F401


class ExactEngine:
    def __init__(self):
        self.words = [OcrWord("Ann", 32, 200, 40, 16, .99)]
        self.fault = None
        self.after = None
        self.seen = []

    def recognize(self, image, *, region):
        height, width = screen_proof.decode(image).shape[:2]
        self.seen.append((region, width, height))
        if self.after:
            self.after()
        words = copy.deepcopy(self.words)
        if self.fault == "outside":
            words[0].x = -1
        return OcrResult(text="\n".join(word.text for word in words), words=words,
                         confidence=.99, truncated=self.fault == "truncated",
                         engine="offline-fake", engine_version="1", width=width, height=height)


def unrecognized(rig):
    rig.source.image = rig.frame("wechat_list")
    # Remove both page anchors while retaining the real, calibrated phone outline.
    rig.source.image[66:98, 116:188] = 240
    rig.camera.ocr_engine = ExactEngine()


def locate(rig):
    result = asyncio.run(rig.camera.locate_text(
        "Ann", region="account", exact=True, localization_mode="screen_text"))
    assert result["found"]
    return {"target_id": "conversation_item", "target_ref": {"source": "camera_ocr", **result},
            "_layout_binding": copy.deepcopy(
                rig.runtime.layout_observation.execution_binding("arm-01"))}


def test_screen_text_uses_calibrated_root_without_page_claim(layout_rig):
    rig = layout_rig
    unrecognized(rig)
    with pytest.raises(ValueError, match="模板匹配"):
        asyncio.run(rig.camera.locate_text("Ann", region="account", exact=True))
    result = locate(rig)["target_ref"]
    assert result["localizationMode"] == "screen_text"
    assert result["bbox"] == pytest.approx([132, 250, 172, 266], abs=.1)
    assert rig.camera.ocr_engine.seen == [("account", 320, 640)]
    frame = asyncio.run(rig.camera.current_text_frame(result))
    assert not any(hasattr(frame, attr) for attr in ("state", "profile", "target", "verify_target_point"))


@pytest.mark.parametrize("kwargs", [
    {"localization_mode": "screen_text"},
    {"localization_mode": "screen_text", "exact": False},
    {"localization_mode": "unknown", "exact": True},
])
def test_invalid_mode_or_nonexact_query_fails_before_capture(layout_rig, monkeypatch, kwargs):
    async def forbidden():
        pytest.fail("Invalid request must be rejected before capturing")
    monkeypatch.setattr(layout_rig.camera, "_capture_for_operation", forbidden)
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(layout_rig.camera.locate_text("Ann", **kwargs))


def test_dynamic_screen_text_click_rechecks_using_same_provider(target_rig, monkeypatch):
    rig = target_rig
    unrecognized(rig)
    target = locate(rig)

    async def forbidden():
        pytest.fail("Explicit screen_text must not re-enter page-template observation")
    rig.arm.attach_layout_provider(forbidden)
    result = asyncio.run(rig.arm.click(target))
    assert result["target_source"] == "camera_ocr"
    assert sum(line.startswith("TAP ") for line in motion(rig)) == 1
    assert len(rig.camera.ocr_engine.seen) >= 3  # locate and fresh contact observations
    assert set(rig.camera.ocr_engine.seen) == {("account", 320, 640)}


def test_screen_text_click_does_not_expire_while_building_discarded_alignment(target_rig, monkeypatch):
    """Repeated full-camera alignment must not consume the original proof's TTL."""
    from types import SimpleNamespace
    from drivers import real_camera, screen_pose

    rig = target_rig
    unrecognized(rig)
    clock = SimpleNamespace(now=time.monotonic())
    monkeypatch.setattr(real_camera, "time", SimpleNamespace(monotonic=lambda: clock.now))
    original = screen_pose.align_screen
    calls = []

    def slow_full_camera_alignment(*args, **kwargs):
        calls.append(True)
        # A deterministic CPU-time seam, without sleeping or changing asyncio's clock.
        clock.now += .75
        return original(*args, **kwargs)

    monkeypatch.setattr(screen_pose, "align_screen", slow_full_camera_alignment)
    target = locate(rig)
    result = asyncio.run(rig.arm.click(target))
    assert result["ok"] is True
    assert sum(line.startswith("TAP ") for line in motion(rig)) == 1
    assert calls == []  # Text uses its own calibrated-root warp, never this output.


def test_screen_text_action_relocates_after_travel_without_renewing_original(target_rig, monkeypatch):
    from types import SimpleNamespace
    from drivers import real_camera

    rig = target_rig
    unrecognized(rig)
    clock = SimpleNamespace(now=time.monotonic())
    monkeypatch.setattr(real_camera, "time", SimpleNamespace(monotonic=lambda: clock.now))
    target = locate(rig)
    original_id = target["target_ref"]["locationId"]
    original_created = rig.camera._text_locations[original_id]["created"]

    def after(line):
        if line.startswith("XY "):
            clock.now += 3  # Real travel can exceed the admission credential's lifetime.

    rig.transport.after_send = after
    result = asyncio.run(rig.arm.click(target))
    assert result["ok"] is True
    assert sum(line.startswith("TAP ") for line in motion(rig)) == 1
    assert rig.camera._text_locations[original_id]["created"] == original_created
    assert len(rig.camera._text_locations) >= 3  # independent fresh location credentials
    original_query = rig.camera._text_locations[original_id]["query"]
    assert all(record["query"] == original_query
               for record in rig.camera._text_locations.values())
    before = list(motion(rig))
    with pytest.raises(DriverError, match="原文字定位已过期"):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == before  # The accepted action is never reusable by a new click.


@pytest.mark.parametrize("fault", ["expired", "missing", "polygon"])
def test_text_action_admission_rejects_invalid_original_before_capture(target_rig, monkeypatch, fault):
    rig = target_rig
    unrecognized(rig)
    target = locate(rig)
    ref = target["target_ref"]
    if fault == "expired":
        rig.camera._text_locations[ref["locationId"]]["created"] -= 3
    elif fault == "missing":
        ref["locationId"] = "forged"
    else:
        ref["cameraPolygon"][0][0] += 1

    async def forbidden():
        pytest.fail("Admission must reject the original before any new camera capture")

    monkeypatch.setattr(rig.camera, "_capture_for_operation", forbidden)
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


@pytest.mark.parametrize("outcome", ["success", "text_changed", "cancelled"])
def test_text_action_is_closed_after_every_click_outcome(target_rig, outcome):
    rig = target_rig
    unrecognized(rig)
    target = locate(rig)
    actions = []
    begin = rig.arm._text_action_provider

    def capture_action(ref):
        action = begin(ref)
        actions.append(action)
        return action

    rig.arm.attach_text_action_provider(capture_action)

    def after(line):
        if line.startswith("XY "):
            if outcome == "text_changed":
                rig.camera.ocr_engine.words[0].text = "Other"
            elif outcome == "cancelled":
                raise asyncio.CancelledError()

    rig.transport.after_send = after
    if outcome == "success":
        assert asyncio.run(rig.arm.click(target))["ok"] is True
    else:
        expected = asyncio.CancelledError if outcome == "cancelled" else DriverError
        with pytest.raises(expected):
            asyncio.run(rig.arm.click(target))
        assert not any(line.startswith("TAP") for line in motion(rig))
    assert len(actions) == 1 and actions[0].active is False
    with pytest.raises(ValueError, match="已结束"):
        asyncio.run(actions[0].current_frame())


def test_text_action_final_lock_rejects_expired_fresh_observation(target_rig, monkeypatch):
    from types import SimpleNamespace
    from drivers import real_camera

    rig = target_rig
    unrecognized(rig)
    clock = SimpleNamespace(now=time.monotonic())
    monkeypatch.setattr(real_camera, "time", SimpleNamespace(monotonic=lambda: clock.now))
    target = locate(rig)

    def before(line):
        if line.startswith("TAP "):
            clock.now += 1.01

    rig.transport.before_send = before
    with pytest.raises(DriverError, match="复核已过期"):
        asyncio.run(rig.arm.click(target))
    assert any(line.startswith("XY ") for line in motion(rig))
    assert not any(line.startswith("TAP") for line in motion(rig))


def test_text_action_does_not_reuse_a_previous_observation(layout_rig):
    rig = layout_rig
    unrecognized(rig)
    target = locate(rig)
    action = rig.camera.begin_text_action(target["target_ref"])

    async def scenario():
        frame = await action.current_frame()
        await action.verify(frame)
        with pytest.raises(ValueError):
            await action.verify(frame)
        action.close()

    asyncio.run(scenario())


def test_text_relocation_proves_original_area_without_renewing_old_proof(layout_rig):
    rig = layout_rig
    unrecognized(rig)
    rig.camera.ocr_engine.words[0].width = 120
    rig.camera.ocr_engine.words[0].height = 32
    original = locate(rig)["target_ref"]
    original_id = original["locationId"]
    record = rig.camera._text_locations[original_id]
    record["created"] -= 3
    old_created = record["created"]
    rig.camera.ocr_engine.words[0].x += 4
    refreshed = asyncio.run(rig.camera.locate_text(
        "Ann", region="account", exact=True, localization_mode="screen_text",
        previous_location_id=original_id))
    assert refreshed["previousLocationId"] == original_id
    assert refreshed["locationId"] != original_id
    assert rig.camera._text_locations[original_id]["created"] == old_created
    rig.camera._text_location_record(refreshed)
    with pytest.raises(ValueError, match="原文字定位已过期"):
        rig.camera._text_location_record(original)
    # Multiple valid observations must remain anchored to the first area.
    rig.camera.ocr_engine.words[0].x += 65
    with pytest.raises(ValueError, match="安全范围"):
        asyncio.run(rig.camera.locate_text(
            "Ann", region="account", exact=True, localization_mode="screen_text",
            previous_location_id=original_id))


@pytest.mark.parametrize("fault", ["query", "binding", "calibration", "source_geometry",
                                 "source_generation", "text", "duplicate", "truncated"])
def test_text_relocation_rejects_changed_intent_or_unproven_observation(layout_rig, fault):
    from drivers import calibration_bundle as cb

    rig = layout_rig
    unrecognized(rig)
    rig.camera.ocr_engine.words[0].width = 120
    rig.camera.ocr_engine.words[0].height = 32
    original_id = locate(rig)["target_ref"]["locationId"]
    kwargs = dict(region="account", exact=True, localization_mode="screen_text",
                  previous_location_id=original_id)
    if fault == "query":
        kwargs["min_confidence"] = .9
    elif fault == "binding":
        rig.runtime.devices.get("arm-01")["binding_revision"] += 1
    elif fault == "calibration":
        bundle = copy.deepcopy(rig.bundle)
        bundle["camera"]["cameraToScreen"]["maxErrorPx"] += .1
        cb.save_bundle(bundle, rig.data)
    elif fault == "source_geometry":
        rig.source.last_source_geometry_id = "d" * 64
    elif fault == "source_generation":
        original = rig.camera._capture_for_operation

        async def changed_source():
            frame = await original()
            frame["source_generation_id"] = "other-generation"
            return frame

        rig.camera._capture_for_operation = changed_source
    elif fault == "text":
        rig.camera.ocr_engine.words[0].text = "Other"
    elif fault == "duplicate":
        rig.camera.ocr_engine.words.append(OcrWord("Ann", 32, 300, 120, 32, .99))
    else:
        rig.camera.ocr_engine.fault = "truncated"
    with pytest.raises((ValueError, RuntimeError, OcrEngineError)):
        asyncio.run(rig.camera.locate_text("Ann", **kwargs))
    assert len(rig.camera._text_locations) == 1


@pytest.mark.parametrize("fault", ["marker", "adaptation", "text", "truncated",
                                 "source_geometry"])
def test_screen_text_changes_after_xy_zero_contact(target_rig, fault):
    rig = target_rig
    unrecognized(rig)
    target = locate(rig)

    def after(line):
        if not line.startswith("XY "):
            return
        if fault == "marker":
            target["target_ref"]["localizationMode"] = "layout"
        elif fault == "adaptation":
            changed = copy.deepcopy(rig.adaptation)
            changed["version"] = 3
            rig.select(changed)
        elif fault == "text":
            rig.camera.ocr_engine.words[0].text = "Other"
        elif fault == "source_geometry":
            rig.source.last_source_geometry_id = "d" * 64
        else:
            rig.camera.ocr_engine.fault = "truncated"
    rig.transport.after_send = after
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert any(line.startswith("XY ") for line in motion(rig))
    assert not any(line.startswith("TAP") for line in motion(rig))


@pytest.mark.parametrize("attribute,value", [
    ("last_source_geometry_id", "d" * 64),
    ("last_source_geometry_id", None),
    ("last_camera_binding_id", None),
])
def test_screen_text_requires_actual_source_geometry_before_ocr(layout_rig, attribute, value):
    from runtime.layout_observation import LayoutObservationError

    rig = layout_rig
    unrecognized(rig)
    setattr(rig.source, attribute, value)
    with pytest.raises(LayoutObservationError) as error:
        locate(rig)
    assert error.value.reason_code == "source_binding_changed"
    assert rig.camera.ocr_engine.seen == []
    assert rig.camera._text_locations == {}


@pytest.mark.parametrize("fault", ["marker", "expired", "missing"])
def test_text_provider_validates_original_credential_before_capture(layout_rig, monkeypatch, fault):
    rig = layout_rig
    unrecognized(rig)
    ref = locate(rig)["target_ref"]
    if fault == "marker":
        ref["localizationMode"] = "layout"
    elif fault == "expired":
        rig.camera._text_locations[ref["locationId"]]["created"] = time.monotonic() - 3
    else:
        ref["locationId"] = "forged"

    async def forbidden():
        pytest.fail("Invalid original proof must fail before a new geometry frame")
    monkeypatch.setattr(rig.camera, "_capture_for_operation", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(rig.camera.current_text_frame(ref))


@pytest.mark.parametrize("fault", ["outside", "truncated", "duplicate"])
def test_screen_text_rejects_bad_or_ambiguous_ocr(layout_rig, fault):
    rig = layout_rig
    unrecognized(rig)
    if fault == "duplicate":
        rig.camera.ocr_engine.words.append(OcrWord("Ann", 32, 300, 40, 16, .99))
    else:
        rig.camera.ocr_engine.fault = fault
    with pytest.raises((ValueError, RuntimeError, OcrEngineError)):
        locate(rig)
    assert rig.camera._text_locations == {}


def test_screen_text_roi_filters_calibrated_domain(layout_rig):
    rig = layout_rig
    unrecognized(rig)
    rig.camera.ocr_engine.words.append(OcrWord("Ann", 32, 300, 40, 16, .99))
    result = asyncio.run(rig.camera.locate_text(
        "Ann", exact=True, localization_mode="screen_text",
        roi={"x1": 130, "y1": 245, "x2": 175, "y2": 270, "frameW": 1280, "frameH": 720}))
    assert result["found"] and result["cameraY"] == pytest.approx(258)


def test_default_proof_cannot_be_promoted_to_screen_text(target_rig, monkeypatch):
    from tests.test_pre_enable_s3_driver import locate as default_locate

    rig = target_rig
    target = default_locate(rig)
    target["target_ref"]["localizationMode"] = "screen_text"

    async def forbidden():
        pytest.fail("Forged mode must be rejected before geometry capture")
    monkeypatch.setattr(rig.camera, "_capture_for_operation", forbidden)
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


def test_screen_text_requires_dedicated_provider(target_rig):
    rig = target_rig
    unrecognized(rig)
    target = locate(rig)
    rig.arm.attach_text_layout_provider(None)
    with pytest.raises(DriverError, match="专用文字几何"):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


def test_text_geometry_does_not_authorize_fixed_target(target_rig):
    rig = target_rig
    unrecognized(rig)
    target = locate(rig)
    target["target_id"] = "wechat_chats_tab"
    with pytest.raises(DriverError, match="模板匹配"):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


@pytest.mark.parametrize("fault", ["calibration", "binding", "region"])
def test_screen_text_context_changes_during_ocr_invalidates_location(layout_rig, fault):
    from drivers import calibration_bundle as cb

    rig = layout_rig
    unrecognized(rig)

    def change():
        if fault == "calibration":
            bundle = copy.deepcopy(rig.bundle)
            bundle["camera"]["cameraToScreen"]["maxErrorPx"] += .1
            cb.save_bundle(bundle, rig.data)
        elif fault == "binding":
            rig.runtime.devices.get("arm-01")["binding_revision"] += 1
        else:
            rig.parts["region_store"].cas_pointer("arm-01", 1, 2, "f" * 64, "test")
    rig.camera.ocr_engine.after = change
    with pytest.raises((ValueError, RuntimeError)):
        locate(rig)
    assert rig.camera._text_locations == {}


def test_capability_upgrade_publishes_mode_to_existing_installations(layout_rig):
    from runtime.capabilities import CapabilityRegistry

    registry = CapabilityRegistry(layout_rig.runtime.store)
    registry.bootstrap()
    previous = registry.get("camera.locate_text")
    previous.update(version="1.0.0", input_schema={"type": "object"})
    layout_rig.runtime.store.save_capability(previous)
    registry.bootstrap()
    current = registry.get("camera.locate_text")
    assert current["version"] == "1.1.0"
    assert "localization_mode" in current["input_schema"]["properties"]


@pytest.mark.parametrize("params,accepted", [
    ({"text": "Ann"}, True),
    ({"text": "Ann", "localization_mode": "layout"}, True),
    ({"text": "Ann", "localization_mode": "screen_text", "exact": True}, True),
    ({"text": "Ann", "localization_mode": "screen_text"}, False),
    ({"text": "Ann", "localization_mode": "screen_text", "exact": False}, False),
    ({"text": "Ann", "localization_mode": "other", "exact": True}, False),
])
def test_capability_schema_requires_explicit_exact_mode(params, accepted):
    from jsonschema import Draft202012Validator
    from runtime.capabilities import BASE_CAPABILITIES

    schema = next(c["input_schema"] for c in BASE_CAPABILITIES
                  if c["capability_id"] == "camera.locate_text")
    assert Draft202012Validator(schema).is_valid(params) is accepted


def test_runtime_passes_explicit_mode_to_driver_and_keeps_default_compatible(tmp_path):
    from tasks.plugin_api import StepContext
    from tests.helpers import PROBE_MANIFEST, make_device_configs, make_runtime

    async def scenario():
        manifest = {**PROBE_MANIFEST,
                    "capabilities": [*PROBE_MANIFEST["capabilities"], "camera.locate_text"]}
        runtime = make_runtime(tmp_path, manifest=manifest)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = {"task_id": "mode-options", "device_id": "arm-01",
                    "plugin_id": manifest["plugin_id"], "status": "executing",
                    "lease_id": runtime.leases.acquire("arm-01", "mode-options")["lease_id"]}
            runtime.store.save_task(task)
            ctx = StepContext(runtime, task, manifest)
            params = {"text": "Ann", "exact": True, "localization_mode": "screen_text"}
            result = await ctx.call_capability("camera.locate_text", params)
            assert result["localizationMode"] == "screen_text"
            default = await ctx.call_capability("camera.locate_text", {"text": "Ann", "exact": True})
            assert "localizationMode" not in default
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
