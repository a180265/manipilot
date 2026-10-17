"""屏幕边缘检测：以摄像头画面中的手机屏幕四边形作为几何稳定性基准。

对应《统一校准与输入法点击验收实施方案》§8.1：
- 摄像头绝对固定，运行时不检查摄像头是否移动；检查的是画面中的屏幕边缘
  是否仍与校准时一致（偏差 ≤ tolerancePx）。
- 检测思路：校准页亮区轮廓只作粗定位，采集与运行均拟合实际可观察的
  四条直边并取交点；圆角/探头窄缺口不改变角点定义，缺边时拒绝。
- 角点统一按 顺时针 TL→TR→BR→BL 排序后比较，偏差 = 对应角点最大欧氏距离。

纯 OpenCV/numpy 实现，可离线单测（合成图）。
"""
from __future__ import annotations

import math

import cv2
import numpy as np

MIN_AREA_RATIO = 0.05  # 屏幕四边形至少占画面 5%


class ScreenEdgeError(RuntimeError):
    """屏幕边缘检测失败（画面无有效屏幕四边形）。"""


def _order_corners(points: np.ndarray) -> list[list[float]]:
    """四点按 TL→TR→BR→BL 顺时针排序（sum/diff 法）。"""
    pts = [(float(p[0]), float(p[1])) for p in points]
    center_x = sum(x for x, _ in pts) / 4.0
    center_y = sum(y for _, y in pts) / 4.0

    def score(p: tuple[float, float]) -> tuple[float, float]:
        return (math.atan2(p[1] - center_y, p[0] - center_x), 0.0)

    ordered = sorted(pts, key=lambda p: score(p)[0])  # 逆时针（图像 y 向下）
    # 图像坐标系 y 向下，atan2 排序得到的是视觉上的顺时针；起点不定，旋转到 TL
    start = min(range(4), key=lambda i: ordered[i][0] + ordered[i][1])
    ordered = ordered[start:] + ordered[:start]
    return [[round(x, 1), round(y, 1)] for x, y in ordered]


