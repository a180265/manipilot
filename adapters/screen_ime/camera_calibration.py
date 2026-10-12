"""固定机位摄像头定位：用摄像头完成屏幕标定与触点定位（无需手机回报触点）。

场景：摄像头与手机位置都固定。两段式标定：

1. 屏幕段（摄像头 → 屏幕）：手机打开校准页"摄像头标定"模式，四角显示 4 个不同颜色
   圆点，页面回报每个圆点的精确屏幕像素坐标；摄像头拍一帧，按 HSV 颜色检出 4 个圆心，
   拟合单应矩阵 H（摄像头像素 → 屏幕像素）。摄像头/手机任一移动后重拍一次即可。
2. 机械臂段（机械 → 屏幕）：机械臂探头贴醒目颜色标记（默认品红），移动到 ≥3 个位置，
   每个位置拍一帧检出探头尖，经 H 换算成屏幕像素，与机械坐标配对，拟合
   screen = A·robot + b —— 与手机触点回报法产出同一格式的映射，下游（打字/点击）零改动。

安全与边界：
- 摄像头只做"定位测量"，不构成动作授权；机械移动仍走平台安全门禁。
- 单应/映射按 deviceId 存档并带版本号；摄像头、手机、夹具任一改变必须重新标定。
- 本模块属于 screen_ime 适配器的测量仪器（与输入法任务服务同层），插件通过服务接口
  使用，不直接操作摄像头（遵守平台插件沙箱规则）。
- OpenCV 为可选依赖（pyproject `real` extra：opencv-python-headless）；未安装时服务
  返回 503 并提示安装命令。
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np


class CameraCalibrationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# 标定点颜色（HSV 上界/下界，OpenCV BGR 空间经 cvtColor 后使用）
MARKER_COLORS: dict[str, list[tuple[tuple[int, int, int], tuple[int, int, int]]]] = {
    # 红色色相跨 0，需要两段
    "red": [((0, 110, 110), (10, 255, 255)), ((170, 110, 110), (180, 255, 255))],
    "green": [((40, 90, 90), (85, 255, 255))],
    "blue": [((95, 90, 90), (130, 255, 255))],
    "yellow": [((18, 90, 90), (38, 255, 255))],
}
# 页面默认标定点（全屏归一化坐标，页面按此绘制并回报精确像素）
MARKER_POSITIONS: dict[str, tuple[float, float]] = {
    "red": (0.06, 0.08),
    "green": (0.94, 0.08),
    "blue": (0.94, 0.92),
    "yellow": (0.06, 0.92),
}
# 探头尖默认颜色：品红（胶带/贴纸）——可选；不贴标记时用"机械臂点击+页面回报触点"配对
TIP_HSV_RANGE = ((135, 90, 90), (165, 255, 255))
MIN_MARKER_AREA = 40.0

# ArUco 基准码（DICT_4X4_50）：带唯一 ID，杜绝"画面里其他颜色物体"造成的误检。
# id → 页面归一化位置（左上/右上/右下/左下）
ARUCO_DICTIONARY = "DICT_4X4_50"
ARUCO_IDS: dict[int, tuple[float, float]] = {
    1: (0.06, 0.08),
    2: (0.94, 0.08),
    3: (0.94, 0.92),
    4: (0.06, 0.92),
}


def generate_aruco_png(marker_id: int, side_px: int = 200) -> bytes | None:
    """生成 ArUco 码 PNG（自带白边留白，供页面 <img> 直接显示）。无 aruco 模块返回 None。"""
    try:
        import cv2

        dictionary = cv2.aruco.getPredefinedDictionary(
            getattr(cv2.aruco, ARUCO_DICTIONARY))
        marker = cv2.aruco.generateImageMarker(dictionary, int(marker_id), int(side_px))
        margin = max(4, side_px // 4)
        padded = np.full((side_px + 2 * margin, side_px + 2 * margin), 255, np.uint8)
        padded[margin:margin + side_px, margin:margin + side_px] = marker
        ok, png = cv2.imencode(".png", padded)
        return bytes(png.tobytes()) if ok else None
    except Exception:
        return None


def detect_aruco_markers(frame) -> dict[int, tuple[float, float]]:
    """按 ID 检测画面中的 ArUco 码，返回 {marker_id: (质心x, 质心y)}。无 aruco 模块返回空。"""
    try:
        import cv2

        dictionary = cv2.aruco.getPredefinedDictionary(
            getattr(cv2.aruco, ARUCO_DICTIONARY))
        corners, ids, _ = cv2.aruco.ArucoDetector(dictionary).detectMarkers(frame)
    except Exception:
        return {}
    result: dict[int, tuple[float, float]] = {}
    if ids is None:
        return result
    for marker_corners, marker_id in zip(corners, ids.reshape(-1)):
        points = np.array(marker_corners).reshape(-1, 2)
        result[int(marker_id)] = (float(points[:, 0].mean()), float(points[:, 1].mean()))
    return result


def detect_color_blobs(frame, hsv_ranges, *, min_area: float = MIN_MARKER_AREA,
                       max_area: float | None = None) -> dict:
    """在 BGR 帧里按 HSV 区间找色块，返回每个颜色最大色块的质心 {color: (cx, cy)}。

    找不到的颜色不出现在结果里。多区间（如红色）取掩码并集。
    """
    import cv2

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    height, width = hsv.shape[:2]
    total_area = height * width
    if max_area is None:
        max_area = total_area * 0.25  # 过大的色块视为背景/误检
    result: dict[str, tuple[float, float]] = {}
    for color, ranges in hsv_ranges.items():
        mask = None
        for lower, upper in ranges:
            layer = cv2.inRange(hsv, np.array(lower, np.uint8), np.array(upper, np.uint8))
            mask = layer if mask is None else (mask | layer)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best, best_area = None, 0.0
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < min_area or area > max_area:
                continue
            if area > best_area:
                best, best_area = contour, area
        if best is None:
            continue
        moments = cv2.moments(best)
        if moments["m00"] == 0:
            continue
        result[color] = (moments["m10"] / moments["m00"], moments["m01"] / moments["m00"])
    return result


def detect_markers(frame) -> dict:
    return detect_color_blobs(frame, MARKER_COLORS)


def detect_pointer_tip(frame) -> tuple[float, float] | None:
    return detect_color_blobs(frame, {"tip": [TIP_HSV_RANGE]}).get("tip")


def fit_camera_to_screen(points: list[dict]) -> dict:
    """≥4 组 (摄像头像素, 屏幕像素) 对应点 → 单应矩阵与重投影 RMS。

    points: [{"cameraX","cameraY","screenX","screenY"}, ...]
    RANSAC 自动剔除个别坏配对（如某次探头尖检测错位）：残差 RMS 只按内点
    计算；外点以 errors/inliers 返回，由调用方决定提示或要求重采。
    """
    import cv2

    if len(points) < 4:
        raise CameraCalibrationError("camera_markers_insufficient",
                                     "摄像头标定至少需要 4 个标定点")
    camera = np.array([[p["cameraX"], p["cameraY"]] for p in points], np.float64)
    screen = np.array([[p["screenX"], p["screenY"]] for p in points], np.float64)
    homography, mask = cv2.findHomography(camera.reshape(-1, 1, 2),
                                          screen.reshape(-1, 1, 2), cv2.RANSAC, 3.0)
    if homography is None:
        raise CameraCalibrationError("camera_homography_failed",
                                     "单应矩阵拟合失败：标定点可能共线或检测异常")
    projected = cv2.perspectiveTransform(camera.reshape(-1, 1, 2), homography).reshape(-1, 2)
    errors = np.linalg.norm(projected - screen, axis=1)
    inliers = (mask.reshape(-1).astype(bool) if mask is not None
               else np.ones(len(points), dtype=bool))

    # Five-point RANSAC can select an exact but geometrically bad four-point subset:
    # its inlier RMS becomes 0 while one valid point is projected hundreds of pixels
    # away. If all points agree within ordinary detection noise, prefer the all-point
    # least-squares result. A genuine gross outlier still uses the RANSAC result.
    if len(points) > 4 and not inliers.all():
        direct, _ = cv2.findHomography(camera.reshape(-1, 1, 2),
                                       screen.reshape(-1, 1, 2), 0)
        if direct is not None:
            direct_projected = cv2.perspectiveTransform(
                camera.reshape(-1, 1, 2), direct).reshape(-1, 2)
            direct_errors = np.linalg.norm(direct_projected - screen, axis=1)
            if float(direct_errors.max()) <= 10.0:
                homography = direct
                errors = direct_errors
                inliers = np.ones(len(points), dtype=bool)

    # Reject a projective pole/fold inside the calibrated camera region. This catches
    # the other false-success case where four selected points have zero residual but
    # interpolation between them diverges.
    hull = cv2.convexHull(camera.astype(np.float32)).reshape(-1, 2)
    checks = [camera.mean(axis=0)]
    checks.extend(0.5 * (a + b) for a, b in itertools.combinations(hull, 2))
    denominators = [homography[2, 0] * p[0] + homography[2, 1] * p[1] + homography[2, 2]
                    for p in checks]
    if (not all(np.isfinite(denominators))
            or min(abs(float(value)) for value in denominators) < 0.05
            or min(denominators) < 0 < max(denominators)):
        raise CameraCalibrationError(
            "camera_homography_unstable",
            "单应矩阵在标定区域内发生折叠/发散，请重新采集屏幕标靶")

    inlier_errors = errors[inliers]
    rms = float(np.sqrt((inlier_errors ** 2).mean())) if inlier_errors.size else float("inf")
    return {"homography": homography.tolist(), "reprojectionRms": rms,
            "pointCount": len(points), "inlierCount": int(inliers.sum()),
            "inliers": inliers.tolist(), "errors": [round(float(e), 2) for e in errors]}


def fit_camera_to_screen_strict(points: list[dict], *, rms_max_px: float,
                                max_error_px: float, grid_rows: int = 3,
                                grid_cols: int = 3, monotonic_tol_px: float = 1.0,
                                screen_width: float | None = None,
                                screen_height: float | None = None,
                                boundary_margin_px: float = 24.0) -> dict:
    """v1.1 自动校准 Phase 0 专用：全点严格质量门禁（规格书 §9.3）。

    与 fit_camera_to_screen 的区别（后者保留给旧手动 D 流程，行为不变）：
    - RANSAC 只用于诊断：规则网格中任一点被排除 → 拒绝（inlierCount 必须 == pointCount，
      4/5 只能是 INVALID，不得为降 RMS 静默剔除 bottom_left）；
    - RMS/最大误差按全部点计算并分别过阈值；
    - 单应性投影回已知屏幕坐标必须落在屏幕边界（含裕量）内；
    - 网格单调性：行内 screenX、列内 screenY 必须单调不减（透视映射在无折叠时
      保持直线上的次序；违反说明区域内折叠/发散）。

    points 必须按行主序（row-major，先 X 后 Y）提供规则网格上的
    (cameraX, cameraY, screenX, screenY)。返回 {"ok": bool, "reason": {...}|None,
    "homography", "rms", "maxError", "errors", "pointCount", "inlierCount",
    "inlierRatio", "monotonic", "boundary"}；ok=False 时 reason 给出 code/中文说明，
    homography 仍返回（供诊断留痕），调用方不得将其锁定。
    """
    def reject(code: str, message: str, fit: dict | None = None) -> dict:
        return {"ok": False, "reason": {"code": code, "message": message},
                "homography": (fit or {}).get("homography"),
                "rms": (fit or {}).get("reprojectionRms"),
                "maxError": (max((fit or {}).get("errors", [0.0])) if fit else None),
                "errors": (fit or {}).get("errors", []),
                "pointCount": len(points),
                "inlierCount": (fit or {}).get("inlierCount", 0),
                "inlierRatio": ((fit or {}).get("inlierCount", 0) / len(points)) if points else 0.0,
                "monotonic": None, "boundary": None}

    if len(points) != grid_rows * grid_cols:
        return reject("camera_plan_size_mismatch",
                      f"标定点数 {len(points)} 与固定计划 {grid_rows}x{grid_cols} 不符")
    try:
        fit = fit_camera_to_screen(points)
    except CameraCalibrationError as exc:
        return reject(exc.code, exc.message)

    if fit["inlierCount"] != len(points):
        bad = [f"点{i + 1} 偏 {e:.0f}px"
               for i, (keep, e) in enumerate(zip(fit["inliers"], fit["errors"])) if not keep]
        return reject("camera_outlier_present",
                      f"标定点存在外点，规则网格不允许剔除（需重新显示/检测替换）：{('；'.join(bad))}", fit)

    errors = [float(e) for e in fit["errors"]]
    rms = float(np.sqrt(np.mean(np.square(errors))))
    max_error = max(errors)
    if rms > rms_max_px or max_error > max_error_px:
        return reject("camera_quality_below_target",
                      f"全点重投影 RMS {rms:.2f}px（≤{rms_max_px}）/ 最大 {max_error:.2f}px"
                      f"（≤{max_error_px}）未达标", fit)

    # 边界：全部网格点投影回屏幕坐标必须落在屏幕内（含裕量）——x 与 y 都要满足
    boundary = True
    if screen_width and screen_height:
        for p in points:
            if not (-boundary_margin_px <= p["screenX"] <= screen_width + boundary_margin_px
                    and -boundary_margin_px <= p["screenY"] <= screen_height + boundary_margin_px):
                boundary = False
                break
    if not boundary:
        return reject("camera_boundary_violation",
                      "标定点屏幕坐标越出屏幕安全边界", fit)

    # 单调性：行内 screenX / 列内 screenY 单调不减（容差 monotonic_tol_px）
    monotonic = True
    for r in range(grid_rows):
        row = [points[r * grid_cols + c] for c in range(grid_cols)]
        for a, b in zip(row, row[1:]):
            if b["screenX"] < a["screenX"] - monotonic_tol_px:
                monotonic = False
    for c in range(grid_cols):
        col = [points[r * grid_cols + c] for r in range(grid_rows)]
        for a, b in zip(col, col[1:]):
            if b["screenY"] < a["screenY"] - monotonic_tol_px:
                monotonic = False
    if not monotonic:
        return reject("camera_monotonicity_violation",
                      "网格映射次序异常（行/列屏幕坐标出现倒退），区域内可能折叠", fit)

    return {"ok": True, "reason": None, "homography": fit["homography"],
            "rms": round(rms, 4), "maxError": round(max_error, 4), "errors": errors,
            "pointCount": len(points), "inlierCount": fit["inlierCount"],
            "inlierRatio": 1.0, "monotonic": True, "boundary": True}


class CameraScreenTransform:
    """摄像头像素 → 屏幕像素（3x3 单应）的封装与持久化视图。"""

    def __init__(self, homography, *, device_id: str = "", reprojection_rms: float = 0.0,
                 version: int = 1, created_at: str = ""):
        self.device_id = device_id
        self.reprojection_rms = reprojection_rms
        self.version = version
        self.created_at = created_at
        self.matrix = np.array(homography, np.float64)

    def screen_from_camera(self, camera_x: float, camera_y: float) -> tuple[float, float]:
        import cv2

        point = np.array([[[float(camera_x), float(camera_y)]]], np.float64)
        projected = cv2.perspectiveTransform(point, self.matrix)
        return float(projected[0, 0, 0]), float(projected[0, 0, 1])

    def to_dict(self) -> dict:
        return {"deviceId": self.device_id, "homography": self.matrix.tolist(),
                "reprojectionRms": self.reprojection_rms, "version": self.version,
                "createdAt": self.created_at,
                "note": "摄像头→屏幕单应；摄像头或手机移动后必须重新标定"}

    @classmethod
    def from_dict(cls, data: dict) -> "CameraScreenTransform":
        return cls(data["homography"], device_id=data.get("deviceId", ""),
                   reprojection_rms=float(data.get("reprojectionRms", 0.0)),
                   version=int(data.get("version", 1)), created_at=data.get("createdAt", ""))


class CameraCalibrationStore:
    """按 deviceId 持久化摄像头→屏幕单应（JSON，跨平台）。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def save(self, transform: CameraScreenTransform) -> dict:
        previous = self._data.get(transform.device_id)
        if previous is not None:
            transform.version = int(previous.get("version", 1)) + 1
        self._data[transform.device_id] = transform.to_dict()
        self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return transform.to_dict()

    def get(self, device_id: str) -> dict | None:
        return self._data.get(device_id)

    def all(self) -> list[dict]:
        return sorted(self._data.values(), key=lambda item: item.get("deviceId", ""))

    def delete(self, device_id: str) -> bool:
        if device_id in self._data:
            del self._data[device_id]
            self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            return True
        return False


