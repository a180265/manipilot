"""Runtime grab -> stored evidence -> real optical flow; no hardware or live data."""
import asyncio
import copy
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb
from runtime.config import PlatformConfig
from runtime.layout_region import canonical_digest
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs
from tests.test_calibrated_scroll import SCROLL_PLUGIN_SOURCE
from tests.test_layout_observation import offline_only  # noqa: F401
from tests.test_pre_enable_s2_region_gate_scroll import (
    BOX_LEFT, FH, FW, OFF, SAVED, _draw_phone, _now, _region, _v3_bundle,
)


@asynccontextmanager
async def measure_rig(tmp):
    runtime = ArmFleetRuntime(PlatformConfig(
        {"db_path": str(tmp / "fleet.db"), "evidence_dir": str(tmp / "ev"),
         "plugin_dir": str(tmp / "plugins"), "hardware_mode": "simulation",
         "daemon_supervisor_enabled": False, "hotplug_discovery_enabled": False,
         "screen_ime_enabled": False}, root=tmp))
    await runtime.startup(device_configs=make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=7,
        daemon_addr="127.0.0.1:8795", expected_frame_width=FW, expected_frame_height=FH))
    runtime.devices.get("arm-01")["daemon_session"] = "session-01"
    runtime.devices.driver("arm-01").data_root = tmp / "data"
    bundle = _v3_bundle()
    cb.save_bundle(bundle, tmp / "data")
    runtime.plugins.install(SCROLL_PLUGIN_SOURCE, operator_id="test", reason="offline")
    runtime.plugins.activate("screen-gesture-adapter", operator_id="test", reason="offline")
    task = {"task_id": "scroll-context", "device_id": "arm-01",
            "task_type": "calibrated_scroll", "status": "executing", "payload": {},
            "plugin_id": "screen-gesture-adapter"}
    task["lease_id"] = runtime.leases.acquire("arm-01", task["task_id"])["lease_id"]
    runtime.store.save_task(task)
    region = {"contract_version": "phone-search-region.v1", "device_id": "arm-01",
              "version": 1, **_region(BOX_LEFT), "reference_evidence_id": "offline",
              "created_at": _now(), "created_by": "offline"}
    region["sha256"] = canonical_digest(region)
    parts = runtime.layout_bundle()
    store = parts["region_store"]
    store.insert_version("arm-01", 1, region)
    pointer = store.cas_pointer("arm-01", 0, 1, region["sha256"], "offline")
    rig = SimpleNamespace(runtime=runtime, bundle=bundle, task=task, region=region,
                          parts=parts, offset=0, after_capture=None, capture_count=0)

    def capture():
        rig.capture_count += 1
        image = np.full((FH, FW, 3), 28, np.uint8)
        _draw_phone(image, SAVED, texture_offset=rig.offset)
        _draw_phone(image, OFF)
        data = cv2.imencode(".png", image)[1].tobytes()
        frame = {"image_bytes": data, "width": FW, "height": FH,
                 "camera_id": "cam-01", "device_id": "arm-01",
                 "device_unique_id": "uvc-01", "binding_revision": 7,
                 "camera_binding_id": bundle["cameraBindingId"],
                 "source_geometry_id": bundle["sourceGeometryId"],
                 "source_generation_id": "offline-generation",
                 "source_frame_seq": rig.capture_count,
                 "captured_at": _now(), "source_captured_at": _now(),
                 "source_received_at": _now(), "sha256": hashlib.sha256(data).hexdigest()}
        if rig.after_capture:
            rig.after_capture()
        return frame

    async def async_capture():
        return capture()

    broker = SimpleNamespace(device_id="arm-01", camera_id="cam-01",
                             capture_sync=capture, capture=async_capture,
                             _driver=runtime.devices.camera("arm-01"))
    runtime.devices._camera_brokers["arm-01"] = broker

    async def invoke(params):
        return await runtime.plugin_invoke_capability(
            "screen-gesture-adapter", task, "camera.measure_scroll", params)

    rig.invoke = invoke
    rig.pointer = {k: pointer[k] for k in ("revision", "selected_version", "selected_sha256")}
    try:
        yield rig
    finally:
        runtime.devices._camera_brokers.pop("arm-01", None)
        await runtime.shutdown()
        for name in ("state_store", "region_store", "template_store"):
            parts[name].close()


