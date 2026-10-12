"""Synthetic pixels, real image decoding and displacement measurement."""
import hashlib
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest

from drivers import fake_camera
from drivers.scroll_observation import measure_displacement


def decode(frame):
    assert hashlib.sha256(frame["image"]).hexdigest() == frame["sha256"]
    image = cv2.imdecode(np.frombuffer(frame["image"], np.uint8), cv2.IMREAD_GRAYSCALE)
    assert image.shape == (frame["height"], frame["width"])
    return image


@pytest.mark.parametrize("offset", [-64, 0, 320])
def test_scroll_pixels_move_both_ways_and_stationary_is_not_displaced(offset):
    camera = fake_camera.FakeCameraDriver("arm-01", width=1280, height=720)
    camera.set_scroll_offset(offset)
    before = camera.capture_sync()
    stationary = camera.capture_sync()
    camera.scroll_by(32)
    after = camera.capture_sync()
    roi = {"x": 300, "y": 100, "width": 550, "height": 550}

    first, same, shifted = map(decode, (before, stationary, after))
    assert before["frame_ref"] != stationary["frame_ref"]
    np.testing.assert_array_equal(first, same)
    np.testing.assert_array_equal(first[32:], shifted[:-32])
    assert measure_displacement(first, same, roi)["status"] == "stationary"
    up = measure_displacement(first, shifted, roi)
    down = measure_displacement(shifted, first, roi)
    assert up["status"] == down["status"] == "displaced", (up, down)
    assert up["dy_px"] == pytest.approx(-32, abs=1)
    assert down["dy_px"] == pytest.approx(32, abs=1)
    camera.scroll_by(-32)
    assert camera.capture_sync()["image"] == before["image"]


def test_swipe_during_capture_cannot_tear_a_simulated_frame(monkeypatch):
    reference = fake_camera.FakeCameraDriver("reference", width=160, height=120)
    expected = reference.capture_sync()["image"]
    camera = fake_camera.FakeCameraDriver("arm-01", width=160, height=120)
    encode = fake_camera.encode_png

    def swipe_mid_frame(width, height, pixel):
        def sample(x, y):
            if x == 0 and y == height // 2:
                camera.scroll_by(32)
            return pixel(x, y)
        return encode(width, height, sample)

    with monkeypatch.context() as patch:
        patch.setattr(fake_camera, "encode_png", swipe_mid_frame)
        captured = camera.capture_sync()

    assert captured["image"] == expected
    reference.scroll_by(32)
    assert camera.capture_sync()["image"] == reference.capture_sync()["image"]


def test_reused_pixels_keep_fresh_metadata_and_follow_scene_changes(monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = iter(start + timedelta(seconds=i) for i in range(4))
    monkeypatch.setattr(fake_camera, "utc_now", lambda: next(times))
    camera = fake_camera.FakeCameraDriver("arm-01")
    first = camera.capture_sync()
    second = camera.capture_sync()
    assert first["image"] == second["image"]
    assert first["frame_ref"] != second["frame_ref"]
    assert first["captured_at"] != second["captured_at"]
    assert first["frozen"] is second["frozen"] is False

    camera.show_conversation("chat-1")
    chat = camera.capture_sync()
    assert chat["sha256"] != first["sha256"]
    camera.configure_simulation_frame(160, 120)
    assert decode(camera.capture_sync()).shape == (120, 160)


@pytest.mark.parametrize("quality", ["blocked", "waiting", "overexposed"])
def test_scroll_texture_does_not_hide_camera_quality_faults(quality):
    camera = fake_camera.FakeCameraDriver("arm-01", width=640, height=480)
    clear = decode(camera.capture_sync())
    camera.set_quality(quality)
    before = decode(camera.capture_sync())
    camera.scroll_by(32)
    after = decode(camera.capture_sync())

    result = measure_displacement(
        before, after, {"x": 60, "y": 50, "width": 520, "height": 380})
    assert result["status"] == "unknown", result
    assert result["reason_code"] in {"insufficient_features", "insufficient_inliers"}
    assert before.std() < 3
    camera.set_quality("clear")
    camera.set_scroll_offset(0)
    np.testing.assert_array_equal(decode(camera.capture_sync()), clear)
