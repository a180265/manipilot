"""常驻串口守护 v3：手动连接 + 在位感知 + 会话锚点 + 可调速度 + 运动租约。

设计（docs/daemon驱动桥设计与职责边界.md v1.2）：
- 项目可以先于机械臂启动：守护进程只监听"设备在位"（每 2 秒 stat 一次串口设备，
  不打开串口），前端据此显示「设备未插入 / 设备已插入·待连接」。
- 串口连接由人工触发：页面点「连接设备」→ CONNECT 指令 → 打开串口并生成
  会话锚点。打开串口必然复位 ESP32（DTR 复位怪癖、MPos 重新计数），所以
  绝不自动连接，什么时候复位由人决定。
- 拔线感知：在位监听发现设备节点消失，或运动指令遇到串口异常，立即丢弃当前
  会话；重新插上后需要再次人工「连接设备」，新连接生成新锚点。
- 会话锚点：下游标定数据与锚点绑定。锚点不变 → 标定继续有效；锚点变了
  （断开重连/守护进程重启/换线）→ 标定失效，必须重新校准。
- 运动租约（v1.2）：多客户端（调试台/业务桥）可同时连接，但 XY/Z/TAP 及点击
  序列属于普通运动，必须先 CLAIM 取得租约；同一串口同一时刻只有一个租约；
  断线/超时/daemon 重启自动失效；业务租约与调试租约互斥。
- 事务：一次点击从 BEGIN_MOTION 到 END_MOTION 持有同一租约与事务 ID。
- daemon 级急停锁定：ESTOP_LOCK 形成全局锁定（不只单个客户端），锁定期间所有
  普通运动拒绝；STOP/ESTOP 不受普通租约与锁定阻塞；RESET_ESTOP 人工复位。
- XY / Z / TAP 支持可选 `F<feed>`（TAP 还可 `P<秒>` 接触停留）；默认
  Z F30 / XY F150 / 点击 F1500。安全限值统一引用 drivers/daemon_protocol.py。

指令（一行一条，响应一行 JSON 或 ERROR）：
  CONNECT / DISCONNECT                连接/断开串口（人工触发，生成/销毁会话锚点）
  STATUS                              连接、在位、会话、急停锁定、运动租约快照
  MPOS                                当前位置 {"x","y","z","state"}
  CLAIM <owner_type> <owner_id> [task_id]   申请运动租约（business|debug）
  RENEW                               续期本连接持有的租约
  RELEASE                             释放本连接持有的租约
  BEGIN_MOTION                        开始动作事务（需租约）
  END_MOTION                          结束动作事务（需租约）
  ESTOP_LOCK [reason...]              急停锁定（daemon 级，立即下发 !）
  RESET_ESTOP <operator>              人工复位急停锁定
  Z <dz> [F<f>] / XY <dx> <dy> [F<f>] / TAP <depth> [F<f>] [P<sec>]   普通运动（需租约）
  TAP2 <depth> [F<f>] [P<sec>] [I<sec>] [R<mm>]  双击；I 抬起停顿，R 可选中途短抬距离
  SWIPE <dx> <dy> <depth> F<xy> Z<z>    连续按下、拖动、抬起，沿用校准深度与进给
  PATH1 <compact-json>                  连续路径宏（path.v1）：down/move/hold/up 序列
  STOP                                进给保持（不受租约/锁定阻塞）
  RESUME                              ~ 恢复进给
  BYE                                 断开本客户端

运行：nohup .venv/bin/python scripts/arm_daemon3.py > /tmp/arm_daemon3.log 2>&1 &
"""
from __future__ import annotations

import json
import math
import os
import select
import socket
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

import serial
import serial.serialutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from drivers.daemon_protocol import (  # noqa: E402
    DEFAULT_TAP_FEED,
    DEFAULT_XY_FEED,
    DEFAULT_Z_FEED,
    DOUBLE_TAP_FEATURE,
    DOUBLE_TAP_FAST_FEATURE,
    DOUBLE_TAP_SHORT_LIFT_FEATURE,
    DOUBLE_TAP_SHORT_LIFT_MAX_MM,
    LEASE_TTL_S,
    LEASE_OWNER_BUSINESS,
    LEASE_OWNER_DEBUG,
    PATH1_FEATURE,
    PATH1_LINE_MAX_BYTES,
    SWIPE_FEATURE,
    check_feed,
    check_double_tap_interval,
    check_double_tap_feed,
    check_stroke,
    check_tap,
    check_swipe,
    reject_json_constant,
    strict_object_pairs,
    validate_path1_payload,
)

PORT = os.environ.get("ARM_DAEMON_SERIAL", "/dev/cu.usbserial-212420")
BAUD = int(os.environ.get("ARM_DAEMON_BAUD", "115200"))
LISTEN = ("127.0.0.1", int(os.environ.get("ARM_DAEMON_PORT", "8795")))
DEVICE_ID = os.environ.get("ARM_DAEMON_DEVICE_ID", "arm-01").strip() or "arm-01"
WATCH_INTERVAL_S = 2.0
MPOS_RESPONSE_TIMEOUT_S = 2.0
CLIENT_IDLE_TIMEOUT_S = 120.0
COMMAND_LOCK_TIMEOUT_S = 5.0
MAX_CLIENTS = 64
EMERGENCY_RESERVED_CLIENTS = 2
EMERGENCY_CLIENT_TIMEOUT_S = 2.0
SERIAL_OPEN_ATTEMPTS = 2
SERIAL_OPEN_RETRY_DELAY_S = 0.35
EMERGENCY_COMMANDS = {"STATUS", "STOP", "ESTOP_LOCK"}
MOTION_COMMANDS = {"Z", "XY", "TAP", "TAP2", "SWIPE", "PATH1", "RESUME"}


