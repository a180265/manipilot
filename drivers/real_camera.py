"""真机摄像头驱动：Mac mini UVC Camera（AVFoundation 采集）。

职责（现场文档）：
- 确认机械臂实际是否移动（运动前后帧差验证，MPos 不是物理证明）；
- 画面质量检查（清晰/等待画面恢复/过曝/冻结/异常，全部中文映射）；
- 界面状态识别：仅在已登记参考画面的前提下给出匹配结果，
  未标定时必须回报"未知 + 需人工确认"，不得猜测界面状态。

测试可用 frame_provider 注入合成帧；真机走 OpenCV AVFoundation。
"""
from __future__ import annotations

import asyncio
import copy
import math
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from drivers.camera import CameraDriver
from drivers.camera_broker import (
    SOURCE_TIMING_PROVENANCE, quality_diagnostics, quality_failure_diagnostics,
)
from adapters.vision.ocr import (
    AppleVisionOcrEngine, OcrEngineError, OcrResult, OcrWord, find_text_bbox, ocr_roi_bounds,
)
from runtime.status import CAMERA_QUALITY, UI_STATE, display
from runtime.util import iso, new_id, sha256_bytes, utc_now


class ScreenRoiNotFound(RuntimeError):
    """No phone-shaped region can be verified in the current frame."""

    reason_code = "screen_roi_not_found"


