"""固定机位摄像头定位测试：全部使用合成帧，不打开真实摄像头。

覆盖：颜色色标检测、单应拟合精度、robot-point 换算、robot-complete 仿射入库、
缺标定点/缺单应的错误路径。
"""
from __future__ import annotations

import numpy as np
import pytest

fastapi = pytest.importorskip("fastapi")
cv2 = pytest.importorskip("cv2")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from adapters.screen_ime.camera_calibration import (  # noqa: E402
    ARUCO_IDS,
    MARKER_POSITIONS,
    CameraScreenTransform,
    detect_aruco_markers,
    detect_markers,
    detect_pointer_tip,
    fit_camera_to_screen,
    generate_aruco_png,
)
from adapters.screen_ime.camera_source import StaticFrameSource  # noqa: E402
from adapters.screen_ime.geometry import ScreenRobotTransform  # noqa: E402
from adapters.screen_ime.routes import (  # noqa: E402
    create_screen_ime_router,
    screen_ime_error_handler,
)
from adapters.screen_ime.service import (  # noqa: E402
    ScreenImeApiError,
    ScreenImeService,
)

CAM_W, CAM_H = 1280, 960
SCREEN_W, SCREEN_H = 1080, 2400
MARKER_BGR = {"red": (0, 0, 255), "green": (0, 255, 0),
              "blue": (255, 0, 0), "yellow": (0, 255, 255)}


def screen_points() -> dict[str, tuple[float, float]]:
    """标定点在手机屏幕（物理像素）上的位置（与页面绘制一致）。"""
    return {color: (fx * SCREEN_W, fy * SCREEN_H)
            for color, (fx, fy) in MARKER_POSITIONS.items()}


def true_screen_to_camera() -> np.ndarray:
    """构造一个已知的透视映射 screen→camera（模拟固定斜视角）。"""
    src = np.array([[0, 0], [SCREEN_W, 0], [SCREEN_W, SCREEN_H], [0, SCREEN_H]], np.float32)
    dst = np.array([[90, 60], [CAM_W - 60, 95], [CAM_W - 110, CAM_H - 50], [70, CAM_H - 90]],
                   np.float32)
    return cv2.getPerspectiveTransform(src, dst)


def draw_camera_frame(cam_positions: dict[str, tuple[float, float]],
                      tip: tuple[float, float] | None = None) -> np.ndarray:
    frame = np.full((CAM_H, CAM_W, 3), 255, np.uint8)
    for color, (cx, cy) in cam_positions.items():
        cv2.circle(frame, (int(round(cx)), int(round(cy))), 16, MARKER_BGR[color], -1)
    if tip is not None:
        cv2.circle(frame, (int(round(tip[0])), int(round(tip[1]))), 12, (255, 0, 255), -1)
    return frame


def build_app(service: ScreenImeService, frames: list) -> FastAPI:
    app = FastAPI()
    app.include_router(create_screen_ime_router(service, StaticFrameSource(frames)))
    app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)
    return app


def test_marker_detection_and_homography_accuracy():
    """合成帧：4 色标检测 + 单应拟合 + 第五点换算误差 < 1px。"""
    matrix = true_screen_to_camera()
    points = screen_points()
    cam_positions = {}
    for color, (sx, sy) in points.items():
        vec = matrix @ np.array([sx, sy, 1.0])
        cam_positions[color] = (vec[0] / vec[2], vec[1] / vec[2])
    frame = draw_camera_frame(cam_positions)

    detected = detect_markers(frame)
    assert set(detected) == set(points), f"应检出全部 4 色，实际 {sorted(detected)}"
    for color, (cx, cy) in cam_positions.items():
        dx, dy = detected[color][0] - cx, detected[color][1] - cy
        assert dx ** 2 + dy ** 2 < 4.0, f"{color} 质心偏差过大"

    correspondences = [{"cameraX": detected[c][0], "cameraY": detected[c][1],
                        "screenX": points[c][0], "screenY": points[c][1]}
                       for c in points]
    fit = fit_camera_to_screen(correspondences)
    assert fit["reprojectionRms"] < 1.0

    transform = CameraScreenTransform.from_dict(
        {"homography": fit["homography"], "reprojectionRms": fit["reprojectionRms"]})
    # 第五个点（屏幕中心）换算误差 < 1px
    sx, sy = SCREEN_W * 0.5, SCREEN_H * 0.5
    vec = matrix @ np.array([sx, sy, 1.0])
    cam_x, cam_y = vec[0] / vec[2], vec[1] / vec[2]
    back_x, back_y = transform.screen_from_camera(cam_x, cam_y)
    assert abs(back_x - sx) < 1.0 and abs(back_y - sy) < 1.0


