"""Saved geometry uses temporary data only; no daemon, cameras or live services."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import multiprocessing
from pathlib import Path
import stat

import jsonschema
import pytest

from drivers import calibration_bundle as cb, saved_screen_gestures as saved, screen_gestures as sg
from tests.test_screen_gesture_driver import gesture_bundle

CONTRACT_DIR = Path(__file__).parents[1] / "api" / "contracts"
SCHEMA = json.loads((CONTRACT_DIR / "saved-screen-gesture.v1.schema.json").read_text())
TASK_SCHEMA = json.loads((CONTRACT_DIR / "screen-gesture.v1.schema.json").read_text())


def definition(action="swipe", **changes):
    return {
        "label": "Home", "operator": "调试台", "action": action,
        **({"point": {"x": .5, "y": .5}, "interval_ms": 120} if action == "double_tap" else
           {"start": {"x": .5, "y": .75}, "end": {"x": .5, "y": .3}}),
        **changes,
    }


def archive_path(root, device="arm-01"):
    return root / "devices" / device / saved.ARCHIVE_FILENAME


@pytest.fixture()
def rig(tmp_path):
    bundle = gesture_bundle()
    cb.save_bundle(bundle, tmp_path)
    return tmp_path, bundle, bundle["daemonSession"]


def validate_view(entry):
    jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker()).validate(entry)
    jsonschema.validate(entry["payload"], {"$defs": TASK_SCHEMA["$defs"], "$ref": "#/$defs/payload"})
    assert entry["payload"]["binding"] == entry["binding"]


@pytest.mark.parametrize("action", ["swipe", "double_tap"])
def test_save_and_read_are_exact_task_payloads_without_calibration_writes(rig, action):
    root, bundle, session = rig
    calibration_before = cb.bundle_path("arm-01", root).read_bytes()
    body = definition(action)
    original = deepcopy(body)
    entry = saved.save_gesture("arm-01", body, bundle, root)
    validate_view(entry)
    assert body == original
    assert entry["gesture_id"].startswith("gesture-")
    assert entry["version"] == 1
    assert entry["created_by"] == entry["updated_by"] == "调试台"
    assert entry["effect_status"] == "unverified"
    assert entry["effect_status_display"] == "手机效果未核验"
    assert entry["usable"] and entry["blocking_reasons"] == []
    assert entry["binding"] == sg.binding_snapshot(bundle)
    timing = ({"duration_ms": 1349} if action == "swipe" else {"interval_ms": 120})
    points = ({key: body[key] for key in ("start", "end")} if action == "swipe" else {"point": body["point"]})
    assert entry["payload"] == {"action": action, "binding": entry["binding"], **points, **timing}
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert saved.list_gestures("arm-01", bundle, session, root) == [entry]
    assert saved.get_gesture("arm-01", entry["gesture_id"], bundle, session, root) == entry
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert cb.bundle_path("arm-01", root).read_bytes() == calibration_before
    assert not (root / "devices" / "arm-01" / "gesture_profiles.json").exists()


def test_empty_list_and_missing_item_are_read_only(tmp_path):
    assert saved.list_gestures("arm-01", None, None, tmp_path) == []
    with pytest.raises(saved.SavedGestureError) as error:
        saved.get_gesture("arm-01", "missing", None, None, tmp_path)
    assert (error.value.code, error.value.http_status) == ("saved_gesture_not_found", 404)
    assert list(tmp_path.iterdir()) == []


def test_label_is_opaque_not_a_path_or_success_claim(rig):
    root, bundle, _ = rig
    label = "../Home / Back <script> 中文"
    entry = saved.save_gesture("arm-01", definition(label=label), bundle, root)
    assert entry["label"] == label
    assert entry["effect_status"] == "unverified"
    assert entry["gesture_id"] != label
    assert not (root / "Home").exists()


def test_update_preserves_creation_audit_and_materialized_payload(rig):
    root, bundle, session = rig
    first = saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    materialized = deepcopy(first["payload"])
    second = saved.save_gesture("arm-01", definition(
        "double_tap", gesture_id="home", expected_version=1, label="Back", operator="second", interval_ms=350,
    ), bundle, root)
    assert second["version"] == 2
    assert second["created_by"] == first["created_by"]
    assert second["created_at"] == first["created_at"]
    assert second["updated_by"] == "second"
    assert second["updated_at"] >= first["updated_at"]
    assert first["payload"] == materialized
    assert second["payload"]["interval_ms"] == 350
    validate_view(second)
    assert saved.get_gesture("arm-01", "home", bundle, session, root) == second
    second["payload"]["point"]["x"] = .1
    assert saved.get_gesture("arm-01", "home", bundle, session, root)["payload"]["point"]["x"] == .5


@pytest.mark.parametrize("version", [None, 2, 999])
def test_update_requires_exact_version_and_does_not_overwrite(rig, version):
    root, bundle, _ = rig
    saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    before = archive_path(root).read_bytes()
    body = definition(gesture_id="home")
    if version is not None:
        body["expected_version"] = version
    with pytest.raises(saved.SavedGestureError) as error:
        saved.save_gesture("arm-01", body, bundle, root)
    assert error.value.code == "saved_gesture_version_conflict"
    assert archive_path(root).read_bytes() == before


def test_update_missing_id_does_not_create(rig):
    root, bundle, _ = rig
    with pytest.raises(saved.SavedGestureError) as error:
        saved.save_gesture("arm-01", definition(gesture_id="absent", expected_version=1), bundle, root)
    assert error.value.code == "saved_gesture_not_found"
    assert not archive_path(root).exists()


@pytest.mark.parametrize("changes", [
    {"operator": ""}, {"operator": "  "}, {"operator": None}, {"label": ""},
    {"label": 7}, {"label": "a" * 101}, {"action": "click"}, {"confirmed": True},
    {"hoverZ": 2}, {"tapDepth": 3}, {"tapFeed": 1500}, {"binding": {}},
    {"duration_ms": 700}, {"durationMs": 700}, {"feed": 1000},
    {"start": {"cameraX": 100, "cameraY": 200, "frameW": 1280, "frameH": 720}},
    {"start": {"x": True, "y": .5}}, {"start": {"x": float("nan"), "y": .5}},
    {"start": {"x": float("inf"), "y": .5}}, {"start": {"x": ".5", "y": .5}},
    {"start": {"x": -1, "y": .5}}, {"start": {"x": .5, "y": .5, "source": "screen_normalized"}},
    {"expected_version": 1}, {"gesture_id": "home", "expected_version": True},
    {"gesture_id": "home", "expected_version": 1.0}, {"gesture_id": "home", "expected_version": 0},
])
def test_invalid_save_is_rejected_without_archive(rig, changes):
    root, bundle, _ = rig
    with pytest.raises(saved.SavedGestureError):
        saved.save_gesture("arm-01", definition(**changes), bundle, root)
    assert not archive_path(root).exists()


@pytest.mark.parametrize("field", ["operator", "label", "action", "start", "end"])
def test_missing_required_field_is_rejected(rig, field):
    root, bundle, _ = rig
    body = definition()
    body.pop(field)
    with pytest.raises(saved.SavedGestureError):
        saved.save_gesture("arm-01", body, bundle, root)
    assert not archive_path(root).exists()


@pytest.mark.parametrize("interval", [50, 60, 120, 350, 500])
def test_double_tap_keeps_valid_interval(rig, interval):
    root, bundle, _ = rig
    entry = saved.save_gesture("arm-01", definition("double_tap", interval_ms=interval), bundle, root)
    assert entry["payload"]["interval_ms"] == interval
    validate_view(entry)


@pytest.mark.parametrize("interval", [49, 51, 125, 501, 120.0, True, "120", None, float("nan")])
def test_double_tap_rejects_invalid_interval(rig, interval):
    root, bundle, _ = rig
    with pytest.raises(saved.SavedGestureError, match="步长 10"):
        saved.save_gesture("arm-01", definition("double_tap", interval_ms=interval), bundle, root)


@pytest.mark.parametrize("bad_id", ["../outside", "/tmp/outside", "a/b", ".", "..", "%2e%2e", "", "a" * 65])
def test_identifiers_never_escape_device_archive(rig, bad_id):
    root, bundle, session = rig
    for operation in [
        lambda: saved.list_gestures(bad_id, bundle, session, root),
        lambda: saved.get_gesture("arm-01", bad_id, bundle, session, root),
        lambda: saved.save_gesture("arm-01", definition(gesture_id=bad_id), bundle, root),
    ]:
        with pytest.raises(saved.SavedGestureError):
            operation()
    assert not archive_path(root).exists()


def test_same_id_is_scoped_to_device(rig):
    root, bundle, session = rig
    first = saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    other_bundle = deepcopy(bundle)
    other_bundle["deviceId"] = "arm-02"
    assert saved.list_gestures("arm-02", other_bundle, session, root) == []
    second = saved.save_gesture("arm-02", definition("double_tap", gesture_id="home"), other_bundle, root)
    assert first["payload"] != second["payload"]
    assert saved.get_gesture("arm-01", "home", bundle, session, root) == first
    assert saved.get_gesture("arm-02", "home", other_bundle, session, root) == second
    with pytest.raises(saved.SavedGestureError):
        saved.save_gesture("arm-02", definition(), bundle, root)


@pytest.mark.parametrize("change", [
    lambda b: b.update(status="draft"),
    lambda b: b.update(daemonSession="restarted"),
    lambda b: b.update(cameraDeviceUniqueId="replacement"),
    lambda b: b.update(bindingRevision=8),
    lambda b: b["z"].update(tapDepth=7),
    lambda b: b["xy"]["robotToScreen"].update(c=430),
    lambda b: b["camera"]["cameraToScreen"].update(maxErrorPx=2),
    lambda b: b.update(safePark={"parkX": 1}),
])
def test_calibration_changes_block_without_rebinding_or_writing(rig, change):
    root, bundle, session = rig
    entry = saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    before = archive_path(root).read_bytes()
    changed = deepcopy(bundle)
    change(changed)
    stale = saved.get_gesture("arm-01", "home", changed, session, root)
    assert not stale["usable"] and stale["blocking_reasons"]
    assert stale["payload"] == entry["payload"] and stale["binding"] == entry["binding"]
    with pytest.raises(saved.SavedGestureError):
        saved.save_gesture("arm-01", definition(gesture_id="home", expected_version=1), changed, root)
    assert archive_path(root).read_bytes() == before
    validate_view(stale)


@pytest.mark.parametrize("session", [None, "", "new-session"])
def test_unknown_or_changed_session_is_not_usable(rig, session):
    root, bundle, _ = rig
    saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    view = saved.get_gesture("arm-01", "home", bundle, session, root)
    assert not view["usable"] and view["effect_status"] == "unverified"


def test_valid_saved_geometry_is_retained_when_bundle_is_missing(rig):
    root, bundle, session = rig
    entry = saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    stale = saved.get_gesture("arm-01", "home", None, session, root)
    assert not stale["usable"] and stale["payload"] == entry["payload"]


@pytest.mark.parametrize("body", [
    definition(start={"x": 0, "y": .5}),
    definition(end={"x": .5, "y": .75}),
])
def test_unresolvable_geometry_is_not_saved(rig, body):
    root, bundle, _ = rig
    with pytest.raises(saved.SavedGestureError):
        saved.save_gesture("arm-01", body, bundle, root)
    assert not archive_path(root).exists()


@pytest.mark.parametrize("corrupt", [
    lambda a: a.update(schema_version=True),
    lambda a: a.update(device_id="arm-02"),
    lambda a: a["entries"]["home"].update(version=True),
    lambda a: a["entries"]["home"].update(device_id="arm-02"),
    lambda a: a["entries"]["home"]["payload"]["binding"].update(binding_revision=True),
    lambda a: a["entries"]["home"]["payload"].update(tapDepth=8),
    lambda a: a["entries"]["home"].update(effect_status="confirmed"),
    lambda a: a["entries"]["home"].pop("created_by"),
    lambda a: a["entries"]["home"]["payload"]["start"].update(x=float("nan")),
])
def test_corrupt_archive_is_not_partially_returned_or_overwritten(rig, corrupt):
    root, bundle, session = rig
    saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    path = archive_path(root)
    content = json.loads(path.read_text())
    corrupt(content)
    path.write_text(json.dumps(content))
    before = path.read_bytes()
    for operation in [
        lambda: saved.list_gestures("arm-01", bundle, session, root),
        lambda: saved.get_gesture("arm-01", "home", bundle, session, root),
        lambda: saved.save_gesture("arm-01", definition(gesture_id="new"), bundle, root),
    ]:
        with pytest.raises(saved.SavedGestureError) as error:
            operation()
        assert error.value.code == "saved_gesture_archive_invalid"
    assert path.read_bytes() == before


@pytest.mark.parametrize("text", ['{broken', '{"schema_version":1,"schema_version":1}', "null", "[]"])
def test_undecodable_or_duplicate_json_is_never_empty_or_overwritten(rig, text):
    root, bundle, session = rig
    path = archive_path(root)
    path.write_text(text)
    with pytest.raises(saved.SavedGestureError):
        saved.list_gestures("arm-01", bundle, session, root)
    with pytest.raises(saved.SavedGestureError):
        saved.save_gesture("arm-01", definition(), bundle, root)
    assert path.read_text() == text


@pytest.mark.parametrize("target", ["device", "archive", "lock"])
def test_symlink_paths_are_rejected(tmp_path, target):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    device = root / "devices" / "arm-01"
    if target == "device":
        device.parent.mkdir(parents=True)
        device.symlink_to(outside, target_is_directory=True)
    else:
        device.mkdir(parents=True)
        link = archive_path(root) if target == "archive" else archive_path(root).with_suffix(".lock")
        link.symlink_to(outside / "must-not-create")
    for operation in [
        lambda: saved.list_gestures("arm-01", gesture_bundle(), "session", root),
        lambda: saved.save_gesture("arm-01", definition(), gesture_bundle(), root),
    ]:
        with pytest.raises(saved.SavedGestureError):
            operation()
    assert list(outside.iterdir()) == []


def test_atomic_replace_failure_preserves_previous_archive(rig, monkeypatch):
    root, bundle, _ = rig
    saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    before = archive_path(root).read_bytes()
    def failed_replace(*_):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(saved.os, "replace", failed_replace)
    with pytest.raises(OSError):
        saved.save_gesture("arm-01", definition(gesture_id="home", expected_version=1), bundle, root)
    assert archive_path(root).read_bytes() == before
    assert not list(archive_path(root).parent.glob(".saved-screen-gestures-*"))


def test_file_and_directory_are_synced(rig, monkeypatch):
    root, bundle, _ = rig
    sync = saved.os.fsync
    kinds = []
    def observed(fd):
        kinds.append("directory" if stat.S_ISDIR(saved.os.fstat(fd).st_mode) else "file")
        return sync(fd)
    monkeypatch.setattr(saved.os, "fsync", observed)
    saved.save_gesture("arm-01", definition(), bundle, root)
    assert kinds == ["file", "directory"]


def _attempt_update(root, bundle):
    try:
        return saved.save_gesture("arm-01", definition(gesture_id="home", expected_version=1), bundle, root)["version"]
    except saved.SavedGestureError as error:
        return error.code


@pytest.mark.parametrize("processes", [False, True])
def test_competing_writers_cannot_both_update_same_version(rig, processes):
    root, bundle, session = rig
    saved.save_gesture("arm-01", definition(gesture_id="home"), bundle, root)
    if processes:
        with multiprocessing.get_context("spawn").Pool(2) as pool:
            results = pool.starmap(_attempt_update, [(root, bundle)] * 2)
    else:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: _attempt_update(root, bundle), range(2)))
    assert sorted(map(str, results)) == ["2", "saved_gesture_version_conflict"]
    assert saved.get_gesture("arm-01", "home", bundle, session, root)["version"] == 2