class RealCameraDriver(CameraDriver):
    DRIVER_VERSION = "real-camera-uvc-1.0.0"

    # 质量阈值（可按现场画面调整）
    BLUR_VARIANCE_MIN = 15.0        # Laplacian 方差低于该值视为画质异常
    BRIGHTNESS_BLOCKED_MAX = 18.0   # 极暗 + 低纹理只证明黑帧，不证明遮挡
    BRIGHTNESS_OVER_MAX = 248.0     # 平均亮度过高 → 过曝
    MATCH_CONFIDENCE_MIN = 0.70     # 参考画面匹配最低置信度

    def __init__(
        self,
        device_id: str,
        *,
        camera_name: str = "UVC Camera",
        camera_index: int = 0,
        frame_provider=None,
        ocr_engine=None,
        screen_device_id=None,
        data_root=None,
    ):
        self.device_id = device_id
        self.camera_name = camera_name
        self.camera_index = camera_index
        self._frame_provider = frame_provider  # 测试注入：() -> np.ndarray(BGR)
        self._capture = None  # cv2.VideoCapture
        self._open = False
        self._last_sha: str | None = None
        self._last_grab_at: float | None = None
        self._references: dict[str, np.ndarray] = {}  # 登记的参考画面（灰度小图）
        self.ocr_engine = ocr_engine or AppleVisionOcrEngine(timeout_seconds=10.0)
        self._shared_broker = None
        self.screen_device_id = screen_device_id
        self.data_root = data_root
        self._layout_provider = None
        self._region_context_provider = None
        self._target_context_provider = None
        self._text_locations = {}

    def attach_target_context_provider(self, provider) -> None:
        self._target_context_provider = provider

    def _target_context(self):
        return copy.deepcopy(self._target_context_provider()) if self._target_context_provider else None

    def _remember_text_location(self, located, query, frame, layout, context, bundle):
        from drivers import calibration_bundle as cb

        if self._target_context() != context:
            raise OcrEngineError("文字识别期间设备绑定或粗框已变化",
                                 reason="screen_context_changed")
        if self.screen_device_id and cb.load_bundle(self.screen_device_id, self.data_root) != bundle:
            raise OcrEngineError("文字识别期间校准已变化", reason="screen_context_changed")
        if layout is not None:
            self._ensure_layout_current(layout)
        if not located.get("found"):
            return located
        location_id = new_id("location")
        result = {**located, "locationId": location_id,
                  "sourceCapturedAt": frame.get("source_captured_at"),
                  "sourceReceivedAt": frame.get("source_received_at")}
        # Bounded, process-local evidence. A restart cannot revive old coordinates.
        while len(self._text_locations) >= 64:
            self._text_locations.pop(next(iter(self._text_locations)))
        self._text_locations[location_id] = {
            "result": copy.deepcopy(result), "query": copy.deepcopy(query),
            "source": copy.deepcopy({k: v for k, v in frame.items()
                                      if k not in {"image", "image_bytes"}}),
            "layout": layout, "context": context,
            "bundle": copy.deepcopy(bundle), "created": time.monotonic(),
        }
        return result

    async def verify_text_target(self, ref, layout=None):
        """Re-observe the original point, never return replacement coordinates."""
        from drivers import calibration_bundle as cb
        from runtime.calibration_gate import _fresh_source_seq

        record = self._text_locations.get(ref.get("locationId"))
        if record is None:
            raise ValueError("动态文字目标缺少本摄像头的有效定位凭据，请重新定位")
        original = record["result"]

        def check_original():
            if any(ref.get(key) != value for key, value in original.items()):
                raise ValueError("动态文字定位结果被改写，禁止沿用原落点")
            if time.monotonic() - record["created"] > 2.0:
                raise ValueError("原文字定位已过期，请重新定位，不能由新帧续鲜")
            _fresh_source_seq(record["source"], None)
            if self._target_context() != record["context"]:
                raise ValueError("文字定位后的设备绑定或粗框已变化")
            if self.screen_device_id:
                if cb.load_bundle(self.screen_device_id, self.data_root) != record["bundle"]:
                    raise ValueError("文字定位后的校准已变化")
            if record["layout"] is not None:
                self._ensure_layout_current(record["layout"])

        check_original()
        if record["layout"] is not None:
            if layout is None or layout.state != record["layout"].state:
                raise ValueError("动态文字所在页面已变化")
            _fresh_source_seq(layout.source, record["source"]["source_frame_seq"])
            observed = await self._locate_layout_text(layout, **record["query"])
            latest_source = layout.source
        else:
            if layout is not None:
                raise ValueError("文字定位后已选模板发生变化")
            observed = await self.locate_text(**record["query"])
            fresh = self._text_locations.get(observed.get("locationId"))
            if fresh is None:
                raise ValueError("原文字目标已消失或不能唯一确认")
            latest_source = fresh["source"]
            _fresh_source_seq(latest_source, record["source"]["source_frame_seq"])
        for key in ("device_id", "camera_id", "device_unique_id", "binding_revision",
                    "source_generation_id", "source_clock_id", "broker_generation_id",
                    "camera_binding_id", "source_geometry_id", "width", "height"):
            if latest_source.get(key) != record["source"].get(key):
                raise ValueError("文字定位的画面来源或几何已变化")
        if (not observed.get("found") or observed.get("text") != original.get("text")
                or not np.allclose(observed.get("bbox"), original.get("bbox"), rtol=0, atol=2.0)):
            raise ValueError("原文字行已移动、消失或不能唯一确认，禁止点击旧落点")
        verified_at = time.monotonic()

        def final_check():
            check_original()
            _fresh_source_seq(latest_source, None)
            if time.monotonic() - verified_at > 1.0:
                raise ValueError("文字行接触前复核已过期")
            if layout is not None:
                self._ensure_layout_current(layout)

        final_check()
        return final_check

    def attach_layout_provider(self, provider) -> None:
        """Runtime owns layout storage; the camera only consumes its resolver."""
        self._layout_provider = provider

    def attach_region_context_provider(self, provider) -> None:
        """注入本台生效粗框上下文查询（phone_region_context.load_region_context）。

        返回 None=未安装粗框包旧设备（保持原全图路径）；新模式返回
        {"mode":"region",...}；缺框/损坏抛 RegionContextError（明确失败）。
        """
        self._region_context_provider = provider

    async def _selected_layout_frame(self, frame=None):
        if self._layout_provider is None or not self.screen_device_id:
            return None
        selected = await asyncio.to_thread(self._layout_provider, self.screen_device_id)
        if selected is None:
            return None
        from drivers import screen_proof
        fingerprint, bundle = screen_proof.binding(
            self.screen_device_id, self.data_root, allow_motion_ready=True)
        if frame is None:
            frame = await self._capture_for_operation()
        aligned, pose = self._aligned_screen_frame(
            frame, bundle, return_pose=True, search_region=selected.region)
        return await asyncio.to_thread(selected.prepare, aligned, frame, bundle, pose, fingerprint,
                                       ocr_engine=self.ocr_engine)

    def _ensure_layout_current(self, layout):
        from drivers import screen_proof
        _, bundle = screen_proof.binding(self.screen_device_id, self.data_root, allow_motion_ready=True)
        layout.ensure_current(bundle)

    def attach_broker(self, broker) -> None:
        """绑定设备级帧 broker；识别操作复用其唯一物理取帧入口。"""
        self._shared_broker = broker

    @property
    def driver_version(self) -> str:
        return self.DRIVER_VERSION

    def is_open(self) -> bool:
        return self._open

    # ---------- 打开/关闭 ----------
    async def open(self) -> dict:
        if self._frame_provider is None:
            def _open_capture():
                cap = cv2.VideoCapture(self.camera_index, cv2.CAP_AVFOUNDATION)
                if not cap.isOpened():
                    raise RuntimeError(f"无法打开摄像头：{self.camera_name}（索引 {self.camera_index}）")
                # 预热两帧：AVFoundation 首帧常为黑帧
                for _ in range(2):
                    cap.read()
                return cap

            self._capture = await asyncio.to_thread(_open_capture)
        self._open = True
        info = await self.capture()
        return {
            "camera_id": self.device_id,
            "camera_name": self.camera_name,
            "driver_version": self.DRIVER_VERSION,
            "width": info["width"],
            "height": info["height"],
        }

    async def close(self) -> dict:
        if self._capture is not None:
            await asyncio.to_thread(self._capture.release)
            self._capture = None
        self._open = False
        return {"closed": True}

    # ---------- 采集 ----------
    def _grab_frame(self) -> np.ndarray:
        if self._frame_provider is not None:
            frame = self._frame_provider()
            if frame is None:
                raise RuntimeError("摄像头无画面")
            return frame
        if self._capture is None:
            raise RuntimeError("摄像头未打开")
        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise RuntimeError("摄像头采集失败")
        return frame

    def last_source_info(self) -> dict | None:
        """最近一次取帧的分路来源元数据（无帧源时返回 None）。

        供设备状态呈现帧来源/最后帧时间/绑定版本等摄像头事实；
        只读快照，不发起新采集。
        """
        source = self._frame_provider
        if source is None:
            return None
        info = {
            "camera_id": getattr(source, "last_camera_id", None),
            "binding_revision": getattr(source, "last_binding_revision", None),
            "frame_seq": getattr(source, "last_frame_seq", None),
            "captured_at": getattr(source, "last_captured_at", None),
            "received_at": getattr(source, "last_received_at", None),
            "source": getattr(source, "last_frame_source", None),
            "legacy_mode": getattr(source, "legacy_mode", None),
        }
        return info if any(value is not None for value in info.values()) else None

    def capture_sync(self) -> dict:
        """同步采集一帧（供证据归档与 MJPEG 流共用）。"""
        try:
            frame = self._grab_frame()
        except Exception as exc:
            raise RuntimeError(f"摄像头采集异常：{exc}") from exc
        ok, png = cv2.imencode(".png", frame)
        if not ok:
            raise RuntimeError("画面编码失败")
        image = png.tobytes()
        now = utc_now()
        sha = sha256_bytes(image)
        # 冻结判定：哈希一致且流确实在推进（桥来源 seq 推进的新帧若字节仍一致，
        # 属静态场景而非画面冻结；桥端无新帧才判冻结）。无 seq 标记的来源
        # （直连/测试注入）维持哈希判定。
        stream_alive = getattr(self._frame_provider, "stream_alive", None)
        same_as_last = self._last_sha is not None and sha == self._last_sha
        if stream_alive is None:
            frozen = same_as_last
        else:
            frozen = same_as_last and not stream_alive
        frame_info = {
            "frame_ref": f"frame-{new_id('f')}",
            "captured_at": iso(now),
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
            "sha256": sha,
            "image": image,
            "frozen": frozen,
        }
        source = self._frame_provider
        if source is not None:
            source_meta = {
                "camera_id": getattr(source, "last_camera_id", None),
                "device_id": getattr(source, "last_device_id", None),
                "device_unique_id": getattr(source, "last_device_unique_id", None),
                "binding_revision": getattr(source, "last_binding_revision", None),
                "source_frame_seq": getattr(source, "last_frame_seq", None),
                "source_captured_at": getattr(source, "last_captured_at", None),
                "source_received_at": getattr(source, "last_received_at", None),
                "source_legacy_mode": getattr(source, "legacy_mode", None),
                # R2-08（camera-binding 附件 §5）：冻结帧包 v2 时钟/代际/源序号/
                # 误差/绑定实例/几何逐层透传；缺失保持缺失，不补默认值，
                # 动作资格由 camera_broker.frame_chain_identity 判定。
                "camera_binding_id": getattr(source, "last_camera_binding_id", None),
                "source_geometry_id": getattr(source, "last_source_geometry_id", None),
                "source_generation_id": getattr(source, "last_source_generation_id", None),
                "source_capture_seq": getattr(source, "last_source_capture_seq", None),
                "source_clock_id": getattr(source, "last_source_clock_id", None),
                "source_captured_monotonic_ns": getattr(
                    source, "last_source_captured_monotonic_ns", None),
                "source_capture_uncertainty_ms": getattr(
                    source, "last_source_capture_uncertainty_ms", None),
                "source_timing_provenance": getattr(
                    source, "last_source_timing_provenance", None),
            }
            frame_info.update({key: value for key, value in source_meta.items()
                               if value is not None})
            # 采样时钟已证明：captured_at 用真实采样时刻投影；读取时刻仅诊断。
            if frame_info.get("source_timing_provenance") == SOURCE_TIMING_PROVENANCE \
                    and frame_info.get("source_captured_at"):
                frame_info["captured_at"] = frame_info["source_captured_at"]
        self._last_sha = sha
        self._last_grab_at = time.monotonic()
        return frame_info

    async def capture(self) -> dict:
        read = asyncio.create_task(asyncio.to_thread(self.capture_sync))
        try:
            frame = await asyncio.shield(read)
        except asyncio.CancelledError:
            # Cancelling to_thread does not stop the reader. Drain it before
            # another capture or camera close can use the same frame source.
            while not read.done():
                try:
                    await asyncio.shield(read)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not read.cancelled():
                read.exception()
            raise
        return {k: v for k, v in frame.items() if k != "image"} | {"image_bytes": frame["image"]}

    # ---------- 画面质量 ----------
    @staticmethod
    def _analyze(image_bytes: bytes, *, expected_corners=None, tolerance_px: float = 8.0,
                 require_phone: bool = False, search_region: dict | None = None) -> dict:
        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        if frame is None:
            raise RuntimeError("画面解码失败")
        h, w = frame.shape
        if search_region is not None:
            # §11.10.2/S1-07：质量前置约束在本台生效粗框内——裁剪后平移预测
            # 四角，仅走预测路径（自由全图极性扫描被 expected 非 None 抑制）；
            # 指标仍来自框内检测到的屏幕 ROI，框外屏幕不参与。
            from runtime.layout_matching import _pixel_rect
            from runtime.phone_region_context import RegionContextError
            try:
                left, top, right, bottom = _pixel_rect(
                    search_region["rect"], w, h)
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"当前生效粗框无效：{exc}") from exc
            if expected_corners is None:
                raise RuntimeError("粗框约束的质量判定必须携带校准预测四角")
            frame = frame[top:bottom, left:right]
            expected_corners = [
                [float(x) - left, float(y) - top]
                for x, y in np.asarray(expected_corners, dtype=float)]
            h, w = frame.shape
        expected = None
        if expected_corners is not None:
            try:
                expected = np.asarray(expected_corners, dtype=np.float32)
                if (expected.shape != (4, 2) or not np.isfinite(expected).all()
                        or cv2.contourArea(expected) < w * h * .05
                        or np.any(expected < 0) or np.any(expected[:, 0] >= w)
                        or np.any(expected[:, 1] >= h)
                        or not cv2.isContourConvex(expected)
                        or not math.isfinite(tolerance_px) or tolerance_px <= 0):
                    raise ValueError("invalid screen geometry")
            except (TypeError, ValueError, cv2.error) as exc:
                raise RuntimeError("校准屏幕区域无效") from exc

        # Both polarities matter: a lit display can be brighter than its
        # surroundings, while a sleeping display can be the dark rectangle.
        from drivers.screen_edge import ScreenEdgeError, detect_screen_quad, locate_screen_quad

        candidates = []
        if expected is not None:
            try:
                located = locate_screen_quad(
                    cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR), expected.tolist(),
                    tolerance_px=max(tolerance_px, 32), require_fittable=True)
                candidates.append((located["area"], np.asarray(located["corners"], np.float32)))
            except ScreenEdgeError:
                # §7.1 校准预测定位回退（2026-09-29 开灯常态）：阈值区域法在
                # 亮桌面/暗屏下失效时，用梯度直线法在预测四角附近定位。
                from drivers.screen_edge import locate_quad_by_gradient_lines

                try:
                    fallback = locate_quad_by_gradient_lines(
                        cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR), expected.tolist(),
                        tolerance_px=max(tolerance_px, 32))
                    quad_arr = np.asarray(fallback["corners"], np.float32)
                    fallback_area = float(cv2.contourArea(
                        quad_arr.astype(np.int32))) if quad_arr.ndim == 2 else 0.0
                    candidates.append((fallback_area, quad_arr))
                except ScreenEdgeError:
                    pass
        for polarity in (() if expected is not None else (frame, cv2.bitwise_not(frame))):
            if min(w, h) >= 100:
                try:
                    found = detect_screen_quad(cv2.cvtColor(polarity, cv2.COLOR_GRAY2BGR))
                except ScreenEdgeError:
                    continue
                found_quads = [(found["area"], np.asarray(found["corners"], np.float32))]
            else:
                blurred = cv2.GaussianBlur(polarity, (5, 5), 0)
                _, threshold = cv2.threshold(
                    blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                threshold = cv2.morphologyEx(
                    threshold, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
                contours, _ = cv2.findContours(
                    threshold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                found_quads = [
                    (cv2.contourArea(contour), cv2.boxPoints(cv2.minAreaRect(contour)))
                    for contour in contours
                ]
            for area, quad in found_quads:
                if not w * h * .05 <= area <= w * h * .90:
                    continue
                if (np.any(quad[:, 0] < 2) or np.any(quad[:, 0] > w - 3)
                        or np.any(quad[:, 1] < 2) or np.any(quad[:, 1] > h - 3)):
                    continue
                if expected is not None:
                    # Compare as sets because contour traversal may start at any corner.
                    if max(float(np.min(np.linalg.norm(quad - p, axis=1)))
                           for p in expected) > tolerance_px:
                        continue
                candidates.append((area, quad))
        if not candidates:
            # A featureless black frame can only mean passive waiting; wake
            # requires a located phone before any motion can begin.
            if not require_phone:
                if search_region is not None:
                    # S1-07：粗框约束下的等待判定按校准预测屏幕域采样（框内
                    # 背景不参与；框外亮屏不构成干扰）。暗屏=waiting；
                    # 亮屏但不可定位=异常（明确失败，不伪装 waiting）。
                    mask = np.zeros((h, w), np.uint8)
                    cv2.fillConvexPoly(mask, expected.astype(np.int32), 255)
                    inset = max(1, int(min(h, w) * .06))
                    mask = cv2.erode(mask, np.ones((2 * inset + 1, 2 * inset + 1),
                                                   np.uint8))
                    pixels = frame[mask != 0]
                    if pixels.size:
                        brightness = float(pixels.mean())
                        texture = float(pixels.std())
                        if (brightness <= RealCameraDriver.BRIGHTNESS_BLOCKED_MAX
                                and texture < 8):
                            return {"blur_variance": 0.0,
                                    "brightness": brightness, "texture": texture}
                elif not require_phone:
                    brightness, texture = float(frame.mean()), float(frame.std())
                    if (brightness <= RealCameraDriver.BRIGHTNESS_BLOCKED_MAX
                            and texture < 8):
                        return {"blur_variance": 0.0, "brightness": brightness,
                                "texture": texture}
            raise ScreenRoiNotFound("画面中无法定位手机屏幕")
        _, quad = max(candidates, key=lambda item: item[0])
        roi_mask = np.zeros((h, w), np.uint8)
        cv2.fillConvexPoly(roi_mask, quad.astype(np.int32), 255)
        x, y, width, height = cv2.boundingRect(quad.astype(np.int32))
        inset = max(1, int(min(width, height) * .04))
        roi_mask = cv2.erode(roi_mask, np.ones((2 * inset + 1, 2 * inset + 1), np.uint8))
        pixels = frame[roi_mask != 0]
        if pixels.size < w * h * .02:
            raise ScreenRoiNotFound("手机屏幕区域过小")
        # Preserve the existing Laplacian threshold scale at a fixed width.
        cropped = frame[y:y + height, x:x + width]
        cropped_mask = roi_mask[y:y + height, x:x + width]
        small = cv2.resize(cropped, (320, max(1, int(320 * height / width))))
        small_mask = cv2.resize(cropped_mask, (320, small.shape[0]),
                                interpolation=cv2.INTER_NEAREST)
        inner_mask = cv2.erode(small_mask, np.ones((5, 5), np.uint8)) != 0
        blur_variance = float(cv2.Laplacian(small, cv2.CV_64F)[inner_mask].var())
        brightness = float(pixels.mean())
        texture = float(pixels.std())
        return {"blur_variance": blur_variance, "brightness": brightness, "texture": texture}

    def _quality_geometry(self, frame: dict) -> tuple[list | None, float,
                                                      dict | None]:
        """返回 (校准预测四角, 容差, 生效粗框 region 记录或 None)。

        新模式（已安装粗框包）必须有生效粗框且与校准一致，缺失/不一致抛
        RegionContextError——由调用方按明确失败处理，不回全图。
        """
        if not self.screen_device_id:
            return None, 8.0, None
        from drivers import calibration_bundle as cb

        bundle = cb.load_bundle(self.screen_device_id, self.data_root)
        if not bundle or bundle.get("status") not in {"ready", cb.MOTION_READY_STATUS}:
            return None, 8.0, None
        binding_fields = (("deviceId", "device_id"), ("cameraId", "camera_id"),
                          ("cameraDeviceUniqueId", "device_unique_id"),
                          ("bindingRevision", "binding_revision"))
        saved_frame = bundle.get("frame") or {}
        if (saved_frame.get("width") != frame["width"]
                or saved_frame.get("height") != frame["height"]
                or any(frame.get(source) is not None and frame[source] != bundle.get(saved)
                       for saved, source in binding_fields)):
            raise RuntimeError("画面与校准绑定不符")
        edge = (bundle.get("screen") or {}).get("edgeModel") or {}
        if not edge.get("corners"):
            raise RuntimeError("校准缺少屏幕区域")
        region = None
        provider = self._region_context_provider
        if provider is not None:
            from runtime.phone_region_context import (
                RegionContextError, context_matches_bundle, load_region_context)
            try:
                ctx = provider(self.screen_device_id)
                if ctx is not None:
                    context_matches_bundle(ctx, bundle)
                    region = ctx["region"]
            except RegionContextError as exc:
                # 新模式缺框/损坏=明确失败（沿用 RuntimeError 通道给出精确
                # 原因），不回全图、不按普通画质异常降级。
                raise RuntimeError(f"生效粗框不可用：{exc.message}") from exc
        return edge["corners"], edge.get("tolerancePx") or 8.0, region

    def _aligned_screen_frame(self, frame: dict, bundle: dict,
                              *, profile: dict | None = None, return_pose=False,
                              search_region=None):
        """Accept only a fresh, bound phone observation in calibrated coordinates."""
        from drivers import screen_pose, screen_proof
        from runtime.calibration_gate import _fresh_source_seq, _quad_of

        expected_source = {
            "device_id": self.screen_device_id, "camera_id": bundle.get("cameraId"),
            "device_unique_id": bundle.get("cameraDeviceUniqueId"),
            "binding_revision": bundle.get("bindingRevision"),
        }
        if (self.device_id != bundle.get("cameraId")
                or any(frame.get(key) != expected for key, expected in expected_source.items())
                or frame.get("source_legacy_mode")
                or (frame.get("width"), frame.get("height")) != (
                    bundle["frame"]["width"], bundle["frame"]["height"])):
            raise ValueError("当前帧来源、绑定版本或尺寸与校准档案不一致")
        _fresh_source_seq(frame, None)
        if frame.get("frozen"):
            raise ValueError("手机画面冻结，等待推流恢复")
        image_bytes = frame.get("image") or frame.get("image_bytes")
        saved = bundle["screen"]["edgeModel"]["corners"]
        if search_region is not None:
            from runtime.layout_geometry import LayoutGeometryError, locate_in_region
            try:
                pose = locate_in_region(screen_proof.decode(image_bytes), search_region, saved)
            except LayoutGeometryError as exc:
                raise ValueError(str(exc)) from exc
        else:
            live = _quad_of(image_bytes, saved)
            if live is None:
                raise ValueError("当前画面未定位到手机屏幕")
            pose = screen_pose.fit_pose(saved, live)
        if profile:
            regions = list(profile.get("regions", {}).values())
            regions += [ref["region"] for ref in profile.get("references", {}).values()]
            polygon = np.asarray(saved, dtype=np.float32)
            for region in regions:
                x1, y1, x2, y2 = screen_proof.bounds(
                    region, frame["width"], frame["height"])
                if any(cv2.pointPolygonTest(polygon, (float(x), float(y)), False) < 0
                       for x in (x1, x2) for y in (y1, y2)):
                    raise ValueError("页面登记区域超出手机屏幕，请重新登记")
        aligned = screen_pose.align_screen(screen_proof.decode(image_bytes), saved, pose)
        return (aligned, pose) if return_pose else aligned

    @classmethod
    def _classify(cls, metrics: dict, frozen: bool) -> str:
        if frozen:
            return "frozen"
        if metrics["brightness"] <= cls.BRIGHTNESS_BLOCKED_MAX and metrics["texture"] < 8.0:
            return "waiting"
        if metrics["brightness"] >= cls.BRIGHTNESS_OVER_MAX:
            return "overexposed"
        if metrics["blur_variance"] < cls.BLUR_VARIANCE_MIN:
            return "abnormal"
        return "clear"

    async def check_quality(self) -> dict:
        capturing = True
        try:
            frame = await self._capture_for_operation()
            capturing = False
            corners, tolerance, search_region = self._quality_geometry(frame)
            metrics = self._analyze(frame["image"], expected_corners=corners,
                                    tolerance_px=tolerance,
                                        search_region=search_region)
            if frame["frozen"] and (self._last_grab_at is None or time.monotonic() - self._last_grab_at < 0.7):
                # 采样间隔小于推帧间隔时可能拿到同一帧：等一个间隔后复核，
                # 只有间隔足够长仍然同哈希才判"画面冻结"。
                await asyncio.sleep(0.7)
                capturing = True
                frame = await self._capture_for_operation()
                capturing = False
                corners, tolerance, search_region = self._quality_geometry(frame)
                metrics = self._analyze(frame["image"], expected_corners=corners,
                                        tolerance_px=tolerance,
                                        search_region=search_region)
        except Exception as exc:
            return {"quality": "abnormal", "quality_display": "画面异常",
                    **quality_failure_diagnostics(exc, capture_failed=capturing)}
        quality = self._classify(metrics, frame["frozen"])
        return {
            "quality": quality,
            "quality_display": display(CAMERA_QUALITY, quality),
            **quality_diagnostics({
                "quality": quality, "metrics": metrics,
                "reason_code": "camera_detail_insufficient" if quality == "abnormal" else None,
            }),
            "frozen": frame["frozen"],
        }

    # ---------- 界面识别（未标定时必须保守回报未知） ----------
    def register_reference(self, name: str, image_bytes: bytes) -> dict:
        """登记参考画面（由人工确认后调用并审计；例如微信主界面参考帧）。"""
        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        if frame is None:
            raise ValueError("参考画面解码失败")
        self._references[name] = cv2.resize(frame, (240, 240))
        return {"registered": name, "references": sorted(self._references)}

    def _match(self, image_bytes: bytes) -> tuple[str | None, float]:
        if not self._references:
            return None, 0.0
        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        if frame is None:
            return None, 0.0
        candidate = cv2.resize(frame, (240, 240))
        best_name, best_score = None, 0.0
        for name, reference in self._references.items():
            diff = float(np.mean(cv2.absdiff(candidate, reference)))
            score = max(0.0, 1.0 - diff / 128.0)
            if score > best_score:
                best_name, best_score = name, score
        return best_name, best_score

    async def identify_ui_state(self) -> dict:
        observed_at = iso(utc_now())
        try:
            frame = await self._capture_for_operation()
        except Exception as exc:
            return {
                "ui_state": "unknown",
                "ui_state_display": display(UI_STATE, "unknown"),
                "confidence": 0.0,
                "observed_at": observed_at,
                "note": f"画面不可用：{exc}",
            }
        try:
            corners, tolerance, search_region = self._quality_geometry(frame)
            quality = self._classify(
                self._analyze(frame["image"], expected_corners=corners,
                              tolerance_px=tolerance,
                              search_region=search_region),
                bool(frame.get("frozen")))
        except (ScreenRoiNotFound, RuntimeError, ValueError, TypeError, cv2.error) as exc:
            return {"ui_state": "unknown", "ui_state_display": "无法确认", "confidence": 0.0,
                    "observed_at": observed_at, "note": f"画面不可用：{exc}"}
        if quality == "waiting":
            return {"ui_state": "unknown", "ui_state_display": "未知", "confidence": 0.0,
                    "observed_at": observed_at, "note": "等待画面恢复，暂不识别页面"}
        if self.screen_device_id:
            from drivers import screen_proof
            try:
                layout = await self._selected_layout_frame(frame)
                if layout is not None:
                    self._ensure_layout_current(layout)
                    return {**layout.ui, "observed_at": observed_at}
                profile = screen_proof.load_profile(self.screen_device_id, self.data_root)
                if profile:
                    _, bundle = screen_proof.binding(self.screen_device_id, self.data_root)
                    image = self._aligned_screen_frame(frame, bundle, profile=profile)
                    return {**screen_proof.identify(image, profile),
                            "observed_at": observed_at}
                return {"ui_state": "unknown", "ui_state_display": "无法确认",
                        "confidence": 0.0, "observed_at": observed_at,
                        "note": "尚未登记手机屏幕页面档案"}
            except (ValueError, OSError, KeyError) as exc:
                return {"ui_state": "unknown", "ui_state_display": "无法确认", "confidence": 0,
                        "observed_at": observed_at, "note": str(exc)}
        name, score = self._match(frame["image"])
        if name is None:
            return {
                "ui_state": "unknown",
                "ui_state_display": display(UI_STATE, "unknown"),
                "confidence": 0.0,
                "observed_at": observed_at,
                "note": "真机画面识别未登记参考画面，需人工标定确认",
            }
        if score < self.MATCH_CONFIDENCE_MIN:
            return {
                "ui_state": "other",
                "ui_state_display": display(UI_STATE, "other"),
                "confidence": round(score, 3),
                "observed_at": observed_at,
                "note": "与已登记参考画面差异较大，需人工确认",
            }
        return {
            "ui_state": name,
            "ui_state_display": display(UI_STATE, name),
            "confidence": round(score, 3),
            "observed_at": observed_at,
        }

    async def identify_account_and_conversation(self) -> dict:
        """仅提供屏幕 OCR 证据；账号和业务身份由 WX 判断。"""
        try:
            layout = await self._selected_layout_frame()
            if layout is not None:
                from drivers import screen_proof
                region = layout.profile["regions"].get("title")
                if not region:
                    raise ValueError("当前页面没有可核对的标题区域")
                result = await asyncio.to_thread(
                    screen_proof.recognize_patch, self.ocr_engine,
                    screen_proof.crop(layout.image, region), "conversation")
                title = result["text"].strip()
                if len(title.splitlines()) != 1:
                    raise ValueError("标题区域为空或存在多行歧义")
                self._ensure_layout_current(layout)
                return {"account_clue": None, "conversation_clue": title,
                        "confidence": result["confidence"], "ui_state": layout.ui["ui_state"],
                        "ui_state_display": layout.ui["ui_state_display"], "ocr": result,
                        "note": "账号不由机械臂侧推断；会话标题仅作 WX 观测证据"}
        except (ValueError, OSError, KeyError) as exc:
            return {"account_clue": None, "conversation_clue": None, "confidence": 0.0,
                    "ui_state": "unknown", "ui_state_display": "无法确认", "note": str(exc)}
        ui = await self.identify_ui_state()
        try:
            frame = await self._capture_for_operation()
            image = frame["image"]
            if self.screen_device_id:
                from drivers import screen_proof
                profile = screen_proof.load_profile(self.screen_device_id, self.data_root)
                region = (profile or {}).get("regions", {}).get("title")
                if not region:
                    raise ValueError("尚未登记本设备标题区域，不能使用全画面首行推断标题")
                _, bundle = screen_proof.binding(self.screen_device_id, self.data_root)
                aligned = self._aligned_screen_frame(frame, bundle, profile=profile)
                ui = screen_proof.identify(aligned, profile)
                cropped = screen_proof.crop(aligned, region)
                result_dict = await asyncio.to_thread(
                    screen_proof.recognize_patch, self.ocr_engine, cropped, "conversation")
            else:
                result = await asyncio.to_thread(self.ocr_engine.recognize, image, region="conversation")
                result_dict = result.to_dict()
            if result_dict.get("truncated") or len([line for line in result_dict["text"].splitlines() if line.strip()]) != 1:
                raise ValueError("标题区域为空、截断或存在多行歧义，请重新核对")
            title = (result_dict["text"].splitlines()[0].strip() if result_dict["text"] else "")
            return {"account_clue": None, "conversation_clue": title or None,
                    "confidence": float(result_dict["confidence"]), "ui_state": ui["ui_state"],
                    "ui_state_display": ui["ui_state_display"], "ocr": result_dict,
                    "note": "账号不由机械臂侧推断；会话标题仅作 WX 观测证据"}
        except Exception as exc:
            return {"account_clue": None, "conversation_clue": None, "confidence": 0.0,
                    "ui_state": ui["ui_state"], "ui_state_display": ui["ui_state_display"],
                    "note": f"会话标题 OCR 不可用：{exc}"}

    async def locate_text(self, text: str, *, region: str = "messages",
                          min_confidence: float = 0.75, exact: bool = False,
                          roi: dict | None = None) -> dict:
        """定位当前帧文字；精确模式未命中仍返回帧信息，异常不猜坐标。"""
        context = self._target_context()
        bundle = None
        if self.screen_device_id:
            from drivers import calibration_bundle as cb
            bundle = copy.deepcopy(cb.load_bundle(self.screen_device_id, self.data_root))
        frame = await self._capture_for_operation()
        query = {"text": text, "region": region, "min_confidence": min_confidence,
                 "exact": exact, "roi": copy.deepcopy(roi)}
        layout = await self._selected_layout_frame(frame)
        if layout is not None:
            located = await self._locate_layout_text(layout, **query)
            return self._remember_text_location(located, query, frame, layout, context, bundle)
        aligned = False
        if self.screen_device_id:
            if not bundle or bundle.get("status") not in {"ready", cb.MOTION_READY_STATUS}:
                raise OcrEngineError("手机屏幕校准尚未就绪", reason="screen_pose_unavailable")
            image = self._aligned_screen_frame(frame, bundle)
            frame = {**frame, "image": cv2.imencode(".png", image)[1].tobytes()}
            aligned = True
            screen_corners = np.asarray(bundle["screen"]["edgeModel"]["corners"], np.float32)
        bounds = (ocr_roi_bounds(roi, frame_width=frame["width"], frame_height=frame["height"])
                  if roi is not None else None)
        result = await asyncio.to_thread(
            self.ocr_engine.recognize, frame["image"],
            region="messages" if region == "full" else region)
        if result.truncated:
            raise OcrEngineError("OCR 结果被截断，无法确认目标是否存在或唯一",
                                 reason="ocr_result_truncated")
        if bounds is not None or aligned:
            if ((result.width and result.width != frame["width"])
                    or (result.height and result.height != frame["height"])):
                raise OcrEngineError("OCR 坐标尺寸与当前画面不一致", reason="ocr_roi_invalid")
            result = replace(result, words=[
                word for word in result.words
                if word.width > 0 and word.height > 0
                and (bounds is None or (
                    bounds[0] <= word.x and word.x + word.width <= bounds[2]
                    and bounds[1] <= word.y and word.y + word.height <= bounds[3]))
                and (not aligned or all(
                    cv2.pointPolygonTest(screen_corners, (float(x), float(y)), False) >= 0
                    for x in (word.x, word.x + word.width)
                    for y in (word.y, word.y + word.height)))
            ])
        located = find_text_bbox(result, text, min_confidence=min_confidence, exact=exact)
        metadata = {
            "engine": result.engine,
            "frameW": int(result.width or frame["width"]),
            "frameH": int(result.height or frame["height"]),
            "frameSeq": frame.get("source_frame_seq"),
            "frameSha256": frame.get("sha256"),
        }
        if aligned:
            metadata["coordinateVersion"] = "calibrated_camera.v1"
        if located is None:
            if exact:
                return self._remember_text_location(
                    {"found": False, **metadata}, query, frame, None, context, bundle)
            raise OcrEngineError(f"未找到文字：{text}", reason="ocr_text_not_found")
        return self._remember_text_location(
            {"found": True, **located, **metadata}, query, frame, None, context, bundle)

    async def _locate_layout_text(self, layout, text, *, region, min_confidence, exact, roi):
        from drivers import screen_proof

        frame = layout.source
        if region == "account" and layout.state != "wechat_list":
            raise OcrEngineError("当前不是已确认的联系人列表", reason="ocr_roi_invalid")
        roi_bounds = (ocr_roi_bounds(roi, frame_width=frame["width"],
                                    frame_height=frame["height"]) if roi is not None else None)
        height, width = layout.image.shape[:2]
        if region == "full":
            left, top, right, bottom = 0, 0, width, height
        else:
            purpose = {"messages": "content", "account": "content",
                       "conversation": "title", "input": "input"}.get(region)
            saved = layout.profile["regions"].get(purpose)
            if not saved:
                raise OcrEngineError("当前页没有已核验的文字定位区域", reason="ocr_roi_invalid")
            left, top, right, bottom = screen_proof.bounds(saved, width, height)
        patch = layout.image[top:bottom, left:right]
        observed = await asyncio.to_thread(
            screen_proof.recognize_patch, self.ocr_engine, patch,
            "messages" if region == "full" else region)
        words = []
        for item in observed["words"]:
            word = OcrWord(**item)
            word.x += left
            word.y += top
            if roi_bounds is not None:
                box = layout.camera_rect(word.x, word.y, word.width, word.height)
                if not (roi_bounds[0] <= box["x"] and roi_bounds[1] <= box["y"]
                        and box["x"] + box["width"] <= roi_bounds[2]
                        and box["y"] + box["height"] <= roi_bounds[3]):
                    continue
            words.append(word)
        result = OcrResult(
            text=observed["text"], words=words, confidence=observed["confidence"],
            truncated=False, engine=observed["engine"], engine_version=observed["engine_version"],
            width=width, height=height)
        located = find_text_bbox(result, text, min_confidence=min_confidence, exact=exact)
        self._ensure_layout_current(layout)
        metadata = {
            "engine": result.engine, "frameW": frame["width"], "frameH": frame["height"],
            "frameSeq": frame.get("source_frame_seq"), "frameSha256": frame.get("sha256"),
            "coordinateVersion": "calibrated_camera.v1", **layout.selected.references,
        }
        if located is not None:
            x1, y1, x2, y2 = located["bbox"]
            box = layout.camera_rect(x1, y1, x2 - x1, y2 - y1)
            if roi_bounds is not None and not (
                    roi_bounds[0] <= box["x"] and roi_bounds[1] <= box["y"]
                    and box["x"] + box["width"] <= roi_bounds[2]
                    and box["y"] + box["height"] <= roi_bounds[3]):
                located = None
            else:
                cx, cy = layout.camera_point(located["cameraX"], located["cameraY"])
                located.update(cameraX=cx, cameraY=cy, bbox=[
                    box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]])
        if located is None:
            if exact:
                return {"found": False, **metadata}
            raise OcrEngineError(f"未找到文字：{text}", reason="ocr_text_not_found")
        return {"found": True, **located, **metadata}

    async def observe_regions(self, *, include_image=False) -> dict:
        """Observe one frame using persisted device regions; never infer delivery."""
        from drivers import screen_proof
        if not self.screen_device_id:
            raise ValueError("当前摄像头没有设备级页面档案")
        layout = await self._selected_layout_frame()
        if layout is not None:
            profile = layout.evidence_profile()
            frame, image, ui = layout.source, layout.image, layout.ui
        else:
            profile = screen_proof.load_profile(self.screen_device_id, self.data_root)
            if not profile or not profile.get("regions"):
                raise ValueError("尚未登记页面识别与内容核对区域")
            frame = await self._capture_for_operation()
            _, bundle = screen_proof.binding(self.screen_device_id, self.data_root)
            image = self._aligned_screen_frame(frame, bundle, profile=profile)
            ui = screen_proof.identify(image, profile)

        def recognize(patch, region):
            return screen_proof.recognize_patch(self.ocr_engine, patch, region)

        conversation = layout is None or layout.state != "wechat_list"
        if conversation:
            title = await asyncio.to_thread(
                recognize, screen_proof.crop(image, profile["regions"]["title"]), "conversation")
            input_image = screen_proof.crop(image, profile["regions"]["input"])
            input_ocr = (layout.input_ocr if layout is not None else None)
            if input_ocr is None:
                input_ocr = await asyncio.to_thread(recognize, input_image, "input")
        else:
            title = {"text": "", "confidence": 0.0, "words": [], "truncated": False}
            input_ocr = {"text": "", "confidence": 0.0}
        # WX 列表导航需要整帧文字框来识别“微信/WeChat”标题和底部四个导航
        # 标签。标题区域/内容色块只能证明会话页内容，不能推导主页列表 ROI。
        navigation_ocr = await asyncio.to_thread(recognize, image, "messages")
        import base64
        input_empty, components = False, []
        if conversation:
            saved_input = screen_proof.decode(base64.b64decode(profile["empty_input"], validate=True))
            input_empty = screen_proof.input_is_empty(input_image, saved_input, input_ocr["text"])
            components = screen_proof.colored_components(image, profile)
        for item in components:
            patch = item.pop("image")
            observed = await asyncio.to_thread(recognize, patch, "messages")
            item.update(text=observed["text"], confidence=observed["confidence"],
                        truncated=observed["truncated"])
            if layout is not None:
                item.update(layout.camera_rect(item["x"], item["y"], item["width"], item["height"]))
        ocr_boxes = []
        screen_corners = (np.asarray(bundle["screen"]["edgeModel"]["corners"], np.float32)
                          if layout is None else None)
        for word in navigation_ocr.get("words", []):
            try:
                text = str(word.get("text", "")).strip()
                x = float(word["x"])
                y = float(word["y"])
                width = float(word["width"])
                height = float(word["height"])
                confidence = float(word["confidence"])
            except (KeyError, TypeError, ValueError):
                continue
            if (text and width > 0 and height > 0
                    and all(math.isfinite(value)
                            for value in (x, y, width, height, confidence))
                    and (layout is not None or all(cv2.pointPolygonTest(
                        screen_corners, (float(px), float(py)), False) >= 0
                        for px in (x, x + width) for py in (y, y + height)))):
                if layout is not None:
                    mapped = layout.camera_rect(x, y, width, height)
                    x, y, width, height = (mapped[k] for k in ("x", "y", "width", "height"))
                ocr_boxes.append({
                    "text": text, "x": x, "y": y, "w": width, "h": height,
                    "confidence": confidence,
                })
        result = {
            "contract_version": "screen-proof.v1", "device_id": self.screen_device_id,
            "camera_id": self.device_id, "profile_revision": profile["revision"],
            "binding": profile["binding"], "frame_seq": frame.get("source_frame_seq"),
            "frame_sha256": frame["sha256"],
            "captured_at": frame.get("source_captured_at") or frame["captured_at"],
            "frameW": int(frame["width"]), "frameH": int(frame["height"]),
            "ui": ui, "title": title, "input_empty": bool(input_empty),
            "input_text": input_ocr["text"], "components": components,
            "input_confidence": input_ocr["confidence"],
            "ocr_boxes": ocr_boxes,
        }
        if include_image:
            result["_image_bytes"] = frame["image"]
        if layout is not None:
            self._ensure_layout_current(layout)
            result.update(layout.selected.references, coordinateVersion="calibrated_camera.v1")
        return result

    async def _capture_for_operation(self) -> dict:
        broker = self._shared_broker
        if broker is not None:
            return await broker.capture()
        return await asyncio.to_thread(self.capture_sync)

    async def confirm_main_view(self, expected_ui_state: str = "wechat_main") -> dict:
        result = await self.identify_ui_state()
        payload = {
            "confirmed": False,
            "reason": None,
            "ui_state": result["ui_state"],
            "ui_state_display": result["ui_state_display"],
            "confidence": result.get("confidence", 0.0),
        }
        if result.get("note"):
            payload["reason"] = result["note"]
            return payload
        if result["ui_state"] != expected_ui_state:
            payload["reason"] = f"当前画面为{result['ui_state_display']}，与期望不一致"
            return payload
        payload["confirmed"] = True
        return payload


class HttpFrameSource:
    """从本机取流桥（scripts/camera_frame_server.py）拉取 JPEG 帧并解码为 BGR。

    用途：macOS TCC 将摄像头权限授予浏览器时，由 Chrome 推帧到本机桥服务，
    驱动经此来源取帧，质量检查/帧差验证逻辑不变。

    stream_alive：桥序列号是否在推进。True=推到新帧；False=桥端无新帧。
    静态场景下摄像头推的新帧可能字节完全一致，"画面冻结"不能只看哈希，
    必须结合本标记（见 RealCameraDriver.capture_sync）。
    """

    def __init__(
        self,
        url: str = "http://127.0.0.1:8790/latest.jpg",
        timeout: float = 3.0,
        *,
        expected_camera_id: str | None = None,
        expected_device_id: str | None = None,
        expected_device_unique_id: str | None = None,
        expected_width: int | None = None,
        expected_height: int | None = None,
        allow_legacy: bool = False,
        max_age_seconds: float = 1.5,
    ):
        self.url = url
        self.timeout = timeout
        self.expected_camera_id = expected_camera_id
        self.expected_device_id = expected_device_id
        self.expected_device_unique_id = expected_device_unique_id
        self.expected_width = int(expected_width) if expected_width else None
        self.expected_height = int(expected_height) if expected_height else None
        self.allow_legacy = bool(allow_legacy)
        self.max_age_seconds = float(max_age_seconds)
        self._last_seq: int | None = None
        self._last_seq_at: float | None = None
        self.stream_alive: bool = True
        # seq 推进的活性窗口：静态场景下取流桥可能连续数秒无新帧推送
        # （源头节流/快速连续采样），单次同 seq 不能判流死；窗口内推进过
        # 即活，窗口外仍无推进才是真断流。
        self.seq_staleness_seconds: float = 3.0
        self.last_camera_id: str | None = None
        self.last_device_id: str | None = None
        self.last_device_unique_id: str | None = None
        self.last_binding_revision: int | None = None
        self.last_frame_seq: int | None = None
        self.last_captured_at: str | None = None
        self.last_received_at: str | None = None
        self.last_resolution: tuple[int, int] | None = None
        self.resolution_changed: bool = False
        self.last_frame_source: str | None = None
        self.legacy_mode: bool = False
        # R2-08（camera-binding 附件 §5）：冻结帧包 v2 头逐层透传——绑定实例/
        # 几何/代际/源序号/时钟登记/采样单调时刻/不确定度/PTS 证明。缺失即缺，
        # 不在本层补默认值；动作资格由 camera_broker.frame_chain_identity 判定。
        self.last_camera_binding_id: str | None = None
        self.last_source_geometry_id: str | None = None
        self.last_source_generation_id: str | None = None
        self.last_source_capture_seq: int | None = None
        self.last_source_clock_id: str | None = None
        self.last_source_captured_monotonic_ns: int | None = None
        self.last_source_capture_uncertainty_ms: float | None = None
        self.last_source_timing_provenance: str | None = None

    @staticmethod
    def _parse_int(name: str, raw: str | None, *, required: bool) -> int | None:
        if raw in (None, ""):
            if required:
                raise RuntimeError(f"取流桥缺少响应头 {name}")
            return None
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"取流桥响应头 {name} 无效：{raw!r}") from exc

    @staticmethod
    def _parse_time(name: str, raw: str | None, *, required: bool) -> datetime | None:
        if not raw:
            if required:
                raise RuntimeError(f"取流桥缺少响应头 {name}")
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(f"取流桥响应头 {name} 无效：{raw!r}") from exc

    @staticmethod
    def _require_identity(name: str, actual: str | None, expected: str | None, *, legacy: bool) -> None:
        if expected is None:
            return
        if not actual:
            if legacy:
                return
            raise RuntimeError(f"取流桥缺少响应头 {name}")
        if actual != expected:
            raise RuntimeError(f"取流桥身份不匹配：{name} 期望 {expected}，实际 {actual}")

    def __call__(self) -> np.ndarray:
        import urllib.request

        with urllib.request.urlopen(self.url, timeout=self.timeout) as response:
            data = response.read()
            headers = response.headers
            camera_id = headers.get("X-Camera-Id")
            device_id = headers.get("X-Device-Id")
            unique_id = headers.get("X-Camera-Unique-Id")
            identity_missing = not camera_id or not device_id
            legacy = bool(self.allow_legacy and identity_missing)
            self._require_identity("X-Camera-Id", camera_id, self.expected_camera_id, legacy=legacy)
            self._require_identity("X-Device-Id", device_id, self.expected_device_id, legacy=legacy)
            self._require_identity(
                "X-Camera-Unique-Id", unique_id, self.expected_device_unique_id, legacy=legacy
            )
            formal = bool(self.expected_camera_id and not legacy)
            binding_revision = self._parse_int(
                "X-Binding-Revision", headers.get("X-Binding-Revision"), required=formal
            )
            seq = self._parse_int("X-Frame-Seq", headers.get("X-Frame-Seq"), required=formal)
            width_header = self._parse_int(
                "X-Frame-Width", headers.get("X-Frame-Width"), required=formal
            )
            height_header = self._parse_int(
                "X-Frame-Height", headers.get("X-Frame-Height"), required=formal
            )
            captured_at = headers.get("X-Captured-At") or headers.get("X-Frame-At")
            received_at = headers.get("X-Received-At")
            received_time = self._parse_time("X-Received-At", received_at, required=formal)
            # R2-08：v2 采样时序头（可选；缺失=该帧不进入新链，不补默认值）
            source_provenance = (headers.get("X-Source-Timing-Provenance") or "").strip() or None
            source_geometry_id = (headers.get("X-Source-Geometry-Id") or "").strip() or None
            source_generation = (headers.get("X-Source-Generation-Id") or "").strip() or None
            source_capture_seq = self._parse_int(
                "X-Source-Frame-Seq", headers.get("X-Source-Frame-Seq"), required=False)
            captured_monotonic_ns = self._parse_int(
                "X-Source-Captured-Monotonic-Ns",
                headers.get("X-Source-Captured-Monotonic-Ns"), required=False)
            uncertainty_raw = (headers.get("X-Source-Capture-Uncertainty-Ms") or "").strip()
            try:
                capture_uncertainty_ms = float(uncertainty_raw) if uncertainty_raw else None
            except ValueError as exc:
                raise RuntimeError(
                    f"取流桥响应头 X-Source-Capture-Uncertainty-Ms 无效：{uncertainty_raw!r}"
                ) from exc
        # R2-08：可证明采样时刻的帧按采样时刻（含误差裕量）判断新鲜度；
        # 接收时间不能让早已采样的帧续鲜。无采样时钟证明的帧保持原
        # 接收时间判断（兼容分支），不进入新链。
        proven_sample_time = (
            source_provenance == SOURCE_TIMING_PROVENANCE
            and captured_monotonic_ns is not None)
        if proven_sample_time:
            uncertainty_s = (capture_uncertainty_ms or 0.0) / 1000.0
            sample_age = (time.monotonic_ns() - captured_monotonic_ns) / 1e9 - uncertainty_s
            if sample_age > self.max_age_seconds:
                raise RuntimeError(
                    f"取流桥返回过期采样：采样时刻距今 {sample_age:.3f}s"
                    f"（含 ±{(capture_uncertainty_ms or 0.0):.1f}ms 误差）超过 "
                    f"{self.max_age_seconds:.3f}s；接收时间不能替采样时间续鲜")
            if -sample_age > 0.5 + uncertainty_s:
                raise RuntimeError(
                    f"取流桥返回未来采样时刻（{-sample_age:.3f}s）：拒绝")
        if received_time is not None:
            if received_time.tzinfo is None:
                received_time = received_time.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - received_time.astimezone(timezone.utc)).total_seconds()
            if not proven_sample_time and age > self.max_age_seconds:
                raise RuntimeError(f"取流桥返回过期帧：帧年龄 {age:.3f}s")
        if seq is not None and self._last_seq is not None and seq < self._last_seq:
            raise RuntimeError(f"取流桥帧序号倒退：{self._last_seq} -> {seq}")
        if seq is not None and seq != self._last_seq:
            self._last_seq_at = time.monotonic()
        self.stream_alive = bool(
            seq is not None and self._last_seq_at is not None
            and time.monotonic() - self._last_seq_at <= self.seq_staleness_seconds)
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError("取流桥返回的帧无法解码")
        resolution = (int(frame.shape[1]), int(frame.shape[0]))
        if width_header is not None and height_header is not None \
                and resolution != (width_header, height_header):
            raise RuntimeError(
                f"取流桥分辨率响应头与 JPEG 不一致：头 {width_header}x{height_header}，"
                f"图片 {resolution[0]}x{resolution[1]}"
            )
        if self.expected_width and self.expected_height \
                and resolution != (self.expected_width, self.expected_height):
            raise RuntimeError(
                f"摄像头分辨率不匹配：期望 {self.expected_width}x{self.expected_height}，"
                f"实际 {resolution[0]}x{resolution[1]}"
            )
        self.resolution_changed = self.last_resolution is not None and resolution != self.last_resolution
        self._last_seq = seq
        self.last_camera_id = camera_id
        self.last_device_id = device_id
        self.last_device_unique_id = unique_id
        self.last_binding_revision = binding_revision
        self.last_frame_seq = seq
        self.last_captured_at = captured_at
        self.last_received_at = received_at
        self.last_resolution = resolution
        # R2-08：冻结帧包 v2 采样时序逐层透传（缺失保持 None，不补默认值）
        self.last_camera_binding_id = (headers.get("X-Camera-Binding-Id") or "").strip() or None
        self.last_source_geometry_id = source_geometry_id
        self.last_source_generation_id = source_generation
        self.last_source_capture_seq = source_capture_seq
        self.last_source_clock_id = (headers.get("X-Source-Clock-Id") or "").strip() or None
        self.last_source_captured_monotonic_ns = captured_monotonic_ns
        self.last_source_capture_uncertainty_ms = capture_uncertainty_ms
        self.last_source_timing_provenance = source_provenance
        # 帧来源标记（camera_helper 正式采集 / chrome_compat 兼容降级桥 / legacy 旧单路）
        self.last_frame_source = (headers.get("X-Frame-Source") or "").strip().lower() or None
        self.legacy_mode = legacy
        return frame


