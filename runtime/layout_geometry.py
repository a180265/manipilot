"""Shared camera-to-screen evidence validation; no IO except the supplied reader."""
from __future__ import annotations

import copy
import hashlib

import cv2
import numpy as np

from drivers import calibration_bundle
from runtime.layout_state import content_sha
from runtime.layout_matching import sum_error_bounds

MAX_IMAGE_PIXELS = 20_000_000


class LayoutGeometryError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _reject(message, code="items_unverified"):
    raise LayoutGeometryError(code, message)


def _size(value):
    if not isinstance(value, dict):
        _reject("来源尺寸缺失")
    width, height = value.get("width"), value.get("height")
    if any(type(v) is not int or v <= 0 or v > 10000 for v in (width, height)):
        _reject("来源尺寸无效")
    if width * height > MAX_IMAGE_PIXELS:
        _reject("摆正输入或输出超过 2000 万像素上限")
    return width, height


def matrix_and_size(bundle):
    from drivers.calibration_display import validate_display_reference
    try:
        validate_display_reference(bundle)
    except (ValueError, KeyError, TypeError) as exc:
        _reject(str(exc))
    try:
        model = (bundle.get("camera") or {}).get("cameraToScreen") or {}
        matrix = np.asarray(model.get("homography"), dtype=float)
        if matrix.shape == (9,):
            matrix = matrix.reshape(3, 3)
        screen = bundle.get("screen") or {}
        width, height = _size({
            "width": screen.get("screenWidth") or screen.get("widthPx"),
            "height": screen.get("screenHeight") or screen.get("heightPx")})
        if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
            raise ValueError
        scale = float(np.max(np.abs(matrix)))
        if scale == 0:
            raise ValueError
        matrix = matrix / scale
        if np.linalg.matrix_rank(matrix) != 3 or min(width, height) < 100:
            raise ValueError
    except (TypeError, ValueError, OverflowError, np.linalg.LinAlgError):
        _reject("本台 v3 缺少有效摆正矩阵或屏幕尺寸")
    return matrix, width, height


def mapping_error_bound(bundle):
    """Training and independent measured maxima, in screen pixels; no defaults."""
    if not isinstance(bundle, dict) or bundle.get("schemaVersion") != 3 \
            or bundle.get("status") not in ("ready", "motion_ready"):
        return None
    xy, camera = bundle.get("xy") or {}, bundle.get("camera") or {}
    if not isinstance(xy, dict) or not isinstance(camera, dict) \
            or xy.get("validated") is not True or camera.get("validated") is not True:
        return None
    model = camera.get("cameraToScreen") or {}
    if not isinstance(model, dict):
        return None
    return calibration_bundle.mapping_error_bound(bundle)


def _installation(value):
    value = value or {}
    return value.get("installation_binding") or value


def _same_installation(actual, expected):
    actual = _installation(actual)
    expected = _installation(expected)
    for key in ("camera_id", "camera_device_unique_id",
                "camera_binding_id", "arm_binding_id"):
        if not actual.get(key) or actual[key] != expected.get(key):
            _reject("当前绑定、校准、区域或证据身份不完整/不一致",
                    "source_binding_changed")


def _screen_footprint(matrix, width, height):
    corners = np.array([[0, 0, 1], [width - 1, 0, 1],
                        [width - 1, height - 1, 1], [0, height - 1, 1]], dtype=float)
    points = corners @ np.linalg.inv(matrix).T
    denominators = points[:, 2]
    # A projective horizon inside the output invalidates the whole image,
    # even when the four divided corner coordinates appear finite.
    if not np.isfinite(points).all() or not (
            np.all(denominators > 1e-10) or np.all(denominators < -1e-10)):
        _reject("摆正矩阵在屏幕内穿过无穷远，不能作为图像证据")
    return points[:, :2] / denominators[:, None]


def _region_bounds(region, camera_size):
    try:
        rect = region["rect"]
        x, y, w, h = [float(rect[key]) for key in ("x", "y", "width", "height")]
        if not np.isfinite([x, y, w, h]).all() or min(x, y) < 0 \
                or min(w, h) <= 0 or x + w > 1 or y + h > 1:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        _reject("当前生效粗框无效")
    cw, ch = camera_size
    return np.array([x * cw, y * ch]), np.array([(x + w) * cw, (y + h) * ch])


