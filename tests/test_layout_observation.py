"""Offline camera proof and selected-layout integration regressions."""
from __future__ import annotations

import asyncio
import builtins
import io
import copy
import hashlib
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import traceback

import cv2
import numpy as np
import pytest

from adapters.vision.ocr import OcrResult, OcrWord
from drivers import screen_proof
from drivers.real_camera import RealCameraDriver
from tests.test_screen_proof import PhoneSource, png, registered_scene, regions


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    cv2.setNumThreads(1)
    attempts = []
    real_connect = sqlite3.connect
    roots = (Path(__file__).resolve().parents[1] / "data",
             Path(__file__).resolve().parents[2] / "data")

    def denied(*args, **kwargs):
        attempts.append(("external IO", [
            (Path(frame.filename).name, frame.name, frame.lineno)
            for frame in traceback.extract_stack(limit=9)[:-1]
        ]))
        raise AssertionError("Hardware, processes and network are forbidden")

    def connect(path, *args, **kwargs):
        from urllib.parse import urlparse, unquote
        checked_path = unquote(urlparse(path).path) if kwargs.get("uri") else path
        if checked_path != ":memory:" and not Path(checked_path).resolve().is_relative_to(tmp_path):
            attempts.append("non-temporary SQLite")
            raise AssertionError("SQLite must belong to this test's tmp_path")
        return real_connect(path, *args, **kwargs)

    def guard_open(original):
        def guarded(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = Path(os.fsdecode(path)).resolve()
                if any(resolved.is_relative_to(root) for root in roots):
                    attempts.append(("production data", str(resolved), [
                        (Path(frame.filename).name, frame.name, frame.lineno)
                        for frame in traceback.extract_stack(limit=7)[:-1]
                    ]))
                    raise AssertionError("Production data must not be accessed")
            return original(path, *args, **kwargs)
        return guarded

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
    monkeypatch.setattr(io, "open", guard_open(io.open))
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "bind", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(cv2, "VideoCapture", denied)
    for name in list(os.environ):
        if name.startswith("ARM_FLEET_"):
            monkeypatch.delenv(name)
    yield
    assert attempts == []


class Engine:
    def __init__(self, fault=None):
        self.fault = fault
        self.seen = []

    def recognize(self, image, *, region):
        patch = screen_proof.decode(image)
        height, width = patch.shape[:2]
        self.seen.append((region, width, height))
        text = "Target" if region == "conversation" else ""
        words = [OcrWord(text, 10, 10, 45, 20, .99)] if text else []
        if region == "input" and np.any(patch < 80):
            text = "test"
            words = [OcrWord(text, 10, 5, 24, 12, .99)]
        if self.fault == "title-size" and region == "conversation":
            width, height = 1280, 720
        if self.fault == "title-outside" and region == "conversation":
            words = [OcrWord(text, -12, 10, 45, 20, .99)]
        if self.fault == "title-nan" and region == "conversation":
            words = [OcrWord(text, float("nan"), 10, 45, 20, .99)]
        if self.fault == "input-size" and region == "input":
            width, height = 1280, 720
        return OcrResult(text=text, words=words, confidence=.99, truncated=False,
                         width=width, height=height, engine="offline-fake",
                         engine_version="1")


def camera(root, image, engine=None):
    return RealCameraDriver(
        "cam-01", frame_provider=PhoneSource(image), screen_device_id="arm-01",
        data_root=root, ocr_engine=engine or Engine())


def test_legacy_valid_crop_control(tmp_path):
    image = registered_scene(tmp_path)
    engine = Engine()
    result = asyncio.run(camera(tmp_path, image, engine).observe_regions())
    assert result["ui"]["ui_state"] == "conversation"
    assert result["input_empty"] is True
    assert ("conversation", 120, 60) in engine.seen
    assert ("input", 600, 50) in engine.seen


@pytest.mark.parametrize("shape", [(1, 1), (1, 600), (50, 1)])
def test_empty_reference_must_have_exact_crop_dimensions(tmp_path, shape):
    image = registered_scene(tmp_path)
    with screen_proof._edit("arm-01", "offline-fixture", tmp_path) as profile:
        profile["empty_input"] = screen_proof.encode(np.full((*shape, 3), 240, np.uint8))
    with pytest.raises(ValueError):
        asyncio.run(camera(tmp_path, image).observe_regions())


@pytest.mark.parametrize("fault", [
    "title-size", "title-outside", "title-nan", "input-size",
])
def test_local_ocr_must_belong_to_actual_crop(tmp_path, fault):
    image = registered_scene(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(camera(tmp_path, image, Engine(fault)).observe_regions())


@pytest.mark.parametrize("key,value", [
    ("x1", -.5), ("y1", -.5), ("x2", 16.5), ("y2", 16.5),
])
def test_fractional_out_of_frame_coordinates_rejected(key, value):
    region = dict(x1=0, y1=0, x2=16, y2=16, frameW=16, frameH=16)
    region[key] = value
    with pytest.raises(ValueError):
        screen_proof.bounds(region, 16, 16)


def test_registration_and_observation_share_pose_coordinate_domain(tmp_path):
    image = registered_scene(tmp_path)
    shifted = cv2.warpAffine(image, np.float32([[1, 0, 7], [0, 1, 5]]),
                             (1280, 720), borderValue=(20, 20, 20))
    original = regions()["title"]
    region = {**original, "x1": original["x1"] + 7, "x2": original["x2"] + 7,
              "y1": original["y1"] + 5, "y2": original["y2"] + 5}
    screen_proof.save_reference(
        "arm-01", state="conversation", label="conversation", region=region,
        image=png(shifted), operator="offline-fixture", data_root=tmp_path)
    result = asyncio.run(camera(tmp_path, shifted).observe_regions())
    assert result["ui"]["ui_state"] == "conversation"


class LayoutRig:
    """Real Runtime wiring and tmp stores, with synthetic camera/arm/IME only."""

    def __init__(self, tmp_path, monkeypatch, *, seed_selected=True, layout_db_path=None):
        from drivers import calibration_bundle as cb
        from drivers.fake_arm import FakeArmDriver
        from drivers.fake_input import FakeInputDriver
        from runtime.config import PlatformConfig
        from runtime.layout_matching import compute_page_check
        from runtime.runtime import ArmFleetRuntime
        from runtime.layout_state import content_sha, state_key
        from tests.test_ui_targets import ready_bundle

        self.sha = content_sha
        self.state_key = state_key
        self.runtime = ArmFleetRuntime(PlatformConfig({
            "pre_enable_maintenance": True, "hardware_mode": "simulation",
            "layout_db_path": str(layout_db_path) if layout_db_path else None,
        }, root=tmp_path))
        self.data = tmp_path / "data"
        self.bundle = ready_bundle()
        self.bundle.update(schemaVersion=3, cameraBindingId="aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
                           sourceGeometryId="b" * 64, coordinateFrameHash="c" * 64)
        self.bundle["screen"].update(screenWidth=320, screenHeight=640,
            edgeModel={"corners": [[100, 50], [419, 50], [419, 689], [100, 689]],
                       "tolerancePx": 8})
        self.bundle["camera"]["cameraToScreen"]["homography"] = [1, 0, -100, 0, 1, -50, 0, 0, 1]
        self.bundle["camera"]["cameraToScreen"]["maxErrorPx"] = .5
        cb.save_bundle(self.bundle, self.data)
        self.binding = {"camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
                        "camera_binding_id": self.bundle["cameraBindingId"],
                        "arm_binding_id": "offline-arm"}
        self.parts = self.runtime.layout_bundle()
        self.state = self.parts["state_store"]
        self.assets = {}
        self.screens = {}
        pages, checks, colors = [], [], []
        states = {
            "wechat_list": ["wechat_chats_tab", "list_marker"],
            "chat_ime_hidden": ["back_button", "input_box"],
            "chat_ime_empty": ["back_button", "input_box", "keyboard_boundary"],
            "chat_ime_text": ["back_button", "input_box", "keyboard_boundary",
                              "send_button", "send_button_feature"],
        }
        rgb = {"foreground_rgb": [100, 210, 80], "background_rgb": [240, 240, 240]}
        for state_index, (state, names) in enumerate(states.items()):
            screen = np.full((640, 320, 3), 240, np.uint8)
            screen[210:250, 224:288] = (80, 210, 100)
            anchors = []
            for index, name in enumerate(names):
                asset_id = state + "-" + name
                cells = np.random.default_rng(state_index * 10 + index).integers(
                    25, 220, (8, 8, 3), dtype=np.uint8)
                tile = cv2.resize(cells, (32, 32), interpolation=cv2.INTER_NEAREST)
                left, top = 16 + index * 40, 16
                screen[top:top + 32, left:left + 32] = tile
                self.assets[asset_id] = png(tile)
                anchors.append({
                    "id": name, "asset_id": asset_id, "required": True,
                    "search_region": {"x": left / 320, "y": top / 640,
                                      "width": .1, "height": .05},
                    "reference_rect": {"x": left / 320, "y": top / 640,
                                       "width": .1, "height": .05}})
            rects = {"title": (32, 100, 192, 32), "input": (16, 520, 240, 40),
                     "content": (16, 180, 288, 260),
                     "foreground": (230, 215, 16, 16), "background": (32, 215, 16, 16)}
            if state == "wechat_list":
                rects = {"unread": (16, 100, 32, 32), "content": (16, 180, 288, 260),
                         "foreground": (230, 215, 16, 16), "background": (32, 215, 16, 16)}
            definitions = []
            for name, (x, y, w, h) in rects.items():
                definitions.append({
                    "id": name, "purpose": name, "anchor_id": names[0],
                    "relative_rect": {"dx": (x - 16) / 320, "dy": (y - 16) / 640,
                                      "width": w / 320, "height": h / 640}})
            page = {"state": state, "anchors": anchors, "targets": [], "regions": definitions}
            pages.append(page)
            self.screens[state] = screen
            check = compute_page_check(page, png(screen),
                {a["id"]: self.assets[a["asset_id"]] for a in anchors}, ["ev-1", "ev-2"])
            assert check["status"] == "passed", check
            checks.append(check)
            reference = None
            if state in {"chat_ime_hidden", "chat_ime_empty"}:
                data = png(screen[520:560, 16:256])
                reference = {"id": "empty-input-" + state,
                             "sha256": hashlib.sha256(data).hexdigest()}
                if seed_selected:
                    self.state.write_adaptation_asset("arm-01", 1, reference["id"] + ".png", data)
            colors.append({"state": state, **rgb, "empty_input_asset_ref": reference,
                           "evidence_ids": ["ev-1", "ev-2"]})
        compatibility = {key: "test" for key in ("wechat_version", "layout_family",
            "font_setting", "display_scale", "theme", "system_navigation_mode",
            "ime_package", "ime_layout_version")}
        compatibility.update(platform="android", orientation="portrait")
        compatibility["sources"] = {k: "operator" for k in compatibility}
        descriptor = {"width_px": 320, "height_px": 640, "orientation": "portrait",
                      "system_insets": {"left": 0, "top": 0, "right": 0, "bottom": 0}}
        self.template = {
            "contract_version": "ui-layout-template.v1", "template_id": "offline-template",
            "version": 1, "parent_version": None, "compatibility": compatibility,
            "reference_screen_descriptor": descriptor, "pages": pages,
            "assets": [{"id": key, "relative_path": f"assets/{key}.png",
                        "sha256": hashlib.sha256(data).hexdigest(), "media_type": "image/png",
                        "width": 32, "height": 32} for key, data in self.assets.items()],
            "created_at": "2026-10-08T00:00:00Z", "created_by": "offline"}
        self.seal(self.template)
        if seed_selected:
            self.parts["template_store"].save_template("offline-template", 1, self.template)
            for key, data in self.assets.items():
                self.parts["template_store"].save_asset("offline-template", key, data)
            self.catalog("template", "offline-template", 1, self.template["sha256"])
        self.region = {
            "contract_version": "phone-search-region.v1", "device_id": "arm-01", "version": 1,
            "installation_binding": self.binding, "source_geometry_id": "b" * 64,
            "source_size": {"width": 1280, "height": 720},
            "rect": {"x": 0, "y": 0, "width": .5, "height": 1},
            "pixel_rect": {"left": 0, "top": 0, "right": 640, "bottom": 720},
            "reference_evidence_id": "offline-region", "created_at": "2026-10-08T00:00:00Z",
            "created_by": "offline"}
        self.seal(self.region)
        store = self.parts["region_store"]
        store.insert_version("arm-01", 1, self.region)
        store.cas_pointer("arm-01", 0, 1, self.region["sha256"], "offline")
        self.adaptation = {
            "contract_version": "device-layout-adaptation.v1", "device_id": "arm-01", "version": 1,
            "template_ref": {"id": "offline-template", "version": 1, "sha256": self.template["sha256"]},
            "installation_region_ref": {"id": "arm-01", "version": 1, "sha256": self.region["sha256"]},
            "ime_device_id": "offline-ime", "keyboard_geometry_mode": "live_ime_v1",
            "screen_descriptor": {**descriptor, "calibration_geometry_hash": "c" * 64},
            "compatibility_observed": compatibility, "page_checks": checks,
            "local_overrides": [], "color_samples": colors,
            "reviewed_by": "offline", "reviewed_at": "2026-10-08T00:00:00Z"}
        if seed_selected:
            self.select(self.adaptation)
        self.source = PhoneSource(self.frame("chat_ime_empty"))
        self.source.last_camera_binding_id = self.bundle["cameraBindingId"]
        self.source.last_source_geometry_id = "b" * 64
        self.engine = Engine()
        self.camera = RealCameraDriver("cam-01", frame_provider=self.source, ocr_engine=self.engine,
                                      screen_device_id="arm-01", data_root=self.data)
        arm = FakeArmDriver("arm-01")
        arm.data_root = self.data
        monkeypatch.setattr(self.runtime.devices, "_create_drivers",
            lambda cfg: (arm, self.camera, FakeInputDriver("arm-01"), "offline", "simulation"))
        self.runtime.devices.register_device({
            "device_id": "arm-01", "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
            "camera_binding_id": self.bundle["cameraBindingId"], "source_geometry_id": "b" * 64,
            "daemon_binding_id": "offline-arm", "ime_device_id": "offline-ime",
            "binding_revision": 7, "data_root": str(self.data), "calibration_status": "due"})

    def seal(self, value):
        value["sha256"] = self.sha({k: v for k, v in value.items() if k != "sha256"})

    def catalog(self, kind, owner, version, sha):
        self.state.state_insert("catalog", self.state_key("catalog", [kind, owner, version]),
            None, None, {"resource": {"kind": kind, "ref": {"id": owner, "version": version,
            "sha256": sha}}, "operation_id": "offline-seed", "created_at": "2026-10-08T00:00:00Z"})

    def select(self, record):
        if record["version"] != 1:
            for sample in record["color_samples"]:
                ref = sample["empty_input_asset_ref"]
                if ref:
                    data = self.state.read_adaptation_asset("arm-01", 1, ref["id"] + ".png")
                    self.state.write_adaptation_asset("arm-01", record["version"], ref["id"] + ".png", data)
        self.seal(record)
        version = record["version"]
        self.state.write_adaptation_version("arm-01", version, record)
        self.catalog("adaptation", "arm-01", version, record["sha256"])
        pointer = self.state.adaptation_pointer_get("arm-01") or {"revision": 0}
        self.state.adaptation_pointer_cas("arm-01", pointer["revision"], version,
                                         record["sha256"], "offline-seed")

    def frame(self, state):
        image = np.full((720, 1280, 3), 20, np.uint8)
        image[50:690, 100:420] = self.screens[state]
        return image

    def close(self):
        for key in ("state_store", "template_store", "region_store"):
            self.parts[key].close()
        self.parts["kv_store"]._conn.close()
        self.runtime.store._conn.close()

    def template_version(self, template):
        self.seal(template)
        self.parts["template_store"].save_template(template["template_id"], template["version"], template)
        self.catalog("template", template["template_id"], template["version"], template["sha256"])
        adaptation = copy.deepcopy(self.adaptation)
        adaptation["version"] = template["version"]
        adaptation["template_ref"] = {"id": template["template_id"], "version": template["version"],
                                       "sha256": template["sha256"]}
        self.select(adaptation)


@pytest.fixture
def layout_rig(tmp_path, monkeypatch):
    rig = LayoutRig(tmp_path, monkeypatch)
    yield rig
    rig.close()


def test_runtime_selected_layout_works_without_legacy_profile(layout_rig):
    rig = layout_rig
    assert not screen_proof.profile_path("arm-01", rig.data).exists()
    observed = asyncio.run(rig.runtime.devices.camera("arm-01").observe_regions())
    assert observed["ui"]["ui_state"] == "conversation"
    assert observed["input_empty"] is True
    assert observed["adaptation_ref"]["version"] == 1
    assert ("conversation", 192, 32) in rig.engine.seen
    assert ("messages", 320, 640) in rig.engine.seen
    assert observed["frameW"] == 1280
    assert observed["components"][0]["x"] == 324
    assert observed["components"][0]["y"] == 260


def test_selected_version_changes_actual_crop_and_proof_binding(layout_rig):
    rig = layout_rig
    before = asyncio.run(rig.camera.observe_regions())
    updated = copy.deepcopy(rig.adaptation)
    updated["version"] = 2
    updated["local_overrides"] = [{
        "state": "chat_ime_empty", "kind": "region", "item_id": "title",
        "anchor_id": "back_button",
        "relative_rect": {"dx": .05, "dy": 84 / 640, "width": .5, "height": .05},
        "source_evidence_ids": ["ev-1", "ev-2"], "reason": "offline crop change"}]
    rig.select(updated)
    after = asyncio.run(rig.camera.observe_regions())
    assert before["profile_revision"] == 1 and after["profile_revision"] == 2
    assert before["binding"] != after["binding"]
    assert ("conversation", 160, 32) in rig.engine.seen
    assert before["adaptation_ref"] != after["adaptation_ref"]


def test_old_revision_one_cannot_collide_with_shared_version_one(layout_rig):
    rig = layout_rig
    fingerprint, _ = screen_proof.binding("arm-01", rig.data)
    shared = asyncio.run(rig.camera.observe_regions())
    legacy = {"binding": fingerprint, "profile_revision": 1}
    assert shared["profile_revision"] == legacy["profile_revision"] == 1
    assert not all(shared[key] == legacy[key] for key in ("binding", "profile_revision"))


@pytest.mark.parametrize("fault", ["adaptation-hash", "missing-version", "template-asset",
    "empty-asset", "region-selection", "current-phone", "source-geometry", "camera-binding",
    "v3-geometry", "outside-matrix", "missing-catalog"])
def test_invalid_selected_layout_never_falls_back_to_legacy(layout_rig, monkeypatch, fault):
    from drivers import calibration_bundle as cb
    import json
    rig = layout_rig
    old_calls = []
    monkeypatch.setattr(screen_proof, "load_profile", lambda *args, **kwargs:
                        old_calls.append(True) or {"regions": {"title": {}}})
    path = rig.data / "devices/arm-01/layout_adaptation/versions/1.json"
    if fault == "adaptation-hash":
        record = json.loads(path.read_text())
        record["screen_descriptor"]["width_px"] = 999
        path.write_text(json.dumps(record))
    elif fault == "missing-version":
        path.unlink()
    elif fault == "template-asset":
        key = next(iter(rig.assets))
        rig.parts["template_store"]._write(
            "UPDATE layout_assets SET data=? WHERE asset_id=?", (b"invalid", key))
    elif fault == "empty-asset":
        path = rig.data / "devices/arm-01/layout_adaptation/versions/1/assets/empty-input-chat_ime_empty.png"
        path.write_bytes(b"broken")
    elif fault == "region-selection":
        rig.parts["region_store"].cas_pointer("arm-01", 1, 2, "f" * 64, "offline-change")
    elif fault == "current-phone":
        rig.runtime.devices._devices["arm-01"]["ime_device_id"] = "different-phone"
    elif fault == "source-geometry":
        rig.source.last_source_geometry_id = "d" * 64
    elif fault == "camera-binding":
        rig.source.last_camera_binding_id = "bbbbbbbb-1111-4111-8111-aaaaaaaaaaaa"
    elif fault == "v3-geometry":
        bundle = copy.deepcopy(rig.bundle)
        bundle["coordinateFrameHash"] = "d" * 64
        cb.save_bundle(bundle, rig.data)
    elif fault == "outside-matrix":
        bundle = copy.deepcopy(rig.bundle)
        bundle["camera"]["cameraToScreen"]["homography"] = [1, 0, -1200, 0, 1, -50, 0, 0, 1]
        cb.save_bundle(bundle, rig.data)
    else:
        rig.state._conn.execute("DELETE FROM layout_typed_state WHERE kind='catalog'")
        rig.state._conn.commit()
    with pytest.raises(ValueError):
        asyncio.run(rig.camera.observe_regions())
    assert old_calls == []


def test_same_image_matching_two_pages_is_ambiguous(layout_rig):
    from runtime.layout_matching import compute_page_check
    rig = layout_rig
    template = copy.deepcopy(rig.template)
    template["version"] = 2
    pages = {p["state"]: p for p in template["pages"]}
    pages["chat_ime_hidden"]["anchors"] = copy.deepcopy(pages["chat_ime_empty"]["anchors"])
    page = pages["chat_ime_hidden"]
    updated_check = compute_page_check(page, png(rig.screens["chat_ime_empty"]),
        {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]}, ["ev-1", "ev-2"])
    assert updated_check["status"] == "passed"
    rig.adaptation["page_checks"] = [
        updated_check if check["state"] == "chat_ime_hidden" else check
        for check in rig.adaptation["page_checks"]]
    rig.template_version(template)
    with pytest.raises(ValueError, match="歧义"):
        asyncio.run(rig.camera.observe_regions())


@pytest.mark.parametrize("fault", [
    "missing", "too-wide", "wrong-kind", "duplicate", "no-position",
    "failed-anchor", "duplicate-anchor", "region-safe",
])
def test_saved_target_requires_unique_current_safe_box(layout_rig, fault):
    from runtime.layout_matching import compute_page_check
    from runtime.layout_geometry import mapping_error_bound
    rig = layout_rig
    template = copy.deepcopy(rig.template)
    template["version"] = 2
    page = next(p for p in template["pages"] if p["state"] == "chat_ime_empty")
    page["targets"] = [{
        "id": "back_button", "anchor_id": "back_button", "mode": "tap",
        "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05},
    }]
    check = compute_page_check(page, png(rig.screens["chat_ime_empty"]),
        {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]},
        ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle))
    assert check["status"] == "passed"
    item = next(i for i in check["item_results"] if i["kind"] == "target")
    if fault == "missing":
        item["safe_rect"] = None
    elif fault == "too-wide":
        item["safe_rect"] = copy.deepcopy(item["resolved_rect"])
    elif fault == "wrong-kind":
        item["kind"] = "region"
    elif fault == "no-position":
        item["resolved_rect"] = None
    elif fault == "failed-anchor":
        check["anchor_results"][0].update(status="failed", reason_code="anchor_ambiguous")
    elif fault == "duplicate-anchor":
        duplicate = copy.deepcopy(check["anchor_results"][0])
        check["anchor_results"][0]["localization_error_bound_px"] = 5
        check["anchor_results"].append(duplicate)
    elif fault == "region-safe":
        region = next(i for i in check["item_results"] if i["kind"] == "region")
        region["safe_rect"] = copy.deepcopy(region["resolved_rect"])
    else:
        check["item_results"].append(copy.deepcopy(item))
    rig.adaptation["page_checks"] = [
        check if c["state"] == "chat_ime_empty" else c for c in rig.adaptation["page_checks"]]
    rig.template_version(template)
    selected = rig.runtime.layout_observation.resolve("arm-01")
    with pytest.raises(ValueError, match="安全"):
        selected.metadata(rig.bundle)
    with pytest.raises(ValueError, match="安全"):
        asyncio.run(rig.camera.observe_regions())


