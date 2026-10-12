"""Read-only coordinates and binding checks for production screen gestures."""
from __future__ import annotations

import json
import math

import cv2
import numpy as np

from drivers import calibration_bundle as cb
from drivers.daemon_protocol import (
    DOUBLE_TAP_RELEASE_MM, DOUBLE_TAP_DEFAULT_INTERVAL_MS,
    SWIPE_DURATION_MAX_MS, SWIPE_DURATION_MIN_MS,
    SWIPE_FEED_MAX, XY_FEED_MIN,
    check_feed, check_swipe, check_tap,
)

BINDING_KEYS = (
    "device_id", "daemon_session", "camera_id", "camera_device_unique_id",
    "binding_revision", "calibration_fingerprint", "frame_width", "frame_height",
)


def number(value, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label}必须是有限数字")
    return float(value)


def binding_snapshot(bundle: dict) -> dict:
    if not isinstance(bundle, dict) or bundle.get("status") not in {"ready", cb.MOTION_READY_STATUS}:
        raise ValueError("现有校准尚未完成验收")
    if bundle["status"] == cb.MOTION_READY_STATUS:
        checks = cb.validate_calibration_bundle(
            bundle, device_id=bundle.get("deviceId"), daemon_session=bundle.get("daemonSession"),
            camera_id=bundle.get("cameraId"),
            camera_device_unique_id=bundle.get("cameraDeviceUniqueId"),
            binding_revision=bundle.get("bindingRevision"), daemon_addr=bundle.get("daemonAddr"),
            frame_width=(bundle.get("frame") or {}).get("width"),
            frame_height=(bundle.get("frame") or {}).get("height"))
        if checks:
            raise ValueError("；".join(check["message"] for check in checks))
    frame = bundle.get("frame") or {}
    binding = {
        "device_id": bundle.get("deviceId"),
        "daemon_session": bundle.get("daemonSession"),
        "camera_id": bundle.get("cameraId"),
        "camera_device_unique_id": bundle.get("cameraDeviceUniqueId"),
        "binding_revision": bundle.get("bindingRevision"),
        "calibration_fingerprint": cb.motion_calibration_fingerprint(bundle),
        "frame_width": frame.get("width"),
        "frame_height": frame.get("height"),
    }
    for key in ("device_id", "daemon_session", "camera_id", "camera_device_unique_id"):
        if not isinstance(binding[key], str) or not binding[key].strip():
            raise ValueError(f"校准绑定缺少 {key}")
    for key in ("binding_revision", "frame_width", "frame_height"):
        if type(binding[key]) is not int or binding[key] < (0 if key == "binding_revision" else 1):
            raise ValueError(f"校准绑定 {key} 无效")
    return binding


def require_binding(requested: dict, bundle: dict) -> dict:
    current = binding_snapshot(bundle)
    if (not isinstance(requested, dict) or set(requested) != set(BINDING_KEYS)
            or json.dumps(requested, sort_keys=True, allow_nan=False)
            != json.dumps(current, sort_keys=True, allow_nan=False)):
        raise ValueError("设备、会话或校准已变化，请重新获取手势资格")
    return current


# calibrated-scroll.v1 新绑定（2026-10-03 规格 §5.2）：十个必填字段。
# binding_revision 取本台设备登记修订（bundle.bindingRevision），不是相机
# 服务全局诊断版本；全局诊断代际不进入比较（相机附件边界）。
BINDING_V3_KEYS = (
    "device_id", "daemon_session", "camera_id", "camera_device_unique_id",
    "camera_binding_id", "source_geometry_id", "binding_revision",
    "calibration_fingerprint", "frame_width", "frame_height",
)


