"""A manual wake may reuse only a point verified in the current calibration."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

import cv2
import numpy as np

from drivers import calibration_bundle as cb, screen_gestures as sg
from drivers.arm import DriverError
from drivers.real_camera import RealCameraDriver, ScreenRoiNotFound

# 已发布校准 bundle 中本轮“确认点击参数/测试点击”锚点的键（W01）。
# 校准向导提升时写入；读取方容错缺键——没有锚点时按 W02 规则回退到同轮
# 独立验证点。锚点字段：anchorId、runId、verified、touchTargetErrorPx、
# u/v（或 screenX/screenY + screenWidth/screenHeight）、deviceId、cameraId、
# daemonSession、sampleId、source、ts。
TAP_ANCHOR_KEY = "wakeTapAnchor"
# §9.1(3)：锚点屏幕误差不超过本轮验证 tolerancePx 且不超过 10px。
TAP_ANCHOR_MAX_ERROR_PX = 10.0
# §9.2：与当前校准绑定的双击速度验收证据键。向导/校准流程提升时写入；
# 缺键或与当前 run 不绑定时，双击沿用校准速度，不因 daemon 上报快双击
# 能力而私自提速（W03）。
DOUBLE_TAP_SPEED_EVIDENCE_KEY = "doubleTapSpeedAcceptance"


class WakeScreenNotLocated(ValueError):
    """The wake frame is intact, but the phone cannot be located safely."""


def _tap_anchor_point(anchor: dict, bundle: dict) -> dict:
    if anchor.get("u") is not None or anchor.get("v") is not None:
        return {"x": sg.number(anchor.get("u"), "点击锚点横向位置"),
                "y": sg.number(anchor.get("v"), "点击锚点纵向位置")}
    screen = bundle.get("screen") or {}
    width = sg.number(
        anchor.get("screenWidth") or screen.get("screenWidth"), "点击锚点屏幕宽")
    height = sg.number(
        anchor.get("screenHeight") or screen.get("screenHeight"), "点击锚点屏幕高")
    if width <= 0 or height <= 0:
        raise ValueError("点击锚点缺少屏幕尺寸，无法归一化")
    return {"x": sg.number(anchor.get("screenX"), "点击锚点屏幕横坐标") / width,
            "y": sg.number(anchor.get("screenY"), "点击锚点屏幕纵坐标") / height}


def _resolve_tap_anchor(bundle: dict, run, tolerance: float, binding: dict) -> dict | None:
    """优先使用本轮真实触点建立的点击锚点（W01）；缺键时返回 None 走 W02。"""
    if TAP_ANCHOR_KEY not in bundle or bundle.get(TAP_ANCHOR_KEY) is None:
        return None
    anchor = bundle.get(TAP_ANCHOR_KEY)
    if not isinstance(anchor, dict):
        raise ValueError("点击锚点记录无效")
    identifier = anchor.get("anchorId")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("点击锚点缺少编号")
    if anchor.get("runId") != run:
        raise ValueError("点击锚点不属于本次校准，不能作为唤醒点")
    if anchor.get("verified") is not True:
        raise ValueError("点击锚点未通过验证")
    error = sg.number(anchor.get("touchTargetErrorPx"), "点击锚点触点误差")
    if not 0 <= error <= min(float(tolerance), TAP_ANCHOR_MAX_ERROR_PX):
        raise ValueError("点击锚点未通过发布模型回算")
    point = _tap_anchor_point(anchor, bundle)
    sg.resolve_point({"source": "screen_normalized", **point}, bundle)
    for field, expected in (("deviceId", bundle.get("deviceId")),
                            ("cameraId", bundle.get("cameraId")),
                            ("daemonSession", bundle.get("daemonSession"))):
        value = anchor.get(field)
        if value is not None and value != expected:
            raise ValueError("点击锚点与当前设备、摄像头或会话绑定不符")
    return {
        "validation_point_id": identifier, "point": point, "binding": binding,
        "target_source": "wake_tap_anchor",
        "anchor": {"anchorId": identifier, "runId": run,
                   "sampleId": anchor.get("sampleId"),
                   "source": anchor.get("source"), "verifiedAt": anchor.get("ts")},
    }


def resolve_wake_target(bundle: dict) -> dict:
    binding = sg.binding_snapshot(bundle)
    checks = cb.validate_calibration_bundle(
        bundle, device_id=bundle.get("deviceId"), camera_id=bundle.get("cameraId"),
        camera_device_unique_id=bundle.get("cameraDeviceUniqueId"),
        binding_revision=bundle.get("bindingRevision"), daemon_addr=bundle.get("daemonAddr"),
        daemon_session=bundle.get("daemonSession"),
        frame_width=binding["frame_width"], frame_height=binding["frame_height"],
    )
    if checks:
        raise ValueError("；".join(item["message"] for item in checks))
    validation = bundle.get("validation") or {}
    run = (bundle.get("autoCalibration") or {}).get("runId")
    if (not isinstance(validation, dict) or validation.get("passed") is not True
            or not run or validation.get("runId") != run):
        raise ValueError("唤醒需要本次校准已通过的独立验证点，不使用默认位置")
    tolerance = sg.number(validation.get("tolerancePx"), "验证点误差上限")
    if tolerance <= 0:
        raise ValueError("独立验证点误差上限无效")
    anchor = _resolve_tap_anchor(bundle, run, tolerance, binding)
    if anchor is not None:
        return anchor
    candidates = []
    for entry in validation.get("points") or []:
        if not isinstance(entry, dict) or entry.get("passed") is not True:
            continue
        identifier = entry.get("targetId")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("独立验证点缺少编号")
        error = sg.number(entry.get("touchTargetErrorPx"), "验证点触点误差")
        if not 0 <= error <= tolerance:
            raise ValueError("独立验证点误差未通过")
        point = {"x": sg.number(entry.get("u"), "验证点横向位置"),
                 "y": sg.number(entry.get("v"), "验证点纵向位置")}
        sg.resolve_point({"source": "screen_normalized", **point}, bundle)
        candidates.append((math.hypot(point["x"] - .5, point["y"] - .5),
                           identifier, point))
    if not candidates or len({item[1] for item in candidates}) != len(candidates):
        raise ValueError("本次校准没有唯一且通过的唤醒验证点")
    _, identifier, point = min(candidates, key=lambda item: (item[0], item[1]))
    return {"validation_point_id": identifier, "point": point, "binding": binding,
            "target_source": "validation_point"}


def accepted_double_tap_feed(bundle: dict) -> float | None:
    """读取与当前校准绑定的双击速度验收证据（W03）。

    只有证据存在、明确通过且 runId 与当前自动校准一致时才返回提速值；
    其他情况（含缺键）返回 None，调用方必须沿用校准速度。"""
    evidence = bundle.get(DOUBLE_TAP_SPEED_EVIDENCE_KEY)
    if not isinstance(evidence, dict):
        return None
    run = (bundle.get("autoCalibration") or {}).get("runId")
    if not run or evidence.get("runId") != run or evidence.get("passed") is not True:
        return None
    try:
        feed = sg.number(evidence.get("feed"), "双击速度验收")
    except (ValueError, TypeError):
        return None
    from drivers.daemon_protocol import DOUBLE_TAP_FEED_MAX

    if feed <= 0 or feed > DOUBLE_TAP_FEED_MAX:
        return None
    return feed


def wake_frame_quality(frame: dict, binding: dict | None = None,
                       *, screen_corners=None, require_phone: bool = False) -> str:
    """Require a fresh, identifiable source frame even when all its pixels are dark."""
    if not isinstance(frame, dict) or frame.get("frozen") or frame.get("source_legacy_mode"):
        raise ValueError("唤醒画面冻结或缺少正式采集来源")
    seq = frame.get("source_frame_seq")
    if type(seq) is not int or seq < 0:
        raise ValueError("唤醒画面缺少有效来源帧序号")
    try:
        timestamps = []
        for field in ("source_captured_at", "source_received_at"):
            value = frame.get(field)
            if value:
                moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    raise ValueError("missing timezone")
                timestamps.append((field, moment))
        if not timestamps:
            raise ValueError("missing source timestamp")
        now = datetime.now(timezone.utc)
        for _field, moment in timestamps:
            age = (now - moment).total_seconds()
            if not -.5 <= age <= 2:
                raise ValueError("stale frame")
        if len(timestamps) == 2:
            captured = dict(timestamps)["source_captured_at"]
            received = dict(timestamps)["source_received_at"]
            if not -.5 <= (received - captured).total_seconds() <= 2:
                raise ValueError("inconsistent source timestamps")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("唤醒画面已过期或采集时间不可确认") from exc
    if binding is not None:
        fields = {"device_id": "device_id", "camera_id": "camera_id",
                  "device_unique_id": "camera_device_unique_id",
                  "binding_revision": "binding_revision",
                  "width": "frame_width", "height": "frame_height"}
        if any(type(frame.get(key)) is not type(binding[name])
               or frame.get(key) != binding[name] for key, name in fields.items()):
            raise ValueError("唤醒画面与当前校准设备、摄像头或尺寸不符")
    image = frame.get("image_bytes") or frame.get("image")
    try:
        decoded = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        if decoded is None or (decoded.shape[1], decoded.shape[0]) != (
                frame.get("width"), frame.get("height")):
            raise ValueError("invalid dimensions")
        phone_located = True
        try:
            metrics = RealCameraDriver._analyze(
                image, expected_corners=screen_corners,
                tolerance_px=18, require_phone=require_phone)
        except ScreenRoiNotFound:
            # During the dark-screen phase the calibrated phone rectangle is
            # the fallback observation region.  It lets us distinguish a
            # sleeping display from an unrelated bright scene without making
            # screen-edge detection a prerequisite for the wake tap.
            phone_located = False
            metrics = None
            if screen_corners is not None:
                corners = np.asarray(screen_corners, dtype=np.int32)
                if corners.shape == (4, 2) and np.isfinite(corners).all():
                    mask = np.zeros(decoded.shape[:2], np.uint8)
                    cv2.fillConvexPoly(mask, corners, 255)
                    pixels = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)[mask != 0]
                    if pixels.size:
                        metrics = {
                            "blur_variance": 0.0,
                            "brightness": float(pixels.mean()),
                            "texture": float(pixels.std()),
                        }
            if metrics is None:
                gray = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)
                metrics = {
                    "blur_variance": 0.0,
                    "brightness": float(gray.mean()),
                    "texture": float(gray.std()),
                }
        quality = RealCameraDriver._classify(metrics, False)
        if require_phone and not phone_located and quality != "waiting":
            # W04/§9.4：仍为有效暗屏（waiting）时双击后样本保留、返回等待；
            # 只有亮屏/异常画面且无法定位手机几何时才拒绝。
            raise ScreenRoiNotFound("wake phone not located")
    except ScreenRoiNotFound as exc:
        raise WakeScreenNotLocated("唤醒画面无法定位手机屏幕，等待画面恢复") from exc
    except (RuntimeError, ValueError, TypeError, cv2.error) as exc:
        raise ValueError("唤醒画面无法解码或尺寸不符") from exc
    if quality not in {"waiting", "clear"}:
        raise ValueError("唤醒画面过曝或异常，不能借黑屏等待放行动作")
    return quality


def wake_target_digest(target: dict) -> str:
    """唤醒点+绑定+参数摘要：人工确认与执行用同一指纹核对参数一致性。"""
    import json

    return hashlib.sha256(json.dumps(
        [target.get("validation_point_id"), target.get("point"), target.get("binding")],
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_manual_confirmation(record, target: dict, device_id: str) -> dict | None:
    """核对单次人工现场确认（W13/§9.3）是否仍授权本任务豁免实时姿态。

    过期、设备/绑定/参数摘要不一致一律返回 None（确认撤销，姿态门禁照常）。
    该授权只豁免实时手机姿态，不豁免新鲜源帧、坐标基准、急停或租约。"""
    if not isinstance(record, dict):
        return None
    if record.get("device_id") != device_id:
        return None
    binding = record.get("binding")
    if not isinstance(binding, dict) or binding != target.get("binding"):
        return None
    if record.get("target_digest") != wake_target_digest(target):
        return None
    try:
        moment = datetime.fromisoformat(str(record.get("expires_at")).replace("Z", "+00:00"))
        if moment.tzinfo is None or moment <= datetime.now(timezone.utc):
            return None
    except (AttributeError, ValueError, TypeError):
        return None
    return record


class WakeScreenObserver:
    """Observe wake effects without claiming that page changes prove arm motion."""
    observation_only = True

    def __init__(self, gate, bundle_provider, target: dict, bundle: dict,
                 *, require_live_pose: bool = True):
        self.gate = gate
        self.bundle_provider = bundle_provider
        self.target = target
        self.validation_hash = cb.profile_hash(bundle, keys=("autoCalibration", "validation"))
        self.frames: list[dict] = []
        self.pressed = False
        self.pose: dict | None = None
        # §9.4/W14：awake 需要亮屏帧同时具备可信实时姿态；仅亮但姿态
        # 不可确认时保留 unverified，不把亮屏事实改判为已验证。
        self.pose_verified_seq: int | None = None
        # W13/§9.3(6)：单次人工现场确认只豁免本任务的实时手机姿态（含双击后
        # 亮屏帧的姿态要求）；新鲜帧、绑定与帧推进校验照常。
        self.require_live_pose = require_live_pose

    def _adopt_pose(self, pose: dict, source_seq: int | None = None) -> None:
        if self.pose is not None:
            corners = np.asarray(pose["corners"], dtype=float)
            initial = np.asarray(self.pose["corners"], dtype=float)
            if (corners.shape != (4, 2) or initial.shape != (4, 2)
                    or not np.isfinite(corners).all()
                    or np.max(np.linalg.norm(corners - initial, axis=1)) > 2):
                raise ValueError("唤醒期间手机位置变化，禁止沿用原点击坐标")
        else:
            self.pose = pose
        if source_seq is not None:
            self.pose_verified_seq = source_seq

    async def sample(self, *, require_dark: bool = False) -> dict:
        if not callable(self.gate):
            raise DriverError("driver_policy_rejected", "唤醒缺少专用新帧校准检查，未执行动作")
        requested_pose = self.pressed
        try:
            checks, frame, _ = await self.gate(require_pose=requested_pose)
        except TypeError as exc:
            # Deployed callers may still expose the original no-argument gate.
            # Only fall back for an unsupported keyword; do not hide an
            # implementation TypeError raised inside the gate itself.
            if "require_pose" not in str(exc):
                raise
            checks, frame, _ = await self.gate()
        if checks:
            raise DriverError("driver_policy_rejected", "；".join(
                f"{item['code']}：{item['message']}" for item in checks))
        try:
            bundle = self.bundle_provider()
            if (resolve_wake_target(bundle) != self.target
                    or cb.profile_hash(bundle, keys=("autoCalibration", "validation"))
                    != self.validation_hash):
                raise ValueError("唤醒验证点或校准已变化")
            quality = wake_frame_quality(
                frame, self.target["binding"],
                screen_corners=(bundle.get("screen") or {}).get("edgeModel", {}).get("corners"),
                require_phone=requested_pose)
            if require_dark and quality != "waiting":
                raise ValueError("当前画面并非黑屏等待，不执行唤醒双击")
            if self.frames and frame["source_frame_seq"] <= self.frames[-1]["source_frame_seq"]:
                raise ValueError("唤醒画面序号未推进")
            pose = frame.get("_screen_pose")
            if requested_pose:
                # §9.4/W04：双击后仅亮屏帧要求实时姿态；有效暗屏样本保留，
                # 继续校验帧推进与绑定，不因仍暗屏否定控制器已确认的完成。
                if quality == "clear":
                    if isinstance(pose, dict):
                        self._adopt_pose(pose, frame["source_frame_seq"])
                    elif self.require_live_pose:
                        raise ValueError("双击后手机实时位置不可确认")
            elif require_dark:
                # §9.3/W08：暗屏阶段尽力消费可观测姿态（机身/屏幕四边仍可
                # 定位时），供双击前补偿；不可观测时保持等待语义，不伪造姿态。
                if isinstance(pose, dict):
                    self._adopt_pose(pose)
        except (ValueError, TypeError, KeyError) as exc:
            raise DriverError("driver_policy_rejected", str(exc)) from exc
        evidence = {
            "quality": quality, "source_frame_seq": frame["source_frame_seq"],
            "captured_at": frame.get("source_captured_at"),
            "sha256": hashlib.sha256(frame.get("image_bytes") or frame["image"]).hexdigest(),
        }
        self.frames.append(evidence)
        return evidence

    async def before(self, label: str) -> None:
        await self.sample(require_dark=not self.pressed)

    async def after(self, label: str) -> dict:
        if label == "double_tap":
            self.pressed = True
        evidence = await self.sample()
        return {"verified": False, "measurement_kind": "wake_frame_observation",
                "reason": "画面证据只证明亮暗变化，不独立证明机械臂运动", **evidence}

    def effect_status(self) -> str:
        if (self.frames and self.pressed and self.frames[0]["quality"] == "waiting"
                and self.frames[-1]["quality"] == "clear"
                and self.pose_verified_seq == self.frames[-1]["source_frame_seq"]
                and self.frames[-1]["source_frame_seq"] > self.frames[0]["source_frame_seq"]):
            return "awake"
        return "unverified"
