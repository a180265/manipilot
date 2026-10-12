"""Public screen evidence contract with isolated camera and OCR inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from adapters.vision.ocr import OcrResult, OcrWord
from api.app import create_app
from drivers.calibration_bundle import save_bundle
from runtime.errors import ApiError
from runtime.util import iso, utc_now


CONTRACT_DIR = Path(__file__).resolve().parents[1] / "api" / "contracts"


def contract_schema():
    return json.loads(
        (CONTRACT_DIR / "screen-observation.v1.schema.json").read_text(encoding="utf-8")
    )


class EvidenceHardware:
    def __init__(self, data_root):
        self.data_root = data_root
        self.account = "wx-01"
        self.frame_seq = 12
        image = np.full((600, 300, 3), 220, dtype=np.uint8)
        image[30:570, 30:270] = np.random.default_rng(7).integers(
            90, 175, (540, 240, 3), dtype=np.uint8)
        self.image = cv2.imencode(".png", image)[1].tobytes()
        self.ocr_engine = self

    def get(self, device_id):
        if device_id != "arm-01":
            raise ApiError("device_not_found")
        return {
            "device_id": device_id, "camera_id": "cam-01",
            "daemon_session": "daemon-01", "wechat_account_id": self.account,
        }

    def driver(self, device_id):
        return self

    def camera_broker(self, device_id):
        return self

    def camera(self, device_id):
        return self

    async def capture(self):
        captured_at = iso(utc_now())
        return {
            "image": self.image, "width": 300, "height": 600,
            "device_id": "arm-01", "camera_id": "cam-01",
            "captured_at": captured_at, "source_captured_at": captured_at,
            "source_received_at": captured_at, "frozen": False,
            "sha256": hashlib.sha256(self.image).hexdigest(),
            "device_unique_id": "physical-camera-01", "binding_revision": 3,
            "source_frame_seq": self.frame_seq,
        }

    def recognize(self, image, *, region):
        return OcrResult(
            text="hello", words=[OcrWord("hello", 50, 100, 50, 20, 0.98)],
            confidence=0.98, truncated=False, engine="fixture-ocr", engine_version="1",
            region=region, request_id="ocr-01", elapsed_ms=1, width=300, height=600,
        )


@pytest.fixture
def evidence_api(tmp_path):
    bundle = {
        "deviceId": "arm-01", "cameraId": "cam-01", "status": "ready",
        "daemonSession": "daemon-01", "cameraDeviceUniqueId": "physical-camera-01",
        "bindingRevision": 3, "frame": {"width": 300, "height": 600},
        "screen": {"screenWidth": 300, "screenHeight": 600,
                   "edgeModel": {"corners": [[30, 30], [269, 30],
                                             [269, 569], [30, 569]]}},
        "camera": {"cameraToScreen": {"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}},
    }
    save_bundle(bundle, tmp_path)
    hardware = EvidenceHardware(tmp_path)
    runtime = SimpleNamespace(config={"screen_ime_enabled": False}, devices=hardware)
    app = create_app(runtime)
    # Exercise the real route without starting runtime workers or device discovery.
    app.state.runtime = runtime
    client = TestClient(app)
    try:
        yield client, hardware, bundle
    finally:
        client.close()


def test_http_screen_evidence_matches_published_v1_contract(evidence_api):
    client, _, _ = evidence_api
    response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 200, response.text
    schema = contract_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response.json())
    assert response.json()["quality"] == "clear"
    assert response.json()["ocr"]["words"][0]["text"] == "hello"


def test_http_invalid_calibration_matches_documented_409_error(evidence_api):
    client, hardware, bundle = evidence_api
    save_bundle({**bundle, "status": "invalidated"}, hardware.data_root)
    response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["key"] == "calibration_invalid"
    error_schema = contract_schema()["$defs"]["apiError"]
    Draft202012Validator(error_schema).validate(body)
    assert body["error"]["suggestion"]
    assert "image_base64" not in body


@pytest.mark.parametrize("account", [None, ""])
def test_contract_preserves_unbound_account_with_fresh_stream(evidence_api, account):
    client, hardware, _ = evidence_api
    hardware.account = account
    response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 200, response.text
    body = response.json()
    Draft202012Validator(contract_schema(), format_checker=FormatChecker()).validate(body)
    assert body["wechat_account_id"] == account
    assert body["frame_seq"] == 12


def test_missing_stream_sequence_cannot_supply_screen_evidence(evidence_api):
    client, hardware, _ = evidence_api
    hardware.frame_seq = None
    response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["key"] == "camera_unavailable"


@pytest.mark.parametrize(("path", "value"), [
    (("contract_version",), "other.v1"),
    (("image_sha256",), "not-a-hash"),
    (("captured_at",), "2026-09-13T12:00:00"),
    (("screen_width",), 0),
    (("screen_to_camera",), [[1, 0, 0]]),
    (("ocr", "words", 0, "confidence"), 1.5),
])
def test_contract_rejects_corrupt_evidence_fields(evidence_api, path, value):
    client, _, _ = evidence_api
    response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 200, response.text
    body = response.json()
    target = body
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        Draft202012Validator(contract_schema(), format_checker=FormatChecker()).validate(body)


def test_success_contract_does_not_accept_an_error_as_screen_evidence(evidence_api):
    client, _, _ = evidence_api
    response = client.get("/v1/devices/missing-device/screen-observation")
    assert response.status_code == 404, response.text
    schema = contract_schema()
    Draft202012Validator(schema["$defs"]["apiError"]).validate(response.json())
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(response.json())