class CameraMotionVerifier:
    """运动前后帧差验证：ok/MPos 不是真实移动的证明，必须画面变化才确认。

    settle_seconds：运动停止后等待再采"后帧"（取流桥按固定间隔推帧时，
    立即采帧可能拿到的仍是运动前画面；按推帧间隔设置，建议 ≥ 推帧间隔）。
    evidence_dir：提供时把每段前后帧落盘存档（方向验证等现场证据）。
    """

    measurement_kind = "frame_change"

    def __init__(
        self,
        camera: CameraDriver,
        diff_threshold: float = 1.5,
        *,
        settle_seconds: float = 0.0,
        evidence_dir=None,
    ):
        self._camera = camera
        self._threshold = diff_threshold
        self._settle = settle_seconds
        self._evidence_dir = Path(evidence_dir) if evidence_dir else None
        if self._evidence_dir:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
        self._pending: dict[str, bytes] = {}
        self.history: list[dict] = []

    def _save_evidence(self, label: str, phase: str, image_bytes: bytes) -> str | None:
        if not self._evidence_dir:
            return None
        safe = label.replace(":", "_").replace("/", "_")
        target = self._evidence_dir / f"{safe}-{phase}.png"
        target.write_bytes(image_bytes)
        return target.name

    async def before(self, label: str) -> dict:
        frame = await self._camera.capture()
        self._pending[label] = frame["image_bytes"]
        saved = self._save_evidence(label, "before", frame["image_bytes"])
        return {"label": label, "sha256": frame["sha256"], "captured_at": frame["captured_at"], "evidence": saved}

    async def after(self, label: str) -> dict:
        before_bytes = self._pending.pop(label, None)
        if self._settle > 0:
            await asyncio.sleep(self._settle)
        frame = await self._camera.capture()
        checked_at = iso(utc_now())
        saved = self._save_evidence(label, "after", frame["image_bytes"])
        if before_bytes is None:
            return {"verified": False, "reason": "缺少运动前基准帧，无法验证真实移动", "checked_at": checked_at, "evidence": saved}
        diff = _frame_diff(before_bytes, frame["image_bytes"])
        verified = diff >= self._threshold
        record = {
            "verified": verified,
            "diff_score": round(diff, 3),
            "threshold": self._threshold,
            "before_sha256": sha256_bytes(before_bytes),
            "after_sha256": frame["sha256"],
            "checked_at": checked_at,
            "evidence": saved,
            "reason": None if verified else "画面未出现可确认的变化，机械真实移动未被证实（MPos/ok 不作为依据）",
        }
        self.history.append(record)
        return record


def _frame_diff(before: bytes, after: bytes) -> float:
    a = cv2.imdecode(np.frombuffer(before, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    b = cv2.imdecode(np.frombuffer(after, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if a is None or b is None:
        return 0.0
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    return float(np.mean(cv2.absdiff(a, b)))
