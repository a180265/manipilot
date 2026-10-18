"""真实 v2 上传/取流保留来源，受控新会话可重置消费序号；不接设备。"""
from __future__ import annotations

from datetime import datetime, timezone
import time

import pytest

from drivers.real_camera import HttpFrameSource
from tests.test_r3_10_source_session_timing import (
    _http, bind_channel, jpeg_bytes, register_session, timing_env,
)


def _upload(env, binding, session, seq, *, source="camera_helper", blob=None):
    headers = {
        **env["auth"],
        "Content-Type": "image/jpeg",
        "X-Camera-Binding-Contract": "camera-binding.v2",
        "X-Camera-Id": "cam-01",
        "X-Camera-Unique-Id": "uvc-01",
        "X-Camera-Binding-Id": binding,
        "X-Source-Geometry-Id": session["source_geometry_id"],
        "X-Source-Generation-Id": session["source_generation_id"],
        "X-Source-Frame-Seq": str(seq),
        "X-Source-Clock-Id": env["clock_id"],
        "X-Source-Captured-Monotonic-Ns": str(time.monotonic_ns()),
        "X-Source-Capture-Uncertainty-Ms": "1",
        "X-Source-Timing-Provenance": "camera_pts_host_clock.v1",
        "X-Captured-At": datetime.now(timezone.utc).isoformat(),
    }
    if source is not None:
        headers["X-Frame-Source"] = source
    return _http(env["base"], "/cameras/cam-01/frame", method="POST",
                 body=blob or jpeg_bytes(), headers=headers)


def _session(env, binding, **kwargs):
    status, session = register_session(env, binding, **kwargs)
    assert status == 200, session
    return session


def _consumer(env):
    return HttpFrameSource(
        env["base"] + "/cameras/cam-01/latest.jpg",
        expected_camera_id="cam-01", expected_device_id="arm-01",
        expected_device_unique_id="uvc-01", expected_width=640,
        expected_height=480)


def test_v2_get_preserves_explicit_helper_source(timing_env):
    env = timing_env
    binding = bind_channel(env)
    session = _session(env, binding)
    blob = jpeg_bytes((23, 45, 67))
    status, receipt = _upload(env, binding, session, 40, blob=blob)
    assert status == 200, receipt
    status, headers, served = _http(
        env["base"], "/cameras/cam-01/latest.jpg", raw=True)
    assert status == 200 and served == blob
    assert headers["X-Frame-Source"] == "camera_helper"
    assert headers["X-Source-Generation-Id"] == session["source_generation_id"]
    consumer = _consumer(env)
    consumer()
    assert consumer.last_frame_source == "camera_helper"


def test_real_v2_new_session_restarts_http_consumer_sequence(timing_env):
    env = timing_env
    binding = bind_channel(env)
    first = _session(env, binding)
    for seq in (40, 41):
        status, receipt = _upload(env, binding, first, seq)
        assert status == 200, receipt
    consumer = _consumer(env)
    consumer()
    assert consumer.last_frame_seq == 2
    second = _session(env, binding, replaces=first["source_generation_id"])
    status, receipt = _upload(env, binding, second, 1)
    assert status == 200 and receipt["bridge_seq"] == 1, receipt
    consumer()
    assert consumer.last_frame_seq == 1
    assert consumer.last_source_generation_id == second["source_generation_id"]
    assert consumer.last_frame_source == "camera_helper"


@pytest.mark.parametrize("source", [None, "", "chrome_compat", "legacy", "unknown"])
def test_v2_missing_or_other_source_cannot_publish_frame(timing_env, source):
    env = timing_env
    binding = bind_channel(env)
    session = _session(env, binding)
    status, body = _upload(env, binding, session, 1, source=source)
    assert status == 422, body
    assert body["error"]["code"] == "camera_request_invalid"
    status, view = _http(env["base"], "/cameras/cam-01/status")
    assert status == 200 and view["has_frame"] is False


@pytest.mark.parametrize("source", [None, "chrome_compat"])
def test_retransmission_cannot_bypass_explicit_source_check(timing_env, source):
    env = timing_env
    binding = bind_channel(env)
    session = _session(env, binding)
    blob = jpeg_bytes()
    status, receipt = _upload(env, binding, session, 1, blob=blob)
    assert status == 200, receipt
    status, body = _upload(env, binding, session, 1, source=source, blob=blob)
    assert status == 422, body
    status, headers, served = _http(
        env["base"], "/cameras/cam-01/latest.jpg", raw=True)
    assert status == 200 and served == blob
    assert headers["X-Frame-Source"] == "camera_helper"
    assert headers["X-Frame-Seq"] == str(receipt["bridge_seq"])
