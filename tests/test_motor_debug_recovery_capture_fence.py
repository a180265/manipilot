"""恢复复核必须等到动作完成后真实采集的帧；全部使用假 HTTP 响应。"""
from datetime import datetime, timezone
from types import SimpleNamespace
import time

import pytest

from drivers import real_camera
from scripts import motor_debug_server as debug


@pytest.fixture
def bridge(monkeypatch):
    clock = SimpleNamespace(ns=time.monotonic_ns())
    clock.monotonic_ns = lambda: clock.ns
    clock.monotonic = lambda: clock.ns / 1e9
    clock.sleep = lambda seconds: setattr(clock, "ns", clock.ns + int(seconds * 1e9))
    monkeypatch.setattr(debug, "time", clock)
    monkeypatch.setattr(real_camera, "time", clock)
    monkeypatch.setattr(debug, "device_config", lambda _: {"camera_id": "cam-01"})
    monkeypatch.setattr(debug, "camera_frame_url_for", lambda _: "http://fake/latest.jpg")
    replies, calls = [], []

    def open_fake(_url, *, timeout):
        calls.append(timeout)
        assert replies, "unexpected frame request"
        value = replies.pop(0) if len(replies) > 1 else replies[0]
        data, headers = value

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return data

        response = Response()
        response.headers = headers
        return response

    monkeypatch.setattr(debug.urllib.request, "urlopen", open_fake)

    def frame(seq, captured_ns, *, uncertainty=1, proven=True):
        headers = {
            "X-Camera-Id": "cam-01", "X-Device-Id": "arm-01",
            "X-Binding-Revision": "3", "X-Frame-Seq": str(seq),
            "X-Frame-Width": "1280", "X-Frame-Height": "720",
            "X-Received-At": datetime.now(timezone.utc).isoformat(),
            "X-Source-Generation-Id": "capture-generation",
            "X-Source-Frame-Seq": str(seq),
        }
        if proven:
            headers.update({
                "X-Source-Captured-Monotonic-Ns": str(captured_ns),
                "X-Source-Capture-Uncertainty-Ms": str(uncertainty),
                "X-Source-Timing-Provenance": "camera_pts_host_clock.v1",
            })
        return f"jpeg-{seq}".encode(), headers

    return clock, replies, calls, frame


def test_recovery_waits_for_actual_post_motion_capture_despite_advancing_sequence(bridge):
    clock, replies, calls, frame = bridge
    fence = clock.ns
    # 新 seq 和刚刚接收，仍然都是动作完成前采样，不能拿去定位探头。
    replies.extend([frame(10, fence - 300_000_000),
                    frame(11, fence - 100_000_000),
                    frame(12, fence + 10_000_000)])
    assert debug.fetch_camera_frame(
        "arm-01", not_before_monotonic_ns=fence) == (b"jpeg-12", 12)
    assert len(calls) == 3


def test_recovery_capture_uncertainty_must_clear_motion_fence(bridge):
    clock, replies, calls, frame = bridge
    fence = clock.ns
    replies.extend([frame(10, fence + 5_000_000, uncertainty=10),
                    frame(11, fence + 15_000_000, uncertainty=10)])
    assert debug.fetch_camera_frame(
        "arm-01", not_before_monotonic_ns=fence) == (b"jpeg-11", 11)
    assert len(calls) == 2


@pytest.mark.parametrize("proven,uncertainty", [(False, 1), (True, -1),
                                               (True, float("nan")), (True, 21)])
def test_recovery_rejects_missing_or_untrusted_capture_proof(bridge, proven, uncertainty):
    clock, replies, calls, frame = bridge
    replies.append(frame(10, clock.ns, proven=proven, uncertainty=uncertainty))
    with pytest.raises(RuntimeError, match="采样时刻|采样证明"):
        debug.fetch_camera_frame("arm-01", not_before_monotonic_ns=clock.ns)
    assert len(calls) == 1


def test_recovery_capture_polling_has_a_finite_deadline(bridge):
    clock, replies, calls, frame = bridge
    fence = clock.ns
    # 限定所有迟到帧都落在原先的新鲜度预算内，只测动作完成采样门。
    monkey_freshness = real_camera.HttpFrameSource._fetch_validated

    def repeat_old(source):
        source.max_age_seconds = 100
        return monkey_freshness(source)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(real_camera.HttpFrameSource, "_fetch_validated", repeat_old)
        replies.append(frame(10, fence - 100_000_000))
        with pytest.raises(RuntimeError, match="超时.*采样|采样.*超时"):
            debug.fetch_camera_frame("arm-01", not_before_monotonic_ns=fence)
    assert 1 < len(calls) <= 65
    assert clock.ns - fence <= 3_100_000_000


