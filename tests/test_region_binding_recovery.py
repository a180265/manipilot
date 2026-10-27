"""Binding changes must leave the operator's region editor usable."""
from __future__ import annotations

import uuid

import pytest

from tests.layout_d2_helpers import AppEnv, FrameBridgeFake, phone_scene


def _preview(env, frame):
    return env.client.post(
        "/v1/devices/arm-01/phone-search-region/preview",
        headers=env.auth(), json={
            "contract_version": "phone-search-region-preview.v1",
            "correlation_id": str(uuid.uuid4()),
            "source_evidence_id": frame["evidence_id"],
            "rect": {"x": .2, "y": .15, "width": .6, "height": .7}})


def _publish(env, preview, revision=0):
    return env.client.put(
        "/v1/devices/arm-01/phone-search-region", headers=env.auth(), json={
            "contract_version": "phone-search-region-write.v1",
            "correlation_id": str(uuid.uuid4()),
            "client_request_id": str(uuid.uuid4()),
            "preview_id": preview["preview_id"],
            "region_digest": preview["region_digest"],
            "expected_revision": revision, "publish": True})


def _read(env):
    return env.client.get(
        "/v1/devices/arm-01/phone-search-region", headers={
            **env.auth(), "X-Correlation-Id": str(uuid.uuid4()),
            "X-Layout-Contract-Version": "phone-search-region-read.v1"})


@pytest.mark.parametrize("changed", [
    {"camera_id": "cam-02", "camera_binding_id": None},
    {"camera_device_unique_id": "uvc-new", "camera_binding_id": None},
    {"camera_binding_id": "44444444-4444-4444-8444-444444444444"},
    {"source_geometry_id": "c" * 64},
    {"expected_frame_width": 1280},
])
def test_old_region_remains_readable_after_binding_change(tmp_path, changed):
    with AppEnv(tmp_path) as env:
        bridge = FrameBridgeFake(env.runtime)
        frame = bridge.push(phone_scene())
        preview = _preview(env, frame)
        assert preview.status_code == 200, preview.text
        saved = _publish(env, preview.json())
        assert saved.status_code == 200, saved.text
        before = _read(env)
        assert before.status_code == 200, before.text
        assert before.json()["status"] == "verified"

        env.runtime.devices._devices["arm-01"].update(changed)
        response = _read(env)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "needs_review"
        assert response.json()["reasons"] == ["source_binding_changed"]
        assert response.json()["selected"] == before.json()["selected"]
        assert response.json()["revision"] == before.json()["revision"]


def test_operator_can_replace_region_with_new_binding_without_recollecting_template(tmp_path):
    with AppEnv(tmp_path) as env:
        bridge = FrameBridgeFake(env.runtime)
        old_frame = bridge.push(phone_scene())
        preview = _preview(env, old_frame)
        saved = _publish(env, preview.json())
        assert saved.status_code == 200, saved.text
        registered = env.runtime.devices._devices["arm-01"]
        registered.update(camera_id="cam-02", camera_device_unique_id="uvc-new",
                          camera_binding_id=None, source_geometry_id=None,
                          status="paused", calibration_status="due")
        stale = _read(env)
        assert stale.status_code == 200, stale.text
        assert stale.json()["status"] == "needs_review"

        # Formal layout capture still refuses to treat the old archive as current.
        draft = env.client.post("/v1/layout-template-drafts", headers=env.auth(), json={
            "contract_version": "ui-layout-edit.v1",
            "correlation_id": str(uuid.uuid4()), "client_request_id": str(uuid.uuid4()),
            "device_id": "arm-01", "base_template_ref": None})
        assert draft.status_code == 409, draft.text
        assert draft.json()["code"] == "layout_binding_changed"

        bridge.binding.update(camera_id="cam-02", camera_device_unique_id="uvc-new",
                              camera_binding_id="44444444-4444-4444-8444-444444444444")
        current = bridge.push(phone_scene())
        # An old frozen image cannot be rebound to the new source.
        rejected = _preview(env, old_frame)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "layout_binding_changed"
        replacement = _preview(env, current)
        assert replacement.status_code == 200, replacement.text
        saved_new = _publish(env, replacement.json(), revision=saved.json()["revision"])
        assert saved_new.status_code == 200, saved_new.text
        current_view = _read(env)
        assert current_view.status_code == 200, current_view.text
        assert current_view.json()["status"] == "verified"
        assert current_view.json()["selected"]["version"] == 2
        store = env.runtime.layout_bundle(tmp_path / "layout.db")["region_store"]
        assert store.get_version("arm-01", 1)["installation_binding"]["camera_id"] == "cam-01"
        assert store.get_version("arm-01", 2)["installation_binding"]["camera_id"] == "cam-02"


def test_region_without_current_source_is_readable_but_needs_review(tmp_path, monkeypatch):
    with AppEnv(tmp_path) as env:
        frame = FrameBridgeFake(env.runtime).push(phone_scene())
        preview = _preview(env, frame)
        assert preview.status_code == 200, preview.text
        saved = _publish(env, preview.json())
        assert saved.status_code == 200, saved.text
        before = _read(env)
        assert before.json()["status"] == "verified"

        monkeypatch.setattr(env.runtime.evidence, "latest_source_frame_v2",
                            lambda device_id: None)
        response = _read(env)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "needs_review"
        assert response.json()["reasons"] == ["source_binding_changed"]
        assert response.json()["selected"] == before.json()["selected"]
