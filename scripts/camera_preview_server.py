"""多摄像头高帧率预览媒体平面。

职责边界：
- CameraHelper 在有预览观看者时，把预览 JPEG 推到这里；
- 本服务只保存每个 camera_id 的最新 JPEG，并向多个查看会话广播；
- OCR、校准、机械臂动作和查看会话权限仍由 8790/8788 负责；
- 不解码 JPEG，不持久化帧，不建立无界队列。

默认监听 127.0.0.1:8793。8793 不直接暴露给 WX 前端，8788 的
camera-view.v1 stream 会把已经授权的查看会话代理到这里。
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINDINGS_PATH = ROOT / "data" / "camera_helper" / "bindings.json"
DEFAULT_CAMERA_IDS = tuple(f"cam-{index:02d}" for index in range(1, 11))
MAX_FRAME_BYTES = 8 * 1024 * 1024
STALE_AFTER_SECONDS = 1.5

STATE_LOCK = threading.RLock()
FRAME_READY = threading.Condition(STATE_LOCK)
BINDINGS_PATH = DEFAULT_BINDINGS_PATH
BINDINGS_MTIME_NS: int | None = None
BINDINGS: dict[str, str] = {}
CHANNELS: dict[str, dict] = {}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _camera_id(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if not value.startswith("cam-") or not value[4:].isdigit():
        raise ValueError("camera_id 必须使用 cam-数字格式")
    return value


def _new_channel(camera_id: str) -> dict:
    return {
        "camera_id": camera_id,
        "frame": b"",
        "seq": 0,
        "source_seq": None,
        "captured_at": None,
        "received_at": None,
        "received_epoch": None,
        "width": None,
        "height": None,
        "viewer_count": 0,
        "window_start": time.monotonic(),
        "window_count": 0,
        "fps": 0.0,
    }


def _load_bindings_if_changed() -> None:
    global BINDINGS_MTIME_NS, BINDINGS
    try:
        mtime_ns = BINDINGS_PATH.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    if mtime_ns == BINDINGS_MTIME_NS:
        return
    loaded: dict[str, str] = {}
    try:
        payload = json.loads(BINDINGS_PATH.read_text(encoding="utf-8"))
        for raw in payload.get("bindings", []):
            try:
                camera_id = _camera_id(raw.get("cameraId"))
            except ValueError:
                continue
            unique_id = str(raw.get("deviceUniqueId") or "").strip()
            if unique_id:
                loaded[camera_id] = unique_id
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        loaded = {}
    BINDINGS = loaded
    BINDINGS_MTIME_NS = mtime_ns


def configure_state(bindings_path: Path | None = None) -> None:
    global BINDINGS_PATH, BINDINGS_MTIME_NS, BINDINGS, CHANNELS
    BINDINGS_PATH = bindings_path or DEFAULT_BINDINGS_PATH
    BINDINGS_MTIME_NS = None
    BINDINGS = {}
    with STATE_LOCK:
        CHANNELS = {camera_id: _new_channel(camera_id) for camera_id in DEFAULT_CAMERA_IDS}
        _load_bindings_if_changed()


def _channel(camera_id: str) -> dict:
    with STATE_LOCK:
        return CHANNELS.setdefault(camera_id, _new_channel(camera_id))


def _frame_age_ms(channel: dict) -> int | None:
    epoch = channel.get("received_epoch")
    if epoch is None:
        return None
    return max(0, int((time.time() - float(epoch)) * 1000))


def _status(camera_id: str) -> dict:
    channel = _channel(camera_id)
    with STATE_LOCK:
        age_ms = _frame_age_ms(channel)
        return {
            "camera_id": camera_id,
            "has_frame": bool(channel["frame"]),
            "frame_seq": channel["seq"],
            "source_frame_seq": channel["source_seq"],
            "captured_at": channel["captured_at"],
            "received_at": channel["received_at"],
            "age_ms": age_ms,
            "width": channel["width"],
            "height": channel["height"],
            "viewer_count": channel["viewer_count"],
            "fps": round(float(channel["fps"]), 2),
            "status": "streaming" if channel["frame"] and age_ms is not None and age_ms <= int(STALE_AFTER_SECONDS * 1000) else "stale",
        }


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("Content-Length 无效") from exc
    if length <= 0:
        raise ValueError("请求体不能为空")
    if length > MAX_FRAME_BYTES:
        raise OverflowError(f"请求体超过 {MAX_FRAME_BYTES} 字节")
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ValueError("请求体读取不完整")
    return body


def _validate_upload(camera_id: str, unique_id: str | None) -> None:
    with STATE_LOCK:
        _load_bindings_if_changed()
        expected = BINDINGS.get(camera_id)
    if not expected:
        raise RuntimeError("camera_unbound")
    if not unique_id or unique_id != expected:
        raise RuntimeError("camera_binding_mismatch")


def accept_frame(camera_id: str, data: bytes, headers: dict[str, str]) -> dict:
    """保存一帧最新预览数据；不解码、不排队。"""
    _validate_upload(camera_id, headers.get("X-Camera-Unique-Id"))
    now = _utc_iso()
    channel = _channel(camera_id)
    with FRAME_READY:
        channel["frame"] = data
        channel["seq"] += 1
        channel["source_seq"] = headers.get("X-Source-Frame-Seq")
        channel["captured_at"] = headers.get("X-Captured-At") or now
        channel["received_at"] = now
        channel["received_epoch"] = time.time()
        try:
            channel["width"] = int(headers.get("X-Frame-Width", "0")) or None
            channel["height"] = int(headers.get("X-Frame-Height", "0")) or None
        except ValueError:
            channel["width"] = None
            channel["height"] = None
        channel["window_count"] += 1
        elapsed = time.monotonic() - channel["window_start"]
        if elapsed >= 1.0:
            channel["fps"] = channel["window_count"] / elapsed
            channel["window_count"] = 0
            channel["window_start"] = time.monotonic()
        FRAME_READY.notify_all()
        return _status(camera_id)


class PreviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"ok": False, "error": {"code": code, "message": message}})

    @staticmethod
    def _camera_route(path: str, suffix: str) -> str | None:
        prefix = "/cameras/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        value = path[len(prefix):-len(suffix)].strip("/")
        return value or None

    def _stream(self, camera_id: str) -> None:
        channel = _channel(camera_id)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=preview")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last_seq = -1
        with FRAME_READY:
            channel["viewer_count"] += 1
        try:
            self.wfile.write(b"--preview\r\n")
            self.wfile.flush()
            while True:
                with FRAME_READY:
                    FRAME_READY.wait_for(lambda: channel["seq"] != last_seq, timeout=1.0)
                    if channel["seq"] == last_seq:
                        keepalive = b": keep-alive\r\n\r\n"
                        frame = None
                    else:
                        last_seq = channel["seq"]
                        frame = bytes(channel["frame"])
                if frame is None:
                    self.wfile.write(keepalive)
                else:
                    self.wfile.write(
                        b"Content-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode("ascii")
                        + b"\r\nX-Frame-Seq: "
                        + str(last_seq).encode("ascii")
                        + b"\r\n\r\n"
                        + frame
                        + b"\r\n--preview\r\n"
                    )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with FRAME_READY:
                channel["viewer_count"] = max(0, channel["viewer_count"] - 1)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True, "service": "camera-preview-server", "service_version": "preview-v1"})
            return
        camera_id = self._camera_route(path, "/preview/status")
        if camera_id:
            try:
                camera_id = _camera_id(camera_id)
            except ValueError:
                self._error(400, "camera_id_invalid", "camera_id 无效")
                return
            self._json(200, _status(camera_id))
            return
        camera_id = self._camera_route(path, "/preview.mjpeg")
        if camera_id:
            try:
                self._stream(_camera_id(camera_id))
            except ValueError:
                self._error(400, "camera_id_invalid", "camera_id 无效")
            return
        self._error(404, "not_found", "预览接口不存在")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        camera_id = self._camera_route(path, "/preview/frame")
        if not camera_id:
            self._error(404, "not_found", "预览接口不存在")
            return
        try:
            camera_id = _camera_id(camera_id)
            data = _read_body(self)
            result = accept_frame(camera_id, data, {key: value for key, value in self.headers.items()})
        except OverflowError as exc:
            self._error(413, "frame_too_large", str(exc))
            return
        except ValueError as exc:
            self._error(400, "preview_frame_invalid", str(exc))
            return
        except RuntimeError as exc:
            code = str(exc)
            messages = {
                "camera_unbound": "摄像头尚未绑定物理设备",
                "camera_binding_mismatch": "上传摄像头与保存绑定不一致",
            }
            self._error(409, code, messages.get(code, "预览摄像头身份校验失败"))
            return
        self._json(200, {"ok": True, **result})


def main() -> None:
    parser = argparse.ArgumentParser(description="Arm Fleet 多摄像头高帧率预览服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8793)
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS_PATH)
    args = parser.parse_args()
    configure_state(args.bindings)
    server = ThreadingHTTPServer((args.host, args.port), PreviewHandler)
    print(f"camera-preview-server listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