def detect_probe_tip(frame) -> tuple[float, float] | None:
    """在固定机位画面里定位机械臂探头尖（无需贴标）。

    原理：屏幕是画面中最大的亮区（凸矩形，凸包填充可密封探头杆切出的缝隙）；
    探头是亮区内的暗色物体，螺纹细杆被腐蚀掉，圆形探头头存活，其质心即
    探头尖位置（探头垂直向下时头与触点同 X/Y）。
    """
    import cv2

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    _, bright = cv2.threshold(gray, 140, 255, cv2.THRESH_BINARY)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE,
                              np.ones((31, 31), np.uint8))
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    screen_contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(screen_contour) < 0.03 * gray.size:
        return None
    # 凸包填充：细杆切入形成的缺口会被外轮廓排除，凸包（屏幕为凸矩形）可覆盖
    hull = cv2.convexHull(screen_contour)
    screen_mask = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(screen_mask, [hull], -1, 255, -1)
    dark = cv2.inRange(gray, 0, 120)
    dark = cv2.bitwise_and(dark, screen_mask)
    # 螺纹杆约 25px 宽，15x15 两轮腐蚀（≈28px）只留下圆头
    eroded = cv2.erode(dark, np.ones((15, 15), np.uint8), iterations=2)
    contours, _ = cv2.findContours(eroded, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 200:
        return None
    moments = cv2.moments(contour)
    return (float(moments["m10"] / moments["m00"]),
            float(moments["m01"] / moments["m00"]))
