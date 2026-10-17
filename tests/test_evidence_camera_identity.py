"""Screenshot identity across the broker, evidence store and plugin context."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from runtime.evidence import EvidenceStore
from runtime.util import sha256_bytes
from tasks.plugin_api import StepContext
from tests.helpers import PROBE_MANIFEST, make_device_configs, make_runtime


def test_plugin_capture_preserves_actual_broker_camera_identity(tmp_path, monkeypatch):
    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs())
        try:
            camera = runtime.devices.camera("arm-01")
            source = camera.capture_sync()
            source.update(source_frame_seq=42, source_captured_at=source["captured_at"],
                          source_generation_id="test-source-generation",
                          camera_binding_id="test-camera-binding", source_geometry_id="test-geometry",
                          binding_revision=3, device_unique_id="test-camera-source")
            monkeypatch.setattr(camera, "capture_sync", lambda: deepcopy(source))
            broker = runtime.devices.camera_broker("arm-01")
            assert not hasattr(broker, "device_id")
            task = {"task_id": "screenshot-identity", "device_id": "arm-01",
                    "plugin_id": PROBE_MANIFEST["plugin_id"], "status": "executing",
                    "action_call_count": 0, "cancel_requested": False}
            task["lease_id"] = runtime.leases.acquire("arm-01", task["task_id"])["lease_id"]
            runtime.store.save_task(task)
            ctx = StepContext(runtime, task, PROBE_MANIFEST)
            record = await ctx.capture_evidence("摄像头来源身份回归")
            assert record["camera_id"] == broker.camera_id == ctx.device_context["camera_id"]
            assert ctx.get_evidence(record["evidence_id"]) == record
            raw = (ctx.evidence_root / record["file_path"]).read_bytes()
            assert sha256_bytes(raw) == record["sha256"] == source["sha256"]
            assert record["sampling"]["source_frame_seq"] == 42
            assert record["sampling"]["camera_binding_id"] == source["camera_binding_id"]
            assert record["sampling"]["source_captured_at"] == source["source_captured_at"]
            assert record["task_id"] == task["task_id"] and record["device_id"] == "arm-01"
            assert runtime.get_task(task["task_id"])["action_call_count"] == 0
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("identity,expected", [({"device_id": "legacy-camera"}, "legacy-camera"), ({}, None)])
def test_direct_legacy_capture_keeps_available_identity_only(tmp_path, identity, expected):
    records = []
    store = EvidenceStore(SimpleNamespace(save_evidence=records.append), tmp_path / "evidence")
    raw = b"isolated-evidence-bytes"
    frame = {"image_bytes": raw, "sha256": sha256_bytes(raw),
             "width": 1, "height": 1, "captured_at": "2026-10-18T00:00:00Z"}
    camera = SimpleNamespace(capture_sync=lambda: frame, **identity)
    record = store.capture_screenshot(device_id="arm-01", camera=camera,
                                     task_id="legacy-capture", label="旧驱动来源")
    assert record["camera_id"] == expected
    assert records == [record]