def test_five_point_fit_does_not_accept_zero_rms_folded_subset():
    """回归：5 点轻微噪声时，RANSAC 不得用奇异 4 点子集制造 RMS=0 假成功。"""
    camera = [(651.4, 349.0), (1097.3, 159.1), (1091.7, 543.9),
              (270.8, 160.0), (263.7, 537.6)]
    screen = [(540.625, 1231.375), (183.625, 422.25), (890.25, 429.125),
              (190.75, 1923.875), (874.75, 1933.0)]
    points = [{"cameraX": cx, "cameraY": cy, "screenX": sx, "screenY": sy}
              for (cx, cy), (sx, sy) in zip(camera, screen)]
    fit = fit_camera_to_screen(points)
    assert fit["inlierCount"] == 5
    assert max(fit["errors"]) < 10.0
    transform = CameraScreenTransform.from_dict({"homography": fit["homography"]})
    center = transform.screen_from_camera(691.7, 352.0)
    assert center == pytest.approx((540.0, 1160.0), abs=12.0)


def test_pointer_tip_detection():
    frame = np.full((CAM_H, CAM_W, 3), 255, np.uint8)
    assert detect_pointer_tip(frame) is None
    cv2.circle(frame, (700, 430), 12, (255, 0, 255), -1)  # 品红探头尖
    tip = detect_pointer_tip(frame)
    assert tip is not None and abs(tip[0] - 700) < 2 and abs(tip[1] - 430) < 2


def test_camera_calibration_endpoints_end_to_end(tmp_path):
    """HTTP 全链路：markers → robot-point ×3 → robot-complete → 映射入库且可逆推。"""
    service = ScreenImeService(tmp_path / "ime.db", tmp_path / "mappings.json")
    matrix = true_screen_to_camera()
    points = screen_points()
    cam_positions = {}
    for color, (sx, sy) in points.items():
        vec = matrix @ np.array([sx, sy, 1.0])
        cam_positions[color] = (vec[0] / vec[2], vec[1] / vec[2])

    markers_frame = draw_camera_frame(cam_positions)
    # 探头尖三个位置：对应屏幕三个不共线点
    tip_screen = [(540.0, 600.0), (640.0, 600.0), (540.0, 800.0)]
    tip_frames = []
    for sx, sy in tip_screen:
        vec = matrix @ np.array([sx, sy, 1.0])
        tip_frames.append(draw_camera_frame(cam_positions, tip=(vec[0] / vec[2],
                                                                vec[1] / vec[2])))
    app = build_app(service, [markers_frame, *tip_frames])

    with TestClient(app) as client:
        # 相机状态
        status = client.get("/api/v1/screen-ime/camera/status")
        assert status.status_code == 200 and status.json()["camera"]

        # ① 屏幕段：页面回报标定点（物理像素）
        markers = [{"color": color, "screenX": sx, "screenY": sy}
                   for color, (sx, sy) in points.items()]
        report = client.post("/api/v1/screen-ime/camera/markers",
                             json={"deviceId": "k40-01", "markers": markers})
        assert report.status_code == 200, report.json()
        assert report.json()["reprojectionRms"] < 1.0
        assert report.json()["version"] == 1

        # ② 机械臂段：3 个 robot-point（每个消耗一帧）
        pairs = []
        for index, (rx, ry) in enumerate([(150.0, 60.0), (165.0, 60.0), (150.0, 75.0)]):
            point = client.post("/api/v1/screen-ime/camera/robot-point",
                                json={"deviceId": "k40-01", "robotX": rx, "robotY": ry})
            assert point.status_code == 200, point.json()
            body = point.json()
            assert abs(body["screenX"] - tip_screen[index][0]) < 1.0
            assert abs(body["screenY"] - tip_screen[index][1]) < 1.0
            pairs.append({"robotX": rx, "robotY": ry,
                          "screenX": body["screenX"], "screenY": body["screenY"]})

        # ③ 完成拟合：映射入库（与触点回报法同一张表）
        done = client.post("/api/v1/screen-ime/camera/robot-complete",
                           json={"deviceId": "k40-01", "pairs": pairs,
                                 "screenWidth": SCREEN_W, "screenHeight": SCREEN_H})
        assert done.status_code == 200, done.json()
        mapping = service.mappings.get("k40-01")
        assert mapping is not None and mapping["residualRms"] < 1e-6

        # ④ 下游用法验证：屏幕目标 → 机械坐标（图标点击/打字同一条链路）。
        # 本用例的真实仿射：screen_x = 6.667·rx − 460，screen_y = 13.333·ry − 200，
        # 故屏幕 (600,1200) 的解析逆解为 (159.0, 105.0)。
        transform = ScreenRobotTransform.from_dict(mapping)
        rx, ry = transform.robot_from_screen(600.0, 1200.0)
        assert abs(rx - 159.0) < 0.5 and abs(ry - 105.0) < 0.5

    service.close()