def locate_in_region(image, region, saved):
    """Constrain calibrated edge fitting and candidate checks to the saved crop."""
    from drivers import screen_pose
    from drivers.screen_edge import ScreenEdgeError, locate_screen_quad
    from runtime.layout_region import _screen_candidates
    from runtime.layout_matching import _pixel_rect

    height, width = image.shape[:2]
    try:
        left, top, right, bottom = _pixel_rect(region["rect"], width, height)
    except (KeyError, TypeError, ValueError) as exc:
        raise LayoutGeometryError("items_unverified", "当前生效粗框无效") from exc
    crop = image[top:bottom, left:right]
    if len(_screen_candidates(crop)) >= 2:
        raise LayoutGeometryError(
            "items_unverified", "粗框内存在多个屏幕候选：multiple_screen_candidates")
    try:
        expected = screen_pose.validate_corners(saved) - [left, top]
        found = locate_screen_quad(
            crop, expected.tolist(), tolerance_px=32,
            require_fittable=True, reject_clipped_candidate=True)
        corners = np.asarray(found["corners"]) + [left, top]
        if (np.any(corners <= [left + 1, top + 1])
                or np.any(corners >= [right - 1, bottom - 1])):
            raise ValueError("当前屏幕被粗框裁断：screen_outside_region")
        return screen_pose.fit_pose(saved, corners)
    except (ScreenEdgeError, ValueError) as exc:
        raise LayoutGeometryError("items_unverified", str(exc)) from exc


def warp_root_from_raw(image, bundle, pose, region):
    """Resample real raw pixels once; validate continuous physical/source coverage."""
    from drivers.screen_pose import validate_corners

    matrix, width, height = matrix_and_size(bundle)
    saved = validate_corners(bundle["screen"]["edgeModel"]["corners"])
    points = _screen_footprint(matrix, width, height)
    edges = np.roll(saved, -1, axis=0) - saved
    delta = points[:, None, :] - saved[None, :, :]
    cross = edges[None,:,0] * delta[:,:,1] - edges[None,:,1] * delta[:,:,0]
    distance = cross / np.linalg.norm(edges, axis=1)
    # 1e-5 camera px is numerical precision, never a geometric expansion.
    # Integer polygon masks invent invalid border pixels before resampling.
    if not (np.all(distance >= -1e-5) or np.all(distance <= 1e-5)):
        _reject("摆正采样包含校准屏幕以外的像素")
    try:
        movement = np.asarray(pose["matrix"], dtype=float)
        if movement.shape != (3,3) or not np.isfinite(movement).all():
            raise ValueError
        composed = matrix @ np.linalg.inv(movement)
    except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
        _reject("当前手机姿态矩阵无效")
    live_points = _screen_footprint(composed, width, height)
    camera_size = (image.shape[1], image.shape[0])
    if camera_size != _size(bundle.get("frame")):
        _reject("实际原图尺寸与校准来源不一致", "source_binding_changed")
    lower, upper = _region_bounds(region, camera_size)
    if np.any(live_points < 0) or np.any(live_points > np.asarray(camera_size)-1):
        _reject("摆正屏幕超出原图，不能使用边界填充作为证据")
    if np.any(live_points < lower) or np.any(live_points > upper):
        _reject("摆正屏幕超出当前生效粗框")
    return cv2.warpPerspective(image, composed, (width,height), flags=cv2.INTER_LINEAR)