def test_switch_during_ocr_rejects_mixed_proof(layout_rig):
    rig = layout_rig

    class SwitchingEngine(Engine):
        changed = False

        def recognize(self, image, *, region):
            if not self.changed:
                self.changed = True
                updated = copy.deepcopy(rig.adaptation)
                updated["version"] = 2
                rig.select(updated)
            return super().recognize(image, region=region)

    rig.camera.ocr_engine = SwitchingEngine()
    with pytest.raises(ValueError, match="版本已变化"):
        asyncio.run(rig.camera.observe_regions())


def test_shared_list_has_navigation_but_never_claims_empty_input(layout_rig):
    rig = layout_rig
    rig.source.image = rig.frame("wechat_list")
    result = asyncio.run(rig.camera.observe_regions())
    assert result["ui"]["ui_state"] == "wechat_main"
    assert result["input_empty"] is False
    assert result["components"] == []


def test_shared_ui_and_title_use_same_selected_template(layout_rig):
    rig = layout_rig
    assert asyncio.run(rig.camera.identify_ui_state())["ui_state"] == "conversation"
    title = asyncio.run(rig.camera.identify_account_and_conversation())
    assert title["conversation_clue"] == "Target"
    assert title["ui_state"] == "conversation"


def test_shared_navigation_boxes_are_projected_to_camera(layout_rig):
    rig = layout_rig

    class NavigationEngine(Engine):
        def recognize(self, image, *, region):
            result = super().recognize(image, region=region)
            if (result.width, result.height) == (320, 640):
                result.words = [OcrWord("Navigation", 32, 100, 64, 32, .99)]
                result.text = "Navigation"
            return result

    rig.camera.ocr_engine = NavigationEngine()
    result = asyncio.run(rig.camera.observe_regions())
    assert result["ocr_boxes"] == [
        {"text": "Navigation", "x": 132, "y": 150, "w": 64, "h": 32, "confidence": .99}]
    assert result["coordinateVersion"] == "calibrated_camera.v1"


