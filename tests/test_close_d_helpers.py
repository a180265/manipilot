"""close-D 域（项目输入法服务端链 G1/G7）共享测试基建。

与既有逐段测试的差异：本文件提供**正式入口自动推进**的整链环境——
- 真实 create_app + 仿真 Runtime，经本机回环 uvicorn（临时端口）提供
  正式 HTTP 入口（平台 /v1/* 与手机 /api/v1/* 同一进程装配）；
- 调度由真实 _dispatch_loop 自动推进（禁止手调 executor/手填终态）；
- 手机侧 = 真实 HTTP 协议客户端（PhoneActor）：配对/心跳/几何握手与
  nonce 应答/任务认领与状态机/取消命令确认，全部走正式路由；
- 假物理 IO：GateFakeArm 复刻 daemon_arm 的传输边界语义（走位后、接触前
  执行 R3-02 最终几何门 + 最后认领）；合成几何快照由 PhoneActor 产生。

不替代的生产边界：真机 daemon 协议/串口、真实 Android 输入法进程、
真实相机帧、真实 TLS（回环为 HTTP——LAN TLS 属部署层，由 B 域客户端
对真实 TLS 桩验证）。
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

import httpx

from drivers.fake_arm import FakeArmDriver

TRUST = {"https_origin": "https://fleet-lan.test", "ca_cert_b64": "QUJD",
         "spki_sha256": "d" * 64}
GEOM = "ime-geometry.v1"
PAIR = "ime-pairing.v1"

CANDIDATE_BOUNDS = (54, 2160, 1026, 2280)
DELETE_BOUNDS = (960, 1960, 1050, 2050)
DISPLAY = {"display_id": 0, "width_px": 1080, "height_px": 2400,
           "rotation": 0, "density": 2.75}


class GateFakeArm(FakeArmDriver):
    """仿真机械臂（假物理 IO），传输边界与 drivers/daemon_arm.py 同序：

    走位（sleep）完成 → R3-02 最终几何门（geometry_gate）→ §4.8.4 最后
    认领回调 → 接触（action_log 记录）。门/认领失败 = 零接触，走位事实
    保留。默认 FakeArmDriver 不执行 geometry_gate，仿真链路会缺 R3-02
    最终门；本替身补齐该边界语义（接口变更请求已登记：FakeArmDriver
    应内建同序门）。
    """

    def __init__(self, device_id: str, move_seconds: float = 0.02):
        super().__init__(device_id, move_seconds=move_seconds)
        self.motions: list[tuple[str, str]] = []  # (action, target) 走位记录
        self.gate_calls = 0

    def has_motion(self, action: str, target: str) -> bool:
        return any(a == action and t == target for a, t in self.motions)

    async def _move(self, action: str, target, *, input_claim: dict | None = None):
        if self._fault(f"fail_{action}"):
            from drivers.arm import DriverError
            raise DriverError("driver_motion_failed", f"模拟{action}运动失败")
        self._check_connection()
        self.motions.append((action, str(target)))  # 走位开始（注入观察窗）
        await asyncio.sleep(self._move_seconds)
        if input_claim:
            gate = input_claim.get("geometry_gate")
            if callable(gate):
                self.gate_calls += 1
                gate()  # R3-02：先几何后认领（daemon_arm 同序）
            callback = input_claim.get("callback")
            if callable(callback):
                callback()
        self._position = target
        self.action_log.append({"action": action, "target": target})
        if self._fault("disconnect_after"):
            self._connected = False
        return {"ok": True, "position": self._position}


class LoopbackFleet:
    """真实 app + 仿真 Runtime + 回环 uvicorn（临时端口）。

    用法：
        fleet = LoopbackFleet(tmp_path, plugin=MyPlugin())
        with fleet.serving():
            ... httpx 正式入口请求 / fleet.runtime 直读断言 ...
    """

    def __init__(self, tmp_path: Path, *, plugin=None, manifest=None,
                 n_devices: int = 1, host: str = "127.0.0.1"):
        from api.app import create_app
        from runtime.config import PlatformConfig
        from runtime.ime_pairing import ImePairingService
        from runtime.runtime import ArmFleetRuntime
        from tests.helpers import PROBE_MANIFEST, make_device_configs

        tmp_path = Path(tmp_path)
        manifest = deepcopy(manifest or getattr(plugin, "manifest", None)
                            or PROBE_MANIFEST)
        config = PlatformConfig({
            "db_path": str(tmp_path / "fleet.db"),
            "evidence_dir": str(tmp_path / "evidence"),
            "plugin_dir": str(tmp_path / "plugins"),
            "hardware_mode": "simulation",
            "daemon_supervisor_enabled": False,
            "hotplug_discovery_enabled": False,
            "screen_ime_enabled": True,
        }, root=tmp_path)
        self.runtime = ArmFleetRuntime(config)
        if plugin is not None:
            self.runtime.plugins.register_instance_for_tests(plugin, manifest)
        # R3-17 配对身份库（真实服务 + 临时库）与信任材料（部署输入）
        self.pairing = ImePairingService(tmp_path / "ime_pairing.db")
        self.pairing.init_server_id()
        self.runtime.ime_pairing = self.pairing
        self.runtime.ime_pairing_trust = dict(TRUST)
        self.app = create_app(self.runtime)
        self.app.state.runtime = self.runtime
        self.app.state.device_configs = []
        self.configs = make_device_configs(n_devices)
        self.tmp_path = tmp_path
        self.host = host
        self._server = None
        self._thread: threading.Thread | None = None
        self.base: str | None = None

    # ---- 生命周期 ----
    def serving(self):
        return _Serving(self)

    def _start(self) -> None:
        import uvicorn

        asyncio.run(self.runtime.devices.register_all(self.configs))
        config = uvicorn.Config(self.app, host=self.host, port=0,
                                log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run,
                                        name="close-d-loopback", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            sockets = getattr(self._server, "servers", None) or []
            if sockets:
                port = sockets[0].sockets[0].getsockname()[1]
                self.base = f"http://{self.host}:{port}"
                break
            if not self._thread.is_alive():
                raise RuntimeError("回环服务启动失败")
            time.sleep(0.02)
        else:
            raise RuntimeError("回环服务启动超时")
        # 仿真设备镜像：可执行 + 校准通过（正式绑定源=设备镜像；测试布线，
        # 与既有 G7/R3-17 测试同一方式）
        for index in range(len(self.configs)):
            device_id = self.configs[index]["device_id"]
            self.runtime.devices._devices[device_id].update({
                "status": "ready", "calibration_status": "validated",
                "daemon_session": f"sess-close-d-{index + 1}"})
            # 假物理 IO：换入传输边界语义完整的仿真机械臂（连接语义随自检）
            arm = GateFakeArm(device_id)
            asyncio.run(arm.connect())
            self.runtime.devices._arms[device_id] = arm

    def _stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)
        try:
            self.pairing.close()
        except Exception:
            pass

    # ---- 便捷访问 ----
    @property
    def screen_ime(self):
        return self.app.state.screen_ime

    def arm(self, device_id: str = "arm-01") -> GateFakeArm:
        return self.runtime.devices._arms[device_id]

    def client(self, **kwargs) -> httpx.Client:
        assert self.base, "回环服务未启动"
        return httpx.Client(base_url=self.base, timeout=30.0, **kwargs)


class _Serving:
    def __init__(self, fleet: LoopbackFleet):
        self.fleet = fleet

    def __enter__(self):
        self.fleet._start()
        return self.fleet

    def __exit__(self, *exc):
        self.fleet._stop()
        return False


# ============================================================ 手机协议客户端

class PhoneActor:
    """真实 HTTP 手机客户端（K40 输入法协议语义）。

    线程循环全部走正式路由：
    - heartbeat：完整 v1 心跳（inputTaskControlVersion=1，keyboardActive）
    - geometry：握手 + 长轮询领取 nonce + 合成几何快照应答
    - tasks：长轮询领取任务 → displayed →（可选停靠点）typing → inserted
    - control：长轮询取消命令 → 持久化（本地账本替身）→ 恰一次 ack（同体重放）

    故障注入（默认全关）：
    - stop_after："displayed"|"typing" —— 停在该状态不入终态（制造在途窗口）
    - drop_ack_responses: int —— ack 请求发出但丢弃响应（模拟响应掉包），
      之后同体重发（客户端持久账本重试语义）
    """

    def __init__(self, base_url: str, ime_device_id: str, token: str,
                 *, label: str = "close-D 手机"):
        self.base = base_url.rstrip("/")
        self.ime = ime_device_id
        self.token = token
        self.label = label
        self._http = httpx.Client(base_url=self.base, timeout=30.0,
                                  headers={"x-ime-phone-token": token})
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # 几何状态
        self.session_id: str | None = None
        self._view = str(uuid.uuid4())
        self._seq = 0
        self._seq_lock = threading.Lock()
        # 任务状态（本地持久账本替身：只记已提交事实）
        self.claimed: dict[str, dict] = {}
        self._claimed_lock = threading.Lock()
        self.reported_status: dict[str, list[str]] = {}
        self.typed_counts: dict[str, int] = {}
        self.ack_bodies: dict[str, list[dict]] = {}
        self.stop_after: str | None = None
        self.typing_stop_count: int | None = None  # typing 半途停靠字数
        self.drop_ack_responses = 0
        self.acks_sent: list[tuple[str, dict]] = []
        self.errors: list[str] = []
        self.current_task_id: str | None = None  # 候选栏绑定的 IME 任务

    # ---- 控制 ----
    def start(self, *, heartbeat: bool = True, geometry: bool = True,
              tasks: bool = True, controls: bool = True) -> None:
        loops = []
        if heartbeat:
            loops.append(self._heartbeat_loop)
        if geometry:
            loops.append(self._geometry_loop)
        if tasks:
            loops.append(self._task_loop)
        if controls:
            loops.append(self._control_loop)
        for fn in loops:
            t = threading.Thread(target=fn, daemon=True,
                                 name=f"phone-{fn.__name__}")
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._http.close()

    # ---- 心跳 ----
    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = self._http.post(f"/api/v1/devices/{self.ime}/heartbeat", json={
                    "appVersion": "close-d-sim", "keyboardActive": True,
                    "inputTaskControlVersion": 1})
                if r.status_code != 200:
                    self.errors.append(f"heartbeat {r.status_code}")
            except Exception as exc:
                self.errors.append(f"heartbeat {exc}")
            self._stop.wait(3.0)

    # ---- 几何 ----
    def handshake(self, client: httpx.Client | None = None) -> dict:
        http = client or self._http
        r = http.post(f"/api/v1/devices/{self.ime}/geometry/sessions", json={
            "contract_version": GEOM, "imeGeometryVersion": 1,
            "client_instance_id": str(uuid.uuid4())})
        r.raise_for_status()
        self.session_id = r.json()["runtime_session_id"]
        return r.json()

    def snapshot_body(self, nonce: str | None, *, task_id: str | None = None,
                      candidate_visible: bool = True,
                      geometry_valid: bool = True,
                      reason: str | None = None,
                      current_seq: int | None = None) -> dict:
        with self._seq_lock:
            if current_seq is None:
                self._seq += 1
                seq = self._seq
            else:
                seq = current_seq
        body = {
            "contract_version": GEOM, "runtime_session_id": self.session_id,
            "view_instance_id": self._view, "seq": seq,
            "captured_uptime_ms": int(time.monotonic() * 1000) % 10_000_000,
            "display": dict(DISPLAY), "layout_version": "close-d-kb-1",
            "keyboard_active": geometry_valid,
            "geometry_valid": geometry_valid,
            "keyboard_bounds": {"left": 0, "top": 1560, "right": 1080,
                                "bottom": 2400} if geometry_valid else None,
            "current_task_id": task_id if task_id is not None else self.current_task_id,
            "reason": reason or ("snapshot" if nonce else "layout_ready"),
            "request_nonce": nonce,
            "targets": [
                {"id": "ime_candidate_bar", "visible": candidate_visible,
                 "enabled": candidate_visible, "clickable": candidate_visible,
                 "bounds": ({"left": CANDIDATE_BOUNDS[0], "top": CANDIDATE_BOUNDS[1],
                             "right": CANDIDATE_BOUNDS[2], "bottom": CANDIDATE_BOUNDS[3]}
                            if candidate_visible else None)},
                {"id": "ime_delete_key", "visible": candidate_visible,
                 "enabled": candidate_visible, "clickable": candidate_visible,
                 "bounds": ({"left": DELETE_BOUNDS[0], "top": DELETE_BOUNDS[1],
                             "right": DELETE_BOUNDS[2], "bottom": DELETE_BOUNDS[3]}
                            if candidate_visible else None)},
            ],
        }
        if not geometry_valid:
            for t in body["targets"]:
                t["visible"] = t["enabled"] = t["clickable"] = False
                t["bounds"] = None
        return body

    def report(self, body: dict):
        return self._http.post(f"/api/v1/devices/{self.ime}/geometry",
                               json=body)

    def _geometry_loop(self) -> None:
        # 握手重试（服务就绪/重启后重握手）
        while not self._stop.is_set() and self.session_id is None:
            try:
                self.handshake()
            except Exception:
                self._stop.wait(0.2)
        while not self._stop.is_set():
            try:
                r = self._http.get(
                    f"/api/v1/devices/{self.ime}/geometry/requests/next"
                    f"?waitSeconds=1&runtime_session_id={self.session_id}")
                if r.status_code == 204:
                    continue
                if r.status_code != 200:
                    # 会话失效（如服务重启/新实例）→ 重握手
                    self.session_id = None
                    try:
                        self.handshake()
                    except Exception:
                        pass
                    continue
                nonce = r.json()["request_nonce"]
                body = self.snapshot_body(nonce)
                resp = self.report(body)
                if resp.status_code != 200:
                    self.errors.append(f"geometry report {resp.status_code}")
            except Exception as exc:
                if not self._stop.is_set():
                    self.errors.append(f"geometry {exc}")
                    self._stop.wait(0.1)

    # ---- 任务 ----
    def _task_loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = self._http.get(
                    f"/api/v1/devices/{self.ime}/tasks/next?waitSeconds=1")
                if r.status_code == 204:
                    continue
                if r.status_code != 200:
                    self.errors.append(f"tasks/next {r.status_code}")
                    self._stop.wait(0.2)
                    continue
                task = r.json()
                self._drive_task(task)
            except Exception as exc:
                if not self._stop.is_set():
                    self.errors.append(f"tasks {exc}")
                    self._stop.wait(0.1)

    def _post_status(self, task_id: str, status: str, typed: int):
        r = self._http.post(f"/api/v1/tasks/{task_id}/status", json={
            "deviceId": self.ime, "status": status,
            "typedCharacterCount": typed})
        if r.status_code == 200:
            self.reported_status.setdefault(task_id, []).append(status)
            if status == "typing":
                # 持久账本（替身）：只记已提交的最大计数（单调）
                self.typed_counts[task_id] = max(
                    typed, self.typed_counts.get(task_id, 0))
        return r

    def _drive_task(self, task: dict) -> None:
        task_id = task["taskId"]
        with self._claimed_lock:
            if task_id in self.claimed:
                return  # 正文只认领一次（同 taskId 二次到达不再驱动）
            self.claimed[task_id] = dict(task)
            self.claimed[task_id]["_unacked"] = True
        self.current_task_id = task_id  # 候选栏绑定本任务（R3-02）
        content = task.get("content") or ""
        self._post_status(task_id, "displayed", 0)
        if self.stop_after == "displayed":
            return
        # 走位/TAP 由机械臂完成；手机侧开始输入并上报。typing 是单次开始
        # 许可（服务端重复 typing 一律 409 already_claimed）：只发一次，
        # 携带此刻已提交计数（持久账本单调）。
        target_count = len(content) if self.typing_stop_count is None \
            else min(self.typing_stop_count, len(content))
        self._post_status(task_id, "typing", target_count)
        if self.stop_after == "typing":
            return
        self._post_status(task_id, "inserted", len(content))

    # ---- 取消命令 ----
    def _control_loop(self) -> None:
        while not self._stop.is_set():
            with self._claimed_lock:
                pending = [tid for tid, task in self.claimed.items()
                           if task.get("_unacked")]
            if not pending:
                self._stop.wait(0.05)
                continue
            for task_id in pending:
                try:
                    r = self._http.get(
                        f"/api/v1/tasks/{task_id}/control/next"
                        f"?deviceId={self.ime}&waitSeconds=1")
                    if r.status_code == 204:
                        continue
                    if r.status_code != 200:
                        self.errors.append(f"control/next {r.status_code}")
                        continue
                    command = r.json()
                    if command.get("command") != "cancel":
                        self.errors.append(f"unknown command {command}")
                        continue
                    self._ack_cancel(task_id, command)
                except Exception as exc:
                    if not self._stop.is_set():
                        self.errors.append(f"control {exc}")
            self._stop.wait(0.05)

    def resume_cancel(self, task_id: str, typed_count: int):
        """新实例恢复：按持久账本（替身）恢复取消确认——轮询命令→恰一次 ack。"""
        r = self._http.get(
            f"/api/v1/tasks/{task_id}/control/next"
            f"?deviceId={self.ime}&waitSeconds=2")
        if r.status_code != 200:
            return r
        command = r.json()
        with self._claimed_lock:
            self.claimed.setdefault(task_id, {"taskId": task_id})
            self.claimed[task_id]["_unacked"] = False
        body = {"deviceId": self.ime, "commandId": command["commandId"],
                "typedCharacterCount": typed_count}
        self.ack_bodies.setdefault(task_id, []).append(body)
        ra = self._http.post(f"/api/v1/tasks/{task_id}/control/ack", json=body)
        if ra.status_code == 200:
            self.acks_sent.append((task_id, body))
        return ra

    def _ack_cancel(self, task_id: str, command: dict) -> None:
        """持久账本（替身）提交后才发 ack；响应掉包→同体重放（不新造命令）。"""
        with self._claimed_lock:
            task = self.claimed.get(task_id) or {}
            task["_unacked"] = False
            task["_cancel_persisted"] = True
            task["_cancel_command_id"] = command["commandId"]
        body = {"deviceId": self.ime, "commandId": command["commandId"],
                "typedCharacterCount": self.typed_counts.get(task_id, 0)}
        self.ack_bodies.setdefault(task_id, []).append(body)
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            try:
                r = self._http.post(f"/api/v1/tasks/{task_id}/control/ack",
                                    json=body)
            except Exception as exc:
                self.errors.append(f"ack {exc}")
                self._stop.wait(0.2)
                continue
            if attempt <= self.drop_ack_responses:
                continue  # 响应丢弃（客户端不知结果）→ 持久事实同体重放
            if r.status_code == 200:
                self.acks_sent.append((task_id, body))
                return
            self.errors.append(f"ack {r.status_code} {r.text[:120]}")
            self._stop.wait(0.2)


def pair_phone(client: httpx.Client, *, instance: str,
               request_prefix: str = "close-d") -> tuple[dict, str]:
    """调试台发起邀请 → 手机 confirm → activate（全部真实 HTTP）。

    返回 (activate 响应, credential_token)。
    """
    from runtime.ime_pairing import new_credential_token, new_invite_secret

    secret = new_invite_secret()
    r = client.post("/v1/ime-pairing/invitations", json={
        "contract_version": PAIR, "request_id": f"{request_prefix}-inv-{instance}",
        "invite_secret": secret, "arm_id": "arm-01"})
    r.raise_for_status()
    invitation = r.json()
    token = new_credential_token()
    rc = client.post("/api/v1/pairings/confirm", json={
        "contract_version": PAIR, "request_id": f"{request_prefix}-c-{instance}",
        "invitation_id": invitation["invitation_id"], "invite_secret": secret,
        "phone_instance_id": instance, "phone_label": "close-D 手机",
        "credential_id": str(uuid.uuid4()), "credential_token": token})
    rc.raise_for_status()
    pairing = rc.json()
    ra = client.post(f"/api/v1/pairings/{pairing['pairing_id']}/activate", json={
        "contract_version": PAIR, "request_id": f"{request_prefix}-a-{instance}",
        "expected_revision": pairing["revision"]},
        headers={"x-ime-phone-token": token})
    ra.raise_for_status()
    return ra.json(), token


def enable_input(client: httpx.Client, ime_device_id: str, *,
                 request_id: str, expected_revision: int = 0) -> dict:
    r = client.put("/v1/devices/arm-01/input-settings", json={
        "contract_version": "device-input-settings.v1",
        "operator_id": "操作员甲", "role": "operator", "reason": "close-D 链路",
        "correlation_id": "close-d", "client_request_id": request_id,
        "enabled": True, "expected_revision": expected_revision,
        "ime_device_id": ime_device_id})
    r.raise_for_status()
    return r.json()


def await_status(predicate, timeout: float = 20.0, interval: float = 0.05):
    """轮询断言辅助：predicate() 返回真或超时。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def fleet_task_payload(task_id: str, *, text: str = "整链正文closeD",
                       task_type: str = "declared_input",
                       device_id: str = "arm-01") -> dict:
    payload = {"steps": [{"is_action": True}]}
    if task_type == "declared_input":
        payload["text"] = text
    return {
        "task_type": task_type, "device_id": device_id, "task_id": task_id,
        "idempotency_key": task_id, "correlation_id": "close-d",
        "expires_at": None, "contract_version": "v1",
        "payload": payload,
    }