def rectify_capture(*, device_id, snapshot, current_binding, bundle,
                    region_record, capture, frame_reader):
    """Validate both raw frames, then rectify without accepting border fill."""
    from drivers import screen_pose

    if not isinstance(bundle, dict) or bundle.get("schemaVersion") != 3:
        _reject("四态匹配要求本台有效 v3 校准")
    if bundle.get("status") not in ("ready", "motion_ready") \
            or (bundle.get("camera") or {}).get("validated") is not True:
        _reject("本台 v3 尚未完成校准或摄像头映射验证")
    if bundle.get("deviceId") != device_id:
        _reject("校准不属于当前设备", "source_binding_changed")
    expected = {
        "camera_id": bundle.get("cameraId"),
        "camera_device_unique_id": bundle.get("cameraDeviceUniqueId"),
        "camera_binding_id": bundle.get("cameraBindingId"),
        "arm_binding_id": _installation(current_binding).get("arm_binding_id"),
    }
    _same_installation(current_binding, expected)
    _same_installation(snapshot, expected)
    geometry = bundle.get("sourceGeometryId")
    if not geometry or (current_binding or {}).get("source_geometry_id") != geometry:
        _reject("当前来源几何与 v3 校准不一致", "source_binding_changed")
    if not snapshot or snapshot.get("source_geometry_id") != geometry:
        _reject("页面快照缺少或已变更来源几何", "source_binding_changed")
    camera_size = _size(bundle.get("frame"))
    if snapshot.get("source_size") is not None \
            and _size(snapshot["source_size"]) != camera_size:
        _reject("页面快照尺寸与 v3 校准不一致", "source_binding_changed")
    region = copy.deepcopy(region_record)
    if not isinstance(region, dict):
        _reject("缺少当前生效粗框，不能默认使用全图")
    try:
        digest = content_sha({k: v for k, v in region.items() if k != "sha256"})
    except (TypeError, ValueError):
        _reject("当前生效粗框内容无效")
    if not region.get("sha256") or region["sha256"] != digest:
        _reject("当前生效粗框摘要不符")
    if type(region.get("version")) is not int or region["version"] < 1:
        _reject("当前生效粗框版本无效")
    _same_installation(region, expected)
    if region.get("device_id") != device_id \
            or region.get("source_geometry_id") != geometry \
            or _size(region.get("source_size")) != camera_size:
        _reject("当前生效粗框的设备/来源几何不一致", "source_binding_changed")
    matrix, width, height = matrix_and_size(bundle)
    points = _screen_footprint(matrix, width, height)
    lower, upper = _region_bounds(region, camera_size)
    # Both the complete screen and every bilinear sample must belong to the
    # real camera frame; clipping or synthetic border pixels are not evidence.
    if np.any(points < -1e-7) or np.any(points > np.array(camera_size) - 1 + 1e-7):
        _reject("摆正屏幕超出原图或被裁断，不能使用边界填充作为证据")
    if np.any(points < lower - 1e-7) or np.any(points > upper + 1e-7):
        _reject("v3 屏幕边界超出当前生效粗框")
    saved = (bundle.get("screen", {}).get("edgeModel") or {}).get("corners")
    try:
        saved = screen_pose.validate_corners(saved).tolist()
    except ValueError:
        _reject("本台 v3 缺少可核验的屏幕边界，不能登记运行模板")
    evidence = capture.get("evidence") or []
    if capture.get("device_id") != device_id or len(evidence) != 2 \
            or len({e.get("evidence_id") for e in evidence}) != 2:
        _reject("页面匹配必须使用本设备两帧证据", "page_evidence_invalid")
    frames = []
    for row in evidence:
        source = row.get("source_binding") or {}
        _same_installation(source, expected)
        if row.get("device_id") != device_id or source.get("device_id") != device_id \
                or source.get("source_geometry_id") != geometry \
                or _size(source.get("source_size")) != camera_size:
            _reject("证据设备/来源几何/尺寸与校准不一致", "source_binding_changed")
        evidence_id = row["evidence_id"]
        if source.get("evidence_id") != evidence_id:
            _reject("证据编号与来源证明不一致", "page_evidence_invalid")
        data = frame_reader(evidence_id)
        if not data or hashlib.sha256(data).hexdigest() != row.get("image_sha256"):
            _reject("原图字节与证据摘要不一致")
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise LayoutGeometryError("source_unavailable", "受控证据原图无法解码", 503)
        if (image.shape[1], image.shape[0]) != camera_size:
            _reject("实际原图尺寸与可信来源/校准尺寸不一致", "source_binding_changed")
        pose = locate_in_region(image, region, saved)
        live_quad = np.asarray(pose["corners"])
        if np.any(live_quad < lower) or np.any(live_quad > upper) \
                or np.any(live_quad < 0) \
                or np.any(live_quad > np.array(camera_size) - 1):
            _reject("当前手机超出原图或生效粗框，不能裁断后登记")
        rectified = warp_root_from_raw(image, bundle, pose, region)
        ok, encoded = cv2.imencode(".png", rectified)
        if not ok:
            raise LayoutGeometryError("source_unavailable", "摆正图编码失败", 503)
        frames.append(encoded.tobytes())
    summary = {
        "source_evidence_id": evidence[0]["evidence_id"],
        "source_size": {"width": camera_size[0], "height": camera_size[1]},
        "rectified_size": {"width": width, "height": height},
        "coordinate_domain": "camera->screen", "matrix_kind": "cameraToScreen",
        "matrix_version": bundle.get("coordinateFrameHash")
        or bundle.get("bindingFingerprint") or content_sha({"homography": matrix.tolist()}),
        "source_geometry_id": geometry, "camera_binding_id": bundle["cameraBindingId"],
        "evidence_ids": [e["evidence_id"] for e in evidence],
        "region_ref": {"id": device_id, "version": region["version"],
                       "sha256": region["sha256"]},
    }
    return {"png": frames[0], "frames": frames, "summary": summary,
            "mapping_error_bound_px": mapping_error_bound(bundle)}


