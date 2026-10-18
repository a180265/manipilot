"""Session rollover through the actual HttpFrameSource consumer; no device/network IO."""
from __future__ import annotations

from datetime import datetime, timezone
import time
import urllib.request

import cv2
import numpy as np
import pytest

from drivers.real_camera import HttpFrameSource

GEN_A = "11111111-1111-4111-8111-111111111111"
GEN_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def bridge(monkeypatch):
    ok, encoded = cv2.imencode(".jpg", np.zeros((6, 8, 3), dtype=np.uint8))
    assert ok
    state = {"body": encoded.tobytes(), "headers": {}}

    class Response:
        @property
        def headers(self):
            return state["headers"]

        def read(self):
            return state["body"]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_kw: Response())

    def publish(seq, generation=GEN_A, **overrides):
        headers = {
            "X-Camera-Id": "cam-01", "X-Device-Id": "arm-01",
            "X-Camera-Unique-Id": "uvc-01", "X-Binding-Revision": "3",
            "X-Frame-Seq": str(seq), "X-Frame-Width": "8", "X-Frame-Height": "6",
            "X-Received-At": datetime.now(timezone.utc).isoformat(),
            "X-Camera-Binding-Id": "33333333-3333-4333-8333-333333333333",
            "X-Source-Geometry-Id": "ab" * 32,
            "X-Source-Generation-Id": generation,
            "X-Source-Frame-Seq": str(seq),
            "X-Source-Clock-Id": "mach-absolute:" + "cd" * 32,
            "X-Source-Captured-Monotonic-Ns": str(time.monotonic_ns()),
            "X-Source-Capture-Uncertainty-Ms": "1",
            "X-Source-Timing-Provenance": "camera_pts_host_clock.v1",
            "X-Frame-Source": "camera_helper",
        }
        headers.update(overrides)
        state["headers"] = headers

    source = HttpFrameSource("http://offline.invalid/frame.jpg",
        expected_camera_id="cam-01", expected_device_id="arm-01",
        expected_device_unique_id="uvc-01", expected_width=8, expected_height=6)
    return source, publish, state


@pytest.mark.parametrize("next_seq", [93, 6953])
def test_fresh_new_generation_can_restart_sequence(bridge, next_seq):
    source, publish, _ = bridge
    publish(6953)
    source()
    source._last_seq_at = time.monotonic() - 10
    publish(next_seq, GEN_B)
    source()
    assert source.last_frame_seq == next_seq
    assert source.last_source_generation_id == GEN_B
    assert source.stream_alive is True


def test_same_generation_regression_still_rejected(bridge):
    source, publish, _ = bridge
    publish(6953)
    source()
    publish(93)
    with pytest.raises(RuntimeError, match="序号倒退"):
        source()
    assert source.last_frame_seq == 6953


def test_missing_generation_cannot_authorize_reset(bridge):
    source, publish, _ = bridge
    publish(6953)
    source()
    publish(93, "")
    with pytest.raises(RuntimeError, match="序号倒退"):
        source()


def test_old_generation_cannot_return_even_with_higher_sequence(bridge):
    source, publish, _ = bridge
    publish(6953)
    source()
    publish(6954, GEN_B)
    source()
    publish(7000, GEN_A)
    with pytest.raises(RuntimeError, match="旧采集代际"):
        source()
    assert source.last_source_generation_id == GEN_B


@pytest.mark.parametrize("override, message", [
    ({"X-Device-Id": "other"}, "身份不匹配"),
    ({"X-Source-Captured-Monotonic-Ns": "1"}, "过期采样"),
])
def test_generation_change_preserves_identity_and_freshness(bridge, override, message):
    source, publish, _ = bridge
    publish(6953)
    source()
    publish(93, GEN_B, **override)
    with pytest.raises(RuntimeError, match=message):
        source()
    assert source.last_source_generation_id == GEN_A


@pytest.mark.parametrize("missing", [
    "X-Source-Timing-Provenance", "X-Source-Capture-Uncertainty-Ms",
    "X-Source-Frame-Seq", "X-Camera-Binding-Id", "X-Source-Geometry-Id",
    "X-Source-Clock-Id", "X-Frame-Source",
])
def test_incomplete_source_proof_cannot_authorize_reset(bridge, missing):
    source, publish, _ = bridge
    publish(6953)
    source()
    publish(93, GEN_B, **{missing: ""})
    with pytest.raises(RuntimeError, match="序号倒退|代际.*证明"):
        source()


def test_failed_decode_does_not_retire_previous_generation(bridge):
    source, publish, state = bridge
    publish(6953)
    source()
    original = state["body"]
    publish(7000, GEN_B)
    state["body"] = b"not a jpeg"
    with pytest.raises(RuntimeError, match="无法解码"):
        source()
    state["body"] = original
    publish(6954)
    source()
    assert source.last_source_generation_id == GEN_A


def test_failed_resolution_does_not_retire_previous_generation(bridge):
    source, publish, _ = bridge
    publish(6953)
    source()
    publish(7000, GEN_B, **{"X-Frame-Width": "9"})
    with pytest.raises(RuntimeError, match="不一致"):
        source()
    publish(6954)
    source()
    assert source.last_source_generation_id == GEN_A


def test_raw_bytes_consumer_rejects_retired_generation(bridge):
    source, publish, _ = bridge
    publish(6953)
    source()
    publish(6954, GEN_B)
    source()
    publish(7000, GEN_A)
    with pytest.raises(RuntimeError, match="旧采集代际"):
        source.fetch_bytes()


def test_legacy_baseline_cannot_be_reset_with_generation_header(bridge):
    source, publish, _ = bridge
    publish(6953, "")
    source()
    publish(93, GEN_B)
    with pytest.raises(RuntimeError, match="序号倒退"):
        source()