@pytest.mark.parametrize("shift", [7, 25, 60])
def test_shared_layout_preserves_existing_pose_gate(layout_rig, shift):
    rig = layout_rig
    rig.source.image = cv2.warpAffine(rig.source.image,
        np.float32([[1, 0, shift], [0, 1, 0]]), (1280, 720), borderValue=(20, 20, 20))
    if shift <= 45:
        assert asyncio.run(rig.camera.observe_regions())["ui"]["ui_state"] == "conversation"
    else:
        with pytest.raises(ValueError):
            asyncio.run(rig.camera.observe_regions())


def test_shared_ocr_still_rejects_wrong_local_dimensions(layout_rig):
    rig = layout_rig
    rig.camera.ocr_engine = Engine("title-size")
    with pytest.raises(ValueError, match="OCR"):
        asyncio.run(rig.camera.observe_regions())


def test_content_can_move_away_from_registration_color_sample(layout_rig):
    rig = layout_rig
    rig.source.image[260:300, 324:388] = 240
    rig.source.image[350:390, 324:388] = (80, 210, 100)
    result = asyncio.run(rig.camera.observe_regions())
    assert len(result["components"]) == 1
    assert result["components"][0]["y"] == 350


def test_empty_patch_shape_is_not_resized_or_broadcast_for_shared_layout(layout_rig):
    rig = layout_rig
    replacement = png(np.full((1, 1, 3), 240, np.uint8))
    path = rig.data / "devices/arm-01/layout_adaptation/versions/1/assets/empty-input-chat_ime_empty.png"
    path.write_bytes(replacement)
    updated = copy.deepcopy(rig.adaptation)
    updated["version"] = 2
    for sample in updated["color_samples"]:
        if sample["state"] == "chat_ime_empty":
            sample["empty_input_asset_ref"]["sha256"] = hashlib.sha256(replacement).hexdigest()
    rig.select(updated)
    with pytest.raises(ValueError, match="尺寸"):
        asyncio.run(rig.camera.observe_regions())