def binding_snapshot_v3(bundle: dict) -> dict:
    """calibrated_scroll 专用 v3 绑定快照；v2 档案一律拒绝，不得加字段续用。

    FIX-07：schemaVersion=3 只能来自真实校准链（安装 UUID/来源几何身份由
    保存/提升链路写入）；v3 的机械证据检查沿用 v2 标准（掩码副本压回 v2
    版本号走既有校验分支，不回写档案），指纹按原始 v3 内容重算
    （motion_calibration_fingerprint 显式 v2/v3 分支：v3 排除全局
    bindingRevision 并纳入两新字段）。
    """
    if not isinstance(bundle, dict):
        raise ValueError("校准档案缺失")
    if int(bundle.get("schemaVersion") or 0) != cb.BUNDLE_SCHEMA_VERSION_V3:
        raise ValueError(
            f"校准版本过旧（schemaVersion={bundle.get('schemaVersion')}）："
            "calibrated_scroll 需要实际验证完成的 schemaVersion=3 校准，"
            "旧 v2 不得加字段升级续用")
    for key in ("cameraBindingId", "sourceGeometryId"):
        value = bundle.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"v3 校准绑定缺少 {key}")
    base = binding_snapshot({**bundle, "schemaVersion": cb.V2_SCHEMA_VERSION})
    binding = {**base,
               "calibration_fingerprint": cb.motion_calibration_fingerprint(bundle),
               "camera_binding_id": bundle["cameraBindingId"],
               "source_geometry_id": bundle["sourceGeometryId"]}
    if set(binding) != set(BINDING_V3_KEYS):
        raise ValueError("v3 绑定字段集合不一致")
    return binding


def require_binding_v3(requested: dict, bundle: dict) -> dict:
    """语义比较（附件 §2）：九字段全比；binding_revision（设备登记修订）
    随绑定对象携带、不参与比较——其变化只触发重读通道事实。"""
    current = binding_snapshot_v3(bundle)
    if not isinstance(requested, dict) or set(requested) != set(BINDING_V3_KEYS):
        raise ValueError("设备、会话、相机绑定或校准已变化，请重新获取 calibrated-scroll 能力")
    compare = (lambda d: {k: d[k] for k in BINDING_V3_KEYS if k != "binding_revision"})
    if (json.dumps(compare(requested), sort_keys=True, allow_nan=False)
            != json.dumps(compare(current), sort_keys=True, allow_nan=False)):
        raise ValueError("设备、会话、相机绑定或校准已变化，请重新获取 calibrated-scroll 能力")
    return current


def resolve_point(point: dict, bundle: dict) -> dict:
    if not isinstance(point, dict) or set(point) != {"source", "x", "y"} \
            or point["source"] != "screen_normalized":
        raise ValueError("屏幕点必须为 screen_normalized 的 x、y")
    x, y = number(point["x"], "屏幕横向位置"), number(point["y"], "屏幕纵向位置")
    if not 0 <= x <= 1 or not 0 <= y <= 1:
        raise ValueError("屏幕位置必须在 0 到 1 之间")
    screen, xy = bundle.get("screen") or {}, bundle.get("xy") or {}
    width = number(screen.get("screenWidth"), "屏幕宽度")
    height = number(screen.get("screenHeight"), "屏幕高度")
    if width <= 0 or height <= 0:
        raise ValueError("屏幕尺寸无效")
    sx, sy = x * width, y * height
    # Use screen-space calibration uncertainty, not the camera UI's coarse
    # marking inset, for this explicit calibration-bound coordinate contract.
    camera = (bundle.get("camera") or {}).get("cameraToScreen") or {}
    uncertainty = max(1.0, number(xy.get("maxResidualPx", cb.XY_RESIDUAL_MAX_PX),
                                  "机械映射误差"))
    uncertainty += max(0.0, number(camera.get("maxErrorPx", cb.CAMERA_REPROJECTION_MAX_PX),
                                    "相机映射误差"))
    if min(sx, sy, width - sx, height - sy) < uncertainty:
        raise ValueError("屏幕点过于接近物理边缘，超出当前校准精度允许的范围")
    matrix = np.asarray(camera.get("homography"), dtype=float)
    if matrix.shape == (9,):
        matrix = matrix.reshape(3, 3)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() \
            or abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError("相机屏幕映射无效")
    projected = np.linalg.solve(matrix, [sx, sy, 1])
    if abs(projected[2]) < 1e-9:
        raise ValueError("屏幕点投影发散")
    cx, cy = (projected[:2] / projected[2]).tolist()
    binding = binding_snapshot(bundle)
    if not 0 <= cx < binding["frame_width"] or not 0 <= cy < binding["frame_height"]:
        raise ValueError("屏幕点投影超出当前相机画面")
    edge = screen.get("edgeModel") or {}
    corners = np.asarray(edge.get("corners"), dtype=np.float32)
    # The image contour is approximate, unlike the independently fitted touch
    # projection above. Honor its recorded tolerance only; screen-space bounds
    # and calibration uncertainty still apply without moving the requested point.
    edge_tolerance = number(edge.get("tolerancePx", 0.0), "屏幕轮廓容差")
    if not 0 <= edge_tolerance <= cb.DEFAULT_EDGE_TOLERANCE_PX:
        raise ValueError("屏幕轮廓容差超出已支持范围")
    if (corners.shape != (4, 2) or not np.isfinite(corners).all()
            or not cv2.isContourConvex(corners)
            or cv2.pointPolygonTest(corners, (cx, cy), True) < -edge_tolerance):
        raise ValueError("屏幕点超出已校准的屏幕轮廓及其容差，不是机械行程超限")
    rx, ry = cb.robot_from_screen(xy.get("robotToScreen") or {}, sx, sy)
    check_x, check_y = cb.screen_from_robot(xy["robotToScreen"], rx, ry)
    if math.hypot(check_x - sx, check_y - sy) > uncertainty:
        raise ValueError("屏幕坐标反解未收敛，禁止使用不确定的机械位置")
    return {"robotX": round(number(rx, "机械 X"), 3),
            "robotY": round(number(ry, "机械 Y"), 3),
            "cameraX": cx, "cameraY": cy,
            "frameW": binding["frame_width"], "frameH": binding["frame_height"],
            "screenX": sx, "screenY": sy, "edgeMarginPx": uncertainty}


