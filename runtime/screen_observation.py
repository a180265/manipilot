"""通用屏幕证据：校准透视矫正与 OCR，不解释任何应用业务。"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import math

import cv2
import numpy as np

from drivers import calibration_bundle
from drivers.real_camera import RealCameraDriver
from drivers.screen_edge import locate_screen_quad, ScreenEdgeError
from drivers.screen_pose import align_screen, fit_pose
from runtime.calibration_gate import _fresh_source_seq
from runtime.errors import ApiError
from runtime.phone_region_context import (
    RegionContextError, context_matches_bundle, load_region_context,
    locate_quad_in_region, require_unchanged)


def _calibrated_inverse(bundle: dict) -> np.ndarray:
    screen = bundle["screen"]
    width, height = int(screen["screenWidth"]), int(screen["screenHeight"])
    scale = min(1.0, 720 / width, 1600 / height)
    homography = bundle["camera"]["cameraToScreen"]["homography"]
    matrix = np.asarray(homography, dtype=float).reshape(3, 3)
    return np.linalg.inv(np.diag([scale, scale, 1.0]) @ matrix)


def rectify(image: bytes, bundle: dict,
            search_region: dict | None = None) -> tuple[bytes, list, int, int]:
    screen = bundle["screen"]
    width, height = int(screen["screenWidth"]), int(screen["screenHeight"])
    homography = bundle["camera"]["cameraToScreen"]["homography"]
    if isinstance(homography, (list, tuple)) and len(homography) == 9:
        homography = [homography[:3], homography[3:6], homography[6:9]]
    matrix = np.asarray(homography, dtype=float)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError("屏幕投影矩阵无效")
    if not (100 <= width <= 10000 and 100 <= height <= 10000):
        raise ValueError("屏幕尺寸无效")
    source = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if source is None:
        raise ValueError("画面无法解码")
    saved_corners = ((screen.get("edgeModel") or {}).get("corners"))
    pose_matrix = np.eye(3)
    if saved_corners:
        try:
            if search_region is not None:
                # S1：本台生效粗框约束——框内定位（多候选/裁断拒绝），四角
                # 加回原图偏移后再做姿态拟合；不回全图。
                live_corners = locate_quad_in_region(
                    source, search_region, saved_corners)["corners"]
            else:
                live_corners = locate_screen_quad(
                    source, saved_corners, tolerance_px=32,
                    require_fittable=True)["corners"]
            pose = fit_pose(saved_corners, live_corners)
            source = align_screen(source, saved_corners, pose)
            pose_matrix = np.asarray(pose["matrix"], dtype=float)
        except (ScreenEdgeError, ValueError, np.linalg.LinAlgError,
                RegionContextError) as exc:
            raise ValueError(f"手机位置无法确认，禁止使用旧坐标识别页面：{exc}") from exc
    scale = min(1.0, 720 / width, 1600 / height)
    transform = np.linalg.inv(_calibrated_inverse(bundle))
    width, height = round(width * scale), round(height * scale)
    output = cv2.warpPerspective(source, transform, (width, height),
                                 borderValue=(127, 127, 127))
    # 只改善证据副本的亮度；原始帧哈希及校准矩阵不改写。
    light = cv2.cvtColor(output, cv2.COLOR_BGR2LAB)
    light[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(light[:, :, 0])
    output = cv2.cvtColor(light, cv2.COLOR_LAB2BGR)
    ok, encoded = cv2.imencode(".png", output)
    if not ok:
        raise ValueError("屏幕画面编码失败")
    return encoded.tobytes(), (pose_matrix @ np.linalg.inv(transform)).tolist(), width, height


def _validate_calibration(bundle: dict, device_id: str, device: dict) -> None:
    # Auto_CS#20 纯观测通道：observation_ready 档案跳过 daemonSession 绑定
    # （观测是纯读：相机+OCR，与机械臂状态无关）；物理漂移仍由 rectify 的
    # live 四角定位每帧实测拒绝（"手机位置无法确认，禁止使用旧坐标"）。
    # ready/motion_ready 原语义零变化（仍要求本设备当前会话）。
    observation_only = bool(bundle) and \
        bundle.get("status") == calibration_bundle.OBSERVATION_READY_STATUS
    if (not bundle or bundle.get("status") not in {
            "ready", calibration_bundle.MOTION_READY_STATUS,
            calibration_bundle.OBSERVATION_READY_STATUS}
            or bundle.get("deviceId") != device_id
            or bundle.get("cameraId") != device.get("camera_id")
            or (not observation_only
                and (not device.get("daemon_session")
                     or bundle.get("daemonSession") != device["daemon_session"]))):
        raise ApiError("calibration_invalid", "屏幕观测需要本设备当前状态的有效校准")
    try:
        width = int(bundle["screen"]["screenWidth"])
        height = int(bundle["screen"]["screenHeight"])
        homography = bundle["camera"]["cameraToScreen"]["homography"]
        if isinstance(homography, (list, tuple)) and len(homography) == 9:
            homography = [homography[:3], homography[3:6], homography[6:9]]
        matrix = np.asarray(homography, dtype=float)
        corners = np.asarray((bundle["screen"].get("edgeModel") or {}).get("corners"),
                             dtype=float)
        valid_matrix = (matrix.shape == (3, 3) and np.isfinite(matrix).all()
                        and abs(np.linalg.det(matrix)) >= 1e-12)
        valid_corners = (corners.shape == (4, 2) and np.isfinite(corners).all()
                         and cv2.isContourConvex(corners.astype(np.float32)))
        if not (100 <= width <= 10000 and 100 <= height <= 10000
                and valid_matrix and valid_corners):
            raise ValueError("屏幕校准矩阵或尺寸无效")
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ApiError("calibration_invalid", "屏幕校准矩阵或尺寸无效")


class ScreenObservationService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.locks = {}
        self.ocr_limit = asyncio.Semaphore(2)

    def _pin_context(self, device_id, bundle):
        devices = self.runtime.devices
        device, bundle = copy.deepcopy(devices.get(device_id)), copy.deepcopy(bundle)
        _validate_calibration(bundle, device_id, device)
        provider = getattr(self.runtime, "layout_bundle", None)
        store = provider()["region_store"] if provider is not None else None
        keys = ("camera_id", "camera_device_unique_id", "camera_binding_id",
                "binding_revision", "daemon_session", "source_geometry_id",
                "daemon_binding_id", "arm_binding_id",
                "expected_frame_width", "expected_frame_height")
        binding = {key: device.get(key) for key in keys}
        try:
            context = copy.deepcopy(load_region_context(store, device_id))
            context_matches_bundle(context, bundle)
        except RegionContextError as exc:
            raise ApiError("region_context_invalid", f"生效粗框不可用：{exc.message}") from exc
        driver = devices.driver(device_id)
        root = getattr(driver, "data_root", None)

        def ensure_current():
            current_device = devices.get(device_id)
            if {key: current_device.get(key) for key in keys} != binding:
                raise ApiError("region_context_changed", "设备绑定在识别过程中已变化，原观察无效")
            current_bundle = calibration_bundle.load_bundle(device_id, root)
            _validate_calibration(current_bundle, device_id, current_device)
            if current_bundle != bundle:
                raise ApiError("region_context_changed", "校准在识别过程中已变化，原观察无效")
            try:
                current = require_unchanged(context, store, device_id)
                context_matches_bundle(current, current_bundle)
            except RegionContextError as exc:
                raise ApiError("region_context_changed", f"原观察已失效：{exc.message}") from exc

        return device, bundle, context, ensure_current

    async def capture(self, device_id):
        lock = self.locks.setdefault(device_id, asyncio.Lock())
        async with lock, self.ocr_limit:
            devices = self.runtime.devices
            try:
                driver = devices.driver(device_id)
                bundle = calibration_bundle.load_bundle(device_id, getattr(driver, "data_root", None))
                device, bundle, region_ctx, ensure_current = self._pin_context(device_id, bundle)
                search_region = (region_ctx or {}).get("region")
                frame = await devices.camera_broker(device_id).capture()
                if (frame.get("width") != bundle.get("frame", {}).get("width")
                        or frame.get("device_id") != device_id
                        or frame.get("camera_id") != device.get("camera_id")
                        or frame.get("height") != bundle.get("frame", {}).get("height")
                        or frame.get("device_unique_id") != bundle.get("cameraDeviceUniqueId")
                        or frame.get("binding_revision") != bundle.get("bindingRevision")):
                    raise ApiError("calibration_invalid", "画面来源或分辨率与校准不一致")
                image = frame.get("image") or frame.get("image_bytes")
                if frame.get("frozen") or not image:
                    raise ApiError("camera_unavailable", "屏幕证据画面冻结或缺失")
                try:
                    _fresh_source_seq(frame, None)
                except ValueError as exc:
                    raise ApiError("camera_unavailable", f"屏幕证据不是新鲜推流帧：{exc}") from exc
                source = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
                if source is None or source.shape != (frame["height"], frame["width"]):
                    raise ApiError("camera_unavailable", "屏幕证据画面无法解码或尺寸不符")
                metrics = RealCameraDriver._analyze(
                    image, expected_corners=bundle["screen"]["edgeModel"]["corners"],
                    search_region=search_region)

                quality = RealCameraDriver._classify(metrics, False)
                if quality not in {"waiting", "clear"}:
                    raise ApiError("camera_unavailable", "手机屏幕画质异常，暂不识别页面")
                waiting = quality == "waiting"
                calibrated_inverse = None
                if waiting:
                    screen = bundle["screen"]
                    width, height = int(screen["screenWidth"]), int(screen["screenHeight"])
                    scale = min(1.0, 720 / width, 1600 / height)
                    width, height = round(width * scale), round(height * scale)
                    png = cv2.imencode(".png", np.zeros((height, width), np.uint8))[1].tobytes()
                    inverse = np.eye(3).tolist()
                    ocr_data = {"region": "messages", "text": "", "words": [],
                                "confidence": 0, "truncated": False,
                                "engine": "", "engine_version": "", "request_id": "",
                                "elapsed_ms": 0, "width": width, "height": height}
                else:
                    png, inverse, width, height = await asyncio.to_thread(
                        rectify, image, bundle, search_region)
                    calibrated_inverse = _calibrated_inverse(bundle).tolist()
                    camera = devices.camera(device_id)
                    ocr = await asyncio.to_thread(camera.ocr_engine.recognize, png, region="messages")
                    ocr_data = ocr.to_dict()
                    if (ocr_data["width"], ocr_data["height"]) != (width, height):
                        raise ApiError("camera_unavailable", "屏幕 OCR 坐标尺寸与矫正画面不一致")
                    homography = np.asarray(
                        bundle["camera"]["cameraToScreen"]["homography"],
                        dtype=float).reshape(3, 3)
                    scale = np.diag([
                        width / bundle["screen"]["screenWidth"],
                        height / bundle["screen"]["screenHeight"], 1])
                    corners = np.asarray(
                        bundle["screen"]["edgeModel"]["corners"], np.float32)
                    polygon = cv2.perspectiveTransform(
                        corners[None], (scale @ homography).astype(np.float32))[0]
                    words = []
                    for word in ocr_data["words"]:
                        try:
                            x, y = float(word["x"]), float(word["y"])
                            w, h = float(word["width"]), float(word["height"])
                            confidence = float(word["confidence"])
                            inside = (w > 0 and h > 0 and
                                      all(math.isfinite(v) for v in (x, y, w, h, confidence))
                                      and all(cv2.pointPolygonTest(
                                          polygon, (px, py), False) >= 0
                                          for px in (x, x + w) for py in (y, y + h)))
                            if inside:
                                words.append(word)
                        except (KeyError, TypeError, ValueError):
                            continue
                    if len(words) != len(ocr_data["words"]) or (not words and ocr_data["text"]):
                        ocr_data["text"] = "\n".join(word["text"] for word in words)
                        if not words:
                            ocr_data["confidence"] = 0
                    ocr_data["words"] = words
                ensure_current()
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError("camera_unavailable", f"屏幕证据处理失败：{exc}") from exc
            result = {
                "contract_version": "screen-observation.v1", "device_id": device_id,
                "camera_id": device["camera_id"],
                # 纯观测档案（observation_ready）设备无串口会话：字段恒在，值可为 null
                "daemon_session": device.get("daemon_session"),
                "wechat_account_id": device.get("wechat_account_id"),
                "binding_revision": frame["binding_revision"],
                "camera_device_unique_id": frame["device_unique_id"],
                "calibration_fingerprint": hashlib.sha256(
                    str(bundle["camera"]["cameraToScreen"]).encode()).hexdigest(),
                "motion_calibration_fingerprint": (
                    calibration_bundle.motion_calibration_fingerprint(bundle)),
                "captured_at": frame["source_captured_at"], "frame_sha256": frame["sha256"],
                "frame_seq": frame.get("source_frame_seq"),
                "frame_width": frame["width"], "frame_height": frame["height"],
                "screen_width": width, "screen_height": height,
                "screen_to_camera": inverse,
                "quality": "waiting" if waiting else "clear",
                "image_base64": base64.b64encode(png).decode(),
                "image_sha256": hashlib.sha256(png).hexdigest(),
                "ocr": ocr_data,
            }
            if calibrated_inverse is not None:
                result["screen_to_calibrated_camera"] = calibrated_inverse
            return result


# ---- §5.1（camera-binding B03）：从指定 camera_source_v2 原图摆正识别 ----
async def observe_from_source_frame(runtime, *, device_id: str, bundle: dict,
                                    evidence_id: str) -> dict:
    """只从指定原图证据摆正/识别（不另拍新帧、不沿用旧观察）。

    observation 为 screen-observation.v1 已声明字段内的严格投影；
    frame_sha256/frame_seq/captured_at/物理身份/尺寸与外层 source 逐项一致；
    摆正失败/截断/画质 waiting 按原规则拒绝动作（不判断客户可否回复）。
    """
    service = getattr(runtime, "_screen_observation_service", None)
    if service is None:
        service = ScreenObservationService(runtime)
        runtime._screen_observation_service = service
    return await service.observe_from_source_frame(device_id, bundle, evidence_id)


def _build_source_observation(service, device_id: str, bundle: dict,
                              record: dict, image: bytes, *, device: dict,
                              ensure_current, search_region: dict | None = None) -> dict:
    """摆正+OCR+严格投影（与 ScreenObservationService.capture 同管线）。

    search_region（S1）：本台生效粗框——质量前置与定位均受框约束；OCR 后
    重核指针，识别期间换框/换绑原观察无效。
    """
    devices = service.runtime.devices
    jpeg_sha = hashlib.sha256(image).hexdigest()
    if jpeg_sha != record.get("jpeg_sha256"):
        raise ApiError("source_unavailable", "来源证据哈希与记录不一致")
    source_gray = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    if source_gray is None:
        raise ApiError("source_unavailable", "来源证据无法解码")
    from runtime.layout_capture import archived_camera_identity
    try:
        physical_camera = archived_camera_identity(record)
    except ValueError as exc:
        raise ApiError("source_unavailable", str(exc)) from exc
    if (physical_camera != bundle.get("cameraDeviceUniqueId")
            or (device.get("camera_device_unique_id")
                and physical_camera != device["camera_device_unique_id"])):
        raise ApiError("source_unavailable", "指定原帧不属于当前校准的物理相机")
    expected_size = (bundle["frame"]["height"], bundle["frame"]["width"])
    if (source_gray.shape != expected_size
            or (record.get("frame_height"), record.get("frame_width")) != expected_size):
        raise ApiError("source_unavailable", "指定原帧尺寸与归档或本台校准不一致")
    for key, expected in (
            ("camera_id", bundle.get("cameraId")),
            ("camera_binding_id", bundle.get("cameraBindingId")),
            ("source_geometry_id", bundle.get("sourceGeometryId"))):
        if key in record and record[key] != expected:
            raise ApiError("source_unavailable", "指定原帧来源与本台校准不一致")
    metrics = RealCameraDriver._analyze(
        image, expected_corners=bundle["screen"]["edgeModel"]["corners"],
        search_region=search_region)
    quality = RealCameraDriver._classify(metrics, False)
    if quality == "abnormal":
        raise ApiError("camera_unavailable", "手机屏幕画质异常，暂不识别页面")
    waiting = quality == "waiting"
    screen = bundle["screen"]
    width, height = int(screen["screenWidth"]), int(screen["screenHeight"])
    scale = min(1.0, 720 / width, 1600 / height)
    width, height = round(width * scale), round(height * scale)
    calibrated_inverse = None
    inverse = np.eye(3).tolist()
    if waiting:
        png = cv2.imencode(".png", np.zeros((height, width), np.uint8))[1].tobytes()
        ocr_data = {"region": "messages", "text": "", "words": [],
                    "confidence": 0, "truncated": False,
                    "engine": "", "engine_version": "", "request_id": "",
                    "elapsed_ms": 0, "width": width, "height": height}
    else:
        png, inverse, width, height = rectify(image, bundle, search_region)
        calibrated_inverse = _calibrated_inverse(bundle).tolist()
        camera = devices.camera(device_id)
        ocr_data = camera.ocr_engine.recognize(png, region="messages").to_dict()
        if (ocr_data["width"], ocr_data["height"]) != (width, height):
            raise ApiError("source_unavailable",
                           "屏幕 OCR 坐标尺寸与矫正画面不一致")
        homography = np.asarray(
            bundle["camera"]["cameraToScreen"]["homography"], dtype=float).reshape(3, 3)
        scale_m = np.diag([
            width / screen["screenWidth"], height / screen["screenHeight"], 1])
        corners = np.asarray(screen["edgeModel"]["corners"], np.float32)
        polygon = cv2.perspectiveTransform(
            corners[None], (scale_m @ homography).astype(np.float32))[0]
        words = []
        for word in ocr_data["words"]:
            try:
                x, y = float(word["x"]), float(word["y"])
                w, h = float(word["width"]), float(word["height"])
                confidence = float(word["confidence"])
                inside = (w > 0 and h > 0
                          and all(math.isfinite(v) for v in (x, y, w, h, confidence))
                          and all(cv2.pointPolygonTest(polygon, (px, py), False) >= 0
                                  for px in (x, x + w) for py in (y, y + h)))
                if inside:
                    words.append(word)
            except (KeyError, TypeError, ValueError):
                continue
        if len(words) != len(ocr_data["words"]) or (not words and ocr_data["text"]):
            ocr_data["text"] = "\n".join(word["text"] for word in words)
            if not words:
                ocr_data["confidence"] = 0
        ocr_data["words"] = words
    from drivers.gesture_program import coordinate_frame_hash
    frame_geometry = {
        "frame": bundle.get("frame"), "screen": bundle.get("screen"),
        "camera": bundle.get("camera"), "xy": bundle.get("xy"),
        "deviceId": bundle.get("deviceId"),
    }
    result = {
        "contract_version": "screen-observation.v1", "device_id": device_id,
        "camera_id": device["camera_id"], "daemon_session": device.get("daemon_session"),
        "wechat_account_id": device.get("wechat_account_id"),
        "camera_device_unique_id": physical_camera,
        "calibration_fingerprint": hashlib.sha256(
            str(bundle["camera"]["cameraToScreen"]).encode()).hexdigest(),
        "motion_calibration_fingerprint": (
            calibration_bundle.motion_calibration_fingerprint(bundle)),
        "captured_at": record.get("captured_at"),
        "binding_revision": record.get("binding_revision"),
        "frame_sha256": record.get("jpeg_sha256"),
        "frame_seq": record.get("source_frame_seq"),
        "frame_width": record.get("frame_width"),
        "frame_height": record.get("frame_height"),
        "screen_width": width, "screen_height": height,
        "screen_to_camera": inverse,
        "quality": "waiting" if waiting else "clear",
        "image_base64": base64.b64encode(png).decode(),
        "image_sha256": hashlib.sha256(png).hexdigest(),
        "ocr": ocr_data,
        "coordinate_frame_hash": coordinate_frame_hash(frame_geometry),
        "source": {key: record.get(key) for key in (
            "device_id", "camera_id", "camera_binding_id",
            "source_geometry_id", "source_generation_id",
            "source_capture_seq", "source_clock_id",
            "source_captured_monotonic_ns", "captured_at",
            "received_at", "jpeg_sha256", "frame_width", "frame_height",
            "camera_unique_id")},
    }
    if calibrated_inverse is not None:
        result["screen_to_calibrated_camera"] = calibrated_inverse
    ensure_current()
    return result


async def _observe_from_source_frame_inner(self, device_id: str, bundle: dict,
                                           evidence_id: str) -> dict:
    lock = self.locks.setdefault(device_id, asyncio.Lock())
    async with lock, self.ocr_limit:
        record = self.runtime.evidence.get(evidence_id)
        if (record is None or record.get("device_id") != device_id
                or record.get("evidence_type") != "camera_source_v2"):
            raise ApiError("camera_not_found", "来源证据不存在")
        image = self.runtime.evidence.read_source_frame_bytes(evidence_id)
        device, bundle, region_ctx, ensure_current = self._pin_context(device_id, bundle)
        try:
            return _build_source_observation(
                self, device_id, bundle, record, image,
                device=device, ensure_current=ensure_current,
                search_region=(region_ctx or {}).get("region"))
        except (ValueError, RuntimeError, cv2.error, RegionContextError) as exc:
            raise ApiError(
                "source_unavailable", "指定原帧的屏幕边界、画质或OCR无法核验") from exc


ScreenObservationService.observe_from_source_frame = (
    _observe_from_source_frame_inner)
