from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from scripts import camera_frame_server as server
from drivers.real_camera import HttpFrameSource, RealCameraDriver


def jpeg(color: tuple[int, int, int], width: int = 64, height: int = 48) -> bytes:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = color
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return encoded.tobytes()


@pytest.fixture()
def frame_server(tmp_path):
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({
        "devices": [
            {"device_id": "arm-01", "camera_id": "cam-01"},
            {"device_id": "arm-02", "camera_id": "cam-02"},
        ]
    }), encoding="utf-8")
    server.configure_state(
        bindings_path=tmp_path / "bindings.json",
        devices_path=devices,
        legacy_camera_id="cam-01",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def request(base: str, path: str, *, method: str = "GET", body: bytes | None = None,
            headers: dict[str, str] | None = None):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def bind(base: str, camera_id: str, unique_id: str) -> None:
    status, _, raw = request(
        base,
        f"/cameras/{camera_id}/binding",
        method="PUT",
        body=json.dumps({
            "deviceUniqueId": unique_id,
            "displayName": unique_id,
            "operator": "test",
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, raw


def upload(base: str, camera_id: str, unique_id: str, data: bytes, *, source_seq: int | str | None = None):
    if source_seq is None:
        source_seq = int(server.CHANNELS.get(camera_id, {}).get("source_seq") or 0) + 1
    return request(
        base,
        f"/cameras/{camera_id}/frame",
        method="POST",
        body=data,
        headers={
            "Content-Type": "image/jpeg",
            "X-Camera-Id": camera_id,
            "X-Camera-Unique-Id": unique_id,
            "X-Source-Frame-Seq": str(source_seq),
            "X-Frame-Source": "camera_helper",
            "X-Captured-At": datetime.now(timezone.utc).isoformat(),
        },
    )


def test_channels_keep_independent_frames_and_sequences(frame_server):
    bind(frame_server, "cam-01", "uvc-01")
    bind(frame_server, "cam-02", "uvc-02")
    red = jpeg((0, 0, 255))
    green = jpeg((0, 255, 0))

    assert upload(frame_server, "cam-01", "uvc-01", red)[0] == 200
    assert upload(frame_server, "cam-01", "uvc-01", red)[0] == 200
    assert upload(frame_server, "cam-02", "uvc-02", green)[0] == 200

    status, headers, cam1 = request(frame_server, "/cameras/cam-01/latest.jpg")
    assert status == 200
    assert headers["X-Camera-Id"] == "cam-01"
    assert headers["X-Device-Id"] == "arm-01"
    assert headers["X-Camera-Unique-Id"] == "uvc-01"
    assert headers["X-Frame-Seq"] == "2"
    assert cam1 == red

    status, headers, cam2 = request(frame_server, "/cameras/cam-02/latest.jpg")
    assert status == 200
    assert headers["X-Camera-Id"] == "cam-02"
    assert headers["X-Device-Id"] == "arm-02"
    assert headers["X-Frame-Seq"] == "1"
    assert cam2 == green


def test_binding_conflicts_and_wrong_upload_identity_are_rejected(frame_server):
    bind(frame_server, "cam-01", "uvc-01")
    status, _, raw = request(
        frame_server,
        "/cameras/cam-02/binding",
        method="PUT",
        body=json.dumps({"deviceUniqueId": "uvc-01"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "camera_unique_id_conflict"

    status, _, raw = upload(frame_server, "cam-01", "wrong-camera", jpeg((1, 2, 3)))
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "camera_binding_mismatch"


def test_unknown_camera_invalid_jpeg_and_route_header_mismatch(frame_server):
    status, _, raw = upload(frame_server, "cam-99", "uvc-99", jpeg((1, 2, 3)))
    assert status == 404
    assert json.loads(raw)["error"]["code"] == "camera_not_found"

    bind(frame_server, "cam-01", "uvc-01")
    status, _, raw = upload(frame_server, "cam-01", "uvc-01", b"not-jpeg")
    assert status == 400
    assert json.loads(raw)["error"]["code"] == "camera_frame_decode_failed"

    status, _, raw = request(
        frame_server,
        "/cameras/cam-01/frame",
        method="POST",
        body=jpeg((1, 2, 3)),
        headers={
            "Content-Type": "image/jpeg",
            "X-Camera-Id": "cam-02",
            "X-Camera-Unique-Id": "uvc-01",
        },
    )
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "camera_binding_mismatch"


def test_multichannel_upload_requires_explicit_source_marker(frame_server):
    bind(frame_server, "cam-01", "uvc-01")
    status, _, raw = request(
        frame_server,
        "/cameras/cam-01/frame",
        method="POST",
        body=jpeg((4, 5, 6)),
        headers={
            "Content-Type": "image/jpeg",
            "X-Camera-Id": "cam-01",
            "X-Camera-Unique-Id": "uvc-01",
        },
    )
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "camera_source_invalid"


def test_multichannel_upload_rejects_missing_or_out_of_order_sequence(frame_server):
    bind(frame_server, "cam-01", "uvc-01")
    status, _, raw = request(
        frame_server,
        "/cameras/cam-01/frame",
        method="POST",
        body=jpeg((4, 5, 6)),
        headers={
            "Content-Type": "image/jpeg",
            "X-Camera-Id": "cam-01",
            "X-Camera-Unique-Id": "uvc-01",
            "X-Frame-Source": "camera_helper",
            "X-Captured-At": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "frame_sequence_invalid"

    assert upload(frame_server, "cam-01", "uvc-01", jpeg((1, 2, 3)), source_seq=2)[0] == 200
    status, _, raw = upload(frame_server, "cam-01", "uvc-01", jpeg((7, 8, 9)), source_seq=2)
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "frame_sequence_out_of_order"


def test_status_becomes_stale_and_unavailable(frame_server):
    bind(frame_server, "cam-01", "uvc-01")
    assert upload(frame_server, "cam-01", "uvc-01", jpeg((1, 2, 3)))[0] == 200
    channel = server.CHANNELS["cam-01"]

    channel["received_epoch"] = time.time() - 2
    status, _, raw = request(frame_server, "/cameras/cam-01/status")
    assert status == 200
    assert json.loads(raw)["status"] == "stale"
    assert request(frame_server, "/cameras/cam-01/latest.jpg")[0] == 503

    channel["received_epoch"] = time.time() - 11
    status, _, raw = request(frame_server, "/cameras/cam-01/status")
    assert status == 200
    assert json.loads(raw)["status"] == "unavailable"


def test_legacy_route_works_for_one_stream_and_rejects_multiple(frame_server):
    first = jpeg((10, 20, 30))
    status, _, _ = request(
        frame_server,
        "/frame",
        method="POST",
        body=first,
        headers={"Content-Type": "image/jpeg", "X-Camera-Id": "cam-01"},
    )
    assert status == 200
    status, headers, data = request(frame_server, "/latest.jpg")
    assert status == 200
    assert headers["X-Camera-Id"] == "cam-01"
    assert data == first

    bind(frame_server, "cam-02", "uvc-02")
    assert upload(frame_server, "cam-02", "uvc-02", jpeg((30, 20, 10)))[0] == 200
    status, _, raw = request(frame_server, "/latest.jpg")
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "legacy_route_ambiguous"


def test_bindings_persist_with_monotonic_revision(frame_server, tmp_path):
    bind(frame_server, "cam-01", "uvc-01")
    first_revision = server.BINDING_REVISION
    assert first_revision == 1
    payload = json.loads(server.BINDINGS_PATH.read_text(encoding="utf-8"))
    assert payload["revision"] == 1
    assert payload["bindings"][0]["cameraId"] == "cam-01"


def test_multiple_bindings_persist_once_each(frame_server):
    """多路绑定持久化后每个 camera_id 只出现一次。"""
    bind(frame_server, "cam-01", "uvc-01")
    bind(frame_server, "cam-02", "uvc-02")
    payload = json.loads(server.BINDINGS_PATH.read_text(encoding="utf-8"))
    assert [item["cameraId"] for item in payload["bindings"]] == ["cam-01", "cam-02"]


def test_resaving_same_binding_while_streaming_is_idempotent(frame_server):
    bind(frame_server, "cam-01", "uvc-01")
    assert upload(frame_server, "cam-01", "uvc-01", jpeg((1, 2, 3)))[0] == 200
    before = server.BINDING_REVISION
    status, _, raw = request(
        frame_server,
        "/cameras/cam-01/binding",
        method="PUT",
        body=json.dumps({"deviceUniqueId": "uvc-01"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    assert json.loads(raw)["binding_unchanged"] is True
    assert server.BINDING_REVISION == before


def test_http_frame_source_validates_identity_and_exposes_metadata(frame_server):
    bind(frame_server, "cam-01", "uvc-01")
    assert upload(frame_server, "cam-01", "uvc-01", jpeg((2, 4, 6)))[0] == 200
    source = HttpFrameSource(
        frame_server + "/cameras/cam-01/latest.jpg",
        expected_camera_id="cam-01",
        expected_device_id="arm-01",
        expected_device_unique_id="uvc-01",
        expected_width=64,
        expected_height=48,
    )

    frame = source()
    assert frame.shape[:2] == (48, 64)
    assert source.last_camera_id == "cam-01"
    assert source.last_device_id == "arm-01"
    assert source.last_device_unique_id == "uvc-01"
    assert source.last_binding_revision == 1
    assert source.last_frame_seq == 1
    assert source.stream_alive is True
    # 静态场景：窗口内连续取到同一 seq 不是断流（源头节流/快速采样），
    # 只有窗口外仍无推进才是真死。
    assert source() is not None
    assert source.stream_alive is True
    source._last_seq_at = time.monotonic() - source.seq_staleness_seconds - 1.0
    assert source() is not None
    assert source.stream_alive is False

    captured = RealCameraDriver("cam-01", frame_provider=source).capture_sync()
    assert captured["camera_id"] == "cam-01"
    assert captured["device_id"] == "arm-01"
    assert captured["device_unique_id"] == "uvc-01"
    assert captured["binding_revision"] == 1
    assert captured["source_frame_seq"] == 1


def test_http_frame_source_rejects_wrong_device_and_resolution(frame_server):
    bind(frame_server, "cam-01", "uvc-01")
    assert upload(frame_server, "cam-01", "uvc-01", jpeg((2, 4, 6)))[0] == 200

    wrong_device = HttpFrameSource(
        frame_server + "/cameras/cam-01/latest.jpg",
        expected_camera_id="cam-01",
        expected_device_id="arm-02",
    )
    with pytest.raises(RuntimeError, match="身份不匹配"):
        wrong_device()

    wrong_resolution = HttpFrameSource(
        frame_server + "/cameras/cam-01/latest.jpg",
        expected_camera_id="cam-01",
        expected_device_id="arm-01",
        expected_width=1280,
        expected_height=720,
    )
    with pytest.raises(RuntimeError, match="分辨率不匹配"):
        wrong_resolution()
