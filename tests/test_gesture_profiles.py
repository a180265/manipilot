from copy import deepcopy
import json

import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")


from drivers import calibration_bundle as cb, gesture_profiles as gp
from tests.test_ui_targets import ready_bundle


def navigation():
    def point(x, y):
        return {"cameraX": x, "cameraY": y, "frameW": 1280, "frameH": 720}

    return {
        "search_gesture": {"start": point(600, 550), "end": point(600, 200)},
        "top_gesture": {"start": point(600, 200), "end": point(600, 550)},
        "top_anchor_text": "Pinned contact",
        "top_anchor_region": {"x1": 330, "y1": 120, "x2": 780, "y2": 180,
                              "frameW": 1280, "frameH": 720},
        "list_region": {"x1": 300, "y1": 100, "x2": 850, "y2": 650,
                        "frameW": 1280, "frameH": 720},
    }


def evidence(direction, **kwargs):
    return {
        "before": {"sha256": "before", "path": "/test/before.png",
                   "source_frame_seq": 1, "top_anchor_found": direction == "search"},
        "after": {"sha256": "after", "path": "/test/after.png",
                  "source_frame_seq": 2, "top_anchor_found": direction == "top"},
        "motion": {"ok": True, "motion_verified": True, "swipe_executed": True},
        "main_ui_confirmed": True, **kwargs,
    }


@pytest.fixture()
def profile_root(tmp_path):
    cb.save_bundle(ready_bundle(), tmp_path)
    return tmp_path


def draft(root, config=None, device="arm-01", session="sess-A"):
    return gp.save_draft(device, "wx_chat_list", config or navigation(),
                         operator="tester", current_session=session, data_root=root)


def run_test(root, entry, direction, *, outcome="confirmed", proof=None):
    started = gp.begin_test(
        "arm-01", "wx_chat_list", direction, expected_revision=entry["revision"],
        current_session="sess-A", operator="tester", data_root=root)
    return gp.finish_test(
        "arm-01", "wx_chat_list", expected_revision=started["revision"],
        test_id=started["pending_test"]["id"], outcome=outcome,
        evidence=proof if proof is not None else evidence(direction),
        current_session="sess-A", operator="tester", data_root=root)


def make_tested(root):
    entry = draft(root)
    entry = run_test(root, entry, "search")
    return run_test(root, entry, "top")


def enable(root, entry, confirmed=True):
    return gp.set_enabled(
        "arm-01", "wx_chat_list", enabled=True, expected_revision=entry["revision"],
        operator="tester", current_session="sess-A", human_confirmed=confirmed, data_root=root)


def resolve(root, entry, **kwargs):
    return gp.resolve_profile(
        "arm-01", "wx_chat_list", entry["version"], owner_plugin="wx-wechat-adapter",
        current_session="sess-A", data_root=root, **kwargs)


def test_lifecycle_and_calibration_immutable(profile_root):
    path = cb.bundle_path("arm-01", profile_root)
    before = path.read_bytes()
    entry = draft(profile_root)
    assert (entry["version"], entry["revision"], entry["state"]) == (1, 1, "draft")
    with pytest.raises(ValueError):
        enable(profile_root, entry)
    entry = run_test(profile_root, entry, "search")
    assert entry["state"] == "draft"
    entry = run_test(profile_root, entry, "top")
    assert entry["state"] == "tested"
    with pytest.raises(ValueError):
        enable(profile_root, entry, False)
    entry = enable(profile_root, entry)
    assert resolve(profile_root, entry) == gp.validate_navigation(navigation())
    view = gp.view_profile(entry, cb.load_bundle("arm-01", profile_root), "sess-A")
    assert view["usable"] and not view["blocking_reasons"]
    entry = gp.set_enabled(
        "arm-01", "wx_chat_list", enabled=False, expected_revision=entry["revision"],
        operator="tester", current_session=None, data_root=profile_root)
    with pytest.raises(ValueError):
        resolve(profile_root, entry)
    enable(profile_root, entry)
    assert path.read_bytes() == before


def test_edit_resets_all_proofs_and_increments_version(profile_root):
    old = enable(profile_root, make_tested(profile_root))
    updated = draft(profile_root)
    assert updated["version"] == old["version"] + 1
    assert updated["revision"] > old["revision"]
    assert updated["tests"] == {} and updated["state"] == "draft"
    with pytest.raises(ValueError):
        enable(profile_root, updated)
    with pytest.raises(ValueError):
        resolve(profile_root, old)