class ArmSession:
    """一条串口会话：open 成功即生成新锚点。"""

    def __init__(self) -> None:
        self.session = uuid.uuid4().hex
        self.serial = None
        # P11 宽限：开串口必然复位 ESP32，Grbl 启动横幅是预期输出而非
        # "会话中途隐藏复位"。宽限在首次成功位置/指令交换后关闭，此后
        # 同会话再出现横幅仍按隐藏复位撤销会话。
        self._boot_banner_grace = True
        try:
            self.serial = self._open_serial()
            # 部分 macOS USB-串口驱动不支持 DTR/RTS 控制，会在设备已成功打开后
            # 返回 errno=22。不能把这种可选线路控制错误误判为串口不可用，否则
            # 调试台重新 CONNECT 会让整台设备持续 offline。串口读写仍由后续
            # 自检验证；支持线路控制的驱动继续保持原有复位行为。
            for name, line_control, value in (
                ("set_dtr", self.serial.setDTR, False),
                ("set_rts", self.serial.setRTS, False),
            ):
                try:
                    line_control(value)
                except Exception as exc:
                    if self._errno(exc) in (22, 25):
                        print(
                            f"串口线路控制不支持，已忽略：{name} errno="
                            f"{self._errno(exc)}",
                            flush=True,
                        )
                        continue
                    raise RuntimeError(
                        f"串口连接失败（阶段 {name}）：{exc}") from exc
            try:
                time.sleep(2.5)  # ESP32 打开即复位，等启动完成
            except Exception as exc:
                raise RuntimeError(
                    f"串口连接失败（阶段 startup_wait）：{exc}") from exc
            try:
                self.serial.reset_input_buffer()
            except Exception as exc:
                raise RuntimeError(
                    f"串口连接失败（阶段 startup_flush）：{exc}") from exc
        except Exception:
            self.close()
            raise
        print(f"串口已连接，会话锚点 {self.session}", flush=True)

    @staticmethod
    def _close_serial(serial_port) -> None:
        if serial_port is None:
            return
        try:
            serial_port.close()
        except Exception:
            pass

    @staticmethod
    def _errno(exc) -> int | None:
        value = getattr(exc, "errno", None)
        if isinstance(value, int):
            return value
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
        text = str(exc).lower()
        if "invalid argument" in text or "errno 22" in text:
            return 22
        return None

    def _open_serial(self):
        """打开串口并保留句柄所有权；仅对打开阶段 EINVAL 做一次重试。"""
        last_error = None
        for attempt in range(1, SERIAL_OPEN_ATTEMPTS + 1):
            serial_port = None
            try:
                # 先构造 closed 的对象，再显式 open，保证 open 失败时仍能回收
                # 已分配的 pyserial 资源，也让所有串口参数在这里可审计。
                serial_port = serial.Serial(
                    port=None,
                    baudrate=BAUD,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    xonxoff=False,
                    rtscts=False,
                    dsrdtr=False,
                    timeout=0.6,
                    write_timeout=1.0,
                )
                serial_port.port = PORT
                serial_port.open()
                return serial_port
            except Exception as exc:
                last_error = exc
                self._close_serial(serial_port)
                errno = self._errno(exc)
                retryable = errno == 22 and attempt < SERIAL_OPEN_ATTEMPTS
                print(
                    f"串口打开失败（阶段 serial_open，第 {attempt}/{SERIAL_OPEN_ATTEMPTS} 次，"
                    f"errno={errno}）：{exc}",
                    flush=True,
                )
                if retryable:
                    time.sleep(SERIAL_OPEN_RETRY_DELAY_S)
                    if not os.path.exists(PORT):
                        break
                    continue
                break
        errno = self._errno(last_error)
        detail = str(last_error) if last_error is not None else "未知错误"
        advice = (
            "；请先确认调试台没有其他连接，再重新插拔 USB；仍失败时再断电重启机械臂"
            if errno == 22 else ""
        )
        raise RuntimeError(
            f"串口连接失败（阶段 serial_open，errno={errno}）：{detail}{advice}"
        ) from last_error

    def close(self) -> None:
        try:
            if self.serial is not None:
                self.serial.close()
        except Exception:
            pass

    def _readline(self, deadline: float) -> bytes:
        # Serial.readline may return a partial line on timeout. Keep fragments
        # together, and enforce the command deadline even while bytes arrive.
        pending = bytearray()
        original_timeout = self.serial.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise serial.serialutil.SerialException("串口控制器无响应或回包不完整")
            try:
                self.serial.timeout = min(original_timeout or remaining, remaining)
                raw = self.serial.read(1)
            except serial.serialutil.SerialException as exc:
                # CH340 偶发“无数据 readiness”异常可以短暂重试，但不能无限吞掉。
                # 持续无响应意味着本物理会话已不可信，必须让 ArmHolder 丢弃它。
                if time.monotonic() >= deadline:
                    raise serial.serialutil.SerialException(
                        f"串口读取持续异常：{exc}"
                    ) from exc
                time.sleep(0.05)
                continue
            finally:
                self.serial.timeout = original_timeout
            if time.monotonic() >= deadline:
                raise serial.serialutil.SerialException("串口控制器无响应")
            if raw:
                pending.extend(raw)
                if len(pending) > 4096:
                    raise serial.serialutil.SerialException("串口控制器回包超长")
                if pending.endswith(b"\n"):
                    return bytes(pending)

    def _send_raw(self, line: str, expect_ok: bool = True) -> list[str]:
        # Serialize only the write with STOP, never its blocking response read.
        with getattr(self, "_write_lock", nullcontext()):
            guard = getattr(self, "_motion_guard", None)
            if guard is not None:
                guard()
            self.serial.write((line + "\n").encode())
        if not expect_ok:
            return []
        out: list[str] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            text = self._readline(deadline).decode(errors="ignore").strip()
            if not text:
                continue
            if self._is_controller_reset_banner(text):
                if getattr(self, "_boot_banner_grace", False):
                    continue  # CONNECT 启动横幅：预期内的开机输出，跳过
                raise self._controller_reset_error(text)
            out.append(text)
            if text == "ok":
                self._boot_banner_grace = False
                return out
            if text.startswith("error"):
                raise RuntimeError(f"控制器拒绝: {text}（{line}）")
        raise serial.serialutil.SerialException(f"等待 ok 超时: {line}")

    def send(self, line: str, expect_ok: bool = True) -> list[str]:
        return self._send_raw(line, expect_ok)

    # 控制器启动/复位横幅（P11/规范 §5.4）：已建立的串口会话内读到即代表
    # 控制器经历了复位（看门狗/掉电），坐标基准丢失。会话字符串没换不能
    # 证明坐标系连续——必须撤销会话，绝不能把复位后的 MPos:0,0,0 当作
    # 旧坐标系中的有效位置。
    @staticmethod
    def _is_controller_reset_banner(text: str) -> bool:
        return (text.startswith("Grbl ")
                or text.startswith("[MSG:controller-ready]"))

    @staticmethod
    def _controller_reset_error(text: str) -> serial.serialutil.SerialException:
        return serial.serialutil.SerialException(
            f"控制器启动/复位提示出现在已建立的串口会话中：{text}"
            "（坐标基准丢失，会话已撤销；请重新连接并按新坐标系复核校准）")

    def mpos(self) -> dict:
        self.serial.write(b"?\n")
        deadline = time.monotonic() + MPOS_RESPONSE_TIMEOUT_S
        while time.monotonic() < deadline:
            text = self._readline(deadline).decode(errors="ignore").strip()
            if self._is_controller_reset_banner(text):
                if getattr(self, "_boot_banner_grace", False):
                    continue  # CONNECT 启动横幅：预期内的开机输出，跳过
                raise self._controller_reset_error(text)
            if (not text.startswith("<") or not text.endswith(">")
                    or text.count("<") != 1 or text.count(">") != 1):
                continue
            parts = text[1:-1].split("|")
            positions = [part[5:] for part in parts if part.startswith("MPos:")]
            if not parts[0] or len(positions) != 1:
                continue
            try:
                values = [float(value) for value in positions[0].split(",")]
            except ValueError:
                continue
            if len(values) != 3 or not all(math.isfinite(value) for value in values):
                continue
            x, y, z = values
            self._boot_banner_grace = False
            return {"x": x, "y": y, "z": z, "state": parts[0]}
        raise serial.serialutil.SerialException("状态查询无响应")

    def idle(self, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            guard = getattr(self, "_motion_guard", None)
            if guard is not None:
                with self._write_lock:
                    guard()
            st = self.mpos()
            if st.get("state") == "Idle":
                return st
            time.sleep(0.3)
        raise serial.serialutil.SerialException("等待 Idle 超时，动作结果未知")


class LeaseManager:
    """运动租约：同一串口同一时刻一个租约；绑定连接，断线/超时自动失效。"""

    def __init__(self, ttl: float = LEASE_TTL_S) -> None:
        self.ttl = ttl
        self.lock = threading.Lock()
        self.lease: dict | None = None

    def _expired(self, now: float | None = None) -> bool:
        if self.lease is None:
            return True
        return (now or time.time()) >= self.lease["expires_at"]

    def _expire_locked(self) -> None:
        if self._expired():
            if self.lease is not None:
                print(f"运动租约超时失效：{self.lease['owner_type']}/{self.lease['owner_id']}", flush=True)
            self.lease = None

    def claim(self, conn_id: int, owner_type: str, owner_id: str, task_id: str = "") -> dict:
        if owner_type not in (LEASE_OWNER_BUSINESS, LEASE_OWNER_DEBUG):
            raise ValueError("owner_type 必须是 business 或 debug")
        with self.lock:
            self._expire_locked()
            if self.lease is not None:
                holder = self.lease
                raise ValueError(
                    f"运动租约被占用：{holder['owner_type']}/{holder['owner_id']}"
                    f"（lease {holder['lease_id'][:8]}，剩余 {max(0, int(holder['expires_at'] - time.time()))}s）"
                    f"；业务租约期间调试台默认只读")
            self.lease = {
                "lease_id": uuid.uuid4().hex,
                "owner_type": owner_type,
                "owner_id": owner_id,
                "task_id": task_id,
                "conn_id": conn_id,
                "expires_at": time.time() + self.ttl,
            }
            print(f"运动租约已授予：{owner_type}/{owner_id}（连接 {conn_id}）", flush=True)
            return self.snapshot_locked()

    def renew(self, conn_id: int) -> dict:
        with self.lock:
            self._expire_locked()
            if self.lease is None or self.lease["conn_id"] != conn_id:
                raise ValueError("本连接未持有运动租约")
            self.lease["expires_at"] = time.time() + self.ttl
            return self.snapshot_locked()

    def release(self, conn_id: int) -> dict:
        with self.lock:
            if self.lease is None or self.lease["conn_id"] != conn_id:
                return {"released": False}
            print(f"运动租约已释放：{self.lease['owner_type']}/{self.lease['owner_id']}", flush=True)
            self.lease = None
            return {"released": True}

    def release_conn(self, conn_id: int) -> None:
        with self.lock:
            if self.lease is not None and self.lease["conn_id"] == conn_id:
                print(f"连接断开，运动租约自动失效：{self.lease['owner_type']}/{self.lease['owner_id']}", flush=True)
                self.lease = None

    def invalidate_all(self, reason: str = "物理会话失效") -> None:
        """Invalidate every lease when the underlying serial session disappears.

        A TCP client can remain connected after the USB/serial device is removed.
        Its lease must not survive that physical-session boundary, otherwise a
        newly connected client can be blocked until the normal TTL expires.
        """
        with self.lock:
            if self.lease is not None:
                print(
                    f"{reason}，运动租约自动失效："
                    f"{self.lease['owner_type']}/{self.lease['owner_id']}",
                    flush=True,
                )
            self.lease = None

    def require(self, conn_id: int) -> dict:
        with self.lock:
            self._expire_locked()
            if self.lease is None:
                raise ValueError("普通运动需要运动租约：先 CLAIM（普通运动/点击序列必须持租约）")
            if self.lease["conn_id"] != conn_id:
                raise ValueError("运动租约由其他连接持有")
            self.lease["expires_at"] = time.time() + self.ttl  # 持有期间的连续运动自动续期
            return dict(self.lease)

    def begin(self, conn_id: int) -> str:
        lease = self.require(conn_id)  # 锁内校验后即释放，不嵌套持锁
        tx_id = uuid.uuid4().hex
        print(f"动作事务开始：tx {tx_id[:8]}（lease {lease['lease_id'][:8]}）", flush=True)
        return tx_id

    def snapshot_locked(self) -> dict | None:
        if self.lease is None:
            return None
        return {k: v for k, v in self.lease.items() if k != "conn_id"}

    def snapshot(self) -> dict | None:
        with self.lock:
            self._expire_locked()
            return self.snapshot_locked()


class ArmHolder:
    """串口持有者：在位监听（不自动连接）；连接/断开由 CONNECT 指令人工触发。

    多个 TCP 客户端可同时连入（每个连接一个线程），串口指令全程持 cmd_lock 串行；
    普通运动还必须通过租约校验（校验在同一把锁内完成，保证原子性）。
    """

    def __init__(self) -> None:
        self.arm: ArmSession | None = None
        self.device_present = os.path.exists(PORT)
        self.lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self.estop_locked = False
        self.stop_generation = 0
        self.transactions: dict[int, str] = {}  # conn_id -> tx_id
        self.active_commands: dict[int, tuple[threading.Event, bool]] = {}
        self.leases = LeaseManager()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        while True:
            present = os.path.exists(PORT)
            if present != self.device_present:
                print(f"设备在位变化：{'插入' if present else '拔出'}", flush=True)
            self.device_present = present
            arm = self.arm
            if arm is not None and not present:
                # 拔线感知：设备节点消失，立即丢弃会话（锚点随之失效）
                self._drop(arm, reason="串口设备已拔出")
            time.sleep(WATCH_INTERVAL_S)

    def _drop(self, arm: ArmSession, reason: str) -> None:
        # Watcher, manual disconnect and command-error paths all serialize
        # teardown with motion/CONNECT so no command can use a dead session.
        with self.cmd_lock:
            self._drop_locked(arm, reason)

    def _drop_locked(self, arm: ArmSession, reason: str) -> None:
        """Drop a session while ``cmd_lock`` is already held."""
        with self.lock:
            if self.arm is not arm:
                return
            self.arm = None
            self.leases.invalidate_all(reason)
            self.transactions.clear()
            arm.close()
        print(f"串口会话已丢弃：{reason}（重连后将生成新锚点）", flush=True)

    def connect(self) -> dict:
        with self.cmd_lock:  # 全程持锁：两路同时 CONNECT 不允许交错开串口
            if self.arm is not None:
                return {"connected": True, "deviceId": DEVICE_ID,
                        "session": self.arm.session, "already": True}
            try:
                arm = ArmSession()
            except Exception as exc:
                raise RuntimeError(f"连接失败：{exc}") from exc
            with self.lock:
                self.arm = arm
            return {"connected": True, "deviceId": DEVICE_ID, "session": arm.session}

    def disconnect(self) -> dict:
        with self.cmd_lock:
            arm = self.arm
            if arm is not None:
                self._drop_locked(arm, reason="人工断开")
        return {"disconnected": True, "deviceId": DEVICE_ID}

    def use(self) -> ArmSession:
        arm = self.arm
        if arm is None:
            raise RuntimeError("串口未连接：请先在调试台点击「连接设备」")
        return arm

    def _require_motion_allowed(self, conn_id: int) -> None:
        """普通运动前置校验：急停锁定 + 运动租约（在 cmd_lock 内调用，保证原子）。"""
        with self.lock:
            if self.estop_locked:
                raise ValueError("急停锁定中（daemon 级）：人工 RESET_ESTOP 复位前拒绝一切普通运动")
        self.leases.require(conn_id)

    def stop_now(self) -> dict:
        """发出 GRBL 实时停止，不等待被卡住的普通串口命令锁。

        ``!`` 是控制器的异步实时命令。位置查询或普通运动卡住时，若仍走
        ``cmd_lock`` 会把停止也排在故障命令后面，违背急停优先级。
        """
        with self.lock:
            return self._stop_locked()

    def _stop_locked(self) -> dict:
        self.stop_generation = getattr(self, "stop_generation", 0) + 1
        arm = self.arm
        if arm is None:
            return {"stopped": False, "connected": False}
        try:
            data = b"!\n"
            if arm.serial.write(data) != len(data):
                raise OSError("停止指令写入不完整")
            return {"stopped": True, "connected": True}
        except (serial.serialutil.SerialException, OSError) as exc:
            raise RuntimeError(f"停止指令下发失败：{exc}") from exc

    def begin_client_command(self, conn_id: int, *, motion: bool = False) -> threading.Event:
        event = threading.Event()
        with self.lock:
            self.active_commands[conn_id] = (event, motion)
        return event

    def end_client_command(self, conn_id: int, event: threading.Event) -> None:
        with self.lock:
            active = self.active_commands.get(conn_id)
            if active is not None and active[0] is event:
                self.active_commands.pop(conn_id, None)

    def cancel_client(self, conn_id: int) -> dict:
        """Cancel an in-flight request after its TCP client disappears."""
        with self.lock:
            active = self.active_commands.get(conn_id)
            if active is None or active[0].is_set():
                return {"stopped": False, "connected": self.arm is not None}
            event, motion = active
            event.set()
            # A read-only observer disappearing must not stop another owner.
            with self.leases.lock:
                lease = self.leases.lease
                owns_motion = lease is not None and lease["conn_id"] == conn_id
            if not motion or not owns_motion:
                return {"stopped": False, "connected": self.arm is not None}
            self.estop_locked = True
            try:
                return self._stop_locked()
            except RuntimeError as exc:
                return {
                    "stopped": False,
                    "connected": self.arm is not None,
                    "outcome_hint": "unknown",
                    "error": str(exc),
                }

    def run(self, fn, conn_id: int | None = None, motion: bool = False,
            cancel_event: threading.Event | None = None):
        """执行 fn(arm)：全程持 cmd_lock 串行；motion=True 时先做租约/锁定校验。"""
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("请求已取消")
        arm = self.use()
        generation = getattr(self, "stop_generation", 0)
        if not self.cmd_lock.acquire(timeout=COMMAND_LOCK_TIMEOUT_S):
            raise RuntimeError("daemon 正忙：上一条串口指令尚未结束，本次指令未下发")
        entered_motion = False
        try:
            try:
                if self.arm is not arm:
                    raise RuntimeError("串口会话已重置：请重新连接设备")
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("请求已取消")
                if motion:
                    assert conn_id is not None
                    self._require_motion_allowed(conn_id)
                    def guard():
                        if (cancel_event is not None and cancel_event.is_set()
                                or self.estop_locked
                                or self.stop_generation != generation):
                            raise RuntimeError("动作已被停止，结果未知；禁止继续剩余指令")
                        if self.arm is not arm:
                            raise RuntimeError("串口会话已重置")
                        self.leases.require(conn_id)
                    arm._write_lock = self.lock
                    arm._motion_guard = guard
                    with self.lock:
                        guard()
                    entered_motion = True
                result = fn(arm)
                if motion:
                    with self.lock:
                        guard()
                return result
            except (serial.serialutil.SerialException, OSError) as exc:
                if entered_motion:
                    self._latch_failed_motion(arm, generation)
                self._drop_locked(arm, reason=f"串口异常：{exc}")
                raise RuntimeError(f"串口异常，会话已重置：{exc}") from exc
            except Exception:
                if entered_motion:
                    self._latch_failed_motion(arm, generation)
                raise
            finally:
                arm._motion_guard = None
        finally:
            self.cmd_lock.release()

    def _latch_failed_motion(self, arm, generation: int) -> None:
        """A failed macro may leave queued motion or contact; never append recovery."""
        with self.lock:
            if self.arm is not arm:
                return
            self.estop_locked = True
            if getattr(self, "stop_generation", 0) != generation:
                return
            try:
                self._stop_locked()
            except RuntimeError:
                # Keep the latch even when STOP delivery cannot be confirmed.
                pass

    def status(self) -> dict:
        with self.lock:
            arm = self.arm
            return {"deviceId": DEVICE_ID,
                    "connected": arm is not None,
                    "devicePresent": self.device_present,
                    "session": arm.session if arm else None,
                    "estopLocked": self.estop_locked,
                    "motionFeatures": [DOUBLE_TAP_FEATURE, DOUBLE_TAP_SHORT_LIFT_FEATURE,
                                       DOUBLE_TAP_FAST_FEATURE, SWIPE_FEATURE,
                                       PATH1_FEATURE],
                    "lease": self.leases.snapshot()}


HOLDER: "ArmHolder | None" = None


def _check_request_active(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("请求已取消")


def _parse_feed(parts: list[str], default: float, *, axis: str, start: int) -> float:
    feed = default
    seen = False
    for extra in parts[start:]:
        if extra.upper().startswith("F"):
            feed = float(extra[1:])
            seen = True
        else:
            raise ValueError(f"无法识别的参数：{extra}")
    if seen:
        check_feed(feed, axis=axis)
    return feed


def handle(line: str, conn_id: int,
           cancel_event: threading.Event | None = None) -> str:
    parts = line.split()
    cmd = parts[0].upper()
    holder = HOLDER
    assert holder is not None
    _check_request_active(cancel_event)

    # ---- 连接管理（不受租约/锁定限制） ----
    if cmd == "CONNECT":
        return json.dumps(holder.connect(), ensure_ascii=False)
    if cmd == "DISCONNECT":
        return json.dumps(holder.disconnect(), ensure_ascii=False)
    if cmd == "STATUS":
        return json.dumps(holder.status(), ensure_ascii=False)

    # ---- 急停（高优先级，不受租约阻塞） ----
    if cmd == "STOP":
        return json.dumps(holder.stop_now(), ensure_ascii=False)
    if cmd == "ESTOP_LOCK":
        reason = " ".join(parts[1:]) or "人工急停"
        with holder.lock:
            holder.estop_locked = True
            try:
                stopped = holder._stop_locked()
            except RuntimeError as exc:
                stopped = {"stopped": False, "outcome_hint": "unknown", "error": str(exc)}
        print(f"急停锁定（daemon 级）：{reason}", flush=True)
        return json.dumps({"estop_locked": True, "reason": reason, **stopped}, ensure_ascii=False)
    if cmd == "RESET_ESTOP":
        if len(parts) < 2:
            raise ValueError("RESET_ESTOP 需要操作者标识（人工复位留痕）")
        operator = " ".join(parts[1:])
        # Never queue an unlock behind a motion that can outlive the caller.
        if not holder.cmd_lock.acquire(blocking=False):
            raise RuntimeError("daemon 正忙：急停未复位，请人工确认后重新操作")
        try:
            if not holder.lock.acquire(blocking=False):
                raise RuntimeError("daemon 正忙：急停未复位，请人工确认后重新操作")
            try:
                _check_request_active(cancel_event)
                holder.estop_locked = False
            finally:
                holder.lock.release()
        finally:
            holder.cmd_lock.release()
        print(f"急停锁定已人工复位：{operator}", flush=True)
        return json.dumps({"estop_locked": False, "reset_by": operator}, ensure_ascii=False)
    if cmd == "RESUME":
        def do_resume(arm: ArmSession) -> str:
            arm.send("~", expect_ok=False)
            time.sleep(0.3)
            return '{"resumed": true}'
        return holder.run(do_resume, conn_id, motion=True, cancel_event=cancel_event)

    # ---- 运动租约管理 ----
    if cmd == "CLAIM":
        if len(parts) < 3:
            raise ValueError("CLAIM 需要 owner_type 与 owner_id")
        with holder.lock:
            if holder.estop_locked:
                raise ValueError("急停锁定中（daemon 级）：RESET_ESTOP 人工复位前拒绝申请运动租约")
            holder.use()
            lease = holder.leases.claim(conn_id, parts[1].lower(), parts[2],
                                        " ".join(parts[3:]) if len(parts) > 3 else "")
        return json.dumps({"claimed": True, **lease}, ensure_ascii=False)
    if cmd == "RENEW":
        lease = holder.leases.renew(conn_id)
        return json.dumps({"renewed": True, **lease}, ensure_ascii=False)
    if cmd == "RELEASE":
        result = holder.leases.release(conn_id)
        holder.transactions.pop(conn_id, None)
        return json.dumps(result, ensure_ascii=False)
    if cmd == "BEGIN_MOTION":
        holder.leases.require(conn_id)
        if conn_id in holder.transactions:
            raise ValueError("本连接已有打开的动作事务")
        holder.transactions[conn_id] = holder.leases.begin(conn_id)
        return json.dumps({"tx": holder.transactions[conn_id]}, ensure_ascii=False)
    if cmd == "END_MOTION":
        holder.leases.require(conn_id)
        tx = holder.transactions.pop(conn_id, None)
        if tx is None:
            raise ValueError("本连接没有打开的动作事务")
        return json.dumps({"ended": tx}, ensure_ascii=False)

    # ---- 状态查询 ----
    if cmd == "MPOS":
        return holder.run(lambda arm: json.dumps(arm.mpos(), ensure_ascii=False),
                          conn_id, cancel_event=cancel_event)

    # ---- 普通运动（需租约 + 非锁定；cmd_lock 内先校验再执行） ----
    if cmd == "Z":
        mm = float(parts[1])
        feed = _parse_feed(parts, DEFAULT_Z_FEED, axis="Z", start=2)
        check_stroke(mm, axis="Z")

        def do_z(arm: ArmSession) -> str:
            arm.send("G91")
            arm.send(f"G1 Z{mm:.3f} F{feed:g}")
            arm.send("G90")
            return json.dumps(arm.idle(), ensure_ascii=False)
        return holder.run(do_z, conn_id, motion=True, cancel_event=cancel_event)

    if cmd == "XY":
        dx, dy = float(parts[1]), float(parts[2])
        feed = _parse_feed(parts, DEFAULT_XY_FEED, axis="X", start=3)
        check_stroke(dx, axis="X")
        check_stroke(dy, axis="Y")

        def do_xy(arm: ArmSession) -> str:
            arm.send("G91")
            arm.send(f"G1 X{dx:.3f} Y{dy:.3f} F{feed:g}")
            arm.send("G90")
            return json.dumps(arm.idle(), ensure_ascii=False)
        return holder.run(do_xy, conn_id, motion=True, cancel_event=cancel_event)

    if cmd == "SWIPE":
        if len(parts) != 6:
            raise ValueError("SWIPE 需要 dx dy depth F<滑动进给> Z<下压进给>")
        dx, dy, depth = map(float, parts[1:4])
        options = {}
        for extra in parts[4:]:
            head = extra[0].upper()
            if head not in {"F", "Z"} or head in options:
                raise ValueError(f"SWIPE 参数无效或重复：{extra}")
            options[head] = float(extra[1:])
        if set(options) != {"F", "Z"}:
            raise ValueError("SWIPE 需要 F<滑动进给> 与 Z<下压进给>")
        feed, tap_feed = options["F"], options["Z"]
        check_swipe(dx, dy, depth, feed, tap_feed)

        def do_swipe(arm: ArmSession) -> str:
            arm.send("G91")
            arm.send(f"G1 Z{depth:.3f} F{tap_feed:g}")
            arm.send(f"G1 X{dx:.3f} Y{dy:.3f} F{feed:g}")
            arm.send(f"G1 Z{-depth:.3f} F{tap_feed:g}")
            arm.send("G90")
            return json.dumps(arm.idle(), ensure_ascii=False)
        return holder.run(do_swipe, conn_id, motion=True, cancel_event=cancel_event)

    if cmd == "PATH1":
        # 连续路径宏（path.v1，规范第 6.5/22.9 节）：入口完成全部预验证后
        # 才开始运动；JSON 解析不是 split/正则拼接，G-code 仅由已验证数值生成。
        rest = line[6:]
        if len((line + "\n").encode("utf-8")) > PATH1_LINE_MAX_BYTES:
            raise ValueError(f"PATH1 整行超过 {PATH1_LINE_MAX_BYTES} 字节上限")
        try:
            payload = json.loads(rest, object_pairs_hook=strict_object_pairs,
                                 parse_constant=reject_json_constant)
        except ValueError as exc:
            raise ValueError(f"PATH1 JSON 解析失败：{exc}") from exc
        path = validate_path1_payload(payload)
        depth, z_feed, segments = path["depth"], path["z_feed"], path["segments"]

        def do_path(arm: ArmSession) -> str:
            arm.send("G91")
            for segment in segments:
                op = segment["op"]
                if op == "move":
                    arm.send(f"G1 X{segment['dx']:.3f} Y{segment['dy']:.3f} "
                             f"F{segment['feed']:g}")
                elif op == "hold":
                    arm.send(f"G4 P{segment['ms'] / 1000:.3f}")
                elif op == "down":
                    arm.send(f"G1 Z{depth:.3f} F{z_feed:g}")
                else:  # up：抬回宏进入前的 Z（相对模式，净位移为零）
                    arm.send(f"G1 Z{-depth:.3f} F{z_feed:g}")
            arm.send("G90")
            return json.dumps({**arm.idle(), "path_completed": True,
                               "segments_completed": len(segments),
                               "contact_released": True}, ensure_ascii=False)
        return holder.run(do_path, conn_id, motion=True, cancel_event=cancel_event)

    if cmd in {"TAP", "TAP2"}:
        depth = float(parts[1])
        feed, dwell = DEFAULT_TAP_FEED, 0.0
        interval = 0.12
        repeat_lift = depth
        seen = set()
        for extra in parts[2:]:
            head = extra[0].upper()
            if cmd == "TAP2" and head in seen:
                raise ValueError(f"TAP2 参数重复：{head}")
            seen.add(head)
            if head == "F":
                feed = float(extra[1:])
            elif head == "P":
                dwell = float(extra[1:])
            elif head == "I" and cmd == "TAP2":
                interval = float(extra[1:])
            elif head == "R" and cmd == "TAP2":
                repeat_lift = float(extra[1:])
            else:
                raise ValueError(f"{cmd} 参数不支持：{extra}")
        check_tap(depth, dwell)
        if cmd == "TAP2":
            check_double_tap_feed(feed)
        else:
            check_feed(feed, axis="Z")
        if cmd == "TAP2":
            check_double_tap_interval(interval)
            if "R" in seen and (not math.isfinite(repeat_lift)
                                or not 0 < repeat_lift <= min(depth, DOUBLE_TAP_SHORT_LIFT_MAX_MM)):
                raise ValueError("双击中途抬起必须为不超过点击深度及 1mm 的正数")

        def do_tap(arm: ArmSession) -> str:
            arm.send("G91")
            for index in range(2 if cmd == "TAP2" else 1):
                if index:
                    # The controller starts the pause after the prior release.
                    arm.send(f"G4 P{interval:g}")
                press = repeat_lift if index else depth
                release = repeat_lift if cmd == "TAP2" and index == 0 else depth
                arm.send(f"G1 Z{press:.3f} F{feed:g}")
                if dwell > 0:
                    arm.send(f"G4 P{dwell:g}")
                arm.send(f"G1 Z{-release:.3f} F{feed:g}")
            arm.send("G90")
            return json.dumps(arm.idle(), ensure_ascii=False)
        return holder.run(do_tap, conn_id, motion=True, cancel_event=cancel_event)

    return f"未知指令: {line}"


def _watch_client_disconnect(conn, conn_id: int, stop_event: threading.Event) -> None:
    while not stop_event.wait(0.1):
        try:
            readable, _, _ = select.select([conn], [], [], 0)
            if not readable:
                continue
            data = conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
            if data == b"":
                HOLDER.cancel_client(conn_id)
                # Keep watching until the handler exits: EOF may have arrived
                # between reading a request and registering its cancellation.
                continue
            # Bytes are a pipelined next request; leave them for makefile().
        except (BlockingIOError, InterruptedError):
            continue
        except (OSError, ValueError):
            HOLDER.cancel_client(conn_id)
            return


def acquire_client_slot(normal_slots, emergency_slots):
    if normal_slots.acquire(blocking=False):
        return normal_slots
    if emergency_slots.acquire(blocking=False):
        return emergency_slots
    return None


def serve_client(conn, conn_id: int, slot=None, *, emergency_only: bool = False) -> None:
    """单连接处理：每个客户端一个线程；串口指令由 HOLDER 内部锁串行。"""
    f = None
    disconnect_watch_stop = threading.Event()
    disconnect_watch = threading.Thread(
        target=_watch_client_disconnect,
        args=(conn, conn_id, disconnect_watch_stop),
        daemon=True,
    )
    try:
        # A client that connects and never sends a line must not consume a
        # daemon thread forever. The lease TTL is shorter than this idle cap,
        # so an abandoned connection cannot retain motion ownership forever.
        conn.settimeout(EMERGENCY_CLIENT_TIMEOUT_S if emergency_only else CLIENT_IDLE_TIMEOUT_S)
        emergency_deadline = time.monotonic() + EMERGENCY_CLIENT_TIMEOUT_S
        conn.sendall(f"arm-daemon3 ready device={DEVICE_ID}\n".encode())
        f = conn.makefile("rwb")
        try:
            disconnect_watch.start()
        except RuntimeError:
            # Without disconnect monitoring, accepting motion is unsafe.
            return
        while True:
            if emergency_only:
                remaining = emergency_deadline - time.monotonic()
                if remaining <= 0:
                    break
                conn.settimeout(remaining)
            line = f.readline(65536)
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            text = line.decode(errors="ignore").strip()
            if not text:
                continue
            if text.upper() == "BYE":
                break
            command = text.split()[0].upper()
            if emergency_only and command not in EMERGENCY_COMMANDS:
                conn.sendall("ERROR: 普通连接已满，应急连接仅允许状态查询和停止\n".encode())
                break
            cancel_event = HOLDER.begin_client_command(
                conn_id, motion=command in MOTION_COMMANDS)
            try:
                resp = handle(text, conn_id, cancel_event=cancel_event)
            except (ValueError, RuntimeError) as exc:
                resp = f"ERROR: {exc}"
            except Exception as exc:
                resp = f"ERROR: {exc}"
            finally:
                HOLDER.end_client_command(conn_id, cancel_event)
            conn.sendall((resp + "\n").encode())
    except OSError:
        pass
    finally:
        disconnect_watch_stop.set()
        HOLDER.cancel_client(conn_id)
        HOLDER.leases.release_conn(conn_id)
        HOLDER.transactions.pop(conn_id, None)
        try:
            if f is not None:
                f.close()
            conn.close()
        except OSError:
            pass
        if slot is not None:
            slot.release()
        if disconnect_watch.ident is not None:
            disconnect_watch.join(timeout=0.5)


def start_client_thread(conn, conn_id: int, slot, *, emergency_only: bool = False) -> bool:
    """启动客户端线程；线程资源不足时立即关闭连接并归还容量。"""
    worker = threading.Thread(
        target=serve_client,
        args=(conn, conn_id, slot),
        kwargs={"emergency_only": emergency_only},
        daemon=True,
    )
    try:
        worker.start()
    except RuntimeError:
        try:
            conn.close()
        except OSError:
            pass
        if slot is not None:
            slot.release()
        return False
    return True


def main() -> None:
    global HOLDER
    HOLDER = ArmHolder()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(MAX_CLIENTS + EMERGENCY_RESERVED_CLIENTS)
    normal_slots = threading.BoundedSemaphore(MAX_CLIENTS)
    emergency_slots = threading.BoundedSemaphore(EMERGENCY_RESERVED_CLIENTS)
    print(f"arm-daemon3 就绪：{DEVICE_ID} @ {LISTEN[0]}:{LISTEN[1]}"
          f"（监听 {PORT} 在位，等待人工 CONNECT；运动需 CLAIM 租约）", flush=True)
    conn_seq = iter(range(1, 10 ** 9))
    while True:
        conn, _ = srv.accept()
        slot = acquire_client_slot(normal_slots, emergency_slots)
        if slot is None:
            # Refuse excess sockets before starting a thread. This prevents a
            # burst of abandoned TCP clients from exhausting the process.
            try:
                conn.settimeout(EMERGENCY_CLIENT_TIMEOUT_S)
                conn.sendall("ERROR: daemon 客户端连接数已达上限\n".encode())
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            continue
        start_client_thread(
            conn, next(conn_seq), slot,
            emergency_only=slot is emergency_slots,
        )


if __name__ == "__main__":
    main()