def test_regular_fetch_preserves_legacy_behavior_without_capture_proof(bridge):
    _clock, replies, calls, frame = bridge
    replies.append(frame(10, 0, proven=False))
    assert debug.fetch_camera_frame("arm-01") == (b"jpeg-10", 10)
    assert len(calls) == 1


def test_capture_proof_returned_after_deadline_cannot_authorize_recovery(bridge, monkeypatch):
    clock, replies, calls, frame = bridge
    fence = clock.ns
    replies.append(frame(10, fence + 100_000_000))
    original_fetch = real_camera.HttpFrameSource.fetch_bytes

    def delayed_fetch(source):
        result = original_fetch(source)
        clock.ns += 3_100_000_000
        return result

    monkeypatch.setattr(real_camera.HttpFrameSource, "fetch_bytes", delayed_fetch)
    with pytest.raises(RuntimeError, match="采样超时"):
        debug.fetch_camera_frame("arm-01", not_before_monotonic_ns=fence)
    assert len(calls) == 1
    assert 0 < calls[0] <= 3


def test_fetch_bytes_exposes_sample_metadata_without_committing_decoded_sequence(bridge):
    clock, replies, _calls, frame = bridge
    replies.append(frame(10, clock.ns))
    source = real_camera.HttpFrameSource("http://fake/latest.jpg")
    assert source.fetch_bytes() == (b"jpeg-10", 10)
    assert source.last_source_captured_monotonic_ns == clock.ns
    assert source.last_source_capture_uncertainty_ms == 1
    assert source.last_source_timing_provenance == "camera_pts_host_clock.v1"
    assert source._last_seq is None
    assert source._last_seq_generation is None
    assert source._retired_seq_generations == set()


@pytest.mark.parametrize("failure_stage", [None, "before", "after"])
def test_anchor_verification_fences_both_captures_and_returns_on_failure(
        monkeypatch, failure_stage):
    import cv2
    import numpy as np
    from drivers import screen_pose
    from runtime import recovery

    clock = SimpleNamespace(ns=1)
    monkeypatch.setattr(debug, "time", SimpleNamespace(monotonic_ns=lambda: clock.ns))
    moves, fences = [], []
    corners = [[100, 100], [1180, 100], [1180, 620], [100, 620]]
    bundle = {
        "xy": {"validation": [{"effectiveRobotXY": [30., 0.],
                               "targetScreen": [368., 220.]}]},
        "z": {"hoverZ": 2., "tapDepth": 8., "tapFeed": 2000.},
        "camera": {"cameraToScreen": {"homography": np.eye(3).tolist()}},
        "screen": {"edgeModel": {"corners": corners}},
    }
    monkeypatch.setattr(debug.calibration_bundle, "load_bundle", lambda *args: bundle)

    def position():
        clock.ns = 11
        return {"x": 0., "y": 0., "z": 2., "state": "Idle"}

    monkeypatch.setattr(debug, "daemon_for", lambda _: SimpleNamespace(
        status=lambda: {"session": "same"}, mpos=position))

    def move(_device, rx, ry, **kwargs):
        assert kwargs["tap"] is False
        moves.append((rx, ry))
        clock.ns += 100

    def capture(_device, *, not_before_monotonic_ns):
        fences.append(not_before_monotonic_ns)
        assert not_before_monotonic_ns == clock.ns
        if failure_stage == ("before" if len(fences) == 1 else "after"):
            raise RuntimeError("动作后采样超时")
        return b"jpeg", len(fences)

    monkeypatch.setattr(debug, "do_goto_robot", move)
    monkeypatch.setattr(debug, "fetch_camera_frame", capture)
    monkeypatch.setattr(cv2, "imdecode", lambda *args: np.zeros((720, 1280, 3), np.uint8))
    monkeypatch.setattr(recovery, "locate_recovery_screen", lambda *args: {"corners": corners})
    monkeypatch.setattr(recovery, "observe_recovery_probe", lambda *args: (368., 220.))
    monkeypatch.setattr(screen_pose, "fit_pose", lambda *args: {"matrix": np.eye(3).tolist()})
    monkeypatch.setattr(debug, "log_op", lambda *args, **kwargs: None)
    if failure_stage:
        with pytest.raises(RuntimeError, match="采样超时"):
            debug._arm_anchor_verify("arm-01")
    else:
        assert debug._arm_anchor_verify("arm-01") is True
    if failure_stage == "before":
        assert moves == []
        assert fences == [11]
    else:
        assert moves == [(30., 0.), (0., 0.)]
        assert fences == [11, 111]
