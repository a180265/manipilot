from __future__ import annotations

import cv2
import pytest

from runtime.devices import DeviceManager
from runtime.errors import ApiError


class _DeniedCapture:
    def __init__(self, *_args, **_kwargs):
        pass

    def read(self):
        return False, None

    def release(self):
        return None


def test_real_camera_without_explicit_mode_cannot_guess_global_frame(monkeypatch):
    def forbidden_capture(*_args, **_kwargs):
        raise AssertionError("未声明来源时不应直接打开 UVC")

    monkeypatch.setattr(cv2, "VideoCapture", forbidden_capture)
    with pytest.raises(ApiError, match="未提供多路 camera_frame_url"):
        DeviceManager._make_real_camera({
            "device_id": "arm-02",
            "camera_id": "cam-02",
            "camera_index": 0,
        })


def test_legacy_single_explicitly_allows_8790_compatibility(monkeypatch):
    monkeypatch.setattr(cv2, "VideoCapture", _DeniedCapture)
    camera = DeviceManager._make_real_camera({
        "device_id": "arm-01",
        "camera_id": "cam-01",
        "camera_mode": "legacy_single",
        "camera_index": 0,
    })
    provider = camera._frame_provider
    assert provider.url.endswith("/latest.jpg")
    assert provider.allow_legacy is True
