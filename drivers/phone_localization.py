"""F3 公共手机定位模块（规范 §7，验收规范 2026-09-29）。

统一输出 §7.1 的定位结果结构，调用方不得对同一份证据自行重新解释：
- ``locate_phone_quality``：§7.3 质量规则（先解码→再定位手机区域→再统计；
  统计只取手机内部区域；暗屏=灰度均值≤18 且纹理 std<8 → waiting；
  全帧新鲜纯黑 → waiting + pose_status=unavailable，绝不报 offline）。
- ``fit_landmark_pose``：§7.2 手机固连锚点拟合（≥4 有效匹配、覆盖 ≥3 个
  边/角区域、凸包 ≥校准手机面积 20%、前后向误差 ≤1.5px、保留率 ≥70%、
  拟合沿用 screen_pose 的全部 MAX_* 限制）。
- ``resolve_conflict``：同帧边缘与锚点证据矛盾（对应角差 >2px）→ 拒帧，
  不取平均、不择优。

本模块是纯函数：不读业务数据、不获取租约、不下发动作、不自动保存校准。
帧获取、校准资料读写和任务状态变更仍由调用层负责。
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from drivers import calibration_bundle as cb
from drivers.screen_pose import (
    MAX_COMPENSATION_PX,
    MAX_FIT_ERROR_PX,
    MAX_SCALE_CHANGE,
    validate_corners,
)

# §7.1 枚举
QUALITY_VALUES = ("clear", "waiting", "overexposed", "abnormal", "frozen")
POSE_STATUS_VALUES = ("verified", "unavailable")
GEOMETRY_SOURCE_VALUES = (
    "observed_edges",
    "calibration_targets",
    "phone_landmarks",
    "calibrated_roi_only",
    "none",
)
REFERENCE_KIND_VALUES = ("published", "calibration_run", "none")

# §7.3 质量规则（与 RealCameraDriver 阈值同源，值由规范固定）
DARK_BRIGHTNESS_MAX = 18.0     # 暗屏：灰度均值 ≤18
DARK_TEXTURE_MAX = 8.0         # 且纹理标准差 <8
OVEREXPOSED_MIN = 248.0        # 过曝：均值 ≥248
BLUR_VARIANCE_MIN = 15.0       # 清晰度：Laplacian 方差 <15 为异常

# §7.2 锚点路径验收参数
LANDMARK_MIN_MATCHES = 4       # 至少 4 个有效匹配
LANDMARK_MIN_REGIONS = 3       # 覆盖至少 3 个不同边/角区域
LANDMARK_MIN_HULL_RATIO = 0.2  # 匹配点凸包 ≥ 校准手机面积的 20%
LANDMARK_FB_MAX_PX = 1.5       # 前后向匹配误差 ≤1.5px
LANDMARK_MIN_RETENTION = 0.7   # 保留比例 ≥70%

# §7.2 同帧矛盾拒帧
EDGE_LANDMARK_CONFLICT_PX = 2.0


def blank_result() -> dict:
    """§7.1 全字段的空结果（缺省值：未定位、无姿态、无基准、无相对变换）。

    movementPx/matrix 等"无相对基准时为空、不填零或一表示已通过"。
    """
    return {
        "quality": None,
        "pose_status": "unavailable",
        "geometry_source": "none",
        "corners": None,
        "camera_to_screen": None,
        "reference_kind": "none",
        "reference_id": None,
        "matrix": None,
        "movementPx": None,
        "fitErrorPx": None,
        "scaleChange": None,
        "source_frame_seq": None,
        "source_captured_at": None,
        "source_received_at": None,
        "binding": {
            "deviceId": None,
            "cameraDeviceUniqueId": None,   # 物理相机
            "cameraId": None,               # 逻辑相机
            "bindingRevision": None,
            "frameWidth": None,
            "frameHeight": None,
            "publishedCalibrationFingerprint": None,  # 首次候选为空
            "candidateId": None,                      # 本轮候选标识
        },
        "reason_code": None,
    }


def _classify(metrics: dict) -> str:
    """§7.3 质量分类（在 CLAHE/亮度增强之前的原始灰度上统计）。"""
    if metrics["brightness"] <= DARK_BRIGHTNESS_MAX and metrics["texture"] < DARK_TEXTURE_MAX:
        return "waiting"
    if metrics["brightness"] >= OVEREXPOSED_MIN:
        return "overexposed"
    if metrics["blur_variance"] < BLUR_VARIANCE_MIN:
        return "abnormal"
    return "clear"


def _decode_gray(frame_image) -> np.ndarray:
    """先验证解码（§7.3.1）；坏图抛 ValueError，不能借黑屏例外放行。"""
    if isinstance(frame_image, np.ndarray):
        frame = frame_image if frame_image.ndim == 2 else cv2.cvtColor(
            frame_image, cv2.COLOR_BGR2GRAY)
        if frame is None or frame.size == 0:
            raise ValueError("画面为空")
        return frame
    buffer = np.frombuffer(frame_image, dtype=np.uint8)
    frame = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
    if frame is None or frame.size == 0:
        raise ValueError("画面解码失败")
    return frame


def _phone_interior_pixels(gray: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """统计只取手机内部区域（§7.3.1）：按四角掩码并内缩 4% 排除边缘环境。"""
    height, width = gray.shape[:2]
    mask = np.zeros((height, width), np.uint8)
    cv2.fillConvexPoly(mask, np.rint(quad).astype(np.int32), 255)
    x, y, w, h = cv2.boundingRect(np.rint(quad).astype(np.int32))
    inset = max(1, int(min(w, h) * 0.04))
    mask = cv2.erode(mask, np.ones((2 * inset + 1, 2 * inset + 1), np.uint8))
    pixels = gray[mask != 0]
    if pixels.size < width * height * 0.02:
        raise ValueError("手机屏幕区域过小")
    return pixels


def locate_phone_quality(frame_image, roi_corners=None) -> dict:
    """§7.3：解码→定位手机区域→只统计手机内部质量。

    - ``roi_corners`` 为调用方给的校准 ROI（四角）。只允许据此判定"等待"
      （``geometry_source=calibrated_roi_only``），不证明当前位置、不输出
      可执行姿态（§7.1/§7.3.3）。
    - 不给 ROI 时尝试边缘轮廓定位（亮/暗两种极性）；成功则
      ``geometry_source=observed_edges``、``pose_status=verified``。
    - 全帧新鲜纯黑 → ``waiting`` + ``pose_status=unavailable``（reason_code=
      dark_screen），不得输出 blocked/offline/clear（V02）。
    - 返回 §7.1 全字段结构；来源序号/时间/绑定由调用层填入。
    """
    result = blank_result()
    result["binding"]["frameWidth"], result["binding"]["frameHeight"] = None, None
    try:
        gray = _decode_gray(frame_image)
    except ValueError as exc:
        result["quality"] = "abnormal"
        result["reason_code"] = "decode_failed"
        result["detail"] = str(exc)
        return result
    height, width = gray.shape[:2]
    result["binding"]["frameWidth"], result["binding"]["frameHeight"] = int(width), int(height)
    frame_dark = (float(gray.mean()) <= DARK_BRIGHTNESS_MAX
                  and float(gray.std()) < DARK_TEXTURE_MAX)

    quad = None
    if roi_corners is not None:
        try:
            corners = validate_corners(roi_corners)
            if (np.any(corners < 0) or np.any(corners[:, 0] >= width)
                    or np.any(corners[:, 1] >= height)):
                raise ValueError("校准 ROI 超出当前帧")
        except (TypeError, ValueError) as exc:
            result["quality"] = "abnormal"
            result["reason_code"] = "invalid_roi"
            result["detail"] = str(exc)
            return result
        quad = corners
        # 校准 ROI 只是绑定区域：只允许判定等待，不能输出可执行姿态
        result["geometry_source"] = "calibrated_roi_only"
    else:
        from drivers.screen_edge import ScreenEdgeError, detect_screen_quad

        bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        for polarity in (bgr, cv2.cvtColor(cv2.bitwise_not(gray), cv2.COLOR_GRAY2BGR)):
            try:
                found = detect_screen_quad(polarity)
            except ScreenEdgeError:
                continue
            area = float(found["area"])
            if not width * height * 0.05 <= area <= width * height * 0.90:
                continue
            quad = np.asarray(found["corners"], np.float64)
            break
        if quad is not None:
            result["geometry_source"] = "observed_edges"
            result["pose_status"] = "verified"
            result["corners"] = [[round(float(x), 2), round(float(y), 2)]
                                 for x, y in quad]
    if quad is None:
        # 定位不到手机区域：全帧新鲜纯黑只能等待，不报 offline（§7.3.4）
        if frame_dark:
            result["quality"] = "waiting"
            result["reason_code"] = "dark_screen"
            return result
        result["quality"] = "abnormal"
        result["reason_code"] = "phone_not_located"
        return result

    try:
        pixels = _phone_interior_pixels(gray, quad)
    except ValueError as exc:
        if frame_dark:
            result["quality"] = "waiting"
            result["reason_code"] = "dark_screen"
            return result
        result["quality"] = "abnormal"
        result["reason_code"] = "phone_region_too_small"
        result["detail"] = str(exc)
        return result
    x, y, w, h = cv2.boundingRect(np.rint(quad).astype(np.int32))
    cropped = gray[y:y + h, x:x + w]
    small = cv2.resize(cropped, (320, max(1, int(320 * cropped.shape[0] / max(1, cropped.shape[1])))))
    metrics = {
        "blur_variance": float(cv2.Laplacian(small, cv2.CV_64F).var()),
        "brightness": float(pixels.mean()),
        "texture": float(pixels.std()),
    }
    result["quality"] = _classify(metrics)
    if result["quality"] == "waiting":
        result["reason_code"] = "dark_screen"
    result["metrics"] = {key: round(value, 3) for key, value in metrics.items()}
    # 全帧纯黑时即使 ROI 命中也无法证明手机当前位置（§7.3.4）
    if frame_dark and result["geometry_source"] == "calibrated_roi_only":
        result["pose_status"] = "unavailable"
        result["reason_code"] = "dark_screen"
    return result


def _region_labels(quad: np.ndarray, points: np.ndarray) -> set[str]:
    """把匹配点归入边/角区域（§7.2）：四角 + 四边共 8 个区域。"""
    labels: set[str] = set()
    tl, tr, br, bl = quad
    names = ("top", "right", "bottom", "left")
    corners = ("top_left", "top_right", "bottom_right", "bottom_left")
    center = quad.mean(axis=0)
    diagonal = max(float(np.linalg.norm(tr - bl)), float(np.linalg.norm(tl - br)), 1e-6)
    for point in points:
        distances = [float(np.linalg.norm(point - c)) for c in (tl, tr, br, bl)]
        if min(distances) <= 0.25 * diagonal:
            labels.add(corners[int(np.argmin(distances))])
            continue
        # 最近的两条边（点到线段距离），角区之外的点归属其所在边
        edge_distances = []
        for index in range(4):
            a, b = quad[index], quad[(index + 1) % 4]
            vec = b - a
            t = float(np.clip(np.dot(point - a, vec) / max(float(vec @ vec), 1e-6), 0.0, 1.0))
            edge_distances.append(float(np.linalg.norm(point - (a + t * vec))))
        order = np.argsort(edge_distances)
        labels.add(names[int(order[0])])
    return labels


def _hull_area_ratio(quad: np.ndarray, points: np.ndarray) -> float:
    quad_area = float(cv2.contourArea(quad.astype(np.float32)))
    if len(points) < 3 or quad_area <= 0:
        return 0.0
    hull = cv2.convexHull(points.astype(np.float32))
    return float(cv2.contourArea(hull)) / quad_area


def fit_landmark_pose(saved_corners, reference_points, live_points,
                      *, fb_errors=None) -> dict:
    """§7.2 手机固连锚点拟合（纯函数；不读业务、不下发动作）。

    - ``saved_corners``：校准时登记的手机四角（校准相机平面）。
    - ``reference_points`` / ``live_points``：锚点参考位置与当前帧匹配位置
      （一一配对）。``fb_errors`` 可选提供每对前后向匹配误差（相机 px）。
    - 有效匹配：前后向误差 ≤1.5px；保留率（有效/总）≥70%；有效匹配 ≥4；
      覆盖 ≥3 个不同边/角区域；匹配点凸包 ≥校准手机面积 20%。
    - 相似变换拟合沿用 screen_pose 全部 MAX_* 限制（位移 16px、拟合 2px、
      尺度 1%）。成功输出 pose_status=verified、geometry_source=phone_landmarks。
    """
    from drivers.screen_edge import _fit_similarity

    result = blank_result()
    result["geometry_source"] = "phone_landmarks"
    if not isinstance(reference_points, (list, tuple)) \
            or not isinstance(live_points, (list, tuple)) \
            or len(reference_points) != len(live_points):
        result["reason_code"] = "landmark_input_mismatch"
        return result
    try:
        quad = validate_corners(saved_corners)
    except (TypeError, ValueError) as exc:
        result["reason_code"] = "landmark_corners_invalid"
        result["detail"] = str(exc)
        return result
    total = len(reference_points)
    if total == 0:
        result["reason_code"] = "landmark_no_matches"
        return result
    try:
        reference = np.asarray(reference_points, np.float64).reshape(-1, 2)
        live = np.asarray(live_points, np.float64).reshape(-1, 2)
        if not (np.isfinite(reference).all() and np.isfinite(live).all()):
            raise ValueError("锚点坐标含非有限值")
    except (TypeError, ValueError) as exc:
        result["reason_code"] = "landmark_points_invalid"
        result["detail"] = str(exc)
        return result

    scale, rotation, translation = _fit_similarity(reference, live)
    fitted = scale * (reference @ rotation.T) + translation
    residuals = np.linalg.norm(fitted - live, axis=1)
    if fb_errors is not None:
        if not isinstance(fb_errors, (list, tuple)) or len(fb_errors) != total:
            result["reason_code"] = "landmark_input_mismatch"
            return result
        fb = np.asarray(fb_errors, np.float64)
        if not np.isfinite(fb).all():
            result["reason_code"] = "landmark_points_invalid"
            return result
    else:
        fb = residuals  # 未提供前后向误差时以拟合残差作为每对匹配误差
    valid = fb <= LANDMARK_FB_MAX_PX
    retention = float(valid.sum()) / float(total)
    checks = {
        "matches": int(valid.sum()),
        "retention": round(retention, 4),
        "regions": sorted(_region_labels(quad, reference[valid])) if valid.any() else [],
        "hullRatio": round(_hull_area_ratio(quad, reference[valid]), 4) if valid.any() else 0.0,
    }
    result["landmark"] = checks
    if int(valid.sum()) < LANDMARK_MIN_MATCHES:
        result["reason_code"] = "landmark_matches_insufficient"
        return result
    if retention < LANDMARK_MIN_RETENTION:
        result["reason_code"] = "landmark_retention_below_min"
        return result
    if len(checks["regions"]) < LANDMARK_MIN_REGIONS:
        result["reason_code"] = "landmark_coverage_insufficient"
        return result
    if checks["hullRatio"] < LANDMARK_MIN_HULL_RATIO:
        result["reason_code"] = "landmark_hull_too_small"
        return result

    fitted_corners = scale * (quad @ rotation.T) + translation
    fitted_corners = np.asarray(fitted_corners, np.float64)
    try:
        # 复用 screen_pose 全部 MAX_* 限制（位移/拟合误差/尺度变化）
        pose = _fit_pose_checked(quad, fitted_corners)
    except ValueError as exc:
        result["reason_code"] = "landmark_pose_limit_exceeded"
        result["detail"] = str(exc)
        return result
    result.update(pose)
    result["pose_status"] = "verified"
    result["landmark"]["fitErrorMaxPx"] = round(float(residuals[valid].max()), 3)
    return result


def _fit_pose_checked(saved: np.ndarray, live: np.ndarray) -> dict:
    from drivers.screen_pose import fit_pose

    pose = fit_pose(saved.tolist(), live.tolist())
    return {
        "corners": pose["corners"],
        "matrix": pose["matrix"],
        "movementPx": pose["movementPx"],
        "fitErrorPx": pose["fitErrorPx"],
        "scaleChange": _matrix_scale(pose["matrix"]),
    }


def _matrix_scale(matrix: list) -> float:
    m = np.asarray(matrix, np.float64)
    return round(float(math.hypot(m[0, 0], m[1, 0]) - 1.0), 4)


def resolve_conflict(edges_pose: dict, landmarks_pose: dict) -> dict:
    """§7.2：同帧边缘与锚点证据矛盾（对应角差 >2px）→ 拒帧。

    不取平均、不选"看起来更好"的一个（V06）。一致时返回边缘实测姿态
    （§6.1.2：四边清晰优先实测边线），并记录交叉核对结果。
    """
    result = blank_result()
    edge_corners = (edges_pose or {}).get("corners")
    landmark_corners = (landmarks_pose or {}).get("corners")
    if edge_corners is None or landmark_corners is None:
        result["reason_code"] = "conflict_input_missing_pose"
        return result
    try:
        edge_quad = validate_corners(edge_corners)
        landmark_quad = validate_corners(landmark_corners)
    except (TypeError, ValueError) as exc:
        result["reason_code"] = "conflict_input_invalid_pose"
        result["detail"] = str(exc)
        return result
    deviation = float(np.max(np.linalg.norm(edge_quad - landmark_quad, axis=1)))
    result["conflict"] = {"cornerDeviationPx": round(deviation, 3),
                          "maxPx": EDGE_LANDMARK_CONFLICT_PX}
    if deviation > EDGE_LANDMARK_CONFLICT_PX:
        # 拒帧：几何证据矛盾，不能执行接触
        result["reason_code"] = "geometry_conflict"
        result["pose_status"] = "unavailable"
        result["geometry_source"] = "none"
        return result
    outcome = dict(edges_pose)
    outcome["pose_status"] = "verified"
    outcome.setdefault("geometry_source", "observed_edges")
    outcome["conflict"] = result["conflict"]
    return outcome


__all__ = [
    "blank_result",
    "locate_phone_quality",
    "fit_landmark_pose",
    "resolve_conflict",
    "QUALITY_VALUES",
    "POSE_STATUS_VALUES",
    "GEOMETRY_SOURCE_VALUES",
    "REFERENCE_KIND_VALUES",
    "DARK_BRIGHTNESS_MAX",
    "DARK_TEXTURE_MAX",
    "EDGE_LANDMARK_CONFLICT_PX",
    "LANDMARK_FB_MAX_PX",
    "LANDMARK_MIN_MATCHES",
    "LANDMARK_MIN_REGIONS",
    "LANDMARK_MIN_HULL_RATIO",
    "LANDMARK_MIN_RETENTION",
    # 透传 F4 限制常量，供调用方/测试引用同一来源
    "MAX_COMPENSATION_PX",
    "MAX_FIT_ERROR_PX",
    "MAX_SCALE_CHANGE",
]
