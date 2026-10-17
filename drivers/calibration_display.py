"""Pure conversion of same-session Android root metadata to a display reference."""
from copy import deepcopy
import math

import numpy as np

from drivers.screen_pose import validate_corners


def validate_metadata(metadata, *, ime_device_id, session_id, screen=None):
    if not isinstance(metadata, dict):
        raise ValueError("legacy metadata 缺失，不能重建坐标桥")
    page = metadata.get("page") or {}
    if not isinstance(page, dict):
        raise ValueError("legacy metadata 页面元数据无效")
    if (not ime_device_id or not session_id or metadata.get("deviceId") != ime_device_id
            or metadata.get("sessionId") != session_id
            or page.get("legacyCalibrationSessionId") != session_id
            or page.get("pageId") != "legacy-calibration"):
        raise ValueError("legacy metadata 设备、会话或原校准页面不匹配")
    if type(page.get("rotation")) is not int or page["rotation"] != 0 or page.get("coordinateSpaceVersion") != 2:
        raise ValueError("legacy metadata 坐标版本或屏幕方向不受支持")
    for field in ("rootWidthPx", "rootHeightPx", "displayWidthPx", "displayHeightPx"):
        if type(page.get(field)) is not int or page[field] <= 0:
            raise ValueError("legacy metadata 屏幕尺寸无效")
    origin = page.get("rootOriginDisplayPx")
    if (not isinstance(origin, list) or len(origin) != 2
            or any(type(value) is not int or value < 0 for value in origin)
            or origin[0] + page["rootWidthPx"] > page["displayWidthPx"]
            or origin[1] + page["rootHeightPx"] > page["displayHeightPx"]):
        raise ValueError("legacy metadata 校准根视图超出物理屏幕")
    density = page.get("density")
    if (type(density) not in (int, float) or not math.isfinite(density) or density <= 0
            or not isinstance(metadata.get("clientInstanceId"), str)
            or not metadata["clientInstanceId"].strip()):
        raise ValueError("legacy metadata 密度或客户端来源无效")
    if screen is not None and (page["rootWidthPx"], page["rootHeightPx"]) != (
            screen.get("screenWidth"), screen.get("screenHeight")):
        raise ValueError("legacy metadata root 尺寸与本轮校准不一致")
    return deepcopy(metadata)


def attach_display_reference(bundle, metadata, *, ime_device_id, session_id):
    """Keep detected root evidence; derive a display pose reference from measured H.

    This reference is not an observed display contour. Runtime must independently
    locate a current display and fit its pose before consuming an image or moving.
    """
    screen = bundle["screen"]
    frozen = validate_metadata(metadata, ime_device_id=ime_device_id,
                               session_id=session_id, screen=screen)
    page = frozen["page"]
    ox, oy = page["rootOriginDisplayPx"]
    w, h = page["displayWidthPx"], page["displayHeightPx"]
    points = np.array([[-ox,-oy,1],[w-1-ox,-oy,1],
                       [w-1-ox,h-1-oy,1],[-ox,h-1-oy,1]], dtype=float)
    matrix = np.asarray(bundle["camera"]["cameraToScreen"]["homography"],dtype=float).reshape(3,3)
    try:
        projected = points @ np.linalg.inv(matrix).T
    except np.linalg.LinAlgError as exc:
        raise ValueError("校准屏幕反投影无效") from exc
    if not np.isfinite(projected).all() or not (np.all(projected[:,2] > 1e-10)
                                               or np.all(projected[:,2] < -1e-10)):
        raise ValueError("校准屏幕反投影无效")
    corners = projected[:,:2] / projected[:,2:]
    centered = corners - corners.mean(axis=0)
    corners = corners[np.argsort(np.arctan2(centered[:,1], centered[:,0]))]
    corners = np.roll(corners, -int(np.argmin(corners.sum(axis=1))), axis=0)
    corners = validate_corners(corners)
    size = [bundle["frame"]["width"]-1, bundle["frame"]["height"]-1]
    if np.any(corners < 0) or np.any(corners > size):
        raise ValueError("完整显示屏超出校准相机原图")
    observed = deepcopy(screen["edgeModel"])
    screen["displayGeometryVersion"] = 1
    screen["calibrationRootEdgeModel"] = observed
    screen["displayCoordinateMetadata"] = frozen
    screen["edgeModel"] = {**observed, "corners":corners.tolist(),
                           "coordinateDomain":"android_display", "referenceVersion":1,
                           "source":"root_homography_display_metadata"}


def validate_display_reference(bundle):
    """New display-domain geometry requires versioned, matching root metadata."""
    screen = bundle.get("screen") or {}
    edge = screen.get("edgeModel") or {}
    version = screen.get("displayGeometryVersion")
    if version is None and edge.get("coordinateDomain") is None:
        return
    if version != 1 or edge.get("coordinateDomain") != "android_display":
        raise ValueError("本次校准缺少已验证的完整显示屏坐标桥")
    if edge.get("referenceVersion") != 1:
        raise ValueError("完整显示屏参考版本不受支持")
    metadata = screen.get("displayCoordinateMetadata")
    if not isinstance(metadata, dict):
        raise ValueError("完整显示屏缺少本次校准的坐标桥元数据")
    expected = deepcopy(bundle)
    if not isinstance(screen.get("calibrationRootEdgeModel"),dict):
        raise ValueError("完整显示屏缺少独立根视图边缘证据")
    expected["screen"]["edgeModel"] = deepcopy(screen["calibrationRootEdgeModel"])
    attach_display_reference(
        expected, metadata,
        ime_device_id=bundle.get("imeDeviceId") or metadata.get("deviceId"),
        session_id=(bundle.get("autoCalibration") or {}).get("phoneSessionId")
        or metadata.get("sessionId"))
    if not np.allclose(validate_corners(edge.get("corners")),
                       expected["screen"]["edgeModel"]["corners"], atol=1e-7,rtol=0):
        raise ValueError("完整显示屏参考与本次 root 映射不一致")
