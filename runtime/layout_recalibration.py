"""Reuse selected UI assets after an operator completes a new calibration.

The receipt names immutable layout versions and the new calibration fingerprint.
Only execution geometry is derived; archive records and historical evidence stay intact.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from drivers import calibration_bundle as cb
from drivers.screen_pose import validate_corners
from runtime.layout_state import content_sha

CONTRACT = "layout-recalibration.v1"


def receipt_path(device_id, data_root):
    # Use the same device path rules as the calibration writer.
    return cb.bundle_path(device_id, data_root).with_name("layout_recalibration.json")


def _reference(record, owner):
    return {"id": record[owner], "version": record["version"], "sha256": record["sha256"]}


def _checked_record(record):
    if record.get("sha256") != content_sha({k: v for k, v in record.items() if k != "sha256"}):
        raise ValueError("布局资料摘要不符，不能沿用")
    return record


def _selected(data_root, device_id, db_path=None):
    """Take a read-only snapshot; do not create layout storage during calibration."""
    root = Path(data_root)
    db = Path(db_path) if db_path else root / "layout.db"
    if not db.exists():
        return None
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='layout_adaptation_pointer'").fetchone():
            return None
        conn.execute("BEGIN")
        pointer = conn.execute("SELECT * FROM layout_adaptation_pointer WHERE device_id=?", (device_id,)).fetchone()
        if pointer is None:
            return None
        region_pointer = conn.execute("SELECT * FROM layout_region_pointer WHERE device_id=?", (device_id,)).fetchone()
        if region_pointer is None:
            raise ValueError("已选布局缺少粗框指针")
        path = db.parent / "devices" / device_id / "layout_adaptation" / "versions" / f"{pointer['selected_version']}.json"
        adaptation = _checked_record(json.loads(path.read_text(encoding="utf-8")))
        row = conn.execute("SELECT json FROM layout_region_versions WHERE device_id=? AND version=?",
                           (device_id, region_pointer["selected_version"])).fetchone()
        if row is None:
            raise ValueError("已选粗框不存在")
        region = _checked_record(json.loads(row["json"]))
        if (adaptation["device_id"] != device_id or region["device_id"] != device_id
                or adaptation["version"] != pointer["selected_version"]
                or adaptation["sha256"] != pointer["selected_sha256"]
                or region["version"] != region_pointer["selected_version"]
                or region["sha256"] != region_pointer["selected_sha256"]
                or adaptation["installation_region_ref"] != _reference(region, "device_id")):
            raise ValueError("布局选择在校准期间已变化，请重新确认")
        return adaptation, region


def record_completed_calibration(bundle, data_root, *, source, db_path=None):
    """Called only after manual finish or successful candidate promotion."""
    if source not in {"manual_finish", "auto_promote"}:
        raise ValueError("只有完成新校准才能登记布局复用")
    device_id = bundle.get("deviceId")
    if (bundle.get("status") not in {"ready", "motion_ready"}
            or bundle.get("schemaVersion") != 3
            or (bundle.get("camera") or {}).get("validated") is not True):
        return None
    selected = _selected(data_root, device_id, db_path)
    if selected is None:
        return None
    adaptation, region = selected
    from runtime.layout_geometry import matrix_and_size
    _, width, height = matrix_and_size(bundle)
    descriptor = adaptation["screen_descriptor"]
    if (descriptor["width_px"], descriptor["height_px"], descriptor["orientation"]) != (
            width, height, (bundle.get("screen") or {}).get("orientation")):
        raise ValueError("新校准屏幕尺寸或方向与页面素材不同，需适配受影响资料")
    if adaptation["ime_device_id"] != bundle.get("imeDeviceId"):
        raise ValueError("新校准手机与已选布局不符，不能自动沿用")
    receipt = {"contract_version": CONTRACT, "device_id": device_id,
               "calibration_fingerprint": cb.motion_calibration_fingerprint(bundle),
               "adaptation_ref": _reference(adaptation, "device_id"),
               "template_ref": adaptation["template_ref"],
               "region_ref": _reference(region, "device_id"),
               "source": source, "completed_at": cb.now_iso()}
    receipt["sha256"] = content_sha(receipt)
    path = receipt_path(device_id, data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".layout-recalibration-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return receipt


def derive_execution_layout(data_root, bundle, adaptation, region):
    """Return fresh execution copies only when the exact receipt still applies."""
    path = receipt_path(bundle.get("deviceId"), data_root)
    if not path.exists():
        return None
    try:
        receipt = _checked_record(json.loads(path.read_text(encoding="utf-8")))
        if (receipt.get("contract_version") != CONTRACT
                or receipt.get("device_id") != bundle.get("deviceId")
                or receipt.get("source") not in {"manual_finish", "auto_promote"}
                or bundle.get("status") not in {"ready", "motion_ready"}
                or receipt.get("calibration_fingerprint") != cb.motion_calibration_fingerprint(bundle)
                or receipt.get("adaptation_ref") != _reference(adaptation, "device_id")
                or receipt.get("template_ref") != adaptation["template_ref"]
                or receipt.get("region_ref") != _reference(region, "device_id")):
            return None
        corners = validate_corners(bundle["screen"]["edgeModel"]["corners"])
        width, height = bundle["frame"]["width"], bundle["frame"]["height"]
        # The newly measured screen supplies the search area. Do not carry over
        # old camera pixels after a USB/camera/pose change. A bounded margin lets
        # the normal live pose checks measure the screen rather than clip it.
        left = max(0, math.floor(float(corners[:, 0].min())) - 32)
        top = max(0, math.floor(float(corners[:, 1].min())) - 32)
        right = min(width, math.ceil(float(corners[:, 0].max())) + 33)
        bottom = min(height, math.ceil(float(corners[:, 1].max())) + 33)
        if left >= right or top >= bottom:
            return None
        derived = deepcopy(region)
        derived["installation_binding"].update(
            camera_id=bundle["cameraId"], camera_device_unique_id=bundle["cameraDeviceUniqueId"],
            camera_binding_id=bundle["cameraBindingId"])
        derived.update(source_geometry_id=bundle["sourceGeometryId"], source_size=deepcopy(bundle["frame"]),
                       rect={"x": left / width, "y": top / height,
                             "width": (right-left) / width, "height": (bottom-top) / height},
                       pixel_rect={"left": left, "top": top, "right": right, "bottom": bottom},
                       recalibration_receipt_sha256=receipt["sha256"])
        derived["sha256"] = content_sha({k: v for k, v in derived.items() if k != "sha256"})
        adapted = deepcopy(adaptation)
        adapted["screen_descriptor"]["calibration_geometry_hash"] = (
            bundle.get("coordinateFrameHash") or bundle.get("bindingFingerprint"))
        return adapted, derived
    except (ValueError, TypeError, KeyError, OSError):
        return None


def execution_region(data_root, bundle, region, *, db_path=None):
    if not receipt_path(bundle.get("deviceId"), data_root).exists():
        return region
    selected = _selected(data_root, bundle.get("deviceId"), db_path)
    if selected is None or selected[1] != region:
        return region
    derived = derive_execution_layout(data_root, bundle, selected[0], region)
    return derived[1] if derived else region
