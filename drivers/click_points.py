"""Pure geometry for explicitly requested alternatives inside a verified target.

No retries, location credentials or device access are owned by this module.
"""
from __future__ import annotations

import numpy as np


RETRY_TARGETS = frozenset({"conversation_item", "back_button", "input_box"})


def validate_attempt(target_id, attempt):
    if type(attempt) is not int or attempt not in (0, 1, 2):
        raise ValueError("click_attempt 必须是整数 0、1 或 2")
    if attempt and target_id not in RETRY_TARGETS:
        raise ValueError("此目标禁止替代落点：仅会话行、返回键、输入框允许")
    return attempt


def rectangle_point(target_id, rect, width, height, attempt=0):
    validate_attempt(target_id, attempt)
    size = np.array([rect["width"] * width, rect["height"] * height], dtype=float)
    point = np.array([(rect["x"] + rect["width"] / 2) * width,
                      (rect["y"] + rect["height"] / 2) * height])
    if not np.isfinite(size).all() or min(size) <= 0 or not np.isfinite(point).all():
        raise ValueError("目标安全区无效")
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
              *, camera_polygon=None, require_polygon=False):
    """Inset the projected quadrilateral by measured error plus 2 screen pixels.

    The alternative stays on a line through the original centre. Half-plane
    distances constrain it against all four *actual* edges, including perspective.
    An unavailable inset never alters the original centre on attempt zero.
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
        elif require_polygon:
            raise ValueError("共享布局文字缺少原始四角，不能使用相机包围框扩大安全区")
        else:
            box = np.asarray(bbox, dtype=float)
            if (box.shape != (4,) or not np.isfinite(box).all()
                    or box[2] <= box[0] or box[3] <= box[1]):
                raise ValueError("原始 OCR 边界无效")
            x1, y1, x2, y2 = box
            camera_polygon = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
        polygon = project(matrix, camera_polygon)
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
        if attempt:
            raise ValueError(f"目标不支持替代点击落点：{exc}") from exc
        safety["reason"] = str(exc)
        return camera_point, screen_point, False, safety
