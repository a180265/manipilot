"""Translate authenticated Android display geometry into this calibration root."""
from __future__ import annotations

import math

from drivers import gesture_program as gp


def coordinate_frame(device_id, bundle, display, data_root):
    frame = gp.load_coordinate_frame(device_id, data_root)
    if frame is None or gp.is_internal_coordinate_frame(frame):
        raise ValueError("实时输入法点击缺少本次校准的物理屏幕坐标桥")
    gp.validate_coordinate_frame_shape(frame)
    gp.validate_coordinate_frame(frame, bundle)
    run = (bundle.get("autoCalibration") or {}).get("runId") or "manual-finish"
    if frame.get("calibration_run_id") != run:
        raise ValueError("输入法坐标桥不属于当前校准轮次")
    for key in ("root_width_px", "root_height_px", "display_width_px", "display_height_px"):
        if type(frame.get(key)) is not int or frame[key] <= 0:
            raise ValueError("输入法坐标桥尺寸无效")
    if (type(frame.get("density")) not in (int, float)
            or not math.isfinite(frame["density"]) or frame["density"] <= 0):
        raise ValueError("输入法坐标桥密度无效")
    origin = frame.get("root_origin_display_px")
    if (not isinstance(origin, list) or len(origin) != 2
            or any(type(v) is not int or v < 0 for v in origin)
            or origin[0] + frame["root_width_px"] > frame["display_width_px"]
            or origin[1] + frame["root_height_px"] > frame["display_height_px"]):
        raise ValueError("输入法坐标桥原点或屏幕范围无效")
    if (not isinstance(display, dict)
            or type(display.get("display_id")) is not int
            or display.get("display_id") != 0
            or any(type(display.get(key)) is not int
                   for key in ("width_px", "height_px", "rotation"))
            or (display.get("width_px"), display.get("height_px")) !=
               (frame["display_width_px"], frame["display_height_px"])
            or display.get("rotation") != frame["rotation"]
            or type(display.get("density")) not in (int, float)
            or not math.isfinite(display["density"])
            or display["density"] <= 0
            or abs(display["density"] - frame["density"]) > 1e-6):
        raise ValueError("输入法 display 尺寸、方向、密度或显示器与校准坐标桥不一致")
    return frame


def root_geometry(ref, frame):
    """Return root centre and bounds without modifying the original display ref."""
    display = ref.get("display") or {}
    if (ref.get("frameW"), ref.get("frameH")) != (
            display.get("width_px"), display.get("height_px")):
        raise ValueError("输入法 display 尺寸与原始快照不一致")
    bounds = ref.get("bounds")
    if not isinstance(bounds, dict):
        raise ValueError("输入法目标缺少真实可点击范围")
    values = [bounds.get(k) for k in ("left", "top", "right", "bottom")]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError("输入法目标边界无效")
    left, top, right, bottom = values
    if not (0 <= left < right <= display["width_px"]
            and 0 <= top < bottom <= display["height_px"]):
        raise ValueError("输入法目标边界超出物理屏幕")
    x, y = ref.get("cameraX"), ref.get("cameraY")
    if (x, y) != ((left + right) / 2, (top + bottom) / 2):
        raise ValueError("输入法原始落点与真实范围中心不一致")
    ox, oy = frame["root_origin_display_px"]
    root_bounds = dict(left=left-ox, top=top-oy, right=right-ox, bottom=bottom-oy)
    if not (0 <= root_bounds["left"] < root_bounds["right"] <= frame["root_width_px"]
            and 0 <= root_bounds["top"] < root_bounds["bottom"] <= frame["root_height_px"]):
        raise ValueError("输入法目标范围超出本次校准根视图")
    return gp.display_to_calibration_px(x, y, frame), root_bounds