def test_current_device_change_during_ocr_rejects_proof(layout_rig):
    rig = layout_rig

    class RebindingEngine(Engine):
        def recognize(self, image, *, region):
            rig.runtime.devices._devices["arm-01"]["ime_device_id"] = "changed-phone"
            return super().recognize(image, region=region)

    rig.camera.ocr_engine = RebindingEngine()
    with pytest.raises(ValueError, match="绑定"):
        asyncio.run(rig.camera.observe_regions())


@pytest.mark.parametrize("change", ["homography", "sourceGeometryId", "cameraBindingId"])
def test_calibration_change_during_ocr_rejects_proof(layout_rig, change):
    from drivers import calibration_bundle as cb
    rig = layout_rig

    class RecalibratingEngine(Engine):
        def recognize(self, image, *, region):
            updated = copy.deepcopy(rig.bundle)
            if change == "homography":
                updated["camera"]["cameraToScreen"]["homography"][2] -= 1
            else:
                updated[change] = "changed-during-ocr"
            cb.save_bundle(updated, rig.data)
            return super().recognize(image, region=region)

    rig.camera.ocr_engine = RecalibratingEngine()
    with pytest.raises(ValueError, match="校准"):
        asyncio.run(rig.camera.observe_regions())


@pytest.mark.parametrize("rect,expected", [
    ({"x": .07, "y": .14, "width": .14, "height": .07}, (7, 14, 21, 21)),
    ({"x": .7, "y": .6, "width": .3000000000000002, "height": .4},
     (70, 60, 100, 100)),
    ({"x": .07125, "y": .1425, "width": .143, "height": .0725}, (7, 14, 22, 22)),
])
def test_runtime_crop_uses_matching_pixel_boundary_convention(rect, expected):
    from runtime.layout_observation import _pixel_region
    frame = np.zeros((100, 100, 3), np.uint8)
    left, top, right, bottom = expected
    actual = _pixel_region(rect, 100, 100)
    assert (actual["x1"], actual["y1"], actual["x2"], actual["y2"]) == expected
    assert screen_proof.crop(frame, actual).shape == (bottom - top, right - left, 3)


