import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timezone
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from api.app import create_app
from runtime.errors import ApiError
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from runtime.screen_observation import ScreenObservationService, rectify
from tests.helpers import make_device_configs


def test_rectification_uses_calibrated_projection_without_mutation():
    image = np.zeros((300, 300, 3), np.uint8)
    image[40:70, 20:50] = (0, 255, 0)
    png = cv2.imencode(".png", image)[1].tobytes()
    bundle = {"screen": {"screenWidth": 300, "screenHeight": 300},
              "camera": {"cameraToScreen": {"homography": [[1, 0, 10], [0, 1, 20], [0, 0, 1]]}}}
    result, inverse, width, height = rectify(png, bundle)
    decoded = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)
    assert decoded[70, 40, 1] > decoded[70, 40, 2] + 100
    assert np.allclose(np.asarray(inverse) @ [40, 70, 1], [30, 50, 1])
    assert (width, height) == (300, 300)
    assert bundle["camera"]["cameraToScreen"]["homography"][0][2] == 10


def test_invalid_projection_rejected():
    with pytest.raises(ValueError):
        rectify(b"", {"screen": {"screenWidth": 300, "screenHeight": 300},
                      "camera": {"cameraToScreen": {"homography": [[0]*3]*3}}})


def test_rectification_accepts_wire_flat_nine_homography():
    image = np.zeros((300, 300, 3), np.uint8)
    image[40:70, 20:50] = (0, 255, 0)
    png = cv2.imencode(".png", image)[1].tobytes()
    bundle = {"screen": {"screenWidth": 300, "screenHeight": 300},
              "camera": {"cameraToScreen": {
                  "homography": [1, 0, 10, 0, 1, 20, 0, 0, 1]}}}
    result, inverse, width, height = rectify(png, bundle)
    decoded = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)
    assert decoded[70, 40, 1] > decoded[70, 40, 2] + 100
    assert np.allclose(np.asarray(inverse) @ [40, 70, 1], [30, 50, 1])
    assert (width, height) == (300, 300)


def test_rectification_tracks_phone_translation_without_mutating_calibration(monkeypatch):
    image = np.zeros((300, 300, 3), np.uint8)
    image[70:80, 75:85] = (0, 255, 0)
    saved = [[30, 30], [270, 30], [270, 270], [30, 270]]
    live = [[x + 5, y + 3] for x, y in saved]
    monkeypatch.setattr("runtime.screen_observation.locate_screen_quad",
                        lambda _image, _saved, **_kwargs: {"corners": live})
    bundle = {"screen": {"screenWidth": 300, "screenHeight": 300,
                         "edgeModel": {"corners": saved}},
              "camera": {"cameraToScreen": {"homography": np.eye(3).tolist()}}}
    png, inverse, _, _ = rectify(cv2.imencode(".png", image)[1].tobytes(), bundle)
    result = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert result[72, 75, 1] > 200
    assert np.allclose(np.asarray(inverse) @ [75, 72, 1], [80, 75, 1])
    assert bundle["camera"]["cameraToScreen"]["homography"] == np.eye(3).tolist()


def test_rectification_excludes_surroundings_after_phone_translation(monkeypatch):
    image = np.zeros((300, 300, 3), np.uint8)
    image[5:20, 5:20] = (0, 0, 255)
    image[70:80, 75:85] = (0, 255, 0)
    saved = [[30, 30], [270, 30], [270, 270], [30, 270]]
    live = [[x + 5, y + 3] for x, y in saved]
    monkeypatch.setattr("runtime.screen_observation.locate_screen_quad",
                        lambda _image, _saved, **_kwargs: {"corners": live})
    bundle = {"screen": {"screenWidth": 300, "screenHeight": 300,
                         "edgeModel": {"corners": saved}},
              "camera": {"cameraToScreen": {"homography": np.eye(3).tolist()}}}
    png, _, _, _ = rectify(cv2.imencode(".png", image)[1].tobytes(), bundle)
    result = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert result[72, 75, 1] > result[72, 75, 2] + 100
    assert max(result[10, 10]) - min(result[10, 10]) == 0


class _ObservationDevices:
    def __init__(self, *, broker=None, camera=None):
        self.broker = broker
        self.camera_driver = camera

    def get(self, device_id):
        return {"device_id": device_id, "camera_id": "cam-01",
                "daemon_session": "daemon-1"}

    def driver(self, device_id):
        return type("Driver", (), {"data_root": None})()

    def camera_broker(self, device_id):
        if isinstance(self.broker, Exception):
            raise self.broker
        return self.broker

    def camera(self, device_id):
        return self.camera_driver


def _valid_bundle():
    return {
        "status": "ready", "deviceId": "arm-01", "cameraId": "cam-01",
        "daemonSession": "daemon-1",
        "cameraDeviceUniqueId": "camera-unique", "bindingRevision": 1,
        "frame": {"width": 300, "height": 300},
        "screen": {"screenWidth": 300, "screenHeight": 300,
                   "edgeModel": {"corners": [[30, 30], [270, 30],
                                             [270, 270], [30, 270]]}},
        "camera": {"cameraToScreen": {
            "homography": [1, 0, 0, 0, 1, 0, 0, 0, 1]}},
    }


