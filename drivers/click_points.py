"""Pure geometry for explicitly requested alternatives inside a verified target.

No retries, location credentials or device access are owned by this module.
"""
from __future__ import annotations

import numpy as np


RETRY_TARGETS = frozenset({"conversation_item", "back_button", "input_box"})


def validate_policy(policy, screen_size):
    """Device-only preference, never a statement that the screen is reliable."""
    try:
        if (not isinstance(policy, dict)
                or set(policy) != {"mode", "preferredInsetsPx"}
                or policy["mode"] != "screen_interior"):
            raise ValueError
        insets = policy["preferredInsetsPx"]
        if not isinstance(insets, dict) or set(insets) != {"left", "top", "right", "bottom"}:
            raise ValueError
        values = [*screen_size, *insets.values()]
        if any(type(value) not in (int, float) or not np.isfinite(value) or value < 0
               for value in values):
            raise ValueError
        width, height = screen_size
        if (min(width, height) <= 0 or insets["left"] + insets["right"] >= width
                or insets["top"] + insets["bottom"] >= height):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("本台点击选点策略无效，需完整且有限的屏幕内部边距") from None
    return {"mode": "screen_interior", "preferredInsetsPx": dict(insets)}


def policy_from_bundle(bundle):
    xy = bundle.get("xy") or {}
    if "clickPointPolicy" not in xy:
        return None
    screen = bundle.get("screen") or {}
    return validate_policy(xy["clickPointPolicy"], [
        screen.get("screenWidth") or screen.get("widthPx"),
        screen.get("screenHeight") or screen.get("heightPx")])


def _polygon_planes(polygon):
    polygon = np.asarray(polygon, dtype=float)
    if polygon.shape != (4, 2) or not np.isfinite(polygon).all():
        raise ValueError("目标真实四角无效")
    edges = np.roll(polygon, -1, axis=0) - polygon
    lengths = np.linalg.norm(edges, axis=1)
    following = np.roll(edges, -1, axis=0)
    turns = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
    if min(lengths) <= 1e-8 or not (np.all(turns > 1e-8) or np.all(turns < -1e-8)):
        raise ValueError("目标真实四角必须为非退化凸多边形")
    normals = np.column_stack((-edges[:, 1], edges[:, 0])) / lengths[:, None]
    normals *= np.sign(turns[0])
    return polygon, edges, normals, np.sum(normals * polygon, axis=1)


def _clip(polygon, normal, bound):
    """Intersect a convex polygon with normal.dot(point) >= bound."""
    result = []
    for previous, current in zip(np.roll(polygon, 1, axis=0), polygon):
        a, b = float(normal @ previous - bound), float(normal @ current - bound)
        if (a >= 0) != (b >= 0):
            result.append(previous + (current - previous) * (a / (a - b)))
        if b >= 0:
            result.append(current)
    return np.asarray(result, dtype=float).reshape(-1, 2)


