"""Formal scroll task with real flow, temporary region/calibration and fake motion."""
import asyncio
import copy

import pytest

from drivers import calibration_bundle as cb
from drivers.arm import DriverError
from runtime.calibrated_scroll import binding_view
from runtime.layout_matching import _pixel_rect
from runtime.layout_region import canonical_digest
from tests.test_calibrated_scroll import (
    _lease, _scroll_payload, _scroll_runtime, _stable_session, _submit,
)
from tests.test_layout_observation import offline_only  # noqa: F401


@pytest.mark.parametrize("fault", [
    "none", "capture", "claim", "before_swipe", "after_motion", "motion_rejected",
])
def test_formal_scroll_preserves_context_and_motion_facts(tmp_path, monkeypatch, fault):
    async def run():
        runtime, root, _ = await _scroll_runtime(tmp_path)
        parts = runtime.layout_bundle()
        try:
            bundle = cb.load_bundle("arm-01", root)
            assert bundle["status"] == cb.MOTION_READY_STATUS
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            rect = {"x": .1, "y": .02, "width": .8, "height": .96}
            w, h = bundle["frame"]["width"], bundle["frame"]["height"]
            left, top, right, bottom = _pixel_rect(rect, w, h)
            region = {
                "device_id": "arm-01", "version": 1, "rect": rect,
                "pixel_rect": dict(left=left, top=top, right=right, bottom=bottom),
                "source_size": dict(width=w, height=h),
                "source_geometry_id": bundle["sourceGeometryId"],
                "installation_binding": {
                    "camera_id": bundle["cameraId"],
                    "camera_device_unique_id": bundle["cameraDeviceUniqueId"],
                    "camera_binding_id": bundle["cameraBindingId"]},
            }
            region["sha256"] = canonical_digest(region)
            parts["region_store"].insert_version("arm-01", 1, region)
            parts["region_store"].cas_pointer("arm-01", 0, 1, region["sha256"], "offline")
            camera, arm = runtime.devices.camera("arm-01"), runtime.devices.driver("arm-01")
            mutations = []

            def change():
                if mutations:
                    return
                mutations.append(True)
                changed = copy.deepcopy(bundle)
                changed["camera"]["cameraToScreen"]["maxErrorPx"] = 0.7
                cb.save_bundle(changed, root)

            original_swipe = arm.swipe

            async def swipe(*args, **kwargs):
                result = await original_swipe(*args, **kwargs)
                camera.scroll_by(24)
                if fault == "after_motion":
                    change()
                if fault == "motion_rejected":
                    raise DriverError("driver_uncalibrated", "offline: changed after travel")
                return result
            monkeypatch.setattr(arm, "swipe", swipe)
            if fault == "capture":
                broker = runtime.devices.camera_broker("arm-01")
                original_capture = broker.capture
                async def capture(*args, **kwargs):
                    frame = await original_capture(*args, **kwargs)
                    change()
                    return frame
                monkeypatch.setattr(broker, "capture", capture)
            if fault == "claim":
                from runtime.calibrated_scroll import get_scroll_action_store
                store = get_scroll_action_store(runtime)
                original_claim = store.claim
                def claim(*args, **kwargs):
                    result = original_claim(*args, **kwargs)
                    change()
                    return result
                monkeypatch.setattr(store, "claim", claim)
            if fault == "before_swipe":
                original_action = runtime._run_task_action
                async def action(task, capability_id, invoke):
                    if capability_id == "arm.swipe":
                        change()
                    return await original_action(task, capability_id, invoke)
                monkeypatch.setattr(runtime, "_run_task_action", action)
            task = _lease(runtime, _submit(runtime, _scroll_payload(binding)))
            done = await runtime.executor.execute(task["task_id"])
            detail = (runtime.get_task(task["task_id"]).get("step_log") or [{}])[-1].get("detail") or {}
            motions = [row for row in arm.action_log if row.get("action") == "swipe"]
            assert parts["state_store"].adaptation_pointer_get("arm-01") is None
            if fault == "none":
                assert done["status"] == "confirmed", (done, detail)
                assert detail["effect_status"] == "displaced"
                assert len(motions) == 1
            elif fault == "after_motion":
                assert done["status"] == "unknown", (done, detail)
                assert detail["mechanical_status"] == "confirmed"
                assert detail["motion_started"] is True
                assert detail["effect_status"] == "unknown"
                assert detail["manual_review_required"] is True
                assert len(motions) == 1
            elif fault == "motion_rejected":
                assert done["status"] == "unknown", (done, detail)
                assert detail["mechanical_status"] == "unknown"
                assert detail["motion_started"] is not False
                assert detail["effect_status"] == "unknown"
                assert len(motions) == 1
            else:
                assert mutations
                assert motions == [], (done, detail, motions)
                assert detail["mechanical_status"] == "not_started"
                assert detail["motion_started"] is False
            assert detail["retryable"] is False
        finally:
            await runtime.shutdown()
            for name in ("state_store", "region_store", "template_store"):
                parts[name].close()
    asyncio.run(run())