def test_camera_endpoints_error_paths(tmp_path):
    """缺标定点（画面无色标）→ 409；未标定就查探头 → 404；帧耗尽 → 503。"""
    service = ScreenImeService(tmp_path / "ime.db", tmp_path / "mappings.json")
    blank = np.full((CAM_H, CAM_W, 3), 255, np.uint8)
    app = build_app(service, [blank])
    points = screen_points()
    markers = [{"color": color, "screenX": sx, "screenY": sy}
               for color, (sx, sy) in points.items()]
    with TestClient(app) as client:
        failed = client.post("/api/v1/screen-ime/camera/markers",
                             json={"deviceId": "k40-01", "markers": markers})
        assert failed.status_code == 409
        assert failed.json()["error"] == "camera_markers_not_detected"
        assert "缺" in failed.json()["message"]

        missing = client.post("/api/v1/screen-ime/camera/robot-point",
                              json={"deviceId": "k40-01", "robotX": 1, "robotY": 2})
        assert missing.status_code == 404
        assert missing.json()["error"] == "mapping_not_found"

        exhausted = client.post("/api/v1/screen-ime/camera/markers",
                                json={"deviceId": "k40-01", "markers": markers})
        assert exhausted.status_code == 503
        assert exhausted.json()["error"] == "camera_unavailable"
    service.close()


# ---------------------------------------------------------------------------
# ArUco 基准码（首选方案）+ 手动触点配对（机械臂直接点屏幕，无需贴标）
# ---------------------------------------------------------------------------

def draw_aruco_camera_frame(cam_positions: dict[int, tuple[float, float]]) -> np.ndarray:
    """把 4 个 ArUco 码（含白边）贴到合成相机帧的指定位置。"""
    frame = np.full((CAM_H, CAM_W, 3), 255, np.uint8)
    cv2.rectangle(frame, (0, 0), (CAM_W - 1, CAM_H - 1), (0, 0, 0), 6)  # 黑边框参考
    for marker_id, (cx, cy) in cam_positions.items():
        png = generate_aruco_png(marker_id, side_px=100)
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        h, w = img.shape
        x0, y0 = int(cx - w / 2), int(cy - h / 2)
        frame[y0:y0 + h, x0:x0 + w] = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return frame


def test_aruco_detection_and_end_to_end(tmp_path):
    """ArUco 按 ID 配对：页面报 {id, screenX, screenY}，检测按 ID 对应，不会张冠李戴。"""
    matrix = true_screen_to_camera()
    cam_positions = {}
    for marker_id, (fx, fy) in ARUCO_IDS.items():
        sx, sy = fx * SCREEN_W, fy * SCREEN_H
        vec = matrix @ np.array([sx, sy, 1.0])
        cam_positions[marker_id] = (vec[0] / vec[2], vec[1] / vec[2])
    frame = draw_aruco_camera_frame(cam_positions)

    detected = detect_aruco_markers(frame)
    assert set(detected) == set(ARUCO_IDS), f"应按 ID 检出 4 个基准码，实际 {sorted(detected)}"
    for marker_id, (cx, cy) in cam_positions.items():
        assert (detected[marker_id][0] - cx) ** 2 + (detected[marker_id][1] - cy) ** 2 < 9.0

    service = ScreenImeService(tmp_path / "ime.db", tmp_path / "m.json")
    app = build_app(service, [frame])
    with TestClient(app) as client:
        markers = [{"id": marker_id, "screenX": fx * SCREEN_W, "screenY": fy * SCREEN_H}
                   for marker_id, (fx, fy) in ARUCO_IDS.items()]
        report = client.post("/api/v1/screen-ime/camera/markers",
                             json={"deviceId": "k40-01", "markers": markers})
        assert report.status_code == 200, report.json()
        assert report.json()["mode"] == "aruco"
        assert report.json()["reprojectionRms"] < 1.0
    service.close()