def test_invalid_calibration_returns_explicit_calibration_error(monkeypatch):
    bundle = _valid_bundle()
    bundle["camera"]["cameraToScreen"]["homography"] = [1, 2, 3]
    monkeypatch.setattr("runtime.screen_observation.calibration_bundle.load_bundle",
                        lambda *args, **kwargs: bundle)
    service = ScreenObservationService(type("Runtime", (), {
        "devices": _ObservationDevices()
    })())
    with pytest.raises(ApiError) as raised:
        asyncio.run(service.capture("arm-01"))
    assert raised.value.key == "calibration_invalid"
    assert raised.value.http == 409


def test_camera_initialization_error_returns_explicit_camera_error(monkeypatch):
    monkeypatch.setattr("runtime.screen_observation.calibration_bundle.load_bundle",
                        lambda *args, **kwargs: _valid_bundle())
    service = ScreenObservationService(type("Runtime", (), {
        "devices": _ObservationDevices(broker=RuntimeError("OCR 初始化失败"))
    })())
    with pytest.raises(ApiError) as raised:
        asyncio.run(service.capture("arm-01"))
    assert raised.value.key == "camera_unavailable"
    assert raised.value.http == 409


def test_fresh_black_stream_waits_without_ocr_or_stale_coordinates(monkeypatch):
    bundle = _valid_bundle()
    bundle["screen"]["edgeModel"] = {
        "corners": [[30, 30], [270, 30], [270, 270], [30, 270]]}
    monkeypatch.setattr("runtime.screen_observation.calibration_bundle.load_bundle",
                        lambda *args, **kwargs: bundle)
    image = cv2.imencode(".png", np.zeros((300, 300, 3), np.uint8))[1].tobytes()
    now = datetime.now(timezone.utc).isoformat()
    frame = {"image_bytes": image, "camera_id": "cam-01", "device_id": "arm-01",
             "device_unique_id": "camera-unique", "binding_revision": 1,
             "width": 300, "height": 300, "frozen": False,
             "source_frame_seq": 7, "source_received_at": now,
             "source_captured_at": now,
             "captured_at": now, "sha256": "0" * 64}

    class Broker:
        async def capture(self):
            return frame

    class RejectOcr:
        def recognize(self, *args, **kwargs):
            raise AssertionError("等待时不能运行 OCR")

    service = ScreenObservationService(type("Runtime", (), {
        "devices": _ObservationDevices(broker=Broker(), camera=type(
            "Camera", (), {"ocr_engine": RejectOcr()})())
    })())
    result = asyncio.run(service.capture("arm-01"))
    assert result["quality"] == "waiting"
    from drivers.screen_gestures import binding_snapshot
    assert result["motion_calibration_fingerprint"] == (
        binding_snapshot(bundle)["calibration_fingerprint"])
    assert result["camera_device_unique_id"] == bundle["cameraDeviceUniqueId"]
    assert "screen_to_calibrated_camera" not in result
    assert result["ocr"]["words"] == []
    assert result["frame_seq"] == 7
    assert cv2.imdecode(np.frombuffer(base64.b64decode(result["image_base64"]),
                                     np.uint8), cv2.IMREAD_GRAYSCALE).max() == 0
    frame["source_received_at"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ApiError) as raised:
        asyncio.run(service.capture("arm-01"))
    assert raised.value.key == "camera_unavailable"


def test_motion_ready_dark_phone_waits_even_with_bright_surroundings(monkeypatch):
    bundle = _valid_bundle()
    bundle["status"] = "motion_ready"
    monkeypatch.setattr("runtime.screen_observation.calibration_bundle.load_bundle",
                        lambda *args, **kwargs: bundle)
    image = np.full((300, 300, 3), 220, np.uint8)
    image[30:271, 30:271] = 0
    now = datetime.now(timezone.utc).isoformat()
    frame = {"image_bytes": cv2.imencode(".png", image)[1].tobytes(),
             "camera_id": "cam-01", "device_id": "arm-01",
             "device_unique_id": "camera-unique", "binding_revision": 1,
             "width": 300, "height": 300, "frozen": False,
             "source_frame_seq": 7, "source_received_at": now,
             "source_captured_at": now, "captured_at": now, "sha256": "0" * 64}

    class Broker:
        async def capture(self):
            return frame

    class RejectOcr:
        def recognize(self, *args, **kwargs):
            raise AssertionError("暗屏时不能运行 OCR")

    service = ScreenObservationService(type("Runtime", (), {
        "devices": _ObservationDevices(broker=Broker(), camera=type(
            "Camera", (), {"ocr_engine": RejectOcr()})())
    })())
    result = asyncio.run(service.capture("arm-01"))
    assert result["quality"] == "waiting"
    assert result["ocr"]["words"] == []


