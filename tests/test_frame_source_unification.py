"""Auto_CS#6 取帧协议单实现回归：校准链取帧收敛 HttpFrameSource。

- HttpFrameSource.fetch_bytes：原字节+seq 通道。头/身份/新鲜度校验与
  __call__ 同一套；不解码不重编码（证据存档与 MJPEG 需要桥上原字节）。
- motor_debug_server.fetch_camera_frame：改经 fetch_bytes 单实现；桥宕机
  与观测链失败语义一致（RuntimeError）。
- scripts/register_view.grab_frame：同一实现（allow_legacy 单路兼容）。

全部使用临时端口假帧桥；不接触真实相机、机械臂或生产端口（8788/8790/8792）。
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


def _formal_headers() -> dict[str, str]:
    """正式链路全套 v2 头；X-Received-At 每次现算（新鲜度门 1.5s）。"""
    now = _now_iso()
    return {
        "X-Camera-Id": "cam-01",
        "X-Device-Id": "arm-01",
        "X-Camera-Unique-Id": "uvc-01",
        "X-Binding-Revision": "7",
        "X-Frame-Seq": "12",
        "X-Frame-Width": "8",
        "X-Frame-Height": "6",
        "X-Received-At": now,
        "X-Captured-At": now,
    }


class _BridgeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        bridge = self.server  # type: ignore[attr-defined]
        body = bridge.jpeg
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        for key, value in bridge.headers.items():
            self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args) -> None:
        pass


class _FakeBridge(ThreadingHTTPServer):
    def __init__(self, jpeg: bytes, headers: dict[str, str]):
        super().__init__(("127.0.0.1", 0), _BridgeHandler)
        self.jpeg = jpeg
        self.headers = headers


@pytest.fixture()
def frame_bridge():
    servers = []

    def _start(jpeg: bytes, headers: dict[str, str]) -> str:
        server = _FakeBridge(jpeg, headers)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}/cameras/cam-01/latest.jpg"

    yield _start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _tiny_jpeg(width: int = 8, height: int = 6) -> bytes:
    import cv2

    ok, buffer = cv2.imencode(".jpg", np.zeros((height, width, 3), dtype=np.uint8))
    assert ok
    return buffer.tobytes()


# ---------------------------------------------------------------------------
# HttpFrameSource.fetch_bytes：原字节通道（共享核心）
# ---------------------------------------------------------------------------

def test_fetch_bytes_returns_original_bytes_and_seq(frame_bridge):
    """fetch_bytes 交付桥上原字节（不改字节=证据 sha256 稳定）与帧序号。"""
    from drivers.real_camera import HttpFrameSource

    original = _tiny_jpeg()
    url = frame_bridge(original, _formal_headers())
    source = HttpFrameSource(url, expected_camera_id="cam-01",
                             expected_device_id="arm-01")
    data, seq = source.fetch_bytes()
    assert data == original
    assert seq == 12
    # 同一实例 __call__ 仍走解码路径：返回 ndarray，分辨率与头一致。
    frame = source()
    assert isinstance(frame, np.ndarray)
    assert frame.shape == (6, 8, 3)


def test_fetch_bytes_enforces_identity_and_formal_headers(frame_bridge):
    """身份不匹配 / 必填头缺失：与 __call__ 同一套拒绝语义。"""
    from drivers.real_camera import HttpFrameSource

    body = _tiny_jpeg()
    url = frame_bridge(body, {**_formal_headers(), "X-Device-Id": "arm-02"})
    source = HttpFrameSource(url, expected_camera_id="cam-01",
                             expected_device_id="arm-01")
    with pytest.raises(RuntimeError, match="身份不匹配"):
        source.fetch_bytes()

    url = frame_bridge(body, {key: value for key, value in _formal_headers().items()
                              if key != "X-Binding-Revision"})
    source = HttpFrameSource(url, expected_camera_id="cam-01",
                             expected_device_id="arm-01")
    with pytest.raises(RuntimeError, match="X-Binding-Revision"):
        source.fetch_bytes()


def test_fetch_bytes_enforces_freshness(frame_bridge):
    """过期帧（X-Received-At 超龄）拒绝：与观测链同一新鲜度语义。"""
    from drivers.real_camera import HttpFrameSource

    stale = (datetime.now(timezone.utc) - timedelta(seconds=30)) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")
    url = frame_bridge(_tiny_jpeg(), {**_formal_headers(), "X-Received-At": stale})
    source = HttpFrameSource(url, expected_camera_id="cam-01",
                             expected_device_id="arm-01")
    with pytest.raises(RuntimeError, match="过期帧"):
        source.fetch_bytes()


# ---------------------------------------------------------------------------
# 校准链入口：motor_debug_server.fetch_camera_frame 单实现
# ---------------------------------------------------------------------------

@pytest.fixture()
def debug_module():
    from scripts import motor_debug_server as debug

    return debug


def _patch_device(monkeypatch, debug, url: str) -> None:
    cfg = {"device_id": "arm-01", "camera_id": "cam-01",
           "camera_mode": "multi_channel", "camera_frame_url": url}
    monkeypatch.setattr(debug, "device_config", lambda device: cfg)


def test_fetch_camera_frame_goes_through_frame_source(
        frame_bridge, debug_module, monkeypatch):
    """校准链取帧：经 HttpFrameSource 单实现拿原字节+seq。"""
    original = _tiny_jpeg()
    url = frame_bridge(original, _formal_headers())
    _patch_device(monkeypatch, debug_module, url)
    data, seq = debug_module.fetch_camera_frame("arm-01")
    assert data == original
    assert seq == 12


def test_fetch_camera_frame_bridge_down_is_runtime_error(
        debug_module, monkeypatch):
    """桥宕机：RuntimeError（与观测链 capture_sync 的失败类型一致）。"""
    _patch_device(monkeypatch, debug_module,
                  "http://127.0.0.1:1/cameras/cam-01/latest.jpg")
    with pytest.raises(RuntimeError, match="取流失败"):
        debug_module.fetch_camera_frame("arm-01")


# 真实帧桥端到端探针（binding-v2→会话登记→帧上传→fetch_camera_frame）仅在
# WX 侧本文件：其夹具宿主 tests/test_v110_repair_camera_source.py 为 WX 载具；
# manipilot 侧真链路由 tests/test_r3_10_source_session_timing.py 覆盖。


# ---------------------------------------------------------------------------
# 旧注册脚本：register_view.grab_frame 同一实现
# ---------------------------------------------------------------------------

def test_register_view_grab_frame_uses_frame_source(frame_bridge, monkeypatch):
    import scripts.register_view as register_view

    original = _tiny_jpeg()
    url = frame_bridge(original, _formal_headers())
    monkeypatch.setattr(register_view, "FRAME_BRIDGE_URL", url)
    frame = register_view.grab_frame()
    assert isinstance(frame, np.ndarray)
    assert frame.shape == (6, 8, 3)