def test_manual_touch_robot_complete_flow(tmp_path):
    """用户流程：机械臂直接点屏幕（页面自动回报触点）→ 电脑端按顺序给机械坐标 → 完成拟合。"""
    service = ScreenImeService(tmp_path / "ime.db", tmp_path / "m.json")
    app = build_app(service, [])  # 本流程不取帧
    robot_coords = [(150.0, 60.0), (165.0, 60.0), (150.0, 75.0)]
    # 机械臂点下的屏幕位置（页面收到 ACTION_UP 上报；这里模拟真实仿射）
    true_screen = [(2.0 * rx + 540.0, 2.0 * ry + 1140.0) for rx, ry in robot_coords]
    with TestClient(app) as client:
        for sx, sy in true_screen:
            touched = client.post("/api/v1/screen-ime/camera/manual-touch",
                                  json={"deviceId": "k40-01", "screenX": sx, "screenY": sy})
            assert touched.json()["touchCount"] == len(
                service.manual_touches.get("k40-01") or [])
        done = client.post("/api/v1/screen-ime/camera/robot-complete",
                           json={"deviceId": "k40-01", "useManualTouches": True,
                                 "robotCoords": robot_coords,
                                 "screenWidth": SCREEN_W, "screenHeight": SCREEN_H})
        assert done.status_code == 200, done.json()
        mapping = service.mappings.get("k40-01")
        assert mapping is not None and mapping["residualRms"] < 1e-6
        # 配对后清空触点，防止下次误用旧数据
        assert (service.manual_touches.get("k40-01") or []) == [
            {"screenX": sx, "screenY": sy, "at": ""} for sx, sy in true_screen]

    # 数量不匹配必须拒绝（防止错位配对）
    service2 = ScreenImeService(tmp_path / "ime2.db", tmp_path / "m2.json")
    app2 = build_app(service2, [])
    with TestClient(app2) as client:
        bad = client.post("/api/v1/screen-ime/camera/robot-complete",
                          json={"deviceId": "k40-01", "useManualTouches": True,
                                "robotCoords": [[1, 2], [3, 4]]})
        assert bad.status_code == 400
    service.close()
    service2.close()


# ---------------------------------------------------------------------------
# v1.1 自动校准 Phase 0：严格全点质量门禁（fit_camera_to_screen_strict）+ 多候选检测
# ---------------------------------------------------------------------------

from adapters.screen_ime.camera_calibration import fit_camera_to_screen_strict  # noqa: E402
from drivers.screen_edge import detect_green_candidates, detect_green_target  # noqa: E402
from drivers.screen_edge import ScreenEdgeError  # noqa: E402

AUTO_SCREEN_W, AUTO_SCREEN_H = 1080, 2320


def _auto_grid_points(perturb: dict[int, tuple[float, float]] | None = None):
    """行主序 15%/50%/85% 网格对应点（补充文档 §2），相机=屏幕线性缩放。"""
    ratios = (0.15, 0.50, 0.85)
    points = []
    for r, v in enumerate(ratios):
        for c, u in enumerate(ratios):
            index = r * 3 + c
            sx, sy = u * AUTO_SCREEN_W, v * AUTO_SCREEN_H
            cx, cy = sx * 0.5, sy * 0.4
            if perturb and index in perturb:
                cx += perturb[index][0]
                cy += perturb[index][1]
            points.append({"cameraX": cx, "cameraY": cy, "screenX": sx, "screenY": sy})
    return points


def test_strict_fit_accepts_exact_nine_point_grid():
    result = fit_camera_to_screen_strict(
        _auto_grid_points(), rms_max_px=3.0, max_error_px=6.0,
        screen_width=AUTO_SCREEN_W, screen_height=AUTO_SCREEN_H)
    assert result["ok"] is True
    assert result["reason"] is None
    assert result["inlierCount"] == 9 and result["inlierRatio"] == 1.0
    assert result["rms"] <= 3.0 and result["maxError"] <= 6.0
    assert result["monotonic"] is True and result["boundary"] is True