def detect_screen_quad(frame) -> dict:
    """检测画面中的屏幕四边形。返回 {"corners": [[x,y]*4], "frameW", "frameH", "area"}。

    检测失败抛 ScreenEdgeError（调用方据此返回 screen_edge_not_detected）。
    """
    if frame is None:
        raise ScreenEdgeError("摄像头帧为空")
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    min_area = w * h * MIN_AREA_RATIO
    # Prefer the raw measured boundary: even blur alone can bridge a one-pixel
    # gap to a stand and produce a larger but false screen rectangle.
    tiers = []
    for image, close in ((gray, False), (blur, False),
                         (gray, True), (blur, True)):
        threshold = cv2.threshold(
            image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        binary = (cv2.morphologyEx(
            threshold, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            if close else threshold)
        candidates = []
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            area = cv2.contourArea(contour)
            if min_area <= area <= w * h * .90:
                corners = _quad_from_observed_edges(contour)
                if corners is not None:
                    candidates.append((area, corners))
        tiers.append(candidates)
    # Free detection needs the same photometric reflection separation as
    # calibrated localization. Every candidate still fits four observed
    # sides and passes the unchanged ambiguity, occlusion and border gates.
    otsu_level, _ = cv2.threshold(
        blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    foreground = gray[gray > otsu_level]
    reflection_candidates = []
    if foreground.size:
        upper_level, _ = cv2.threshold(
            foreground, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if upper_level > otsu_level:
            binary = cv2.threshold(
                gray, (otsu_level + upper_level) / 2,
                255, cv2.THRESH_BINARY)[1]
            contours, _ = cv2.findContours(
                binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            candidates = []
            for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
                area = cv2.contourArea(contour)
                if min_area <= area <= w * h * .90:
                    corners = _quad_from_observed_edges(contour)
                    if corners is not None:
                        candidates.append((area, corners))
            reflection_candidates = candidates
            tiers.append(candidates)
    if not any(tiers):
        raise ScreenEdgeError("画面中未检出屏幕四边形（确认屏幕点亮且在画面内）")
    candidates = sorted(next(items for items in tiers if items),
                        key=lambda item: item[0], reverse=True)

    def substantial_quad(item):
        edges = np.linalg.norm(
            np.roll(item[1], -1, axis=0) - np.asarray(item[1]), axis=1)
        return float(edges.min() / edges.max()) >= .25

    best_area, best = candidates[0]
    # Each threshold must agree on the same physical screen. A valid first
    # tier must not hide a second screen revealed after reflection separation.
    distinct = [candidates[0]]
    # Raw/blur/closing are ordered noise treatments of the ordinary threshold;
    # preserve raw boundary preference so closing cannot merge a nearby stand.
    for tier in (candidates, reflection_candidates):
        for item in tier:
            if not any(quad_deviation_px(item[1], seen[1]) <= 3
                       for seen in distinct):
                distinct.append(item)
    if any(substantial_quad(item) for item in distinct[1:]):
        raise ScreenEdgeError("画面中存在多个可能的屏幕四边形，无法确认手机")
    # JPEG ringing can connect a one-pixel gap to a wide support. A mild
    # opening exposes that disagreement; it must not silently become a baseline.
    raw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    opened = cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    opened_contours, _ = cv2.findContours(
        opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    opened_quads = [
        (cv2.contourArea(contour), _quad_from_observed_edges(contour))
        for contour in sorted(opened_contours, key=cv2.contourArea, reverse=True)[:5]
        if min_area <= cv2.contourArea(contour) <= w * h * .90
    ]
    opened_quads = [
        (area, quad) for area, quad in opened_quads if quad is not None]
    if opened_quads:
        _, opened_best = max(opened_quads, key=lambda item: item[0])
        if quad_deviation_px(best, opened_best) > 3:
            raise ScreenEdgeError("屏幕边缘在去噪前后不一致，无法确认手机屏幕")
    # 有效性守卫（2026-09-04 真机事故）：检测区域触碰画面边界=锁到了背景
    # 亮区/环境干扰，不是手机屏幕（输入法键盘/夜间灯光等场景下高发）。
    # 宁可报"检测无效"也不能拿背景矩形当屏幕去比对边缘基准。
    margin = 8
    if any(x < margin or y < margin or x > w - margin or y > h - margin
           for x, y in best):
        raise ScreenEdgeError(
            "屏幕边缘检测无效：检测区域触碰画面边界（环境亮区干扰，"
            "常见于键盘打开/光线变化场景）——请在手机校准页/可控画面下复验")
    return {"corners": best, "frameW": w, "frameH": h, "area": round(best_area, 1)}


def locate_screen_quad(frame, expected_corners: list | None = None,
                       *, tolerance_px: float = 32.0,
                       require_fittable: bool = False,
                       reject_clipped_candidate: bool = False) -> dict:
    """Locate either display polarity near the calibrated phone, when available."""
    if frame is None:
        raise ScreenEdgeError("摄像头帧为空")
    height, width = frame.shape[:2]
    small_phone = False
    if expected_corners is None:
        crop, left, top = frame, 0, 0
    else:
        corners = np.asarray(expected_corners, dtype=float)
        if (corners.shape != (4, 2) or not np.isfinite(corners).all()
                or not np.isfinite(tolerance_px) or tolerance_px <= 0):
            raise ScreenEdgeError("校准屏幕区域无效")
        small_phone = bool((np.linalg.norm(
            np.roll(corners, -1, axis=0) - corners, axis=1) < 24).any())
        margin = max(24, int(math.ceil(tolerance_px + 12)))
        left = max(0, int(math.floor(corners[:, 0].min())) - margin)
        top = max(0, int(math.floor(corners[:, 1].min())) - margin)
        right = min(width, int(math.ceil(corners[:, 0].max())) + margin + 1)
        bottom = min(height, int(math.ceil(corners[:, 1].max())) + margin + 1)
        crop = frame[top:bottom, left:right]
        if crop.size == 0:
            raise ScreenEdgeError("校准屏幕区域超出当前画面")
    candidates = []
    clipped_candidates = []
    for image in (crop, cv2.bitwise_not(crop)):
        if expected_corners is None and min(crop.shape[:2]) >= 100:
            try:
                found = detect_screen_quad(image)
            except ScreenEdgeError:
                continue
            found_quads = [(found["area"], found["corners"])]
        else:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            # At small phone sizes, smoothing can invent a matching inner edge;
            # closing can also merge the phone through a narrow surrounding gap.
            blurred = gray if small_phone else cv2.GaussianBlur(gray, (5, 5), 0)
            otsu_level, threshold = cv2.threshold(
                blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            thresholds = [threshold]
            if not small_phone:
                thresholds.append(cv2.threshold(
                    gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
            if expected_corners is not None:
                if not small_phone:
                    # A dim reflection touching a bright display can join its
                    # Otsu contour. Split the foreground photometrically before
                    # fitting, rather than deleting exterior contour points.
                    # The intermediate level retains dimmer true display edges
                    # that a foreground-only Otsu split would cut into.
                    foreground = gray[gray > otsu_level]
                    if foreground.size:
                        upper_level, _ = cv2.threshold(
                            foreground, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                        if upper_level > otsu_level:
                            thresholds.append(cv2.threshold(
                                gray, (otsu_level + upper_level) / 2,
                                255, cv2.THRESH_BINARY)[1])
                # A dim display may contain brighter content than its own edge.
                # Otsu alone then extracts rows/text, not the phone silhouette.
                low = float(np.percentile(blurred, 20))
                if low < otsu_level:
                    _, silhouette = cv2.threshold(
                        blurred, low, 255, cv2.THRESH_BINARY)
                    thresholds.append(silhouette)
                    # A nearby bright object must not force the dim edge into a blur halo.
                    dim_level, _ = cv2.threshold(
                        blurred[blurred <= otsu_level], 0, 255,
                        cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                    if low < dim_level < otsu_level:
                        thresholds.append(cv2.threshold(
                            blurred, dim_level, 255, cv2.THRESH_BINARY)[1])
            found_quads = []
            for binary in thresholds:
                binaries = (binary,) if small_phone else (
                    binary, cv2.morphologyEx(
                        binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)))
                for source in binaries:
                    contours, _ = cv2.findContours(
                        source,
                        cv2.RETR_LIST if expected_corners is not None else cv2.RETR_EXTERNAL,
                        cv2.CHAIN_APPROX_NONE)
                    for contour in contours:
                        area = cv2.contourArea(contour)
                        if expected_corners is None:
                            found_quads.append((area, _quad_from_observed_edges(contour)))
                        elif width * height * MIN_AREA_RATIO <= area <= width * height * .90:
                            fitted = _quad_from_screen_edges(
                                contour, corners - [left, top], tolerance_px)
                            if fitted is not None:
                                found_quads.append((area, fitted))
        for area, local in found_quads:
            if local is None:
                continue
            quad = [[x + left, y + top] for x, y in local]
            if (area < width * height * MIN_AREA_RATIO
                    or area > width * height * .90):
                continue
            if expected_corners is not None and max(
                    math.hypot(x - q[0], y - q[1])
                    for (x, y), q in zip(quad, expected_corners)) > tolerance_px:
                continue
            if any(x < 2 or x > width - 3 or y < 2 or y > height - 3
                   for x, y in quad):
                if reject_clipped_candidate and expected_corners is not None:
                    clipped_candidates.append((area, quad))
                continue
            candidates.append((area, quad))
    if not candidates and not clipped_candidates:
        raise ScreenEdgeError("画面中无法定位校准手机屏幕")
    if expected_corners is None:
        area, corners = max(candidates, key=lambda item: item[0])
    else:
        saved = np.asarray(expected_corners, dtype=float)
        if require_fittable:
            from drivers.screen_pose import fit_pose

            def fittable(items):
                result = []
                for item in items:
                    try:
                        fit_pose(saved, item[1])
                    except ValueError:
                        continue
                    result.append(item)
                return result

            candidates = fittable(candidates)
            clipped_candidates = fittable(clipped_candidates)
            if not candidates and not clipped_candidates:
                raise ScreenEdgeError("手机屏幕轮廓存在歧义，无法可靠补偿位置")

        def geometry_rank(item):
            _, quad = item
            live = np.asarray(quad, dtype=float)
            scale, rotation, translation = _fit_similarity(saved, live)
            fitted = scale * (saved @ rotation.T) + translation
            fit_error = float(np.max(np.linalg.norm(fitted - live, axis=1)))
            scale_error = abs(scale - 1.0)
            return (fit_error + 50.0 * scale_error,
                    scale_error,
                    float(np.max(np.linalg.norm(live - saved, axis=1))))

        # A tight ROI must not turn rejection of the best physical boundary
        # into acceptance of a worse inner edge and fictitious phone movement.
        if clipped_candidates and (not candidates or
                min(map(geometry_rank, clipped_candidates)) <=
                min(map(geometry_rank, candidates))):
            raise ScreenEdgeError("粗框过紧，无法确认完整屏幕边缘；请重新框选并留出余量")
        area, corners = min(candidates, key=geometry_rank)
    return {"corners": corners, "frameW": width, "frameH": height, "area": area}


def _quad_from_contour(contour, *, allow_box: bool = True) -> list[list[float]] | None:
    peri = cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
    if len(approx) == 4 and cv2.isContourConvex(approx):
        return _order_corners(approx.reshape(-1, 2))
    if not allow_box:
        return None
    rect = cv2.boxPoints(cv2.minAreaRect(contour))
    return _order_corners(rect)


def _quad_from_observed_edges(contour) -> list[list[float]] | None:
    # Hull/box corners only seed the search, never become captured screen corners.
    seed = _quad_from_contour(cv2.convexHull(contour))
    if seed is None:
        return None
    return _quad_from_screen_edges(contour, np.asarray(seed), 32.0)


def _quad_from_screen_edges(contour, expected: np.ndarray,
                            tolerance_px: float) -> list[list[float]] | None:
    """Intersect four observed sides; the saved quad only bounds the search."""
    points = contour.reshape(-1, 2).astype(float)
    sides = np.roll(expected, -1, axis=0) - expected
    lengths = np.linalg.norm(sides, axis=1)
    if (lengths <= 0).any():
        return None
    normals = np.column_stack((-sides[:, 1], sides[:, 0])) / lengths[:, None]
    distances = np.abs(((points[:, None, :] - expected) * normals).sum(axis=2))
    lines = []
    for index, (start, side, length) in enumerate(zip(expected, sides, lengths)):
        tangent = side / length
        relative = points - start
        along = relative @ tangent / length
        # Wide search bands must not mix opposite sides of a small phone.
        # The unchanged evidence count/coverage gates also reject undersized sides.
        nearby = points[(along >= .12) & (along <= .88)
                        & (distances[:, index] <= tolerance_px)
                        & (distances[:, index] < distances[:, (index + 2) % 4])]
        if len(nearby) < max(12, .3 * length):
            return None
        direction = cv2.fitLine(
            nearby.astype(np.float32), cv2.DIST_HUBER, 0, .01, .01).reshape(4)
        normal = np.array([-direction[1], direction[0]], dtype=float)
        inliers = nearby[np.abs((nearby - direction[2:]) @ normal) <= 1.5]
        if len(inliers) < max(12, .3 * length, .7 * len(nearby)):
            return None
        direction = cv2.fitLine(
            inliers.astype(np.float32), cv2.DIST_L2, 0, .01, .01).reshape(4)
        normal = np.array([-direction[1], direction[0]], dtype=float)
        inliers = inliers[np.abs((inliers - direction[2:]) @ normal) <= 1.5]
        coverage = (inliers - start) @ tangent / length
        # Rounded ends and a narrow probe gap are allowed, not a missing side.
        bins = np.unique(np.floor((coverage - .12) / .095).astype(int))
        if len(inliers) < .3 * length or len(bins) < 6:
            return None
        lines.append((normal, float(normal @ direction[2:])))
    intersections = []
    for index in range(4):
        previous, current = lines[index - 1], lines[index]
        matrix = np.array([previous[0], current[0]])
        if abs(float(np.linalg.det(matrix))) < .2:
            return None
        intersections.append(np.linalg.solve(matrix, [previous[1], current[1]]))
    quad = np.asarray(intersections, dtype=np.float32)
    if not np.isfinite(quad).all() or not cv2.isContourConvex(quad):
        return None
    # An occluder's inner edge must not exclude still-observed parts of the phone.
    # Keep the existing max(3px, 1% of the short side) noise tolerance.
    occlusion_tolerance = max(3.0, 0.01 * float(min(
        np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[1]))))
    # A narrow exterior patch can also be the last visible strip of a mostly
    # occluded screen. Single-frame line support cannot disambiguate the two.
    if any(cv2.pointPolygonTest(
            quad, (float(point[0]), float(point[1])), True) < -occlusion_tolerance
            for point in cv2.convexHull(contour).reshape(-1, 2)):
        return None
    return _order_corners(quad)


def quad_deviation_px(corners_a: list, corners_b: list) -> float:
    """两组角点的最大对应偏差（px）。角点数不一致视为完全失配。"""
    if len(corners_a) != 4 or len(corners_b) != 4:
        return float("inf")
    return max(math.hypot(a[0] - b[0], a[1] - b[1])
               for a, b in zip(corners_a, corners_b))


def _fit_similarity(src: np.ndarray, dst: np.ndarray):
    """最小二乘拟合相似变换 dst ≈ s·R·src + t（Procrustes 闭环解）。"""
    src_c = src.mean(axis=0)
    dst_c = dst.mean(axis=0)
    p = src - src_c
    q = dst - dst_c
    cross = p.T @ q  # 2x2 互协方差
    u, _, vt = np.linalg.svd(cross)
    r = vt.T @ np.diag([1.0, float(np.sign(np.linalg.det(vt.T @ u.T)))]) @ u.T
    denom = float((p * p).sum())
    scale = 1.0 if denom <= 1e-9 else float((q * (p @ r.T)).sum() / denom)
    t = dst_c - scale * (r @ src_c)
    return scale, r, t


def classify_quad_change(saved: list, live: list, tolerance_px: float) -> dict:
    """区分"手机真位移"与"检测受环境干扰"（2026-09-05 真机误报定案）。

    真位移（平移/旋转/缩放）是刚体变化：偏差可被一个相似变换整体解释，
    且所有角点系统性参与。环境亮区污染只让部分角点（通常一侧 ≤2 个）向外/
    向内偏，例如今日实测：左上/左下像素级不动、右上外扩 31px——任何刚体
    变换都解释不了。

    判别链（安全优先：可疑即拦，检测不可信才跳过）：
    1. 单边形态（一切刚性解释之前查）：恰好一对角显著移动（原始位移
       >0.4x 容差）且移动向量一致（差 ≤0.35x 容差）、另两角静止——刚体运动
       有两点不动即恒等，这是单边画面内容变化（键盘/通知栏压暗一条边）或
       单侧环境亮区均匀并入 → anomaly。可证该豁免漏判的刚性位移
       <容差（绕对边旋转要在移动对上凑出 ≤0.35x 容差的向量差，对角位移
       被压到 0.35x 容差 ×(对角距/边高) ≈ 0.8x 容差）；
    2. 一致平移：中位数角点位移超容差且逐角对中位数残差 ≤容差（真平移
       四角 deltas 必然一致；3 角污染会把中位数拖过容差但残差必然超容差
       ——2026-09-05 真机二次误报定案）→ moved；
    3. 平移残差：≥3 角系统性偏移（>0.5x 容差；绕边缘支点 ~1° 旋转会把
       四角残差都压在容差附近，必须降阈值交给相似变换裁决）→ 拟合相似
       变换，残差 ≤容差且角点位移超容差 → moved（旋转/缩放）；残差仍大
       → anomaly；
    4. 仅 ≤2 角偏移且无一致平移 → anomaly（检测干扰，调用方按"无法核实"
       跳过）。
    """
    if len(saved) != 4 or len(live) != 4:
        return {"status": "moved", "deviationPx": float("inf"),
                "movementPx": float("inf"), "anomalyPx": float("inf"),
                "detail": "角点数异常"}
    s = np.array(saved, dtype=float)
    l = np.array(live, dtype=float)
    deltas = l - s
    deviation = float(np.max(np.linalg.norm(deltas, axis=1)))
    if deviation <= tolerance_px:
        return {"status": "ok", "deviationPx": deviation,
                "movementPx": deviation, "anomalyPx": 0.0, "detail": ""}
    # 1) 单边形态：一对角同向量移动、另两角静止（键盘/通知栏/单侧亮区）
    raw = np.linalg.norm(deltas, axis=1)
    significant = raw > 0.4 * tolerance_px
    if int(significant.sum()) == 2:
        pair = deltas[significant]
        if float(np.linalg.norm(pair[0] - pair[1])) <= 0.35 * tolerance_px:
            return {"status": "anomaly", "deviationPx": deviation,
                    "movementPx": float(np.linalg.norm(pair[0])), "anomalyPx": deviation,
                    "detail": (f"仅一对边角同向量移动 {raw[significant].max():.1f}px、"
                               "另两角静止（刚体运动有两点不动即恒等）——"
                               "判为单边画面内容变化（键盘/通知栏），不判为移动")}
    robust_t = np.median(deltas, axis=0)
    robust_t_px = float(np.linalg.norm(robust_t))
    # 平移残差（去掉中位数平移后逐角剩余偏差）
    res_t = np.linalg.norm(deltas - robust_t, axis=1)
    moved_corners = int((res_t > 0.5 * tolerance_px).sum())
    res_t_px = float(res_t.max())
    # 一致性验证（2026-09-05 真机二次误报定案）：3 个角被环境亮区污染时中位数
    # 平移会被拖到刚超容差（实测 8.14px vs 8px），但真平移四角 deltas 必然
    # 一致——逐角对中位数的残差 ≤容差才算"一致平移"判 moved；残差仍大=
    # 不是平移，落入下面的系统性/拟合分支裁决。
    if robust_t_px > tolerance_px and res_t_px <= tolerance_px:
        return {"status": "moved", "deviationPx": deviation,
                "movementPx": robust_t_px, "anomalyPx": res_t_px,
                "detail": f"四角一致平移 {robust_t_px:.1f}px（逐角残差 ≤{res_t_px:.1f}px）"
                           "——判为手机真实移动"}
    if moved_corners >= 3:
        # 系统性偏移：可能是旋转/缩放（刚体），也可能整体性检测漂移
        scale, rot, trans = _fit_similarity(s, l)
        fitted = scale * (s @ rot.T) + trans
        movement = float(np.max(np.linalg.norm(fitted - s, axis=1)))
        anomaly = float(np.max(np.linalg.norm(l - fitted, axis=1)))
        if anomaly <= tolerance_px:
            # 刚体守卫（2026-09-23 真机双态误锁定案）：手机是刚体且摄像头
            # 固定，整体缩放物理上不可能——纯缩放形态只能是检测器锁定了
            # 不同物理边界（屏幕亮区 ↔ 手机边框，实测 6-7% 外扩稳定复现），
            # 不是手机位移。仅近平移/近旋转（|scale-1| ≤1%）才可能是真移动。
            if abs(scale - 1.0) > 0.01:
                return {"status": "anomaly", "deviationPx": deviation,
                        "movementPx": robust_t_px, "anomalyPx": anomaly,
                        "detail": (f"整体缩放 {scale:.3f}（刚体+固定摄像头不可"
                                   "能缩放）——检测器锁定边界漂移（亮区↔边框），"
                                   "不判为移动")}
            if movement > tolerance_px:
                deg = math.degrees(math.atan2(rot[1, 0], rot[0, 0]))
                return {"status": "moved", "deviationPx": deviation,
                        "movementPx": movement, "anomalyPx": anomaly,
                        "detail": (f"刚性位移 {movement:.1f}px（旋转 {deg:.2f}° "
                                   f"缩放 {scale:.3f}）——整体一致，判为手机真实移动")}
            return {"status": "ok", "deviationPx": deviation,
                    "movementPx": movement, "anomalyPx": anomaly, "detail": ""}
        return {"status": "anomaly", "deviationPx": deviation,
                "movementPx": robust_t_px, "anomalyPx": anomaly,
                "detail": (f"非刚性偏差 {anomaly:.1f}px 占主导（相似变换残差仍超容差，"
                           "一致平移仅 {:.1f}px）——检测受环境亮区干扰，不判为移动"
                           .format(robust_t_px))}
    # 最终回退（≤2 角系统性偏移）：
    # a) 存在孤立大偏移角（去平移残差 > 容差）——纯刚体运动的四角残差
    #    同量级（真旋转在前面分支已被拦），孤立大角只能是检测污染；
    # b) 无孤立角时偏差由"≈容差的平移 + 小扩散"构成——交给相似变换终裁
    #    （平移 9.9px+小旋转会把对角顶过容差但逐角残差全 <0.5x 容差，
    #    不能放走）。
    if res_t_px > tolerance_px:
        return {"status": "anomaly", "deviationPx": deviation,
                "movementPx": robust_t_px, "anomalyPx": res_t_px,
                "detail": (f"孤立大偏移角 {res_t_px:.1f}px、无一致平移"
                           f"（{robust_t_px:.1f}px）——单角检测污染，不判为移动")}
    scale, rot, trans = _fit_similarity(s, l)
    fitted = scale * (s @ rot.T) + trans
    movement = float(np.max(np.linalg.norm(fitted - s, axis=1)))
    anomaly = float(np.max(np.linalg.norm(l - fitted, axis=1)))
    if anomaly <= tolerance_px and movement > tolerance_px:
        if abs(scale - 1.0) > 0.01:
            return {"status": "anomaly", "deviationPx": deviation,
                    "movementPx": robust_t_px, "anomalyPx": anomaly,
                    "detail": (f"整体缩放 {scale:.3f}（刚体+固定摄像头不可能"
                               "缩放）——检测器锁定边界漂移，不判为移动")}
        deg = math.degrees(math.atan2(rot[1, 0], rot[0, 0]))
        return {"status": "moved", "deviationPx": deviation,
                "movementPx": movement, "anomalyPx": anomaly,
                "detail": (f"刚性位移 {movement:.1f}px（旋转 {deg:.2f}° "
                           f"缩放 {scale:.3f}）——相似变换可整体解释，判为手机真实移动")}
    return {"status": "anomaly", "deviationPx": deviation,
            "movementPx": robust_t_px, "anomalyPx": res_t_px,
            "detail": (f"仅 {moved_corners} 个角显著偏移（最大 {res_t_px:.1f}px）、"
                       f"无一致平移（{robust_t_px:.1f}px）、相似变换无法整体解释"
                       f"（残差 {anomaly:.1f}px）——检测受环境亮区干扰，"
                       "不判为移动")}


def quad_digest(quad: dict) -> str:
    """屏幕边缘快照摘要（存入 hoverZ 证据，用于人工比对）。"""
    corners = quad.get("corners") or []
    if len(corners) != 4:
        return "invalid"
    (tl, tr, br, bl) = corners
    width = (math.hypot(tr[0] - tl[0], tr[1] - tl[1])
             + math.hypot(br[0] - bl[0], br[1] - bl[1])) / 2
    height = (math.hypot(bl[0] - tl[0], bl[1] - tl[1])
              + math.hypot(br[0] - tr[0], br[1] - tr[1])) / 2
    cx = sum(x for x, _ in corners) / 4
    cy = sum(y for _, y in corners) / 4
    return f"{width:.0f}x{height:.0f}px@({cx:.0f},{cy:.0f})"


# 校准页标靶颜色（CalibrationTargetView：绿色圆角矩形+十字，rgb(0,150,100)）。
# 2026-09-03 实测（HSV 取样于目标中心）：当前摄像头/白平衡下目标真实色相
# H≈108-111、S≈48-53——旧阈值 H≤98/S≥55 骑在颜色分布边缘，画面不同区域
# 色相漂移 1-2 就让检测面积从 100% 崩到 5%（被误判为遮挡/裁剪）。
# 上限放宽到 118（真青色 H≥120 仍然排除），饱和度下限降到 40，亮度保持 35。
# 静态误检由上层防线兜底：屏幕四边形 ROI + 标定点位移 + 面积一致性。
# 2026-09-29：高亮阈值方案（S 25/H 135）在自动亮度暗屏态会把整块紫屏
# 误检为绿色巨区（实测 415k px² 连通域）——回退到原暗屏标定值（该状态
# 下 Phase0 历史检出面积中位 7266px² 稳定）。屏幕全亮的短暂窗口内若
# 需要检测，由上层 ROI/面积门兜底拒绝。
_TARGET_GREEN_HSV = ((60, 40, 35), (118, 255, 255))
_TARGET_GREEN_MIN_AREA = 200.0


def detect_green_candidates(frame, *, min_area: float = _TARGET_GREEN_MIN_AREA) -> list[dict]:
    """检测画面中全部合法绿色标靶候选（v1.1 自动校准 §8.2 多候选门禁用）。

    返回按面积降序的 [{"x","y","area","w","h"}]；空列表 = 无合法候选。
    检测算法与 detect_green_target 完全一致（同 HSV 阈值/形态学/面积下限），
    只是保留全部候选而不是只回最大块——自动 runner 需要据此判断
    target_ambiguous（多候选无法唯一确定时禁止点击）。
    """
    if frame is None:
        return []
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo, hi = (np.array(c, np.uint8) for c in _TARGET_GREEN_HSV)
    mask = cv2.inRange(hsv, lo, hi)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    items: list[dict] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_area:
            continue
        m = cv2.moments(contour)
        if m["m00"] == 0:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        items.append({"x": float(m["m10"] / m["m00"]),
                      "y": float(m["m01"] / m["m00"]),
                      "area": round(area, 1),
                      "w": int(w), "h": int(h)})
    items.sort(key=lambda item: -item["area"])
    return items


def detect_green_target(frame):
    """检测手机校准页上的绿色标靶中心（摄像头像素）。

    标靶是屏幕平面上最大的绿色矩形（≈130x280 屏幕px）——与 OCR 文字同平面，
    用它做 画面→屏幕 单应不存在探头悬停平面的视差问题。失败抛 ScreenEdgeError。
    """
    items = detect_green_candidates(frame)
    if not items:
        raise ScreenEdgeError("画面中未检出绿色标靶（确认手机校准页标靶已显示）")
    best = items[0]
    return (best["x"], best["y"])


def locate_quad_by_gradient_lines(frame, expected_corners: list,
                                  *, tolerance_px: float = 32.0) -> dict:
    """在预期四角附近按梯度直线定点迭代拟合屏幕四边形（规范 §6.1.1）。

    阈值区域法在"亮桌面+暗屏"（房间开灯的常态）下会锁到背景亮区；本方法
    不做区域分割，直接在每个预期边 ±tolerance 带内收集强梯度点并拟合直线，
    四线求交得到实测四角——边就是边，与区域谁亮谁暗无关。预期四角来自
    已保存校准（本方法不接受 None），定位结果仅在该预测邻域内可信。

    定点迭代：梯度定位的答案随锚微滑（多物理边界并存时"最近簇"有滞回），
    迭代至锚自洽（≤0.75px）后答案只依赖图像本身——同一场景任意合法初始锚
    收敛到同一定点，跨帧刚性由此保证。
    偏离守卫（P1-5，2026-09-30 审查）对收敛与不收敛两条路径统一执行：
    收敛≠正确（可能锁到别的物理边界），对原始预测的偏离超限仍拒绝。
    失败抛 ScreenEdgeError（附具体缺失证据）。
    """
    anchor_work = np.asarray(expected_corners, dtype=float)
    expected = np.asarray(expected_corners, dtype=float)
    quad = None
    for _ in range(3):
        quad = _locate_quad_by_gradient_lines_once(
            frame, anchor_work.tolist(), tolerance_px=tolerance_px)
        located = np.asarray(quad["corners"], dtype=float)
        if float(np.max(np.linalg.norm(located - anchor_work, axis=1))) <= 0.75:
            break
        anchor_work = located
    # 上限 2×tolerance：开灯回退的锚可能是数日前的校准（真机实测陈旧偏移
    # ~40px）；完全锁错目标的偏离会是 100px+，防护仍然有效。
    deviation = float(np.max(np.linalg.norm(
        np.asarray(quad["corners"], dtype=float) - expected, axis=1)))
    deviation_limit = tolerance_px * 2.0
    if deviation > deviation_limit:
        raise ScreenEdgeError(
            f"按校准预测定位的四角偏离预测 {deviation:.1f}px（>"
            f"{deviation_limit:.0f}px）：手机可能已移动，请复核")
    return quad


def _locate_quad_by_gradient_lines_once(frame, expected_corners: list,
                                        *, tolerance_px: float = 32.0) -> dict:
    if frame is None:
        raise ScreenEdgeError("摄像头帧为空")
    height, width = frame.shape[:2]
    expected = np.asarray(expected_corners, dtype=float)
    if (expected.shape != (4, 2) or not np.isfinite(expected).all()
            or not np.isfinite(tolerance_px) or tolerance_px <= 0):
        raise ScreenEdgeError("校准屏幕区域无效")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    grad_x = cv2.Sobel(blur, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(blur, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(grad_x, grad_y)
    strong = magnitude > max(30.0, float(np.percentile(magnitude, 95)))
    ys, xs = np.nonzero(strong)
    if len(xs) == 0:
        raise ScreenEdgeError("画面中无可用边缘梯度，无法按校准预测定位手机")
    points = np.column_stack((xs, ys)).astype(float)

    lines = []
    for index in range(4):
        start = expected[index]
        side = expected[(index + 1) % 4] - start
        length = float(np.linalg.norm(side))
        if length < 24:
            raise ScreenEdgeError("校准屏幕区域边长过短，无法按预测定位")
        tangent = side / length
        normal = np.array([-tangent[1], tangent[0]])
        rel = points - start
        along = rel @ tangent / length
        offset = rel @ normal
        # 只取预期线两侧 ±tolerance、边中段 0.10~0.90（避开圆角）；
        # 上半带/下半带分开看，取梯度更强的一侧（屏-边框 vs 桌-边框）。
        # 边界脊搜索带 ±14px：物理边界（边框外缘/屏幕边）紧贴预测线两侧
        # ~±8px；屏幕内部内容（文字、标靶网格）的梯度在更深处的偏移上，
        # 收窄带宽即排除——内容变化不再扰动边界脊（2026-09-29 真机实测
        # 标靶网格显示时逐角抖动 3-4px 的根因）。
        # 自适应边界脊搜索带：±14 排除屏幕内部内容（文字/标靶网格）；
        # 预测锚偏差较大时真实边界可能在带缘被裁，该边自动放宽到 ±24
        # （内容噪声在 ≥20px 深处，仍不会混入）。
        ridge_pts = normal_fit = None
        for boundary_search in (min(14.0, tolerance_px), min(24.0, tolerance_px),
                                min(36.0, tolerance_px + 4.0)):
            band = ((np.abs(offset) <= boundary_search)
                    & (along >= .10) & (along <= .90))
            if int(band.sum()) < max(40, .12 * length):
                continue
            sel = points[band]
            sel_off = offset[band]
            sel_mag = magnitude[ys[band], xs[band]]
            # 梯度脊选择：沿偏移量做幅值加权直方图，峰=一条物理边界。
            # 取「离预期线最近的足够强峰」，只用峰 ±2.5px 内的点拟合——
            # 锁定单一物理边界，跨帧刚性可复现。
            bins = np.arange(-boundary_search, boundary_search + 1.0, 1.0)
            hist, edges = np.histogram(sel_off, bins=bins, weights=sel_mag)
            if hist.max() <= 0:
                continue
            # 连通簇聚峰：≥25% 最大质量的连续 bin 为一簇（屏幕边/边框外缘
            # 相距 ~8px、中间有谷，自然分成两簇）；簇内取质量最大 bin 为峰；
            # 选「峰中心离预期线最近」的簇——预期锚定的是校准捕获时的同一边界。
            strength_floor = float(hist.max()) * 0.25
            above = hist >= strength_floor
            peaks = []
            i = 0
            while i < len(above):
                if above[i]:
                    j = i
                    while j + 1 < len(above) and above[j + 1]:
                        j += 1
                    peaks.append(i + int(np.argmax(hist[i:j + 1])))
                    i = j + 1
                else:
                    i += 1
            if not peaks:
                continue
            ridge_bin = min(peaks, key=lambda i: (
                abs((edges[i] + edges[i + 1]) / 2.0), -hist[i]))
            # 抛物线亚像素修正：峰顶不必贴 bin 量化边界
            ridge = (edges[ridge_bin] + edges[ridge_bin + 1]) / 2.0
            if 0 < ridge_bin < len(hist) - 1 and hist[ridge_bin] > 0:
                denominator = (hist[ridge_bin - 1] - 2 * hist[ridge_bin]
                               + hist[ridge_bin + 1])
                if abs(denominator) > 1e-9:
                    delta = 0.5 * (hist[ridge_bin - 1] - hist[ridge_bin + 1]) / denominator
                    if abs(delta) <= 1.0:
                        ridge += float(delta)
            candidate_pts = sel[np.abs(sel_off - ridge) <= 2.5]
            if len(candidate_pts) < max(24, .08 * length):
                continue
            ridge_pts = candidate_pts
            break
        if ridge_pts is None:
            raise ScreenEdgeError(
                f"预测边 {index} 的边界脊支持点不足（画面干扰或边界不清）")
        direction2 = cv2.fitLine(
            ridge_pts.astype(np.float32), cv2.DIST_HUBER, 0, .01, .01).reshape(4)
        normal_fit = np.array([-direction2[1], direction2[0]], dtype=float)
        offset_fit = float(np.mean((ridge_pts - start) @ normal_fit))
        lines.append((normal_fit, float(normal_fit @ (start + normal_fit * offset_fit))))
    intersections = []
    for index in range(4):
        previous, current = lines[index - 1], lines[index]
        matrix = np.array([previous[0], current[0]])
        if abs(float(np.linalg.det(matrix))) < .2:
            raise ScreenEdgeError("预测边拟合直线近乎平行，无法求交定位")
        intersections.append(np.linalg.solve(matrix, [previous[1], current[1]]))
    quad = np.asarray(intersections, dtype=float)
    from drivers.screen_pose import validate_corners
    try:
        validate_corners(quad.tolist())
    except ValueError as exc:
        raise ScreenEdgeError(f"预测定位四角无效：{exc}") from exc
    deviation = max(float(np.linalg.norm(quad[index] - expected[index]))
                    for index in range(4))
    # 偏离守卫统一由外层定点包装按放宽后的限执行（此处只记录）。
    return {"corners": _order_corners(quad), "frameW": width, "frameH": height,
            "source": "calibrated_prediction",
            "deviationPx": round(deviation, 1)}
