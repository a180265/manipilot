"""R8-F1 尾项：preview 分支撤销终止（含上游停帧）。

静态风险反证（执行单 R8-F1.3）：preview 读取 timeout=None，只在收到
chunk 时复核会话——上游停帧后撤销永不终止（>5s，违反 R4-12-E）。
替身：假 preview 媒体面（线程 HTTP：/health 200 + mjpeg 先供 N chunk 后
停发保持连接）；真实 Runtime/views/路由/uvicorn/流循环保留。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import httpx
import uvicorn

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs


class _PreviewUpstream(BaseHTTPRequestHandler):
    stall_after = 3   # N chunk 后停发（保持连接）
    resume_after = 0  # >0：停发 resume_after 秒后恢复供流（误杀反例用）

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/health"):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=preview")
        self.end_headers()
        for i in range(type(self).stall_after):
            self.wfile.write(f"--preview\r\nContent-Type: image/jpeg\r\n"
                             f"Content-Length: 4\r\n\r\nfake{i}".encode()
                             + b"\r\n")
            self.wfile.flush()
            time.sleep(0.1)
        if type(self).resume_after > 0:
            # 短暂停帧（>看门狗窗）后恢复——看门狗不得误杀
            time.sleep(type(self).resume_after)
            i = 0
            while True:
                self.wfile.write(f"--preview\r\nContent-Type: image/jpeg\r\n"
                                 f"Content-Length: 4\r\n\r\nrecov{i}".encode()
                                 + b"\r\n")
                self.wfile.flush()
                time.sleep(0.1)
                i += 1
        # 停帧：保持连接不发数据（模拟上游采集停滞）
        time.sleep(30)

    def log_message(self, *args):
        pass


@pytest.fixture()
def preview_stack(tmp_path):
    preview = ThreadingHTTPServer(("127.0.0.1", 0), _PreviewUpstream)
    pport = preview.server_address[1]
    threading.Thread(target=preview.serve_forever, daemon=True).start()

    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
        "camera_preview_enabled": True,
        "camera_preview_host": "127.0.0.1",
        "camera_preview_port": pport,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime = runtime
    asyncio.run(runtime.devices.register_all(make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=1,
        daemon_addr="127.0.0.1:8795", expected_frame_width=640,
        expected_frame_height=480)))
    app.state.device_configs = []
    runtime.devices._devices["arm-01"].update({
        "status": "ready", "calibration_status": "validated"})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                                           port=0, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    yield {"base": f"http://127.0.0.1:{port}", "runtime": runtime}
    server.should_exit = True
    preview.shutdown()


def _create(base):
    r = httpx.post(base + "/v1/camera/view-sessions", json={
        "device_id": "arm-01", "purpose": "主动查看",
        "requested_by": "preview-stall-test",
        "correlation_id": str(uuid.uuid4()),
        "client_request_id": str(uuid.uuid4())}, timeout=5)
    assert r.status_code == 200, r.text
    return r.json()["view_session_id"]


def test_preview_stall_revocation_stops_within_5s(preview_stack):
    """上游停帧+服务端撤销：流必须在 5s 内终止（EOF），不能等上游。"""
    base, runtime = preview_stack["base"], preview_stack["runtime"]
    sid = _create(base)
    with httpx.Client(timeout=httpx.Timeout(5.0, read=12.0)) as hc:
        with hc.stream("GET",
                       f"{base}/v1/camera/view-sessions/{sid}/stream") as stream:
            assert stream.status_code == 200
            got_data = False
            t_revoke_status = None
            eof = None
            for chunk in stream.iter_bytes():
                got_data = True
                if t_revoke_status is None:
                    # 收到首批数据即撤销（上游 3 chunk 后将停帧）
                    resp = hc.request(
                        "DELETE", f"{base}/v1/camera/view-sessions/{sid}",
                        json={"reason": "preview 停帧撤销"}, timeout=5.0)
                    assert resp.status_code == 200, resp.text
                    t_revoke_status = time.monotonic()
                if time.monotonic() - t_revoke_status > 5.0:
                    break  # 超限=失败（EOF 未在 5s 内发生）
            eof = time.monotonic()
            assert got_data
            assert t_revoke_status is not None
            assert eof - t_revoke_status <= 5.0, (
                f"preview 停帧下撤销后流未在 5s 内终止：{eof - t_revoke_status:.2f}s")


def test_preview_normal_flow_revocation_stops_within_5s(preview_stack):
    """preview 正常供流（非停帧）时撤销：chunk 流动路径同样 ≤5s 终止。"""
    _PreviewUpstream.stall_after = 100   # 持续供流
    try:
        base, runtime = preview_stack["base"], preview_stack["runtime"]
        sid = _create(base)
        with httpx.Client(timeout=httpx.Timeout(5.0, read=12.0)) as hc:
            with hc.stream("GET",
                           f"{base}/v1/camera/view-sessions/{sid}/stream") as stream:
                assert stream.status_code == 200
                t_revoke = None
                for chunk in stream.iter_bytes():
                    if t_revoke is None:
                        resp = hc.request(
                            "DELETE", f"{base}/v1/camera/view-sessions/{sid}",
                            json={"reason": "preview 正常流撤销"}, timeout=5.0)
                        assert resp.status_code == 200
                        t_revoke = time.monotonic()
                    if time.monotonic() - t_revoke > 5.0:
                        break
                eof = time.monotonic()
                assert t_revoke is not None
                assert eof - t_revoke <= 5.0, (
                    f"正常流撤销未在 5s 内终止：{eof - t_revoke:.2f}s")
    finally:
        _PreviewUpstream.stall_after = 3


def test_preview_stall_over_1s_recovers_no_false_kill(preview_stack):
    """执行单 R4-12-E.6 看门狗不误杀反例：preview 未撤销、短暂停帧 >1s
    再恢复——流必须存活并恢复供帧（不能在第一次看门狗超时就杀流）。

    修前红：wait_for 直接取消 httpx aiter.__anext__ 破坏底层流状态，
    上游恢复后流已死（EOF+unavailable）——产品缺陷实证。
    """
    _PreviewUpstream.stall_after = 3
    _PreviewUpstream.resume_after = 1.5
    try:
        base, runtime = preview_stack["base"], preview_stack["runtime"]
        sid = _create(base)
        t0 = time.monotonic()
        frame_times = []
        with httpx.Client(timeout=httpx.Timeout(5.0, read=9.0)) as hc:
            with hc.stream("GET",
                           f"{base}/v1/camera/view-sessions/{sid}/stream") as stream:
                assert stream.status_code == 200
                for chunk in stream.iter_bytes():
                    now = time.monotonic()
                    frame_times += [now] * chunk.count(b"--preview")
                    if now - t0 >= 6.0:
                        break  # 客户端观察窗结束（主动，非服务端终止）
        early = [t for t in frame_times if t - t0 < 2.0]
        late = [t for t in frame_times if t - t0 >= 4.0]  # 停帧 1.5s+恢复后
        gap = max([b - a for a, b in zip(frame_times, frame_times[1:])] or [0])
        assert len(early) >= 3, f"停帧前帧不足：{len(early)}"
        assert gap >= 1.0, f"未观测到 >1s 停帧窗（maxGap={gap:.2f}s）"
        assert len(late) >= 3, (
            f"恢复后无帧（误杀/流死）：late={len(late)} total={len(frame_times)}")
        stored = runtime.views.get(sid, refresh=False)
        assert stored["status"] in ("valid", "expiring", "waiting"), (
            f"会话被误终态：{stored['status']} {stored.get('close_reason')}")
    finally:
        _PreviewUpstream.stall_after = 3
        _PreviewUpstream.resume_after = 0


def test_no_revocation_stream_keeps_supplying(serverless_guard=None):
    """未撤销时流持续供给（防看门狗误杀正常查看）。复用 r8 主套件环境。"""
    import asyncio as _aio
    import threading as _th
    from http.server import BaseHTTPRequestHandler as _H, ThreadingHTTPServer as _S
    # 直接复用 preview_stack 的构造太重：用回退分支（合成相机持续供帧）
    from runtime.config import PlatformConfig as _PC
    from runtime.runtime import ArmFleetRuntime as _RT
    from api.app import create_app as _ca
    import tempfile as _tf
    from pathlib import Path as _P
    tmp = _P(_tf.mkdtemp())
    config = _PC({
        "db_path": str(tmp / "f.db"), "evidence_dir": str(tmp / "ev"),
        "plugin_dir": str(tmp / "pl"), "hardware_mode": "simulation",
        "daemon_supervisor_enabled": False, "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False, "camera_frame_interval_seconds": 0.1,
    }, root=tmp)
    runtime = _RT(config)
    app = _ca(runtime)
    app.state.runtime = runtime
    _aio.run(runtime.devices.register_all(make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=1,
        daemon_addr="127.0.0.1:8795", expected_frame_width=640,
        expected_frame_height=480)))
    app.state.device_configs = []
    runtime.devices._devices["arm-01"].update({
        "status": "ready", "calibration_status": "validated"})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                                           port=0, log_level="error"))
    _th.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        sid = _create(base)
        with httpx.Client(timeout=httpx.Timeout(5.0, read=6.0)) as hc:
            with hc.stream("GET",
                           f"{base}/v1/camera/view-sessions/{sid}/stream") as stream:
                t0 = time.monotonic()
                frames = 0
                for chunk in stream.iter_bytes():
                    frames += chunk.count(b"--armfleetframe")
                    if time.monotonic() - t0 >= 2.0:
                        break  # 2s 观察窗：客户端主动结束（非服务端终止断言）
        assert frames >= 5, f"未撤销时流不应中断/减速：{frames} 帧/2s"
    finally:
        server.should_exit = True
