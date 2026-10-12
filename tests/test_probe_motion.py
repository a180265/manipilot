import asyncio
import hashlib

import cv2
import numpy as np
import pytest

from drivers.probe_motion import ProbeTracker, register_probe, trace_verdict, ProbeMotionVerifier


def frame(image, seq=1):
    _, raw = cv2.imencode(".png", image)
    raw = raw.tobytes()
    return {"image_bytes": raw, "sha256": hashlib.sha256(raw).hexdigest(),
            "captured_at": "2026-09-14T12:00:00Z", "source_frame_seq": seq,
            "camera_id": "cam-01", "device_id": "arm-01", "device_unique_id": "camera-a",
            "binding_revision": 1, "width": image.shape[1], "height": image.shape[0]}


def scene(x=80, y=100, background=20):
    image = np.full((300, 420, 3), background, dtype=np.uint8)
    patch = np.random.default_rng(2).integers(40, 230, (40, 40, 3), dtype=np.uint8)
    image[y:y + 40, x:x + 40] = patch
    return image


def register(tmp_path):
    register_probe(frame(scene()), {"x": 80, "y": 100, "width": 40, "height": 40},
                   device_id="arm-01", data_root=tmp_path, operator="test")
    return ProbeMotionVerifier(None, device_id="arm-01", data_root=tmp_path)._load_tracker()


def test_probe_tracks_physical_patch_not_background_animation(tmp_path):
    tracker = register(tmp_path)
    assert tracker.locate(scene(background=200))[:2] == (100, 120)
    assert tracker.locate(scene(200, 150, 90))[:2] == (220, 170)


def test_duplicate_and_missing_probe_rejected(tmp_path):
    tracker = register(tmp_path)
    duplicate = scene()
    duplicate[200:240, 200:240] = duplicate[100:140, 80:120]
    with pytest.raises(ValueError):
        tracker.locate(duplicate)
    with pytest.raises(ValueError):
        tracker.locate(np.zeros((300, 420, 3), dtype=np.uint8))


def test_phone_dimming_does_not_change_probe_interior_color(tmp_path):
    image = np.full((300, 420, 3), 230, np.uint8)
    cv2.circle(image, (120, 150), 30, (20, 20, 20), -1)
    register_probe(frame(image), {"x": 80, "y": 110, "width": 80, "height": 80},
                   device_id="arm-01", data_root=tmp_path, operator="test")
    tracker = ProbeMotionVerifier(None, device_id="arm-01", data_root=tmp_path)._load_tracker()
    dimmed = np.full_like(image, 70)
    cv2.circle(dimmed, (120, 150), 30, (20, 20, 20), -1)
    assert tracker.locate(dimmed)[:2] == (120, 150)


def points(values, gap=.06):
    return [{"x": x, "y": y, "at": i * gap, "seq": i + 1}
            for i, (x, y) in enumerate(values)]


@pytest.mark.parametrize("cmd,xy,delta,expected", [
    ("TAP 8 F1500", [(0, 0), (0, 6), (0, 0)], None, True),
    ("TAP2 8 F1500 I0.12 R1", [(0, 0), (0, 6), (0, 5), (0, 6), (0, 0)], None, True),
    ("TAP2 8 F1500 I0.12 R1", [(0, 0), (0, 0), (0, 0)], None, False),
    ("TAP2 8 F1500 I0.12 R1", [(0, 0), (0, 6), (0, 0)], None, False),
    ("TAP 8 F1500", [(0, 0), (0, 6), (0, 9)], None, False),
    ("XY 5 0 F3000", [(0, 0), (10, 0), (20, 0)], [20, 0], True),
    ("SWIPE 5 0 8 F1500 Z1500", [(0, 0), (10, 0), (20, 0)], [20, 0], True),
    ("SWIPE 5 0 8 F1500 Z1500", [(0, 0), (-10, 0), (-20, 0)], [20, 0], False),
    ("SWIPE 5 0 8 F1500 Z1500", [(0, 0), (1, 0), (2, 0)], [20, 0], False),
])
def test_trace_semantics(cmd, xy, delta, expected):
    assert trace_verdict(points(xy), cmd, delta)["verified"] is expected


def test_sparse_trace_is_not_verified():
    assert not trace_verdict(points([(0, 0), (0, 7), (0, 0)], gap=.5), "TAP 8")["verified"]


def test_missing_profile_blocks_before_motion(tmp_path):
    verifier = ProbeMotionVerifier(None, data_root=tmp_path, device_id="arm-01")
    assert verifier.readiness()


def test_sampler_binding_failure_and_cleanup(tmp_path):
    register(tmp_path)

    class Camera:
        async def capture(self):
            return {**frame(scene()), "device_unique_id": "wrong"}

    verifier = ProbeMotionVerifier(Camera(), data_root=tmp_path, device_id="arm-01")
    with pytest.raises(ValueError, match="绑定"):
        asyncio.run(verifier.before("tap"))
    assert verifier._sampler is None
