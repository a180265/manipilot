"""Camera evidence of an explicitly registered physical probe, not page changes."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time
from pathlib import Path

import cv2
import numpy as np

from drivers.real_camera import CameraMotionVerifier
from drivers import calibration_bundle


PROFILE_NAME = "probe_tracking.json"
BINDING_KEYS = ("camera_id", "device_unique_id", "binding_revision", "width", "height")


def profile_stale_for(device_id: str, data_root: Path | None) -> bool:
    """已登记的探头照片是否不属于当前校准绑定（换摄像头/重绑/无档案）。

    规范语义（observation_only 注释同源）：过期的探头照片只是证据，
    绝不作为手势动作的新门禁——返回 True 时调用方应以观察模式构造
    核验器（校准档案本身作为身份源）；重新登记探头后自动恢复强校验。
    """
    base = Path(data_root) if data_root else Path(__file__).resolve().parents[1] / "data"
    try:
        profile = json.loads(
            (base / "devices" / str(device_id) / PROFILE_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return True  # 无探头档案：观察模式（校准为身份源）
    bundle = calibration_bundle.load_bundle(device_id, base)
    if not bundle:
        return True
    frame = bundle.get("frame") or {}
    current = {"camera_id": bundle.get("cameraId"),
               "device_unique_id": bundle.get("cameraDeviceUniqueId"),
               "binding_revision": bundle.get("bindingRevision"),
               "width": frame.get("width"), "height": frame.get("height")}
    binding = profile.get("binding") or {}
    return any(binding.get(key) != value for key, value in current.items())


def _image(frame: dict):
    image = cv2.imdecode(np.frombuffer(frame["image_bytes"], np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("探头核验画面无法解码")
    return image


def register_probe(frame: dict, roi: dict, *, device_id: str, data_root: Path,
                   operator: str) -> dict:
    """Register a camera-selected metal probe patch. This does not move the arm."""
    if not operator.strip() or set(roi) != {"x", "y", "width", "height"}:
        raise ValueError("探头登记需要操作者和完整图像区域")
    if any(type(value) is not int for value in roi.values()):
        raise ValueError("探头区域必须使用整数像素")
    image = _image(frame)
    x, y, w, h = (roi[key] for key in ("x", "y", "width", "height"))
    if not (24 <= w <= 160 and 24 <= h <= 160
            and 0 <= x <= image.shape[1] - w and 0 <= y <= image.shape[0] - h):
        raise ValueError("探头区域不在画面内或大小不适合跟踪")
    binding = {key: frame.get(key) for key in BINDING_KEYS}
    if (any(value is None for value in binding.values())
            or frame.get("device_id") != device_id
            or type(frame.get("source_frame_seq")) is not int):
        raise ValueError("探头登记缺少可信摄像头绑定")
    patch = image[y:y + h, x:x + w]
    if float(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).std()) < 15:
        raise ValueError("探头区域纹理不足，不能使用纯色屏幕区域")
    _, encoded = cv2.imencode(".png", patch)
    record = {
        "schemaVersion": 1, "deviceId": device_id, "binding": binding,
        "operator": operator.strip(), "registeredAt": frame["captured_at"],
        "sourceFrameSha256": frame["sha256"], "sourceFrameSeq": frame["source_frame_seq"],
        "roi": roi, "template": base64.b64encode(encoded.tobytes()).decode("ascii"),
        "templateSha256": hashlib.sha256(encoded.tobytes()).hexdigest(),
    }
    tracker = ProbeTracker(record)
    point = tracker.locate(image)
    if math.dist(point[:2], (x + w / 2, y + h / 2)) > 2:
        raise ValueError("探头图像在当前画面不唯一，拒绝登记")
    path = Path(data_root) / "devices" / device_id / PROFILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return {key: value for key, value in record.items() if key != "template"}


class ProbeTracker:
    def __init__(self, profile: dict):
        self.profile = profile
        raw = base64.b64decode(profile["template"], validate=True)
        if hashlib.sha256(raw).hexdigest() != profile["templateSha256"]:
            raise ValueError("探头模板校验失败")
        self.template = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if self.template is None or min(self.template.shape[:2]) < 24:
            raise ValueError("探头模板无效")

    def locate(self, image) -> tuple[float, float, float]:
        # A fixed physical template, uniqueness and a color check keep phone
        # animations outside the registered patch from becoming motion evidence.
        result = cv2.matchTemplate(image, self.template, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(result)
        h, w = self.template.shape[:2]
        excluded = result.copy()
        excluded[max(0, y - h // 2):y + h // 2 + 1,
                 max(0, x - w // 2):x + w // 2 + 1] = -1
        if score < .72 or score - float(excluded.max()) < .08:
            raise ValueError("探头实体定位置信度不足或存在重复图案")
        patch = image[y:y + h, x:x + w]
        # The selected patch is centered on the probe. Compare its interior,
        # not the surrounding phone background, which may dim independently.
        core = (slice(h // 3, 2 * h // 3), slice(w // 3, 2 * w // 3))
        color_error = float(np.abs(patch[core].mean(axis=(0, 1))
                                  - self.template[core].mean(axis=(0, 1))).mean())
        if color_error > 45:
            raise ValueError(f"探头实体颜色不匹配（差值 {color_error:.1f}），不能用屏幕内容代替")
        return float(x + w / 2), float(y + h / 2), float(score)


def trace_verdict(points: list[dict], command: str, expected_delta=None) -> dict:
    """Verify visible physical displacement; touch counts remain macro reports."""
    if len(points) < 3:
        return {"verified": False, "reason": "动作中有效探头帧不足"}
    xy = np.array([[point["x"], point["y"]] for point in points])
    gaps = np.diff([point["at"] for point in points])
    if np.any(gaps > .30):
        return {"verified": False, "reason": "动作中摄像头采样间隔过大"}
    maximum = float(np.linalg.norm(xy - xy[0], axis=1).max())
    displacement = xy[-1] - xy[0]
    kind = command.split()[0]
    verified = maximum >= 3
    reason = "探头未出现可确认的实体位移"
    if kind in {"TAP", "TAP2"}:
        verified = verified and float(np.linalg.norm(displacement)) <= 5
        reason = "未拍到探头下压并回到悬停位置的完整过程"
        if kind == "TAP2" and verified:
            axis = (xy - xy[0])[np.argmax(np.linalg.norm(xy - xy[0], axis=1))] / maximum
            signal = (xy - xy[0]) @ axis
            prominence = max(.75, maximum * .08)
            releases = 0
            peak = 0.
            valley = None
            for value in signal[1:-1]:
                if valley is None:
                    peak = max(peak, value)
                    if peak >= maximum * .7 and peak - value >= prominence:
                        valley = value
                else:
                    valley = min(valley, value)
                    if value - valley >= prominence and value >= maximum * .7:
                        if valley >= maximum * .4:
                            releases += 1
                        peak, valley = value, None
            verified = releases == 1
            reason = "未拍到双击两次下压之间的短抬起，不确认双击完成"
    elif expected_delta is not None:
        expected = np.asarray(expected_delta, dtype=float)
        length = float(np.linalg.norm(expected))
        tolerance = max(8., length * .22)
        verified = (verified and length >= 1
                    and float(np.linalg.norm(displacement - expected)) <= tolerance
                    and float(np.dot(displacement, expected)) > 0)
        reason = "实体位移方向或终点与校准轨迹不一致"
    return {"verified": bool(verified), "reason": None if verified else reason,
            "measurement_kind": "registered_probe_trace", "sample_count": len(points),
            "max_excursion_px": round(maximum, 3),
            "displacement_px": displacement.round(3).tolist()}


class ProbeMotionVerifier(CameraMotionVerifier):
    measurement_kind = "registered_probe_trace"

    def __init__(self, camera, diff_threshold=1.5, *, settle_seconds=0.0,
                 evidence_dir=None, device_id=None, data_root=None, observation_only=False):
        super().__init__(camera, diff_threshold, settle_seconds=settle_seconds,
                         evidence_dir=evidence_dir)
        self.device_id = device_id or getattr(camera, "screen_device_id", None)
        self.data_root = Path(data_root or getattr(camera, "data_root", None)
                              or Path(__file__).resolve().parents[1] / "data")
        self._sampler = None
        self._failure = None
        self._points = []
        self._command = ""
        self._expected_delta = None
        self.observation_only = observation_only is True
        self._visibility_failure = None
        self._last_seq = None

    def readiness(self) -> list[str]:
        if self.observation_only:
            return []
        try:
            self._load_tracker()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return [f"探头实体运动跟踪尚未就绪：{exc}"]
        return []

    def _load_tracker(self):
        path = self.data_root / "devices" / str(self.device_id) / PROFILE_NAME
        return ProbeTracker(json.loads(path.read_text(encoding="utf-8")))

    def prepare(self, command: str, expected_delta=None) -> None:
        self._command = command
        self._expected_delta = expected_delta

    async def _sample(self):
        frame = await self._camera.capture()
        self._last_frame = frame
        if any(frame.get(key) != value for key, value in self._binding.items()):
            raise ValueError("实体核验摄像头绑定或画面尺寸已变化")
        seq = frame.get("source_frame_seq")
        if type(seq) is not int:
            raise ValueError("实体核验缺少新鲜帧序号")
        if frame.get("frozen"):
            raise ValueError("摄像头画面已冻结")
        if self._last_seq is not None and seq <= self._last_seq:
            return None
        self._last_seq = seq
        if self._evidence_dir:
            self._save_evidence(self._label, f"frame-{seq}", frame["image_bytes"])
        image = _image(frame)
        if self._tracker is None:
            return frame
        try:
            x, y, score = await asyncio.to_thread(self._tracker.locate, image)
        except (ValueError, cv2.error) as exc:
            if not self.observation_only:
                raise
            self._visibility_failure = str(exc)
            return frame
        self._points.append({"x": x, "y": y, "score": round(score, 4),
                             "seq": seq, "at": time.monotonic(),
                             "sha256": frame["sha256"]})
        return frame

    async def _collect(self):
        try:
            while True:
                await self._sample()
                await asyncio.sleep(.025)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = str(exc)

    async def before(self, label: str) -> dict:
        await self.abort()
        self._visibility_failure = None
        self._last_seq = None
        try:
            self._tracker = self._load_tracker()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            if not self.observation_only:
                raise
            self._tracker = None
            self._visibility_failure = f"探头影像未登记或不可用：{exc}"
        if self.observation_only:
            # Debug gestures use the existing calibration as the source identity;
            # an obsolete probe photo is evidence, never a new calibration gate.
            bundle = calibration_bundle.load_bundle(self.device_id, self.data_root)
            self._binding = {
                "device_id": self.device_id, "camera_id": bundle["cameraId"],
                "device_unique_id": bundle["cameraDeviceUniqueId"],
                "binding_revision": bundle["bindingRevision"], **bundle["frame"],
            }
            if self._tracker and any(
                    self._tracker.profile["binding"].get(key) != self._binding.get(key)
                    for key in BINDING_KEYS):
                self._tracker = None
                self._visibility_failure = "探头照片属于旧摄像头绑定，本次仅保留画面证据"
        else:
            self._binding = self._tracker.profile["binding"]
        self._points, self._failure = [], None
        self._label = label
        frame = await self._sample()
        self._started = time.monotonic()
        saved = self._save_evidence(label, "before", frame["image_bytes"])
        self._sampler = asyncio.create_task(self._collect())
        return {"label": label, "sha256": frame["sha256"], "captured_at": frame["captured_at"],
                "evidence": saved}

    async def after(self, label: str) -> dict:
        await self.abort()
        if self._settle:
            await asyncio.sleep(min(self._settle, .15))
        try:
            frame = await self._sample()
        except Exception as exc:
            self._failure = self._failure or str(exc)
            frame = self._last_frame
        saved = self._save_evidence(label, "after", frame["image_bytes"]) if frame else None
        result = trace_verdict(self._points, self._command, self._expected_delta)
        if self._visibility_failure:
            result.update(verified=False, reason=self._visibility_failure)
        if self._failure:
            result.update(verified=False, reason=self._failure, stop_required=True)
        result.update(phase=label, trace=self._points, evidence=saved)
        self.history.append(result)
        if self._evidence_dir:
            safe = label.replace(":", "_").replace("/", "_")
            (self._evidence_dir / f"{safe}-trace.json").write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result

    async def abort(self):
        if self._sampler is not None:
            self._sampler.cancel()
            try:
                await self._sampler
            except asyncio.CancelledError:
                pass
            self._sampler = None