@pytest.mark.parametrize("rect", [
    {"x": -.000001, "y": 0, "width": .14, "height": .14},
    {"x": .7, "y": .6, "width": .300001, "height": .4},
    {"x": .7, "y": .6, "width": .3, "height": .400001},
    {"x": float("nan"), "y": .6, "width": .3, "height": .4},
])
def test_runtime_crop_never_clamps_real_overflow(rect):
    from runtime.layout_observation import _pixel_region
    with pytest.raises(ValueError):
        _pixel_region(rect, 100, 100)


def test_selected_metadata_reads_no_frames_or_stores_and_does_not_match(layout_rig, monkeypatch):
    from runtime import layout_observation
    rig = layout_rig
    selected = rig.runtime.layout_observation.resolve("arm-01")

    def forbidden(*args, **kwargs):
        pytest.fail("Static metadata must not capture, match or reopen stores")

    monkeypatch.setattr(selected.service, "_services", forbidden)
    monkeypatch.setattr(rig.camera, "_capture_for_operation", forbidden)
    monkeypatch.setattr(layout_observation, "compute_page_check", forbidden)
    result = selected.metadata(rig.bundle)
    assert result == {
        "registered": True, "usable": True,
        "reference_states": ["conversation", "wechat_main"],
        "regions_registered": True, "revision": 1, "blocking_reasons": [],
        **selected.references}


