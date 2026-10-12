"""模板登记只消费平台预览；全部相机、证据、档案位于 fake/tmp。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import cv2
import jsonschema
import numpy as np
import pytest

import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")
pytestmark = [needs_wx_example]


from drivers import gesture_program as gp
from runtime import automatic_gestures as ag
from runtime.util import iso, utc_now
from tests.helpers import op_context
from tests.test_automatic_gesture_api import (
    _client, _program_fixture, _run_fixture, _seed_calibration,
)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    bundle = _seed_calibration(tmp_path)
    root = tmp_path / "data"
    run = _run_fixture(bundle, root)
    seed = run["action_results"][0]
    run["action_results"] = [dict(deepcopy(seed), gesture_id=key,
                                   program=_program_fixture(key))
                             for key in ("scroll_up", "scroll_down", "system_home")]
    ag.publish_run_results("arm-01", run, data_root=root)
    image = np.zeros((720, 1280), dtype=np.uint8)
    image[100:601, 100:1201] = 173
    image[200:260, 300:900] = 69
    data = cv2.imencode(".png", image)[1].tobytes()

    class Camera:
        camera_id = device_id = "cam-01"
        calls = 0

        def capture_sync(self):
            self.calls += 1
            return {"image": data, "sha256": hashlib.sha256(data).hexdigest(),
                    "captured_at": iso(utc_now()), "width": 1280,
                    "height": 720, "frame_seq": self.calls}

    with _client(tmp_path, n_devices=2) as client:
        runtime = client.app.state.runtime
        camera = Camera()
        monkeypatch.setattr(runtime.devices, "camera_broker", lambda device: camera)
        monkeypatch.setattr(runtime.devices, "binding_view", lambda device: {
            "binding": {"camera_id": "cam-01", "camera_device_unique_id": "uvc-01"},
            "binding_revision": 7, "daemon_session": bundle["daemonSession"],
            "binding_enabled": True})
        yield client, runtime, bundle, camera, tmp_path


def preview(scene, kind="top"):
    response = scene[0].post("/v1/devices/arm-01/navigation-template-previews",
                             json={"kind": kind, **op_context()})
    assert response.status_code == 200, response.text
    return response.json()


def registration(p, **updates):
    count = 1 if p["kind"] == "top" else 3
    return {"preview_id": p["preview_id"], "expected_version": 0,
            "confirmed": True, "restore_target": None,
            "anchor_regions": [{"left_px": 100 + i * 200, "top_px": 400,
                                "width_px": 100, "height_px": 100}
                               for i in range(count)], **op_context(), **updates}


def register(scene, p, **updates):
    return scene[0].put(f'/v1/devices/arm-01/navigation-templates/{p["kind"]}',
                       json=registration(p, **updates))


def validate(value, definition):
    schema = json.loads((Path(__file__).parents[1] / "api/contracts/"
                         "navigation-template.v1.schema.json").read_text())
    jsonschema.Draft202012Validator(
        {**schema, "$ref": f"#/$defs/{definition}"},
        format_checker=jsonschema.FormatChecker()).validate(value)


@pytest.mark.parametrize("kind,action", [("top", "return_top"), ("launcher", "home")])
def test_preview_register_real_crop_and_navigation_eligible(scene, kind, action):
    client, runtime, bundle, camera, tmp = scene
    p = preview(scene, kind)
    validate(p, "previewResponse")
    assert p["binding"] == client.get("/v1/devices/arm-01/automatic-gestures").json()["binding"]
    assert camera.calls == 3
    response = register(scene, p)
    assert response.status_code == 200, response.text
    template = response.json()
    validate(template, "templateRecord")
    for anchor in template["anchor_evidence"]:
        assert anchor["frame_evidence"] == p["frame_evidence"]
        crop = runtime.evidence.read_bytes(runtime.evidence.get(anchor["evidence_id"]))
        assert hashlib.sha256(crop).hexdigest() == anchor["crop_sha256"]
        pixels = cv2.imdecode(np.frombuffer(crop, np.uint8), cv2.IMREAD_GRAYSCALE)
        assert pixels.shape == (100, 100)
        assert np.all(pixels == 173)
        for ref in anchor["frame_evidence"]:
            record = runtime.evidence.get(ref["evidence_id"])
            assert record["device_id"] == "arm-01"
            assert record["task_id"] == f"navtpl-{kind}"
            assert hashlib.sha256(runtime.evidence.read_bytes(record)).hexdigest() == ref["sha256"]
    frame = gp.load_coordinate_frame("arm-01", tmp / "data")
    view = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                              bundle=bundle, frame=frame, data_root=tmp / "data",
                              evidence_root=runtime.config.evidence_dir,
                              evidence_get=runtime.evidence.get)
    assert view["actions"][action]["usable"], view
    path = tmp / "data/devices/arm-01/navigation_templates" / f"{kind}.json"
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert client.get(f"/v1/devices/arm-01/navigation-templates/{kind}").json() == template
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert camera.calls == 3
    from tests.test_gesture_plugins import load_module, WX_SOURCE
    plugin = load_module("navigation_template_crop_consumer", WX_SOURCE)
    plane = plugin._auto_nav_calibration_plane("arm-01", data_root=tmp / "data")
    for anchor in template["anchor_evidence"]:
        patch = plugin._auto_nav_template_patch(anchor, kind, plane,
                                                binding=template["binding"],
                                                evidence_root=runtime.config.evidence_dir,
                                                evidence_get=runtime.evidence.get)
        stored = runtime.evidence.read_bytes(runtime.evidence.get(anchor["evidence_id"]))
        np.testing.assert_array_equal(
            patch, cv2.imdecode(np.frombuffer(stored, np.uint8), cv2.IMREAD_GRAYSCALE))


@pytest.mark.parametrize("updates", [
    {"binding": {"calibration_fingerprint": "a" * 64}},
    {"frame_evidence": [{"evidence_id": "forged"}]},
    {"evidence_id": "forged"}, {"image": "data:image/png;base64,AAAA"},
    {"file_path": "/tmp/image.png"}, {"url": "http://127.0.0.1:8788"},
    {"kind": "top"}, {"preview_id": "../../outside"},
    {"confirmed": False}, {"confirmed": 1},
    {"expected_version": None}, {"expected_version": "0"},
    {"expected_version": False}, {"expected_version": 0.0},
])
def test_untrusted_registration_fields_rejected(scene, updates):
    p = preview(scene)
    response = register(scene, p, **updates)
    assert response.status_code == 400, response.text
    assert not (scene[4] / "data/devices/arm-01/navigation_templates/top.json").exists()


@pytest.mark.parametrize("field", ["expected_version", "preview_id", "confirmed", "restore_target"])
def test_registration_requires_all_explicit_fields(scene, field):
    p = preview(scene)
    body = registration(p)
    del body[field]
    response = scene[0].put("/v1/devices/arm-01/navigation-templates/top", json=body)
    assert response.status_code == 400


@pytest.mark.parametrize("regions", [
    [], [{"left_px": -1, "top_px": 0, "width_px": 10, "height_px": 10}],
    [{"left_px": 0, "top_px": 0, "width_px": 0, "height_px": 10}],
    [{"left_px": 0, "top_px": 0, "width_px": 1081, "height_px": 10}],
    [{"left_px": 0, "top_px": 2320, "width_px": 1, "height_px": 1}],
    [{"left_px": 0.0, "top_px": 0, "width_px": 10, "height_px": 10}],
    [{"left_px": True, "top_px": 0, "width_px": 10, "height_px": 10}],
    [{"left_px": 0, "top_px": 0, "width_px": 10, "height_px": 10}] * 2,
    [{"left_px": i * 20, "top_px": 0, "width_px": 10, "height_px": 10} for i in range(4)],
])
def test_invalid_screen_regions_rejected(scene, regions):
    response = register(scene, preview(scene), anchor_regions=regions)
    assert response.status_code == 400, response.text


def test_preview_expiration_and_wrong_kind_device_or_reference(scene, monkeypatch):
    from datetime import timedelta
    from adapters.uinav import navigation_templates as nt
    p = preview(scene)
    for device, kind, ref in [("arm-02", "top", p["preview_id"]),
                              ("arm-01", "launcher", p["preview_id"]),
                              ("arm-01", "top", "00000000-0000-4000-8000-000000000001")]:
        response = scene[0].put(f"/v1/devices/{device}/navigation-templates/{kind}",
                                json=registration(p, preview_id=ref))
        assert response.status_code == 409, response.text
    now = nt._now()
    monkeypatch.setattr(nt, "_now", lambda: now + timedelta(seconds=61))
    assert register(scene, p).status_code == 409


@pytest.mark.parametrize("change", ["bundle", "frame", "rules", "profile", "device"])
def test_changed_binding_rejected(scene, monkeypatch, change):
    from drivers import calibration_bundle as cb
    p = preview(scene)
    _, runtime, bundle, _, tmp = scene
    root = tmp / "data"
    if change == "bundle":
        bundle["z"]["tapDepth"] += 0.1
        cb.save_bundle(bundle, root)
    elif change == "frame":
        frame = gp.load_coordinate_frame("arm-01", root)
        frame["density"] += 0.1
        frame["coordinate_frame_hash"] = gp.coordinate_frame_hash(frame)
        gp.save_coordinate_frame("arm-01", frame, data_root=root)
    elif change == "rules":
        # 开关已删：设置文件不再由 fixture 预创建，直接写入规则版本
        path = ag.settings_path("arm-01", root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"gesture_rules_version": "gesture-rules.v2"}))
    elif change == "profile":
        path = ag.profile_path("arm-01", root)
        profile = json.loads(path.read_text())
        profile["version"] += 1
        path.write_text(json.dumps(profile))
    else:
        live = runtime.devices.binding_view("arm-01")
        live["binding_revision"] += 1
        monkeypatch.setattr(runtime.devices, "binding_view", lambda device: live)
    assert register(scene, p).status_code == 409
    assert not (root / "devices/arm-01/navigation_templates/top.json").exists()


@pytest.mark.parametrize("corruption", ["bytes", "missing", "device", "path", "camera"])
def test_platform_evidence_corruption_fails_closed(scene, corruption):
    p = preview(scene)
    runtime, tmp = scene[1], scene[4]
    record = runtime.evidence.get(p["frame_evidence"][1]["evidence_id"])
    path = tmp / "evidence" / record["file_path"]
    if corruption == "bytes":
        path.write_bytes(b"broken")
    elif corruption == "missing":
        path.unlink()
    else:
        record[{"device": "device_id", "path": "file_path", "camera": "camera_id"}[corruption]] = {
            "device": "arm-02", "path": "../../outside.png", "camera": "other"}[corruption]
        runtime.store.save_evidence(record)
    assert register(scene, p).status_code == 409
    assert not (tmp / "data/devices/arm-01/navigation_templates/top.json").exists()


def test_cas_allows_one_writer_and_get_does_not_repair(scene):
    from concurrent.futures import ThreadPoolExecutor
    from adapters.uinav.navigation_templates import NavigationTemplates
    from runtime.errors import ApiError
    p = preview(scene)

    def write():
        try:
            NavigationTemplates(scene[1], "arm-01").register("top", registration(p))
            return 200
        except ApiError as exc:
            assert exc.key == "idempotency_conflict"
            return 409

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: write(), range(2))) == [200, 409]
    assert register(scene, p, expected_version=1).json()["version"] == 2
    assert register(scene, p, expected_version=1).status_code == 409
    path = scene[4] / "data/devices/arm-01/navigation_templates/top.json"
    path.write_text('{"damaged":true}')
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert scene[0].get("/v1/devices/arm-01/navigation-templates/top").status_code == 409
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert register(scene, p, expected_version=2).status_code == 409


def test_unknown_state_permissions_and_invalid_ids_unchanged(scene):
    client, runtime, _, camera, tmp = scene
    runtime.devices._devices["arm-01"].update(status="quarantined", pause_reason="结果未知")
    before = deepcopy(runtime.devices._devices["arm-01"])
    p = preview(scene)
    assert register(scene, p).status_code == 200
    assert runtime.devices._devices["arm-01"]["status"] == before["status"]
    assert runtime.devices._devices["arm-01"]["pause_reason"] == before["pause_reason"]
    assert register(scene, p, role="readonly").status_code == 403
    for kind in ("unknown", "top-copy", "..", "%2e%2e%2Foutside"):
        assert client.put(f"/v1/devices/arm-01/navigation-templates/{kind}",
                          json=registration(p)).status_code in (400, 404, 405)
    for body in ({"kind": "bad"}, {"kind": "top", "binding": {}}, []):
        assert client.post("/v1/devices/arm-01/navigation-template-previews", json=body).status_code == 400
    assert camera.calls == 3


def test_preview_frames_must_be_fresh_and_binding_stable(scene, monkeypatch):
    camera = scene[3]
    original = camera.capture_sync

    def stale():
        frame = original()
        frame["captured_at"] = "2000-01-01T00:00:00Z"
        return frame

    monkeypatch.setattr(camera, "capture_sync", stale)
    response = scene[0].post("/v1/devices/arm-01/navigation-template-previews",
                             json={"kind": "top", **op_context()})
    assert response.status_code == 409
    assert not list((scene[4] / "data/devices/arm-01/navigation_templates").glob("previews/*.json"))


@pytest.mark.parametrize("change", [None, "source_stale", "source_repeat", "camera_id",
                                     "device_id", "device_unique_id", "binding_revision",
                                     "source_legacy_mode"])
def test_real_camera_metadata_provenance(scene, monkeypatch, change):
    camera = scene[3]
    original = camera.capture_sync

    def source_frame():
        frame = original()
        frame.update(source_frame_seq=frame.pop("frame_seq"),
                     source_captured_at=frame["captured_at"], camera_id="cam-01",
                     device_id="arm-01", device_unique_id="uvc-01", binding_revision=7,
                     source_legacy_mode=False)
        if change == "source_stale":
            frame["source_captured_at"] = "2000-01-01T00:00:00Z"
        elif change == "source_repeat":
            frame["source_frame_seq"] = 1
        elif change is not None:
            frame[change] = True if change == "source_legacy_mode" else "wrong"
        return frame

    monkeypatch.setattr(camera, "capture_sync", source_frame)
    response = scene[0].post("/v1/devices/arm-01/navigation-template-previews",
                             json={"kind": "top", **op_context()})
    if change is None:
        assert response.status_code == 200, response.text
        assert [r["frame_seq"] for r in response.json()["frame_evidence"]] == [1, 2, 3]
    else:
        assert response.status_code == 409, change
        assert not list((scene[4] / "data/devices/arm-01/navigation_templates").glob("previews/*.json"))


def test_binding_changes_while_capturing_abort_preview(scene, monkeypatch):
    camera, runtime = scene[3], scene[1]
    original = camera.capture_sync
    live = runtime.devices.binding_view("arm-01")
    monkeypatch.setattr(runtime.devices, "binding_view", lambda device: live)

    def capture_and_rebind():
        frame = original()
        if camera.calls == 2:
            live["binding_revision"] += 1
        return frame

    monkeypatch.setattr(camera, "capture_sync", capture_and_rebind)
    response = scene[0].post("/v1/devices/arm-01/navigation-template-previews",
                             json={"kind": "top", **op_context()})
    assert response.status_code == 409
    assert not list((scene[4] / "data/devices/arm-01/navigation_templates").glob("previews/*.json"))


@pytest.mark.parametrize("fault", ["source_replaced", "all_deleted", "crop_deleted"])
def test_registered_evidence_integrity_revokes_navigation_and_matching(scene, fault):
    from tests.test_gesture_plugins import load_module, WX_SOURCE
    client, runtime, _, _, tmp = scene
    template = register(scene, preview(scene)).json()
    plugin = load_module("registered_template_integrity", WX_SOURCE)
    anchor = template["anchor_evidence"][0]
    if fault == "source_replaced":
        record = runtime.evidence.get(anchor["frame_evidence"][0]["evidence_id"])
        image = np.random.default_rng(123).integers(0, 256, (720, 1280), dtype=np.uint8)
        (tmp / "evidence" / record["file_path"]).write_bytes(cv2.imencode(".png", image)[1].tobytes())
    elif fault == "all_deleted":
        for path in (tmp / "evidence/navtpl-top").glob("*.png"):
            path.unlink()
    else:
        record = runtime.evidence.get(anchor["evidence_id"])
        (tmp / "evidence" / record["file_path"]).unlink()
    view = client.get("/v1/devices/arm-01/automatic-navigation/wx_chat_list_auto").json()
    assert not view["actions"]["return_top"]["usable"]
    assert not view["actions"]["return_top"]["top_proof_available"]
    anchors = plugin._auto_nav_anchors(template, "arm-01", data_root=tmp / "data",
                                        evidence_root=tmp / "evidence")
    assert not anchors
    if fault == "source_replaced":
        from adapters.uinav import automatic_navigation as nav
        matrix, size = plugin._auto_nav_calibration_plane("arm-01", data_root=tmp / "data")
        calibrated = cv2.warpPerspective(image, matrix, size)
        region = anchor["region"]
        x, y, w, h = (region[k] for k in ("left_px", "top_px", "width_px", "height_px"))
        # This is precisely the old recropping exploit: the tampered current frame
        # matches a template recropped from that same file with correlation 1.0.
        unsafe = [{**region, "template": calibrated[y:y + h, x:x + w]}]
        assert nav.verify_top_template(unsafe, calibrated, defaults=nav.load_defaults())["matched"]
        assert not nav.verify_top_template(anchors, calibrated, defaults=nav.load_defaults())["matched"]


@pytest.mark.parametrize("kind,action", [("top", "return_top"), ("launcher", "home")])
@pytest.mark.parametrize("fault", [
    "source_missing", "crop_changed", "source_device", "crop_device", "source_camera",
    "source_path", "crop_path", "source_task", "source_kind", "source_timestamp",
    "source_symlink", "crop_symlink", "source_dimensions", "crop_dimensions",
    "source_ref_camera", "source_ref_binding", "source_ref_path", "crop_ref_path",
    "source_record_missing", "crop_record_missing",
])
def test_registered_evidence_corruption_blocks_every_consumer(scene, kind, action, fault):
    from runtime.errors import ApiError
    from tests.test_gesture_plugins import load_module, WX_SOURCE

    client, runtime, bundle, _, tmp = scene
    template = register(scene, preview(scene, kind)).json()
    anchor = template["anchor_evidence"][0]
    source_ref = anchor["frame_evidence"][1]
    is_source = fault.startswith("source")
    record = runtime.evidence.get(source_ref["evidence_id"] if is_source else anchor["evidence_id"])
    path = runtime.config.evidence_dir / record["file_path"]
    if fault == "source_missing":
        path.unlink()
    elif fault == "crop_changed":
        path.write_bytes(cv2.imencode(".png", np.zeros((100, 100), np.uint8))[1].tobytes())
    elif fault.endswith("symlink"):
        foreign = tmp / "foreign.png"
        foreign.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(foreign)
    elif fault.endswith("dimensions"):
        data = cv2.imencode(".png", np.zeros((17, 23), np.uint8))[1].tobytes()
        path.write_bytes(data)
        record["sha256"] = hashlib.sha256(data).hexdigest()
        if is_source:
            for item in template["anchor_evidence"]:
                item["frame_evidence"][1]["sha256"] = record["sha256"]
        else:
            anchor["crop_sha256"] = record["sha256"]
        runtime.store.save_evidence(record)
    elif fault == "source_ref_camera":
        source_ref["camera_id"] = "cam-02"
    elif fault == "source_ref_binding":
        source_ref["calibration_fingerprint"] = "0" * 64
    elif fault.endswith("ref_path"):
        (source_ref if is_source else anchor)["evidence_id"] = "../../foreign"
    elif fault.endswith("record_missing"):
        runtime.store._write("DELETE FROM evidence WHERE evidence_id=?", (record["evidence_id"],))
    else:
        field, value = {
            "device": ("device_id", "arm-02"), "camera": ("camera_id", "cam-02"),
            "path": ("file_path", "../../foreign.png"), "task": ("task_id", "foreign-task"),
            "kind": ("kind", "file"), "timestamp": ("captured_at", "2000-01-01T00:00:00Z"),
        }[fault.split("_", 1)[1]]
        record[field] = value
        runtime.store.save_evidence(record)
    template_path = tmp / "data/devices/arm-01/navigation_templates" / f"{kind}.json"
    template_path.write_text(json.dumps(template))
    before = template_path.read_bytes(), template_path.stat().st_mtime_ns
    view = client.get("/v1/devices/arm-01/automatic-navigation/wx_chat_list_auto").json()
    assert not view["actions"][action]["usable"], fault
    assert "navigation_template_invalid" in {r["code"] for r in view["actions"][action]["blocking_reasons"]}
    if kind == "top":
        assert not view["actions"][action]["top_proof_available"]
    ref = {key: view[key] for key in ("profile_id", "version", "contract_version")}
    with pytest.raises(ApiError, match="模板"):
        ag.resolve_navigation_action(
            "arm-01", ref, view["binding"], action, bundle=bundle,
            frame=gp.load_coordinate_frame("arm-01", tmp / "data"), data_root=tmp / "data",
            evidence_root=runtime.config.evidence_dir, evidence_get=runtime.evidence.get)
    plugin = load_module("corrupt_registered_consumer", WX_SOURCE)
    assert plugin._auto_nav_anchors(
        template, "arm-01", data_root=tmp / "data", evidence_root=runtime.config.evidence_dir,
        evidence_get=runtime.evidence.get) == []
    assert (template_path.read_bytes(), template_path.stat().st_mtime_ns) == before


def test_consumer_uses_saved_crop_without_recropping(scene, monkeypatch):
    from tests.test_gesture_plugins import load_module, WX_SOURCE

    template = register(scene, preview(scene)).json()
    runtime, tmp = scene[1], scene[4]
    entry = template["anchor_evidence"][0]
    expected = cv2.imdecode(np.frombuffer(
        runtime.evidence.read_bytes(runtime.evidence.get(entry["evidence_id"])), np.uint8), 0)
    plugin = load_module("saved_crop_only_consumer", WX_SOURCE)

    def cannot_recrop(*args, **kwargs):
        pytest.fail("Matching must load the registered crop, never warp the source again")

    monkeypatch.setattr(cv2, "warpPerspective", cannot_recrop)
    anchors = plugin._auto_nav_anchors(
        template, "arm-01", data_root=tmp / "data", evidence_root=runtime.config.evidence_dir,
        evidence_get=runtime.evidence.get)
    np.testing.assert_array_equal(anchors[0]["template"], expected)


def test_evidence_root_is_required_and_never_inferred_from_data_root(scene):
    _, runtime, bundle, _, tmp = scene
    assert register(scene, preview(scene)).status_code == 200
    kwargs = {"bundle": bundle, "frame": gp.load_coordinate_frame("arm-01", tmp / "data"),
              "data_root": tmp / "data", "evidence_get": runtime.evidence.get}
    missing = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID, **kwargs)
    assert not missing["actions"]["return_top"]["usable"]
    assert not missing["actions"]["return_top"]["top_proof_available"]
    valid = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                               evidence_root=runtime.config.evidence_dir, **kwargs)
    assert valid["actions"]["return_top"]["usable"]


def test_launcher_counts_restore_target_and_preview_persistence(scene):
    from adapters.uinav.navigation_templates import NavigationTemplates
    p = preview(scene, "launcher")
    body = registration(p)
    for count in (1, 2, 7):
        regions = [{"left_px": i * 20, "top_px": 0, "width_px": 10, "height_px": 10}
                   for i in range(count)]
        assert register(scene, p, anchor_regions=regions).status_code == 400
    target = {"kind": "ocr_text", "visible_text": "微信", "expected_page_id": "gesture-parent",
              "region": {"x": .8, "y": .1, "width": .3, "height": .1}}
    assert register(scene, p, restore_target=target).status_code == 400
    target["region"]["width"] = .1
    # 新服务实例仅依靠持久预览，客户端响应的任何变更都不是登记事实。
    p["binding"] = {"forged": True}
    p["frame_evidence"] = []
    record = NavigationTemplates(scene[1], "arm-01").register(
        "launcher", dict(body, restore_target=target))
    assert record["binding"]["camera_id"] == "cam-01"
    assert len(record["anchor_evidence"][0]["frame_evidence"]) == 3
    assert record["restore_target"] == target
    top = preview(scene)
    assert register(scene, top, restore_target=target).status_code == 400
