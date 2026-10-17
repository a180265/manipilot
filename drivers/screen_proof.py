"""Device-bound visual evidence profiles. No application or customer decisions."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

import cv2
import numpy as np

from drivers import calibration_bundle as cb
from runtime.util import iso, utc_now


def profile_path(device_id, data_root=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", device_id):
        raise ValueError("设备编号无效")
    root = Path(data_root) if data_root else Path(__file__).resolve().parents[1] / "data"
    return root / "devices" / device_id / "screen_proof.json"


def binding(device_id, data_root=None, *, allow_motion_ready=True):
    # motion_ready（自动校准晋级态）视同就绪：发送门禁证据的登记/读取
    # 必须对晋级档案可用（fleet 读端 2026-10-17 已同口径），默认放行。
    bundle = cb.load_bundle(device_id, data_root)
    statuses = {"ready", cb.MOTION_READY_STATUS} if allow_motion_ready else {"ready"}
    if not bundle or bundle.get("status") not in statuses or bundle.get("deviceId") != device_id:
        raise ValueError("本设备当前校准尚未就绪")
    fields = {key: bundle.get(key) for key in (
        "deviceId", "cameraId", "cameraDeviceUniqueId", "bindingRevision",
        "daemonSession", "frame", "camera", "screen")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest(), bundle


def load_profile(device_id, data_root=None):
    path = profile_path(device_id, data_root)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    fingerprint, _ = binding(device_id, data_root)
    if value.get("binding") != fingerprint or value.get("device_id") != device_id:
        raise ValueError("页面识别档案与当前校准不一致，请重新登记")
    return value


@contextmanager
def _edit(device_id, operator, data_root=None):
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("请填写操作人")
    path = profile_path(device_id, data_root)
    fingerprint, _ = binding(device_id, data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        previous_revision = value.get("revision", 0)
        if value.get("binding") != fingerprint:
            value = {}
        value.update(contract_version="screen-proof.v1", device_id=device_id,
                     binding=fingerprint, revision=previous_revision + 1,
                     updated_at=iso(utc_now()), operator=operator.strip())
        yield value
        value.setdefault("audit", []).append({
            "revision": value["revision"], "operator": value["operator"],
            "at": value["updated_at"]})
        fd, filename = tempfile.mkstemp(dir=path.parent, prefix=".screen-proof-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, path)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)


def decode(image):
    frame = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("画面无法解码")
    return frame


def bounds(region, width, height):
    if not isinstance(region, dict) or set(region) != {"x1", "y1", "x2", "y2", "frameW", "frameH"}:
        raise ValueError("区域需要完整画面尺寸及两个角点")
    if region["frameW"] != width or region["frameH"] != height:
        raise ValueError("登记区域画面尺寸不一致")
    numbers = [region[key] for key in ("x1", "y1", "x2", "y2")]
    if any(isinstance(x, bool) or not isinstance(x, (float, int)) or not np.isfinite(x) for x in numbers):
        raise ValueError("区域坐标必须为有限数字")
    if not (0 <= numbers[0] < numbers[2] <= width
            and 0 <= numbers[1] < numbers[3] <= height):
        raise ValueError("登记区域必须完整位于画面内，不能截断越界坐标")
    x1, y1, x2, y2 = [int(x) for x in numbers]
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height) or min(x2-x1, y2-y1) < 4:
        raise ValueError("登记区域必须完全位于画面内且至少四像素")
    return x1, y1, x2, y2


def crop(frame, region):
    x1, y1, x2, y2 = bounds(region, frame.shape[1], frame.shape[0])
    return frame[y1:y2, x1:x2]


def encode(frame):
    return base64.b64encode(cv2.imencode(".png", frame)[1].tobytes()).decode()


def _check_frame(device_id, frame, data_root):
    _, bundle = binding(device_id, data_root)
    if bundle.get("frame") != {"width": frame.shape[1], "height": frame.shape[0]}:
        raise ValueError("登记画面尺寸与当前校准不一致")
    return bundle


def _registration_frame(device_id, image, regions, data_root):
    from drivers import screen_pose
    from runtime.calibration_gate import _quad_of

    frame = decode(image)
    bundle = _check_frame(device_id, frame, data_root)
    height, width = frame.shape[:2]
    saved = bundle["screen"]["edgeModel"]["corners"]
    live = _quad_of(image, saved)
    if live is None:
        raise ValueError("当前画面未定位到手机屏幕，不能登记")
    pose = screen_pose.fit_pose(saved, live)
    inverse = np.linalg.inv(np.asarray(pose["matrix"], dtype=float))
    transformed = {}
    for name, region in regions.items():
        x1, y1, x2, y2 = bounds(region, width, height)
        points = np.array([[x1, y1, 1], [x2, y1, 1],
                           [x2, y2, 1], [x1, y2, 1]], dtype=float) @ inverse.T
        points = np.round(points[:, :2] / points[:, 2:], 6)
        left, top = np.floor(points.min(axis=0)).astype(int)
        right, bottom = np.ceil(points.max(axis=0)).astype(int)
        rect = dict(x1=int(left), y1=int(top), x2=int(right), y2=int(bottom),
                    frameW=width, frameH=height)
        bounds(rect, width, height)
        polygon = np.asarray(saved, dtype=np.float32)
        if any(cv2.pointPolygonTest(polygon, (float(x), float(y)), False) < 0
               for x in (left, right) for y in (top, bottom)):
            raise ValueError("登记区域超出已校准手机屏幕")
        transformed[name] = rect
    return screen_pose.align_screen(frame, saved, pose), transformed


def recognize_patch(engine, patch, region):
    """Reject OCR coordinates that cannot be evidence for the supplied crop."""
    result = engine.recognize(cv2.imencode(".png", patch)[1].tobytes(), region=region)
    value = result.to_dict()
    height, width = patch.shape[:2]
    if (value.get("truncated")
            or type(value.get("width")) is not int or value["width"] != width
            or type(value.get("height")) is not int or value["height"] != height):
        raise ValueError("区域 OCR 被截断或坐标尺寸与实际裁图不一致")

    def finite(number):
        return (not isinstance(number, bool) and isinstance(number, (int, float))
                and math.isfinite(number))

    confidence = value.get("confidence")
    if not finite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("区域 OCR 置信度无效")
    for word in value.get("words", []):
        x, y, w, h, score = [word.get(k) for k in
                              ("x", "y", "width", "height", "confidence")]
        if (not all(finite(v) for v in (x, y, w, h, score))
                or not (0 <= x < x + w <= width and 0 <= y < y + h <= height)
                or not 0 <= score <= 1):
            raise ValueError("区域 OCR 文字坐标超出实际裁图或数值无效")
    return value


def input_is_empty(actual, reference, text):
    if actual.shape != reference.shape:
        raise ValueError("空输入参考图尺寸与实际输入区域不一致")
    difference = np.max(np.abs(actual.astype(float) - reference.astype(float)), axis=2)
    return bool(not text.strip() and float(difference.mean()) < 5
                and float(np.mean(difference > 35)) < .01)


def save_reference(device_id, *, state, label, region, image, operator, data_root=None):
    if not isinstance(state, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", state):
        raise ValueError("页面状态编号无效")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("请填写页面中文名称")
    frame, mapped = _registration_frame(device_id, image, {"reference": region}, data_root)
    region = mapped["reference"]
    patch = crop(frame, region)
    if patch.std() < 10 or np.mean(cv2.Canny(patch, 50, 150) > 0) < .005:
        raise ValueError("选区缺少可区分页面的稳定图形特征")
    with _edit(device_id, operator, data_root) as value:
        value.setdefault("references", {})[state] = {
            "label": label.strip(), "region": region, "image": encode(patch)}
        value["coordinate_version"] = "calibrated_camera.v1"
    return value


def match_reference(frame, reference):
    actual = cv2.cvtColor(crop(frame, reference["region"]), cv2.COLOR_BGR2GRAY)
    saved = cv2.cvtColor(decode(base64.b64decode(reference["image"])), cv2.COLOR_BGR2GRAY)
    if actual.shape != saved.shape:
        return 0.0
    # Local structural correlation resists small brightness changes, but a
    # dark/blank occlusion is never a match even against a bright reference.
    if actual.std() < 8 or actual.mean() < 20:
        return 0.0
    score = float(cv2.matchTemplate(actual, saved, cv2.TM_CCOEFF_NORMED)[0, 0])
    error = float(np.abs(actual.astype(float) - saved.astype(float)).mean())
    return max(0.0, min(score, 1.0 - error / 128))


def identify(frame, profile):
    ranked = sorted(((match_reference(frame, ref), state, ref["label"])
                     for state, ref in profile.get("references", {}).items()), reverse=True)
    if not ranked or ranked[0][0] < .90 or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .08):
        return {"ui_state": "unknown", "ui_state_display": "无法确认",
                "confidence": 0.0, "note": "页面特征不足或多个页面相似，请人工核对"}
    score, state, label = ranked[0]
    return {"ui_state": state, "ui_state_display": label, "confidence": round(score, 3),
            "profile_revision": profile["revision"]}


def save_regions(device_id, *, regions, image, operator, data_root=None):
    required = {"title", "input", "content", "foreground_sample", "background_sample"}
    if not isinstance(regions, dict) or set(regions) != required:
        raise ValueError("请完整登记标题、输入框、内容区域及两种颜色样本")
    frame, regions = _registration_frame(device_id, image, regions, data_root)
    patches = {name: crop(frame, region) for name, region in regions.items()}
    for left, right in (("title", "input"), ("title", "content"), ("input", "content")):
        a, b = regions[left], regions[right]
        if max(a["x1"], b["x1"]) < min(a["x2"], b["x2"]) and max(a["y1"], b["y1"]) < min(a["y2"], b["y2"]):
            raise ValueError("标题、输入框与消息内容区域不能重叠")
    colors = {}
    for name in ("foreground_sample", "background_sample"):
        pixels = patches[name].reshape(-1, 3).astype(float)
        colors[name] = np.median(pixels, axis=0).tolist()
        if np.max(pixels.std(axis=0)) > 15:
            raise ValueError("颜色样本必须是无文字、无边缘的纯色小块")
    if np.linalg.norm(np.array(colors["foreground_sample"]) - colors["background_sample"]) < 60:
        raise ValueError("内容底色与背景色过于相似，不能用于证明新增内容")
    with _edit(device_id, operator, data_root) as value:
        value.update(regions=regions, colors=colors, empty_input=encode(patches["input"]))
        value["coordinate_version"] = "calibrated_camera.v1"
    return value


def colored_components(frame, profile):
    """Extract connected colored panels and raw clearance evidence, not messages."""
    region = profile["regions"]["content"]
    patch = crop(frame, region)
    color = np.asarray(profile["colors"]["foreground_sample"])
    background = np.asarray(profile["colors"]["background_sample"])
    mask = (np.max(np.abs(patch.astype(float) - color), axis=2) < 28).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    if count > 80:
        raise ValueError("内容色块过多，画面不适合可靠核对")
    components = []
    for x, y, width, height, area in stats[1:]:
        x, y, width, height, area = map(int, (x, y, width, height, area))
        if area < 160 or width < 25 or height < 12 or area / (width * height) < .45:
            continue
        left = patch[y:y+height, max(0, x-40):max(0, x-4)]
        clearance = bool(x >= 40 and left.size and
                         np.mean(np.max(np.abs(left.astype(float) - background), axis=2) > 30) < .01)
        components.append({
            "x": x + int(region["x1"]), "y": y + int(region["y1"]),
            "width": width, "height": height,
            "left_margin_clear": clearance,
            "clipped": x <= 0 or y <= 0 or x+width >= patch.shape[1] or y+height >= patch.shape[0],
            "image": patch[y:y+height, x:x+width],
        })
    if len(components) > 20:
        raise ValueError("可见内容块过多，无法在一次有限核对内确认")
    return components