@pytest.mark.parametrize("fault", ["draft", "camera_binding", "geometry", "ime"])
def test_metadata_and_camera_share_saved_binding_rejections(layout_rig, fault):
    from drivers import calibration_bundle as cb
    from runtime.layout_observation import LayoutObservationError
    rig = layout_rig
    bundle = copy.deepcopy(rig.bundle)
    if fault == "draft":
        bundle["status"] = "draft"
    elif fault == "camera_binding":
        bundle["cameraBindingId"] = "changed-binding"
    elif fault == "geometry":
        bundle["coordinateFrameHash"] = "d" * 64
    else:
        rig.runtime.devices._devices["arm-01"]["ime_device_id"] = "changed-phone"
    cb.save_bundle(bundle, rig.data)
    selected = rig.runtime.layout_observation.resolve("arm-01")
    with pytest.raises(LayoutObservationError):
        selected.metadata(bundle)
    with pytest.raises(ValueError):
        asyncio.run(rig.camera.observe_regions())


def test_shared_metadata_and_camera_do_not_require_removed_input_acceptance(layout_rig):
    from drivers import calibration_bundle as cb

    rig = layout_rig
    bundle = copy.deepcopy(rig.bundle)
    bundle["status"] = cb.MOTION_READY_STATUS
    cb.save_bundle(bundle, rig.data)
    selected = rig.runtime.layout_observation.resolve("arm-01")
    assert selected.metadata(bundle)["usable"] is True
    assert asyncio.run(rig.camera.observe_regions())["ui"]["ui_state"] == "conversation"
    assert screen_proof.binding("arm-01", rig.data)[0]
    with pytest.raises(ValueError):
        screen_proof.binding("arm-01", rig.data, allow_motion_ready=False)


@pytest.mark.parametrize("fault", ["missing_foreground", "empty_reference", "fallback_palette"])
def test_metadata_rejects_published_but_incomplete_visual_evidence(layout_rig, fault):
    from runtime.layout_observation import LayoutObservationError
    rig = layout_rig
    if fault == "missing_foreground":
        template = copy.deepcopy(rig.template)
        template["version"] = 2
        for page in template["pages"]:
            page["regions"] = [r for r in page["regions"] if r["purpose"] != "foreground"]
        rig.template_version(template)
    else:
        updated = copy.deepcopy(rig.adaptation)
        updated["version"] = 2
        for sample in updated["color_samples"]:
            if fault == "empty_reference":
                sample["empty_input_asset_ref"] = None
            else:
                sample["foreground_rgb"] = sample["background_rgb"]
        rig.select(updated)
    selected = rig.runtime.layout_observation.resolve("arm-01")
    with pytest.raises(LayoutObservationError):
        selected.metadata(rig.bundle)
    with pytest.raises(ValueError):
        asyncio.run(rig.camera.observe_regions())