def _interior_point(target_id, original, polygon, screen_size, policy, attempt, *, inset=0.0):
    policy = validate_policy(policy, screen_size)
    width, height = screen_size
    polygon, edges, normals, bounds = _polygon_planes(polygon)
    if np.any(polygon < 0) or np.any(polygon > [width, height]):
        raise ValueError("目标安全区超出手机屏幕")
    original = np.asarray(original, dtype=float)
    if original.shape != (2,) or not np.isfinite(original).all():
        raise ValueError("目标原中心无效")
    insets = policy["preferredInsetsPx"]
    normals = np.vstack((normals, [[1, 0], [0, 1], [-1, 0], [0, -1]]))
    bounds = np.r_[bounds + inset, insets["left"], insets["top"],
                   -(width - insets["right"]), -(height - insets["bottom"])]
    feasible = polygon
    for normal, bound in zip(normals, bounds):
        feasible = _clip(feasible, normal, bound)
        if len(feasible) < 3:
            raise ValueError("目标安全区与屏幕内部偏好区没有可点击交集")
    area = abs(float(np.sum(feasible[:, 0] * np.roll(feasible[:, 1], -1)
                            - feasible[:, 1] * np.roll(feasible[:, 0], -1)))) / 2
    if area <= 1e-6:
        raise ValueError("目标安全区与屏幕内部偏好区交集退化")
    distances = normals @ original - bounds
    point = original.copy() if (np.all(distances[:4] > 1e-7)
                                and np.all(distances[4:] >= 0)) else feasible.mean(axis=0)
    distances = normals @ point - bounds
    if np.any(distances[:4] <= 1e-7) or np.any(distances[4:] < -1e-7):
        raise ValueError("目标交集没有足够的内部选点空间")
    # Pick the longest symmetric direction that remains inside the *same*
    # intersection, rather than sending a retry back toward the old centre.
    axes = [np.array([1., 0.]), np.array([0., 1.]), *edges]
    reach, direction = 0., None
    for axis in axes:
        axis = axis / np.linalg.norm(axis)
        projections = np.abs(normals @ axis)
        active = projections > 1e-10
        candidate = float(np.min(np.maximum(distances[active], 0) / projections[active]))
        if candidate > reach:
            reach, direction = candidate, axis
    offset = min(12., reach / 2)
    supported = target_id in RETRY_TARGETS and offset >= 2
    if attempt and not supported:
        raise ValueError("目标交集没有足够的替代落点空间")
    if attempt:
        point += offset * direction * (-1 if attempt == 1 else 1)
    distances = normals @ point - bounds
    if np.any(distances[:4] <= 1e-7) or np.any(distances[4:] < -1e-7):
        raise ValueError("选点越过目标或屏幕内部偏好区")
    return point, supported, {
        "originalCenter": original.tolist(), "aim": point.tolist(),
        "selectionOffset": (point - original).tolist(), "policy": policy,
        "feasible_polygon": feasible.tolist(), "max_offset_px": offset if supported else 0.,
    }


def validate_attempt(target_id, attempt):
    if type(attempt) is not int or attempt not in (0, 1, 2):
        raise ValueError("click_attempt 必须是整数 0、1 或 2")
    if attempt and target_id not in RETRY_TARGETS:
        raise ValueError("此目标禁止替代落点：仅会话行、返回键、输入框允许")
    return attempt


def rectangle_point(target_id, rect, width, height, attempt=0, *, policy=None):
    validate_attempt(target_id, attempt)
    size = np.array([rect["width"] * width, rect["height"] * height], dtype=float)
    point = np.array([(rect["x"] + rect["width"] / 2) * width,
                      (rect["y"] + rect["height"] / 2) * height])
    if not np.isfinite(size).all() or min(size) <= 0 or not np.isfinite(point).all():
        raise ValueError("目标安全区无效")
    if policy is not None:
        left, top = np.array([rect["x"] * width, rect["y"] * height])
        right, bottom = np.array([left, top]) + size
        chosen, supported, safety = _interior_point(
            target_id, point, [[left, top], [right, top], [right, bottom], [left, bottom]],
            [width, height], policy, attempt)
        return chosen, supported, {"source": "shared_layout", "safe_rect": dict(rect), **safety}
    axis = int(np.argmax(size))
    offset = min(12.0, float(size[axis]) / 4)
    supported = target_id in RETRY_TARGETS and offset >= 2
    if attempt and not supported:
        raise ValueError("目标安全区没有足够的替代落点空间")
    if attempt:
        point[axis] += offset * (-1 if attempt == 1 else 1)
    return point, supported, {"source": "shared_layout", "safe_rect": dict(rect),
                              "max_offset_px": offset if supported else 0.0}


def project(matrix, points):
    points = np.asarray(points, dtype=float)
    mapped = np.column_stack((points, np.ones(len(points)))) @ np.asarray(matrix).T
    if not np.isfinite(mapped).all() or not (
            np.all(mapped[:, 2] > 1e-10) or np.all(mapped[:, 2] < -1e-10)):
        raise ValueError("点击安全区无法投影")
    return mapped[:, :2] / mapped[:, 2, None]


