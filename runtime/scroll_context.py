"""Pinned geometry for scroll capture and measurement, independent of templates."""
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar

from drivers import calibration_bundle
from drivers.arm import DriverError
from runtime.layout_observation import BINDING_KEYS
from runtime.layout_region import canonical_digest
from runtime.phone_region_context import (
    RegionContextError, context_matches_bundle, context_pointer, load_region_context,
)

_active_action = ContextVar("scroll_execution_context", default=None)


def scroll_motion_guard(device_id, task_id):
    active = _active_action.get()
    if active is None:
        return None
    context, original_task_id = active
    if context.device_id != device_id or original_task_id != task_id:
        error = DriverError("driver_policy_rejected", "滑动上下文不属于当前任务")
        error.motion_not_started = True
        raise error
    return context.motion_guard


class ScrollContext:
    def __init__(self, runtime, device_id, *, bundle=None):
        self.runtime = runtime
        self.device_id = device_id
        self.root = runtime._gesture_data_root(device_id)
        self.bundle = deepcopy(bundle if bundle is not None else
                               calibration_bundle.load_bundle(device_id, self.root))
        if not isinstance(self.bundle, dict):
            raise RegionContextError("calibration_invalid", "滑动观察缺少本台校准")
        self.device = self._device()
        self.store = runtime.layout_bundle()["region_store"]
        self.region = deepcopy(load_region_context(self.store, device_id))
        context_matches_bundle(self.region, self.bundle)
        self.snapshot = {
            "device_id": device_id, "device_binding": self.device,
            "calibration_sha256": canonical_digest(self.bundle),
            "search_region_pointer": context_pointer(self.region),
        }
        self.ensure_current()

    def _device(self):
        device = self.runtime.devices.get(self.device_id)
        return deepcopy({key: device.get(key) for key in (
            *BINDING_KEYS, "daemon_session", "expected_frame_width", "expected_frame_height")})

    def ensure_current(self):
        if (self._device() != self.device
                or calibration_bundle.load_bundle(self.device_id, self.root) != self.bundle
                or load_region_context(self.store, self.device_id) != self.region):
            raise RegionContextError(
                "region_context_changed", "滑动观察期间绑定、校准或粗框变化，原帧链无效")

    def motion_guard(self):
        try:
            self.ensure_current()
        except RegionContextError as exc:
            error = DriverError("driver_uncalibrated", exc.message)
            # This command has not been sent. The driver preserves prior travel.
            error.motion_not_started = True
            raise error from exc

    @contextmanager
    def action(self, task_id):
        token = _active_action.set((self, task_id))
        try:
            yield
        finally:
            _active_action.reset(token)

    def archive_context(self):
        return {"search_region_pointer": context_pointer(self.region),
                "execution_context": deepcopy(self.snapshot)}

    def validate_frame(self, frame):
        expected = {
            "camera_id": self.bundle.get("cameraId"),
            "device_unique_id": self.bundle.get("cameraDeviceUniqueId"),
            "camera_binding_id": self.bundle.get("cameraBindingId"),
            "source_geometry_id": self.bundle.get("sourceGeometryId"),
            "binding_revision": self.device.get("binding_revision"),
            "width": self.bundle.get("frame", {}).get("width"),
            "height": self.bundle.get("frame", {}).get("height"),
        }
        if any(value is not None and frame.get(key) != value
               for key, value in expected.items()):
            raise RegionContextError("frame_chain_invalid", "画面来源或尺寸与原校准绑定不符")

    def validate_record(self, record, task_id):
        if (record.get("device_id") != self.device_id
                or record.get("task_id") != task_id
                or record.get("camera_id") != self.bundle.get("cameraId")):
            raise RegionContextError("frame_chain_invalid", "位移证据不属于本台、本相机或本任务")
        context = record.get("observation_context") or {}
        if (context.get("execution_context") != self.snapshot
                or context.get("search_region_pointer") != context_pointer(self.region)):
            raise RegionContextError("region_context_changed", "位移证据缺少原始上下文或已失效")
        sampling = record.get("sampling") or {}
        self.validate_frame({**sampling, **{key: record.get(key)
                             for key in ("camera_id", "width", "height")}})
        seq = sampling.get("source_capture_seq", sampling.get("source_frame_seq"))
        if (type(seq) is not int or seq < 0
                or not isinstance(sampling.get("source_generation_id"), str)
                or not sampling["source_generation_id"]):
            raise RegionContextError("frame_chain_invalid", "位移证据缺少原来源帧序或代际")
        return seq


def unknown_measurement(reason, count):
    from drivers.scroll_observation import default_params

    return {"status": "unknown", "dy_px": None, "dx_px": None,
            "inlier_ratio": 0.0, "valid_points": None,
            "matched_ocr_rows": None, "mad_dy_px": None,
            "reason_code": reason,
            "gesture_rules_version": default_params()["gesture_rules_version"],
            "chain_frames": count}
