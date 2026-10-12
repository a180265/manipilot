import asyncio
from copy import deepcopy
from types import SimpleNamespace

from drivers import calibration_bundle as cb, screen_gestures
from runtime.screen_gestures import gesture_metadata
from tests.test_screen_gesture_driver import gesture_bundle


def runtime_fixture(tmp_path, *, features=None):
    bundle = gesture_bundle()
    cb.save_bundle(bundle, tmp_path)
    state = {"connected": True, "motion_features": (
        ["tap2.v1", "swipe.v1"] if features is None else features)}

    async def read_state():
        return dict(state)

    driver = SimpleNamespace(data_root=tmp_path, read_state=read_state)
    device = {"device_id": "arm-01", "status": "ready", "binding_mode": "simulation"}
    runtime = SimpleNamespace(
        devices=SimpleNamespace(get=lambda _: device, driver=lambda _: driver),
        config=SimpleNamespace(root=tmp_path),
        estop=SimpleNamespace(active=False),
        leases=SimpleNamespace(get=lambda _: None),
        plugins=SimpleNamespace(task_type_registry=lambda: {
            "screen_gesture": {"plugin_id": "screen-gesture-adapter"}}),
        plugin_context_blockers=lambda *_: [],
    )
    return runtime, bundle, state, device


def test_metadata_returns_calibration_binding_without_writing(tmp_path):
    runtime, bundle, _, _ = runtime_fixture(tmp_path)
    before = cb.bundle_path("arm-01", tmp_path).read_bytes()
    result = asyncio.run(gesture_metadata(runtime, "arm-01"))
    assert result["binding"] == screen_gestures.binding_snapshot(bundle)
    assert result["usable"]
    assert all(action["usable"] for action in result["actions"].values())
    assert cb.bundle_path("arm-01", tmp_path).read_bytes() == before


def test_metadata_blocks_draft_and_never_promotes(tmp_path):
    runtime, bundle, _, _ = runtime_fixture(tmp_path)
    bundle["status"] = "draft"
    cb.save_bundle(bundle, tmp_path)
    before = cb.bundle_path("arm-01", tmp_path).read_bytes()
    result = asyncio.run(gesture_metadata(runtime, "arm-01"))
    assert not result["usable"] and result["binding"] is None
    assert not any(action["usable"] for action in result["actions"].values())
    assert cb.bundle_path("arm-01", tmp_path).read_bytes() == before


def test_metadata_accepts_base_macro_without_short_lift_or_fast_feed(tmp_path):
    runtime, _, _, _ = runtime_fixture(tmp_path, features=["tap2.v1", "swipe.v1"])
    result = asyncio.run(gesture_metadata(runtime, "arm-01"))
    assert result["usable"]
    assert result["actions"]["double_tap"]["usable"]
    assert result["actions"]["swipe"]["usable"]


def test_metadata_short_lift_feature_does_not_replace_required_base_macro(tmp_path):
    runtime, _, _, _ = runtime_fixture(tmp_path, features=["tap2.short-lift.v1", "swipe.v1"])
    result = asyncio.run(gesture_metadata(runtime, "arm-01"))
    assert result["usable"]
    assert not result["actions"]["double_tap"]["usable"]
    assert result["actions"]["swipe"]["usable"]


def test_metadata_blocks_busy_device_and_existing_screen_context(tmp_path):
    runtime, _, _, device = runtime_fixture(tmp_path)
    runtime.plugin_context_blockers = lambda *_: ["已有屏幕执行上下文"]
    device["status"] = "busy"
    result = asyncio.run(gesture_metadata(runtime, "arm-01"))
    assert not result["usable"]
    assert "已有屏幕执行上下文" in result["blocking_reasons"]

def test_frame_only_verification_is_explicitly_not_ready(tmp_path):
    runtime, _, _, _ = runtime_fixture(tmp_path)
    runtime.devices.driver("arm-01").motion_verifier = SimpleNamespace(measurement_kind="frame_change")
    result = asyncio.run(gesture_metadata(runtime, "arm-01"))
    assert not result["usable"]
    assert any("画面变化" in reason for reason in result["blocking_reasons"])
    assert not any(action["usable"] for action in result["actions"].values())


def test_full_calibration_hash_changes_for_xy_and_z_not_just_camera():
    bundle = gesture_bundle()
    before = screen_gestures.binding_snapshot(bundle)
    for section, key in [("z", "tapDepth"), ("xy", "residualRmsPx")]:
        changed = deepcopy(bundle)
        changed[section][key] += .1
        assert screen_gestures.binding_snapshot(changed) != before


def test_existing_gesture_capability_metadata_upgrades_without_other_changes(tmp_path):
    from runtime.capabilities import BASE_CAPABILITIES, CapabilityRegistry
    from runtime.store import Store

    store = Store(tmp_path / "test.db")
    registry = CapabilityRegistry(store)
    registry.bootstrap()
    for key in ("arm.double_tap", "arm.swipe"):
        old = store.get_capability(key)
        old.update(version="1.0.0", input_schema={"type": "object"})
        store.save_capability(old)
    custom = store.get_capability("arm.click")
    custom["name_display"] = "preserved custom label"
    store.save_capability(custom)
    registry.bootstrap()
    registry.bootstrap()
    for key in ("arm.double_tap", "arm.swipe"):
        expected = next(item for item in BASE_CAPABILITIES if item["capability_id"] == key)
        actual = store.get_capability(key)
        assert actual["version"] == "1.1.0"
        assert actual["input_schema"] == expected["input_schema"]
    assert store.get_capability("arm.click") == custom
