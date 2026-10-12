"""摄像头帧源：服务端取帧的统一入口（可注入，测试用静态帧替代真机）。"""
from __future__ import annotations

import os
import threading


class CameraUnavailableError(RuntimeError):
    """摄像头打开失败或未安装 OpenCV。"""


class UnconfiguredFrameSource:
    """未绑定具体设备时的安全占位帧源。

    多设备运行不能猜测全局 ``/latest.jpg`` 属于哪台手机；调用方必须先
    提供 ``device_id``，再解析到对应的 ``camera_id`` 或显式启用单路兼容。
    """

    name = "未绑定摄像头"

    def capture(self):
        raise CameraUnavailableError(
            "尚未按 device_id 绑定摄像头；多设备模式禁止猜测 8790/latest.jpg")

    def close(self) -> None:
        return None


class StaticFrameSource:
    """测试/仿真帧源：按顺序返回预置帧。"""

    def __init__(self, frames: list):
        self._frames = list(frames)
        self._lock = threading.Lock()
        self.name = f"static({len(self._frames)} 帧)"

    def capture(self):
        with self._lock:
            if not self._frames:
                raise CameraUnavailableError("静态帧源已耗尽")
            return self._frames.pop(0)

    def close(self) -> None:
        pass


class OpenCVFrameSource:
    """本地 USB/内置摄像头（OpenCV/AVFoundation）。懒打开，跨调用复用同一连接。"""

    def __init__(self, index: int | None = None, warmup_frames: int = 12):
        self.index = int(index if index is not None
                         else os.environ.get("ARM_FLEET_SCREEN_IME_CAMERA", 0))
        self.warmup_frames = max(1, int(warmup_frames))
        self.name = f"opencv(index={self.index})"
        self._lock = threading.Lock()
        self._capture = None

    def _ensure_open(self):
        if self._capture is not None:
            return
        try:
            import cv2
        except ImportError as exc:
            raise CameraUnavailableError(
                "OpenCV 未安装：.venv/bin/pip install opencv-python-headless") from exc
        capture = cv2.VideoCapture(self.index)
        if not capture.isOpened():
            capture.release()
            raise CameraUnavailableError(f"摄像头 index={self.index} 打不开（被占用或不存在）")
        # 固定机位 + USB 摄像头曝光需要逐帧稳定：预热若干帧再交付（沿用 K40 实测经验）
        for _ in range(self.warmup_frames):
            capture.read()
        self._capture = capture

    def capture(self):
        with self._lock:
            self._ensure_open()
            ok, frame = self._capture.read()
            if not ok or frame is None:
                raise CameraUnavailableError("摄像头取帧失败")
            return frame

    def close(self) -> None:
        with self._lock:
            if self._capture is not None:
                self._capture.release()
                self._capture = None


def auto_frame_source(bridge_url: str | None = None,
                      status_url: str | None = None,
                      *, camera_id: str | None = None,
                      expected_device_id: str | None = None,
                      expected_device_unique_id: str | None = None,
                      expected_width: int | None = None,
                      expected_height: int | None = None,
                      allow_legacy: bool = False) -> "tuple[object, str]":
    """按明确 ``camera_id`` 选择 8790 分路帧源。

    未提供 ``camera_id`` 时默认返回安全占位源，不再静默读取全局
    ``/latest.jpg``。单路旧脚本必须显式传 ``allow_legacy=True`` 或设置
    ``ARM_FLEET_SCREEN_IME_ALLOW_LEGACY=1``；这样多设备业务不会串路。
    """
    import urllib.request

    legacy = bool(allow_legacy or os.environ.get("ARM_FLEET_SCREEN_IME_ALLOW_LEGACY") in {"1", "true", "yes"})
    if camera_id:
        camera_id = str(camera_id).strip()
        bridge_url = bridge_url or f"http://127.0.0.1:8790/cameras/{camera_id}/latest.jpg"
        status_url = status_url or f"http://127.0.0.1:8790/cameras/{camera_id}/status"
    elif not legacy:
        source = UnconfiguredFrameSource()
        return source, source.name
    else:
        bridge_url = bridge_url or "http://127.0.0.1:8790/latest.jpg"
        status_url = status_url or "http://127.0.0.1:8790/status"

    try:
        with urllib.request.urlopen(status_url, timeout=1.5) as response:
            payload = response.read(200)
        if b"has_frame" in payload or b"seq" in payload:
            from drivers.real_camera import HttpFrameSource

            return HttpFrameSource(
                bridge_url,
                expected_camera_id=camera_id,
                expected_device_id=expected_device_id,
                expected_device_unique_id=expected_device_unique_id,
                expected_width=expected_width,
                expected_height=expected_height,
                allow_legacy=legacy,
            ), f"取流桥({bridge_url})"
    except Exception:
        pass
    if legacy and camera_id is None:
        source = OpenCVFrameSource()
        return source, source.name
    source = UnconfiguredFrameSource()
    return source, source.name