def ocr_point(target_id, bbox, camera_point, matrix, error_bound, screen_size, attempt=0,
              *, camera_polygon=None, require_polygon=False, policy=None):
    """Inset the projected quadrilateral by measured error plus 2 screen pixels.

    The alternative stays on a line through the original centre. Half-plane
    distances constrain it against all four *actual* edges, including perspective.
    An unavailable inset preserves legacy attempt zero only with policy disabled.
    """
    validate_attempt(target_id, attempt)
    camera_point = np.asarray(camera_point, dtype=float)
    screen_point = None
    safety = {"source": "camera_ocr", "max_offset_px": 0.0}
    try:
        screen_point = project(matrix, [camera_point])[0]
        if (type(error_bound) not in (int, float) or not np.isfinite(error_bound)
                or error_bound < 0):
            raise ValueError("校准最大误差未知")
        margin = float(error_bound) + 2.0
        if camera_polygon is not None:
            camera_polygon = np.asarray(camera_polygon, dtype=float)
            if camera_polygon.shape != (4, 2) or not np.isfinite(camera_polygon).all():
                raise ValueError("原始 OCR 四角无效")
        elif require_polygon or policy is not None:
            raise ValueError("共享布局文字缺少原始四角，不能使用相机包围框扩大安全区")
        else:
            box = np.asarray(bbox, dtype=float)
            if (box.shape != (4,) or not np.isfinite(box).all()
                    or box[2] <= box[0] or box[3] <= box[1]):
                raise ValueError("原始 OCR 边界无效")
            x1, y1, x2, y2 = box
            camera_polygon = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
        polygon = project(matrix, camera_polygon)
        if policy is not None:
            screen_point, supported, choice = _interior_point(
                target_id, screen_point, polygon, screen_size, policy, attempt, inset=margin)
            camera_point = project(np.linalg.inv(matrix), [screen_point])[0]
            safety.update(screen_polygon=polygon.tolist(), inset_px=margin, **choice)
            return camera_point, screen_point, supported, safety
        edges = np.roll(polygon, -1, axis=0) - polygon
        lengths = np.linalg.norm(edges, axis=1)
        if min(lengths) <= 1e-8:
            raise ValueError("原始 OCR 边界退化")
        normals = np.column_stack((-edges[:, 1], edges[:, 0])) / lengths[:, None]
        orientation = np.sign(edges[0, 0] * edges[1, 1] - edges[0, 1] * edges[1, 0])
        normals *= orientation
        distances = np.sum(normals * (screen_point - polygon), axis=1)
        if orientation == 0 or min(distances) <= margin:
            raise ValueError("原始 OCR 安全区没有足够的内缩空间")
        # Average opposite edge vectors to select the quadrilateral's long axis.
        axes = np.array([edges[0] - edges[2], edges[1] - edges[3]]) / 2
        axis = axes[int(np.argmax(np.linalg.norm(axes, axis=1)))]
        direction = axis / np.linalg.norm(axis)
        projections = np.abs(normals @ direction)
        active = projections > 1e-10
        reach = float(np.min((distances[active] - margin) / projections[active]))
        offset = min(12.0, reach / 2)
        if offset < 2 or target_id not in RETRY_TARGETS:
            raise ValueError("原始 OCR 安全区没有足够的替代落点空间")
        candidates = np.array([screen_point - offset * direction,
                               screen_point + offset * direction])
        if any(not (0 <= p[0] < screen_size[0] and 0 <= p[1] < screen_size[1])
               or np.any(np.sum(normals * (p - polygon), axis=1) <= margin)
               for p in candidates):
            raise ValueError("替代落点越过 OCR 安全边界")
        safety.update(screen_polygon=polygon.tolist(), inset_px=margin,
                      max_offset_px=offset)
        if attempt:
            screen_point = candidates[attempt - 1]
            camera_point = project(np.linalg.inv(matrix), [screen_point])[0]
        return camera_point, screen_point, True, safety
    except (TypeError, ValueError, OverflowError, np.linalg.LinAlgError) as exc:
        if attempt or policy is not None:
            raise ValueError(f"目标不支持本次点击落点：{exc}" if policy is not None
                             else f"目标不支持替代点击落点：{exc}") from exc
        safety["reason"] = str(exc)
        return camera_point, screen_point, False, safety
