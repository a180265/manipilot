"""B03 §5.1：screen-source-observation 从指定原图摆正识别（严格投影）。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from tests.test_camera_binding_v3 import v3_bundle
from tests.test_camera_frame_server import jpeg as _jpeg  # noqa: F401


def _realistic_frame(width=640, height=480):
    frame = np.full((height, width, 3), 255, dtype=np.uint8)
    frame[60:420, 80:560] = 40  # 明亮背景+深色屏幕：清晰而非黑屏等待
    return frame


def test_observe_from_source_frame_full_pipeline(tmp_path, monkeypatch):
    from drivers import calibration_bundle as cb
    from runtime.screen_observation import observe_from_source_frame
    from tests.helpers import make_device_configs, make_runtime

    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.devices.register_all(make_device_configs(1))
        bundle = v3_bundle()
        bundle["frame"] = {"width": 640, "height": 480}
        bundle["screen"]["screenWidth"] = 1080
        bundle["screen"]["screenHeight"] = 2400
        bundle["screen"]["edgeModel"]["corners"] = [[80, 60], [560, 60],
                                                    [560, 420], [80, 420]]
        root = tmp_path / "calibration"
        path = cb.bundle_path("arm-01", root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        runtime.devices.driver("arm-01").data_root = root

        # 归档一帧 camera_source_v2 原图（可定位的明暗场景）
        jpeg = cv2.imencode(".jpg", _realistic_frame())[1].tobytes()
        import hashlib
        runtime.devices.get("arm-01").update({"daemon_session": bundle["daemonSession"]})
        record = runtime.evidence.save_source_frame_v2(
            device_id="arm-01", generation="gen-1", seq=1, jpeg=jpeg,
            record={"contract_version": "camera-binding.v2",
                    "evidence_type": "camera_source_v2", "device_id": "arm-01",
                    "camera_id": "cam-01", "camera_binding_id": "cam-bind-01",
                    "source_geometry_id": "geom-01",
                    "source_generation_id": "gen-1", "source_capture_seq": 1,
                    "source_frame_seq": 1,
                    "source_clock_id": "mach-absolute:x",
                    "source_captured_monotonic_ns": 1,
                    "captured_at": "2026-10-03T00:00:00Z",
                    "received_at": "2026-10-03T00:00:01Z",
                    "jpeg_sha256": hashlib.sha256(jpeg).hexdigest(),
                    "frame_width": 640, "frame_height": 480,
                    "camera_unique_id": "uvc-01"})

        # 质量判定打桩：合成平面图必然判 abnormal，测试固定为 clear
        from drivers.real_camera import RealCameraDriver
        monkeypatch.setattr(RealCameraDriver, "_analyze",
                            staticmethod(lambda image, **kw: {
                                "blur_variance": 30.0, "brightness": 120.0,
                                "texture": 30.0}))
        # OCR 引擎打桩：返回空识别（管线打通即可，坐标域已核对尺寸）
        class FakeOcr:
            def recognize(self, png, region="messages"):
                class R:
                    def to_dict(self):
                        return {"region": region, "text": "", "words": [],
                                "confidence": 0, "truncated": False,
                                "engine": "fake", "engine_version": "0",
                                "request_id": "r", "elapsed_ms": 1,
                                "width": 720, "height": 1600}
                return R()
        runtime.devices.camera("arm-01").ocr_engine = FakeOcr()

        try:
            result = await observe_from_source_frame(
                runtime, device_id="arm-01", bundle=bundle,
                evidence_id=record["evidence_id"])
            # source 严格溯源
            src = result["source"]
            assert src["source_generation_id"] == "gen-1"
            assert src["source_capture_seq"] == 1
            assert src["jpeg_sha256"] == record["jpeg_sha256"]
            assert result["frame_sha256"] == record["jpeg_sha256"]
            assert result["frame_seq"] == 1
            assert result["frame_width"] == 640 and result["frame_height"] == 480
            assert result["camera_device_unique_id"] == "uvc-01"
            # 严格投影字段
            assert result["contract_version"] == "screen-observation.v1"
            assert "screen_to_camera" in result
            assert "screen_to_calibrated_camera" in result
            assert "coordinate_frame_hash" in result
            assert result["ocr"]["width"] == result["screen_width"]
            assert result["ocr"]["height"] == result["screen_height"]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_observe_rejects_hash_mismatch_and_missing(tmp_path, monkeypatch):
    from drivers import calibration_bundle as cb
    from runtime.screen_observation import observe_from_source_frame
    from tests.helpers import make_device_configs, make_runtime

    async def scenario():
        runtime = make_runtime(tmp_path)
        await runtime.devices.register_all(make_device_configs(1))
        from drivers.real_camera import RealCameraDriver
        monkeypatch.setattr(RealCameraDriver, "_analyze",
                            staticmethod(lambda image, **kw: {
                                "blur_variance": 30.0, "brightness": 120.0,
                                "texture": 30.0}))
        bundle = v3_bundle()
        root = tmp_path / "calibration"
        path = cb.bundle_path("arm-01", root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        runtime.devices.driver("arm-01").data_root = root
        runtime.devices.get("arm-01").update({"daemon_session": bundle["daemonSession"]})
        record = runtime.evidence.save_source_frame_v2(
            device_id="arm-01", generation="gen-9", seq=3, jpeg=b"not-a-jpeg",
            record={"contract_version": "camera-binding.v2",
                    "evidence_type": "camera_source_v2", "device_id": "arm-01",
                    "jpeg_sha256": "0" * 64})
        from runtime.errors import ApiError
        with pytest.raises(ApiError, match="哈希"):
            await observe_from_source_frame(runtime, device_id="arm-01",
                                            bundle=bundle,
                                            evidence_id=record["evidence_id"])
        with pytest.raises(ApiError):
            await observe_from_source_frame(runtime, device_id="arm-01",
                                            bundle=bundle, evidence_id="missing")
        # Source construction also pins the current calibration and binding.
        await runtime.shutdown()

    asyncio.run(scenario())
