"""daemon 驱动桥单元测试：内嵌假 daemon（v1.2 协议）+ 设备级档案夹具。

覆盖设计文档验收标准第 4 条：会话/定位拒绝、目标解析、分段/悬停、运动租约、
STOP/ESTOP 优先级、进给范围拦截、unknown 语义、动态目标与裸位移拒绝。
不依赖真机与真实 arm_daemon3 进程。
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")


from drivers.arm import DriverError
from drivers.daemon_arm import DaemonArmDriver, DaemonTCPTransport
from drivers.daemon_protocol import (
    PATH1_FEATURE,
    reject_json_constant,
    strict_object_pairs,
    validate_path1_payload,
)

CALIB_SESSION = "sess-fixed-0001"


# ---------------------------------------------------------------- 假 daemon

class FakeDaemon:
    """arm_daemon3 v1.2 协议的进程内仿真：租约、事务、急停锁、安全限值。"""

    def __init__(self, *, serial_connected: bool = True, mpos=(2.0, 2.0, 5.0),
                 device_id: str = "arm-01"):
        self.lock = threading.Lock()
        self.device_id = device_id
        self.mpos = list(mpos)
        self.serial_connected = serial_connected
        self.session = CALIB_SESSION if serial_connected else None
        self.next_session = CALIB_SESSION
        self.estop_locked = False
        self.motion_features = ["tap2.v1", "swipe.v1", PATH1_FEATURE]
        self.lease: dict | None = None
        self.transactions: dict[int, str] = {}
        self.commands: list[str] = []       # 已受理的普通运动指令
        self.requests: list[tuple[int, str]] = []
        self.response_hook = None
        self.welcome = b"arm-daemon3 ready\n"
        self.estop_writes = 0
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        self._conn_seq = iter(range(1, 10 ** 6))
        self._listen_thread = threading.Thread(target=self._serve, daemon=True)
        self._stopped = threading.Event()
        self._clients: dict[int, socket.socket] = {}
        self._threads: list[threading.Thread] = []

    def start(self) -> "FakeDaemon":
        self._srv.listen(8)
        self._srv.settimeout(0.05)
        self._listen_thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        with self.lock:
            clients = list(self._clients.values())
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self._srv.close()
        except OSError:
            pass
        self._listen_thread.join(timeout=1)
        for thread in self._threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
        assert not self._listen_thread.is_alive()

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn_id = next(self._conn_seq)
            with self.lock:
                self._clients[conn_id] = conn
            thread = threading.Thread(target=self._client,
                                      args=(conn, conn_id), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _client(self, conn: socket.socket, conn_id: int) -> None:
        f = None
        try:
            conn.sendall(self.welcome)
            f = conn.makefile("rwb")
            while True:
                line = f.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if not text:
                    continue
                if text.upper() == "BYE":
                    break
                self.requests.append((conn_id, text))
                try:
                    resp = self._handle(text, conn_id)
                except ValueError as exc:
                    resp = f"ERROR: {exc}"
                if self.response_hook is not None:
                    resp = self.response_hook(text, resp, conn_id)
                if resp is None:
                    break
                conn.sendall((resp + "\n").encode())
        except OSError:
            pass
        finally:
            with self.lock:
                if self.lease and self.lease["conn_id"] == conn_id:
                    self.lease = None
                self.transactions.pop(conn_id, None)
                self._clients.pop(conn_id, None)
            if f is not None:
                f.close()
            try:
                conn.close()
            except OSError:
                pass

    def _require_motion(self, conn_id: int) -> None:
        if self.estop_locked:
            raise ValueError("急停锁定中（daemon 级）：人工 RESET_ESTOP 复位前拒绝一切普通运动")
        if self.lease is None:
            raise ValueError("普通运动需要运动租约：先 CLAIM")
        if self.lease["conn_id"] != conn_id:
            raise ValueError("运动租约由其他连接持有")
        self.lease["expires_at"] = time.time() + 90

    def _feed(self, parts: list[str], default: float, start: int, axis: str) -> float:
        feed = default
        for extra in parts[start:]:
            if extra.upper().startswith("F"):
                feed = float(extra[1:])
        if axis == "Z":
            maximum = (3000.0 if parts[0].upper() == "TAP2"
                       and "tap2.feed-3000.v1" in self.motion_features else 2000.0)
            if not 30.0 <= feed <= maximum:
                raise ValueError(f"Z 进给 F{feed:g} 超出安全范围 30-{maximum:g}")
        else:
            if not 100.0 <= feed <= 7000.0:
                raise ValueError(f"X 进给 F{feed:g} 超出安全范围 100-7000")
        return feed

    def _handle(self, line: str, conn_id: int) -> str:
        parts = line.split()
        cmd = parts[0].upper()
        with self.lock:
            if cmd == "CONNECT":
                self.serial_connected = True
                self.session = self.next_session
                return json.dumps({"connected": True, "session": self.session})
            if cmd == "DISCONNECT":
                self.serial_connected = False
                self.session = None
                return '{"disconnected": true}'
            if cmd == "STATUS":
                lease = None
                if self.lease:
                    lease = {k: v for k, v in self.lease.items() if k != "conn_id"}
                return json.dumps({"connected": self.serial_connected,
                                   "deviceId": self.device_id,
                                   "devicePresent": True, "session": self.session,
                                   "motionFeatures": self.motion_features,
                                   "estopLocked": self.estop_locked, "lease": lease})
            if cmd == "ESTOP_LOCK":
                self.estop_locked = True
                if self.serial_connected:
                    self.estop_writes += 1
                return '{"estop_locked": true, "stopped": true}'
            if cmd == "RESET_ESTOP":
                self.estop_locked = False
                return json.dumps({"estop_locked": False, "reset_by": " ".join(parts[1:])})
            if cmd == "STOP":
                if self.serial_connected:
                    self.estop_writes += 1
                return '{"stopped": true}'
            if cmd == "CLAIM":
                if self.estop_locked:
                    raise ValueError("急停锁定中（daemon 级）：RESET_ESTOP 人工复位前拒绝申请运动租约")
                if len(parts) < 3:
                    raise ValueError("CLAIM 需要 owner_type 与 owner_id")
                if self.lease is not None:
                    raise ValueError(f"运动租约被占用：{self.lease['owner_type']}/{self.lease['owner_id']}")
                self.lease = {"lease_id": uuid.uuid4().hex, "conn_id": conn_id,
                              "owner_type": parts[1], "owner_id": parts[2],
                              "task_id": " ".join(parts[3:]), "expires_at": time.time() + 90}
                return json.dumps({"claimed": True, "lease_id": self.lease["lease_id"]})
            if cmd == "RENEW":
                self._require_lease(conn_id)
                self.lease["expires_at"] = time.time() + 90
                return '{"renewed": true}'
            if cmd == "RELEASE":
                if self.lease and self.lease["conn_id"] == conn_id:
                    self.lease = None
                    self.transactions.pop(conn_id, None)
                    return '{"released": true}'
                return '{"released": false}'
            if cmd == "BEGIN_MOTION":
                self._require_lease(conn_id)
                tx = uuid.uuid4().hex
                self.transactions[conn_id] = tx
                return json.dumps({"tx": tx})
            if cmd == "END_MOTION":
                self._require_lease(conn_id)
                tx = self.transactions.pop(conn_id, None)
                if tx is None:
                    raise ValueError("本连接没有打开的动作事务")
                return json.dumps({"ended": tx})
            if cmd == "MPOS":
                self._require_serial()
                return json.dumps({"x": self.mpos[0], "y": self.mpos[1], "z": self.mpos[2],
                                   "state": "Idle"})
            if cmd == "Z":
                self._require_motion(conn_id)
                self._require_serial()
                dz = float(parts[1])
                feed = self._feed(parts, 30.0, 2, "Z")
                if abs(dz) > 10.0:
                    raise ValueError(f"Z 单段行程 {abs(dz):g}mm 超过软上限 10mm")
                self.commands.append(line)
                self.mpos[2] += dz
                return self._mpos_json()
            if cmd == "XY":
                self._require_motion(conn_id)
                self._require_serial()
                dx, dy = float(parts[1]), float(parts[2])
                feed = self._feed(parts, 150.0, 3, "X")
                if abs(dx) > 300.0 or abs(dy) > 300.0:
                    raise ValueError("XY 单条直达行程超过软上限 300mm")
                self.commands.append(line)
                self.mpos[0] += dx
                self.mpos[1] += dy
                return self._mpos_json()
            if cmd in {"TAP", "TAP2"}:
                self._require_motion(conn_id)
                self._require_serial()
                depth = float(parts[1])
                feed = self._feed(parts, 1500.0, 2, "Z")
                if abs(depth) > 10.0:
                    raise ValueError("TAP 深度超过 Z 单段上限 10mm")
                self.commands.append(line)  # 下压+抬起，净位移为零
                return self._mpos_json()
            if cmd == "SWIPE":
                self._require_motion(conn_id)
                self._require_serial()
                self.commands.append(line)
                self.mpos[0] += float(parts[1])
                self.mpos[1] += float(parts[2])
                return self._mpos_json()
            if cmd == "PATH1":
                self._require_motion(conn_id)
                self._require_serial()
                payload = json.loads(line[6:], object_pairs_hook=strict_object_pairs,
                                     parse_constant=reject_json_constant)
                path = validate_path1_payload(payload)
                for segment in path["segments"]:
                    if segment["op"] == "move":
                        self.mpos[0] += segment["dx"]
                        self.mpos[1] += segment["dy"]
                self.commands.append(line)  # down+up 净位移为零，回到进入宏前 Z
                return json.dumps({
                    "x": self.mpos[0], "y": self.mpos[1], "z": self.mpos[2],
                    "state": "Idle", "path_completed": True,
                    "segments_completed": len(path["segments"]),
                    "contact_released": True,
                })
            raise ValueError(f"未知指令: {line}")

    def _require_serial(self) -> None:
        if not self.serial_connected:
            raise ValueError("串口未连接：请先在调试台点击「连接设备」")

    def _require_lease(self, conn_id: int) -> None:
        if self.lease is None or self.lease["conn_id"] != conn_id:
            raise ValueError("本连接未持有运动租约")

    def _mpos_json(self) -> str:
        return json.dumps({"x": self.mpos[0], "y": self.mpos[1], "z": self.mpos[2],
                           "state": "Idle"})


# ---------------------------------------------------------------- 夹具

def write_device_files(tmp: Path, *, session: str = CALIB_SESSION,
                       affine_scale: float = 0.1) -> None:
    device = tmp / "devices" / "arm-01"
    device.mkdir(parents=True, exist_ok=True)
    (device / "calibration.json").write_text(json.dumps({
        "session": session,
        "affine": {"a": affine_scale, "b": 0, "c": 0, "d": 0, "e": affine_scale, "f": 0},
        "frameW": 640, "frameH": 480, "residualRmsMm": 0.4, "fittedAt": "t",
    }, ensure_ascii=False), encoding="utf-8")
    (device / "targets.json").write_text(json.dumps({
        "ime_candidate_bar": {"name_display": "输入法候选栏", "targetMode": "tap",
                              "nx": 0.5, "ny": 0.5, "hoverZ": 2.0,
                              "tapDepth": 3.0, "tapFeed": 1500},
        "ime_delete_key": {"name_display": "输入法删除键位置", "targetMode": "hover_only",
                           "nx": 0.2, "ny": 0.6, "hoverZ": 2.0},
        "send_button": {"name_display": "发送按钮", "targetMode": "tap",
                        "nx": 0.5, "ny": 0.9, "hoverZ": 2.0, "tapDepth": 3.0, "tapFeed": 1500},
    }, ensure_ascii=False), encoding="utf-8")
    (device / "arm_profile.json").write_text(json.dumps(
        {"hoverZ": 2.0, "tapDepth": 3.0, "tapFeed": 1500}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _allow_input_switch(monkeypatch, request):
    """A-开关（2026-10-03 规格 §4.6）：本套件聚焦驱动机械行为，默认放行开关；
    标记 real_input_switch 的用例保留真实门禁（开关本体覆盖）。"""
    if request.node.get_closest_marker("real_input_switch"):
        return
    monkeypatch.setattr(DaemonArmDriver, "_input_switch_allowed", lambda self, calib: True)


def make_driver(daemon: FakeDaemon, tmp: Path) -> DaemonArmDriver:
    return DaemonArmDriver("arm-01", daemon_addr=f"127.0.0.1:{daemon.port}", data_root=tmp)


@pytest.fixture()
def daemon_and_driver(tmp_path: Path):
    write_device_files(tmp_path)
    daemon = FakeDaemon().start()
    driver = make_driver(daemon, tmp_path)
    try:
        asyncio.run(driver.connect())
        yield daemon, driver
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


# ---------------------------------------------------------------- 用例

def test_connect_requires_daemon_serial(tmp_path: Path):
    daemon = FakeDaemon(serial_connected=False).start()
    driver = make_driver(daemon, tmp_path)
    try:
        with pytest.raises(DriverError) as exc:
            asyncio.run(driver.connect())
        assert exc.value.category == "driver_connect_failed"
        assert "连接设备" in exc.value.message
        assert not driver._transport.is_open()
    finally:
        daemon.stop()


def test_click_candidate_bar_sequence(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    result = asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    # 目标 (0.5,0.5)@640x480, scale 0.1 → 机械 (32,24)；起点 (2,2,5)：
    # 先回悬停 Z 5→2，再单条 XY 直达，最后 TAP。
    assert daemon.commands == [
        "Z -3.000 F1500",
        "XY 30.000 22.000 F5000",
        "TAP 3.000 F1500",
    ]
    assert result["ok"] is True
    assert result["resolved_robot_xy"] == [32.0, 24.0]
    assert result["target_display"] == "输入法候选栏"
    assert [p["phase"] for p in result["phases"]] == ["hover_z", "travel_xy", "tap"]
    assert result["outcome_hint"] == "unknown"  # 无 motion_verifier → 不得当成功
    assert result["motion_verified"] is False
    assert daemon.lease is None  # 事务结束租约已释放
    assert not daemon.transactions
    # TAP 净位移为零，机械停在悬停高度
    assert daemon.mpos == [32.0, 24.0, 2.0]


def test_click_dynamic_ocr_target_records_source(daemon_and_driver):
    daemon, driver = daemon_and_driver
    result = asyncio.run(driver.click({
        "target_id": "send_button",
        "target_ref": {
            "source": "camera_ocr", "cameraX": 320, "cameraY": 240,
            "frameW": 640, "frameH": 480, "text": "发送", "confidence": 0.96,
        },
    }))
    assert result["target_source"] == "camera_ocr"
    assert result["target_source_display"] == "摄像头 OCR 自动定位"
    assert driver.action_log[-2]["target_source"] == "camera_ocr"


def test_double_tap_is_one_atomic_transaction(daemon_and_driver):
    daemon, driver = daemon_and_driver
    result = asyncio.run(driver.double_tap({
        **dynamic_target(),
        "intervalMs": 120,
    }))

    assert daemon.commands == [
        "Z -3.000 F1500",
        "XY 30.000 22.000 F5000",
        "TAP2 3.000 F1500 I0.12",
    ]
    assert result["tap_count"] == 2
    assert result["interval_ms"] == 120.0
    assert [p["phase"] for p in result["phases"]] == [
        "hover_z", "travel_xy", "double_tap",
    ]
    motion_prefixes = ("Z ", "XY ", "TAP2 ")
    motion_connections = {
        conn_id for conn_id, text in daemon.requests
        if text.startswith(motion_prefixes)
    }
    assert len(motion_connections) == 1
    assert sum(text == "CLAIM business runtime:arm-01" for _, text in daemon.requests) == 1
    assert sum(text == "BEGIN_MOTION" for _, text in daemon.requests) == 1
    assert sum(text == "END_MOTION" for _, text in daemon.requests) == 1
    assert sum(text == "RELEASE" for _, text in daemon.requests) == 1


def test_double_tap_rejects_old_daemon_before_any_motion(daemon_and_driver):
    daemon, driver = daemon_and_driver
    daemon.motion_features = []
    with pytest.raises(DriverError, match="守护进程.*双击"):
        asyncio.run(driver.double_tap(dynamic_target()))
    assert daemon.commands == []
    assert not any(text.startswith("CLAIM") for _, text in daemon.requests)


def test_double_tap_generic_visual_point_uses_existing_mapping(daemon_and_driver):
    daemon, driver = daemon_and_driver
    result = asyncio.run(driver.double_tap({
        "target_id": "gesture_point", "intervalMs": 120,
        "target_ref": {"source": "camera_vision", "cameraX": 320, "cameraY": 240,
                       "frameW": 640, "frameH": 480},
    }))
    assert result["target_display"] == "视觉手势点"
    assert result["resolved_robot_xy"] == [32.0, 24.0]
    assert daemon.commands[-1] == "TAP2 3.000 F1500 I0.12"
    before = list(daemon.commands)
    with pytest.raises(DriverError, match="动态目标"):
        asyncio.run(driver.double_tap({"target_id": "gesture_point"}))
    assert daemon.commands == before


def test_double_tap_verifies_pair_only_and_preserves_calibration(daemon_and_driver, tmp_path):
    daemon, driver = daemon_and_driver
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    events = []

    class Verifier:
        async def before(self, label):
            events.append(("before", label, len(daemon.commands)))

        async def after(self, label):
            events.append(("after", label, len(daemon.commands)))
            return {"verified": True}

    driver.motion_verifier = Verifier()
    result = asyncio.run(driver.double_tap(dynamic_target()))
    assert events[-2:] == [("before", "double_tap", 2), ("after", "double_tap", 3)]
    assert result["motion_verified"] is True
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


def test_double_tap_lost_reply_is_unknown_and_never_replayed(daemon_and_driver):
    daemon, driver = daemon_and_driver
    daemon.response_hook = lambda text, reply, _conn: None if text.startswith("TAP2 ") else reply
    with pytest.raises(DriverError, match="结果未知"):
        asyncio.run(driver.double_tap(dynamic_target()))
    assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1
    assert not any(cmd.startswith("TAP ") for cmd in daemon.commands)
    assert driver._connected is False


def test_double_tap_does_not_report_taps_when_travel_unverified(daemon_and_driver):
    daemon, driver = daemon_and_driver

    class Verifier:
        async def before(self, label):
            pass

        async def after(self, label):
            return {"verified": label != "travel_xy"}

    driver.motion_verifier = Verifier()
    result = asyncio.run(driver.double_tap(dynamic_target()))
    assert result["tap_count"] == 0
    assert result["outcome_hint"] == "unknown"
    assert not any(cmd.startswith("TAP2 ") for cmd in daemon.commands)


def test_swipe_reuses_calibration_mapping_and_is_atomic(daemon_and_driver):
    daemon, driver = daemon_and_driver
    result = asyncio.run(driver.swipe({
        "start": {
            "source": "camera_gesture",
            "cameraX": 100,
            "cameraY": 100,
            "frameW": 640,
            "frameH": 480,
        },
        "end": {
            "source": "camera_gesture",
            "cameraX": 300,
            "cameraY": 100,
            "frameW": 640,
            "frameH": 480,
        },
        "durationMs": 600,
    }))

    assert daemon.commands == [
        "Z -3.000 F1500",
        "XY 8.000 8.000 F5000",
        "SWIPE 20.000 0.000 3.000 F2000 Z1500",
    ]
    assert result["resolved_start_robot_xy"] == [10.0, 10.0]
    assert result["resolved_end_robot_xy"] == [30.0, 10.0]
    assert result["distance_mm"] == 20.0
    assert [p["phase"] for p in result["phases"]] == [
        "swipe_hover_z", "swipe_to_start", "swipe",
    ]
    assert daemon.lease is None
    assert not daemon.transactions


class FailingSwipeVerifier:
    async def before(self, _label):
        return {"captured": True}

    async def after(self, label):
        return {"verified": label != "swipe"}


class RaisingBeforeSwipeVerifier:
    async def before(self, label):
        if label == "swipe":
            raise RuntimeError("复核器暂时不可用")
        return {"captured": True}

    async def after(self, _label):
        return {"verified": True}


def test_swipe_verification_failure_is_checked_after_release(daemon_and_driver):
    daemon, driver = daemon_and_driver
    driver.motion_verifier = FailingSwipeVerifier()

    result = asyncio.run(driver.swipe({
        "start": {"cameraX": 100, "cameraY": 100, "frameW": 640, "frameH": 480},
        "end": {"cameraX": 300, "cameraY": 100, "frameW": 640, "frameH": 480},
        "durationMs": 600,
    }))

    assert result["outcome_hint"] == "unknown"
    assert [p["phase"] for p in result["phases"]][-1] == "swipe"
    assert daemon.commands == [
        "Z -3.000 F1500",
        "XY 8.000 8.000 F5000",
        "SWIPE 20.000 0.000 3.000 F2000 Z1500",
    ]
    assert daemon.mpos[2] == 2.0


def test_swipe_verifier_exception_before_contact_does_not_press(daemon_and_driver):
    daemon, driver = daemon_and_driver
    driver.motion_verifier = RaisingBeforeSwipeVerifier()

    with pytest.raises(RuntimeError, match="复核器"):
        asyncio.run(driver.swipe(swipe_gesture()))

    assert daemon.commands == [
        "Z -3.000 F1500",
        "XY 8.000 8.000 F5000",
    ]
    assert daemon.mpos[2] == 2.0
    wait_until(lambda: daemon.lease is None and not daemon.transactions)


def test_swipe_rejects_old_daemon_without_press_or_travel(daemon_and_driver):
    daemon, driver = daemon_and_driver
    daemon.motion_features = ["tap2.v1"]
    with pytest.raises(DriverError, match="守护进程.*滑动"):
        asyncio.run(driver.swipe(swipe_gesture()))
    assert daemon.commands == []
    assert not any(text.startswith("CLAIM") for _, text in daemon.requests)


def test_swipe_does_not_wait_for_camera_during_contact(daemon_and_driver, tmp_path):
    daemon, driver = daemon_and_driver
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    events = []

    class Verifier:
        async def before(self, label):
            events.append(("before", label, len(daemon.commands), daemon.mpos[2]))

        async def after(self, label):
            events.append(("after", label, len(daemon.commands), daemon.mpos[2]))
            return {"verified": True}

    driver.motion_verifier = Verifier()
    result = asyncio.run(driver.swipe(swipe_gesture()))
    assert events[-2:] == [("before", "swipe", 2, 2), ("after", "swipe", 3, 2)]
    assert result["motion_verified"] is True
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


@pytest.mark.parametrize("gesture", [
    {"start": {"cameraX": 100, "cameraY": 100, "frameW": 640, "frameH": 480},
     "end": {"cameraX": 300, "cameraY": 100, "frameW": 640, "frameH": 480},
     "durationMs": 100},
    {"start": {"cameraX": -1, "cameraY": 100, "frameW": 640, "frameH": 480},
     "end": {"cameraX": 300, "cameraY": 100, "frameW": 640, "frameH": 480},
     "durationMs": 600},
    {"start": {"cameraX": 100, "cameraY": 100, "frameW": 640, "frameH": 480},
     "end": {"cameraX": 100, "cameraY": 100, "frameW": 640, "frameH": 480},
     "durationMs": 600},
    {"start": {"cameraX": 100, "cameraY": 100, "frameW": 640, "frameH": 480},
     "end": {"cameraX": 300, "cameraY": 100, "frameW": 640, "frameH": 480},
     "durationMs": 600, "feed": 4000},
])
def test_invalid_swipe_never_moves(daemon_and_driver, gesture):
    daemon, driver = daemon_and_driver
    with pytest.raises(DriverError):
        asyncio.run(driver.swipe(gesture))
    assert daemon.commands == []


def swipe_gesture(**overrides):
    return {
        "start": {"cameraX": 100, "cameraY": 100, "frameW": 640, "frameH": 480},
        "end": {"cameraX": 300, "cameraY": 100, "frameW": 640, "frameH": 480},
        "durationMs": 600,
        **overrides,
    }


@pytest.mark.parametrize("reply", [None, "{}", "ERROR: response uncertain"])
def test_swipe_uncertain_response_never_replays_or_lifts(
        daemon_and_driver, reply):
    daemon, driver = daemon_and_driver
    daemon.response_hook = lambda line, response, conn: (
        reply if line.startswith("SWIPE ") else response)
    with pytest.raises(DriverError, match="结果未知"):
        asyncio.run(driver.swipe(swipe_gesture()))
    expected = [
        "Z -3.000 F1500",
        "XY 8.000 8.000 F5000",
        "SWIPE 20.000 0.000 3.000 F2000 Z1500",
    ]
    assert daemon.commands == expected
    assert not driver.connected
    with pytest.raises(DriverError):
        asyncio.run(driver.swipe(swipe_gesture()))
    assert daemon.commands == expected
    wait_until(lambda: daemon.lease is None and not daemon.transactions)


def test_swipe_session_changed_during_live_gate_never_moves(daemon_and_driver):
    daemon, driver = daemon_and_driver

    async def gate(**_kwargs):
        daemon.session = "reconnected-after-resolution"
        return []

    driver.live_calibration_gate = gate
    with pytest.raises(DriverError, match="会话锚点变化"):
        asyncio.run(driver.swipe(swipe_gesture()))
    assert daemon.commands == []


@pytest.mark.parametrize("overrides", [
    {"durationMs": float("nan")}, {"durationMs": True},
    {"tapDepth": 0}, {"tapDepth": -1}, {"tapDepth": 11},
    {"tapFeed": 10}, {"feed": float("inf")},
])
def test_swipe_invalid_contact_and_timing_never_moves(daemon_and_driver, overrides):
    daemon, driver = daemon_and_driver
    with pytest.raises(DriverError):
        asyncio.run(driver.swipe(swipe_gesture(**overrides)))
    assert daemon.commands == []


def test_unregistered_fixed_target_rejected_points_to_new_panel(daemon_and_driver_ui):
    """2026-09-04 三步收敛：业务只认 ui_targets 层，未登记明确拒绝并指向新面板。"""
    daemon, driver, _tmp = daemon_and_driver_ui
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_delete_key"}))
    assert exc.value.category == "driver_policy_rejected"
    assert "固定目标登记" in exc.value.message
    assert daemon.commands == []


def test_move_target_hovers_only(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "ime_delete_key", nx=0.2, ny=0.6)
    result = asyncio.run(driver.move_target("ime_delete_key"))
    assert result["ok"] is True
    assert all(not c.startswith("TAP") for c in daemon.commands)
    assert result["target_source"] == "ui_fixed_target"
    assert daemon.mpos[:2] == [12.8, 28.8]  # H=单位阵, robot=(nx*640/10, ny*480/10)


def test_send_button_clickable_without_auth_file(daemon_and_driver_ui):
    """v1.2：桥不维护发送授权文件；发送管控在 WX SendCommand + Arm Fleet 校验层。"""
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "send_button", nx=0.5, ny=0.9)
    result = asyncio.run(driver.click({"target_id": "send_button"}))
    assert result["ok"] is True
    assert any(c.startswith("TAP") for c in daemon.commands)


def test_unknown_dynamic_and_raw_targets_rejected(daemon_and_driver):
    _, driver = daemon_and_driver
    for target, fragment in [
        ({"target_id": "nope"}, "未知业务目标"),
        ({"target_id": "conversation_item"}, "动态会话目标"),
        ({"dx": 1.0, "dy": 1.0}, "只接受 target_id"),
    ]:
        with pytest.raises(DriverError) as exc:
            asyncio.run(driver.click(target))
        assert exc.value.category == "driver_policy_rejected"
        assert fragment in exc.value.message
    assert daemon_and_driver[0].commands == []


def test_unregistered_target_rejected(daemon_and_driver, tmp_path: Path):
    daemon, driver = daemon_and_driver
    device = tmp_path / "devices" / "arm-01" / "targets.json"
    device.write_text("{}", encoding="utf-8")
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert "尚未登记" in exc.value.message
    assert exc.value.category == "driver_policy_rejected"


def test_calibration_missing_and_session_mismatch(daemon_and_driver_ui):
    daemon, driver, tmp_path = daemon_and_driver_ui
    enable_ui_target(tmp_path, "ime_candidate_bar", nx=0.5, ny=0.5)
    # 运行时事实源是 bundle：bundle 与兼容旧档全部缺失 → 未标定拒绝
    (tmp_path / "devices" / "arm-01" / "calibration_bundle.json").unlink()
    (tmp_path / "devices" / "arm-01" / "calibration.json").unlink()
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert exc.value.category == "driver_uncalibrated"
    # 恢复档案且会话一致 → 可以正常点击（ui 目标绑定与重建档案一致）
    write_ready_bundle(tmp_path)
    result = asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert result["ok"] is True
    # 模拟机械臂重启（锚点变化）→ 旧标定失效，运动拒绝，stop 仍可用
    daemon.next_session = "sess-other"
    daemon.session = "sess-other"
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert exc.value.category == "driver_uncalibrated"
    asyncio.run(driver.stop())


def test_total_displacement_limit(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    daemon.mpos[0], daemon.mpos[1] = -280.0, 0.0  # 距目标 >300mm
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert exc.value.category == "driver_limit_exceeded"
    assert daemon.commands == []


def test_xy_direct_move_over_single_command_limit_rejected(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    daemon.mpos[0], daemon.mpos[1] = -301.0, 0.0
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert exc.value.category == "driver_limit_exceeded"
    assert "超过单次上限" in exc.value.message
    assert daemon.commands == []


def test_z_feed_range_enforced_and_f300_allowed(daemon_and_driver_ui):
    """设备更换后 F300 历史失步禁令解除；进给范围 F30-F2000 仍然强制。"""
    from drivers import calibration_bundle as cb
    daemon, driver, tmp = daemon_and_driver_ui

    bundle = cb.load_bundle("arm-01", tmp)
    bundle["z"]["tapFeed"] = 10  # 低于下限：解析期拒绝
    cb.save_bundle(bundle, tmp)
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert exc.value.category == "driver_limit_exceeded"
    assert daemon.commands == []  # 桥侧预检拦截，未下发任何指令

    bundle = cb.load_bundle("arm-01", tmp)
    bundle["z"]["tapFeed"] = 300  # 解禁值：正常放行
    cb.save_bundle(bundle, tmp)
    result = asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert result["ok"] is True
    assert any("F300" in cmd for cmd in daemon.commands)


def test_estop_daemon_level_lock(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    asyncio.run(driver.estop())
    assert daemon.estop_locked is True
    # daemon 级锁定：任何客户端的普通运动都被拒（用一条裸 TCP 连接验证）
    sock = socket.create_connection(("127.0.0.1", daemon.port), timeout=5)
    f = sock.makefile("rwb")
    f.readline()
    f.write(b"CLAIM debug tester\n"); f.flush()
    assert "急停锁定" in f.readline().decode()  # 锁定期间拒绝申请运动租约
    f.write(b"XY 1 0 F150\n"); f.flush()
    assert "急停锁定" in f.readline().decode()
    # STOP 不受锁定阻塞
    asyncio.run(driver.stop())
    # 本地 latch：驱动自身也拒绝
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert exc.value.category == "driver_estop_latched"
    # 人工复位后恢复
    reset = driver.reset_estop("现场操作员")
    assert reset["estop_latched"] is False
    result = asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert result["ok"] is True
    f.close()
    sock.close()


def test_motion_lease_conflict(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    # 第三方先抢占租约（模拟调试台持有）
    sock = socket.create_connection(("127.0.0.1", daemon.port), timeout=5)
    f = sock.makefile("rwb")
    f.readline()
    f.write(b"CLAIM debug console\n"); f.flush()
    json.loads(f.readline().decode())
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert "运动租约被占用" in exc.value.message
    # 释放后业务可正常取得租约执行
    f.write(b"RELEASE\n"); f.flush()
    f.readline()
    result = asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert result["ok"] is True
    # 无租约的普通运动被 daemon 拒绝
    f.write(b"XY 1 0 F150\n"); f.flush()
    assert "运动租约" in f.readline().decode()
    f.close()
    sock.close()


def test_move_preset_rejected_go_home_holds(daemon_and_driver):
    daemon, driver = daemon_and_driver
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.move_preset("x_plus_1mm_conservative"))
    assert exc.value.category == "driver_policy_rejected"
    before = list(daemon.commands)
    result = asyncio.run(driver.go_home())
    assert result["ok"] is True
    assert "保持当前位置" in result["home_mode"]
    assert daemon.commands == before  # 不产生任何运动


def test_supports_and_read_state(daemon_and_driver):
    daemon, driver = daemon_and_driver
    assert driver.supports("arm.move_target")
    assert driver.supports("arm.home")
    assert not driver.supports("arm.move_preset")
    state = asyncio.run(driver.read_state())
    assert state["connected"] is True
    assert state["position"] == {"x": 2.0, "y": 2.0, "z": 5.0}
    assert state["session"] == CALIB_SESSION


# -------------------------------- ui_targets（校准后固定目标登记层，规格书 v1.0）

def write_ready_bundle(tmp: Path, session: str = CALIB_SESSION) -> None:
    """写入 ready 统一档案：H=单位阵，robotToScreen a=e=10（screen=10·robot）。"""
    from drivers import calibration_bundle as cb

    bundle = cb.new_bundle("arm-01")
    bundle.update({
        "status": "ready", "cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-01",
        "bindingRevision": 1, "daemonAddr": "127.0.0.1:8795", "daemonSession": session,
        "frame": {"width": 640, "height": 480},
        "screen": {"orientation": "portrait", "screenWidth": 1080, "screenHeight": 2400,
                   "edgeModel": {"corners": [[0, 0], [640, 0], [640, 480], [0, 480]],
                                 "tolerancePx": 8, "safeInsetPx": 24}},
        "z": {"hoverZ": 2.0, "zSafe": None, "tapDepth": 3.0, "tapFeed": 1500.0,
              "tapDwellMs": 0, "deleteHoldMs": 2200, "deleteDepth": 3.0,
              "deleteFeed": 1500.0, "validated": True, "deleteValidated": True},
        "xy": {"robotToScreen": {"a": 10.0, "b": 0.0, "c": 0.0,
                                 "d": 0.0, "e": 10.0, "f": 0.0},
               "residualRmsPx": 0.5, "maxResidualPx": 1.0,
               "samples": [], "validation": [], "validated": True},
        "camera": {"cameraToScreen": {"homography": [1, 0, 0, 0, 1, 0, 0, 0, 1]},
                   "reprojectionRmsPx": 0.2, "validated": True},
        "acceptance": {"debug": True, "wechat": True, "lastRunAt": "t", "checks": []},
    })
    bundle["bindingFingerprint"] = cb.binding_fingerprint(
        device_id="arm-01", camera_id="cam-01", camera_device_unique_id="uvc-01",
        daemon_addr="127.0.0.1:8795", frame_width=640, frame_height=480)
    cb.save_bundle(bundle, tmp)


def enable_ui_target(tmp: Path, target_id: str, *, nx: float, ny: float,
                     target_mode: str | None = None, name_display: str | None = None) -> None:
    from drivers import ui_targets

    ui_targets.register_draft("arm-01", target_id, nx=nx, ny=ny,
                              target_mode=target_mode, name_display=name_display,
                              owner_plugin="wx-wechat-adapter", data_root=tmp)
    ui_targets.mark_test_result("arm-01", target_id, passed=True,
                                evidence={"kind": "tap_test"}, data_root=tmp)
    ui_targets.set_enabled("arm-01", target_id, enabled=True, data_root=tmp)


@pytest.fixture()
def daemon_and_driver_ui(tmp_path: Path):
    write_device_files(tmp_path)
    write_ready_bundle(tmp_path)
    daemon = FakeDaemon().start()
    driver = make_driver(daemon, tmp_path)
    try:
        asyncio.run(driver.connect())
        yield daemon, driver, tmp_path
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def test_ui_target_overrides_legacy_and_clicks(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    bundle_bytes = (tmp / "devices" / "arm-01" / "calibration_bundle.json").read_bytes()
    enable_ui_target(tmp, "back_button", nx=0.5, ny=0.5)
    result = asyncio.run(driver.click({"target_id": "back_button"}))
    assert result["ok"] is True
    assert result["target_source"] == "ui_fixed_target"
    assert result["target_source_display"] == "设备固定目标（校准后登记）"
    # H=单位阵，robot=(nx*640/10, ny*480/10) → (32.0, 24.0)
    assert result["resolved_robot_xy"] == [32.0, 24.0]
    assert [p["phase"] for p in result["phases"]] == ["hover_z", "travel_xy", "tap"]
    assert daemon.mpos[:2] == [32.0, 24.0]
    # 业务点击不修改基础校准档案
    assert (tmp / "devices" / "arm-01" / "calibration_bundle.json").read_bytes() == bundle_bytes


def test_phone_moves_after_click_travel_never_taps(daemon_and_driver_ui):
    from drivers import calibration_bundle as cb
    from drivers.screen_pose import fit_pose

    daemon, driver, root = daemon_and_driver_ui
    enable_ui_target(root, "back_button", nx=.5, ny=.5)
    corners = cb.load_bundle("arm-01", root)["screen"]["edgeModel"]["corners"]
    initial = fit_pose(corners, [[x + 5, y + 3] for x, y in corners])
    moved = fit_pose(corners, [[x + 10, y + 3] for x, y in corners])
    calls = 0

    async def gate(**_):
        nonlocal calls
        calls += 1
        travelled = any(command.startswith("XY ") for command in daemon.commands)
        return {"checks": [], "pose": moved if travelled else initial}

    driver.live_calibration_gate = gate
    with pytest.raises(DriverError, match="再次移动"):
        asyncio.run(driver.click({"target_id": "back_button"}))
    assert calls >= 2
    assert any(command.startswith("XY ") for command in daemon.commands)
    assert not any(command.startswith("TAP ") for command in daemon.commands)


def test_calibrated_ocr_click_compensates_once(daemon_and_driver_ui):
    from drivers import calibration_bundle as cb
    from drivers.screen_pose import fit_pose

    daemon, driver, root = daemon_and_driver_ui
    corners = cb.load_bundle("arm-01", root)["screen"]["edgeModel"]["corners"]
    pose = fit_pose(corners, [[x + 5, y + 3] for x, y in corners])

    async def gate(**_):
        return {"checks": [], "pose": pose}

    # 本用例只核补偿次数。文字凭据/同页重识别由 pre_enable_s3 整链验证。
    from unittest.mock import AsyncMock
    driver.attach_layout_provider(AsyncMock(return_value=None))
    driver.attach_text_target_verifier(AsyncMock(return_value=lambda: None))
    driver.live_calibration_gate = gate
    result = asyncio.run(driver.click({
        "target_id": "conversation_item",
        "target_ref": {
            "source": "camera_ocr", "cameraX": 320, "cameraY": 240,
            "frameW": 640, "frameH": 480,
            "frameSha256": "a" * 64,
            "coordinateVersion": "calibrated_camera.v1",
        },
    }))
    assert result["resolved_robot_xy"] == [32.5, 24.3]
    assert any(command.startswith("TAP ") for command in daemon.commands)


def test_unanchored_camera_ocr_point_rejected_after_phone_shift(daemon_and_driver_ui):
    from drivers import calibration_bundle as cb
    from drivers.screen_pose import fit_pose

    daemon, driver, root = daemon_and_driver_ui
    corners = cb.load_bundle("arm-01", root)["screen"]["edgeModel"]["corners"]
    pose = fit_pose(corners, [[x + 5, y + 3] for x, y in corners])

    async def gate(**_):
        return {"checks": [], "pose": pose}

    # 保留此叶测试对坐标域拒绝的断言，不把缺复核器的更早拒绝当通过。
    from unittest.mock import AsyncMock
    driver.attach_layout_provider(AsyncMock(return_value=None))
    driver.attach_text_target_verifier(AsyncMock(return_value=lambda: None))
    driver.live_calibration_gate = gate
    with pytest.raises(DriverError, match="原始摄像头坐标缺少观测时姿态"):
        asyncio.run(driver.click({
            "target_id": "conversation_item",
            "target_ref": {"source": "camera_ocr", "cameraX": 320, "cameraY": 240,
                           "frameW": 640, "frameH": 480, "frameSha256": "a" * 64},
        }))
    assert not any(command.startswith(("XY ", "TAP ")) for command in daemon.commands)


def test_unanchored_camera_swipe_rejected_after_phone_shift(daemon_and_driver_ui):
    from drivers import calibration_bundle as cb
    from drivers.screen_pose import fit_pose

    daemon, driver, root = daemon_and_driver_ui
    corners = cb.load_bundle("arm-01", root)["screen"]["edgeModel"]["corners"]
    pose = fit_pose(corners, [[x + 5, y + 3] for x, y in corners])

    async def gate(**_):
        return {"checks": [], "pose": pose}

    driver.live_calibration_gate = gate
    gesture = {
        "start": {"source": "camera_vision", "cameraX": 280, "cameraY": 280,
                  "frameW": 640, "frameH": 480},
        "end": {"source": "camera_vision", "cameraX": 280, "cameraY": 120,
                "frameW": 640, "frameH": 480},
        "durationMs": 600,
    }
    with pytest.raises(DriverError, match="原始摄像头手势点缺少观测时手机姿态"):
        asyncio.run(driver.swipe(gesture))
    assert not any(command.startswith(("XY ", "SWIPE ", "TAP ")) for command in daemon.commands)


def test_ui_hover_only_rejects_click_but_parks(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "ime_delete_key", nx=0.2, ny=0.6)
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "ime_delete_key"}))
    assert exc.value.category == "driver_policy_rejected"
    assert "hover_only" in exc.value.message
    assert daemon.commands == []  # 拒绝发生在任何运动之前

    result = asyncio.run(driver.move_target("ime_delete_key"))
    assert result["ok"] is True and result["tap_executed"] is False
    assert result["target_source"] == "ui_fixed_target"
    assert all(not c.startswith("TAP") for c in daemon.commands)


def test_ui_target_not_enabled_rejected(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    from drivers import ui_targets
    ui_targets.register_draft("arm-01", "back_button", nx=0.062, ny=0.083, data_root=tmp)
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "back_button"}))
    assert exc.value.category == "driver_policy_rejected"
    assert "未启用" in exc.value.message
    assert daemon.commands == []


def test_ui_target_stale_after_recalibration_rejected(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    from drivers import calibration_bundle as cb
    enable_ui_target(tmp, "back_button", nx=0.062, ny=0.083)
    bundle = cb.load_bundle("arm-01", tmp)
    bundle["xy"]["residualRmsPx"] = 0.9  # 重新标定 → 几何哈希变化
    cb.save_bundle(bundle, tmp)
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "back_button"}))
    assert exc.value.category == "driver_uncalibrated"
    assert "待复核" in exc.value.message
    assert daemon.commands == []


def test_ui_custom_target_id_and_unknown_rejected(daemon_and_driver_ui):
    daemon, driver, tmp = daemon_and_driver_ui
    enable_ui_target(tmp, "wx_home_button", nx=0.1, ny=0.1,
                     target_mode="tap", name_display="自定义主页按钮")
    result = asyncio.run(driver.click({"target_id": "wx_home_button"}))
    assert result["ok"] is True
    assert result["target_display"] == "自定义主页按钮"

    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.click({"target_id": "no_such_button"}))
    assert exc.value.category == "driver_policy_rejected"
    assert "未知业务目标" in exc.value.message


def dynamic_target(**overrides):
    return {"target_id": "send_button", "target_ref": {
        "source": "camera_ocr", "cameraX": 320, "cameraY": 240,
        "frameW": 640, "frameH": 480, **overrides,
    }}


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "fake daemon did not finish cleanup"
        time.sleep(0.005)


@pytest.mark.parametrize("identity", ["arm-02", None])
def test_wrong_or_missing_identity_closes_connection(daemon_and_driver, identity):
    daemon, driver = daemon_and_driver
    daemon.device_id = identity
    with pytest.raises(DriverError, match="身份不匹配"):
        asyncio.run(driver.connect())
    assert not driver.connected
    assert not driver._transport.is_open()
    assert daemon.commands == []


def test_control_refuses_wrong_device(daemon_and_driver):
    daemon, driver = daemon_and_driver
    daemon.device_id = "arm-02"
    assert asyncio.run(driver.stop())["stopped"] is False
    assert asyncio.run(driver.estop())["daemon_locked"] is False
    assert daemon.estop_writes == 0
    assert not daemon.estop_locked
    assert driver._estop_latched


@pytest.mark.parametrize("overrides", [
    {"cameraX": float("nan")}, {"cameraY": float("inf")},
    {"frameW": float("-inf")}, {"frameH": None},
    {"cameraX": -1}, {"cameraY": 481}, {"cameraX": True},
    {"targetMode": "invalid"},
    {"cameraX": None, "bbox": [0, 0, float("nan"), 5]},
])
def test_invalid_dynamic_target_never_moves(daemon_and_driver, overrides):
    daemon, driver = daemon_and_driver
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dynamic_target(**overrides)))
    assert daemon.commands == []
    assert daemon.lease is None


@pytest.mark.parametrize("field", ["hoverZ", "tapDepth", "tapFeed"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), None])
def test_invalid_bundle_motion_values_rejected(daemon_and_driver_ui, field, value):
    from drivers import calibration_bundle as cb
    daemon, driver, tmp = daemon_and_driver_ui
    bundle = cb.load_bundle("arm-01", tmp)
    bundle["z"][field] = value
    cb.save_bundle(bundle, tmp)
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dynamic_target()))
    assert daemon.commands == []
    assert daemon.lease is None


@pytest.mark.parametrize("field", ["x", "y", "z"])
def test_nonfinite_daemon_position_rejected(daemon_and_driver, field):
    daemon, driver = daemon_and_driver
    daemon.mpos["xyz".index(field)] = float("nan")
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dynamic_target()))
    assert daemon.commands == []
    assert daemon.lease is None
    assert not daemon.transactions


def test_nonfinite_calibration_and_huge_hover_rejected(daemon_and_driver, tmp_path):
    daemon, driver = daemon_and_driver
    path = tmp_path / "devices" / "arm-01" / "calibration.json"
    calibration = json.loads(path.read_text())
    calibration["affine"]["a"] = float("nan")
    path.write_text(json.dumps(calibration))
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dynamic_target()))
    write_device_files(tmp_path)
    profile = tmp_path / "devices" / "arm-01" / "arm_profile.json"
    profile.write_text(json.dumps({"hoverZ": 1e100, "tapDepth": 3, "tapFeed": 1500}))
    with pytest.raises(DriverError, match="Z 悬停总位移"):
        asyncio.run(driver.click(dynamic_target()))
    assert not daemon.commands


@pytest.mark.parametrize("response", [None, "not-json", "{}", "ERROR: failed after write"])
def test_motion_reply_loss_or_error_never_replays(daemon_and_driver, response):
    daemon, driver = daemon_and_driver
    daemon.response_hook = lambda line, reply, conn_id: (
        response if line.startswith("Z ") else reply)
    with pytest.raises(DriverError, match="结果未知") as exc:
        asyncio.run(driver.click(dynamic_target()))
    assert exc.value.category == "driver_disconnected"
    assert daemon.commands == ["Z -3.000 F1500"]
    assert not driver.connected
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dynamic_target()))
    assert daemon.commands == ["Z -3.000 F1500"]
    wait_until(lambda: daemon.lease is None and not daemon.transactions)


@pytest.mark.parametrize("command,response", [
    ("CLAIM", "{}"), ("CLAIM", "invalid"),
    ("BEGIN_MOTION", "{}"), ("BEGIN_MOTION", "ERROR: setup rejected"),
])
def test_setup_failure_releases_claim(daemon_and_driver, command, response):
    daemon, driver = daemon_and_driver
    daemon.response_hook = lambda line, reply, conn_id: (
        response if line.split()[0] == command else reply)
    with pytest.raises(DriverError):
        asyncio.run(driver.click(dynamic_target()))
    wait_until(lambda: daemon.lease is None and not daemon.transactions)
    assert daemon.commands == []


@pytest.mark.parametrize("command", ["END_MOTION", "RELEASE"])
def test_cleanup_timeout_is_bounded(daemon_and_driver, monkeypatch, command):
    import drivers.daemon_arm as bridge
    daemon, driver = daemon_and_driver
    monkeypatch.setattr(bridge, "CLEANUP_TIMEOUT_S", 0.08)
    release = threading.Event()
    daemon.response_hook = lambda line, reply, conn_id: (
        reply if line != command or release.wait(2) else None)
    started = time.monotonic()
    try:
        with pytest.raises(DriverError, match="结果未知"):
            asyncio.run(driver.click(dynamic_target()))
        assert time.monotonic() - started < 1
        assert not driver.connected
        assert len(daemon.commands) == 3
    finally:
        release.set()
    wait_until(lambda: daemon.lease is None and not daemon.transactions)


@pytest.mark.parametrize("action", ["stop", "estop", "disconnect", "cancel"])
def test_blocked_motion_can_be_interrupted(daemon_and_driver, action):
    daemon, driver = daemon_and_driver
    blocked, release = threading.Event(), threading.Event()

    def hold_motion(line, reply, conn_id):
        if line.startswith("Z "):
            blocked.set()
            release.wait(2)
        return reply

    daemon.response_hook = hold_motion

    async def run():
        motion = asyncio.create_task(driver.click(dynamic_target()))
        try:
            assert await asyncio.to_thread(blocked.wait, 1)
            started = time.monotonic()
            if action == "cancel":
                motion.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await motion
            else:
                result = await asyncio.wait_for(getattr(driver, action)(), timeout=0.8)
                if action == "stop":
                    assert result["stopped"] is True
                elif action == "estop":
                    assert result["daemon_locked"] is True
                with pytest.raises(DriverError):
                    await asyncio.wait_for(motion, timeout=0.8)
            assert time.monotonic() - started < 0.8
            assert not release.is_set()
            assert not driver.connected
            assert daemon.commands == ["Z -3.000 F1500"]
            if action in {"stop", "estop"}:
                motion_conn = next(c for c, text in daemon.requests if text.startswith("Z "))
                control_conn = next(c for c, text in daemon.requests
                                    if text.split()[0] in {"STOP", "ESTOP_LOCK"})
                assert control_conn != motion_conn
        finally:
            release.set()
            if not motion.done():
                motion.cancel()
            await asyncio.gather(motion, return_exceptions=True)

    asyncio.run(run())
    wait_until(lambda: daemon.lease is None and not daemon.transactions)


def test_session_change_after_claim_prevents_motion(daemon_and_driver):
    daemon, driver = daemon_and_driver

    def change_session(line, reply, conn_id):
        if line == "BEGIN_MOTION":
            daemon.session = "replacement-session"
        return reply

    daemon.response_hook = change_session
    with pytest.raises(DriverError, match="会话锚点变化"):
        asyncio.run(driver.click(dynamic_target()))
    assert daemon.commands == []
    assert daemon.lease is None
    assert not daemon.transactions


def test_session_change_between_segments_never_continues(daemon_and_driver):
    daemon, driver = daemon_and_driver

    def change_session(line, reply, conn_id):
        if line.startswith("Z "):
            daemon.session = "replacement-session"
        return reply

    daemon.response_hook = change_session
    with pytest.raises(DriverError, match="结果未知"):
        asyncio.run(driver.click(dynamic_target()))
    assert daemon.commands == ["Z -3.000 F1500"]
    assert not driver.connected


@pytest.mark.parametrize("command,response", [
    ("RESET_ESTOP", "{}"), ("RESET_ESTOP", "ERROR: rejected"),
    ("MPOS", '{"x": 0, "y": 0, "z": 0, "state": "Run"}'),
    ("MPOS", '{"x": NaN, "y": 0, "z": 0, "state": "Idle"}'),
    ("MPOS", None),
])
def test_failed_reset_preserves_local_latch(daemon_and_driver, command, response):
    daemon, driver = daemon_and_driver
    asyncio.run(driver.estop())
    daemon.response_hook = lambda line, reply, conn_id: (
        response if line.split()[0] == command else reply)
    with pytest.raises(DriverError):
        driver.reset_estop("operator")
    assert driver._estop_latched
    with pytest.raises(DriverError, match="急停"):
        asyncio.run(driver.click(dynamic_target()))
    assert not daemon.commands


def test_older_reset_response_cannot_clear_newer_estop(daemon_and_driver, monkeypatch):
    _daemon, driver = daemon_and_driver
    original = driver._control_cmd

    def reset_with_newer_stop(line, *, reset=False):
        response = original(line, reset=reset)
        if reset:
            asyncio.run(driver.estop())
        return response

    monkeypatch.setattr(driver, "_control_cmd", reset_with_newer_stop)
    with pytest.raises(DriverError, match="急停"):
        driver.reset_estop("operator")
    assert driver._estop_latched


def test_transport_handshake_timeout_closes_socket():
    daemon = FakeDaemon()
    daemon.welcome = b""
    daemon.start()
    transport = DaemonTCPTransport(f"127.0.0.1:{daemon.port}", timeout=0.08)
    try:
        started = time.monotonic()
        with pytest.raises(DriverError):
            transport.open()
        assert time.monotonic() - started < 1
        assert not transport.is_open()
        wait_until(lambda: not daemon._clients)
    finally:
        transport.close()
        daemon.stop()


def test_transport_preserves_coalesced_lines():
    transport = DaemonTCPTransport(timeout=0.1)
    reader, writer = socket.socketpair()
    try:
        writer.sendall(b"first\nsecond\n")
        received = bytearray()
        deadline = time.monotonic() + 0.1
        assert transport._readline(reader, received, deadline) == "first"
        assert transport._readline(reader, received, deadline) == "second"
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_transport_polling_preserves_deadline_and_observes_cancel_without_replay(cancel):
    transport = DaemonTCPTransport(timeout=1)

    class PollingSocket:
        def __init__(self):
            self.timeouts = []
            self.writes = []
            self.reads = 0

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def sendall(self, data):
            self.writes.append(data)

        def recv(self, _size):
            self.reads += 1
            if self.reads == 1:
                if cancel:
                    transport.close()
                raise socket.timeout()
            return b"{}\n"

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    sock = PollingSocket()
    transport._sock = sock
    if cancel:
        with pytest.raises(DriverError):
            transport.cmd("XY 1 0")
        assert sock.reads == 1
    else:
        assert transport.cmd("XY 1 0") == "{}"
    assert sock.writes == [b"XY 1 0\n"]
    assert all(timeout <= 0.1 for timeout in sock.timeouts[1:])


@pytest.mark.parametrize("line", ["STATUS\nTAP 1", "STOP\rRESET_ESTOP bad"])
def test_transport_rejects_multiline_commands(daemon_and_driver, line):
    daemon, driver = daemon_and_driver
    before = list(daemon.requests)
    with pytest.raises(DriverError, match="单行"):
        driver._transport.cmd(line)
    assert daemon.requests == before


def test_stop_preserves_daemon_failed_delivery(daemon_and_driver):
    daemon, driver = daemon_and_driver
    daemon.response_hook = lambda line, reply, conn_id: (
        '{"stopped": false, "outcome_hint": "unknown", "error": "串口写入失败"}'
        if line == "STOP" else reply)
    result = asyncio.run(driver.stop())
    assert result["ok"] is False
    assert result["stopped"] is False
    assert result["outcome_hint"] == "unknown"
    assert result["error"] == "串口写入失败"
    assert "串口写入失败" in result["message"]


def test_estop_preserves_failed_stop_delivery(daemon_and_driver):
    daemon, driver = daemon_and_driver
    daemon.response_hook = lambda line, reply, conn_id: (
        '{"estop_locked": true, "stopped": false, "outcome_hint": "unknown",'
        ' "error": "实时停止下发失败"}'
        if line == "ESTOP_LOCK 平台业务急停" else reply)
    result = asyncio.run(driver.estop())
    assert result["ok"] is False
    assert result["daemon_locked"] is True
    assert result["stopped"] is False
    assert result["outcome_hint"] == "unknown"
    assert result["error"] == "实时停止下发失败"
    assert "结果未知" in result["message"]
    assert driver._estop_latched


def test_tap_dwell_is_finite_bounded_and_forwarded(daemon_and_driver_ui):
    from drivers import calibration_bundle as cb
    daemon, driver, tmp = daemon_and_driver_ui
    bundle = cb.load_bundle("arm-01", tmp)
    bundle["z"]["tapDwellMs"] = 2500
    cb.save_bundle(bundle, tmp)
    enable_ui_target(tmp, "send_button", nx=0.5, ny=0.5)
    asyncio.run(driver.click({"target_id": "send_button"}))
    assert "TAP 3.000 F1500 P2.5" in daemon.commands


@pytest.mark.parametrize("dwell_ms", [-1, 5001, float("nan"), float("inf")])
def test_invalid_tap_dwell_rejected_before_motion(daemon_and_driver_ui, dwell_ms):
    from drivers import calibration_bundle as cb
    daemon, driver, tmp = daemon_and_driver_ui
    bundle = cb.load_bundle("arm-01", tmp)
    bundle["z"]["tapDwellMs"] = dwell_ms
    cb.save_bundle(bundle, tmp)
    enable_ui_target(tmp, "send_button", nx=0.5, ny=0.5)
    with pytest.raises(DriverError):
        asyncio.run(driver.click({"target_id": "send_button"}))
    assert daemon.commands == []


@pytest.mark.parametrize("tap_depth", [0, -1, float("nan"), float("inf")])
def test_invalid_tap_depth_rejected_before_motion(daemon_and_driver_ui, tap_depth):
    from drivers import calibration_bundle as cb
    daemon, driver, tmp = daemon_and_driver_ui
    bundle = cb.load_bundle("arm-01", tmp)
    bundle["z"]["tapDepth"] = tap_depth
    cb.save_bundle(bundle, tmp)
    enable_ui_target(tmp, "send_button", nx=0.5, ny=0.5)
    with pytest.raises(DriverError):
        asyncio.run(driver.click({"target_id": "send_button"}))
    assert daemon.commands == []


# -------------------------------- execute_gesture_program 末道门禁（评审 P0-2/P1-7）

def _path1_plan(tmp: Path) -> dict:
    """用 write_ready_bundle 的档案编译一份合法 path1 计划（屏幕域两点滑动）。"""
    from drivers import calibration_bundle as cb
    from drivers import gesture_program as gp
    from drivers import screen_gestures as sg

    bundle = cb.load_bundle("arm-01", tmp)
    frame = gp.build_coordinate_frame(
        binding=sg.binding_snapshot(bundle), calibration_run_id="run-path1-test",
        root_width_px=bundle["screen"]["screenWidth"],
        root_height_px=bundle["screen"]["screenHeight"],
        display_width_px=1080, display_height_px=2400,
        root_origin_display_px=[0, 80], density=3.0,
        source_client_instance_id="0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001")
    program = {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": [{
            "points": [{"x": 0.5, "y": 0.7, "t_ms": 0},
                       {"x": 0.45, "y": 0.55, "t_ms": 1200},
                       {"x": 0.5, "y": 0.4, "t_ms": 2400}],
            "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0,
        }],
    }
    return gp.plan_program(program, bundle, frame,
                           limits={"path_feature_available": True})


def _write_path1_bundle(tmp: Path) -> None:
    """几何自洽的 ready 档案：缩放单应把屏幕 1080x2400 映进相机 640x480。

    write_ready_bundle 的单位阵 H 会令屏幕点投影超出相机画面，
    归一化程序点无法解析；这里 screen=10·robot 保持不变。
    """
    write_ready_bundle(tmp)
    from drivers import calibration_bundle as cb

    bundle = cb.load_bundle("arm-01", tmp)
    bundle["camera"]["cameraToScreen"]["homography"] = [
        1080.0 / 640.0, 0.0, 0.0,
        0.0, 2400.0 / 480.0, 0.0,
        0.0, 0.0, 1.0]
    cb.save_bundle(bundle, tmp)


@pytest.fixture()
def daemon_and_driver_path1(tmp_path: Path):
    write_device_files(tmp_path)
    _write_path1_bundle(tmp_path)
    daemon = FakeDaemon().start()
    driver = make_driver(daemon, tmp_path)
    try:
        asyncio.run(driver.connect())
        yield daemon, driver, tmp_path
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def test_path1_executes_with_calibrated_profile(daemon_and_driver_path1):
    daemon, driver, tmp = daemon_and_driver_path1
    result = asyncio.run(driver.execute_gesture_program(_path1_plan(tmp)))
    assert result["controller_completed"] is True
    assert any(cmd.startswith("PATH1 ") for cmd in daemon.commands)


def test_path1_rejects_plan_profile_not_derived_from_calibration(
        daemon_and_driver_path1):
    """P0-2：外部 plan 的 profile 必须与当前标定推导一致，伪造 tapDepth 拒绝。"""
    daemon, driver, tmp = daemon_and_driver_path1
    plan = _path1_plan(tmp)
    plan["profile"]["tapDepth"] = 9.0  # 仍在量程内（≤10mm），但非当前标定推导
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.execute_gesture_program(plan))
    assert exc.value.category == "driver_gesture_invalid"
    assert not any(cmd.startswith("PATH1 ") for cmd in daemon.commands)
    assert daemon.lease is None


def test_path1_rejects_waypoint_outside_calibrated_screen(daemon_and_driver_path1):
    """P0-2：mech_points 反解屏幕域必须落在校准屏幕区域内，裸机械坐标不放行。"""
    daemon, driver, tmp = daemon_and_driver_path1
    plan = _path1_plan(tmp)
    stroke = plan["strokes"][0]
    # write_ready_bundle 的映射 screen=10·robot：整体平移到 robot≈(500,120)，
    # 屏幕域 x≈5000 远超 1080 宽，即校准屏幕区域之外
    stroke["mech_points"] = [
        {"robotX": p["robotX"] + 446.0, "robotY": p["robotY"]}
        for p in stroke["mech_points"]]
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.execute_gesture_program(plan))
    assert exc.value.category == "driver_gesture_invalid"
    assert not any(cmd.startswith("PATH1 ") for cmd in daemon.commands)
    assert daemon.lease is None


def test_path1_lease_uses_gesture_verifier(daemon_and_driver_path1):
    """P1-7：PATH1 租约逐段核验必须用 gesture 核验器，而非帧变化核验器。"""
    daemon, driver, tmp = daemon_and_driver_path1
    calls: list[tuple] = []

    class GestureVerifier:
        async def before(self, label):
            calls.append(("gesture", "before", label))

        async def after(self, label):
            calls.append(("gesture", "after", label))
            return {"verified": True}

    class FrameChangeVerifier:
        measurement_kind = "frame_change"

        async def before(self, label):
            calls.append(("frame", "before", label))

        async def after(self, label):
            calls.append(("frame", "after", label))
            return {"verified": True}

    driver.gesture_motion_verifier = GestureVerifier()
    driver.motion_verifier = FrameChangeVerifier()
    result = asyncio.run(driver.execute_gesture_program(_path1_plan(tmp)))
    assert result["controller_completed"] is True
    assert calls, "PATH1 执行期间应调用核验器"
    assert all(owner == "gesture" for owner, _event, _label in calls), calls


# -------------------------------- 校准双击自检回执（评审 P1-6）

def _self_check_params(tmp: Path) -> dict:
    from drivers import calibration_bundle as cb
    from drivers import screen_gestures as sg

    bundle = cb.load_bundle("arm-01", tmp)
    return {"run_id": "run-1", "trial_id": "trial-1", "plan_id": "plan-1",
            "binding": sg.binding_snapshot(bundle),
            "point": {"source": "screen_normalized", "x": 0.5, "y": 0.5},
            "interval_ms": 50}


@pytest.fixture()
def daemon_and_driver_selfcheck(tmp_path: Path):
    write_device_files(tmp_path)
    _write_path1_bundle(tmp_path)
    daemon = FakeDaemon().start()
    driver = make_driver(daemon, tmp_path)
    try:
        asyncio.run(driver.connect())
        yield daemon, driver, tmp_path
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def test_calibrated_double_tap_completed_reports_release(daemon_and_driver_selfcheck):
    daemon, driver, tmp = daemon_and_driver_selfcheck
    reply = asyncio.run(driver.test_calibrated_double_tap(_self_check_params(tmp)))
    assert reply["controller_status"] == "completed"
    assert reply["contact_released"] is True
    assert any(cmd.startswith("TAP2 ") for cmd in daemon.commands)


def test_calibrated_double_tap_unknown_controller_does_not_claim_release(
        daemon_and_driver_selfcheck):
    """P1-6：controller unknown（恰是笔尖可能停在屏上的情形）不得伪造已抬离证据。"""
    daemon, driver, tmp = daemon_and_driver_selfcheck

    class Verifier:
        async def before(self, label):
            pass

        async def after(self, label):
            return {"verified": label != "travel_xy"}

    driver.motion_verifier = Verifier()
    reply = asyncio.run(driver.test_calibrated_double_tap(_self_check_params(tmp)))
    assert reply["controller_status"] == "unknown"
    assert reply["contact_released"] is None, "结果未知不得当成功报告已抬离"
    assert not any(cmd.startswith("TAP2 ") for cmd in daemon.commands)


def test_path1_intermediate_stroke_endpoint_mismatch_is_unknown(
        daemon_and_driver_path1):
    """R1 P2 半修项：中间 stroke 终点漂移须逐段核对，不能只靠最终位姿兜底。"""
    daemon, driver, tmp = daemon_and_driver_path1
    plan = _path1_plan(tmp)
    plan["strokes"].append(json.loads(json.dumps(plan["strokes"][0])))  # 两段
    seen = {"n": 0}

    def hook(text, reply, _conn):
        if text.startswith("PATH1 "):
            seen["n"] += 1
            if seen["n"] == 1:  # 只篡改第一段回执坐标（+1mm 漂移）
                payload = json.loads(reply)
                payload["x"] = float(payload["x"]) + 1.0
                return json.dumps(payload)
        return reply

    daemon.response_hook = hook
    with pytest.raises(DriverError, match="结果未知"):
        asyncio.run(driver.execute_gesture_program(plan))
    assert not driver._connected


def test_normalized_swipe_stroke_mm_scales_endpoint(daemon_and_driver_selfcheck):
    """P0-2（2026-09-30 审查）：归一化卫兵放行 strokeMm——此前白名单
    {"start","end","binding","durationMs"} 把归一化路径的 strokeMm 缩放
    逻辑整段打成不可达（DriverError 禁止覆盖机械参数）。修复后：方向保持
    start→end，终点沿向量缩放到给定毫米数。"""
    from drivers import calibration_bundle as cb
    from drivers import screen_gestures as sg

    daemon, driver, tmp = daemon_and_driver_selfcheck

    class GestureVerifier:
        async def before(self, label):
            pass

        async def after(self, label):
            return {"verified": True}

    driver.gesture_motion_verifier = GestureVerifier()
    bundle = cb.load_bundle("arm-01", tmp)
    result = asyncio.run(driver.swipe({
        "start": {"source": "screen_normalized", "x": 0.5, "y": 0.8},
        "end": {"source": "screen_normalized", "x": 0.5, "y": 0.6},
        "durationMs": 600,
        "strokeMm": 30.0,
        "binding": sg.binding_snapshot(bundle),
    }))
    sx, sy = result["resolved_start_robot_xy"]
    ex, ey = result["resolved_end_robot_xy"]
    assert ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5 == pytest.approx(30.0, abs=1e-6)
    assert result["distance_mm"] == pytest.approx(30.0, abs=1e-6)
    # 方向保持：终点仍由 start 沿原向量方向延伸（y 减小 = 内容上滑）
    assert ey < sy and ex == pytest.approx(sx, abs=1e-6)


def test_normalized_swipe_stroke_mm_survives_live_pose_compensation(
        daemon_and_driver_selfcheck):
    """N-1（R2 复审 2026-09-30）：调试台真机路径总是装 live_calibration_gate
    （motor_debug_server.py 基础手势入口）——位姿补偿用 refs 覆写两端点，
    把早先按 strokeMm 缩放的结果丢弃，mm 滑动长度在真机上静默 no-op
    （无 gate 的旧缩放测试测不到这条真路径）。修复后：补偿之后重施缩放，
    执行距离=给定毫米。"""
    import numpy as np
    from drivers import calibration_bundle as cb
    from drivers import screen_gestures as sg

    daemon, driver, tmp = daemon_and_driver_selfcheck

    class GestureVerifier:
        async def before(self, label):
            pass

        async def after(self, label):
            return {"verified": True}

    driver.gesture_motion_verifier = GestureVerifier()
    bundle = cb.load_bundle("arm-01", tmp)
    # path1 夹具的 H 是 9 元扁平数组（其用例不经补偿路径）；补偿路径的
    # np.linalg.inv 需要规范 3x3——重写为真机档案同形态后再取绑定。
    bundle["camera"]["cameraToScreen"]["homography"] = [
        [bundle["camera"]["cameraToScreen"]["homography"][0],
         bundle["camera"]["cameraToScreen"]["homography"][1],
         bundle["camera"]["cameraToScreen"]["homography"][2]],
        [bundle["camera"]["cameraToScreen"]["homography"][3],
         bundle["camera"]["cameraToScreen"]["homography"][4],
         bundle["camera"]["cameraToScreen"]["homography"][5]],
        [bundle["camera"]["cameraToScreen"]["homography"][6],
         bundle["camera"]["cameraToScreen"]["homography"][7],
         bundle["camera"]["cameraToScreen"]["homography"][8]]]
    cb.save_bundle(bundle, tmp)
    frame_w, frame_h = bundle["frame"]["width"], bundle["frame"]["height"]
    pose = {"matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "corners": np.float32([[0, 0], [frame_w, 0],
                                   [frame_w, frame_h], [0, frame_h]]),
            "movementPx": 0.0}

    async def live_gate(*, wechat_send=False, screen_edge_advisory=False):
        # 身份位姿：补偿后的端点与直接解析一致——本测试只隔离「缩放被覆写」
        # 这一个变量（真机 gate 必在，见 motor_debug_server 基础手势入口）。
        return {"checks": [], "pose": pose}

    driver.live_calibration_gate = live_gate
    result = asyncio.run(driver.swipe({
        "start": {"source": "screen_normalized", "x": 0.5, "y": 0.8},
        "end": {"source": "screen_normalized", "x": 0.5, "y": 0.6},
        "durationMs": 800,
        "strokeMm": 30.0,
        "binding": sg.binding_snapshot(bundle),
    }))
    sx, sy = result["resolved_start_robot_xy"]
    ex, ey = result["resolved_end_robot_xy"]
    assert ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5 == pytest.approx(30.0, abs=1e-6)
    assert result["distance_mm"] == pytest.approx(30.0, abs=1e-6)
    assert ey < sy, "方向保持 start→end（上滑）"


@pytest.mark.real_input_switch
def test_input_switch_gate_blocks_ime_targets_when_switch_off(tmp_path: Path):
    """A-开关（§4.6）：开关关闭时 ime_candidate_bar/delete_key/send_button 的
    点击与双击被拒（input_switch_disabled）；开启后放行；非输入目标不受影响。"""
    from runtime import input_settings as iset

    def build():
        write_device_files(tmp_path)
        daemon = FakeDaemon().start()
        driver = make_driver(daemon, tmp_path)
        asyncio.run(driver.connect())
        return daemon, driver

    daemon, driver = build()
    try:
        write_ready_bundle(tmp_path)
        enable_ui_target(tmp_path, "ime_candidate_bar", nx=0.5, ny=0.5)
        iset.update_settings("arm-01", data_root=tmp_path, enabled=False,
                             ime_device_id=None, expected_revision=0,
                             operator_id="测试", reason="关",
                             client_request_id="gate-off", calibration_ready=False)
        with pytest.raises(DriverError) as caught:
            asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
        assert caught.value.category == "input_switch_disabled"
        iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                             ime_device_id="k40-test", expected_revision=1,
                             operator_id="测试", reason="开",
                             client_request_id="gate-on", calibration_ready=True)
        result = asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
        assert result.get("ok") is True
    finally:
        asyncio.run(driver.disconnect())
