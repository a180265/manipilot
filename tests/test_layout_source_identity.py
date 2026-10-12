"""Canonical frame-bridge identity must survive persistence and reload."""
import uuid

import pytest

from tests.layout_d2_helpers import AppEnv, FrameBridgeFake, phone_scene
from tests.test_layout_observation import offline_only  # noqa: F401


@pytest.mark.parametrize("identity", ["canonical", "legacy", "conflict", "missing"])
def test_region_read_uses_archived_physical_identity(tmp_path, identity):
    env = AppEnv(tmp_path)
    try:
        bridge = FrameBridgeFake(env.runtime)
        first = bridge.push(phone_scene())
        record = bridge.push(phone_scene())
        headers = {**env.auth(), "Content-Type": "application/json"}
        preview = env.client.post("/v1/devices/arm-01/phone-search-region/preview",
            json={"contract_version": "phone-search-region-preview.v1",
                  "correlation_id": str(uuid.uuid4()),
                  "rect": {"x": .2, "y": .15, "width": .65, "height": .8},
                  "source_evidence_id": first["evidence_id"]}, headers=headers)
        assert preview.status_code == 200, preview.text
        assert preview.json()["status"] == "verified", preview.text
        saved = env.client.put("/v1/devices/arm-01/phone-search-region",
            json={"contract_version": "phone-search-region-write.v1",
                  "correlation_id": str(uuid.uuid4()), "client_request_id": str(uuid.uuid4()),
                  "expected_revision": 0, "preview_id": preview.json()["preview_id"],
                  "region_digest": preview.json()["region_digest"], "publish": True},
            headers=headers)
        assert saved.status_code == 200, saved.text
        record.pop("jpeg_bytes")
        if identity == "canonical":
            record["camera_unique_id"] = record.pop("device_unique_id")
        elif identity == "conflict":
            record["camera_unique_id"] = "other-camera"
        elif identity == "missing":
            record.pop("device_unique_id")
        raw = env.runtime.evidence.read_source_frame_bytes(record["evidence_id"])
        env.runtime.evidence.save_source_frame_v2(
            device_id="arm-01", generation=record["source_generation_id"],
            seq=record["source_capture_seq"], jpeg=raw, record=record)
    finally:
        env.close()
    with AppEnv(tmp_path) as reopened:
        response = reopened.client.get(
            "/v1/devices/arm-01/phone-search-region",
            headers={**reopened.auth(), "X-Correlation-Id": str(uuid.uuid4()),
                     "X-Layout-Contract-Version": "phone-search-region-read.v1"})
        assert response.status_code == (200 if identity in {"canonical", "legacy"} else 409), response.text


@pytest.mark.parametrize("identity", ["canonical", "legacy", "conflict", "missing"])
def test_layout_capture_uses_same_archive_identity(identity):
    from runtime.layout_capture import frame_from_record

    record = {
        "evidence_id": "ev", "camera_id": "cam", "camera_binding_id": "binding",
        "source_geometry_id": "geometry", "frame_width": 640, "frame_height": 480,
        "source_generation_id": "generation", "source_frame_seq": 1,
        "source_capture_seq": 1, "source_clock_id": "clock",
        "source_captured_monotonic_ns": 1, "capture_uncertainty_ms": 1,
        "timing_provenance": "camera_pts_host_clock.v1",
    }
    if identity != "missing":
        record["camera_unique_id" if identity == "canonical" else "device_unique_id"] = "uvc"
    if identity == "conflict":
        record["camera_unique_id"] = "other"
    if identity in {"conflict", "missing"}:
        with pytest.raises(ValueError):
            frame_from_record(record, read_bytes=lambda _: b"frame", arm_binding_id="arm")
    else:
        frame = frame_from_record(record, read_bytes=lambda _: b"frame", arm_binding_id="arm")
        assert frame["installation_binding"]["camera_device_unique_id"] == "uvc"
