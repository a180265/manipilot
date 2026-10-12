"""导航链路代码扫查发现问题的回归测试（视觉→滑动→视觉反馈）。

真机 rig 为轴互换（camera y↔screen x）：本文件锁定三个修复：
1. 位移测量必须先把帧矫正到校准平面（§4.3），再按屏幕域 ROI 裁剪；
2. search 位移方向必须与动作期望一致（方向相反不算 verified）；
3. 滑动程序坐标必须用屏幕域 ROI（布局相机 ROI 经 H 换算），
   不得把相机域归一化直接当屏幕域用。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")


from runtime import runtime as runtime_mod
from tests.test_gesture_plugins import (
    AutoNavContext, auto_nav_payload, list_observation, load_module,
    seed_navigation, navigation_context_root,
)

# 相对本文件定位（arm_fleet/tests/），pytest 从仓库根或 arm_fleet/ 运行都成立
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "examples/plugins/wx-wechat-adapter"
# 轴互换 rig（与真机 bundle 同构：camera y→screen x，camera x→screen y 翻转）
H_AXIS_SWAP = [[-0.01, 2.23, -254.96], [-2.25, 0.02, 2607.19], [0.0, 0.0, 1.0]]
SCREEN_W, SCREEN_H = 1080, 2320
CAM_W, CAM_H = 1280, 720


def _warped_textured(seed: int = 7) -> np.ndarray:
    """屏幕平面上的合成列表画面。"""
    import cv2
    rng = np.random.default_rng(seed)
    img = (120 + ((np.arange(SCREEN_H)[:, None] // 36) * 6)
           + ((np.arange(SCREEN_W)[None, :] // 44) * 7)).astype(np.uint8)
    for _ in range(60):
        x, y = int(rng.integers(0, SCREEN_W - 120)), int(rng.integers(0, SCREEN_H - 50))
        cv2.rectangle(img, (x, y),
                      (x + int(rng.integers(40, 110)), y + int(rng.integers(16, 44))),
                      int(rng.integers(30, 255)), -1)
    return img


def _camera_frame(screen_img: np.ndarray, h: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.warpPerspective(screen_img, np.linalg.inv(h), (CAM_W, CAM_H))


class _FakeEvidence:
    def __init__(self, frames: dict, context, bundle):
        self.frames = frames
        self.context = context
        self.bundle = bundle

    def get(self, evidence_id):
        import hashlib
        record = {"evidence_id": evidence_id, "file_path": None,
                  "device_id": "arm-01", "task_id": "t", "camera_id": self.bundle["cameraId"],
                  "width": CAM_W, "height": CAM_H,
                  "sampling": {
                      "device_unique_id": self.bundle["cameraDeviceUniqueId"],
                      "camera_binding_id": self.bundle.get("cameraBindingId"),
                      "source_geometry_id": self.bundle.get("sourceGeometryId"),
                      "source_generation_id": "offline-plane",
                      "source_capture_seq": list(self.frames).index(evidence_id)},
                  "observation_context": self.context}
        record["sha256"] = hashlib.sha256(self.read_bytes(record)).hexdigest()
        return record

    def read_bytes(self, record):
        import cv2
        return cv2.imencode(".png", self.frames[record["evidence_id"]])[1].tobytes()


class _FakeRuntimeForMeasure:
    """只提供 measure_scroll 所需最小面的替身 runtime。"""

    def __init__(self, tmp_path: Path, frames: dict):
        from drivers import calibration_bundle as cb
        from drivers.screen_edge import _order_corners
        from tests.test_screen_gesture_driver import gesture_bundle
        import cv2
        bundle = gesture_bundle()
        bundle["camera"]["cameraToScreen"]["homography"] = H_AXIS_SWAP
        screen_corners = np.array(
            [[[0, 0], [SCREEN_W - 1, 0],
              [SCREEN_W - 1, SCREEN_H - 1], [0, SCREEN_H - 1]]], dtype=np.float32)
        camera_corners = cv2.perspectiveTransform(
            screen_corners, np.linalg.inv(np.array(H_AXIS_SWAP)))[0]
        bundle["screen"]["edgeModel"]["corners"] = _order_corners(camera_corners)
        cb.save_bundle(bundle, tmp_path / "data")
        self.devices = type("D", (), {
            "driver": lambda self, device_id: type(
                "R", (), {"data_root": tmp_path / "data"})(),
            "get": lambda self, device_id: {"camera_id": bundle["cameraId"]},
            "camera_broker": lambda self, device_id: None})()
        self.config = type("C", (), {"root": tmp_path})()
        self._gesture_data_root = lambda device_id: tmp_path / "data"
        # Geometry-only fixture models a legacy device with no region package.
        # Full temporary stores and the real grab producer are tested separately.
        self.layout_bundle = lambda: {"region_store": None}
        from runtime.scroll_context import ScrollContext
        self.evidence = _FakeEvidence(
            frames, ScrollContext(self, "arm-01").archive_context(), bundle)


def _run_measure(runtime, params):
    import inspect
    result = runtime_mod.ArmFleetRuntime._invoke_gesture_capability(
        runtime, "wx-wechat-adapter", {"task_id": "t", "device_id": "arm-01"},
        "camera.measure_scroll", params, {"manifest": {"limits": {}}})
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


class TestMeasureScrollWarpsToCalibrationPlane:
    def test_axis_swap_rig_chain_measures_screen_dy(self, tmp_path):
        """列表沿屏幕 y 滚动（相机帧里表现为 dx）——矫正平面 + 相邻帧链量出 dy。"""
        h = np.array(H_AXIS_SWAP)
        base = _warped_textured()
        frames = {}
        evidence_ids = []
        for index, offset in enumerate(range(0, -361, -30)):  # 13 帧 × -30px
            key = f"evd-{index}"
            frames[key] = _camera_frame(np.roll(base, offset, axis=0), h)
            evidence_ids.append(key)
        runtime = _FakeRuntimeForMeasure(tmp_path, frames)
        roi_camera = {"x": 100, "y": 260, "width": 1000, "height": 200}
        result = _run_measure(runtime, {
            "op": "measure", "evidence_ids": evidence_ids, "roi": roi_camera})
        assert result["status"] == "displaced", result
        assert result["dy_px"] == pytest.approx(-360, abs=30)
        assert result["chain_frames"] == 13

    def test_chain_quality_failure_is_unknown(self, tmp_path):
        """链中任一对质量失败：整体 unknown，不判成没滚动。"""
        h = np.array(H_AXIS_SWAP)
        base = _warped_textured()
        frames = {
            "evd-0": _camera_frame(base, h),
            "evd-1": _camera_frame(np.roll(base, -30, axis=0), h),
            "evd-2": _warped_textured(seed=99),  # 完全不同画面（追踪失败）
        }
        runtime = _FakeRuntimeForMeasure(tmp_path, frames)
        result = _run_measure(runtime, {
            "op": "measure", "evidence_ids": ["evd-0", "evd-1", "evd-2"],
            "roi": {"x": 100, "y": 260, "width": 1000, "height": 200}})
        assert result["status"] == "unknown"
        assert result["reason_code"]


class TestMeasureScrollRoiFrames:
    def test_screen_norm_roi_measures_screen_dy(self, tmp_path):
        """P1-3：屏幕归一化 ROI（程序坐标域）直接解析，调用方无需反解单应。"""
        h = np.array(H_AXIS_SWAP)
        base = _warped_textured()
        frames = {
            "evd-0": _camera_frame(base, h),
            "evd-1": _camera_frame(np.roll(base, -30, axis=0), h),
        }
        runtime = _FakeRuntimeForMeasure(tmp_path, frames)
        result = _run_measure(runtime, {
            "op": "measure", "evidence_ids": ["evd-0", "evd-1"],
            "roi": {"x": 0.1, "y": 0.2, "width": 0.8, "height": 0.3},
            "roi_frame": "screen_norm"})
        assert result["status"] == "displaced", result
        assert result["dy_px"] == pytest.approx(-30, abs=8)

    def test_chain_path_reports_unmeasured_ocr_rows_as_none(self, tmp_path):
        """R2 N11：帧链路径不做 OCR 互证，matched_ocr_rows 恒 0 是误报；应为 None。"""
        h = np.array(H_AXIS_SWAP)
        base = _warped_textured()
        frames = {f"evd-{i}": _camera_frame(np.roll(base, -30 * i, axis=0), h)
                  for i in range(4)}
        runtime = _FakeRuntimeForMeasure(tmp_path, frames)
        result = _run_measure(runtime, {
            "op": "measure",
            "evidence_ids": [f"evd-{i}" for i in range(4)],
            "roi": {"x": 100, "y": 260, "width": 1000, "height": 200}})
        assert result["status"] == "displaced", result
        assert result["matched_ocr_rows"] is None
        assert result["chain_frames"] == 4

    def test_invalid_roi_frame_rejected(self, tmp_path):
        from runtime.errors import ApiError
        h = np.array(H_AXIS_SWAP)
        base = _warped_textured()
        frames = {"evd-0": _camera_frame(base, h),
                  "evd-1": _camera_frame(base, h)}
        runtime = _FakeRuntimeForMeasure(tmp_path, frames)
        with pytest.raises(ApiError):
            _run_measure(runtime, {
                "op": "measure", "evidence_ids": ["evd-0", "evd-1"],
                "roi": {"x": 0, "y": 0, "width": 1, "height": 1},
                "roi_frame": "camera_norm"})


class TestScrollSeparatesPhonePose:
    @staticmethod
    def frames(*, scroll=0, displacement=12):
        import cv2
        base = _warped_textured()
        base[:12] = base[-12:] = 200
        base[:, :12] = base[:, -12:] = 200
        frames = {}
        # The production measurement uses a frame chain for large scrolls.
        for index in range(3):
            content = np.roll(base, round(scroll * index / 2), axis=0)
            content[:12] = content[-12:] = 200
            content[:, :12] = content[:, -12:] = 200
            frame = _camera_frame(content, np.array(H_AXIS_SWAP))
            frames[f"evd-{index}"] = cv2.warpAffine(
                frame, np.float32([[1, 0, displacement * index / 2], [0, 1, 0]]),
                (CAM_W, CAM_H))
        return frames

    @pytest.mark.parametrize("scroll,status", [(0, "stationary"), (-60, "displaced")])
    def test_phone_translation_is_removed_before_scroll_measurement(self, tmp_path, scroll, status):
        runtime = _FakeRuntimeForMeasure(tmp_path, self.frames(scroll=scroll))
        result = _run_measure(runtime, {
            "op": "measure", "evidence_ids": list(runtime.evidence.frames),
            "roi_frame": "screen_norm",
            "roi": {"x": .1, "y": .2, "width": .8, "height": .3}})
        assert result["status"] == status, result
        assert result["dy_px"] == pytest.approx(scroll, abs=4)

    @pytest.mark.parametrize("displacement", [40, 100])
    def test_uncompensatable_phone_motion_never_certifies_scroll(self, tmp_path, displacement):
        runtime = _FakeRuntimeForMeasure(
            tmp_path, self.frames(scroll=-60, displacement=displacement))
        result = _run_measure(runtime, {
            "op": "measure", "evidence_ids": list(runtime.evidence.frames),
            "roi_frame": "screen_norm",
            "roi": {"x": .1, "y": .2, "width": .8, "height": .3}})
        assert result["status"] == "unknown"
        assert result["reason_code"] == "screen_pose_unavailable"

    @pytest.mark.parametrize("scroll,status", [(0, "stationary"), (-30, "displaced")])
    def test_two_frame_measurement_also_removes_phone_motion(self, tmp_path, scroll, status):
        frames = self.frames(scroll=2 * scroll, displacement=24)
        frames.pop("evd-2")
        runtime = _FakeRuntimeForMeasure(tmp_path, frames)
        result = _run_measure(runtime, {
            "op": "measure", "evidence_ids": list(frames),
            "roi_frame": "screen_norm",
            "roi": {"x": .1, "y": .2, "width": .8, "height": .3}})
        assert result["status"] == status, result
        assert result["dy_px"] == pytest.approx(scroll, abs=4)


WRONG_DIRECTION_MEASURE = {
    "status": "displaced", "dx_px": 0.0, "dy_px": 320.5,
    "inlier_ratio": 0.9, "valid_points": 30, "matched_ocr_rows": 3,
    "mad_dy_px": 1.2, "reason_code": None,
    "gesture_rules_version": "gesture-rules.v1"}


def _run_plugin_step(module, ctx):
    plugin = module.Plugin()
    plugin.manifest = {"plugin_id": "wx-wechat-adapter"}
    return asyncio.run(plugin._step_auto_navigate(ctx))


@needs_wx_example
class TestSearchDirectionValidation:
    def test_wrong_direction_not_verified(self, tmp_path, monkeypatch):
        from drivers import calibration_bundle as cb
        from tests.test_screen_gesture_driver import gesture_bundle
        seed_navigation(tmp_path)
        module = load_module("wx_plugin_review_dir", PLUGIN_DIR)
        monkeypatch.setattr(module, "AUTO_NAV_DATA_ROOT", tmp_path / "data")
        ctx = AutoNavContext(auto_nav_payload("search_next"),
                             measure=dict(WRONG_DIRECTION_MEASURE))
        result = _run_plugin_step(module, ctx)
        assert result["outcome"] == "failed"
        assert result["detail"]["reason_code"] == "direction_mismatch"
        assert "相反" in result["detail"]["message"]


@needs_wx_example
class TestProgramRoiDomain:
    def test_program_uses_screen_domain_roi(self, tmp_path, monkeypatch):
        from adapters.uinav import automatic_navigation as nav
        from drivers import calibration_bundle as cb
        from tests.test_screen_gesture_driver import gesture_bundle
        seed_navigation(tmp_path)
        module = load_module("wx_plugin_review_roi", PLUGIN_DIR)
        monkeypatch.setattr(module, "AUTO_NAV_DATA_ROOT", tmp_path / "data")
        captured = {}

        class CapturingContext(AutoNavContext):
            async def call_capability(self, capability_id, params=None):
                if capability_id == "arm.execute_gesture_program":
                    captured.update(params or {})
                    return {"ok": True, "motion_verified": True,
                            "controller_status": "completed", "plan_id": "plan-1"}
                return await super().call_capability(capability_id, params)


        # 相机域布局 ROI：横向长条（真 rig 中列表横躺在相机画面）
        roi_camera = {"x": 140, "y": 280, "width": 1000, "height": 160}
        monkeypatch.setattr(
            nav, "detect_chat_list_layout",
            lambda boxes, w, hh, *, defaults: {
                "matched": True, "language": "zh",
                "roi": dict(roi_camera), "tabs": []})
        ctx = CapturingContext(auto_nav_payload("search_next"))
        ctx._observe = list_observation()
        _run_plugin_step(module, ctx)
        program = captured.get("program")
        assert program, "未捕获执行程序"
        points = program["strokes"][0]["points"]
        # 屏幕域：相机 y 280..440 → 屏幕 x ≈369..726（归一化 0.34..0.67）
        for point in points:
            assert 0.30 < point["x"] < 0.70, f"x={point['x']} 不在屏幕域列表横向带"
        # 屏幕 y 跨度≈全屏（相机 x 140..1140 → 屏幕 y 42..2292）：
        # 程序 y 起终应约 0.74 / 0.31（相机域直除只会给 0.56 / 0.46）
        assert points[0]["y"] > 0.65, f"y_start={points[0]['y']}（相机域误用迹象）"
        assert points[1]["y"] < 0.40, f"y_end={points[1]['y']}（相机域误用迹象）"


@needs_wx_example
class TestTemplateAnchorBridge:
    def test_anchors_from_writer_schema_with_calibrated_patch(self, tmp_path,
                                                              monkeypatch):
        """P1-13/NAV-08：消费登记时保存的校准域裁剪及真实证据摘要。

        登记端（PUT navigation-templates）落盘 anchor_evidence：region 是
        校准域像素矩形 + 登记帧证据；旧读取端找 anchor_regions 恒为空，
        模板验证永远失败。
        """
        import cv2
        from drivers import calibration_bundle as cb
        from tests.test_screen_gesture_driver import gesture_bundle

        from tests.test_automatic_gesture_api import _seed_calibration
        bundle = _seed_calibration(tmp_path)
        module = load_module("wx_plugin_anchor_dir", PLUGIN_DIR)
        monkeypatch.setattr(module, "AUTO_NAV_DATA_ROOT", tmp_path / "data")
        monkeypatch.setattr(module, "AUTO_NAV_EVIDENCE_ROOT", tmp_path / "evidence")

        # 登记帧：屏幕域画面逆映射回相机域（与真机采集同构）；
        # 在锚点区域画高对比标记块（合成纹理局部可能低对比）
        screen_img = _warped_textured()
        cv2.rectangle(screen_img, (220, 720), (340, 800), 255, -1)
        cv2.rectangle(screen_img, (240, 740), (320, 780), 0, -1)
        matrix = np.asarray(
            bundle["camera"]["cameraToScreen"]["homography"], dtype=float)
        camera_img = cv2.warpPerspective(
            screen_img, np.linalg.inv(matrix),
            (int(bundle["frame"]["width"]), int(bundle["frame"]["height"])))
        from drivers.screen_gestures import binding_snapshot
        from tests.test_automatic_gesture_api import _template_fixture
        template = _template_fixture(
            binding_snapshot(bundle), evidence_root=tmp_path / "evidence",
            source=camera_img, screen_plane=cv2.warpPerspective(camera_img, matrix, (1080, 2320)),
            regions=[{"left_px": 200, "top_px": 700, "width_px": 160, "height_px": 120}])
        anchors = module._auto_nav_anchors(template, "arm-01", data_root=tmp_path / "data",
                                            evidence_root=tmp_path / "evidence")
        assert len(anchors) == 1, "按登记端结构必须解析出锚点"
        anchor = anchors[0]
        assert (anchor["left_px"], anchor["top_px"],
                anchor["width_px"], anchor["height_px"]) == (200, 700, 160, 120)
        patch = anchor.get("template")
        assert isinstance(patch, np.ndarray) and patch.shape == (120, 160)
        expected = screen_img[700:820, 200:360]
        # 两次重采样只在锐利边缘产生模糊，用相关性约束内容一致
        assert float(np.corrcoef(patch.ravel().astype(float),
                                 expected.ravel().astype(float))[0, 1]) > 0.98

        # 闭环：同一画面的校准域帧应匹配通过（home/return_top 模板验证）
        from adapters.uinav import automatic_navigation as nav
        result = nav.verify_top_template(
            anchors, screen_img, defaults=nav.load_defaults())
        assert result["matched"], result


@needs_wx_example
class TestCalibrationPlaneGuard:
    def test_corrupted_homography_returns_none_not_raise(self, tmp_path, monkeypatch):
        """R2 N5：损坏档案（单应非 3x3）走契约路径返回 None，不得抛未归类异常。"""
        from drivers import calibration_bundle as cb
        from tests.test_screen_gesture_driver import gesture_bundle

        bundle = gesture_bundle()
        bundle["camera"]["cameraToScreen"]["homography"] = [1, 2, 3]  # 非 9 元素
        cb.save_bundle(bundle, tmp_path / "data")
        module = load_module("wx_plugin_plane_dir", PLUGIN_DIR)
        monkeypatch.setattr(module, "AUTO_NAV_DATA_ROOT", tmp_path / "data")
        assert module._auto_nav_calibration_plane("arm-01") is None


class TestMeasureRoiContract:
    def test_missing_roi_returns_4xx_not_keyerror(self, tmp_path):
        """R2 N6：measure 缺 roi 键应 invalid_request 4xx，而非 KeyError 500。"""
        from runtime.errors import ApiError

        h = np.array(H_AXIS_SWAP)
        base = _warped_textured()
        frames = {"evd-0": _camera_frame(base, h),
                  "evd-1": _camera_frame(base, h)}
        runtime = _FakeRuntimeForMeasure(tmp_path, frames)
        with pytest.raises(ApiError) as exc:
            _run_measure(runtime, {"op": "measure",
                                   "evidence_ids": ["evd-0", "evd-1"]})
        assert exc.value.http == 400
        assert exc.value.key == "invalid_request"
