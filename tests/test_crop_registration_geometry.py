"""Isolated registration tests: real image matching, no live IO or runtime startup."""
from __future__ import annotations

import copy
import hashlib
import json
import socket
import sqlite3
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from runtime.layout_adaptation import AdaptationError, LayoutAdaptationService
from runtime.layout_region import LayoutRegionStore
from runtime.layout_state import LayoutStateStore, content_sha, state_key
from runtime.layout_template import LayoutTemplateStore


DEVICE = "arm-test"
STATE = "wechat_list"
BINDING = {
    "camera_id": "cam-test", "camera_device_unique_id": "uvc-test",
    "camera_binding_id": "11111111-1111-4111-8111-111111111111",
    "arm_binding_id": "arm-binding-test",
}
GEOMETRY = "b" * 64
CAMERA_SIZE = {"width": 800, "height": 800}


def png(image):
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


@pytest.fixture(autouse=True)
def isolated_io(tmp_path, monkeypatch):
    real_connect = sqlite3.connect

    def connect(path, *args, **kwargs):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve()), path
        return real_connect(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Live IO/startup is forbidden in this test")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("ARM_FLEET_PRE_ENABLE", "0")


class Rig:
    def __init__(self, root, monkeypatch, width=400, height=600):
        from api.app import create_app
        from runtime.config import PlatformConfig

        self.root, self.width, self.height = root, width, height
        self.frames, self.reads = {}, []
        self.next_images = []
        self.sequence = 0
        self.assets = {}
        anchors = []
        for index, (x, y) in enumerate(((0.12, 0.12), (0.58, 0.28))):
            name = f"anchor-{index}"
            cells = np.random.default_rng(71 + index).integers(
                30, 235, (6, 5, 3), dtype=np.uint8)
            tile = cv2.resize(cells, (40, 48), interpolation=cv2.INTER_NEAREST)
            self.assets[name] = tile
            anchors.append({
                "id": name, "asset_id": name, "required": True,
                "reference_rect": {"x": x, "y": y, "width": 0.1, "height": 0.08},
                "search_region": {"x": x - 0.04, "y": y - 0.03,
                                  "width": 0.21, "height": 0.15},
            })
        self.page = {
            "state": STATE, "anchors": anchors,
            "targets": [{"id": "wechat_chats_tab", "anchor_id": "anchor-0",
                         "mode": "tap", "relative_rect": {
                             "dx": 0, "dy": 0, "width": 0.1, "height": 0.08}}],
            "regions": [
                {"id": name, "purpose": name, "anchor_id": f"anchor-{i}",
                 "relative_rect": {"dx": 0, "dy": 0, "width": 0.1, "height": 0.08}}
                for i, name in enumerate(("unread", "content"))],
        }
        self.quad = np.float32([[145, 100], [660, 155], [690, 710], [95, 675]])
        self.screen_corners = np.float32(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
        matrix = cv2.getPerspectiveTransform(self.quad, self.screen_corners)
        self.calibration = {
            "schemaVersion": 3, "status": "ready", "deviceId": DEVICE,
            "cameraId": BINDING["camera_id"],
            "cameraDeviceUniqueId": BINDING["camera_device_unique_id"],
            "cameraBindingId": BINDING["camera_binding_id"],
            "sourceGeometryId": GEOMETRY, "frame": dict(CAMERA_SIZE),
            "screen": {"screenWidth": width, "screenHeight": height,
                       "edgeModel": {"corners": self.quad.tolist(), "tolerancePx": 8}},
            "xy": {"validated": True, "maxResidualPx": .5},
            "camera": {"validated": True, "cameraToScreen": {
                "homography": matrix.tolist(), "maxErrorPx": .5}},
            "coordinateFrameHash": "c" * 64,
        }
        from runtime.calibration_gate import _quad_of
        corners = _quad_of(self.image())
        assert corners is not None
        self.calibration["screen"]["edgeModel"]["corners"] = corners
        self.calibration_path = root / "data" / "devices" / DEVICE / "calibration_bundle.json"
        self.sync_calibration()
        self.current = {**BINDING, "source_geometry_id": GEOMETRY}
        db = root / "layout.db"
        self.state = LayoutStateStore(db, data_root=root / "data")
        self.templates = LayoutTemplateStore(db)
        self.regions = LayoutRegionStore(db)
        self.region = {
            "contract_version": "phone-search-region.v1", "device_id": DEVICE,
            "version": 1, "installation_binding": dict(BINDING),
            "source_geometry_id": GEOMETRY, "source_size": dict(CAMERA_SIZE),
            "rect": {"x": 0.0875, "y": 0.08, "width": 0.8375, "height": 0.86},
            "pixel_rect": {"x": 70, "y": 64, "width": 670, "height": 688},
        }
        self.save_region(self.region)
        self.regions.cas_pointer(DEVICE, 0, 1, self.region["sha256"], str(uuid.uuid4()))
        self.templates.save_template("template-test", 1, {
            "template_id": "template-test", "version": 1, "pages": [self.page]})
        for name, image in self.assets.items():
            self.templates.save_asset("template-test", name, png(image))
        self.adaptation = LayoutAdaptationService(
            template_store=self.templates, state_store=self.state,
            calibration_provider=lambda device: self.calibration,
            binding_provider=lambda device: copy.deepcopy(self.current),
            capture_provider=self.capture_frames, frame_reader=self.read_frame)
        self.bundle = {
            "db_path": db, "state_store": self.state, "template_store": self.templates,
            "region_store": self.regions, "adaptation_svc": self.adaptation,
        }
        self.runtime = SimpleNamespace(
            config=PlatformConfig({"screen_ime_enabled": False,
                                   "wx_compat_enabled": False}, root=root),
            devices=SimpleNamespace(
                get=lambda device: {"daemon_binding_id": BINDING["arm_binding_id"]},
                driver=lambda device: SimpleNamespace(data_root=root / "data"),
                input_binding=lambda device: {"ime_device_id": "ime-test"}),
            evidence=SimpleNamespace(
                latest_source_frame_v2=lambda device: {
                    **self.current,
                    "device_unique_id": self.current["camera_device_unique_id"],
                    "frame_width": 800, "frame_height": 800},
                read_source_frame_bytes=self.read_frame),
            layout_db_path=db, layout_bundle=lambda **kwargs: self.bundle,
        )
        self.app = create_app(self.runtime)
        self.app.state.runtime = self.runtime

        @self.app.middleware("http")
        async def trusted_identity(request, call_next):
            request.state.layout_principal = "operator-test"
            request.state.layout_principal_source = "test"
            return await call_next(request)

        # No lifespan: never call runtime.startup or shutdown.
        self.client = TestClient(self.app)

    def sync_calibration(self):
        self.calibration_path.parent.mkdir(parents=True, exist_ok=True)
        if self.calibration is None:
            self.calibration_path.unlink(missing_ok=True)
        else:
            self.calibration_path.write_text(json.dumps(self.calibration), encoding="utf-8")

    def save_region(self, record):
        record["sha256"] = content_sha({k: v for k, v in record.items() if k != "sha256"})
        self.regions.insert_version(DEVICE, record["version"], record)

    def image(self, *, blank=False, shift=0):
        screen = np.full((self.height, self.width, 3), 175, dtype=np.uint8)
        for index, anchor in enumerate(self.page["anchors"]):
            rect = anchor["reference_rect"]
            tile = cv2.resize(self.assets[anchor["id"]],
                              (round(rect["width"] * self.width),
                               round(rect["height"] * self.height)),
                              interpolation=cv2.INTER_NEAREST)
            x = round((rect["x"] + (shift if index == 0 else 0)) * self.width)
            y = round(rect["y"] * self.height)
            h, w = tile.shape[:2]
            if not (blank and index == 0):
                screen[y:y + h, x:x + w] = tile
        inverse = cv2.getPerspectiveTransform(self.screen_corners, self.quad)
        # Visible screen margin keeps bilinear support inside the detected phone.
        screen = cv2.copyMakeBorder(screen, 8, 8, 8, 8, cv2.BORDER_CONSTANT,
                                    value=(175, 175, 175))
        offset = np.float64([[1, 0, -8], [0, 1, -8], [0, 0, 1]])
        return png(cv2.warpPerspective(screen, inverse @ offset, (800, 800)))

    def read_frame(self, evidence_id):
        self.reads.append(evidence_id)
        return self.frames[evidence_id]

    def capture_frames(self, device_id, count, page_state):
        result = []
        for index in range(count):
            self.sequence += 1
            evidence_id = f"frame-{self.sequence}"
            data = self.next_images[index] if self.next_images else self.image()
            self.frames[evidence_id] = data
            ns = 1_000_000_000_000 + self.sequence * 250_000_000
            result.append({
                "evidence_id": evidence_id, "jpeg": data,
                "installation_binding": dict(BINDING), "source_geometry_id": GEOMETRY,
                "source_size": dict(CAMERA_SIZE),
                "source_generation_id": "22222222-2222-4222-8222-222222222222",
                "source_frame_seq": self.sequence, "source_capture_seq": self.sequence,
                "source_clock_id": "mach-absolute:" + "0" * 64,
                "clock_registration_id": "33333333-3333-4333-8333-333333333333",
                "source_captured_monotonic_ns": ns, "source_capture_uncertainty_ms": 1,
                "source_timing_provenance": "camera_pts_host_clock.v1",
                "received_monotonic_ms": ns // 1_000_000 + 20,
            })
        return result

    def capture(self, owner_id, kind="draft"):
        self.sync_calibration()
        # Reattach only the external frame-source substitute after formal app wiring.
        self.adaptation.attach_providers(capture_provider=self.capture_frames)
        return self.adaptation.capture(DEVICE, {
            "client_request_id": str(uuid.uuid4()), "principal_id": "operator-test",
            "owner_kind": kind, "owner_id": owner_id, "page_state": STATE,
            "expected_owner_revision": 1, "controlled_test_confirmed": True,
        })["capture"]

    def draft(self):
        self.sync_calibration()
        response = self.client.post("/v1/layout-template-drafts", json={
            "contract_version": "ui-layout-edit.v1", "correlation_id": str(uuid.uuid4()),
            "client_request_id": str(uuid.uuid4()), "device_id": DEVICE,
            "base_template_ref": None,
        })
        assert response.status_code == 200, response.text
        draft_id = response.json()["draft_id"]
        for name, image in self.assets.items():
            self.templates.save_asset(draft_id, name, png(image))
        return draft_id

    def write_draft(self, draft_id, capture):
        self.sync_calibration()
        return self.client.put(f"/v1/layout-template-drafts/{draft_id}/pages/{STATE}", json={
            "contract_version": "ui-layout-edit.v1", "correlation_id": str(uuid.uuid4()),
            "client_request_id": str(uuid.uuid4()), "expected_revision": 1,
            "source_evidence_ids": [e["evidence_id"] for e in capture["evidence"]],
            "page_definition": self.page,
        })

    def session(self):
        # Draft creation attaches the same providers as the real app uses.
        self.draft()
        return self.adaptation.start_session(DEVICE, {
            "template_id": "template-test", "template_version": 1,
            "principal_id": "operator-test",
        })["session_id"]

    def write_session(self, session_id, capture):
        self.sync_calibration()
        return self.adaptation.write_page(DEVICE, session_id, STATE, {
            "client_request_id": str(uuid.uuid4()), "expected_session_revision": 1,
            "source_evidence_ids": [e["evidence_id"] for e in capture["evidence"]],
            "local_overrides": [],
        })["check"]

    def close(self):
        self.client.close()
        self.adaptation.close()
        self.regions.close()
        self.templates.close()
        self.state.close()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    value = Rig(tmp_path, monkeypatch)
    yield value
    value.close()


def test_formal_draft_matches_both_rectified_frames(rig):
    draft = rig.draft()
    capture = rig.capture(draft)
    response = rig.write_draft(draft, capture)
    assert response.status_code == 200, response.text
    check = response.json()["page"]["page_check"]
    assert check["status"] == "passed", check
    assert set(rig.reads) == {e["evidence_id"] for e in capture["evidence"]}
    matched = check["anchor_results"][0]["matched_rect"]
    assert matched["x"] == pytest.approx(0.12, abs=0.006)
    assert matched["y"] == pytest.approx(0.12, abs=0.006)


def test_tight_crop_cannot_silently_replace_the_calibrated_outer_edge(rig):
    from runtime.layout_geometry import LayoutGeometryError, locate_in_region

    image = cv2.imdecode(np.frombuffer(rig.image(), np.uint8), cv2.IMREAD_COLOR)
    tight = {"rect": {"x": .1, "y": .1, "width": .8, "height": .825}}
    saved = rig.calibration["screen"]["edgeModel"]["corners"]
    with pytest.raises(LayoutGeometryError, match="粗框过紧"):
        locate_in_region(image, tight, saved)


@pytest.mark.parametrize("left", [65, 70, 75])
@pytest.mark.parametrize("dx,dy", [(0, 0), (7, 5), (-4, 3)])
def test_crop_margin_does_not_invent_motion_or_lose_camera_offset(rig, left, dx, dy):
    from runtime.layout_geometry import locate_in_region

    image = cv2.imdecode(np.frombuffer(rig.image(), np.uint8), cv2.IMREAD_COLOR)
    image = cv2.warpAffine(image, np.float32([[1, 0, dx], [0, 1, dy]]), (800, 800))
    region = {"rect": {"x": left / 800, "y": .08,
                       "width": (740 - left) / 800, "height": .86}}
    saved = np.asarray(rig.calibration["screen"]["edgeModel"]["corners"])
    pose = locate_in_region(image, region, saved)
    assert np.asarray(pose["corners"]) == pytest.approx(saved + [dx, dy], abs=.2)
    assert np.asarray(pose["matrix"])[:2, 2] == pytest.approx([dx, dy], abs=.2)


@pytest.mark.parametrize("chain", ["draft", "session"])
@pytest.mark.parametrize("bad_frame", [0, 1])
@pytest.mark.parametrize("failure", ["missing", "shifted"])
def test_either_frame_failure_or_inconsistent_position_rejects(rig, chain, bad_frame, failure):
    owner = rig.draft() if chain == "draft" else rig.session()
    images = [rig.image(), rig.image()]
    images[bad_frame] = rig.image(blank=failure == "missing",
                                  shift=0.05 if failure == "shifted" else 0)
    rig.next_images = images
    capture = rig.capture(owner, chain)
    if chain == "draft":
        response = rig.write_draft(owner, capture)
        assert response.status_code == 200, response.text
        check = response.json()["page"]["page_check"]
    else:
        check = rig.write_session(owner, capture)
    assert check["status"] == "failed", check
    assert check["anchor_results"][0]["status"] == "failed"
    dependent = next(r for r in check["item_results"] if r["id"] == "wechat_chats_tab")
    assert dependent["status"] == "failed"
    assert dependent["safe_rect"] is None
    assert check["anchor_results"][1]["status"] == "passed"
    assert set(rig.reads) == {e["evidence_id"] for e in capture["evidence"]}


@pytest.mark.parametrize("chain", ["draft", "session"])
@pytest.mark.parametrize("screen_size", [(400, 600), (600, 900)])
def test_shared_assets_work_at_different_screen_sizes(tmp_path, monkeypatch, chain, screen_size):
    rig = Rig(tmp_path, monkeypatch, *screen_size)
    try:
        owner = rig.draft() if chain == "draft" else rig.session()
        capture = rig.capture(owner, chain)
        if chain == "draft":
            response = rig.write_draft(owner, capture)
            assert response.status_code == 200, response.text
            check = response.json()["page"]["page_check"]
        else:
            check = rig.write_session(owner, capture)
        assert check["status"] == "passed", check
        assert check["anchor_results"][0]["matched_rect"]["width"] == pytest.approx(0.1)
    finally:
        rig.close()


def test_formal_draft_descriptor_is_screen_not_camera_size(rig):
    draft = rig.draft()
    stored = rig.state.state_get("draft", state_key("draft", [draft]))
    assert stored["reference_screen_descriptor"]["width_px"] == 400
    assert stored["reference_screen_descriptor"]["height_px"] == 600


def test_active_region_pointer_not_latest_and_digest_checked(rig):
    rig.draft()
    bad_latest = copy.deepcopy(rig.region)
    bad_latest.update(version=2, rect={"x": 0, "y": 0, "width": 0.2, "height": 0.2})
    rig.save_region(bad_latest)
    assert rig.adaptation._region_ref_provider(DEVICE)["version"] == 1
    draft = rig.draft()
    response = rig.write_draft(draft, rig.capture(draft))
    assert response.status_code == 200, response.text
    assert response.json()["page"]["page_check"]["status"] == "passed"
    rig.regions.cas_pointer(DEVICE, 1, 2, bad_latest["sha256"], str(uuid.uuid4()))
    draft = rig.draft()
    response = rig.write_draft(draft, rig.capture(draft))
    assert response.status_code == 409, response.text
    rig.regions.cas_pointer(DEVICE, 2, 1, "f" * 64, str(uuid.uuid4()))
    with pytest.raises(Exception, match="摘要"):
        rig.adaptation._region_ref_provider(DEVICE)


def test_missing_provider_never_falls_back_to_camera_coordinates(rig):
    from runtime.layout_drafts import LayoutDraftError, LayoutDraftService

    draft = rig.draft()
    capture = rig.capture(draft)
    unwired = LayoutDraftService(rig.templates, rig.state, frame_reader=rig.read_frame)
    with pytest.raises(LayoutDraftError, match="摆正"):
        unwired.write_page(draft, STATE, {
            "client_request_id": str(uuid.uuid4()), "expected_revision": 1,
            "source_evidence_ids": [e["evidence_id"] for e in capture["evidence"]],
            "page_definition": rig.page,
        }, "operator-test")
    assert not rig.reads


def test_api_recomputes_region_digest(rig, monkeypatch):
    rig.draft()
    corrupt = copy.deepcopy(rig.region)
    corrupt["rect"]["x"] += 0.01
    monkeypatch.setattr(rig.regions, "get_version", lambda *args: corrupt)
    with pytest.raises(Exception, match="摘要"):
        rig.adaptation._region_ref_provider(DEVICE)


def test_search_region_capture_still_needs_no_calibration_or_existing_region(rig):
    rig.calibration = None
    response = rig.adaptation.capture(DEVICE, {
        "client_request_id": str(uuid.uuid4()), "principal_id": "operator-test",
        "owner_kind": "search_region", "owner_id": None, "page_state": None,
        "expected_owner_revision": 0, "controlled_test_confirmed": True,
    })
    assert len(response["capture"]["evidence"]) == 1


def test_current_registered_camera_change_cannot_reuse_old_archived_binding(rig):
    draft = rig.draft()
    capture = rig.capture(draft)
    rig.runtime.devices.get = lambda device: {
        "daemon_binding_id": BINDING["arm_binding_id"],
        "camera_id": "replacement-camera",
        "camera_device_unique_id": "replacement-uvc",
    }
    response = rig.write_draft(draft, capture)
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("state", [None, "draft", "stale", "invalid"])
def test_formal_app_refuses_unqualified_calibration_status(rig, state):
    draft = rig.draft()
    capture = rig.capture(draft)
    rig.calibration["status"] = state
    response = rig.write_draft(draft, capture)
    assert response.status_code == 409, response.text


def test_formal_app_requires_validated_camera_mapping(rig):
    draft = rig.draft()
    capture = rig.capture(draft)
    rig.calibration["camera"]["validated"] = False
    response = rig.write_draft(draft, capture)
    assert response.status_code == 409, response.text


def test_output_over_20m_pixels_refused_before_allocating_warp(rig, monkeypatch):
    draft = rig.draft()
    capture = rig.capture(draft)
    scale = np.diag([5000 / 400, 5000 / 600, 1])
    matrix = np.array(rig.calibration["camera"]["cameraToScreen"]["homography"])
    rig.calibration["screen"] = {"screenWidth": 5000, "screenHeight": 5000}
    rig.calibration["camera"]["cameraToScreen"]["homography"] = (scale @ matrix).tolist()

    def no_allocation(*args, **kwargs):
        pytest.fail("Oversized support bitmap must never be allocated")

    monkeypatch.setattr(cv2, "warpPerspective", no_allocation)
    response = rig.write_draft(draft, capture)
    assert response.status_code == 409, response.text


def test_two_frame_merge_keeps_first_anchor_and_item_coordinates(rig):
    draft = rig.draft()
    rig.next_images = [rig.image(), rig.image(shift=0.005)]
    response = rig.write_draft(draft, rig.capture(draft))
    assert response.status_code == 200, response.text
    check = response.json()["page"]["page_check"]
    assert check["status"] == "passed", check
    anchor = check["anchor_results"][0]["matched_rect"]
    target = next(r for r in check["item_results"] if r["id"] == "wechat_chats_tab")
    assert anchor["x"] == pytest.approx(0.12, abs=0.001)
    assert target["resolved_rect"]["x"] == anchor["x"]


@pytest.mark.parametrize("chain", ["draft", "session"])
@pytest.mark.parametrize("defect", [
    "missing_calibration", "missing_binding", "missing_geometry", "wrong_device",
    "wrong_camera", "changed_binding", "changed_geometry", "missing_region",
    "region_geometry", "region_digest", "bundle_size", "source_size",
    "actual_size", "invalid_matrix", "singular_matrix", "nonfinite_matrix",
    "offscreen_matrix", "horizon_matrix", "missing_second_identity", "changed_bytes",
])
def test_geometry_evidence_fails_closed_without_consuming_capture(rig, chain, defect):
    owner = rig.draft() if chain == "draft" else rig.session()
    capture = rig.capture(owner, chain)
    stored = rig.state.state_get("capture", state_key("capture", [capture["capture_id"]]))
    if defect == "missing_calibration":
        rig.calibration = None
    elif defect == "missing_binding":
        rig.calibration["cameraBindingId"] = None
    elif defect == "missing_geometry":
        rig.calibration["sourceGeometryId"] = None
    elif defect == "wrong_device":
        rig.calibration["deviceId"] = "other-device"
    elif defect == "wrong_camera":
        rig.calibration["cameraId"] = "other-camera"
    elif defect == "changed_binding":
        rig.current["camera_binding_id"] = str(uuid.uuid4())
    elif defect == "changed_geometry":
        rig.current["source_geometry_id"] = "c" * 64
    elif defect == "missing_region":
        rig.adaptation._region_record_provider = lambda device: None
    elif defect in ("region_geometry", "region_digest"):
        region = copy.deepcopy(rig.region)
        if defect == "region_geometry":
            region["source_geometry_id"] = "c" * 64
            region["sha256"] = content_sha({k: v for k, v in region.items() if k != "sha256"})
        else:
            region["rect"]["x"] = 0.2
        rig.adaptation._region_record_provider = lambda device: region
    elif defect == "bundle_size":
        rig.calibration["frame"]["width"] = 801
    elif defect in ("source_size", "missing_second_identity"):
        source = stored["evidence"][1]["source_binding"]
        if defect == "source_size":
            source["source_size"]["width"] = 799
        else:
            source["installation_binding"]["camera_id"] = ""
        rig.state.state_update("capture", state_key("capture", [stored["capture_id"]]),
                               DEVICE, "operator-test", 1, stored)
    elif defect in ("actual_size", "changed_bytes"):
        row = stored["evidence"][1]
        image = cv2.imdecode(np.frombuffer(rig.frames[row["evidence_id"]], np.uint8),
                             cv2.IMREAD_COLOR)
        data = png(image[:790, :] if defect == "actual_size" else image + 1)
        rig.frames[row["evidence_id"]] = data
        if defect == "actual_size":
            row["image_sha256"] = hashlib.sha256(data).hexdigest()
            rig.state.state_update("capture", state_key("capture", [stored["capture_id"]]),
                                   DEVICE, "operator-test", 1, stored)
    else:
        matrices = {
            "invalid_matrix": [[1, 0], [0, 1]],
            "singular_matrix": [[1, 0, 0], [0, 0, 0], [0, 0, 1]],
            "nonfinite_matrix": [[1, 0, 0], [0, float("nan"), 0], [0, 0, 1]],
            "offscreen_matrix": [[1, 0, 30], [0, 1, 40], [0, 0, 1]],
            "horizon_matrix": [[1, 0, 0], [0, 1, 0], [0.005, 0, 1]],
        }
        rig.calibration["camera"]["cameraToScreen"]["homography"] = matrices[defect]
    if chain == "draft":
        response = rig.write_draft(owner, capture)
        assert response.status_code == 409, response.text
    else:
        with pytest.raises(AdaptationError) as failure:
            rig.write_session(owner, capture)
        assert failure.value.status == 409
    after = rig.state.state_get("capture", state_key("capture", [capture["capture_id"]]))
    assert after.get("consumed_by") is None


@pytest.fixture
def region_session(tmp_path):
    # This rig constructs only temp SQLite stores and synthetic camera images.
    from tests.test_r3_layout_rectification import (
        Rig as PerspectiveRig, build_assets, build_pages, build_provider,
        run_four_state,
    )

    rig = PerspectiveRig(tmp_path / "region-session", provider=build_provider(1))
    original = rig.region_store.get_version(rig.device, 1)
    other = copy.deepcopy(original)
    other.update(version=2, rect={"x": 0.015, "y": 0.02, "width": 0.985, "height": 0.95})
    other["sha256"] = content_sha({k: v for k, v in other.items() if k != "sha256"})
    rig.region_store.insert_version(rig.device, 2, other)
    template = rig.seed_template("region-template", build_pages(build_assets()), build_assets())

    def select(version):
        pointer = rig.region_store.get_pointer(rig.device)
        record = rig.region_store.get_version(rig.device, version)
        rig.region_store.cas_pointer(
            rig.device, pointer["revision"], version, record["sha256"], str(uuid.uuid4()))

    def prepare(version=1):
        select(version)
        result = run_four_state(rig, template)
        assert all(check["status"] == "passed" for check in result["checks"].values())
        return result["session_id"]

    rig.select_region = select
    rig.prepare_session = prepare
    yield rig
    rig.close()


def _preview_request(session_id):
    return {"capture_session_id": session_id}


def _publish_request(session_id, preview):
    return {
        "client_request_id": str(uuid.uuid4()), "session_id": session_id,
        "preview_id": preview["preview_id"], "principal_id": "operator",
        "expected_revision": 0, "accepted_items": preview["accepted_items"],
    }


@pytest.mark.parametrize("phase", ["preview", "publish"])
@pytest.mark.parametrize("old,new", [(1, 2), (2, 1)])
def test_region_change_or_rollback_requires_new_page_evidence(region_session, phase, old, new):
    rig = region_session
    sid = rig.prepare_session(old)
    before = copy.deepcopy(rig.svc.get_session(rig.device, sid))
    preview = rig.svc.preview(rig.device, _preview_request(sid)) if phase == "publish" else None
    request = _publish_request(sid, preview) if preview else _preview_request(sid)
    rig.select_region(new)
    with pytest.raises(AdaptationError) as failure:
        getattr(rig.svc, phase)(rig.device, request)
    assert failure.value.code == "items_unverified"
    assert failure.value.status == 409
    assert rig.svc.get_session(rig.device, sid) == before
    assert rig.state.adaptation_pointer_get(rig.device) is None
    if preview:
        assert rig.state.operation_get_by_request(request["client_request_id"]) is None
        assert rig.svc._load_preview(rig.device, preview["preview_id"])["consumed_by"] is None
    assert not (rig.state.data_root / "devices" / rig.device /
                "layout_adaptation" / "versions").exists()


@pytest.mark.parametrize("phase", ["preview", "publish"])
def test_missing_historical_page_region_ref_requires_recapture(region_session, phase):
    rig = region_session
    sid = rig.prepare_session()
    preview = rig.svc.preview(rig.device, _preview_request(sid)) if phase == "publish" else None
    session = rig.svc._load_session(sid)
    session["pages"]["chat_ime_hidden"]["rectify"].pop("region_ref", None)
    request = _publish_request(sid, preview) if preview else _preview_request(sid)
    with pytest.raises(AdaptationError) as failure:
        getattr(rig.svc, phase)(rig.device, request)
    assert failure.value.code == "items_unverified"
    assert rig.state.adaptation_pointer_get(rig.device) is None


def test_mixed_page_region_refs_are_not_relabelled(region_session):
    rig = region_session
    sid = rig.prepare_session()
    rig.select_region(2)
    revision = rig.svc.get_session(rig.device, sid)["revision"]
    capture = rig.svc.capture(rig.device, {
        "client_request_id": str(uuid.uuid4()), "owner_kind": "session",
        "owner_id": sid, "expected_owner_revision": revision,
        "page_state": STATE, "controlled_test_confirmed": True,
    })
    written = rig.svc.write_page(rig.device, sid, STATE, {
        "client_request_id": str(uuid.uuid4()), "expected_session_revision": revision,
        "source_evidence_ids": capture["source_evidence_ids"], "local_overrides": [],
    })
    assert written["check"]["status"] == "passed"
    with pytest.raises(AdaptationError) as failure:
        rig.svc.preview(rig.device, _preview_request(sid))
    assert failure.value.code == "items_unverified"


def test_region_change_during_preview_is_rechecked(region_session):
    rig = region_session
    sid = rig.prepare_session()
    original_capture = rig.svc._capture_provider

    def capture_and_change(*args):
        frames = original_capture(*args)
        rig.select_region(2)
        return frames

    rig.svc._capture_provider = capture_and_change
    with pytest.raises(AdaptationError) as failure:
        rig.svc.preview(rig.device, _preview_request(sid))
    assert failure.value.code == "items_unverified"
    assert not rig.state.state_scan("adaptation_preview", rig.device)


def test_region_change_after_publish_page_check_cannot_attach_new_ref(region_session, monkeypatch):
    rig = region_session
    sid = rig.prepare_session()
    preview = rig.svc.preview(rig.device, _preview_request(sid))
    original = rig.state.next_adaptation_version

    def choose_and_change(device):
        version = original(device)
        rig.select_region(2)
        return version

    monkeypatch.setattr(rig.state, "next_adaptation_version", choose_and_change)
    request = _publish_request(sid, preview)
    with pytest.raises(AdaptationError) as failure:
        rig.svc.publish(rig.device, request)
    assert failure.value.code == "items_unverified"
    assert rig.state.operation_get_by_request(request["client_request_id"]) is None
    assert rig.state.adaptation_pointer_get(rig.device) is None


def test_unchanged_selected_region_keeps_original_ref_despite_newer_version(region_session):
    rig = region_session
    sid = rig.prepare_session()
    expected_ref = rig._region_ref(rig.device)
    assert expected_ref["version"] == 1
    session = rig.svc.get_session(rig.device, sid)
    for page in session["pages"].values():
        assert page["rectify"]["region_ref"] == expected_ref
        assert "region_ref" not in page["capture"]["check"]
    preview = rig.svc.preview(rig.device, _preview_request(sid))
    result = rig.svc.publish(rig.device, _publish_request(sid, preview))
    assert result["status"] == "verified"
    assert result["record"]["installation_region_ref"] == expected_ref


def test_recapture_all_pages_after_region_change_restores_publishability(region_session):
    from runtime.layout_adaptation import PAGE_STATES

    rig = region_session
    sid = rig.prepare_session()
    old = rig.svc.get_session(rig.device, sid)
    old_ids = {e for page in old["pages"].values() for e in page["capture"]["evidence_ids"]}
    rig.select_region(2)
    revision = old["revision"]
    for state in PAGE_STATES:
        captured = rig.svc.capture(rig.device, {
            "client_request_id": str(uuid.uuid4()), "owner_kind": "session",
            "owner_id": sid, "expected_owner_revision": revision,
            "page_state": state, "controlled_test_confirmed": True,
        })
        response = rig.svc.write_page(rig.device, sid, state, {
            "client_request_id": str(uuid.uuid4()), "expected_session_revision": revision,
            "source_evidence_ids": captured["source_evidence_ids"], "local_overrides": [],
        })
        assert response["check"]["status"] == "passed"
        assert not (set(response["check"]["evidence_ids"]) & old_ids)
        revision = response["session_revision"]
    preview = rig.svc.preview(rig.device, _preview_request(sid))
    published = rig.svc.publish(rig.device, _publish_request(sid, preview))
    assert published["status"] == "verified"
    assert published["record"]["installation_region_ref"] == rig._region_ref(rig.device)
    assert published["record"]["installation_region_ref"]["version"] == 2


def test_fractional_input_crop_produced_by_write_page_has_covering_dimensions(rig):
    rig.draft()  # Attach the formal app's isolated providers.
    state = "chat_ime_hidden"
    page = copy.deepcopy(rig.page)
    page["state"] = state
    page["targets"] = [
        {"id": name, "anchor_id": f"anchor-{i}", "mode": "tap",
         "relative_rect": {"dx": 0, "dy": 0, "width": 0.1, "height": 0.08}}
        for i, name in enumerate(("back_button", "input_box"))]
    page["regions"] = [
        {"id": "title", "purpose": "title", "anchor_id": "anchor-0",
         "relative_rect": {"dx": 0, "dy": 0, "width": 0.1, "height": 0.08}},
        {"id": "input", "purpose": "input", "anchor_id": "anchor-1",
         "relative_rect": {"dx": -0.37375, "dy": 0.1255,
                           "width": 0.105, "height": 0.03075}},
        {"id": "content", "purpose": "content", "anchor_id": "anchor-1",
         "relative_rect": {"dx": 0, "dy": 0, "width": 0.1, "height": 0.08}},
    ]
    template_id = "fractional-input"
    rig.templates.save_template(template_id, 1, {
        "template_id": template_id, "version": 1, "pages": [page]})
    for name, image in rig.assets.items():
        rig.templates.save_asset(template_id, name, png(image))
    sid = rig.adaptation.start_session(DEVICE, {
        "template_id": template_id, "template_version": 1,
        "principal_id": "operator-test",
    })["session_id"]
    rig.adaptation.attach_providers(capture_provider=rig.capture_frames)
    capture = rig.adaptation.capture(DEVICE, {
        "client_request_id": str(uuid.uuid4()), "owner_kind": "session",
        "owner_id": sid, "expected_owner_revision": 1,
        "page_state": state, "controlled_test_confirmed": True,
    })
    written = rig.adaptation.write_page(DEVICE, sid, state, {
        "client_request_id": str(uuid.uuid4()), "expected_session_revision": 1,
        "source_evidence_ids": capture["source_evidence_ids"], "local_overrides": [],
    })
    assert written["check"]["status"] == "passed", written["check"]
    actual_input = next(r for r in written["check"]["item_results"] if r["id"] == "input")
    assert actual_input["resolved_rect"] == {
        "x": 0.20625, "y": 0.4055, "width": 0.105, "height": 0.03075}
    stored_page = rig.adaptation.get_session(DEVICE, sid)["pages"][state]
    reference = stored_page["color_sample"]["empty_input_asset_ref"]
    path = (rig.state.data_root / "devices" / DEVICE / "layout_adaptation" /
            "sessions" / sid / "assets" / f"{reference['id']}.png")
    asset = path.read_bytes()
    image = cv2.imdecode(np.frombuffer(asset, np.uint8), cv2.IMREAD_COLOR)
    # At 400x600: [82.5,124.5) x [243.3,261.75) covers [82:125,243:262].
    assert image.shape == (19, 43, 3)
    assert hashlib.sha256(asset).hexdigest() == reference["sha256"]


def _color_check(first, second=None):
    return {"_color_region_purposes": {"input": "foreground", "content": "background"},
            "item_results": [
        {"id": "input", "kind": "region", "status": "passed", "resolved_rect": first},
        {"id": "content", "kind": "region", "status": "passed",
         "resolved_rect": second or {"x": 0.505, "y": 0.505,
                                     "width": 0.105, "height": 0.105}},
    ]}


def test_fractional_color_sample_uses_covering_pixel_bounds(rig):
    columns = ((np.arange(100) % 40) * 5).astype(np.uint8)
    gray = np.tile(columns, (100, 1))
    image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    check = _color_check({"x": 0.205, "y": 0.205, "width": 0.105, "height": 0.105})
    sample = rig.adaptation._sample_colors(
        STATE, check, png(image), ["color-1", "color-2"])
    # 11 columns each: medians at x=25 and x=55, not the old 10-column crops.
    assert sample["foreground_rgb"] == [125, 125, 125]
    assert sample["background_rgb"] == [75, 75, 75]


def test_fractional_fallback_includes_last_covered_column(rig):
    image = np.full((100, 100, 3), 60, dtype=np.uint8)
    image[:, 30] = 230
    check = _fallback_color_check(
        {"x": 0.205, "y": 0.205, "width": 0.105, "height": 0.205},
        {"x": 0.505, "y": 0.505, "width": 0.105, "height": 0.105})
    sample = rig.adaptation._sample_colors(
        STATE, check, png(image), ["texture-1", "texture-2"])
    assert sample["foreground_rgb"] == [60, 60, 60]
    assert sample["background_rgb"] == [230, 230, 230]


@pytest.mark.parametrize("producer", ["input_reference", "colors"])
@pytest.mark.parametrize("bad_rect", [
    {"x": -0.01, "y": 0.2, "width": 0.2, "height": 0.2},
    {"x": 0.9, "y": 0.2, "width": 0.2, "height": 0.2},
    {"x": 0.2, "y": 0.95, "width": 0.2, "height": 0.1},
    {"x": 0.2, "y": 0.2, "width": 0, "height": 0.2},
    {"x": 0.2, "y": 0.2, "width": 0.01, "height": 0.2},
    {"x": float("nan"), "y": 0.2, "width": 0.2, "height": 0.2},
    {"x": True, "y": 0.2, "width": 0.2, "height": 0.2},
    {"x": 0.2, "y": 0.2, "width": 0.2},
    {},
])
def test_crop_producers_reject_invalid_bounds_without_clipping(rig, producer, bad_rect):
    gray = np.tile((np.arange(100) * 2).astype(np.uint8), (100, 1))
    frame = png(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
    check = _color_check(bad_rect)
    with pytest.raises(AdaptationError) as failure:
        if producer == "input_reference":
            rig.adaptation._archive_empty_input_reference(
                DEVICE, "invalid-crop", "chat_ime_hidden", check, frame)
        else:
            rig.adaptation._sample_colors(
                STATE, check, frame, ["invalid-1", "invalid-2"], page_failed=True)
    assert failure.value.code == "items_unverified"
    assert failure.value.status == 409
    assert not (rig.state.data_root / "devices" / DEVICE / "layout_adaptation" /
                "sessions" / "invalid-crop").exists()


def test_write_page_color_purposes_not_region_order_or_id(rig):
    page = copy.deepcopy(rig.page)
    page["regions"].extend([
        {"id": "swatch-paper", "purpose": "background", "anchor_id": "anchor-0",
         "relative_rect": {"dx": 0.3, "dy": 0.3, "width": 0.05, "height": 0.05}},
        {"id": "swatch-ink", "purpose": "foreground", "anchor_id": "anchor-0",
         "relative_rect": {"dx": 10 / 400, "dy": 10 / 600,
                           "width": 3 / 400, "height": 3 / 600}},
    ])
    rig.draft()
    template_id = "purpose-samples"
    rig.templates.save_template(template_id, 1, {
        "template_id": template_id, "version": 1, "pages": [page]})
    for name, image in rig.assets.items():
        rig.templates.save_asset(template_id, name, png(image))
    sid = rig.adaptation.start_session(DEVICE, {
        "template_id": template_id, "template_version": 1,
        "principal_id": "operator-test",
    })["session_id"]
    capture = rig.capture(sid, "session")
    check = rig.write_session(sid, capture)
    assert check["status"] == "passed", check
    sample = rig.adaptation.get_session(DEVICE, sid)["pages"][STATE]["color_sample"]
    # Existing synthetic image, untouched: one solid anchor cell and plain canvas.
    assert sample["foreground_rgb"] == rig.assets["anchor-0"][10, 10, ::-1].tolist()
    assert sample["background_rgb"] == [175, 175, 175]
    assert not any(key.startswith("_") for key in check)
    assert all("purpose" not in item for item in check["item_results"])


def test_explicit_color_purposes_accept_flat_swatches(rig):
    image = np.full((100, 100, 3), (240, 230, 220), dtype=np.uint8)
    image[20:40, 20:40] = (80, 210, 100)
    check = _color_check(
        {"x": .2, "y": .2, "width": .2, "height": .2},
        {"x": .5, "y": .5, "width": .2, "height": .2})
    sample = rig.adaptation._sample_colors(STATE, check, png(image), ["flat-1", "flat-2"])
    assert sample["foreground_rgb"] == [100, 210, 80]
    assert sample["background_rgb"] == [220, 230, 240]


def _fallback_color_check(first, second):
    check = _color_check(first, second)
    check["_color_region_purposes"] = {"input": "input", "content": "content"}
    check["_required_anchor_ids"] = ["anchor-b", "anchor-a"]
    check["anchor_results"] = [
        {"id": name, "status": "passed", "matched_rect": rect}
        for name, rect in (("anchor-b", second), ("anchor-a", first))]
    return check


@pytest.mark.parametrize("one_purpose", [False, True])
def test_color_fallback_uses_sorted_required_anchor_pixel_union(rig, one_purpose):
    image = np.full((100, 100, 3), 240, dtype=np.uint8)
    image[10:20, 10:20] = 20
    image[10:20, 20:30] = 60
    image[10:20, 30:40] = 200
    first = {"x": .1, "y": .1, "width": .2, "height": .1}
    second = {"x": .2, "y": .1, "width": .2, "height": .1}
    check = _fallback_color_check(first, second)
    if one_purpose:
        check["_color_region_purposes"]["input"] = "foreground"
    decoy = {"x": .6, "y": .6, "width": .1, "height": .1}
    check["item_results"][0]["resolved_rect"] = decoy
    check["item_results"][1]["resolved_rect"] = decoy
    check["_required_anchor_ids"].extend(["anchor-c", "anchor-d"])
    check["anchor_results"].extend([
        {"id": "0-optional", "status": "passed", "matched_rect": decoy},
        {"id": "anchor-c", "status": "failed", "matched_rect": decoy},
        {"id": "anchor-d", "status": "passed", "matched_rect": decoy},
    ])
    sample = rig.adaptation._sample_colors(STATE, check, png(image), ["union-1", "union-2"])
    # The shared 100 pixels of value 60 count once, not twice (which yields 60).
    assert sample["foreground_rgb"] == [40, 40, 40]
    assert sample["background_rgb"] == [200, 200, 200]


@pytest.mark.parametrize("dark_count,dark_value", [(15, 30), (16, 194), (0, 30)])
def test_color_fallback_rejects_small_or_low_contrast_classes(rig, dark_count, dark_value):
    image = np.full((100, 100, 3), 200, dtype=np.uint8)
    for index in range(dark_count):
        image[10 + index // 10, 10 + index % 10] = dark_value
    check = _fallback_color_check(
        {"x": .1, "y": .1, "width": .1, "height": .1},
        {"x": .3, "y": .1, "width": .1, "height": .1})
    with pytest.raises(AdaptationError) as failure:
        rig.adaptation._sample_colors(STATE, check, png(image), ["small-1", "small-2"])
    assert failure.value.code == "items_unverified"
    assert failure.value.status == 409
    assert "low_texture" in failure.value.message


def test_color_fallback_requires_two_passed_required_anchors(rig):
    gray = np.tile(((np.arange(100) % 20) * 10).astype(np.uint8), (100, 1))
    check = _fallback_color_check(
        {"x": .1, "y": .1, "width": .1, "height": .1},
        {"x": .3, "y": .1, "width": .1, "height": .1})
    check["_required_anchor_ids"] = ["anchor-a"]
    with pytest.raises(AdaptationError) as failure:
        rig.adaptation._sample_colors(
            STATE, check, png(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)), ["few-1", "few-2"])
    assert failure.value.code == "items_unverified"
    assert "low_texture" in failure.value.message