def change_calibration(rig, tmp):
    bundle = copy.deepcopy(rig.bundle)
    bundle["camera"]["cameraToScreen"]["homography"][0][0] = 1.01
    cb.save_bundle(bundle, tmp / "data")


@pytest.mark.parametrize("fault", [
    "none", "calibration", "binding", "missing_pointer", "foreign_task",
    "foreign_device", "foreign_camera", "source_generation", "missing_context",
    "tampered_bytes", "change_during_read", "duplicate_frame", "reverse_frames",
    "wrong_physical_camera", "missing_source", "wrong_dimensions", "other_action",
])
def test_measure_rejects_mixed_archived_context(tmp_path, fault):
    async def run():
        async with measure_rig(tmp_path) as rig:
            before = await rig.invoke({"op": "grab"})
            rig.offset = 20
            after = await rig.invoke({"op": "grab"})
            record = rig.runtime.evidence.get(after["evidence_id"])
            if fault == "calibration":
                change_calibration(rig, tmp_path)
            elif fault == "binding":
                rig.runtime.devices.get("arm-01")["binding_revision"] += 1
            elif fault in {"foreign_task", "foreign_device", "foreign_camera"}:
                record[{"foreign_task": "task_id", "foreign_device": "device_id",
                        "foreign_camera": "camera_id"}[fault]] = "other"
            elif fault == "source_generation":
                record["sampling"]["source_generation_id"] = "other"
            elif fault == "wrong_physical_camera":
                record["sampling"]["device_unique_id"] = "other"
                first_record = rig.runtime.evidence.get(before["evidence_id"])
                first_record["sampling"]["device_unique_id"] = "other"
                rig.runtime.store.save_evidence(first_record)
            elif fault == "missing_source":
                record["sampling"].pop("source_geometry_id")
            elif fault == "wrong_dimensions":
                record["width"] += 1
            elif fault == "other_action":
                record["action_instance_id"] = "other-action"
            elif fault == "missing_context":
                record.pop("observation_context", None)
            elif fault == "tampered_bytes":
                path = rig.runtime.evidence._dir / record["file_path"]
                data = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                data[0, 0] = 100
                path.write_bytes(cv2.imencode(".png", data)[1].tobytes())
            elif fault == "change_during_read":
                read = rig.runtime.evidence.read_bytes
                def read_then_change(record):
                    data = read(record)
                    change_calibration(rig, tmp_path)
                    return data
                rig.runtime.evidence.read_bytes = read_then_change
            rig.runtime.store.save_evidence(record)
            params = {"op": "measure",
                      "evidence_ids": [before["evidence_id"], after["evidence_id"]],
                      "roi": {"x": .1, "y": .1, "width": .8, "height": .8},
                      "roi_frame": "screen_norm",
                      "search_region_pointer": before["search_region_pointer"]}
            if fault == "missing_pointer":
                params.pop("search_region_pointer")
            elif fault == "duplicate_frame":
                params["evidence_ids"] = [before["evidence_id"]] * 2
            elif fault == "reverse_frames":
                params["evidence_ids"].reverse()
            result = await rig.invoke(params)
            if fault == "none":
                assert result["status"] == "displaced", result
                assert result["dy_px"] == pytest.approx(-20, abs=6)
            else:
                assert result["status"] == "unknown", result
            assert rig.capture_count == 2, "Measuring archived frames must not recapture"
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["invalid_region", "capture_changes_calibration"])
def test_grab_never_archives_a_rebound_frame(tmp_path, fault):
    async def run():
        async with measure_rig(tmp_path) as rig:
            if fault == "invalid_region":
                rig.parts["region_store"].cas_pointer("arm-01", 1, 2, "f" * 64, "offline")
            else:
                rig.after_capture = lambda: change_calibration(rig, tmp_path)
            with pytest.raises(Exception):
                await rig.invoke({"op": "grab"})
            assert rig.runtime.evidence.for_task(rig.task["task_id"]) == []
            if fault == "invalid_region":
                assert rig.capture_count == 0
    asyncio.run(run())
