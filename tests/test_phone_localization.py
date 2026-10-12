"""F3 公共手机定位模块验收（规范 §7 / §13.2 V04、V06 及暗屏规则）。

- V04 反光缺边、固连锚点合格 → 得到有来源的实时姿态；不是复制校准角点。
- V06 屏幕边缘与锚点矛盾 → 拒绝本帧，不取平均或任选一个。
- §7.3 质量规则：统计只取手机内部区域；暗屏 waiting；全帧纯黑
  waiting+unavailable，绝不报 offline/blocked。
- §7.1 公共定位结果字段完整性：无相对基准时位移/尺度为空，不填零或一。

全部纯函数合成数据，不访问真实硬件。
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from drivers.phone_localization import (
    DARK_BRIGHTNESS_MAX,
    DARK_TEXTURE_MAX,
    EDGE_LANDMARK_CONFLICT_PX,
    LANDMARK_FB_MAX_PX,
    LANDMARK_MIN_MATCHES,
    LANDMARK_MIN_REGIONS,
    LANDMARK_MIN_RETENTION,
    blank_result,
    fit_landmark_pose,
    locate_phone_quality,
    resolve_conflict,
)

FRAME_W, FRAME_H = 640, 480
SAVED_QUAD = [[60.0, 40.0], [580.0, 40.0], [580.0, 440.0], [60.0, 440.0]]


def _render_frame(background: int, phone_level: int, *, texture: bool,
                  quad=SAVED_QUAD) -> bytes:
    """合成相机帧：均匀背景 + 手机矩形（可选内部纹理）。"""
    rng = np.random.default_rng(20260929)
    image = np.full((FRAME_H, FRAME_W), background, np.uint8)
    pts = np.asarray(quad, np.int32)
    cv2.fillConvexPoly(image, pts, phone_level)
    if texture:
        x, y, w, h = cv2.boundingRect(pts)
        inner = image[y + 20:y + h - 20, x + 20:x + w - 20]
        for _ in range(400):
            cx = rng.integers(0, inner.shape[1])
            cy = rng.integers(0, inner.shape[0])
            cv2.line(inner, (cx, cy), (cx + 8, cy + 6),
                     int(min(255, phone_level + 55)), 1)
    ok, png = cv2.imencode(".png", image)
    assert ok
    return png.tobytes()


# ---------- §7.1 字段完整性 ----------

def test_result_contains_all_public_fields_and_uses_empty_not_zero():
    result = blank_result()
    fields = ("quality", "pose_status", "geometry_source", "corners",
              "camera_to_screen", "reference_kind", "reference_id", "matrix",
              "source_frame_seq", "source_captured_at", "source_received_at",
              "binding", "movementPx", "fitErrorPx", "reason_code")
    for field in fields:
        assert field in result, f"缺少字段 {field}"
    binding_fields = ("deviceId", "cameraDeviceUniqueId", "cameraId",
                      "bindingRevision", "frameWidth", "frameHeight",
                      "publishedCalibrationFingerprint", "candidateId")
    for field in binding_fields:
        assert field in result["binding"], f"binding 缺少字段 {field}"
    # 无相对基准时位移/尺度为空，不填零或一表示已通过
    assert result["movementPx"] is None and result["fitErrorPx"] is None
    assert result["matrix"] is None and result["scaleChange"] is None
    assert result["pose_status"] in ("verified", "unavailable")
    assert result["quality"] is None  # 未分类，而不是伪造 clear


def test_locate_result_reports_frame_binding_from_decoded_frame():
    result = locate_phone_quality(_render_frame(20, 210, texture=True))
    assert result["binding"]["frameWidth"] == FRAME_W
    assert result["binding"]["frameHeight"] == FRAME_H


# ---------- §7.3 质量规则 ----------

def test_dark_phone_bright_background_is_waiting_with_phone_interior_stats_only():
    """V01a：暗手机+亮背景 → waiting；统计只取手机内部区域（排除环境）。"""
    frame = _render_frame(background=250, phone_level=8, texture=False)
    result = locate_phone_quality(frame)
    assert result["quality"] == "waiting"
    assert result["reason_code"] == "dark_screen"
    # 暗屏判定来自手机内部（均值≤18 且 std<8），而非全帧均值
    assert result["metrics"]["brightness"] <= DARK_BRIGHTNESS_MAX
    assert result["metrics"]["texture"] < DARK_TEXTURE_MAX
    assert result["metrics"]["brightness"] < 30  # 背景亮度 250 未被计入


def test_bright_phone_dark_background_clear_by_phone_texture():
    """V01b：亮手机+暗背景 → 按手机自身纹理判 clear。"""
    frame = _render_frame(background=15, phone_level=205, texture=True)
    result = locate_phone_quality(frame)
    assert result["quality"] == "clear"
    assert result["pose_status"] == "verified"
    assert result["geometry_source"] == "observed_edges"
    for got, want in zip(result["corners"], SAVED_QUAD):
        assert abs(got[0] - want[0]) <= 6 and abs(got[1] - want[1]) <= 6


def test_fresh_full_black_frame_waits_without_offline_or_fake_pose():
    """V02：新鲜全黑帧、无四边 → waiting，无伪造姿态，不触发 offline。"""
    black = np.zeros((FRAME_H, FRAME_W), np.uint8)
    result = locate_phone_quality(cv2.imencode(".png", black)[1].tobytes())
    assert result["quality"] == "waiting"
    assert result["pose_status"] == "unavailable"
    assert result["reason_code"] == "dark_screen"
    assert result["corners"] is None
    serialized = repr(result)
    assert "offline" not in serialized and "blocked" not in serialized


def test_calibrated_roi_dark_screen_can_only_wait_not_verify_pose():
    """§7.3.3：仅绑定校准 ROI 可判暗屏 → 只允许等待，不证明当前位置。"""
    frame = _render_frame(background=250, phone_level=6, texture=False)
    result = locate_phone_quality(frame, roi_corners=SAVED_QUAD)
    assert result["quality"] == "waiting"
    assert result["geometry_source"] == "calibrated_roi_only"
    assert result["pose_status"] == "unavailable"
    assert result["corners"] is None


def test_undecodable_frame_is_abnormal_not_rescued_by_dark_exception():
    """§7.3.5：不能解码 → 不能借黑屏例外放行。"""
    result = locate_phone_quality(b"\x00\x01not-an-image")
    assert result["quality"] == "abnormal"
    assert result["reason_code"] == "decode_failed"
    assert result["pose_status"] == "unavailable"


def test_overexposed_phone_roi_is_overexposed_not_waiting():
    """§7.3.6：手机 ROI 过曝仍按过曝（不与暗屏合并）。"""
    frame = _render_frame(background=20, phone_level=255, texture=True)
    result = locate_phone_quality(frame)
    assert result["quality"] == "overexposed"


# ---------- §7.2 锚点路径（V04） ----------

def _landmark_points():
    """≥4 个覆盖 ≥3 个边/角区域、凸包足够的固连锚点（机身/边框特征）。"""
    quad = np.asarray(SAVED_QUAD)
    top = (quad[0] + quad[1]) / 2 + np.array([0, 6])
    bottom = (quad[3] + quad[2]) / 2 - np.array([0, 6])
    left = (quad[0] + quad[3]) / 2 + np.array([6, 0])
    right = (quad[1] + quad[2]) / 2 - np.array([6, 0])
    return [tuple(p) for p in (top, bottom, left, right,
                               tuple(quad[0] + np.array([10, 10])),
                               tuple(quad[2] - np.array([10, 10])))]


def test_v04_landmark_pose_is_live_sourced_not_copied_calibration_corners():
    """V04：反光缺边、固连锚点合格 → 有来源的实时姿态；不是复制校准角点。"""
    reference = _landmark_points()
    offset = np.array([8.0, 4.0])
    live = [tuple(np.asarray(p) + offset) for p in reference]
    result = fit_landmark_pose(SAVED_QUAD, reference, live)
    assert result["pose_status"] == "verified"
    assert result["geometry_source"] == "phone_landmarks"
    # 实时姿态 = 校准四角经拟合变换后的当前位置，而不是原样复制
    moved = (np.asarray(SAVED_QUAD) + offset)
    for got, want in zip(result["corners"], moved.tolist()):
        assert abs(got[0] - want[0]) <= 0.5 and abs(got[1] - want[1]) <= 0.5
    assert result["corners"] != SAVED_QUAD
    # 姿态质量与补偿限值判定依据齐全
    assert result["matrix"] is not None
    assert result["movementPx"] == pytest.approx(np.hypot(8, 4), abs=0.01)
    assert result["fitErrorPx"] <= 2.0
    assert result["landmark"]["matches"] >= LANDMARK_MIN_MATCHES
    assert result["landmark"]["retention"] >= LANDMARK_MIN_RETENTION
    assert len(result["landmark"]["regions"]) >= LANDMARK_MIN_REGIONS


def test_landmark_pose_rejects_insufficient_matches_or_coverage_or_hull():
    quad = np.asarray(SAVED_QUAD)
    # 只有 3 个匹配（<4）
    few = [tuple(quad.mean(axis=0) + d) for d in ((-60, 0), (0, -60), (60, 0))]
    result = fit_landmark_pose(SAVED_QUAD, few, few)
    assert result["pose_status"] == "unavailable"
    assert result["reason_code"] == "landmark_matches_insufficient"
    assert result["corners"] is None
    # 4 个匹配但都集中在同一条边（覆盖 <3 区域）
    same_edge = [tuple(quad[0] + np.array([10 + i * 120, 10])) for i in range(4)]
    result2 = fit_landmark_pose(SAVED_QUAD, same_edge, same_edge)
    assert result2["reason_code"] in ("landmark_coverage_insufficient",
                                      "landmark_hull_too_small")
    assert result2["pose_status"] == "unavailable"
    # 覆盖 3 个边/角区域但匹配点凸包是一条细带（<校准手机面积 20%）
    thin_band = [(70.0, 50.0), (300.0, 50.0), (560.0, 50.0), (310.0, 80.0)]
    result3 = fit_landmark_pose(SAVED_QUAD, thin_band, thin_band)
    assert result3["reason_code"] == "landmark_hull_too_small"
    assert result3["landmark"]["hullRatio"] < 0.2
    assert result3["pose_status"] == "unavailable"


def test_landmark_pose_enforces_retention_and_fb_error_limit():
    reference = _landmark_points()
    # 10 个锚点、4 个前后向误差 >1.5px → 保留率 0.6 <0.7 拒绝
    extra = [tuple(np.asarray(SAVED_QUAD).mean(axis=0) + np.array([dx, dy]))
             for dx, dy in ((-40, -30), (40, -30), (-40, 30), (40, 30))]
    reference10 = reference + extra
    live10 = list(reference10)
    fb = [0.2] * len(reference10)
    for index in range(4):
        fb[index] = LANDMARK_FB_MAX_PX + 0.5
    result = fit_landmark_pose(SAVED_QUAD, reference10, live10, fb_errors=fb)
    assert result["reason_code"] == "landmark_retention_below_min"
    assert result["pose_status"] == "unavailable"
    assert result["corners"] is None


def test_landmark_pose_keeps_f4_movement_limit():
    """锚点拟合沿用 F4 限制：位移超 16px 拒绝，不放宽。"""
    reference = _landmark_points()
    offset = np.array([20.0, 12.0])  # 位移 ≈23px >16px
    live = [tuple(np.asarray(p) + offset) for p in reference]
    result = fit_landmark_pose(SAVED_QUAD, reference, live)
    assert result["pose_status"] == "unavailable"
    assert result["reason_code"] == "landmark_pose_limit_exceeded"
    assert result["corners"] is None


# ---------- §7.2 同帧矛盾（V06） ----------

def _edges_pose_from_frame():
    frame = _render_frame(background=15, phone_level=205, texture=True)
    return locate_phone_quality(frame)


def test_v06_conflicting_edges_and_landmarks_rejects_frame_without_averaging():
    """V06：屏幕边缘与锚点矛盾 → 拒帧（geometry_conflict），不取平均或择优。"""
    edges = _edges_pose_from_frame()
    assert edges["pose_status"] == "verified"
    # 锚点姿态与边缘实测对应角差 >2px（真实矛盾）
    shifted = (np.asarray(edges["corners"]) + np.array([5.0, 0.0])).tolist()
    landmarks = {"corners": shifted, "pose_status": "verified",
                 "geometry_source": "phone_landmarks"}
    result = resolve_conflict(edges, landmarks)
    assert result["pose_status"] == "unavailable"
    assert result["reason_code"] == "geometry_conflict"
    assert result["geometry_source"] == "none"
    # 不取平均、不择优：无可用姿态输出
    assert result["corners"] is None
    assert result["conflict"]["cornerDeviationPx"] > EDGE_LANDMARK_CONFLICT_PX


def test_conflict_boundary_exact_limit_passes_epsilon_over_rejects():
    edges = {"corners": SAVED_QUAD}
    quad = np.asarray(SAVED_QUAD)
    at_limit = (quad + np.array([EDGE_LANDMARK_CONFLICT_PX, 0.0])).tolist()
    result = resolve_conflict(edges, {"corners": at_limit})
    assert result["pose_status"] == "verified"  # 等于上限：通过其他条件后可用
    assert result["geometry_source"] == "observed_edges"
    over = (quad + np.array([EDGE_LANDMARK_CONFLICT_PX + 0.01, 0.0])).tolist()
    result2 = resolve_conflict(edges, {"corners": over})
    assert result2["pose_status"] == "unavailable"
    assert result2["reason_code"] == "geometry_conflict"


def test_consistent_edges_and_landmarks_keep_measured_edges_with_cross_check():
    edges = _edges_pose_from_frame()
    landmarks = {"corners": (np.asarray(edges["corners"]) + 0.5).tolist()}
    result = resolve_conflict(edges, landmarks)
    assert result["pose_status"] == "verified"
    assert result["geometry_source"] == "observed_edges"
    assert result["conflict"]["cornerDeviationPx"] <= EDGE_LANDMARK_CONFLICT_PX
    for got, want in zip(result["corners"], edges["corners"]):
        assert got == want  # 边缘实测原样保留，未与锚点取平均