def merge_page_checks(checks, page, screen_size, *, mapping_error_bound_px=None):
    """Intersection of two real checks, preserving the existing wire shape."""
    from runtime.layout_matching import apply_target_safety

    first, second = checks
    result = copy.deepcopy(first)
    reasons = set(first["reason_codes"]) | set(second["reason_codes"])
    unstable = set()
    width, height = screen_size

    def pixel(value, scale):
        measured = value * scale
        nearest = round(measured)
        return nearest if abs(measured - nearest) <= 1e-9 else measured

    for index, (a, b) in enumerate(zip(first["anchor_results"], second["anchor_results"])):
        chosen = a if a["status"] != "passed" else b if b["status"] != "passed" else a
        merged = copy.deepcopy(chosen)
        if a["status"] == b["status"] == "passed":
            ra, rb = a["matched_rect"], b["matched_rect"]
            delta = max(abs(pixel(ra[k], scale) - pixel(rb[k], scale))
                        for k, scale in (("x", width), ("y", height),
                                         ("width", width), ("height", height)))
            if delta > 3:
                merged.update(status="failed", matched_rect=None,
                              localization_error_bound_px=None, reason_code="anchor_ambiguous")
                reasons.add("anchor_ambiguous")
            else:
                merged["localization_error_bound_px"] = sum_error_bounds(max(
                    a["localization_error_bound_px"], b["localization_error_bound_px"]), delta)
        if merged["status"] != "passed":
            unstable.add(merged["id"])
        result["anchor_results"][index] = merged
    # Failed matcher checks may contain additional synthetic missing-item rows.
    # Group by kind/id and retain failure if either frame has one.
    grouped = {}
    for check in checks:
        for item in check["item_results"]:
            key = item["kind"], item["id"]
            if key not in grouped or item["status"] != "passed":
                grouped[key] = copy.deepcopy(item)
    for kind, field in (("target", "targets"), ("region", "regions")):
        for definition in page.get(field) or []:
            item = grouped.get((kind, definition["id"]))
            if item is not None and definition.get("anchor_id") in unstable:
                item.update(status="failed", resolved_rect=None,
                            safe_rect=None, reason_code="anchor_missing")
                reasons.add("anchor_missing")
    result["item_results"] = list(grouped.values())
    result["reason_codes"] = sorted(reasons)
    result["status"] = "passed" if (
        first["status"] == second["status"] == "passed" and not unstable
        and all(r["status"] == "passed" for r in result["item_results"])
    ) else "failed"
    return apply_target_safety(result, page, screen_size, mapping_error_bound_px)
