"""R8-F1（执行单 M1.1）：流撤销 ≤5s——时间线定位与 20 次验收。

修正第七轮测试的三处测量缺陷：
- DELETE 经 TestClient（第二事件循环）→撤销完成延迟被计入流停止时间；
  本测试全部请求经同一真实 uvicorn 服务（httpx），撤销计时起点=DELETE
  响应返回（closed 已持久），另报撤销请求总耗时供归因。
- 先收完整图像帧（PNG 魔数）才允许撤销；`assert chunks` 只证 multipart 边界。
- 超过 deadline 时客户端主动 break 判失败（不记作服务端已停止）；EOF
  （iter_bytes 正常耗尽）是唯一合格终止证据。

时间线：t_revoke_sent → t_revoke_status（DELETE 响应=closed 已写）→
t_closed_persisted（store 复核）→ t_last_frame（撤销后最后字节）→
t_client_eof。断言 t_client_eof − t_revoke_status ≤ 5.0（规格原值）。

负载：并发订阅（同设备第二查看会话的读者）+ store 写线程；20 次中
≥10 次在负载下。替身：合成相机帧（外部 IO）；真实 Runtime/views/路由/
uvicorn/流循环保留。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import httpx
import uvicorn

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs

# 证据输出隔离（第十轮证据卡）：样本写入 run 专属目录（环境变量
# WX_TEST_EVIDENCE_DIR 指定，文件已存在即拒绝——不允许覆盖任何已登记
# 证据）；未配置时默认临时测试根，不落共享路径。第九轮复核重跑曾覆盖
# 实施者样本，此规则防止复发。
def _samples_report_path(tmp_path: Path) -> Path:
    env = os.environ.get("WX_TEST_EVIDENCE_DIR", "").strip()
    if not env:
        return tmp_path / "r8-f1-revocation-samples.json"
    root = Path(env)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "r8-f1-revocation-samples.json"
    if target.exists():
        raise AssertionError(f"证据文件已存在，拒绝覆盖：{target}（换新 run 目录）")
    return target


@pytest.fixture()
def server_env(tmp_path):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
        "camera_frame_interval_seconds": 0.2,
        # 负载真实生效前提：主会话+2 并发订阅者同时存活（默认上限 2 会让
        # 订阅者创建全部失败——第十轮实测旧负载仅 1/20 生效，见 close 报告）。
        "view_session_max_per_device": 4,
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
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error"))
    config_server = uvicorn.Config(app, host="127.0.0.1", port=0)
    server = uvicorn.Server(config_server)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    # port=0 时从 server 拿实际端口
    port = server.servers[0].sockets[0].getsockname()[1] if getattr(
        server, "servers", None) else 0
    assert port, "未能取得动态端口"
    yield {"base": f"http://127.0.0.1:{port}", "runtime": runtime}
    server.should_exit = True
    thread.join(timeout=5)


def _create_session(base):
    body = {
        "device_id": "arm-01", "purpose": "主动查看",
        "requested_by": "revocation-test", "correlation_id": str(uuid.uuid4()),
        "client_request_id": str(uuid.uuid4()), "role": "readonly",
    }
    r = httpx.post(base + "/v1/camera/view-sessions", json=body, timeout=5)
    assert r.status_code == 200, r.text
    return r.json()["view_session_id"]


def _one_revocation_run(base, runtime, *, load=False, stall=False,
                        load_stats=None):
    """单次撤销计时。返回时间线 dict；EOF 超时抛错（不合格终止）。

    完整帧判定（第十轮证据卡）：按 multipart boundary+Content-Length 解析
    出完整图像体，PNG 魔数仅作附加校验，不单独作为"已收到完整帧"依据。
    """
    sid = _create_session(base)
    stop_load = threading.Event()
    load_threads = []

    def _store_writer():
        i = 0
        while not stop_load.is_set():
            runtime.audit.record("r8f1_load_write", reason=f"load-{i}")
            i += 1
            if load_stats is not None:
                load_stats["store_writes"] = load_stats.get("store_writes", 0) + 1
            time.sleep(0.02)

    def _subscriber():
        # 并发订阅：同设备另一查看会话持续读流（读 2.5s 后自然退出并关闭，
        # 释放会话名额——不关闭会在 60s TTL 内占坑，令后续订阅全部失败）
        other = None
        try:
            other = _create_session(base)
            if load_stats is not None:
                load_stats["subscriber_sessions_created"] += 1
            with httpx.Client(timeout=httpx.Timeout(3.0, read=2.5)) as hc:
                with hc.stream("GET", f"{base}/v1/camera/view-sessions/{other}/stream") as st:
                    for _ in st.iter_bytes():
                        if stop_load.is_set():
                            break
            if load_stats is not None:
                load_stats["subscriber_streams_ok"] += 1
        except Exception as exc:
            if load_stats is not None:
                key = f"subscriber_error:{type(exc).__name__}:{str(exc)[:120]}"
                load_stats[key] = load_stats.get(key, 0) + 1
        finally:
            if other:
                try:
                    with httpx.Client() as hc:
                        hc.request("DELETE",
                                   f"{base}/v1/camera/view-sessions/{other}",
                                   json={"reason": "负载订阅者退出"}, timeout=2.0)
                except Exception as exc:
                    if load_stats is not None:
                        key = f"cleanup_error:{type(exc).__name__}"
                        load_stats[key] = load_stats.get(key, 0) + 1

    if load:
        for _ in range(2):
            t = threading.Thread(target=_subscriber, daemon=True)
            t.start()
            load_threads.append(t)
        w = threading.Thread(target=_store_writer, daemon=True)
        w.start()
        load_threads.append(w)

    timeline = {"load": load, "frames_before_revoke": 0,
                "png_frames_before_revoke": 0,
                "complete_frames_before_revoke": 0}

    def _feed_complete_frames(buf: bytes) -> int:
        """解析 buf 内 multipart 段：Content-Length 与实际体长一致的才计数。"""
        count = 0
        for part in buf.split(b"\r\n--armfleetframe\r\n"):
            head, _, body = part.partition(b"\r\n\r\n")
            if not head or not body:
                continue
            length = None
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    try:
                        length = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        length = None
            if length is not None and len(body) >= length:
                count += 1
        return count

    try:
        deadline_guard = 12.0
        with httpx.Client(timeout=httpx.Timeout(5.0, read=deadline_guard)) as hc:
            with hc.stream("GET",
                           f"{base}/v1/camera/view-sessions/{sid}/stream") as stream:
                assert stream.status_code == 200
                buf = b""
                got_frame = False
                revoked = False
                overrun = False
                t0 = time.monotonic()
                t_revoke_sent = t_revoke_status = t_closed = None
                t_last_byte = None
                for chunk in stream.iter_bytes():
                    now = time.monotonic()
                    t_last_byte = now
                    if not revoked:
                        buf += chunk
                        timeline["frames_before_revoke"] += chunk.count(b"--armfleetframe")
                        timeline["png_frames_before_revoke"] = buf.count(b"\x89PNG")
                        timeline["complete_frames_before_revoke"] = (
                            _feed_complete_frames(buf))
                        if timeline["complete_frames_before_revoke"] >= 1:
                            got_frame = True
                        if got_frame and now - t0 >= 1.2:
                            # 撤销：同一真实服务的 HTTP DELETE（新连接；流继续读）
                            t_revoke_sent = now
                            resp = hc.request(
                                "DELETE",
                                f"{base}/v1/camera/view-sessions/{sid}",
                                json={"reason": "R8-F1 撤销测试"}, timeout=5.0)
                            t_revoke_status = time.monotonic()
                            timeline["delete_status"] = resp.status_code
                            timeline["revoke_http_s"] = round(
                                t_revoke_status - t_revoke_sent, 3)
                            assert resp.status_code == 200, resp.text
                            stored = runtime.views.get(sid)
                            assert stored["status"] == "closed", stored
                            t_closed = time.monotonic()
                            timeline["closed_to_eof_s"] = None
                            revoked = True
                        continue
                    # 撤销后：越 5.0s 仍供给=失败记录（客户端 break 非成功）
                    if now - t_revoke_status > 5.0:
                        overrun = True
                        break
                # 迭代正常耗尽=服务端 EOF；overrun 时不合格
                t_eof = time.monotonic()
                assert timeline["complete_frames_before_revoke"] >= 1, \
                    "撤销前必须先解析出完整图像帧（multipart+Content-Length），" \
                    "否则测量无效"
                timeline["overrun_after_deadline"] = overrun
                timeline["stop_latency_s"] = round(
                    (t_last_byte or t_eof) - t_revoke_status, 3)
                timeline["eof_latency_s"] = round(t_eof - t_revoke_status, 3)
                timeline["closed_to_eof_s"] = round(t_eof - t_closed, 3)
                assert not overrun, f"撤销后 >5s 仍在供给：{timeline}"
                assert t_eof - t_revoke_status <= 5.0, \
                    f"EOF 超限：{timeline}"
    finally:
        stop_load.set()
        for t in load_threads:
            t.join(timeout=3)
    return timeline


def test_r8_f1_single_run_timeline(server_env):
    base, runtime = server_env["base"], server_env["runtime"]
    tl = _one_revocation_run(base, runtime)
    assert tl["delete_status"] == 200 and tl["png_frames_before_revoke"] >= 1
    assert tl["eof_latency_s"] <= 5.0, tl


def test_r8_f1_twenty_runs_with_load(server_env, tmp_path):
    """连续 20 次（10 轻载+10 负载）全部 ≤5.0s；样本全量落盘。

    负载记录实际成功订阅数与写入数（第十轮证据卡：不能只标 load=true）。
    """
    base, runtime = server_env["base"], server_env["runtime"]
    report = _samples_report_path(tmp_path)
    samples = []
    load_stats = {"subscriber_sessions_created": 0, "subscriber_streams_ok": 0}
    try:
        for i in range(20):
            tl = _one_revocation_run(
                base, runtime, load=(i >= 10), load_stats=load_stats)
            samples.append(tl)
    finally:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(
            {"kind": "r8-f1-revocation-samples", "runs": samples,
             "load_stats": load_stats,
             "max_stop_latency": max(s["stop_latency_s"] for s in samples),
             "max_eof_latency": max(s["eof_latency_s"] for s in samples)},
            ensure_ascii=False, indent=1), encoding="utf-8")
    loaded = [s for s in samples if s["load"]]
    assert len(loaded) >= 10 and all(s["load"] for s in samples[10:])
    # 负载为实际生效证据：每负载轮应有 2 个并发订阅会话成功建立且读到流
    assert load_stats["subscriber_sessions_created"] >= 20
    assert load_stats["subscriber_streams_ok"] >= 20
    worst = max(s["eof_latency_s"] for s in samples)
    assert worst <= 5.0, [s for s in samples if s["eof_latency_s"] > 5.0]
    assert all(s["png_frames_before_revoke"] >= 1 for s in samples)
