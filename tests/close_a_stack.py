"""close-A 工作域共享测试栈（R4-02/R3-15/R4-08-D 浏览器链）。

只替换外部 IO（授权边界允许的假物理）：
- 相机：合成 MJPEG/JPEG 假上游（线程 HTTP，动态端口；identity 可切换）；
- 机械臂：arm_daemon3 协议假 TCP（每设备一个实例，动态端口；记录全部指令，
  运动类指令单独计数供"零动作"断言）；
- Fleet 上游：真实 create_app + uvicorn（动态端口，临时数据根）+ 合成 v2
  源帧连续推进（真实 EvidenceStore 归档，真实检测/保存/指针 CAS）；
- 调试台：真实 motor_debug_server 子进程（动态端口 + 临时数据根，
  MOTOR_DEBUG_DATA_DIR），重启=同数据根新进程。

保留真实：页面、console HTTP 代理与上下文校验、Fleet 全部路由与布局服务、
证据链、wire 契约、真实 Chrome 鼠标事件。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
ARM = REPO / "arm_fleet"

sys.path.insert(0, str(ARM))

# 与 layout_d2_helpers 相同的合成场景（640x480 源帧 + 手机矩形）
from tests.layout_d2_helpers import (  # noqa: E402
    BINDING, FrameBridgeFake, phone_scene)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


MOTION_COMMANDS = {"Z", "XY", "TAP", "TAP2", "SWIPE", "GOTO", "RESUME",
                   "BEGIN_MOTION", "END_MOTION"}


class FakeDaemon:
    """arm_daemon3 最小协议仿真（welcome 带 device=；指令全记录）。

    运动指令单独计数：Z/XY/TAP/TAP2/SWIPE/GOTO/RESUME 及动作事务。
    STATUS/MPOS/CLAIM/RENEW/RELEASE/STOP/ESTOP_LOCK/RESET_ESTOP 只读或
    租约管理，不计为动作。
    """

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.session = f"sess-{device_id}"
        self.mpos = [2.0, 2.0, 5.0]
        self.commands: list[tuple[str, str]] = []   # (conn_seq, line)
        self.motion_count = 0
        self._conn_seq = iter(range(1, 10 ** 6))
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._srv.listen(8)
        self._srv.settimeout(0.05)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,),
                             daemon=True).start()

    def _client(self, conn: socket.socket):
        seq = next(self._conn_seq)
        f = None
        try:
            conn.sendall(f"arm-daemon3 ready v3 device={self.device_id}\n".encode())
            f = conn.makefile("rwb")
            while True:
                line = f.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if not text:
                    continue
                self.commands.append((str(seq), text))
                parts = text.split()
                cmd = parts[0].upper()
                if cmd in MOTION_COMMANDS:
                    self.motion_count += 1
                resp = self._handle(cmd, parts)
                if resp is None:
                    break
                conn.sendall((resp + "\n").encode())
        except OSError:
            pass
        finally:
            if f is not None:
                f.close()
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, cmd: str, parts: list[str]) -> str | None:
        if cmd == "BYE":
            return None
        if cmd == "STATUS":
            return json.dumps({"connected": True, "deviceId": self.device_id,
                               "devicePresent": True, "session": self.session,
                               "motionFeatures": ["tap2.v1", "swipe.v1"],
                               "estopLocked": False, "lease": None})
        if cmd == "MPOS":
            return json.dumps({"x": self.mpos[0], "y": self.mpos[1],
                               "z": self.mpos[2], "state": "Idle"})
        if cmd == "CLAIM":
            return json.dumps({"claimed": True,
                               "lease_id": uuid.uuid4().hex})
        if cmd == "RENEW":
            return '{"renewed": true}'
        if cmd == "RELEASE":
            return '{"released": true}'
        if cmd == "STOP":
            return '{"stopped": true}'
        if cmd == "ESTOP_LOCK":
            return '{"estop_locked": true, "stopped": true}'
        if cmd == "RESET_ESTOP":
            return '{"estop_locked": false}'
        if cmd in MOTION_COMMANDS:
            return json.dumps({"x": self.mpos[0], "y": self.mpos[1],
                               "z": self.mpos[2], "state": "Idle"})
        return "ERROR: 未实现的指令（假 daemon）"


def _camera_jpeg(width: int = 1280, height: int = 720) -> bytes:
    """合成相机帧（仅预览流显示用；region 证据帧来自 FrameBridgeFake）。"""
    import cv2
    canvas = np.full((height, width, 3), 24, dtype=np.uint8)
    cv2.rectangle(canvas, (200, 60), (1000, 660), (60, 60, 60), -1)
    ok, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    assert ok
    return buf.tobytes()


class FakeCamera:
    """分路相机假上游：/cameras/{id}/status 与 /latest.jpg（MJPEG 拉流源）。

    identity 可整体切换（device_unique_id/binding_revision）——模拟换绑相机。
    """

    def __init__(self):
        self._lock = threading.Lock()
        # camera_id -> {device_id, unique_id, revision, width, height}
        self.cameras: dict[str, dict] = {}
        self.seq = 0
        self._stop = threading.Event()
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True)

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parts = self.path.split("?")[0].strip("/").split("/")
                # /cameras/{id}/status | /cameras/{id}/latest.jpg
                if len(parts) >= 3 and parts[0] == "cameras":
                    camera_id = parts[1]
                    tail = "/".join(parts[2:])
                    info = outer.cameras.get(camera_id)
                    if info is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    if tail == "status":
                        with outer._lock:
                            outer.seq += 1
                            seq = outer.seq
                        payload = json.dumps({
                            "camera_id": camera_id,
                            "device_id": info["device_id"],
                            "device_unique_id": info["unique_id"],
                            "binding_revision": info["revision"],
                            "status": "streaming",
                            "has_frame": True,
                            "width": info["width"], "height": info["height"],
                            "frame_seq": seq, "seq": seq,
                        }).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    if tail == "latest.jpg":
                        with outer._lock:
                            outer.seq += 1
                            seq = outer.seq
                        data = _camera_jpeg()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("X-Camera-Id", camera_id)
                        self.send_header("X-Device-Id", info["device_id"])
                        self.send_header("X-Frame-Seq", str(seq))
                        self.end_headers()
                        self.wfile.write(data)
                        # 限速：避免调试台 MJPEG 代理空转打满 CPU
                        time.sleep(0.05)
                        return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        return H

    def register(self, camera_id: str, device_id: str, *, unique_id: str,
                 revision: int, width: int = 1280, height: int = 720):
        self.cameras[camera_id] = {"device_id": device_id, "unique_id": unique_id,
                                   "revision": revision, "width": width,
                                   "height": height}

    def switch_identity(self, camera_id: str, *, unique_id: str, revision: int):
        with self._lock:
            self.cameras[camera_id]["unique_id"] = unique_id
            self.cameras[camera_id]["revision"] = revision

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._srv.shutdown()
        self._srv.server_close()


class FramePusher:
    """连续推进合成 v2 源帧（预览/保存的新鲜帧窗 ≤2s 必须）。

    mode: phone（亮手机，可 verified）| dark（暗屏，保存复验失败）
    换相机=整体替换 binding（camera_binding_id/geometry/unique_id）。
    """

    def __init__(self, bridge: FrameBridgeFake):
        self.bridge = bridge
        self.mode = "phone"
        self.phone = (200, 120, 240, 240)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(0.4):
            try:
                self.push()
            except Exception:
                pass

    def push(self) -> None:
        if self.mode == "dark":
            image = np.full((480, 640, 3), 18, dtype=np.uint8)
        else:
            image = phone_scene(phone=self.phone)
        self.bridge.push(image)

    def start(self):
        # 先同步推一帧：首次 phone_scene 会触发 cv2 导入（约 1 秒），
        # 若发生在推帧线程里，测试前几百毫秒会没有可用新鲜帧。
        self.push()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)


CONSOLE_LAUNCHER = r"""
import sys
sys.path.insert(0, {arm_root!r})
import scripts.motor_debug_server as m
m.LISTEN_HOST = "127.0.0.1"
m.LISTEN_PORT = int(sys.argv[1])
m.main()
"""


class ConsoleProcess:
    """真实 motor_debug_server 子进程（动态端口 + 临时数据根）。

    env：IME_BASE_URL=Fleet、ARMFLEET_DB_PATH=临时 fleet.db、
    ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE=受信转发令牌、
    MOTOR_DEBUG_DATA_DIR=临时数据根（ops/向导/档案不落仓库）。
    """

    def __init__(self, *, fleet_base: str, db_path: Path, data_dir: Path,
                 token_file: Path, venv_python: str | None = None):
        self.fleet_base = fleet_base
        self.db_path = db_path
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.token_file = token_file
        self.python = venv_python or sys.executable
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def _env(self) -> dict:
        env = dict(os.environ)
        env["IME_BASE_URL"] = self.fleet_base
        env["ARMFLEET_DB_PATH"] = str(self.db_path)
        env["ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE"] = str(self.token_file)
        env["MOTOR_DEBUG_DATA_DIR"] = str(self.data_dir)
        env["MOTOR_DEBUG_DATA_ROOT"] = str(self.data_dir)
        env.pop("CAMERA_CONTROL_TOKEN_FILE", None)
        return env

    def start(self, timeout: float = 30.0):
        code = CONSOLE_LAUNCHER.format(arm_root=str(ARM))
        self.proc = subprocess.Popen(
            [self.python, "-c", code, str(self.port)],
            env=self._env(), cwd=str(ARM),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"调试台子进程提前退出：{out[-2000:]}")
            try:
                with urllib.request.urlopen(self.base + "/", timeout=2) as r:
                    if r.status == 200:
                        return self
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("调试台子进程未就绪")

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None


class DelayASGI:
    """Fleet 侧可控延迟（ASGI 包装，仅测试装置）。

    粗框保存/预览在真实 Fleet 进程内延迟响应——页面收到的成功响应
    真实迟到（浏览器原始头/Cookie/CSRF 全程不被替换）。
    """

    def __init__(self, app):
        self.app = app
        self.save_ms = 0
        self.preview_ms = 0

    async def __call__(self, scope, receive, send):
        delay = 0.0
        if scope.get("type") == "http":
            path, method = scope.get("path", ""), scope.get("method", "")
            if method == "PUT" and path.endswith("/phone-search-region"):
                delay = self.save_ms
            elif method == "POST" and path.endswith("/phone-search-region/preview"):
                delay = self.preview_ms
        if delay:
            import asyncio
            await asyncio.sleep(delay / 1000.0)
        await self.app(scope, receive, send)


class CloseAStack:
    """一次浏览器链测试的完整隔离栈（fixture 语义：close() 全清）。"""

    def __init__(self, tmp_path: Path, *, devices: tuple = ("arm-01",)):
        self.tmp_path = tmp_path
        self.device_ids = list(devices)
        # —— 假相机/假 daemon ——
        self.camera = FakeCamera().start()
        self.daemons: dict[str, FakeDaemon] = {}
        for index, device in enumerate(self.device_ids, start=1):
            self.daemons[device] = FakeDaemon(device).start()
            self.camera.register(f"cam-{index:02d}", device,
                                 unique_id=f"uvc-{index:02d}", revision=7)
        # —— Fleet（真实 app + 临时根） ——
        from tests.layout_d2_helpers import AppEnv
        import asyncio
        self.fleet_port = free_port()
        # AppEnv 固定给全部设备同一个 daemon_addr（会触发绑定冲突），
        # 因此先建单设备实例，再把其余设备逐台注册（每台独立假 daemon）。
        self.app_env = AppEnv(tmp_path, token=True, devices=1)
        from tests.helpers import make_device_configs
        if len(self.device_ids) > 1:
            extra = make_device_configs(
                len(self.device_ids), camera_device_unique_id="uvc-01",
                binding_revision=7, expected_frame_width=640,
                expected_frame_height=480)
            for cfg, device in zip(extra[1:], self.device_ids[1:]):
                index = self.device_ids.index(device) + 1
                cfg = dict(cfg, daemon_addr=f"127.0.0.1:{self.daemons[device].port}",
                           camera_device_unique_id=f"uvc-{index:02d}")
                asyncio.run(self.app_env.runtime.devices.register_and_selfcheck(cfg))
        self.bridges: dict[str, FrameBridgeFake] = {}
        for index, device in enumerate(self.device_ids, start=1):
            bridge = FrameBridgeFake(
                self.app_env.runtime, device=device,
                binding=dict(BINDING, camera_id=f"cam-{index:02d}",
                             camera_device_unique_id=f"uvc-{index:02d}"))
            self.bridges[device] = bridge
        self.app_env.bridge = self.bridges[self.device_ids[0]]
        self.pushers = {device: FramePusher(bridge).start()
                        for device, bridge in self.bridges.items()}
        self._rewrite_device_records()
        self._start_fleet()
        # —— 调试台子进程 ——
        self.console = ConsoleProcess(
            fleet_base=f"http://127.0.0.1:{self.fleet_port}",
            db_path=tmp_path / "fleet.db", data_dir=tmp_path / "console-data",
            token_file=self.app_env.token_file)

    def _register_extra_devices(self):
        """第 2+ 台设备逐台注册（独立假 daemon/相机唯一标识，避免绑定冲突）。"""
        import asyncio
        from tests.helpers import make_device_configs
        if len(self.device_ids) <= 1:
            return
        extra = make_device_configs(
            len(self.device_ids), camera_device_unique_id="uvc-01",
            binding_revision=7, expected_frame_width=640,
            expected_frame_height=480)
        for cfg, device in zip(extra[1:], self.device_ids[1:]):
            index = self.device_ids.index(device) + 1
            cfg = dict(cfg, daemon_addr=f"127.0.0.1:{self.daemons[device].port}",
                       camera_device_unique_id=f"uvc-{index:02d}")
            asyncio.run(self.app_env.runtime.devices.register_and_selfcheck(cfg))

    # ---- fleet.db 记录重写：全部指向假 daemon/假相机（不触任何真实端口） ----
    def _rewrite_device_records(self):
        conn = sqlite3.connect(str(self.tmp_path / "fleet.db"))
        try:
            for index, device in enumerate(self.device_ids, start=1):
                row = conn.execute(
                    "SELECT json FROM devices WHERE device_id=?", (device,)).fetchone()
                if not row:
                    continue
                record = json.loads(row[0])
                daemon = self.daemons[device]
                record["daemon_addr"] = f"127.0.0.1:{daemon.port}"
                record["camera_id"] = f"cam-{index:02d}"
                record["camera_frame_url"] = (
                    f"http://127.0.0.1:{self.camera.port}"
                    f"/cameras/cam-{index:02d}/latest.jpg")
                record["camera_status_url"] = (
                    f"http://127.0.0.1:{self.camera.port}"
                    f"/cameras/cam-{index:02d}/status")
                record["status"] = "online"
                record["presence_state"] = "present"
                record["driver_mode"] = "daemon"
                conn.execute("UPDATE devices SET json=? WHERE device_id=?",
                             (json.dumps(record, ensure_ascii=False), device))
            conn.commit()
        finally:
            conn.close()

    def _start_fleet(self):
        import uvicorn
        self.fleet_delay = DelayASGI(self.app_env.app)
        self._uvicorn = uvicorn.Server(uvicorn.Config(
            self.fleet_delay, host="127.0.0.1", port=self.fleet_port,
            log_level="error", lifespan="off"))
        self._fleet_thread = threading.Thread(target=self._uvicorn.run,
                                              daemon=True)
        self._fleet_thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._uvicorn.started:
                return
            time.sleep(0.05)
        raise RuntimeError("Fleet uvicorn 未启动")

    # ---- 场景动作（mjs 经控制服务调用 / pytest 直接调用） ----
    def switch_camera_binding(self, device: str = "arm-01"):
        """换绑相机：证据帧换新安装身份 + 假相机状态换新 unique_id/revision。"""
        index = self.device_ids.index(device) + 1
        bridge = self.bridges[device]
        bridge.binding = dict(
            BINDING, camera_id=f"cam-{index:02d}",
            camera_device_unique_id=f"uvc-{index:02d}-new",
            camera_binding_id=str(uuid.uuid4()))
        bridge.geometry = "c" * 64
        bridge.generation = str(uuid.uuid4())
        self.camera.switch_identity(
            f"cam-{index:02d}", unique_id=f"uvc-{index:02d}-new", revision=8)
        bridge.push(phone_scene(phone=(200, 120, 240, 240)))

    def set_push_mode(self, mode: str):
        for pusher in self.pushers.values():
            pusher.mode = mode

    # ---- 事实读取（正式端点） ----
    def region_get(self, device: str = "arm-01") -> dict:
        headers = {**self.app_env.auth(),
                   "X-Correlation-Id": str(uuid.uuid4()),
                   "X-Layout-Contract-Version": "phone-search-region-read.v1"}
        response = self.app_env.client.get(
            f"/v1/devices/{device}/phone-search-region", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def region_version(self, device: str, version: int) -> dict:
        headers = {**self.app_env.auth(),
                   "X-Correlation-Id": str(uuid.uuid4()),
                   "X-Layout-Contract-Version": "phone-search-region-read.v1"}
        response = self.app_env.client.get(
            f"/v1/devices/{device}/phone-search-region/versions/{version}",
            headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["record"]

    def console_get(self, path: str) -> dict:
        request = urllib.request.Request(self.console.base + path)
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")

    def daemon_motion_total(self) -> int:
        return sum(daemon.motion_count for daemon in self.daemons.values())

    def daemon_command_lines(self) -> list[str]:
        return [line for _, line in
                (cmd for daemon in self.daemons.values()
                 for cmd in daemon.commands)]

    def restart_console(self):
        """同数据根重启调试台进程（重建后 base 变化，页面需重载）。"""
        old_port = self.console.port
        self.console.stop()
        self.console = ConsoleProcess(
            fleet_base=f"http://127.0.0.1:{self.fleet_port}",
            db_path=self.tmp_path / "fleet.db",
            data_dir=self.tmp_path / "console-data",
            token_file=self.app_env.token_file)
        # 数据根复用（目录已存在）；端口必然不同（动态分配）
        assert self.console.port != old_port
        self.console.start()

    def restart_console_and_fleet(self):
        """同数据根重启调试台进程 + Fleet 进程（端口全换；绑定状态保持）。

        R4-02-C 重建语义：两个服务进程都替换，正式记录只依赖临时数据根
        （fleet.db / layout.db / evidence / console-data）。
        """
        self.console.stop()
        self._uvicorn.should_exit = True
        self._fleet_thread.join(timeout=5)
        self.app_env.close()
        # 保留每设备当前相机绑定（换相机场景重启后不得回退成旧绑定）
        kept = {device: (dict(bridge.binding), bridge.geometry, bridge.generation)
                for device, bridge in self.bridges.items()}
        for pusher in self.pushers.values():
            pusher.stop()
        from tests.layout_d2_helpers import AppEnv
        self.fleet_port = free_port()
        self.app_env = AppEnv(self.tmp_path, token=True, devices=1)
        self._register_extra_devices()
        self.bridges = {}
        for index, device in enumerate(self.device_ids, start=1):
            bridge = FrameBridgeFake(
                self.app_env.runtime, device=device,
                binding=dict(BINDING, camera_id=f"cam-{index:02d}",
                             camera_device_unique_id=f"uvc-{index:02d}"))
            binding, geometry, generation = kept.get(
                device, (dict(BINDING), None, None))
            bridge.binding = dict(binding)
            if geometry:
                bridge.geometry = geometry
            if generation:
                bridge.generation = generation
            self.bridges[device] = bridge
        self.app_env.bridge = self.bridges[self.device_ids[0]]
        self.pushers = {device: FramePusher(bridge).start()
                        for device, bridge in self.bridges.items()}
        self._rewrite_device_records()
        self._start_fleet()
        self.restart_console()

    def close(self):
        try:
            self.console.stop()
        except Exception:
            pass
        try:
            self._uvicorn.should_exit = True
            self._fleet_thread.join(timeout=5)
        except Exception:
            pass
        for pusher in self.pushers.values():
            pusher.stop()
        for daemon in self.daemons.values():
            daemon.stop()
        try:
            self.camera.stop()
        except Exception:
            pass
        try:
            self.app_env.close()
        except Exception:
            pass


class ControlServer:
    """浏览器脚本 ↔ pytest 编排的控制面（测试装置，非产品链路）。"""

    def __init__(self, stack: CloseAStack):
        self.stack = stack
        self.events: list[dict] = []
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True)

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def _reply(self, payload: dict, status: int = 200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._reply({"error": "bad json"}, 400)
                    return
                outer.events.append(data)
                action = data.get("action")
                stack = outer.stack
                try:
                    if action == "status":
                        self._reply({
                            "motion_total": stack.daemon_motion_total(),
                            "commands": stack.daemon_command_lines(),
                            "region": stack.region_get(
                                data.get("device") or "arm-01"),
                        })
                    elif action == "push_mode":
                        stack.set_push_mode(data.get("mode") or "phone")
                        self._reply({"ok": True})
                    elif action == "wizard_state":
                        # 正式调试台端点（需页面级上下文：与浏览器 api() 同参形状）
                        import urllib.request as _rq
                        device = data.get("device") or "arm-01"
                        session = _rq.Request(
                            stack.console.base + "/api/debug-session")
                        with _rq.urlopen(session, timeout=5) as resp:
                            csrf = json.loads(resp.read() or b"{}").get("csrf_token")
                            cookie = (resp.headers.get("Set-Cookie") or "").split(";")[0]
                        body = json.dumps({"deviceId": device}).encode()
                        req = _rq.Request(
                            stack.console.base + "/api/device-context", data=body,
                            method="POST", headers={
                                "Content-Type": "application/json", "Origin": stack.console.base,
                                "Sec-Fetch-Site": "same-origin",
                                "X-Debug-CSRF": csrf, "Cookie": cookie})
                        with _rq.urlopen(req, timeout=10) as resp:
                            context_id = json.loads(resp.read() or b"{}")["contextId"]
                        req = _rq.Request(
                            stack.console.base + f"/api/wizard/state?device={device}"
                            f"&contextId={context_id}")
                        with _rq.urlopen(req, timeout=10) as resp:
                            view = json.loads(resp.read() or b"{}")
                        self._reply({"auto": view.get("auto") or {}})
                    elif action == "fleet_delay":
                        stack.fleet_delay.save_ms = int(data.get("save_ms") or 0)
                        stack.fleet_delay.preview_ms = int(data.get("preview_ms") or 0)
                        self._reply({"ok": True})
                    elif action == "switch_camera":
                        stack.switch_camera_binding(
                            data.get("device") or "arm-01")
                        self._reply({"ok": True})
                    else:
                        self._reply({"error": f"unknown action {action}"}, 400)
                except Exception as exc:
                    self._reply({"error": str(exc)}, 500)

            def do_GET(self):  # noqa: N802
                self._reply({"ok": True})

            def log_message(self, *args):
                pass

        return H

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