@pytest.mark.parametrize("motion", [
    {"ok": True, "motion_verified": True},
    {"ok": True, "motion_verified": True, "swipe_executed": False},
    {"ok": True, "motion_verified": True, "swipe_executed": True, "aborted": True},
    {"ok": True, "motion_verified": True, "swipe_executed": True, "unknown": True},
])
def test_incomplete_motion_cannot_be_certified_or_reused_from_old_test(profile_root, motion):
    entry = draft(profile_root)
    proof = evidence("search")
    proof["motion"] = motion
    with pytest.raises(ValueError, match="真实滑动证据"):
        run_test(profile_root, entry, "search", proof=proof)
    entry = enable(profile_root, make_tested(profile_root))
    entry["tests"]["search"]["evidence"]["motion"] = motion
    view = gp.view_profile(entry, cb.load_bundle("arm-01", profile_root), "sess-A")
    assert not view["usable"]
    assert any("缺少实际测试证据" in reason for reason in view["blocking_reasons"])


@pytest.mark.parametrize("key,value", [
    ("max_swipes", True), ("max_swipes", -1), ("max_swipes", 11), ("max_swipes", 2.0),
    ("top_max_swipes", 11), ("swipe_duration_ms", 149), ("swipe_duration_ms", 5001),
    ("settle_ms", 99), ("settle_ms", 2001), ("return_to_top", "true"),
    ("top_anchor_text", "WeChat"), ("top_anchor_text", "\u5fae\u4fe1"),
    ("top_anchor_text", "WeChat (12)"), ("top_anchor_text", ""),
    ("top_anchor_region", None), ("list_region", "account"), ("unknown", "value"),
])
def test_invalid_config_rejected(key, value):
    config = navigation()
    config[key] = value
    with pytest.raises(ValueError):
        gp.validate_navigation(config)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "600", -1, 1280])
def test_nonfinite_out_of_frame_points_rejected(value):
    config = navigation()
    config["search_gesture"]["start"]["cameraX"] = value
    with pytest.raises(ValueError):
        gp.validate_navigation(config)


def test_geometry_and_required_regions(profile_root):
    config = navigation()
    config["top_gesture"]["start"]["frameW"] = 1920
    with pytest.raises(ValueError):
        gp.validate_navigation(config)
    config = navigation()
    config.pop("list_region")
    gp.validate_navigation(config)  # Legacy inline coordinates can omit the list ROI.
    with pytest.raises(ValueError):
        draft(profile_root, config)
    config = navigation()
    config.pop("top_anchor_region")
    with pytest.raises(ValueError):
        gp.validate_navigation(config)
    config = navigation()
    config["search_gesture"]["end"] = deepcopy(config["search_gesture"]["start"])
    with pytest.raises(ValueError):
        gp.validate_navigation(config)
    config = navigation()
    config["search_gesture"]["end"]["cameraY"] = 30
    with pytest.raises(ValueError):
        gp.validate_navigation(config)


@pytest.mark.parametrize("change", ["session", "geometry", "z", "binding", "frame", "orientation", "not_ready"])
def test_binding_changes_invalidate(profile_root, change):
    entry = enable(profile_root, make_tested(profile_root))
    bundle = cb.load_bundle("arm-01", profile_root)
    if change == "session":
        bundle["daemonSession"] = "other-session"
    elif change == "geometry":
        bundle["xy"]["robotToScreen"]["a"] = 9.0
    elif change == "z":
        bundle["z"]["tapDepth"] = 4
    elif change == "binding":
        bundle["bindingRevision"] += 1
    elif change == "frame":
        bundle["frame"]["width"] = 1920
    elif change == "orientation":
        bundle["screen"]["orientation"] = "landscape"
    else:
        bundle["status"] = "draft"
    cb.save_bundle(bundle, profile_root)
    assert not gp.view_profile(entry, bundle, "sess-A")["usable"]
    with pytest.raises(ValueError):
        resolve(profile_root, entry)
    with pytest.raises(ValueError):
        enable(profile_root, entry)


def test_missing_bundle_session_or_profile(tmp_path, profile_root):
    empty = tmp_path / "empty"
    assert gp.load_profile("arm-01", "wx_chat_list", empty) is None
    assert not empty.exists()
    with pytest.raises(ValueError):
        draft(empty)
    with pytest.raises(ValueError):
        draft(profile_root, session="wrong")
    with pytest.raises(ValueError):
        draft(profile_root, session=None)
    entry = enable(profile_root, make_tested(profile_root))
    assert not gp.view_profile(entry, None, "sess-A")["usable"]
    assert not gp.view_profile(entry, cb.load_bundle("arm-01", profile_root), None)["usable"]


