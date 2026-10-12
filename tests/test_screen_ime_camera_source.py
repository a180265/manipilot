"""screen_ime 摄像头来源必须按 device_id 分路，禁止默认猜全局 8790。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from adapters.screen_ime.camera_source import (
    CameraUnavailableError,
    UnconfiguredFrameSource,
    auto_frame_source,
)
from drivers.real_camera import HttpFrameSource


def test_auto_source_without_device_id_is_safe_placeholder(monkeypatch):
    monkeypatch.delenv("ARM_FLEET_SCREEN_IME_ALLOW_LEGACY", raising=False)
    source, description = auto_frame_source()
    assert isinstance(source, UnconfiguredFrameSource)
    assert "未绑定" in description
    with pytest.raises(CameraUnavailableError, match="禁止猜测"):
        source.capture()


def test_auto_source_with_camera_id_uses_explicit_route(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("bridge down")),
    )
    source, _ = auto_frame_source(camera_id="cam-07")
    assert isinstance(source, UnconfiguredFrameSource)


def test_auto_source_preserves_identity_and_resolution_expectations(monkeypatch):
    class StatusResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b'{"has_frame":true}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: StatusResponse())
    source, _ = auto_frame_source(
        bridge_url="http://camera.example/cameras/cam-07/latest.jpg",
        status_url="http://camera.example/cameras/cam-07/status",
        camera_id="cam-07",
        expected_device_id="arm-07",
        expected_device_unique_id="uvc-07",
        expected_width=1280,
        expected_height=720,
    )
    assert isinstance(source, HttpFrameSource)
    assert source.url == "http://camera.example/cameras/cam-07/latest.jpg"
    assert source.expected_camera_id == "cam-07"
    assert source.expected_device_id == "arm-07"
    assert source.expected_device_unique_id == "uvc-07"
    assert source.expected_width == 1280
    assert source.expected_height == 720


def test_legacy_source_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ARM_FLEET_SCREEN_IME_ALLOW_LEGACY", "1")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("bridge down")),
    )
    source, _ = auto_frame_source()
    assert source.name.startswith("opencv(")


def test_screen_ime_source_uses_binding_url_and_rebuilds_after_same_channel_change(tmp_path, monkeypatch):
    """屏幕校准按设备绑定取流；同 camera_id 换 URL/版本也不能复用旧源。"""
    from api.app import create_app
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime

    calls = []

    class Source:
        name = "spy-source"

        def capture(self):
            # 让校准路由继续走到检测逻辑；本测试只验证来源解析参数。
            import numpy as np
            return np.zeros((8, 8, 3), dtype=np.uint8)

        def close(self):
            return None

    def fake_auto_frame_source(*args, **kwargs):
        calls.append((args, kwargs))
        return Source(), "spy-source"

    monkeypatch.setattr("adapters.screen_ime.camera_source.auto_frame_source", fake_auto_frame_source)
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "hotplug_discovery_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.device_configs = [{
        "device_id": "arm-cam",
        "camera_id": "cam-07",
        "camera_mode": "multi_channel",
        "camera_frame_url": "http://camera-a/cameras/cam-07/latest.jpg",
        "camera_status_url": "http://camera-a/cameras/cam-07/status",
        "camera_device_unique_id": "uvc-07",
        "expected_frame_width": 1280,
        "expected_frame_height": 720,
        "sim_calibrated": True,
    }]
    markers = [{"color": color, "screenX": i * 10, "screenY": i * 10}
               for i, color in enumerate(("red", "green", "blue", "yellow"))]
    with TestClient(app) as client:
        first = client.post("/api/v1/screen-ime/camera/markers",
                            json={"deviceId": "arm-cam", "markers": markers})
        assert first.status_code == 409
        assert len(calls) == 1
        first_kwargs = calls[-1][1]
        assert first_kwargs["bridge_url"].startswith("http://camera-a/")
        assert first_kwargs["status_url"].startswith("http://camera-a/")
        assert first_kwargs["expected_device_id"] == "arm-cam"
        assert first_kwargs["expected_device_unique_id"] == "uvc-07"

        # 相同配置复用已创建的来源。
        second = client.post("/api/v1/screen-ime/camera/markers",
                             json={"deviceId": "arm-cam", "markers": markers})
        assert second.status_code == 409
        assert len(calls) == 1

        # 只换 URL，camera_id 不变：必须重建来源并使用新地址。
        binding = {
            "operator_id": "op",
            "role": "operator",
            "reason": "更换摄像头服务地址",
            "correlation_id": "corr-cam",
            "client_request_id": "req-cam-url",
            "confirmation_token": "确认执行",
            "camera_frame_url": "http://camera-b/cameras/cam-07/latest.jpg",
            "camera_status_url": "http://camera-b/cameras/cam-07/status",
        }
        updated = client.put("/v1/devices/arm-cam/binding", json=binding)
        assert updated.status_code == 200, updated.text
        third = client.post("/api/v1/screen-ime/camera/markers",
                            json={"deviceId": "arm-cam", "markers": markers})
        assert third.status_code == 409
        assert len(calls) == 2
        assert calls[-1][1]["bridge_url"].startswith("http://camera-b/")