def calibrated_profile(bundle: dict) -> dict:
    z = bundle.get("z") or {}
    hover = z.get("hoverZ")
    return {
        "hoverZ": number(z.get("zSafe") if hover is None else hover, "校准悬停高度"),
        "tapDepth": number(z.get("tapDepth"), "校准点击深度"),
        "tapFeed": number(z.get("tapFeed"), "校准点击速度"),
    }


def automatic_swipe_duration(distance: float) -> int:
    distance = number(distance, "滑动距离")
    shortest = max(int(SWIPE_DURATION_MIN_MS), math.ceil(distance / SWIPE_FEED_MAX * 60000) + 1)
    longest = min(int(SWIPE_DURATION_MAX_MS), math.floor(distance / XY_FEED_MIN * 60000))
    if shortest > longest:
        raise ValueError("滑动行程无法同时满足当前速度和时长限值")
    return min(max(600, shortest), longest)


def point_from_camera(point: dict, bundle: dict) -> dict:
    if not isinstance(point, dict) or set(point) != {"cameraX", "cameraY", "frameW", "frameH"}:
        raise ValueError("相机标记必须包含画面坐标和完整尺寸")
    binding = binding_snapshot(bundle)
    if (type(point["frameW"]) is not int or type(point["frameH"]) is not int
            or (point["frameW"], point["frameH"]) !=
            (binding["frame_width"], binding["frame_height"])):
        raise ValueError("相机标记尺寸与当前校准不一致")
    cx, cy = number(point["cameraX"], "相机 X"), number(point["cameraY"], "相机 Y")
    if not 0 <= cx < point["frameW"] or not 0 <= cy < point["frameH"]:
        raise ValueError("相机标记超出画面")
    h = bundle["camera"]["cameraToScreen"]["homography"]
    sx, sy = cb.apply_homography(h, cx, cy)
    result = {"x": sx / bundle["screen"]["screenWidth"], "y": sy / bundle["screen"]["screenHeight"]}
    resolve_point({"source": "screen_normalized", **result}, bundle)
    return result