def test_strict_fit_rejects_any_grid_outlier():
    """9 点网格任一外点 → 拒绝（4/5 式剔除不允许，§9.3）。"""
    result = fit_camera_to_screen_strict(
        _auto_grid_points({6: (200.0, 0.0)}),  # calibration-7 外点
        rms_max_px=3.0, max_error_px=6.0,
        screen_width=AUTO_SCREEN_W, screen_height=AUTO_SCREEN_H)
    assert result["ok"] is False
    assert result["reason"]["code"] == "camera_outlier_present"
    assert "点7" in result["reason"]["message"]


def test_strict_fit_rejects_grid_order_violation():
    """行内屏幕坐标次序倒退（交换两列）→ 拒绝。"""
    points = _auto_grid_points()
    points[1]["screenX"], points[2]["screenX"] = points[2]["screenX"], points[1]["screenX"]
    result = fit_camera_to_screen_strict(
        points, rms_max_px=3.0, max_error_px=6.0,
        screen_width=AUTO_SCREEN_W, screen_height=AUTO_SCREEN_H)
    assert result["ok"] is False


def test_strict_fit_rejects_plan_size_mismatch():
    result = fit_camera_to_screen_strict(
        _auto_grid_points()[:5], rms_max_px=3.0, max_error_px=6.0)
    assert result["ok"] is False
    assert result["reason"]["code"] == "camera_plan_size_mismatch"


def test_strict_fit_reports_quality_below_target():
    """全点轻微噪声累积超阈值 → 质量未达标（不是外点路径）。"""
    noisy = _auto_grid_points(
        {i: ((1.2, 0.8) if i % 2 == 0 else (-1.2, -0.8)) for i in range(9)})
    result = fit_camera_to_screen_strict(
        noisy, rms_max_px=0.5, max_error_px=0.8,
        screen_width=AUTO_SCREEN_W, screen_height=AUTO_SCREEN_H)
    assert result["ok"] is False
    assert result["reason"]["code"] == "camera_quality_below_target"


def test_green_candidates_keeps_all_and_target_picks_largest():
    """多候选检测保留全部合法块；旧 detect_green_target 语义不变（取最大）。"""
    frame = np.full((720, 1280, 3), 255, np.uint8)
    # 生产标靶绿 rgb(0,150,100) → BGR(100,150,0)，落在 HSV 阈值 H68..98
    cv2.rectangle(frame, (100, 100), (160, 220), (100, 150, 0), -1)   # 大块
    cv2.rectangle(frame, (600, 400), (640, 480), (100, 150, 0), -1)   # 小块
    items = detect_green_candidates(frame)
    assert len(items) == 2
    assert items[0]["area"] >= items[1]["area"]
    assert items[0]["x"] == pytest.approx(130.0, abs=1.0)
    assert detect_green_target(frame) == (items[0]["x"], items[0]["y"])
    assert detect_green_candidates(np.zeros((720, 1280, 3), np.uint8)) == []
    with pytest.raises(ScreenEdgeError):
        detect_green_target(np.zeros((720, 1280, 3), np.uint8))


def test_green_mask_covers_teal_shifted_target_and_excludes_cyan():
    """2026-09-03 现场：目标实际色相 H≈108-111/S≈48-53，阈值必须覆盖；
    真青色（H≥120，如青蓝色贴纸）必须继续排除。"""
    def frame_of(h, s, v):
        hsv = np.zeros((720, 1280, 3), np.uint8)
        hsv[:, :] = (h, s, v)
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        color = tuple(int(c) for c in bgr[0, 0])
        cv2.rectangle(bgr, (500, 300), (620, 420), color, -1)
        return bgr

    teal = detect_green_candidates(frame_of(110, 50, 200))
    assert teal and teal[0]["area"] > 5000
    dim_teal = detect_green_candidates(frame_of(111, 45, 180))
    assert dim_teal and dim_teal[0]["area"] > 5000
    cyan = detect_green_candidates(frame_of(130, 200, 200))
    assert cyan == []
    gray = detect_green_candidates(frame_of(90, 25, 200))
    assert gray == []