def test_full_v3_size_used_without_clahe_or_thumbnail(layout_rig):
    import math
    from drivers import calibration_bundle as cb
    from runtime.layout_matching import compute_page_check
    rig = layout_rig
    bundle = copy.deepcopy(rig.bundle)
    bundle["camera"]["cameraToScreen"]["homography"] = [3, 0, -300, 0, 3, -150, 0, 0, 1]
    # Map the final output sample to the last real camera pixel, never border fill.
    bundle["screen"].update(screenWidth=958, screenHeight=1918)
    cb.save_bundle(bundle, rig.data)
    updated = copy.deepcopy(rig.adaptation)
    updated["version"] = 2
    updated["screen_descriptor"].update(width_px=958, height_px=1918)
    # A new geometry requires actual new checks and references, not resized old
    # sample dimensions: the discrete anchor position can move by one pixel.
    images = {}
    updated["page_checks"] = []
    matrix = np.array(bundle["camera"]["cameraToScreen"]["homography"], dtype=float).reshape(3, 3)
    for page in rig.template["pages"]:
        image = cv2.warpPerspective(rig.frame(page["state"]), matrix, (958, 1918))
        images[page["state"]] = image
        check = compute_page_check(page, png(image),
            {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]}, ["ev-1", "ev-2"])
        assert check["status"] == "passed"
        updated["page_checks"].append(check)
    for sample in updated["color_samples"]:
        ref = sample["empty_input_asset_ref"]
        if ref:
            check = next(c for c in updated["page_checks"] if c["state"] == sample["state"])
            rect = next(i["resolved_rect"] for i in check["item_results"] if i["id"] == "input")
            left, top = math.floor(rect["x"] * 958), math.floor(rect["y"] * 1918)
            right = math.ceil((rect["x"] + rect["width"]) * 958)
            bottom = math.ceil((rect["y"] + rect["height"]) * 1918)
            data = png(images[sample["state"]][top:bottom, left:right])
            (rig.data / f"devices/arm-01/layout_adaptation/versions/1/assets/{ref['id']}.png").write_bytes(data)
            ref["sha256"] = hashlib.sha256(data).hexdigest()
    rig.select(updated)
    # Shared keyboard states use current input pixels even for UI identification.
    result = asyncio.run(rig.camera.identify_ui_state())
    assert result["ui_state"] == "conversation", result.get("note", result)
    layout = asyncio.run(rig.camera._selected_layout_frame())
    assert layout.image.shape == (1918, 958, 3)
    assert layout.image[1500, 500].tolist() == [240, 240, 240]
    mapped = layout.camera_rect(96, 300, 192, 96)
    assert mapped == {"x": 132, "y": 150, "width": 64, "height": 32}