def test_owner_version_and_device_isolation(profile_root):
    config = navigation()
    config["owner_plugin"] = "another-plugin"
    entry = draft(profile_root, config)
    entry = run_test(profile_root, entry, "search")
    entry = run_test(profile_root, entry, "top")
    entry = enable(profile_root, entry)
    with pytest.raises(ValueError):
        resolve(profile_root, entry)
    assert gp.resolve_profile("arm-01", "wx_chat_list", 1, owner_plugin="another-plugin",
                              current_session="sess-A", data_root=profile_root)
    for version in (True, "1", 2):
        with pytest.raises(ValueError):
            gp.resolve_profile("arm-01", "wx_chat_list", version, owner_plugin="another-plugin",
                               current_session="sess-A", data_root=profile_root)
    assert gp.load_profile("arm-02", "wx_chat_list", profile_root) is None
    for device, profile in (("../escape", "okay"), ("arm-01", "../escape")):
        with pytest.raises(ValueError):
            gp.load_profile(device, profile, profile_root)


def test_expected_revision_blocks_stale_test_and_enable(profile_root):
    entry = draft(profile_root)
    started = gp.begin_test("arm-01", "wx_chat_list", "search", expected_revision=1,
                           operator="tester", current_session="sess-A", data_root=profile_root)
    with pytest.raises(ValueError):
        gp.begin_test("arm-01", "wx_chat_list", "search", expected_revision=entry["revision"],
                      operator="tester", current_session="sess-A", data_root=profile_root)
    draft(profile_root)
    with pytest.raises(ValueError):
        gp.finish_test("arm-01", "wx_chat_list", expected_revision=started["revision"],
                       test_id=started["pending_test"]["id"], outcome="confirmed", evidence=evidence("search"),
                       operator="tester", current_session="sess-A", data_root=profile_root)
    entry = make_tested(profile_root)
    draft(profile_root)
    with pytest.raises(ValueError):
        enable(profile_root, entry)


def test_unknown_cannot_be_replayed_or_enabled(profile_root):
    entry = run_test(profile_root, draft(profile_root), "search", outcome="unknown")
    assert entry["test_blocked"]
    with pytest.raises(ValueError):
        run_test(profile_root, entry, "search")
    with pytest.raises(ValueError):
        enable(profile_root, entry)
    assert not gp.view_profile(entry, cb.load_bundle("arm-01", profile_root), "sess-A")["usable"]


def test_pending_test_after_crash_is_not_replayable(profile_root):
    entry = draft(profile_root)
    pending = gp.begin_test("arm-01", "wx_chat_list", "search", expected_revision=entry["revision"],
                           current_session="sess-A", operator="tester", data_root=profile_root)
    with pytest.raises(ValueError):
        run_test(profile_root, pending, "search")
    assert gp.load_profile("arm-01", "wx_chat_list", profile_root)["state"] == "testing"


@pytest.mark.parametrize("bad_proof", [
    {"motion": {"ok": True, "motion_verified": False}},
    {"motion": {"ok": True, "motion_verified": True, "outcome_hint": "unknown"}},
    {"main_ui_confirmed": False}, {"after": {"sha256": "before", "path": "/test/a"}},
    {"after": {"sha256": "after", "path": "/test/a", "top_anchor_found": True}},
    {"after": {"sha256": "after", "path": "/test/a"}},
])
def test_ack_or_missing_anchor_transition_cannot_pass(profile_root, bad_proof):
    with pytest.raises(ValueError):
        run_test(profile_root, draft(profile_root), "search", proof=evidence("search", **bad_proof))


def test_top_test_requires_search_proof(profile_root):
    with pytest.raises(ValueError):
        run_test(profile_root, draft(profile_root), "top")


def test_corrupt_archive_is_not_silently_overwritten(profile_root):
    draft(profile_root)
    path = profile_root / "devices" / "arm-01" / "gesture_profiles.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        gp.load_profile("arm-01", "wx_chat_list", profile_root)
    with pytest.raises(ValueError):
        draft(profile_root)
    assert path.read_text() == "{broken"


def test_empty_operator_rejected_and_no_profile_saved(profile_root):
    with pytest.raises(ValueError):
        gp.save_draft("arm-01", "wx_chat_list", navigation(), operator="",
                      current_session="sess-A", data_root=profile_root)
    assert gp.load_profile("arm-01", "wx_chat_list", profile_root) is None


def test_empty_legacy_navigation_stays_off():
    assert gp.validate_navigation({}) == {}


def test_profile_usable_is_not_production_ready(profile_root):
    entry = enable(profile_root, make_tested(profile_root))
    view = gp.view_profile(entry, cb.load_bundle("arm-01", profile_root), "sess-A")
    assert view["usable"] is True
    assert view["production_ready"] is False
    assert view["production_blocking_reasons"]
