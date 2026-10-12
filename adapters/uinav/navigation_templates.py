"""只读采帧与显式模板登记；不执行动作、不改变 unknown 事实。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import fcntl
import hashlib
import json
from pathlib import Path
import re
import uuid

import cv2
import jsonschema
import numpy as np

from drivers import calibration_bundle as cb, gesture_program as gp
from runtime import automatic_gestures as ag
from runtime.errors import ApiError


CONTEXT_FIELDS = {"operator_id", "role", "reason", "correlation_id",
                  "client_request_id", "confirmation_token"}


def _now():
    return datetime.now(timezone.utc)


@lru_cache(maxsize=None)
def _validator(definition):
    path = Path(__file__).resolve().parents[2] / "api/contracts/navigation-template.v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(
        {**schema, "$ref": f"#/$defs/{definition}"},
        format_checker=jsonschema.FormatChecker())


def request_payload(body, definition):
    if not isinstance(body, dict):
        raise ApiError("invalid_request", "模板请求必须为对象")
    payload = {key: value for key, value in body.items() if key not in CONTEXT_FIELDS}
    if not _validator(definition).is_valid(payload):
        raise ApiError("invalid_request", "模板请求字段、确认或区域格式不符合契约")
    if definition == "registrationRequest" and (
            type(payload["expected_version"]) is not int
            or any(type(value) is not int for region in payload["anchor_regions"]
                   for value in region.values())):
        raise ApiError("invalid_request", "版本和像素区域必须使用整数")
    return payload


def load_template_patch(entry, *, kind, device_id, binding, evidence_root,
                        screen_size=None, evidence_get=None):
    """Read-only integrity check; return the saved crop, never recrop a source.

    Callers supply the configured evidence root, not a data-root-derived guess.
    Runtime gates also supply the authoritative evidence lookup to check ownership.
    No captures, writes, repairs, or cross-call caches are permitted here.
    """
    try:
        if (kind not in ("top", "launcher") or evidence_root is None
                or binding["device_id"] != device_id):
            raise ValueError("模板证据根或设备绑定无效")
        root = Path(evidence_root).resolve(strict=True)
        region = entry["region"]
        x, y, w, h = (region[k] for k in ("left_px", "top_px", "width_px", "height_px"))
        if (any(type(v) is not int for v in (x, y, w, h))
                or min(x, y) < 0 or min(w, h) <= 0
                or (screen_size is not None and
                    (x + w > screen_size[0] or y + h > screen_size[1]))):
            raise ValueError("模板裁剪区域无效")

        def image(evidence_id, digest, shape, source=None):
            if (not isinstance(evidence_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", evidence_id)
                    or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("模板证据编号或摘要无效")
            relative = f"navtpl-{kind}/{evidence_id}.png"
            path = root / relative
            if path.resolve(strict=True) != path or not path.is_file():
                raise ValueError("模板证据路径不安全")
            if evidence_get is not None:
                record = evidence_get(evidence_id)
                expected = {"evidence_id": evidence_id, "device_id": device_id,
                            "task_id": f"navtpl-{kind}", "file_path": relative,
                            "sha256": digest, "kind": "screenshot" if source else "file"}
                if source:
                    expected.update(camera_id=binding["camera_id"],
                                    captured_at=source["captured_at"],
                                    height=shape[0], width=shape[1])
                if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
                    raise ValueError("模板证据记录缺失或归属不符")
            data = path.read_bytes()
            if (not data.startswith(b"\x89PNG\r\n\x1a\n")
                    or hashlib.sha256(data).hexdigest() != digest):
                raise ValueError("模板证据摘要不符")
            decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
            if decoded is None or decoded.shape != shape:
                raise ValueError("模板证据图像尺寸不符")
            return decoded

        sources = entry["frame_evidence"]
        if len(sources) != 3 or len({ref["evidence_id"] for ref in sources}) != 3:
            raise ValueError("模板原图证据不完整")
        for ref in sources:
            if (ref["kind"] != "image" or ref["camera_id"] != binding["camera_id"]
                    or ref["calibration_fingerprint"] != binding["calibration_fingerprint"]):
                raise ValueError("模板原图绑定不符")
            image(ref["evidence_id"], ref["sha256"],
                  (binding["frame_height"], binding["frame_width"]), ref)
        if entry["evidence_id"] in {ref["evidence_id"] for ref in sources}:
            raise ValueError("裁剪不能引用原图证据")
        return image(entry["evidence_id"], entry["crop_sha256"], (h, w))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, cv2.error) as exc:
        raise ValueError("模板证据缺失、损坏、归属不符或路径不安全") from exc


class NavigationTemplates:
    def __init__(self, runtime, device_id):
        if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", device_id):
            raise ApiError("invalid_request", "设备编号无效")
        self.runtime = runtime
        self.device_id = device_id
        self.root = runtime._gesture_data_root(device_id)
        self.directory = self.root / "devices" / device_id / "navigation_templates"

    @staticmethod
    def _kind(kind):
        if kind not in ("top", "launcher"):
            raise ApiError("invalid_request", "模板编号必须为 top 或 launcher")
        return kind

    def _snapshot(self):
        bundle = cb.load_bundle(self.device_id, self.root)
        frame = gp.load_coordinate_frame(self.device_id, self.root)
        settings = ag.load_settings(self.device_id, self.root)
        binding, reasons = ag._current_binding(bundle, frame, settings["gesture_rules_version"])
        if binding is None:
            raise ApiError("calibration_unavailable", "；".join(r["message"] for r in reasons))
        profile = ag.load_profile(self.device_id, self.root)
        if profile is not None:
            version = profile.get("version")
            if type(version) is not int or version < 1:
                raise ApiError("gesture_profile_invalid", "自动手势档案版本无效")
            binding["gesture_profile_version"] = version
        live = self.runtime.devices.binding_view(self.device_id)
        current = {**live["binding"], "binding_revision": live["binding_revision"],
                   "daemon_session": live["daemon_session"]}
        if (binding["device_id"] != self.device_id or not live["binding_enabled"]
                or any(current.get(key) != binding[key] for key in (
                    "camera_id", "camera_device_unique_id", "binding_revision", "daemon_session"))):
            raise ApiError("gesture_binding_changed", "当前设备绑定与校准不一致")
        try:
            size = (bundle["screen"]["screenWidth"], bundle["screen"]["screenHeight"])
            matrix = np.asarray(bundle["camera"]["cameraToScreen"]["homography"], dtype=float).reshape(3, 3)
            if (not all(type(v) is int and 100 <= v <= 10000 for v in size)
                    or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12):
                raise ValueError("校准平面无效")
        except (ValueError, TypeError, KeyError) as exc:
            raise ApiError("calibration_invalid", "屏幕校准平面无效") from exc
        return binding, matrix, size

    def _same_binding(self, binding):
        current = self._snapshot()
        if not ag._same_json(current[0], binding):
            raise ApiError("gesture_binding_changed", "预览后完整绑定已变化，请重新预览")
        return current

    async def preview(self, body, operator):
        kind = request_payload(body, "previewRequest")["kind"]
        binding, _, size = self._snapshot()
        camera = self.runtime.devices.camera_broker(self.device_id)
        if camera.camera_id != binding["camera_id"]:
            raise ApiError("gesture_binding_changed", "取帧摄像头与校准不符")

        class Capture:
            # EvidenceStore 的截图接口使用 device_id 记录摄像头编号。
            device_id = camera.camera_id
            frame = None

            def capture_sync(self):
                self.frame = dict(camera.capture_sync())
                for source_key, binding_key in (
                        ("camera_id", "camera_id"), ("device_id", "device_id"),
                        ("device_unique_id", "camera_device_unique_id"),
                        ("binding_revision", "binding_revision")):
                    if source_key in self.frame and not ag._same_json(
                            self.frame[source_key], binding[binding_key]):
                        raise ValueError("摄像头源身份与当前绑定不符")
                if self.frame.get("source_legacy_mode") is True:
                    raise ValueError("兼容取流缺少正式来源绑定，不能登记模板")
                if "source_captured_at" in self.frame:
                    self.frame["captured_at"] = self.frame["source_captured_at"]
                if "source_frame_seq" in self.frame:
                    self.frame["frame_seq"] = self.frame["source_frame_seq"]
                return self.frame

        capture = Capture()
        refs = []
        last_time = None
        last_seq = None
        for index in range(3):
            self._same_binding(binding)
            try:
                record = self.runtime.evidence.capture_screenshot(
                    device_id=self.device_id, camera=capture, task_id=f"navtpl-{kind}",
                    label=f"模板预览帧 {index + 1}", operator=operator)
                moment = datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))
                seq = capture.frame.get("frame_seq")
                if (not 0 <= (_now() - moment).total_seconds() <= 5
                        or (last_time is not None and moment <= last_time)
                        or (seq is not None and (type(seq) is not int or seq < 1
                            or (last_seq is not None and seq <= last_seq)))
                        or (record["width"], record["height"]) != (
                            binding["frame_width"], binding["frame_height"])):
                    raise ValueError("帧不新鲜、序号或尺寸不符")
                last_time, last_seq = moment, seq
                refs.append({"kind": "image", "evidence_id": record["evidence_id"],
                             "sha256": record["sha256"], "captured_at": record["captured_at"],
                             "camera_id": binding["camera_id"], "frame_seq": seq,
                             "calibration_fingerprint": binding["calibration_fingerprint"]})
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                raise ApiError("camera_unavailable", "预览取证失败，请检查摄像头") from exc
            if index < 2:
                await asyncio.sleep(0.3)
        self._same_binding(binding)
        width, height = size
        result = {
            "preview_id": str(uuid.uuid4()), "device_id": self.device_id,
            "kind": kind, "binding": binding, "frame_evidence": refs,
            "candidates": [{"left_px": width * (2 * col + 1) // 8,
                            "top_px": height * (2 * row + 1) // 6,
                            "width_px": max(1, width // 10),
                            "height_px": max(1, height // 10)}
                           for row in range(2) for col in range(3)],
            "expires_at": (_now() + timedelta(seconds=60)).isoformat(),
        }
        self._planes(result)  # 无法解码的截图不得成为可登记预览。
        ag._atomic_write_json(self.directory / "previews" / f'{result["preview_id"]}.json', result)
        return deepcopy(result)

    def _planes(self, preview):
        _, matrix, size = self._same_binding(preview["binding"])
        planes = []
        for ref in preview["frame_evidence"]:
            try:
                record = self.runtime.evidence.get(ref["evidence_id"])
                if (record is None or record["device_id"] != self.device_id
                        or record["task_id"] != f'navtpl-{preview["kind"]}'
                        or record["file_path"] != f'navtpl-{preview["kind"]}/{ref["evidence_id"]}.png'
                        or record["kind"] != "screenshot"
                        or any(record[key] != ref[key] for key in ("sha256", "camera_id", "captured_at"))
                        or ref["camera_id"] != preview["binding"]["camera_id"]
                        or ref["calibration_fingerprint"] != preview["binding"]["calibration_fingerprint"]):
                    raise ValueError("证据归属不符")
                path = Path(self.runtime.config.evidence_dir).resolve(strict=True) / record["file_path"]
                if path.resolve(strict=True) != path or not path.is_file():
                    raise ValueError("证据路径不安全")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != ref["sha256"]:
                    raise ValueError("证据哈希不符")
                # 登记时矫正至校准屏幕；消费端只读保存的裁剪，不再重裁原图。
                image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
                binding = preview["binding"]
                if image is None or image.shape != (binding["frame_height"], binding["frame_width"]):
                    raise ValueError("证据图像无效")
                planes.append(cv2.warpPerspective(image, matrix, size))
            except (OSError, ValueError, TypeError, KeyError, cv2.error) as exc:
                raise ApiError("idempotency_conflict", "平台预览证据缺失、损坏或归属不符，请重新预览") from exc
        return planes

    def _read_preview(self, preview_id, kind):
        try:
            path = self.directory / "previews" / f"{preview_id}.json"
            preview = json.loads(path.read_text(encoding="utf-8"))
            if (not _validator("previewResponse").is_valid(preview)
                    or preview["preview_id"] != preview_id
                    or preview["device_id"] != self.device_id or preview["kind"] != kind
                    or datetime.fromisoformat(preview["expires_at"].replace("Z", "+00:00")) <= _now()):
                raise ValueError("预览身份或有效期不符")
        except (OSError, ValueError, TypeError) as exc:
            raise ApiError("idempotency_conflict", "预览不存在、过期或设备/类型不符，请重新预览") from exc
        self._same_binding(preview["binding"])
        return preview

    def get(self, kind):
        self._kind(kind)
        try:
            record = json.loads((self.directory / f"{kind}.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ApiError("device_not_found", "模板不存在") from exc
        except (OSError, ValueError) as exc:
            raise ApiError("gesture_profile_invalid", "模板档案损坏") from exc
        if (not _validator("templateRecord").is_valid(record)
                or record["device_id"] != self.device_id
                or record["kind"] != kind or record["template_id"] != kind):
            raise ApiError("gesture_profile_invalid", "模板档案身份或内容无效")
        return record

    def register(self, kind, body):
        self._kind(kind)
        payload = request_payload(body, "registrationRequest")
        preview = self._read_preview(payload["preview_id"], kind)
        _, _, (width, height) = self._same_binding(preview["binding"])
        regions = payload["anchor_regions"]
        if not (1 if kind == "top" else 3) <= len(regions) <= (3 if kind == "top" else 6):
            raise ApiError("invalid_request", "顶部需 1-3 个锚点，桌面需 3-6 个锚点")
        rectangles = []
        for region in regions:
            x, y, w, h = (region[k] for k in ("left_px", "top_px", "width_px", "height_px"))
            if x + w > width or y + h > height:
                raise ApiError("invalid_request", "锚点区域超出校准屏幕")
            if any(x < r and x + w > l and y < b and y + h > t for l, t, r, b in rectangles):
                raise ApiError("invalid_request", "锚点区域不能重叠")
            rectangles.append((x, y, x + w, y + h))
        target = payload["restore_target"]
        if target is not None:
            r = target["region"]
            if kind != "launcher" or r["x"] + r["width"] > 1 or r["y"] + r["height"] > 1:
                raise ApiError("invalid_request", "恢复目标仅适用于桌面且必须在归一化屏幕范围内")
        # 复用档案线程锁，辅以进程锁，CAS 覆盖整个读取/裁剪/原子替换过程。
        with ag._lock_for(self.device_id):
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / ".register.lock").open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                path = self.directory / f"{kind}.json"
                version = self.get(kind)["version"] if path.exists() else 0
                if payload["expected_version"] != version:
                    raise ApiError("idempotency_conflict", "模板版本已变化，请重新读取核对")
                preview = self._read_preview(payload["preview_id"], kind)
                planes = self._planes(preview)
                anchors = []
                for region, (x, y, right, bottom) in zip(regions, rectangles):
                    crop = planes[0][y:bottom, x:right]
                    ok, encoded = cv2.imencode(".png", crop)
                    if not ok:
                        raise ApiError("camera_unavailable", "模板裁剪编码失败")
                    evidence = self.runtime.evidence.save_file_evidence(
                        device_id=self.device_id, task_id=f"navtpl-{kind}", data=encoded.tobytes(),
                        label=f'模板校准域裁剪；预览 {preview["preview_id"]}', suffix=".png")
                    anchors.append({"region": region, "frame_evidence": preview["frame_evidence"],
                                    "crop_sha256": evidence["sha256"], "evidence_id": evidence["evidence_id"]})
                try:
                    for anchor in anchors:
                        load_template_patch(
                            anchor, kind=kind, device_id=self.device_id, binding=preview["binding"],
                            screen_size=(width, height), evidence_root=self.runtime.config.evidence_dir,
                            evidence_get=self.runtime.evidence.get)
                except ValueError as exc:
                    raise ApiError("idempotency_conflict", "模板登记证据已变化，请重新预览") from exc
                record = {"contract_version": "navigation-template.v1", "template_id": kind,
                          "version": version + 1, "device_id": self.device_id, "kind": kind,
                          "binding": preview["binding"], "layout_version": "wechat-list-layout.v1",
                          "anchor_evidence": anchors, "restore_target": target,
                          "confirmed_at": _now().isoformat(),
                          "confirmation_source": "debug_explicit_confirmation"}
                if not _validator("templateRecord").is_valid(record):
                    raise ApiError("gesture_profile_invalid", "服务端模板记录不符合契约")
                self._read_preview(payload["preview_id"], kind)
                ag._atomic_write_json(path, record)
                return record
