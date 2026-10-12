from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb
from drivers.arm import DriverError
from drivers.daemon_arm import DaemonArmDriver
from drivers.screen_pose import fit_pose
from runtime import calibration_gate
from runtime.errors import ApiError


def ready_bundle() -> dict:
    bundle = cb.new_bundle("arm-01", camera_id="cam-01")
    bundle.update({
        "status": "ready",
        "cameraDeviceUniqueId": "uvc-01",
        "bindingRevision": 7,
        "daemonAddr": "127.0.0.1:8795",
        "daemonSession": "session-01",
        "frame": {"width": 64, "height": 48},
        "screen": {
            "orientation": "portrait", "screenWidth": 1080, "screenHeight": 2400,
            "edgeModel": {
                "corners": [[4, 4], [59, 4], [59, 43], [4, 43]],
                "tolerancePx": 8,
            },
        },
        "z": {
            "hoverZ": 2, "tapDepth": 3, "tapFeed": 1500, "tapDwellMs": 0,
            "deleteHoldMs": 2200, "deleteDepth": 3, "deleteFeed": 1500,
            "validated": True, "deleteValidated": True,
        },
        "xy": {
            "robotToScreen": {"a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0},
            "residualRmsPx": 0, "maxResidualPx": 0, "validated": True,
        },
        "camera": {
            "cameraToScreen": {"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]},
            "reprojectionRmsPx": 0, "validated": True,
        },
        "acceptance": {"debug": True, "wechat": True, "checks": []},
    })
    bundle["bindingFingerprint"] = cb.binding_fingerprint(
        device_id="arm-01", camera_id="cam-01",
        camera_device_unique_id="uvc-01", daemon_addr="127.0.0.1:8795",
        frame_width=64, frame_height=48,
    )
    return bundle


class FakeCamera:
    def __init__(self, unique_id: str = "uvc-01", *, brightness=120, frozen=False):
        self.unique_id = unique_id
        self.brightness = brightness
        self.frozen = frozen

    async def capture(self):
        image = np.full((48, 64, 3), self.brightness if self.brightness <= 18 else 0,
                        np.uint8)
        if self.brightness > 18:
            image[5:43, 5:59] = self.brightness
            for y in range(10, 40, 6):
                image[y:y + 2, 10:50] = min(255, self.brightness + 100)
        ok, encoded = cv2.imencode(".jpg", image)
        assert ok
        return {
            "image_bytes": encoded.tobytes(),
            "camera_id": "cam-01",
            "device_id": "arm-01",
            "device_unique_id": self.unique_id,
            "binding_revision": 7,
            "width": 64,
            "height": 48,
            "frozen": self.frozen,
        }


def run_gate(monkeypatch, *, camera=None, corners=None, bundle=None, **overrides):
    monkeypatch.setattr(calibration_gate, "EDGE_CONSENSUS_INTERVAL_S", 0)
    monkeypatch.setattr(calibration_gate, "locate_screen_quad", lambda _frame, *_args, **_kwargs: {
        "corners": corners or [[4, 4], [59, 4], [59, 43], [4, 43]],
    })
    options = dict(
        device_id="arm-01", camera_id="cam-01", daemon_addr="127.0.0.1:8795",
        daemon_session="session-01", estop_locked=False,
        camera=camera or FakeCamera(), bundle=bundle or ready_bundle(), wechat_send=True,
    )
    options.update(overrides)
    return asyncio.run(calibration_gate.check_live_calibration(**options))


def test_live_gate_accepts_matching_binding(monkeypatch):
    checks, frame, deviation = run_gate(monkeypatch)
    assert checks == []
    assert frame["device_unique_id"] == "uvc-01"
    assert deviation == 0


def test_live_gate_rejects_physical_camera_change(monkeypatch):
    checks, _frame, _deviation = run_gate(monkeypatch, camera=FakeCamera("uvc-02"))
    codes = {item["code"] for item in checks}
    assert "camera_unique_id_changed" in codes
    assert "binding_fingerprint_changed" in codes


def test_live_gate_rejects_screen_edge_change(monkeypatch):
    shifted = [[20, 20], [75, 20], [75, 59], [20, 59]]
    checks, _frame, deviation = run_gate(monkeypatch, corners=shifted)
    assert deviation > 8
    assert "screen_edge_changed" in {item["code"] for item in checks}


def test_live_gate_skips_edge_check_on_contamination(monkeypatch):
    """2026-09-05 真机定案：环境亮区把右上/右下外扩 39px、左上/左下不动=
    非刚性检测干扰。业务门禁不得误报 screen_edge_changed（手机未动，
    用户实测输入/删除零偏移）。"""
    contaminated = [[4, 4], [70, 10], [68, 43], [5, 43]]  # TR/BR 外扩、TL/BL 不动
    checks, _frame, deviation = run_gate(monkeypatch, corners=contaminated)
    assert "screen_edge_changed" not in {item["code"] for item in checks}
    assert deviation is None


def test_live_gate_skips_edge_check_on_keyboard_top_shrink(monkeypatch):
    """键盘打开/通知栏把顶边压暗：单边内容变化（两角同向量+两角静止），
    业务正常场景，不得拦截。"""
    shrunk = [[4, 12], [59, 12], [59, 43], [4, 43]]  # 顶边下移 8px（=容差边界附近再加大）
    checks, _frame, deviation = run_gate(monkeypatch, corners=[[4, 16], [59, 16], [59, 43], [4, 43]])
    assert "screen_edge_changed" not in {item["code"] for item in checks}
    assert deviation is None


def test_live_gate_still_blocks_real_translation(monkeypatch):
    """真实整体平移仍硬拦（回归保护：干扰豁免不得放过真位移）。"""
    shifted = [[9, 9], [64, 9], [64, 48], [9, 48]]  # 整体 +5,+5（容差 8，构造 +5? 用 +6/+7）
    checks, _frame, deviation = run_gate(monkeypatch, corners=[[10, 11], [65, 11], [65, 50], [10, 50]])
    assert "screen_edge_changed" in {item["code"] for item in checks}
    assert deviation > 8


@pytest.mark.parametrize("brightness", [0, 12, 18])
@pytest.mark.parametrize("advisory", [False, True])
def test_live_black_frame_waits_without_invalidating_calibration(monkeypatch, brightness, advisory):
    bundle = ready_bundle()
    before = deepcopy(bundle)
    camera = FakeCamera(brightness=brightness)
    checks, _, deviation = run_gate(
        monkeypatch, camera=camera, bundle=bundle, screen_edge_advisory=advisory,
        corners=[[20, 20], [75, 20], [75, 59], [20, 59]],
    )
    assert [item["code"] for item in checks] == ["camera_waiting"]
    assert "等待画面恢复" in checks[0]["message"]
    assert deviation is None
    assert bundle == before
    camera.brightness = 120
    checks, _, deviation = run_gate(monkeypatch, camera=camera, bundle=bundle)
    assert checks == []
    assert deviation == 0
    assert bundle == before


@pytest.mark.parametrize("sample", [2, 3, 4])
def test_black_frame_during_edge_consensus_blocks_motion(monkeypatch, sample):
    class ChangingCamera(FakeCamera):
        calls = 0

        async def capture(self):
            self.calls += 1
            self.brightness = 0 if self.calls >= sample else 120
            return await super().capture()

    checks, _, deviation = run_gate(monkeypatch, camera=ChangingCamera())
    assert [item["code"] for item in checks] == ["camera_waiting"]
    assert deviation is None


def test_frozen_black_frame_is_still_a_fault(monkeypatch):
    checks, _, deviation = run_gate(
        monkeypatch, camera=FakeCamera(brightness=0, frozen=True),
    )
    assert [item["code"] for item in checks] == ["camera_unavailable"]
    assert "冻结" in checks[0]["message"]
    assert deviation is None


@pytest.mark.parametrize("sample", [1, 2, 3, 4])
def test_capture_failure_cannot_reuse_an_earlier_frame(monkeypatch, sample):
    class FailingCamera(FakeCamera):
        calls = 0

        async def capture(self):
            self.calls += 1
            if self.calls == sample:
                raise RuntimeError("injected capture failure")
            return await super().capture()

    checks, _, deviation = run_gate(monkeypatch, camera=FailingCamera())
    assert [item["code"] for item in checks] == ["camera_unavailable"]
    assert deviation is None


def test_waiting_does_not_hide_estop_or_session_change(monkeypatch):
    checks, _, _ = run_gate(
        monkeypatch, camera=FakeCamera(brightness=0), estop_locked=True,
        daemon_session="session-new", screen_edge_advisory=True,
    )
    codes = {item["code"] for item in checks}
    assert "camera_waiting" in codes
    assert "daemon_session_changed" in codes
    assert "motion_blocked" in codes


@pytest.mark.parametrize("checks,category", [
    ([{"code": "camera_waiting", "message": "等待画面恢复后再执行动作"}],
     "driver_camera_waiting"),
    ([{"code": "camera_unavailable", "message": "画面冻结，禁止执行动作"}],
     "driver_policy_rejected"),
    ([{"code": "camera_waiting", "message": "等待画面恢复"},
      {"code": "daemon_session_changed", "message": "当前会话已变化"}],
     "driver_uncalibrated"),
])
def test_live_gate_preserves_driver_error_classification(tmp_path, checks, category):
    driver = DaemonArmDriver("arm-01", data_root=tmp_path)
    driver.live_calibration_gate = AsyncMock(return_value=checks)
    with pytest.raises(DriverError) as error:
        asyncio.run(driver._check_live_calibration())
    assert error.value.category == category
    assert getattr(error.value, "motion_not_started", False)
    assert driver.action_log == []


@pytest.mark.parametrize("action", ["stop", "estop"])
def test_waiting_cannot_block_stop_or_estop(monkeypatch, tmp_path, action):
    driver = DaemonArmDriver("arm-01", data_root=tmp_path)
    driver._transport = object()
    driver.live_calibration_gate = AsyncMock(side_effect=AssertionError("must not wait for camera"))
    commands = []

    def control(line):
        commands.append(line)
        return {"stopped": True, "estop_locked": True}

    monkeypatch.setattr(driver, "_control_cmd", control)
    result = asyncio.run(getattr(driver, action)())
    assert result["stopped"]
    assert commands == (["STOP"] if action == "stop" else ["ESTOP_LOCK 平台业务急停"])
    driver.live_calibration_gate.assert_not_called()


def test_waiting_api_error_is_not_an_unknown_failure():
    error = ApiError("camera_waiting")
    assert error.http == 409
    assert error.title == "等待画面恢复"
    assert "校准" not in error.suggestion


def test_pose_gate_requires_fresh_distinct_stream_frames(monkeypatch):
    from datetime import datetime, timezone

    class StreamCamera(FakeCamera):
        seq = 0
        frozen_stream = False

        async def capture(self):
            frame = await super().capture()
            if not self.frozen_stream:
                self.seq += 1
            frame["source_frame_seq"] = self.seq
            frame["source_received_at"] = datetime.now(timezone.utc).isoformat()
            frame["source_captured_at"] = frame["source_received_at"]
            return frame

    # Isolate the geometry/stream preflight from synthetic uniform pixels.
    monkeypatch.setattr(calibration_gate, "_frame_checks", lambda *a, **kw: ([], None))
    camera = StreamCamera()
    checks, frame, _ = run_gate(monkeypatch, camera=camera, require_pose=True)
    assert checks == []
    assert frame["_screen_pose"]["movementPx"] == pytest.approx(0)
    camera.frozen_stream = True
    checks, _, _ = run_gate(monkeypatch, camera=camera, require_pose=True)
    assert "screen_pose_unavailable" in {c["code"] for c in checks}


def test_screen_wake_dark_phase_does_not_run_edge_consensus(monkeypatch):
    class StreamCamera(FakeCamera):
        seq = 0

        async def capture(self):
            frame = await super().capture()
            self.seq += 1
            frame.update(
                source_frame_seq=self.seq,
                source_received_at=datetime.now(timezone.utc).isoformat(),
                source_captured_at=datetime.now(timezone.utc).isoformat(),
            )
            return frame

    # §9.4/W04：暗屏阶段不强制四边共识——只采一帧、允许等待不报错；
    # 全黑无位置连续性证据时不伪造姿态（W09），也不追加采样。
    camera = StreamCamera(brightness=0)
    monkeypatch.setattr(calibration_gate, "_quad_of", lambda *_a, **_k: None)
    checks, frame, deviation = run_gate(
        monkeypatch, camera=camera, screen_wake=True, require_pose=False)
    assert checks == []
    assert camera.seq == 1
    assert "_screen_pose" not in frame
    assert deviation is None


def test_screen_wake_dark_phase_fits_pose_when_phone_body_is_locatable(monkeypatch):
    # W08/§9.3：暗屏但机身/屏幕姿态可观测时尽力拟合一次，供双击前补偿。
    class DarkPhoneCamera(FakeCamera):
        seq = 0

        async def capture(self):
            self.seq += 1
            image = np.full((48, 64, 3), 65, np.uint8)
            image[4:44, 4:60] = 0  # 暗屏手机在灰背景中仍可定位（同 wake 帧）
            ok, encoded = cv2.imencode(".png", image)
            assert ok
            return {
                "image_bytes": encoded.tobytes(),
                "camera_id": "cam-01", "device_id": "arm-01",
                "device_unique_id": "uvc-01", "binding_revision": 7,
                "width": 64, "height": 48, "frozen": False,
                "source_frame_seq": self.seq,
                "source_received_at": datetime.now(timezone.utc).isoformat(),
                "source_captured_at": datetime.now(timezone.utc).isoformat(),
            }

    camera = DarkPhoneCamera()
    checks, frame, deviation = run_gate(
        monkeypatch, camera=camera, screen_wake=True, require_pose=False)
    assert checks == []
    assert camera.seq == 1, "暗屏阶段单帧尽力拟合，不跑四边共识"
    assert "_screen_pose" in frame
    assert deviation is None


def test_pose_gate_rejects_old_capture_with_new_receive_time():
    from datetime import datetime, timezone

    frame = {
        "source_frame_seq": 7,
        "source_captured_at": "2020-01-01T00:00:00+00:00",
        "source_received_at": datetime.now(timezone.utc).isoformat(),
    }
    with pytest.raises(ValueError, match="已过期"):
        calibration_gate._fresh_source_seq(frame, None)


def test_pose_gate_blocks_ambiguous_edges_instead_of_skipping(monkeypatch):
    from datetime import datetime, timezone

    class StreamCamera(FakeCamera):
        seq = 0

        async def capture(self):
            frame = await super().capture()
            self.seq += 1
            frame["source_frame_seq"] = self.seq
            frame["source_received_at"] = datetime.now(timezone.utc).isoformat()
            frame["source_captured_at"] = frame["source_received_at"]
            return frame

    monkeypatch.setattr(calibration_gate, "_frame_checks", lambda *a, **kw: ([], None))
    monkeypatch.setattr(calibration_gate, "EDGE_CONSENSUS_INTERVAL_S", 0)
    monkeypatch.setattr(calibration_gate, "locate_screen_quad",
                        lambda _frame, *_args, **_kwargs: {"corners": [[4, 4], [70, 10],
                                                    [68, 43], [5, 43]]})
    checks, frame, _ = asyncio.run(calibration_gate.check_live_calibration(
        device_id="arm-01", camera_id="cam-01", daemon_addr="127.0.0.1:8795",
        daemon_session="session-01", estop_locked=False, camera=StreamCamera(),
        bundle=ready_bundle(), require_pose=True))
    assert "screen_pose_unavailable" in {c["code"] for c in checks}
    assert "_screen_pose" not in frame


def test_frame_quality_uses_phone_region_not_bright_background():
    from tests.test_camera_waiting import encoded, phone_frame

    corners = [[95, 30], [224, 30], [224, 209], [95, 209]]
    frame = {"image_bytes": encoded(phone_frame(lit=False, background=220)),
             "frozen": False}
    checks, quality = calibration_gate._frame_checks(frame, screen_corners=corners)
    assert [item["code"] for item in checks] == ["camera_waiting"]
    frame["image_bytes"] = encoded(phone_frame(lit=True, background=255))
    assert calibration_gate._frame_checks(frame, screen_corners=corners) == ([], None)


def test_bad_phone_quality_does_not_authorize_contact(monkeypatch):
    from tests.test_camera_waiting import encoded, phone_frame

    corners = [[95, 30], [224, 30], [224, 209], [95, 209]]
    frame = {"image_bytes": encoded(phone_frame(lit=True, background=220)),
             "frozen": False}
    monkeypatch.setattr(calibration_gate.RealCameraDriver, "_classify",
                        lambda *_: "overexposed")
    checks, _quality = calibration_gate._frame_checks(frame, screen_corners=corners)
    assert [item["code"] for item in checks] == ["camera_unavailable"]


def test_missing_phone_is_not_reported_as_decode_failure():
    from tests.test_camera_waiting import encoded

    frame = {"image_bytes": encoded(np.full((240, 320, 3), 180, np.uint8)),
             "frozen": False}
    checks, _quality = calibration_gate._frame_checks(
        frame, screen_corners=[[95, 30], [224, 30], [224, 209], [95, 209]])
    assert [item["code"] for item in checks] == ["screen_pose_unavailable"]
    assert "无法定位" in checks[0]["message"]
    assert "无法解码" not in checks[0]["message"]


def test_dark_phone_is_located_in_inverse_image_but_black_scene_is_not():
    from tests.test_camera_waiting import encoded, phone_frame

    corners = calibration_gate._quad_of(
        encoded(phone_frame(lit=False, background=220)))
    assert corners is not None
    assert np.max(np.abs(np.asarray(corners) -
                         [[95, 30], [225, 30], [225, 210], [95, 210]])) <= 3
    assert calibration_gate._quad_of(
        encoded(np.zeros((240, 320, 3), np.uint8))) is None