@pytest.mark.parametrize("reverse_regions", [False, True])
@pytest.mark.parametrize("list_color_regions", [True, False])
def test_real_four_page_publish_products_are_consumed_by_runtime_camera(
        tmp_path, monkeypatch, reverse_regions, list_color_regions):
    """No seeded template/adaptation, direct services only, no HTTP/lifespan."""
    import base64
    import uuid
    from runtime.layout_drafts import LayoutDraftService

    rig = LayoutRig(tmp_path, monkeypatch, seed_selected=False)
    try:
        svc = rig.parts["adaptation_svc"]
        pages = copy.deepcopy(rig.template["pages"])
        for page in pages:
            if page["state"] == "wechat_list" and not list_color_regions:
                page["regions"] = [region for region in page["regions"]
                                   if region["purpose"] in {"unread", "content"}]
            for region in page["regions"]:
                if region["purpose"] == "input":
                    region["relative_rect"] = {
                        "dx": .5 / 320, "dy": 504.25 / 640,
                        "width": 239.25 / 320, "height": 39.25 / 640}
                elif region["purpose"] == "foreground":
                    region["id"] = "outgoing_bubble_color"
                elif region["purpose"] == "background":
                    region["id"] = "chat_surface_color"
            if reverse_regions:
                page["regions"].reverse()
        rig.screens["chat_ime_text"][535:546, 48:64] = 30
        archived = {}
        sequence = 0
        current = {**rig.binding, "source_geometry_id": "b" * 64,
                   "source_size": {"width": 1280, "height": 720},
                   "ime_device_id": "offline-ime",
                   "screen_descriptor": rig.adaptation["screen_descriptor"]}

        def capture(device, count, state=None):
            nonlocal sequence
            frames = []
            for _ in range(count):
                sequence += 1
                name = f"offline-source-{sequence}"
                data = png(rig.frame(state or "chat_ime_empty"))
                archived[name] = data
                ns = 1_000_000_000_000 + sequence * 250_000_000
                frames.append({
                    "evidence_id": name, "jpeg": data, "installation_binding": rig.binding,
                    "source_size": current["source_size"], "source_geometry_id": "b" * 64,
                    "source_generation_id": "22222222-2222-4222-8222-222222222222",
                    "source_frame_seq": sequence, "source_capture_seq": sequence,
                    "source_clock_id": "mach-absolute:" + "0" * 64,
                    "clock_registration_id": "33333333-3333-4333-8333-333333333333",
                    "source_captured_monotonic_ns": ns, "source_capture_uncertainty_ms": 1,
                    "source_timing_provenance": "camera_pts_host_clock.v1",
                    "received_monotonic_ms": ns // 1_000_000 + 20,
                })
            return frames

        svc.attach_providers(capture_provider=capture, frame_reader=archived.get,
            calibration_provider=lambda device: rig.bundle,
            binding_provider=lambda device: copy.deepcopy(current),
            region_ref_provider=lambda device: rig.adaptation["installation_region_ref"],
            region_record_provider=lambda device: copy.deepcopy(rig.region))
        drafts = LayoutDraftService(rig.parts["template_store"], rig.state,
            frame_reader=archived.get, binding_snapshot_provider=lambda device: copy.deepcopy(current),
            rectifier=svc._rectify_evidence_to_screen)
        created = drafts.create({"device_id": "arm-01", "client_request_id": str(uuid.uuid4())},
                                 "offline")
        draft_id, revision = created["draft_id"], created["revision"]
        updated = drafts.update_metadata(draft_id, {
            "client_request_id": str(uuid.uuid4()), "expected_revision": revision,
            "compatibility": rig.template["compatibility"]}, "offline")
        revision = updated["draft"]["revision"]
        for asset in rig.template["assets"]:
            saved = drafts.upload_asset(draft_id, {
                "client_request_id": str(uuid.uuid4()), "expected_revision": revision,
                "privacy_confirmed": True, "asset": asset,
                "content_base64": base64.b64encode(rig.assets[asset["id"]]).decode(),
                "source_evidence_ids": []}, "offline")
            revision = saved["revision"]
        for page in pages:
            definition = copy.deepcopy(page)
            for anchor in definition["anchors"]:
                if anchor["id"] in {"wechat_chats_tab", "back_button", "input_box", "send_button"}:
                    definition["targets"].append({
                        "id": anchor["id"], "anchor_id": anchor["id"], "mode": "tap",
                        "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05}})
            acquired = svc.capture("arm-01", {
                "client_request_id": str(uuid.uuid4()), "principal_id": "offline",
                "owner_kind": "draft", "owner_id": draft_id, "page_state": page["state"],
                "expected_owner_revision": revision, "controlled_test_confirmed": True})
            saved = drafts.write_page(draft_id, page["state"], {
                "client_request_id": str(uuid.uuid4()), "expected_revision": revision,
                "source_evidence_ids": acquired["source_evidence_ids"],
                "page_definition": definition}, "offline")
            assert saved["page"]["page_check"]["status"] == "passed"
            assert all(item["safe_rect"] is not None
                       for item in saved["page"]["page_check"]["item_results"]
                       if item["kind"] == "target")
            revision = saved["revision"]
        published_template = drafts.publish({
            "client_request_id": str(uuid.uuid4()), "template_id": "produced-template",
            "draft_id": draft_id, "expected_draft_revision": revision,
            "expected_parent_version": None}, "offline")
        template_ref = published_template["template_ref"]
        session = svc.start_session("arm-01", {
            "template_ref": template_ref, "principal_id": "offline"})
        session_id, revision = session["session_id"], 1
        for page in pages:
            acquired = svc.capture("arm-01", {
                "client_request_id": str(uuid.uuid4()), "owner_kind": "session",
                "owner_id": session_id, "page_state": page["state"],
                "expected_owner_revision": revision, "controlled_test_confirmed": True})
            checked = svc.write_page("arm-01", session_id, page["state"], {
                "client_request_id": str(uuid.uuid4()), "expected_session_revision": revision,
                "source_evidence_ids": acquired["source_evidence_ids"], "local_overrides": []})
            assert checked["check"]["status"] == "passed"
            assert all(item["safe_rect"] is not None
                       for item in checked["check"]["item_results"] if item["kind"] == "target")
            revision = checked["session_revision"]
        preview = svc.preview("arm-01", {"capture_session_id": session_id})
        assert preview["failed_items"] == []
        published = svc.publish("arm-01", {
            "client_request_id": str(uuid.uuid4()), "principal_id": "offline",
            "session_id": session_id, "preview_id": preview["preview_id"], "expected_revision": 0})
        assert published["status"] == "verified"
        assert not screen_proof.profile_path("arm-01", rig.data).exists()
        record = rig.parts["ops_svc"].resolve_adaptation(
            "arm-01", published["adaptation_ref"]["version"])
        for sample in record["color_samples"]:
            if sample["state"] == "wechat_list" and not list_color_regions:
                assert np.linalg.norm(np.array(sample["foreground_rgb"], dtype=float)
                                      - sample["background_rgb"]) >= 60
                assert sample["evidence_ids"]
            else:
                assert sample["foreground_rgb"] == [100, 210, 80]
                assert sample["background_rgb"] == [240, 240, 240]
            ref = sample["empty_input_asset_ref"]
            if ref:
                data = rig.state.read_adaptation_asset(
                    "arm-01", record["version"], ref["id"] + ".png")
                assert hashlib.sha256(data).hexdigest() == ref["sha256"]
                assert screen_proof.decode(data).shape == (40, 240, 3)
        for page in pages:
            state = page["state"]
            rig.source.image = rig.frame(state)
            result = asyncio.run(rig.runtime.devices.camera("arm-01").observe_regions())
            assert result["adaptation_ref"] == published["adaptation_ref"]
            assert result["template_ref"] == template_ref
            assert result["ui"]["ui_state"] == (
                "wechat_main" if state == "wechat_list" else "conversation")
            assert result["input_empty"] is (state in {"chat_ime_hidden", "chat_ime_empty"})
            if state != "wechat_list":
                assert len(result["components"]) == 1
                assert result["components"][0]["x"] == 324
                assert result["components"][0]["y"] == 260
        assert ("input", 240, 40) in rig.engine.seen
    finally:
        rig.close()


@pytest.mark.parametrize("state", ["chat_ime_hidden", "chat_ime_empty", "chat_ime_text"])
@pytest.mark.parametrize("purpose", ["title", "input", "foreground", "background"])
def test_chat_page_evidence_still_requires_explicit_regions(layout_rig, state, purpose):
    from runtime.layout_observation import LayoutObservationError
    selected = layout_rig.runtime.layout_observation.resolve("arm-01")
    selected.pages[state]["regions"] = [row for row in selected.pages[state]["regions"]
                                        if row["purpose"] != purpose]
    with pytest.raises(LayoutObservationError, match="明确用途"):
        selected._verify_page_evidence(state, 320, 640)