def plan_gesture(action: str, points: dict, bundle: dict) -> dict:
    """Calculate the exact production points/timing, without motion or writes."""
    binding = binding_snapshot(bundle)
    profile = calibrated_profile(bundle)
    check_tap(profile["tapDepth"])
    check_feed(profile["tapFeed"], axis="Z")
    keys = {"point"} if action == "double_tap" else {"start", "end"} if action == "swipe" else set()
    if not keys or not isinstance(points, dict) or set(points) != keys:
        raise ValueError("仅允许双击的一点或滑动的两点，禁止覆盖机械参数")
    resolved = {}
    for key in keys:
        value = points[key]
        if not isinstance(value, dict) or set(value) != {"x", "y"}:
            raise ValueError("手势点必须且仅包含屏幕 x、y")
        resolved[key] = resolve_point({"source": "screen_normalized", **value}, bundle)
    result = {
        "action": action, **{key: dict(points[key]) for key in sorted(keys)},
        "binding": binding, "calibrationFingerprint": binding["calibration_fingerprint"],
        "profile": profile, "calculated": True, "blockingReasons": [],
        "effectStatus": "unverified",
        "points": {key: {name: value[name] for name in ("cameraX", "cameraY", "frameW", "frameH")}
                   for key, value in resolved.items()},
    }
    if action == "double_tap":
        lift = min(profile["tapDepth"], DOUBLE_TAP_RELEASE_MM)
        result.update(intervalMs=DOUBLE_TAP_DEFAULT_INTERVAL_MS, repeatLiftMm=lift,
                      contactIntervalMinMs=2 * lift / profile["tapFeed"] * 60000
                      + DOUBLE_TAP_DEFAULT_INTERVAL_MS)
    else:
        start, end = resolved["start"], resolved["end"]
        validate_swipe_path(start, end, bundle)
        dx, dy = end["robotX"] - start["robotX"], end["robotY"] - start["robotY"]
        distance = math.hypot(dx, dy)
        duration = automatic_swipe_duration(distance)
        check_swipe(dx, dy, profile["tapDepth"], distance / duration * 60000, profile["tapFeed"])
        result.update(durationMs=duration, distanceMm=round(distance, 3))
    return result


def default_plans(bundle: dict) -> list[dict]:
    binding_snapshot(bundle)
    specs = (
        ("double_tap_center", "中心双击", "double_tap", {"point": {"x": .5, "y": .5}}),
        ("swipe_up", "上滑", "swipe", {"start": {"x": .5, "y": .75}, "end": {"x": .5, "y": .3}}),
        ("swipe_down", "下滑", "swipe", {"start": {"x": .5, "y": .3}, "end": {"x": .5, "y": .75}}),
        ("swipe_left", "左滑", "swipe", {"start": {"x": .75, "y": .5}, "end": {"x": .25, "y": .5}}),
        ("swipe_right", "右滑", "swipe", {"start": {"x": .25, "y": .5}, "end": {"x": .75, "y": .5}}),
        ("bottom_up", "底边上滑候选", "swipe", {"start": {"x": .5, "y": .99}, "end": {"x": .5, "y": .8}}),
    )
    plans = []
    for identifier, label, action, points in specs:
        try:
            if identifier == "bottom_up":
                # Generate a candidate inside the measured edge, never clip a
                # caller's arbitrary point or change the calibration itself.
                for step in range(91):
                    points = {**points, "start": {"x": .5, "y": (990 - step) / 1000}}
                    try:
                        planned = plan_gesture(action, points, bundle)
                        break
                    except ValueError:
                        if step == 90:
                            raise
            else:
                planned = plan_gesture(action, points, bundle)
        except (ValueError, TypeError, KeyError, np.linalg.LinAlgError) as exc:
            planned = {"action": action, "calculated": False, "blockingReasons": [str(exc)],
                       "points": {}, "effectStatus": "unverified"}
        plans.append({"id": identifier, "label": label, **planned})
    return plans


def validate_swipe_path(start: dict, end: dict, bundle: dict) -> None:
    """Check the actual straight robot stroke, including quadratic extrema."""
    model = bundle["xy"]["robotToScreen"]
    width, height = bundle["screen"]["screenWidth"], bundle["screen"]["screenHeight"]
    dx, dy = end["robotX"] - start["robotX"], end["robotY"] - start["robotY"]

    def at(t):
        return cb.screen_from_robot(model, start["robotX"] + dx * t, start["robotY"] + dy * t)

    first, middle, last = at(0), at(.5), at(1)
    fractions = {n / 32 for n in range(33)}
    for axis in (0, 1):
        a = 2 * (last[axis] + first[axis] - 2 * middle[axis])
        b = last[axis] - first[axis] - a
        if abs(a) > 1e-9 and 0 < -b / (2 * a) < 1:
            fractions.add(-b / (2 * a))
    for t in fractions:
        sx, sy = at(t)
        resolve_point({"source": "screen_normalized", "x": sx / width, "y": sy / height}, bundle)