def test_motion_ready_observation_rectifies_shifted_phone_before_ocr(monkeypatch):
    from adapters.vision.ocr import OcrResult, OcrWord

    bundle = _valid_bundle()
    bundle["status"] = "motion_ready"
    monkeypatch.setattr("runtime.screen_observation.calibration_bundle.load_bundle",
                        lambda *args, **kwargs: bundle)
    image = np.zeros((300, 300, 3), np.uint8)
    image[33:274, 35:276] = np.random.default_rng(7).integers(
        90, 210, (241, 241, 3), dtype=np.uint8)
    image[5:20, 5:20] = (0, 0, 255)
    now = datetime.now(timezone.utc).isoformat()
    frame = {"image_bytes": cv2.imencode(".png", image)[1].tobytes(),
             "camera_id": "cam-01", "device_id": "arm-01",
             "device_unique_id": "camera-unique", "binding_revision": 1,
             "width": 300, "height": 300, "frozen": False,
             "source_frame_seq": 8, "source_received_at": now,
             "source_captured_at": now, "captured_at": now, "sha256": "1" * 64}

    class Broker:
        async def capture(self):
            return frame

    class Engine:
        def recognize(self, image, *, region):
            observed = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
            assert max(observed[10, 10]) - min(observed[10, 10]) == 0
            assert np.std(observed[100:200, 100:200]) > 10
            return OcrResult(text="屏外\n微信",
                             words=[OcrWord("屏外", 5, 5, 20, 12, .99),
                                    OcrWord("微信", 100, 100, 30, 14, .98)],
                             confidence=.98,
                             truncated=False, engine="test", engine_version="1",
                             width=observed.shape[1], height=observed.shape[0])

    service = ScreenObservationService(type("Runtime", (), {
        "devices": _ObservationDevices(broker=Broker(), camera=type(
            "Camera", (), {"ocr_engine": Engine()})())
    })())
    result = asyncio.run(service.capture("arm-01"))
    assert result["quality"] == "clear"
    from drivers.calibration_bundle import profile_hash
    assert result["motion_calibration_fingerprint"] == profile_hash(bundle, keys=(
        "schemaVersion", "deviceId", "daemonSession", "cameraId",
        "cameraDeviceUniqueId", "bindingRevision", "frame",
        "camera", "screen", "xy", "z", "safePark",
    ))
    assert result["ocr"]["text"] == "微信"
    assert [word["text"] for word in result["ocr"]["words"]] == ["微信"]
    assert np.allclose(np.asarray(result["screen_to_camera"]) @ [100, 100, 1],
                       [105, 103, 1], atol=2)
    assert np.allclose(np.asarray(result["screen_to_calibrated_camera"]) @ [100, 100, 1],
                       [100, 100, 1], atol=2)


def test_http_screen_observation_reports_bad_calibration_as_4xx(tmp_path, monkeypatch):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime_factory = lambda: runtime
    app.state.device_configs = make_device_configs(1)
    bad_bundle = _valid_bundle()
    bad_bundle["camera"]["cameraToScreen"]["homography"] = [1, 2, 3]
    monkeypatch.setattr("runtime.screen_observation.calibration_bundle.load_bundle",
                        lambda *args, **kwargs: bad_bundle)
    with TestClient(app) as client:
        response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 409
    assert response.json()["error"]["key"] == "calibration_invalid"


def test_calibration_invalid_is_a_registered_client_error():
    """校准未就绪属于可解释的门禁，不得被统一处理器变成 500 未知错误。"""
    error = ApiError("calibration_invalid", "设备会话尚未完成校准")
    assert error.http == 409
    payload = error.to_payload()["error"]
    assert payload["title"] == "校准档案无效"
    assert payload["suggestion"]


def test_motion_fingerprint_matches_wake_without_changing_projection_identity(monkeypatch):
    from drivers import calibration_bundle as cb
    from drivers.wake_screen import resolve_wake_target
    from tests.test_screen_gesture_driver import gesture_bundle
    from tests.test_screen_wake_driver import validated_bundle, wake_frame

    bundle = validated_bundle(gesture_bundle())
    monkeypatch.setattr("runtime.screen_observation.calibration_bundle.load_bundle",
                        lambda *args, **kwargs: bundle)
    frame = wake_frame(bundle)
    frame.update(source_received_at=frame["source_captured_at"], sha256="0" * 64)

    class Devices(_ObservationDevices):
        def get(self, device_id):
            return {"device_id": device_id, "camera_id": bundle["cameraId"],
                    "daemon_session": bundle["daemonSession"]}

    class Broker:
        async def capture(self):
            return frame

    service = ScreenObservationService(type("Runtime", (), {
        "devices": Devices(broker=Broker())
    })())
    original = deepcopy(bundle)
    first = asyncio.run(service.capture("arm-01"))
    assert first["motion_calibration_fingerprint"] == (
        resolve_wake_target(bundle)["binding"]["calibration_fingerprint"])
    assert first["calibration_fingerprint"] != first["motion_calibration_fingerprint"]
    assert bundle == original

    bundle["z"]["tapFeed"] += 1
    second = asyncio.run(service.capture("arm-01"))
    assert second["calibration_fingerprint"] == first["calibration_fingerprint"]
    assert second["motion_calibration_fingerprint"] != first["motion_calibration_fingerprint"]
