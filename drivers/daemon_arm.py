"""daemon 驱动桥：以 arm_daemon3 为后端的 ArmDriver 实现（业务运行时唯一通道）。

设计与职责边界见 docs/daemon驱动桥设计与职责边界.md（v1.2）。要点：
- 串口由对应 arm_daemon 实例独占；本驱动只讲 daemon 协议，绝不直连串口。
- 业务 arm.click 只接受 target_id（设备级目标档案）；裸位移属于调试接口，
  不得由普通插件提交（驱动在此拒绝）。
- 目标解析：画面坐标 → 当前设备标定（robot = A·camera + b）→ 机械坐标；
  标定缺失或会话锚点不匹配（机械臂重启/串口重连）→ driver_uncalibrated，
  除 stop/estop/read_state 外一切运动拒绝。
- 普通运动必须持有 daemon 运动租约；一次点击从 BEGIN_MOTION 到 END_MOTION
  使用同一租约与事务 ID；STOP/ESTOP 不受租约阻塞。
- ESTOP 形成 daemon 级锁定（所有客户端的普通运动都被拒），复位必须人工
  reset_estop(operator)。
- go_home = 停止并保持当前位置：设备无限位器，v1 不执行自动回零、不猜测归位；
  聊天结束的停靠由插件显式 move_target("ime_delete_key")。
- ok/MPos 只代表控制器接受；配置 motion_verifier 时逐段摄像头验证，未验证时
  outcome_hint="unknown"，不得当成功（与 RealArmDriver 语义一致）。
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import socket
import threading
import time

import numpy as np

from drivers import target_archive
from drivers import calibration_bundle
from drivers import ui_targets
from drivers import screen_gestures
from drivers import screen_pose
from drivers import click_points
from drivers.arm import ArmDriver, DriverError
from drivers.daemon_protocol import (
    DEFAULT_DAEMON_ADDR,
    DEFAULT_TARGET_MODES,
    CALIBRATED_XY_FEED,
    DOUBLE_TAP_FEATURE,
    DOUBLE_TAP_RELEASE_MM,
    DOUBLE_TAP_DEFAULT_INTERVAL_MS,
    DOUBLE_TAP_FAST_FEATURE,
    DOUBLE_TAP_FEED_MAX,
    DYNAMIC_TARGET_DISPLAY_NAMES,
    DOUBLE_TAP_INTERVAL_MAX_MS,
    DOUBLE_TAP_INTERVAL_MIN_MS,
    HOVER_SEGMENT_MAX_MM,
    LEASE_OWNER_BUSINESS,
    PATH1_FEATURE,
    SWIPE_DURATION_MAX_MS,
    SWIPE_DURATION_MIN_MS,
    SWIPE_FEED_MAX,
    SWIPE_FEATURE,
    SWIPE_TOTAL_MAX_MM,
    TARGET_DISPLAY_NAMES,
    TARGET_MODE_TAP,
    TRAVEL_TOTAL_MAX_MM,
    XY_STROKE_MAX,
    check_feed,
    check_tap,
    split_segments,
    validate_path1_payload,
)


def json_loads(text: str) -> dict:
    try:
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("响应必须是对象")
        return result
    except (ValueError, TypeError) as exc:
        raise DriverError("driver_disconnected", "daemon 响应无效，结果未知，禁止重放动作") from exc


def _finite(value, label: str) -> float:
    try:
        number = float(value)
        if isinstance(value, bool) or not math.isfinite(number):
            raise ValueError()
        return number
    except (ValueError, TypeError, OverflowError) as exc:
        raise DriverError("driver_limit_exceeded", f"{label} 必须是有限数值") from exc


CONNECT_TIMEOUT_S = 8.0
CONTROL_TIMEOUT_S = 3.0
CLEANUP_TIMEOUT_S = 2.0
GESTURE_POSITION_TOLERANCE_MM = 0.05
_LAYOUT_UNSET = object()

# S3（启用前剩余修复执行单 §4-S3，2026-10-08）：动态视觉目标“不查固定目标表”
# 与“仍须核对原任务上下文”分离。逐一核对的产品调用点（S3-04）：
#   conversation_item + camera_ocr：wx-wechat-adapter 打开会话 / 监督 conversation_opener
#   mark_read_row    + camera_ocr：快速恢复（标读）进入点击
#   gesture_point    + camera_vision：电机调试台手势双击
# 其余组合（含 vision_back_button 全部组合）当前无产品生产者，明确拒绝，不膨胀。
DYNAMIC_VISUAL_SOURCES = {"camera_ocr", "camera_vision", "vision_back_button"}
DYNAMIC_VISUAL_TARGET_IDS = {"gesture_point", "conversation_item", "mark_read_row"}
DYNAMIC_COMBOS_IN_USE = {
    ("conversation_item", "camera_ocr"),
    ("mark_read_row", "camera_ocr"),
    ("gesture_point", "camera_vision"),
}
# 任务快照固化的 target_ref 身份字段：解析前抓取，走位后/接触前复核未被改写。
DYNAMIC_REF_IDENTITY_KEYS = (
    "source", "cameraX", "cameraY", "bbox", "cameraPolygon", "frameW", "frameH", "frameSeq",
    "frameSha256", "coordinateVersion", "template_ref", "adaptation_ref",
    "installation_region_ref", "locationId", "sourceCapturedAt", "sourceReceivedAt",
)


class DaemonTCPTransport:
    """到 arm_daemon 的常驻 TCP 连接：一行指令、一行响应。"""

    def __init__(self, addr: str = DEFAULT_DAEMON_ADDR, timeout: float = 180.0):
        host, _, port = addr.partition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port or 8795)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._received = bytearray()
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._generation = 0
        self._welcome = ""

    def open(self) -> str:
        budget = min(CONNECT_TIMEOUT_S, self.timeout)
        deadline = time.monotonic() + budget
        if not self._lock.acquire(timeout=budget):
            raise DriverError("driver_connect_failed", "daemon 连接忙，拒绝替换活动连接")
        sock = None
        try:
            with self._state_lock:
                if self._sock is not None:
                    return self._welcome
                generation = self._generation
            sock = socket.create_connection(
                (self.host, self.port), timeout=self._remaining(deadline))
            received = bytearray()
            with self._state_lock:
                if generation != self._generation:
                    raise OSError("连接已取消")
                self._sock = sock
            welcome = self._readline(sock, received, deadline, generation=generation)
            if welcome.split()[:2] != ["arm-daemon3", "ready"]:
                raise OSError(f"daemon 欢迎行异常：{welcome!r}")
            with self._state_lock:
                if generation != self._generation:
                    raise OSError("连接已取消")
                self._sock = sock
                self._received = received
                self._welcome = welcome
            return welcome
        except (OSError, UnicodeError) as exc:
            self._close_socket(sock)
            raise DriverError("driver_connect_failed",
                              f"无法连接 arm_daemon（{self.host}:{self.port}）：{exc}") from exc
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._state_lock:
            self._generation += 1
            sock, self._sock = self._sock, None
        self._close_socket(sock)

    def _close_socket(self, sock) -> None:
        if sock is not None:
            with self._state_lock:
                if self._sock is sock:
                    self._sock = None
            # No stream close/flush or command lock: shutdown must interrupt
            # a blocked read immediately, even during cancellation.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("daemon 响应超时")
        return remaining

    def _readline(self, sock, received: bytearray, deadline: float,
                  *, generation: int | None = None) -> str:
        while True:
            if generation is not None:
                with self._state_lock:
                    if generation != self._generation or self._sock is not sock:
                        raise OSError("daemon 连接已取消")
            newline = received.find(b"\n")
            if newline >= 0:
                raw = bytes(received[:newline])
                del received[:newline + 1]
                return raw.decode("utf-8").strip()
            if len(received) >= 65536:
                raise OSError("daemon 响应行过长")
            # A concurrent close does not reliably wake a long socket wait.
            # Poll cancellation without changing the command's total deadline.
            sock.settimeout(min(0.1, self._remaining(deadline)))
            try:
                chunk = sock.recv(min(4096, 65536 - len(received)))
            except socket.timeout:
                continue
            if not chunk:
                raise OSError("daemon 连接中断（响应不完整）")
            received.extend(chunk)

    def is_open(self) -> bool:
        return self._sock is not None

    def cmd(self, line: str, *, timeout: float | None = None,
            input_claim: dict | None = None) -> str:
        if not isinstance(line, str) or not line.strip() or "\n" in line or "\r" in line:
            raise DriverError("driver_policy_rejected", "daemon 指令必须是单行，禁止指令注入")
        budget = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        with self._state_lock:
            generation = self._generation
        if not self._lock.acquire(timeout=budget):
            raise DriverError("driver_timeout", "daemon 连接忙，指令未下发")
        with self._state_lock:
            sock = self._sock
            received = self._received
            current_generation = self._generation
        try:
            if sock is None or generation != current_generation:
                raise DriverError("driver_disconnected", "daemon 连接已中断")
            if input_claim:
                # §4.8.4：传输锁内、连接确认后、sendall 前同步最后认领；
                # 认领不跨 sendall/等回包，失败=零动作未下发。
                # R3-02：最终几何门在同一位置、认领之前执行（先几何后认领，
                # 门失败同样零动作）。
                gate = input_claim.get("geometry_gate")
                if callable(gate):
                    gate()
                callback = input_claim.get("callback")
                if callable(callback):
                    callback()
            try:
                sock.settimeout(self._remaining(deadline))
                sock.sendall((line + "\n").encode())
                text = self._readline(sock, received, deadline, generation=generation)
            except (OSError, UnicodeError) as exc:
                self._close_socket(sock)
                raise DriverError("driver_disconnected",
                                  f"daemon 通信失败：{exc}；结果未知，禁止重放动作") from exc
        finally:
            self._lock.release()
        if text.startswith("ERROR:"):
            raise DriverError("driver_motion_failed", text[len("ERROR:"):])
        return text


class DaemonArmDriver(ArmDriver):
    DRIVER_VERSION = "daemon-arm-bridge-1.2.0"
    SUPPORTED = {
        "arm.home", "arm.move_target", "arm.click", "arm.double_tap",
        "arm.swipe", "arm.stop", "arm.estop", "arm.wake_screen",
    }

    def __init__(
        self,
        device_id: str,
        *,
        daemon_addr: str = DEFAULT_DAEMON_ADDR,
        data_root=None,
        owner_type: str = LEASE_OWNER_BUSINESS,
        owner_id: str = "",
        task_id: str = "",
    ):
        self.device_id = device_id
        self.daemon_addr = daemon_addr
        self.data_root = data_root
        self.owner_type = owner_type
        self.owner_id = owner_id or f"runtime:{device_id}"
        self.task_id = task_id
        self._transport: DaemonTCPTransport | None = None
        self._connected = False
        self._estop_latched = False
        self._daemon_session: str | None = None
        self._motion_lock = threading.Lock()
        self._safety_lock = threading.Lock()
        self._stop_generation = 0
        self.action_log: list[dict] = []
        self.motion_verifier = None  # 摄像头运动验证器（可选，devices 装配时挂载）
        self.gesture_motion_verifier = None
        self.live_calibration_gate = None  # 业务动作前由 DeviceManager 注入实时绑定门禁
        self.screen_wake_gate = None
        self._layout_provider = None
        self._text_target_verifier = None

    def attach_text_target_verifier(self, verifier) -> None:
        self._text_target_verifier = verifier

    def attach_layout_provider(self, provider) -> None:
        self._layout_provider = provider

    # ---------- 基础 ----------
    @property
    def driver_version(self) -> str:
        return self.DRIVER_VERSION

    def supports(self, capability_id: str) -> bool:
        return capability_id in self.SUPPORTED

    @property
    def connected(self) -> bool:
        return self._connected and (self._transport.is_open() if self._transport else False)

    def _check_ready(self) -> None:
        if self._estop_latched:
            raise DriverError("driver_estop_latched", "软件急停已锁定：人工 reset_estop 复位前拒绝一切运动")
        if not self.connected:
            raise DriverError("driver_disconnected", "daemon 连接未建立")

    def _cmd(self, line: str, *, timeout: float | None = None,
             input_claim: dict | None = None) -> str:
        transport = self._transport
        if transport is None:
            raise DriverError("driver_disconnected", "daemon 连接未初始化")
        try:
            if input_claim is not None and \
                    "input_claim" in inspect.signature(transport.cmd).parameters:
                # 测试替身/旧传输可能没有 input_claim 形参：此时边界认领由
                # execute_command 兜底（连接确认后、同步调用内），不越过 sendall。
                resp = transport.cmd(line, timeout=timeout, input_claim=input_claim)
            else:
                resp = transport.cmd(line, timeout=timeout)
        except DriverError:
            if self._transport is transport and not transport.is_open():
                self._connected = False
            raise
        self.action_log.append({"cmd": line, "resp": resp})
        return resp

    def _status(self, *, timeout: float | None = None) -> dict:
        status = self._json_cmd("STATUS", timeout=timeout)
        try:
            self._check_identity(status)
        except DriverError:
            self._transport.close()
            self._connected = False
            raise
        return status

    def _mpos(self) -> dict:
        return self._position(self._json_cmd("MPOS"))

    def _json_cmd(self, line: str, *, timeout: float | None = None,
                  input_claim: dict | None = None) -> dict:
        transport = self._transport
        try:
            return json_loads(self._cmd(line, timeout=timeout,
                                        input_claim=input_claim))
        except DriverError as exc:
            if exc.category == "driver_disconnected":
                if transport is not None:
                    transport.close()
                if self._transport is transport:
                    self._connected = False
            raise

    @staticmethod
    def _position(position: dict) -> dict:
        for axis in ("x", "y", "z"):
            position[axis] = _finite(position.get(axis), f"daemon {axis} 坐标")
        return position

    def _check_identity(self, status: dict) -> None:
        if status.get("deviceId") != self.device_id:
            raise DriverError("driver_connect_failed",
                              f"daemon 设备身份不匹配：预期 {self.device_id}，"
                              f"收到 {status.get('deviceId')!r}")

    async def _call(self, fn, *args, **kwargs):
        transport = self._transport
        generation = self._stop_generation

        def run():
            if self._transport is not transport or generation != self._stop_generation:
                raise DriverError("driver_stopped", "连接或动作已取消，禁止继续指令")
            return fn(*args, **kwargs)

        try:
            return await asyncio.to_thread(run)
        except asyncio.CancelledError:
            with self._safety_lock:
                self._stop_generation += 1
            self._connected = False
            if transport is not None:
                transport.close()
            raise

    # ---------- 会话/定位校验 ----------
    def _refresh_session(self) -> str | None:
        status = self._status()
        if status.get("connected") is not True:
            raise DriverError("driver_disconnected", "daemon 串口未连接")
        session = status.get("session")
        if not isinstance(session, str) or not session.strip():
            raise DriverError("driver_uncalibrated", "daemon 会话锚点缺失，拒绝运动")
        if status.get("estopLocked"):
            raise DriverError("driver_estop_latched", "daemon 急停已锁定")
        self._daemon_session = session
        return self._daemon_session

    def _require_motion_feature(self, feature: str, display: str) -> None:
        features = self._status().get("motionFeatures")
        if not isinstance(features, list) or feature not in features:
            raise DriverError(
                "driver_policy_rejected",
                f"当前守护进程不支持连续{display}，请在空闲时人工更新服务；"
                "本次未下发动作，不回退为分段点击或长按",
            )

    def _ensure_located(self) -> dict:
        """会话/定位校验：标定存在、已拟合、锚点与 daemon 当前会话一致。"""
        calibration = target_archive.load_runtime_calibration(self.device_id, self.data_root)
        if calibration is None:
            raise DriverError(
                "driver_uncalibrated",
                f"设备 {self.device_id} 尚未完成摄像头→机械标定：请到电机调试台采集样本并拟合")
        session = self._refresh_session()
        expected_session = calibration.get("daemonSession", calibration.get("session"))
        # FIX-07：统一档案显式 v2/v3 分支——两版本的设备身份与验收状态检查一致。
        if calibration.get("schemaVersion") in (
                calibration_bundle.V2_SCHEMA_VERSION,
                calibration_bundle.BUNDLE_SCHEMA_VERSION_V3):
            if calibration.get("deviceId") != self.device_id:
                raise DriverError("driver_uncalibrated", "统一校准档案设备身份不匹配")
            if calibration.get("status") not in {"ready", calibration_bundle.MOTION_READY_STATUS}:
                raise DriverError("driver_uncalibrated", "统一校准档案尚未完成验收，拒绝业务点击")
        if not expected_session or session != expected_session:
            raise DriverError(
                "driver_uncalibrated",
                "机械臂已重启或串口重连（会话锚点变化），旧标定失效："
                "请到电机调试台重新采集样本并拟合后再运行业务点击")
        return calibration

    # ---------- 生命周期 ----------
    async def connect(self) -> dict:
        from drivers.real_stubs import require_confirmed_hardware

        require_confirmed_hardware()
        if self._estop_latched:
            raise DriverError("driver_estop_latched", "软件急停锁定中，需人工复位后才能连接")
        if self._motion_lock.locked():
            raise DriverError("driver_policy_rejected", "动作执行中，拒绝重新连接")
        if self._transport is not None:
            self._transport.close()
        self._transport = DaemonTCPTransport(self.daemon_addr)
        self._connected = False
        try:
            banner = await self._call(self._transport.open)
            status = await self._call(self._status, timeout=CONNECT_TIMEOUT_S)
            if status.get("connected") is not True:
                raise DriverError(
                    "driver_connect_failed",
                    "daemon 串口未连接（设备未就绪）：请先在电机调试台点击「连接设备」")
            if not isinstance(status.get("session"), str) or not status["session"].strip():
                raise DriverError("driver_connect_failed", "daemon 会话锚点缺失")
        except BaseException:
            self._transport.close()
            self._daemon_session = None
            raise
        self._daemon_session = status.get("session")
        self._connected = True
        self.action_log.append({"action": "connect", "banner": banner, "status": status})
        return {
            "connected": True,
            "driver_version": self.DRIVER_VERSION,
            "daemon_addr": self.daemon_addr,
            "session": self._daemon_session,
            "device_present": status.get("devicePresent"),
            "estop_locked": status.get("estopLocked"),
        }

    async def disconnect(self) -> dict:
        with self._safety_lock:
            self._stop_generation += 1
        self._connected = False
        if self._transport is not None:
            self._transport.close()
        self._daemon_session = None
        self.action_log.append({"action": "disconnect"})
        return {"connected": False}

    async def read_state(self) -> dict:
        if not self._connected or self._transport is None:
            return {"connected": False, "position": None, "estop_latched": self._estop_latched,
                    "limit_ok": True, "error": "未连接"}
        try:
            mpos = await self._call(self._mpos)
            status = await self._call(self._status)
            if status.get("connected") is not True:
                raise DriverError("driver_disconnected", "daemon 串口未连接")
        except DriverError as exc:
            return {"connected": False, "position": None, "estop_latched": self._estop_latched,
                    "limit_ok": True, "error": exc.message}
        return {
            "connected": True,
            "position": {k: mpos[k] for k in ("x", "y", "z")},
            "controller_state": mpos.get("state"),
            "session": status.get("session"),
            "daemon_estop_locked": status.get("estopLocked"),
            "motion_lease": status.get("lease"),
            "motion_features": status.get("motionFeatures", []),
            "estop_latched": self._estop_latched,
            "limit_ok": True,
            "error": None,
        }

    async def read_gesture_state(self) -> dict:
        """Feature discovery must not queue MPOS on the motion connection."""
        def probe():
            transport = DaemonTCPTransport(self.daemon_addr, timeout=1.0)
            try:
                transport.open()
                status = json_loads(transport.cmd("STATUS", timeout=1.0))
                self._check_identity(status)
                return {
                    "connected": status.get("connected") is True and self.connected,
                    "session": status.get("session"),
                    "daemon_estop_locked": bool(status.get("estopLocked")),
                    "estop_latched": self._estop_latched,
                    "motion_lease": status.get("lease"),
                    "motion_features": status.get("motionFeatures") or [],
                }
            finally:
                transport.close()

        return await asyncio.to_thread(probe)

    # ---------- 运动验证（与 RealArmDriver 相同的挂钩语义） ----------
    async def _verify_motion(self, label: str, verifier=None, *,
                             stop_on_error: bool = False) -> dict | None:
        verifier = verifier if verifier is not None else self.motion_verifier
        if verifier is None:
            return None
        try:
            return await verifier.after(label)
        except Exception as exc:
            result = {"verified": False, "reason": f"运动验证异常（按结果未知处理）：{exc}"}
            if stop_on_error:
                result["stop_required"] = True
            return result

    async def _check_live_calibration(self, *, wechat_send: bool = False,
                                      screen_edge_advisory: bool = False) -> dict | None:
        if self.live_calibration_gate is None:
            return None
        evidence = await self.live_calibration_gate(
            wechat_send=wechat_send, screen_edge_advisory=screen_edge_advisory)
        checks = evidence["checks"] if isinstance(evidence, dict) else evidence
        if checks:
            reasons = "；".join(f"{item['code']}：{item['message']}" for item in checks)
            codes = {item["code"] for item in checks}
            if codes == {"camera_waiting"}:
                category = "driver_camera_waiting"
            elif codes <= {"camera_waiting", "camera_unavailable"}:
                category = "driver_policy_rejected"
            else:
                category = "driver_uncalibrated"
            error = DriverError(category, reasons)
            error.motion_not_started = True
            raise error
        return evidence.get("pose") if isinstance(evidence, dict) else None

    @staticmethod
    def _compensate_point(resolved: dict, calibration: dict, pose: dict | None,
                          camera_x: float, camera_y: float, *,
                          already_live: bool = False) -> None:
        if pose is None:
            return
        if already_live and float(pose.get("movementPx", float("inf"))) > 2.0:
            error = DriverError(
                "driver_uncalibrated",
                "手机已偏移，原始摄像头坐标缺少观测时姿态；请重新识别校准坐标")
            error.motion_not_started = True
            raise error
        try:
            rx, ry = screen_pose.physical_point(
                calibration, pose, camera_x, camera_y, already_live=already_live)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            error = DriverError("driver_uncalibrated", f"手机偏移补偿失败：{exc}")
            error.motion_not_started = True
            raise error from exc
        resolved["robotX"], resolved["robotY"] = rx, ry

    def _contact_pose_guard(self, pose: dict | None, *, wechat_send: bool = False):
        if pose is None:
            return None

        async def check():
            current = await self._check_live_calibration(wechat_send=wechat_send)
            if current is None:
                raise DriverError("driver_uncalibrated", "接触前无法确认手机屏幕位置")
            previous_corners = np.asarray(pose["corners"], dtype=float)
            current_corners = np.asarray(current["corners"], dtype=float)
            if (previous_corners.shape != (4, 2) or current_corners.shape != (4, 2)
                    or not np.isfinite(current_corners).all()
                    or float(np.max(np.linalg.norm(
                        current_corners - previous_corners, axis=1))) > 2.0):
                raise DriverError("driver_uncalibrated", "手机在走位后再次移动，未接触屏幕")

        return check

    # ---------- 目标解析 ----------
    async def _resolve_action_target(self, target, *, allow_click_attempt=False):
        if isinstance(target, dict) and target.get("click_attempt", 0) != 0 and not allow_click_attempt:
            error = DriverError("driver_policy_rejected", "替代落点仅允许用于 arm.click 单次点击")
            error.motion_not_started = True
            raise error
        layout = None
        dynamic_snapshot = None
        ref = (target.get("target_ref") or {}) if isinstance(target, dict) else {}
        if (isinstance(ref, dict) and ref.get("source") == "camera_ocr"
                and target.get("target_id") in DYNAMIC_VISUAL_TARGET_IDS
                and (self._layout_provider is None or self._text_target_verifier is None)):
            error = DriverError("driver_policy_rejected", "动态文字目标缺少接触前视觉复核器")
            error.motion_not_started = True
            raise error
        if (self._layout_provider is None and isinstance(target, dict)
                and target.get("_layout_binding") is not None):
            raise DriverError("driver_policy_rejected", "驱动缺少任务已选模板解析器，禁止回退旧目标")
        if self._layout_provider is not None and isinstance(target, dict):
            ref = target.get("target_ref") or {}
            # Dynamic conversation rows and trusted IME geometry keep their own gates.
            dynamic = (isinstance(ref, dict) and (
                ref.get("source") == "live_ime_geometry"
                or (ref.get("source") in DYNAMIC_VISUAL_SOURCES
                    and target.get("target_id") in DYNAMIC_VISUAL_TARGET_IDS)))
            if not dynamic:
                try:
                    layout = await self._layout_provider()
                    if ("_layout_binding" in target and target["_layout_binding"] != (
                            layout.selected.execution_binding if layout is not None else None)):
                        raise ValueError("目标解析前已选模板或粗框版本变化，禁止沿用任务")
                except (ValueError, RuntimeError) as exc:
                    error = DriverError("driver_policy_rejected", str(exc))
                    error.motion_not_started = True
                    raise error from exc
            elif isinstance(ref, dict) and ref.get("source") in DYNAMIC_VISUAL_SOURCES:
                # S3：动态视觉目标不查固定目标表，但必须核对原任务上下文。
                # 解析前：任务快照（如有）与当前已选模板/粗框指针、设备绑定一致；
                # 缺省时由驱动现场固定快照，供走位后复核与传输锁内最终下发前核对。
                try:
                    if (target.get("target_id"), ref["source"]) not in DYNAMIC_COMBOS_IN_USE:
                        raise ValueError("动态目标与来源组合未被产品使用，禁止动作")
                    frame = await self._layout_provider()
                    expected = frame.selected.execution_binding if frame is not None else None
                    if ("_layout_binding" in target and target["_layout_binding"] != expected):
                        raise ValueError("目标解析前已选模板或粗框版本变化，禁止沿用任务")
                    dynamic_snapshot = self._pin_dynamic_visual_context(target, ref, frame, expected)
                    if ref.get("source") == "camera_ocr":
                        dynamic_snapshot["text_check"] = await self._text_target_verifier(ref, frame)
                except (ValueError, RuntimeError) as exc:
                    error = DriverError("driver_policy_rejected", str(exc))
                    error.motion_not_started = True
                    raise error from exc
        trusted_ocr = bool(dynamic_snapshot and callable(dynamic_snapshot.get("text_check")))
        resolved = await self._call(self._resolve_business_target, target, layout_frame=layout,
                                    trusted_ocr=trusted_ocr)
        return (*resolved, layout, dynamic_snapshot)

    def _pin_dynamic_visual_context(self, target, ref, frame, expected) -> dict:
        """S3：为动态视觉目标固定任务快照（上下文+定位来源身份）。

        - 直接驱动路径不因缺 Runtime _pin_task_layout 就无条件接纳共享视觉目标；
        - camera_ocr 行必须带可核对的定位来源，由相机凭据复核器核实原始
          定位、时间和行内容；不能用裸坐标或新帧给旧结果续期；
        - 不重定向新版本、不回旧档：快照固定后任何一项变化即拒绝原目标。
        """
        source = ref.get("source")
        position = ref.get("cameraX") is not None and ref.get("cameraY") is not None
        if not position and not isinstance(ref.get("bbox"), (list, tuple)):
            raise ValueError("动态视觉目标缺少当次定位坐标（cameraX/cameraY 或 bbox）")
        if source == "camera_ocr":
            for key in ("frameSha256", "frameW", "frameH"):
                if ref.get(key) in (None, ""):
                    raise ValueError(
                        f"动态 OCR 目标缺少定位来源 {key}：禁止沿用无法核对来源的裸坐标")
        seq = ref.get("frameSeq")
        if seq is not None and (type(seq) is not int or seq < 0):
            raise ValueError("动态目标定位帧序无效")
        if frame is not None:
            stream_seq = frame.source.get("source_frame_seq")
            if type(seq) is int and type(stream_seq) is int and stream_seq < seq:
                raise ValueError("定位来源帧序超前于当前推流：疑似重放证据，禁止沿用旧行坐标")
        snapshot = {
            "execution_binding": copy.deepcopy(expected),
            "frame": frame,
            "ref": {key: copy.deepcopy(ref[key]) for key in DYNAMIC_REF_IDENTITY_KEYS
                    if ref.get(key) is not None},
            "target_ref": ref,
            "frame_seq": seq if type(seq) is int else None,
            "device_binding": (copy.deepcopy(frame.selected.device_binding)
                               if frame is not None else None),
        }
        if frame is not None:
            snapshot["calibration_sha"] = frame.calibration_sha
        else:
            from runtime.layout_state import content_sha
            snapshot["calibration_sha"] = content_sha(
                target_archive.load_runtime_calibration(self.device_id, self.data_root))
        return snapshot

    def _layout_motion_guards(self, layout, target_id, resolved, calibration, pose, *,
                              wechat_send=False, dynamic_snapshot=None):
        pose_guard = self._contact_pose_guard(pose, wechat_send=wechat_send)
        if layout is None:
            if dynamic_snapshot is None:
                return pose_guard, None
            # S3：动态视觉目标没有共享布局帧，但走位后复核与传输锁内最终
            # 下发前核对照常执行（任务快照：适配/粗框指针、设备绑定、校准、
            # 观测来源代际与新鲜度），guard 挂在现有传输互斥锁语义内。
            return self._dynamic_motion_guards(dynamic_snapshot, pose_guard, pose)
        latest = [layout]
        checked_at = [time.monotonic()]

        def check_current(position=None, *, check_age=True):
            from runtime.calibration_gate import _fresh_source_seq
            try:
                current = target_archive.load_runtime_calibration(self.device_id, self.data_root)
                layout.ensure_current(current)
                latest[0].ensure_current(current)
                if (target_id in {"ime_candidate_bar", "ime_delete_key", "send_button"}
                        and resolved["targetMode"] == "tap" and not self._input_switch_allowed(current)):
                    error = DriverError("input_switch_disabled", "接触前输入与发送开关已关闭，未下发")
                    error.motion_not_started = True
                    raise error
                if check_age:
                    _fresh_source_seq(latest[0].source, None)
                if check_age and time.monotonic() - checked_at[0] > 1.0:
                    raise ValueError("目标视觉复核已过期，未下发下一段动作")
                if position is not None:
                    latest[0].verify_target_point(
                        target_id, {"robotX": position["x"], "robotY": position["y"]}, current)
            except (ValueError, TypeError, KeyError) as exc:
                error = DriverError("driver_uncalibrated", str(exc))
                error.motion_not_started = True
                raise error from exc

        async def check_frame():
            from runtime.calibration_gate import _fresh_source_seq
            try:
                check_current(check_age=False)
                if pose_guard is not None:
                    await pose_guard()
                current = await self._layout_provider()
                if current is None or current.state != layout.state:
                    raise ValueError("当前页面或已选模板已变化，禁止沿用旧目标")
                layout.ensure_current(calibration)
                current.ensure_current(calibration)
                previous = latest[0].source
                _fresh_source_seq(current.source, previous["source_frame_seq"])
                for key in ("source_generation_id", "source_clock_id", "broker_generation_id"):
                    if current.source.get(key) != previous.get(key):
                        raise ValueError("目标观测来源代际已变化，禁止继续动作")
                if (previous.get("source_capture_seq") is not None
                        and (type(current.source.get("source_capture_seq")) is not int
                             or current.source["source_capture_seq"] <= previous["source_capture_seq"])):
                    raise ValueError("目标观测采样序号未推进，禁止继续动作")
                if pose is None:
                    raise ValueError("共享目标缺少实时姿态门，禁止动作")
                if float(np.max(np.linalg.norm(
                        np.asarray(current.pose["corners"]) - np.asarray(pose["corners"]),
                        axis=1))) > 2.0:
                    raise ValueError("目标观测时手机再次移动，禁止沿用旧落点")
                current.verify_target_point(target_id, resolved, calibration)
                latest[0] = current
                checked_at[0] = time.monotonic()
                check_current()
            except (ValueError, TypeError, KeyError) as exc:
                error = DriverError("driver_uncalibrated", str(exc))
                error.motion_not_started = True
                raise error from exc

        return check_frame, check_current

    def _dynamic_motion_guards(self, snapshot, pose_guard, pose):
        """S3：动态视觉目标的走位后复核与传输锁内最终 guard。

        与共享布局 guard 同构：check_frame 是异步复核（每个等待后的关键出口
        重新取帧核对），check_current 是同步复核（在传输锁内、sendall 前经
        geometry_gate 执行）。核对的是任务快照上下文（指针/绑定/校准/来源
        代际与新鲜度），并在当次帧重识别原文字行；不重定向新落点。
        """
        from runtime.calibration_gate import _fresh_source_seq
        from runtime.layout_state import content_sha

        latest = [snapshot["frame"]]
        checked_at = [time.monotonic()]
        pinned_binding = snapshot["execution_binding"]
        pinned_sha = snapshot["calibration_sha"]
        pinned_device = snapshot["device_binding"]
        text_check = [snapshot.get("text_check")]

        def verify_context(current_frame, current):
            current_binding = (current_frame.selected.execution_binding
                               if current_frame is not None else None)
            if current_binding != pinned_binding:
                raise ValueError(
                    "动态目标任务的已选模板、粗框或设备绑定已变化，禁止沿用原落点")
            if current_frame is not None:
                if (pinned_device is not None
                        and current_frame.selected.device_binding != pinned_device):
                    raise ValueError("动态目标任务的设备绑定已变化，禁止沿用原落点")
                current_frame.ensure_current(current)
            elif content_sha(current) != pinned_sha:
                raise ValueError("动态目标任务的校准几何已变化，禁止沿用原落点")

        def check_ref_unchanged():
            ref, pinned = snapshot["target_ref"], snapshot["ref"]
            for key, value in pinned.items():
                if ref.get(key) != value:
                    raise ValueError("动态目标定位来源在动作期间被改写，禁止沿用原落点")

        def check_current(position=None, *, check_age=True):
            try:
                current = target_archive.load_runtime_calibration(self.device_id, self.data_root)
                verify_context(latest[0], current)
                if check_age:
                    if latest[0] is not None:
                        _fresh_source_seq(latest[0].source, None)
                    if time.monotonic() - checked_at[0] > 1.0:
                        raise ValueError("动态目标视觉复核已过期，未下发接触动作")
                check_ref_unchanged()
                if check_age and text_check[0] is not None:
                    text_check[0]()
            except (ValueError, TypeError, KeyError) as exc:
                error = DriverError("driver_uncalibrated", str(exc))
                error.motion_not_started = True
                raise error from exc

        async def check_frame():
            try:
                check_current(check_age=False)
                if pose_guard is not None:
                    await pose_guard()
                current = await self._layout_provider()
                pinned_frame = snapshot["frame"]
                if (current is None) != (pinned_frame is None):
                    raise ValueError("动态目标所在页面或已选模板已变化，禁止沿用旧落点")
                if current is not None:
                    if pinned_frame is not None and current.state != pinned_frame.state:
                        raise ValueError("动态目标所在页面已变化，禁止沿用旧落点")
                    verify_context(current, target_archive.load_runtime_calibration(
                        self.device_id, self.data_root))
                    previous = pinned_frame.source if pinned_frame is not None else None
                    _fresh_source_seq(current.source,
                                      previous.get("source_frame_seq") if previous else None)
                    if previous is not None:
                        for key in ("source_generation_id", "source_clock_id",
                                    "broker_generation_id"):
                            if current.source.get(key) != previous.get(key):
                                raise ValueError("目标观测来源代际已变化，禁止继续动作")
                        if (previous.get("source_capture_seq") is not None
                                and (type(current.source.get("source_capture_seq")) is not int
                                     or current.source["source_capture_seq"]
                                     <= previous["source_capture_seq"])):
                            raise ValueError("目标观测采样序号未推进，旧行坐标不得续鲜")
                    if pose is None:
                        raise ValueError("动态目标缺少实时姿态门，禁止动作")
                    if float(np.max(np.linalg.norm(
                            np.asarray(current.pose["corners"]) - np.asarray(pose["corners"]),
                            axis=1))) > 2.0:
                        raise ValueError("目标观测时手机再次移动，禁止沿用旧落点")
                    latest[0] = current
                if snapshot["ref"].get("source") == "camera_ocr":
                    text_check[0] = await self._text_target_verifier(
                        snapshot["target_ref"], current)
                # 无已选布局时没有整帧观测可固定；实时姿态门每次都是新观测，
                # 以它推进复核时效（不宣称已重新核对列表行内容）。
                checked_at[0] = time.monotonic()
                check_current()
            except (ValueError, TypeError, KeyError) as exc:
                error = DriverError("driver_uncalibrated", str(exc))
                error.motion_not_started = True
                raise error from exc

        return check_frame, check_current

    def _resolve_business_target(self, target: dict, *, layout_frame=None,
                                 trusted_ocr=False) -> tuple[str, dict, dict, dict]:
        if not isinstance(target, dict):
            raise DriverError("driver_policy_rejected", "业务目标必须是对象")
        target_id = target.get("target_id")
        if not isinstance(target_id, str) or not target_id:
            raise DriverError(
                "driver_policy_rejected",
                "业务 arm.click 只接受 target_id（设备级固定目标）；裸位移仅限调试/标定接口")
        try:
            click_attempt = click_points.validate_attempt(target_id, target.get("click_attempt", 0))
        except ValueError as exc:
            error = DriverError("driver_policy_rejected", str(exc))
            error.motion_not_started = True
            raise error from exc
        target_ref = target.get("target_ref") or {}
        if not isinstance(target_ref, dict):
            raise DriverError("driver_policy_rejected", "target_ref 必须是目标对象")
        dynamic_source = str(target_ref.get("source") or "")
        # FIX-15（§11.13.2）：live_ime_geometry 由 Runtime 几何门注入——坐标
        # 已是手机屏幕像素（Android getLocationOnScreen），不经相机单应投影。
        dynamic = layout_frame is None and bool(target_ref) and dynamic_source in {
            "camera_ocr", "vision_back_button", "camera_vision", "live_ime_geometry"}
        # S3-04：S3 动态视觉目标（gesture_point/conversation_item/mark_read_row）
        # 只认产品实际在用的组合；未用组合（含 vision_back_button 全部组合）
        # 明确拒绝，不做无用组合膨胀。其他 target_id 携带视觉 ref 的既有
        # 动态路径（如 send_button＋camera_ocr）不在本包范围，保持原语义。
        if (dynamic and target_id in DYNAMIC_VISUAL_TARGET_IDS
                and dynamic_source in DYNAMIC_VISUAL_SOURCES
                and (target_id, dynamic_source) not in DYNAMIC_COMBOS_IN_USE):
            raise DriverError(
                "driver_policy_rejected",
                f"动态目标组合未被产品使用，拒绝：{target_id}＋{dynamic_source}"
                "（在用组合：conversation_item/mark_read_row＋camera_ocr、"
                "gesture_point＋camera_vision）")
        if target_id in {"conversation_item", "gesture_point"} and not dynamic:
            raise DriverError("driver_policy_rejected",
                              ("动态会话目标" if target_id == "conversation_item" else "动态目标")
                              + "必须携带经摄像头确认的 target_ref")
        # A selected shared layout is authoritative; only unselected devices use the legacy archive.
        ui_entry = None
        if not dynamic and layout_frame is None:
            try:
                ui_entry = ui_targets.load_entry(self.device_id, target_id, self.data_root)
            except ui_targets.UiTargetError as exc:
                raise DriverError("driver_policy_rejected", str(exc)) from exc
        if (target_id not in TARGET_DISPLAY_NAMES
                and target_id not in DYNAMIC_TARGET_DISPLAY_NAMES
                and not (dynamic and (target_id, dynamic_source) in DYNAMIC_COMBOS_IN_USE)
                and ui_entry is None and layout_frame is None):
            known = "、".join(sorted(TARGET_DISPLAY_NAMES.keys() | DYNAMIC_TARGET_DISPLAY_NAMES.keys()))
            raise DriverError("driver_policy_rejected", f"未知业务目标：{target_id}（已知：{known}）")
        calibration = self._ensure_located()
        try:
            click_policy = click_points.policy_from_bundle(calibration)
        except (ValueError, TypeError) as exc:
            error = DriverError("driver_policy_rejected", str(exc))
            error.motion_not_started = True
            raise error from exc
        if calibration.get("schemaVersion") in (
                calibration_bundle.V2_SCHEMA_VERSION,
                calibration_bundle.BUNDLE_SCHEMA_VERSION_V3):
            z = calibration.get("z") or {}
            for key in ("hoverZ", "tapDepth", "tapFeed"):
                value = z.get(key)
                if key == "hoverZ" and value is None:
                    value = z.get("zSafe")
                _finite(value, f"校准 {key}")
        try:
            profile = target_archive.load_profile(self.device_id, self.data_root)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DriverError("driver_limit_exceeded", "设备运动参数无效") from exc
        if layout_frame is not None:
            try:
                layout_frame.ensure_current(calibration)
                entry = layout_frame.target(target_id, target_ref, click_attempt=click_attempt)
            except (ValueError, TypeError, KeyError) as exc:
                error = DriverError("driver_policy_rejected", str(exc))
                error.motion_not_started = True
                raise error from exc
            entry["name_display"] = TARGET_DISPLAY_NAMES.get(target_id, target_id)
        elif dynamic:
            # R2-10（FIX-15 §11.13.2）：live_ime_geometry 的坐标域是手机屏幕
            # 像素（Android getLocationOnScreen），frameW/frameH 是经验证的
            # display 尺寸——缺失/非法必须明确拒绝，绝不能回落相机帧尺寸
            # （640x480 之类）冒充手机 display。其余动态目标仍按相机帧域。
            if dynamic_source == "live_ime_geometry":
                raw_w, raw_h = target_ref.get("frameW"), target_ref.get("frameH")
                if raw_w is None or raw_h is None:
                    raise DriverError(
                        "driver_policy_rejected",
                        "live 几何目标缺少经验证的 display 尺寸"
                        "（frameW/frameH）：禁止以相机帧尺寸冒充手机 display")
                frame_w = _finite(raw_w, "display 宽")
                frame_h = _finite(raw_h, "display 高")
            else:
                frame = calibration.get("frame") or {}
                frame_w = _finite(target_ref.get("frameW", frame.get("width")), "帧宽")
                frame_h = _finite(target_ref.get("frameH", frame.get("height")), "帧高")
            camera_x = target_ref.get("cameraX")
            camera_y = target_ref.get("cameraY")
            if camera_x is None or camera_y is None:
                bbox = target_ref.get("bbox") or target_ref.get("camera_bbox")
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    camera_x = (_finite(bbox[0], "目标边界") + _finite(bbox[2], "目标边界")) / 2
                    camera_y = (_finite(bbox[1], "目标边界") + _finite(bbox[3], "目标边界")) / 2
            if frame_w <= 0 or frame_h <= 0 or camera_x is None or camera_y is None:
                raise DriverError("driver_policy_rejected", "动态目标缺少有效摄像头坐标或帧尺寸")
            entry = {
                "name_display": DYNAMIC_TARGET_DISPLAY_NAMES.get(
                    target_id, TARGET_DISPLAY_NAMES.get(target_id, target_id)),
                "nx": _finite(camera_x, "目标 X") / frame_w,
                "ny": _finite(camera_y, "目标 Y") / frame_h,
                "targetMode": target_ref.get("targetMode", "tap"),
                "target_source": dynamic_source,
                "ocr_text": target_ref.get("text") or target_ref.get("user_name"),
                "ocr_confidence": target_ref.get("confidence"),
                "frame_seq": target_ref.get("frameSeq"),
                "frame_sha256": target_ref.get("frameSha256") or target_ref.get("frame_sha256"),
                "coordinate_version": target_ref.get("coordinateVersion") or target_ref.get("coordinate_version"),
            }
            if dynamic_source == "live_ime_geometry":
                # R2-10：把经验证的 display 尺寸随 entry 保留到最终解析——
                # _resolve_live_geometry_target 据此还原屏幕像素域目标中心，
                # 不再读相机帧尺寸（此前 KeyError/域混算的根因）。
                entry["frameW"] = frame_w
                entry["frameH"] = frame_h
            elif (dynamic_source == "camera_ocr" and trusted_ocr
                  and (target_id in click_points.RETRY_TARGETS or click_policy is not None)):
                from runtime.layout_geometry import mapping_error_bound

                model = (calibration.get("camera") or {}).get("cameraToScreen") or {}
                screen = calibration.get("screen") or {}
                try:
                    matrix = np.asarray(model.get("homography"), dtype=float).reshape(3, 3)
                except (TypeError, ValueError):
                    matrix = None
                try:
                    chosen, screen_point, supported, safety = click_points.ocr_point(
                        target_id, target_ref.get("bbox"), [camera_x, camera_y], matrix,
                        mapping_error_bound(calibration),
                        [screen.get("screenWidth") or screen.get("widthPx"),
                         screen.get("screenHeight") or screen.get("heightPx")], click_attempt,
                        camera_polygon=target_ref.get("cameraPolygon"),
                        require_polygon=any(target_ref.get(key) is not None for key in
                                            ("template_ref", "adaptation_ref", "installation_region_ref")),
                        policy=click_policy)
                except ValueError as exc:
                    error = DriverError("driver_policy_rejected", str(exc))
                    error.motion_not_started = True
                    raise error from exc
                entry.update(nx=float(chosen[0] / frame_w), ny=float(chosen[1] / frame_h),
                             click_attempt=click_attempt, click_retry_supported=supported,
                             click_point={"screen": screen_point.tolist() if screen_point is not None else None,
                                          "camera": chosen.tolist(), "safety": safety,
                                          **{key: safety[key] for key in
                                             ("originalCenter", "aim", "selectionOffset", "policy")
                                             if key in safety}})
        else:
            # 2026-09-04 三步收敛：业务固定目标只认校准后登记层（ui_targets），
            # 旧 bundle targets 兑底已移除——未登记/未启用一律明确拒绝，
            # 不再静默使用验收链路标记的坐标。旧链路仅剩 E 验收读取（迁移期）。
            if ui_entry is not None:
                try:
                    entry = ui_targets.resolve_entry(ui_entry, calibration)
                except ui_targets.UiTargetError as exc:
                    category = ("driver_uncalibrated" if exc.code == "target_stale"
                                else "driver_policy_rejected")
                    raise DriverError(category, str(exc)) from exc
            else:
                display = TARGET_DISPLAY_NAMES.get(target_id, target_id)
                raise DriverError(
                    "driver_policy_rejected",
                    f"目标 {target_id}（{display}）尚未登记："
                    "请到调试台「固定目标登记」面板用「仅标记」标出、"
                    "受控测试通过并启用业务调用后再使用")
        for key in ("nx", "ny"):
            value = _finite(entry.get(key), f"目标 {key}")
            if not 0 <= value <= 1:
                raise DriverError("driver_policy_rejected", "目标坐标超出摄像头画面")
        if entry.get("targetMode") not in ("tap", "hover_only"):
            raise DriverError("driver_policy_rejected", "目标动作模式无效")
        if "click_attempt" not in entry:
            if click_attempt:
                error = DriverError("driver_policy_rejected", "目标缺少可信安全区，不支持替代点击落点")
                error.motion_not_started = True
                raise error
            frame = calibration.get("frame") or {}
            if entry.get("target_source") == "live_ime_geometry":
                screen_point = [entry["nx"] * entry["frameW"], entry["ny"] * entry["frameH"]]
                camera_point = None
            else:
                screen_point = None
                camera_point = [entry["nx"] * (frame.get("width") or calibration.get("frameW", 1)),
                                entry["ny"] * (frame.get("height") or calibration.get("frameH", 1))]
            entry.update(click_attempt=0, click_retry_supported=False,
                         click_point={"screen": screen_point, "camera": camera_point,
                                      "safety": {"source": "unavailable"}})
        try:
            if entry.get("target_source") == "live_ime_geometry":
                # FIX-15（§11.13.2）：live 几何坐标已是手机屏幕像素
                # （Android getLocationOnScreen+宽高），直接 robotToScreen
                # 逆映射，不经相机→屏幕单应投影。
                resolved = self._resolve_live_geometry_target(entry, calibration, profile)
            else:
                resolved = target_archive.resolve_target(entry, calibration, profile)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise DriverError("driver_limit_exceeded", str(exc)) from exc
        z_profile = calibration.get("z") or {}
        dwell_ms = z_profile.get("tapDwellMs", 0)
        if dwell_ms is None:
            dwell_ms = 0
        dwell_ms = _finite(dwell_ms, "点击停留毫秒")
        resolved["tapDwell"] = dwell_ms / 1000.0
        self.action_log.append({"action": "resolve_target", "target_id": target_id,
                                "resolved": resolved, "session": calibration.get("session"),
                                "target_source": entry.get("target_source", "fixed_target"),
                                "target_ref": dict(target_ref) if dynamic else None})
        return target_id, entry, resolved, calibration

    def _resolve_live_geometry_target(self, entry: dict, calibration: dict,
                                      profile: dict) -> dict:
        """FIX-15（§11.13.2）：live 键盘几何目标解析（屏幕像素 → 机械坐标）。

        输入 nx/ny 已按手机 display 宽高归一化（Runtime 几何门注入的
        live_ime_geometry target_ref）；robotToScreen 逆映射不经相机单应，
        避免把屏幕像素当相机像素。Z 参数与普通目标同源（profile 兜底）。
        """
        robot_to_screen = ((calibration.get("xy") or {}).get("robotToScreen"))
        if not robot_to_screen:
            raise DriverError("driver_uncalibrated",
                              "统一校准档案缺少 robotToScreen 映射，无法解析 live 几何目标")
        if entry.get("frameW") in (None, "") or entry.get("frameH") in (None, ""):
            # R2-10：进入本函数前 entry 必须已带经验证的 display 尺寸
            #（_resolve_business_target 注入）；缺失即协议不符，不读相机帧尺寸。
            raise DriverError("driver_policy_rejected",
                              "live 几何目标缺少 display 尺寸（frameW/frameH）："
                              "禁止以相机帧尺寸冒充手机 display")
        sx = float(entry["nx"]) * float(entry["frameW"])
        sy = float(entry["ny"]) * float(entry["frameH"])
        rx, ry = calibration_bundle.robot_from_screen(robot_to_screen, sx, sy)
        resolved = {
            "robotX": round(rx, 3),
            "robotY": round(ry, 3),
            "hoverZ": float(profile["hoverZ"]),
            "tapDepth": float(entry.get("tapDepth") or profile["tapDepth"]),
            "tapFeed": float(entry.get("tapFeed") or profile["tapFeed"]),
            "targetMode": entry.get("targetMode") or "tap",
        }
        check_feed(resolved["tapFeed"], axis="Z")
        return resolved

    def _precheck_motion(self, resolved: dict, current: dict, *, with_tap: bool) -> None:
        for key in ("robotX", "robotY", "hoverZ", "tapDepth", "tapFeed"):
            resolved[key] = _finite(resolved.get(key), key)
        self._position(current)
        try:
            check_feed(resolved["tapFeed"], axis="Z")
        except ValueError as exc:
            raise DriverError("driver_limit_exceeded", str(exc)) from exc
        if with_tap:
            try:
                check_tap(resolved["tapDepth"], resolved["tapDwell"])
            except ValueError as exc:
                raise DriverError("driver_limit_exceeded", str(exc)) from exc
        dz = resolved["hoverZ"] - current["z"]
        dx = resolved["robotX"] - current["x"]
        dy = resolved["robotY"] - current["y"]
        _finite(dz, "Z 位移")
        if abs(dz) > TRAVEL_TOTAL_MAX_MM:
            raise DriverError("driver_limit_exceeded", "Z 悬停总位移超过单次上限")
        total = (dx * dx + dy * dy) ** 0.5
        if total > TRAVEL_TOTAL_MAX_MM:
            raise DriverError(
                "driver_limit_exceeded",
                f"目标位移 {total:.0f}mm 超过单次上限 {TRAVEL_TOTAL_MAX_MM:g}mm，标定映射疑似异常")
        if abs(dx) > XY_STROKE_MAX or abs(dy) > XY_STROKE_MAX:
            raise DriverError("driver_limit_exceeded",
                              f"XY 单次直达位移超出上限 {XY_STROKE_MAX:g}mm")

    def _precheck_swipe_motion(self, resolved: dict, current: dict) -> None:
        """校验滑动的两段 XY 和接触深度，仍沿用当前设备 Z 标定。"""
        for key in (
            "startRobotX", "startRobotY", "endRobotX", "endRobotY",
            "hoverZ", "tapDepth", "tapFeed", "swipeFeed", "durationMs",
        ):
            resolved[key] = _finite(resolved.get(key), key)
        self._position(current)
        try:
            check_feed(resolved["tapFeed"], axis="Z")
            check_feed(resolved["swipeFeed"], axis="X")
            check_tap(resolved["tapDepth"], 0.0)
        except ValueError as exc:
            raise DriverError("driver_gesture_invalid", str(exc)) from exc
        if resolved["swipeFeed"] > SWIPE_FEED_MAX:
            raise DriverError(
                "driver_gesture_invalid",
                f"滑动速度 F{resolved['swipeFeed']:g} 超过上限 {SWIPE_FEED_MAX:g}",
            )
        if not SWIPE_DURATION_MIN_MS <= resolved["durationMs"] <= SWIPE_DURATION_MAX_MS:
            raise DriverError(
                "driver_gesture_invalid",
                f"滑动时长必须为 {SWIPE_DURATION_MIN_MS:g}~{SWIPE_DURATION_MAX_MS:g}ms",
            )
        start_dx = resolved["startRobotX"] - current["x"]
        start_dy = resolved["startRobotY"] - current["y"]
        stroke_dx = resolved["endRobotX"] - resolved["startRobotX"]
        stroke_dy = resolved["endRobotY"] - resolved["startRobotY"]
        for value, label in (
            (resolved["hoverZ"] - current["z"], "Z 悬停总位移"),
            (start_dx, "滑动起点 X"),
            (start_dy, "滑动起点 Y"),
            (stroke_dx, "滑动路径 X"),
            (stroke_dy, "滑动路径 Y"),
        ):
            _finite(value, label)
        if abs(resolved["hoverZ"] - current["z"]) > TRAVEL_TOTAL_MAX_MM:
            raise DriverError("driver_limit_exceeded", "Z 悬停总位移超过单次上限")
        start_distance = (start_dx * start_dx + start_dy * start_dy) ** 0.5
        stroke_distance = (stroke_dx * stroke_dx + stroke_dy * stroke_dy) ** 0.5
        if start_distance > TRAVEL_TOTAL_MAX_MM:
            raise DriverError("driver_limit_exceeded", "滑动起点位移超过单次上限")
        if stroke_distance <= 0.05:
            raise DriverError("driver_gesture_invalid", "滑动起点和终点不能相同")
        if stroke_distance > SWIPE_TOTAL_MAX_MM:
            raise DriverError(
                "driver_gesture_invalid",
                f"滑动总距离 {stroke_distance:.1f}mm 超过上限 {SWIPE_TOTAL_MAX_MM:g}mm",
            )
        if abs(stroke_dx) > XY_STROKE_MAX or abs(stroke_dy) > XY_STROKE_MAX:
            raise DriverError("driver_gesture_invalid", "滑动单轴位移超过机械臂软限位")

    def _build_swipe_steps(self, resolved: dict, current: dict) -> list[tuple[str, str]]:
        steps: list[tuple[str, str]] = []
        dz = resolved["hoverZ"] - current["z"]
        if abs(dz) >= 0.05:
            for chunk in split_segments(dz, HOVER_SEGMENT_MAX_MM):
                steps.append(("swipe_hover_z", f"Z {chunk:.3f} F{min(resolved['tapFeed'], 1500.0):g}"))
        start_dx = resolved["startRobotX"] - current["x"]
        start_dy = resolved["startRobotY"] - current["y"]
        if abs(start_dx) > 0.05 or abs(start_dy) > 0.05:
            steps.append(("swipe_to_start",
                          f"XY {start_dx:.3f} {start_dy:.3f} F{CALIBRATED_XY_FEED:g}"))
        stroke_dx = resolved["endRobotX"] - resolved["startRobotX"]
        stroke_dy = resolved["endRobotY"] - resolved["startRobotY"]
        steps.append(("swipe",
                      f"SWIPE {stroke_dx:.3f} {stroke_dy:.3f} {resolved['tapDepth']:.3f} "
                      f"F{resolved['swipeFeed']:g} Z{resolved['tapFeed']:g}"))
        return steps

    def _validate_path1_waypoints(self, strokes: list, calibration: dict) -> None:
        """PATH1 机械点末道门禁：robot→screen 反解必须落在校准屏幕区域内。

        外部 plan 的 mech_points 是裸机械坐标，相对位移限值挡不住
        "合法 binding + 整体平移到屏幕外"的组合；这里与 resolve_point
        同一屏幕域边界规则（含校准不确定量），逐点拒绝越界。
        """
        xy = calibration.get("xy") or {}
        screen = calibration.get("screen") or {}
        width = screen_gestures.number(screen.get("screenWidth"), "屏幕宽度")
        height = screen_gestures.number(screen.get("screenHeight"), "屏幕高度")
        uncertainty = calibration_bundle.mapping_error_bound(
            calibration, allow_legacy_defaults=True)
        if uncertainty is None:
            raise ValueError("校准最大误差无效，不能确认屏幕安全边界")
        model = xy.get("robotToScreen") or {}
        for stroke_index, stroke in enumerate(strokes):
            for point_index, point in enumerate(stroke.get("mech_points") or []):
                try:
                    rx = screen_gestures.number(point.get("robotX"), "机械 X")
                    ry = screen_gestures.number(point.get("robotY"), "机械 Y")
                    sx, sy = calibration_bundle.screen_from_robot(model, rx, ry)
                except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
                    raise ValueError(
                        f"第 {stroke_index + 1} 段第 {point_index + 1} 个机械点"
                        f"无法反解屏幕坐标：{exc}") from exc
                # 0.05px 容差吸收规划期 0.001mm 量化与浮点噪声
                if min(sx, sy, width - sx, height - sy) < uncertainty - 0.05:
                    raise ValueError(
                        f"第 {stroke_index + 1} 段第 {point_index + 1} 个机械点反解屏幕"
                        f"({sx:.1f},{sy:.1f}) 超出校准屏幕区域（含不确定量 "
                        f"{uncertainty:.1f}px），拒绝执行")

    def _precheck_path_motion(self, resolved: dict, current: dict) -> None:
        """连续路径的悬停/走位预检：接触段本身由 PATH1 宏内限值把关。"""
        for key in ("hoverZ", "tapDepth", "tapFeed"):
            resolved[key] = _finite(resolved.get(key), key)
        self._position(current)
        try:
            check_feed(resolved["tapFeed"], axis="Z")
            check_tap(resolved["tapDepth"], 0.0)
        except ValueError as exc:
            raise DriverError("driver_limit_exceeded", str(exc)) from exc
        if abs(resolved["hoverZ"] - current["z"]) > TRAVEL_TOTAL_MAX_MM:
            raise DriverError("driver_limit_exceeded", "Z 悬停总位移超过单次上限")
        waypoints: list[tuple[tuple[float, float], tuple[float, float]]] = []
        x, y = current["x"], current["y"]
        for index, stroke in enumerate(resolved["_strokes"]):
            points = stroke["mech_points"]
            try:
                start = (_finite(points[0]["robotX"], f"PATH1 第 {index + 1} 段起点 X"),
                         _finite(points[0]["robotY"], f"PATH1 第 {index + 1} 段起点 Y"))
                end = (_finite(points[-1]["robotX"], f"PATH1 第 {index + 1} 段终点 X"),
                       _finite(points[-1]["robotY"], f"PATH1 第 {index + 1} 段终点 Y"))
            except (IndexError, TypeError, KeyError) as exc:
                raise DriverError(
                    "driver_gesture_invalid", f"第 {index + 1} 段 mech_points 无效") from exc
            dx, dy = start[0] - x, start[1] - y
            _finite(dx, "PATH1 起点 X 位移")
            _finite(dy, "PATH1 起点 Y 位移")
            if (dx * dx + dy * dy) ** 0.5 > TRAVEL_TOTAL_MAX_MM:
                raise DriverError("driver_limit_exceeded", "PATH1 起点位移超过单次上限")
            if abs(dx) > XY_STROKE_MAX or abs(dy) > XY_STROKE_MAX:
                raise DriverError(
                    "driver_limit_exceeded",
                    f"PATH1 单轴直达位移超出上限 {XY_STROKE_MAX:g}mm")
            waypoints.append((start, end))
            x, y = end
        resolved["_waypoints"] = waypoints

    def _build_path_steps(self, resolved: dict, current: dict) -> list[tuple[str, str]]:
        """每个 stroke 前回悬停并走位到起点；stroke 之间在悬停状态衔接。"""
        steps: list[tuple[str, str]] = []
        x, y, z = current["x"], current["y"], current["z"]
        for index, ((start, end), payload) in enumerate(
                zip(resolved["_waypoints"], resolved["_payloads"])):
            dz = resolved["hoverZ"] - z
            if abs(dz) >= 0.05:
                for chunk in split_segments(dz, HOVER_SEGMENT_MAX_MM):
                    steps.append(("path_hover_z",
                                  f"Z {chunk:.3f} F{min(resolved['tapFeed'], 1500.0):g}"))
                z = resolved["hoverZ"]  # PATH1 抬回宏前 Z，即悬停高度
            dx, dy = start[0] - x, start[1] - y
            if abs(dx) > 0.05 or abs(dy) > 0.05:
                steps.append(("path_to_start",
                              f"XY {dx:.3f} {dy:.3f} F{CALIBRATED_XY_FEED:g}"))
            x, y = start
            compact = json.dumps(payload, separators=(",", ":"))
            if "\n" in compact or "\r" in compact:
                raise DriverError("driver_policy_rejected",
                                  "PATH1 指令包含换行，禁止下发")
            steps.append((f"path_stroke_{index}", "PATH1 " + compact))
            x, y = end
        return steps

    @staticmethod
    def _gesture_expected_endpoint(command: str, current: dict) -> dict:
        """Use the rounded, prechecked relative command, not the last reply."""
        parts = command.split()
        expected = {axis: float(current[axis]) for axis in ("x", "y", "z")}
        try:
            if parts[0] == "Z":
                expected["z"] += float(parts[1])
            elif parts[0] in {"XY", "SWIPE"}:
                expected["x"] += float(parts[1])
                expected["y"] += float(parts[2])
            elif parts[0] == "TAP2":
                pass
            else:
                raise ValueError(f"未知手势运动指令：{parts[0]}")
        except (IndexError, TypeError, ValueError) as exc:
            raise DriverError(
                "driver_disconnected",
                f"手势预期终点无法解析：{command}；结果未知，禁止重放动作",
            ) from exc
        return expected

    @staticmethod
    def _validate_gesture_reply(response: dict, expected: dict, command: str) -> dict:
        """Daemon motion replies are flat XYZ/state, without macro counters."""
        if not isinstance(response, dict):
            raise DriverError(
                "driver_disconnected", f"手势回包不是对象：{command}")
        if response.get("error"):
            raise DriverError(
                "driver_disconnected", f"daemon 报告手势失败：{response['error']}")
        # Optional flags may contradict completion, but are never required.
        for field in ("ok", "completed"):
            if field in response and response[field] is not True:
                raise DriverError(
                    "driver_disconnected", f"daemon 回包未完成 {field}：{command}")
        for field in ("unknown", "failed", "stopped", "cancelled", "abort", "aborted"):
            if field in response and response[field] is not False:
                raise DriverError(
                    "driver_disconnected",
                    f"daemon 回包包含 {field} 状态：{command}")
        for field in ("status", "outcome", "outcome_hint"):
            if field in response and response[field] not in (
                    "ok", "success", "confirmed", "completed"):
                raise DriverError(
                    "driver_disconnected",
                    f"daemon 回包包含非完成状态 {field}={response[field]!r}：{command}")
        if response.get("state") != "Idle":
            raise DriverError(
                "driver_disconnected",
                f"daemon 回包状态不是 Idle：{response.get('state')!r}")
        try:
            actual = DaemonArmDriver._position(dict(response))
        except DriverError as exc:
            raise DriverError(
                "driver_disconnected",
                f"daemon 手势位置回包无效：{command}") from exc
        if any(abs(actual[axis] - expected[axis]) > GESTURE_POSITION_TOLERANCE_MM
               for axis in ("x", "y", "z")):
            deltas = ", ".join(
                f"{axis}={actual[axis] - expected[axis]:.3f}"
                for axis in ("x", "y", "z"))
            raise DriverError(
                "driver_disconnected",
                f"daemon 手势终点与预期不符（{deltas}mm）：{command}")
        return actual

    @staticmethod
    def _validate_path1_reply(reply, expected_segments: int) -> dict:
        """PATH1 回包校验：宏计数、Idle、抬回确认（规范第 22.9 节）。

        不能用已发送行数证明执行完成；全部字段确认通过才视为控制器完成。
        """
        if not isinstance(reply, dict) or reply.get("error"):
            raise DriverError(
                "driver_disconnected", "PATH1 回包无效或报错，结果未知，禁止重放动作")
        for field in ("ok", "completed"):
            if field in reply and reply[field] is not True:
                raise DriverError(
                    "driver_disconnected", f"PATH1 回包未完成 {field}，结果未知")
        if reply.get("state") != "Idle":
            raise DriverError(
                "driver_disconnected",
                f"PATH1 回包状态不是 Idle：{reply.get('state')!r}，结果未知")
        if reply.get("path_completed") is not True:
            raise DriverError(
                "driver_disconnected", "PATH1 回包未确认 path_completed，结果未知")
        if reply.get("contact_released") is not True:
            raise DriverError(
                "driver_disconnected", "PATH1 回包未确认已抬回（contact_released），结果未知")
        if reply.get("segments_completed") != expected_segments:
            raise DriverError(
                "driver_disconnected",
                f"PATH1 回包段数 {reply.get('segments_completed')!r} 与预期 "
                f"{expected_segments} 不符，结果未知")
        return reply

    # ---------- 运动租约事务 ----------
    async def _with_motion_lease(self, resolved, *, with_tap: bool,
                                 input_claim: dict | None = None,
                                  session: str, generation: int,
                                  tap_count: int = 1,
                                  tap_interval_s: float = 0.0,
                                  precheck=None,
                                  build_steps=None,
                                  verifier=None,
                                  execution_guard=None,
                                  contact_guard=None,
                                  command_guard=None,
                                  validate_gesture_replies: bool = False,
                                  allow_gesture_observation: bool = False) -> tuple[dict, list]:
        """CLAIM → BEGIN_MOTION → 依次执行 steps → END_MOTION → RELEASE。

        在取得租约后重新读取位置，避免执行租约前生成的相对位移。
        """
        if not self._motion_lock.acquire(blocking=False):
            raise DriverError("driver_policy_rejected", "本设备已有动作执行中")
        transport = self._transport
        claimed = False
        begun = False
        motion_started = False
        motion_failure: BaseException | None = None
        phases: list[dict] = []
        mpos: dict = {}
        verifier = verifier if verifier is not None else self.motion_verifier
        # 观察模式语义（R4 P1-3）：verifier 自述 observation_only 且调用方
        # 允许手势观测即视为观察模式——证据照记、verified 照实报 False，
        # 但不得因"未验证"中止执行。是否依赖回包校验（validate_gesture_replies）
        # 与是否允许观察解耦：PATH1 自带五重回包确认、不依赖租约内校验，
        # 但同样不允许被观察模式判失败而中断。
        observe_only = (allow_gesture_observation
                        and getattr(verifier, "observation_only", False) is True)
        retract_after_observation_failure = False

        def check_context():
            self._check_ready()
            if self._transport is not transport or generation != self._stop_generation:
                raise DriverError("driver_stopped", "动作已停止，禁止继续下发运动")
            if execution_guard is not None:
                execution_guard()
            if "_screenBinding" in resolved:
                try:
                    screen_gestures.require_binding(
                        resolved["_screenBinding"],
                        target_archive.load_runtime_calibration(self.device_id, self.data_root))
                except (ValueError, TypeError) as exc:
                    raise DriverError("driver_uncalibrated", str(exc)) from exc

        # FIX-01/§4.8.4：输入认领回调只允许附在产生本次输入接触的命令上；
        # 走位/状态查询/抬笔/安全收尾一律不带认领（不得提前消费）。
        CONTACT_LABELS = frozenset({"tap"})
        claimed_once = {"fired": False}

        def execute_command(command, label=None):
            nonlocal motion_started
            # Recheck after the worker queue, immediately before transport I/O.
            check_context()
            previously_started = motion_started
            motion_started = True
            attach = input_claim if (input_claim is not None
                                     and label in CONTACT_LABELS
                                     and not claimed_once["fired"]) else None
            if attach is not None:
                claimed_once["fired"] = True
            if command_guard is not None:
                if "input_claim" not in inspect.signature(transport.cmd).parameters:
                    motion_started = previously_started
                    error = DriverError("driver_policy_rejected", "传输不支持目标最终门禁，禁止运动")
                    error.motion_not_started = True
                    raise error
                previous_gate = (attach or {}).get("geometry_gate")

                def combined_gate():
                    command_guard(mpos if label in {"tap", "double_tap"} else None)
                    if callable(previous_gate):
                        previous_gate()

                attach = dict(attach or {}, geometry_gate=combined_gate)
            try:
                return self._json_cmd(command, input_claim=attach)
            except DriverError as exc:
                if getattr(exc, "motion_not_started", False):
                    motion_started = previously_started
                raise

        try:
            check_context()
            claim = await self._call(
                self._json_cmd, f"CLAIM {self.owner_type} {self.owner_id} {self.task_id}".strip())
            claimed = True
            if claim.get("claimed") is not True or not claim.get("lease_id"):
                raise DriverError("driver_disconnected", "运动租约响应无效")
            tx = await self._call(self._json_cmd, "BEGIN_MOTION")
            if not isinstance(tx.get("tx"), str) or not tx["tx"]:
                raise DriverError("driver_disconnected", "动作事务响应无效")
            begun = True
            current_session = await self._call(self._refresh_session)
            if current_session != session:
                raise DriverError("driver_uncalibrated", "动作前会话锚点变化，旧标定失效")
            mpos = await self._call(self._mpos)
            if precheck is None:
                self._precheck_motion(resolved, mpos, with_tap=with_tap)
            else:
                precheck(resolved, mpos)
            if build_steps is None:
                steps = self._build_steps(
                    resolved, mpos, with_tap=with_tap, tap_count=tap_count,
                    tap_interval_s=tap_interval_s)
            else:
                steps = build_steps(resolved, mpos)
            expected_positions = []
            expected = dict(mpos)
            if validate_gesture_replies:
                for _, command in steps:
                    expected = self._gesture_expected_endpoint(command, expected)
                    expected_positions.append(expected)
            for step_index, (label, cmd) in enumerate(steps):
                check_context()
                # A wake TAP2 may have completed mechanically while its
                # camera review failed.  The retract is a prebuilt safety
                # motion, so do not require another clear-screen/pose sample
                # before it.  It is never a second contact action.
                skip_retract_observation = (
                    observe_only
                    and label == "double_tap_retract"
                    and retract_after_observation_failure
                )
                if verifier is not None and not skip_retract_observation:
                    prepare = getattr(verifier, "prepare", None)
                    if callable(prepare):
                        prepare(cmd, self._camera_motion_delta(cmd, mpos))
                    await verifier.before(label)
                check_context()
                if await self._call(self._refresh_session) != session:
                    raise DriverError("driver_uncalibrated", "运动会话变化，禁止继续动作")
                check_context()
                # STATUS and leased MPOS may block while the phone moves.
                # Check geometry after those waits, before travel or contact.
                if command_guard is not None and label in {"tap", "double_tap"}:
                    mpos = await self._call(self._mpos)
                if contact_guard is not None and (
                        step_index == 0 or cmd.startswith("XY ")
                        or label in {"tap", "double_tap", "swipe"}
                        or label.startswith("path_stroke_")):
                    await contact_guard()
                check_context()
                response = await self._call(execute_command, cmd, label)
                if validate_gesture_replies:
                    mpos = self._validate_gesture_reply(
                        response, expected_positions[step_index], cmd)
                else:
                    mpos = self._position(response)
                check_context()
                verification = await self._verify_motion(
                    label, verifier, stop_on_error=validate_gesture_replies)
                check_context()
                phases.append({"phase": label, "cmd": cmd, "verification": verification})
                if verification is not None and (
                        (validate_gesture_replies and verification.get("stop_required") is True)
                        or (not verification.get("verified") and not observe_only)):
                    # A wake TAP2 may have completed mechanically while the
                    # post-tap camera observation failed.  The controller
                    # reply is already validated in this branch, so execute
                    # the prebuilt retract step once; keep the final result
                    # unknown and never replay the tap.  Lost replies and
                    # cancellations still exit through the exception path.
                    if (observe_only and label == "double_tap"
                            and step_index + 1 < len(steps)
                            and steps[step_index + 1][0] == "double_tap_retract"):
                        retract_after_observation_failure = True
                        continue
                    break
        except BaseException as exc:
            motion_failure = exc
            # A cancelled worker may still be sending/reading. Closing the
            # pinned connection is bounded and never replays the motion.
            if (motion_started or isinstance(exc, asyncio.CancelledError)
                    or not isinstance(exc, DriverError) or exc.category == "driver_disconnected"):
                if transport is not None:
                    transport.close()
                self._connected = False
            if motion_started and isinstance(exc, DriverError):
                if getattr(exc, "motion_not_started", False):
                    # R2-01/FIX-01：最后认领在接触 sendall 前拒绝——接触确定
                    # 未下发；保留输入拒绝语义，不抹掉已发生的走位事实，
                    # 也不谎称整个动作结果未知。
                    raise DriverError(
                        exc.category,
                        f"{exc.message}；认领前走位已下发并中止，接触未下发") from exc
                raise DriverError(
                    "driver_disconnected",
                    f"{exc.message}；运动已下发，结果未知，禁止重放动作") from exc
            if isinstance(exc, DriverError):
                exc.motion_not_started = True
            raise
        finally:
            cleanup_error = None
            try:
                abort_verification = getattr(verifier, "abort", None)
                if callable(abort_verification):
                    await abort_verification()
            except BaseException as exc:
                cleanup_error = exc
            try:
                if transport is not None and transport.is_open() and begun:
                    ended = await self._call(
                        self._json_cmd, "END_MOTION", timeout=CLEANUP_TIMEOUT_S)
                    if ended.get("ended") != tx["tx"]:
                        cleanup_error = DriverError(
                            "driver_disconnected", "动作事务结束未确认，结果未知")
            except BaseException as exc:
                cleanup_error = exc
            try:
                if transport is not None and transport.is_open() and claimed:
                    release = await self._call(
                        self._json_cmd, "RELEASE", timeout=CLEANUP_TIMEOUT_S)
                    if release.get("released") is not True:
                        cleanup_error = DriverError(
                            "driver_disconnected", "运动租约释放未确认，结果未知")
            except BaseException as exc:
                cleanup_error = exc
            try:
                if motion_failure is not None and motion_started:
                    if transport is not None:
                        transport.close()
                    self._connected = False
                if cleanup_error is not None:
                    if transport is not None:
                        transport.close()
                    self._connected = False
                    if motion_started and isinstance(cleanup_error, Exception):
                        raise DriverError(
                            "driver_disconnected",
                            f"动作收尾未确认：{cleanup_error}；运动已下发，结果未知，禁止重放动作",
                        ) from cleanup_error
                    if isinstance(cleanup_error, DriverError):
                        cleanup_error.motion_not_started = True
                    raise cleanup_error
            finally:
                self._motion_lock.release()
        return mpos, phases

    def _camera_motion_delta(self, command: str, current: dict):
        parts = command.split()
        if parts[0] not in {"XY", "SWIPE"}:
            return None
        calibration = target_archive.load_runtime_calibration(self.device_id, self.data_root)
        model = calibration["xy"]["robotToScreen"]
        matrix = np.asarray(calibration["camera"]["cameraToScreen"]["homography"])
        points = []
        for x, y in ((current["x"], current["y"]),
                     (current["x"] + float(parts[1]), current["y"] + float(parts[2]))):
            sx, sy = calibration_bundle.screen_from_robot(model, x, y)
            point = np.linalg.solve(matrix, [sx, sy, 1])
            points.append(point[:2] / point[2])
        return (points[1] - points[0]).tolist()

    def _build_steps(self, resolved: dict, current: dict, *, with_tap: bool,
                     tap_count: int = 1, tap_interval_s: float = 0.0) -> list[tuple[str, str]]:
        steps: list[tuple[str, str]] = []
        dz = resolved["hoverZ"] - current["z"]
        if abs(dz) >= 0.05:
            for chunk in split_segments(dz, HOVER_SEGMENT_MAX_MM):
                steps.append(("hover_z", f"Z {chunk:.3f} F{min(resolved['tapFeed'], 1500.0):g}"))
        dx = resolved["robotX"] - current["x"]
        dy = resolved["robotY"] - current["y"]
        if abs(dx) > 0.05 or abs(dy) > 0.05:
            # XY 只发一条直达指令；超过 daemon 单段限值时在预检阶段拒绝。
            steps.append(("travel_xy", f"XY {dx:.3f} {dy:.3f} F{CALIBRATED_XY_FEED:g}"))
        if with_tap:
            dwell = resolved["tapDwell"]
            dwell_arg = f" P{dwell:g}" if dwell > 0 else ""
            if tap_count == 2:
                lift = resolved.get("repeatLift", resolved["tapDepth"])
                approach = resolved["tapDepth"] - lift
                # Travel at the calibrated hover height, then shorten both
                # press cycles without changing the calibrated contact plane.
                if approach > 0:
                    steps.append(("double_tap_approach",
                                  f"Z {approach:.3f} F{resolved['tapFeed']:g}"))
                steps.append(("double_tap", f"TAP2 {lift:.3f} "
                              f"F{resolved.get('doubleTapFeed', resolved['tapFeed']):g}"
                              f"{dwell_arg} I{tap_interval_s:g}"))
                if approach > 0:
                    steps.append(("double_tap_retract",
                                  f"Z {-approach:.3f} F{resolved['tapFeed']:g}"))
            else:
                steps.append(("tap", f"TAP {resolved['tapDepth']:.3f} "
                              f"F{resolved['tapFeed']:g}{dwell_arg}"))
        return steps

    # ---------- 平台动作 ----------
    def _gesture_verifier(self):
        return self.gesture_motion_verifier or self.motion_verifier

    def _input_switch_allowed(self, calibration: dict) -> bool:
        """A-开关（2026-10-03 规格 §4.6）：候选/发送/删除清空的实际接触前检查。
        E 验收退役；校准悬停停靠等非输入动作不经过本方法，不会误拦。"""
        from runtime.input_settings import is_enabled
        device_id = self.device_id
        try:
            return is_enabled(device_id, self.data_root)
        except Exception:
            return False

    def _check_screen_motion_verifier(self) -> None:
        verifier = self._gesture_verifier()
        if getattr(verifier, "observation_only", False) is True:
            return
        if getattr(verifier, "measurement_kind", None) == "frame_change":
            raise DriverError(
                "driver_policy_rejected",
                "当前核验器只能测量画面变化，不能区分手机动画与机械运动；"
                "任意点手势尚缺可靠机械核验，本次未下发动作",
            )
        readiness = getattr(verifier, "readiness", None)
        if callable(readiness) and (reasons := readiness()):
            raise DriverError("driver_policy_rejected", "；".join(reasons))

    async def click(self, target: dict) -> dict:
        input_claim = target.pop("input_claim", None) if isinstance(target, dict) else None
        # R3-02：最终几何门随 target_ref 下沉（Runtime 在规划时构造）；在
        # 传输锁边界先几何后认领，由 _with_motion_lease 组合执行。
        target_ref = target.get("target_ref") if isinstance(target, dict) else None
        final_geometry_gate = None
        if isinstance(target_ref, dict):
            final_geometry_gate = target_ref.pop("finalGeometryGate", None)
        self._check_ready()
        generation = self._stop_generation
        target_id, entry, resolved, _calib, layout, dynamic_snapshot = \
            await self._resolve_action_target(target, allow_click_attempt=True)
        if (target_id in {"ime_candidate_bar", "ime_delete_key", "send_button"}
                and not self._input_switch_allowed(_calib)):
            raise DriverError("input_switch_disabled", "输入与发送未允许（开关关闭），禁止输入、清除或发送点击")
        pose = await self._check_live_calibration(wechat_send=target_id == "send_button")
        if pose is not None:
            frame = _calib["frame"]
            dynamic = entry.get("target_source") in {
                "camera_ocr", "vision_back_button", "camera_vision", "live_ime_geometry"}
            if entry.get("target_source") == "live_ime_geometry":
                # R2-10（FIX-15 §11.13.2）：live 目标已是手机屏幕像素，机械坐标
                # 只经 robotToScreen 逆映射（_resolve_live_geometry_target）——
                # 姿态守卫只核对手机位移（>2px=快照域失效，零接触），屏幕像素
                # 绝不再经相机→屏幕单应投影补偿（nx 是 display 域，乘相机帧宽
                # 属域混算）。
                movement = pose.get("movementPx") if isinstance(pose, dict) else None
                if not isinstance(movement, (int, float)) \
                        or float(movement) > 2.0:
                    error = DriverError(
                        "driver_uncalibrated",
                        "手机已偏移，live 几何快照域失效：请重新取得新鲜键盘几何，"
                        "原始屏幕坐标不做相机投影补偿")
                    error.motion_not_started = True
                    raise error
            else:
                self._compensate_point(
                    resolved, _calib, pose, entry["nx"] * frame["width"],
                    entry["ny"] * frame["height"],
                    already_live=dynamic and entry.get("coordinate_version") != "calibrated_camera.v1")
        with_tap = resolved["targetMode"] == "tap"
        if not with_tap and entry.get("target_source") in {"ui_fixed_target", "shared_layout"}:
            # 规格 §5.2/§7：hover_only 固定目标不允许经 arm.click 执行。
            # 旧 bundle targets 的 hover_only 维持既有"只走位不点击"语义（零回归）。
            raise DriverError(
                "driver_policy_rejected",
                f"目标 {target_id}（{entry.get('name_display')}）为 hover_only 固定目标："
                "不允许经 arm.click 执行，停靠请使用 arm.move_target")
        # FIX-01：认领必须真正进入下发边界——input_claim 随 click 传入租约，
        # 由 execute_command 仅在 tap（接触）命令的 sendall 前消费。
        # R3-02：最终几何门挂到 input_claim（geometry_gate），由 transport 在
        # 传输锁取得后、sendall 前按"几何门→输入认领"顺序同步执行；无认领
        # 记录的接触也照常过门（几何安全不依赖认领存在）。
        if final_geometry_gate is not None:
            if input_claim is None:
                input_claim = {"geometry_gate": final_geometry_gate}
            else:
                input_claim = dict(input_claim, geometry_gate=final_geometry_gate)
        contact_guard, command_guard = self._layout_motion_guards(
            layout, target_id, resolved, _calib, pose, wechat_send=target_id == "send_button",
            dynamic_snapshot=dynamic_snapshot)
        mpos, phases = await self._with_motion_lease(
            resolved, with_tap=with_tap, input_claim=input_claim,
            generation=generation,
            session=_calib.get("daemonSession", _calib.get("session")),
            contact_guard=contact_guard, command_guard=command_guard)
        verified = bool(phases) and all(
            p["verification"] and p["verification"].get("verified") for p in phases)
        unverified = any(p["verification"] is None or not p["verification"].get("verified")
                         for p in phases)
        outcome_hint = "unknown" if unverified else ("success" if verified else None)
        result = {
            "ok": True,
            "position": {k: mpos[k] for k in ("x", "y", "z") if k in mpos},
            "target_id": target_id,
            "target_display": entry.get("name_display") or TARGET_DISPLAY_NAMES.get(target_id, target_id),
            "target_source": entry.get("target_source", "fixed_target"),
            "target_source_display": {"camera_ocr": "摄像头 OCR 自动定位",
                                       "vision_back_button": "视觉规则自动定位返回键",
                                       "fixed_target": "设备固定目标",
                                       "shared_layout": "共享布局当次安全目标",
                                       "ui_fixed_target": "设备固定目标（校准后登记）"}.get(
                                           entry.get("target_source", "fixed_target"), "其他") ,
            "resolved_robot_xy": [resolved["robotX"], resolved["robotY"]],
            "click_attempt": entry["click_attempt"],
            "click_retry_supported": entry["click_retry_supported"],
            "click_point": entry["click_point"],
            "phases": phases,
            "motion_verified": verified,
            "outcome_hint": outcome_hint,
            "message": None if outcome_hint != "unknown"
            else "daemon 已确认指令，但摄像头未逐段确认真实移动，结果未知（不得当作成功）",
        }
        if layout is not None:
            result.update(layout_state=layout.state, **layout.selected.references)
        # 单独记录一次来源事件，便于调试台/审计读取，而不必依赖
        # 运动指令日志的具体条目顺序。
        self.action_log.append({
            "action": "click_source",
            "target_id": target_id,
            "target_source": result["target_source"],
            "target_source_display": result["target_source_display"],
            "ocr_text": entry.get("ocr_text"),
            "ocr_confidence": entry.get("ocr_confidence"),
            "frame_sha256": entry.get("frame_sha256"),
            "frame_seq": entry.get("frame_seq"),
            "coordinate_version": entry.get("coordinate_version"),
            "click_attempt": result["click_attempt"],
            "click_retry_supported": result["click_retry_supported"],
            "click_point": result["click_point"],
        })
        self.action_log.append({"action": "click", "target": dict(target), "result": result})
        return result

    async def double_tap(self, target: dict) -> dict:
        """在同一运动租约/事务内完成两次点击，避免双击被拆成两个动作。"""
        self._check_ready()
        if not isinstance(target, dict):
            raise DriverError("driver_gesture_invalid", "双击参数必须是对象")
        interval_ms = _finite(target.get("intervalMs", target.get(
            "doubleTapIntervalMs", DOUBLE_TAP_DEFAULT_INTERVAL_MS if "point" in target else 120)),
                              "双击间隔")
        if not DOUBLE_TAP_INTERVAL_MIN_MS <= interval_ms <= DOUBLE_TAP_INTERVAL_MAX_MS:
            raise DriverError(
                "driver_gesture_invalid",
                f"双击间隔必须为 {DOUBLE_TAP_INTERVAL_MIN_MS:g}~"
                f"{DOUBLE_TAP_INTERVAL_MAX_MS:g}ms",
            )
        generation = self._stop_generation
        normalized = "point" in target
        layout = None
        dynamic_snapshot = None
        if normalized:
            self._check_screen_motion_verifier()
            if set(target) - {"point", "binding", "intervalMs"}:
                raise DriverError("driver_gesture_invalid", "任意点双击禁止覆盖校准或混用固定目标")
            calib = await self._call(self._ensure_located)
            try:
                binding = screen_gestures.require_binding(target.get("binding"), calib)
                resolved = {**screen_gestures.resolve_point(target["point"], calib),
                            **screen_gestures.calibrated_profile(calib),
                            "targetMode": TARGET_MODE_TAP, "tapDwell": 0,
                            "_screenBinding": binding}
                screen_gestures.number(target.get("intervalMs", DOUBLE_TAP_DEFAULT_INTERVAL_MS),
                                       "双击抬起停顿")
            except (ValueError, TypeError, KeyError) as exc:
                raise DriverError("driver_gesture_invalid", str(exc)) from exc
            resolved["repeatLift"] = min(resolved["tapDepth"], DOUBLE_TAP_RELEASE_MM)
            target_id, entry = "gesture_point", {"name_display": "屏幕任意位置",
                                                 "target_source": "screen_normalized"}
        else:
            if "binding" in target:
                raise DriverError("driver_gesture_invalid", "绑定式双击必须使用归一化屏幕点")
            target_id, entry, resolved, calib, layout, dynamic_snapshot = \
                await self._resolve_action_target(target)
        if (target_id in {"ime_candidate_bar", "ime_delete_key", "send_button"}
                and not self._input_switch_allowed(calib)):
            raise DriverError("input_switch_disabled", "输入与发送未允许（开关关闭），禁止输入、清除或发送双击")
        pose = await self._check_live_calibration(wechat_send=target_id == "send_button")
        if pose is not None:
            if normalized:
                cx, cy = resolved["cameraX"], resolved["cameraY"]
            else:
                frame = calib["frame"]
                cx, cy = entry["nx"] * frame["width"], entry["ny"] * frame["height"]
            self._compensate_point(
                resolved, calib, pose, cx, cy,
                already_live=(entry.get("target_source") in {
                    "camera_ocr", "vision_back_button", "camera_vision"}
                    and entry.get("coordinate_version") != "calibrated_camera.v1"))
        if resolved["targetMode"] != TARGET_MODE_TAP:
            raise DriverError(
                "driver_policy_rejected",
                f"目标 {target_id}（{entry.get('name_display')}）不是可点击目标，拒绝双击",
            )
        await self._call(self._require_motion_feature, DOUBLE_TAP_FEATURE, "双击")
        if normalized:
            features = (await self._call(self._status)).get("motionFeatures") or []
            resolved["doubleTapFeed"] = (DOUBLE_TAP_FEED_MAX
                                        if DOUBLE_TAP_FAST_FEATURE in features
                                        else resolved["tapFeed"])
        contact_guard, command_guard = self._layout_motion_guards(
            layout, target_id, resolved, calib, pose, wechat_send=target_id == "send_button",
            dynamic_snapshot=dynamic_snapshot)
        mpos, phases = await self._with_motion_lease(
            resolved, with_tap=True, tap_count=2, tap_interval_s=interval_ms / 1000.0,
            generation=generation, session=calib.get("daemonSession", calib.get("session")),
            verifier=self._gesture_verifier(),
            validate_gesture_replies=True,
            allow_gesture_observation=True,
            contact_guard=contact_guard, command_guard=command_guard,
        )
        final_phase = ("double_tap_retract"
                       if resolved.get("repeatLift", resolved["tapDepth"]) < resolved["tapDepth"]
                       else "double_tap")
        controller_completed = (bool(phases)
                                and any(p["phase"] == "double_tap" for p in phases)
                                and phases[-1]["phase"] == final_phase
                                and all((p["verification"] or {}).get("stop_required") is not True
                                        for p in phases))
        verified = controller_completed and all(
            p["verification"] and p["verification"].get("verified") for p in phases)
        unverified = not controller_completed or any(
            p["verification"] is None or not p["verification"].get("verified") for p in phases)
        result = {
            "ok": True,
            "position": {k: mpos[k] for k in ("x", "y", "z") if k in mpos},
            "target_id": target_id,
            "target_display": entry.get("name_display") or TARGET_DISPLAY_NAMES.get(target_id, target_id),
            "target_source": entry.get("target_source", "fixed_target"),
            "resolved_robot_xy": [resolved["robotX"], resolved["robotY"]],
            "tap_count": 2 if controller_completed else 0,
            "controller_completed": controller_completed,
            "interval_ms": interval_ms,
            "interval_semantics": "release_pause",
            "repeat_lift_mm": resolved.get("repeatLift", resolved["tapDepth"]),
            "double_tap_feed": resolved.get("doubleTapFeed", resolved["tapFeed"]),
            "requested_double_tap_feed": DOUBLE_TAP_FEED_MAX if normalized else resolved["tapFeed"],
            "speed_note": ("当前常驻服务尚未加载双击 F3000，实际沿用校准速度；未重启定位服务"
                           if normalized and resolved["doubleTapFeed"] < DOUBLE_TAP_FEED_MAX else None),
            "contact_interval_min_ms": (
                2 * resolved.get("repeatLift", resolved["tapDepth"]) /
                resolved.get("doubleTapFeed", resolved["tapFeed"]) * 60000
                + interval_ms + resolved["tapDwell"] * 1000),
            "effect_status": "unverified",
            "phases": phases,
            "motion_verified": verified,
            "outcome_hint": "unknown" if unverified else ("success" if verified else None),
            "message": None if not unverified else
                "双击未完整执行或摄像头未确认真实动作，结果未知（不得当作成功）",
        }
        if layout is not None:
            result.update(layout_state=layout.state, **layout.selected.references)
        self.action_log.append({"action": "double_tap", "target": dict(target), "result": result})
        return result

    async def wake_screen(self, params: dict, *, execution_guard=None,
                          manual_confirmation=None) -> dict:
        """One manual double tap at a verified point; no unlock or fallback action."""
        from drivers.wake_screen import (
            WakeScreenObserver, accepted_double_tap_feed, resolve_wake_target,
            validate_manual_confirmation,
        )

        def check_execution():
            if execution_guard is not None:
                try:
                    execution_guard()
                except Exception as exc:
                    # Only the motion transaction can attest that nothing moved.
                    raise DriverError(
                        getattr(exc, "category", "driver_policy_rejected"), str(exc),
                    ) from exc

        try:
            self._check_ready()
            check_execution()
            if (not isinstance(params, dict) or set(params) != {"binding", "confirmed"}
                    or params.get("confirmed") is not True):
                raise DriverError("driver_gesture_invalid", "唤醒仅接受当前绑定和明确人工确认")
            generation = self._stop_generation
            calib = await self._call(self._ensure_located)
            try:
                binding = screen_gestures.require_binding(params["binding"], calib)
                target = resolve_wake_target(calib)
                resolved = {
                    **screen_gestures.resolve_point(
                        {"source": "screen_normalized", **target["point"]}, calib),
                    **screen_gestures.calibrated_profile(calib),
                    "targetMode": TARGET_MODE_TAP, "tapDwell": 0, "_screenBinding": binding,
                }
            except (ValueError, TypeError, KeyError) as exc:
                raise DriverError("driver_gesture_invalid", str(exc)) from exc
            # W13/§9.3：单次人工现场确认只豁免本任务的实时手机姿态；过期、
            # 绑定/设备/参数摘要不一致即撤销，不豁免新鲜帧、急停或租约。
            manual_reference = validate_manual_confirmation(
                manual_confirmation, target, self.device_id)
            observer = WakeScreenObserver(
                self.screen_wake_gate,
                lambda: target_archive.load_runtime_calibration(self.device_id, self.data_root),
                target, calib, require_live_pose=manual_reference is None,
            )
            await observer.sample(require_dark=True)
            # §9.3 决策表：暗屏但姿态可观测（机身/屏幕仍可定位）→ 补偿到当前
            # 位置后允许一次双击（W08）；全黑或姿态不可观测 → 等待，自动唤醒
            # 不盲动，仅单次人工现场确认生效时可使用冻结的已验证位置（W09）。
            pose = observer.pose
            if pose is not None:
                self._compensate_point(
                    resolved, calib, pose, resolved["cameraX"], resolved["cameraY"])
            elif manual_reference is None:
                raise DriverError(
                    "driver_camera_waiting",
                    "暗屏手机当前姿态不可观测，自动唤醒不盲动；等待画面恢复或完成单次人工现场确认")
            elif isinstance(manual_reference.get("reference_pose"), dict):
                # 人工确认冻结的参考变换（相同校准基准下最后已验证姿态）。
                self._compensate_point(
                    resolved, calib, manual_reference["reference_pose"],
                    resolved["cameraX"], resolved["cameraY"])
            check_execution()
            await self._call(self._require_motion_feature, DOUBLE_TAP_FEATURE, "双击")
            features = (await self._call(self._status)).get("motionFeatures") or []
            resolved["repeatLift"] = min(resolved["tapDepth"], DOUBLE_TAP_RELEASE_MM)
            # W03/§9.2：daemon 上报 tap2.feed-3000.v1 不等于可以提速；只有
            # 校准档案存有与当前校准 run 绑定的双击速度验收证据才用快双击，
            # 否则沿用本轮验证的校准速度，接触平面不变。
            accepted_feed = accepted_double_tap_feed(calib)
            if (accepted_feed is not None
                    and DOUBLE_TAP_FAST_FEATURE in features
                    and accepted_feed <= DOUBLE_TAP_FEED_MAX):
                resolved["doubleTapFeed"] = accepted_feed
                feed_source = "calibrated_double_tap_evidence"
                speed_note = None
            else:
                resolved["doubleTapFeed"] = resolved["tapFeed"]
                feed_source = "calibrated_tap_feed"
                speed_note = ("daemon 支持快双击，但本轮校准没有与当前校准绑定的双击速度验收证据，"
                              "沿用本轮验证的校准速度"
                              if accepted_feed is None and DOUBLE_TAP_FAST_FEATURE in features
                              else None)
        except DriverError as exc:
            exc.motion_not_started = True
            raise
        mpos, phases = await self._with_motion_lease(
            resolved, with_tap=True, tap_count=2,
            tap_interval_s=DOUBLE_TAP_DEFAULT_INTERVAL_MS / 1000,
            generation=generation, session=calib["daemonSession"], verifier=observer,
            execution_guard=check_execution,
            contact_guard=lambda: observer.sample(require_dark=not observer.pressed),
            validate_gesture_replies=True, allow_gesture_observation=True,
        )
        last = ("double_tap_retract" if resolved["repeatLift"] < resolved["tapDepth"]
                else "double_tap")
        completed = (bool(phases) and any(p["phase"] == "double_tap" for p in phases)
                     and phases[-1]["phase"] == last
                     and all((p["verification"] or {}).get("stop_required") is not True
                             for p in phases))
        result = {
            "ok": completed, "controller_completed": completed,
            "mechanical_status": "completed" if completed else "unknown",
            "motion_verified": False, "tap_count": 2 if completed else 0,
            "effect_status": observer.effect_status() if completed else "unverified",
            "validation_point_id": target["validation_point_id"],
            "target_source": target.get("target_source", "validation_point"),
            "position": {key: mpos[key] for key in ("x", "y", "z") if key in mpos},
            "double_tap_feed": resolved["doubleTapFeed"],
            "double_tap_feed_source": feed_source,
            "manual_position_confirmed": manual_reference is not None,
            "position_reference": (
                "manual_frozen_reference"
                if manual_reference is not None and pose is None else "live_pose"),
            "speed_note": speed_note,
            "phases": phases, "frame_observations": observer.frames,
            "outcome_hint": None if completed else "unknown",
            "message": "仅执行唤醒双击；亮屏不表示解锁，不自动重试",
        }
        self.action_log.append({"action": "wake_screen", "result": result})
        return result

    def _resolve_gesture_point(self, point: dict, calibration: dict,
                               profile: dict, *, label: str) -> dict:
        if not isinstance(point, dict):
            raise DriverError("driver_gesture_invalid", f"{label}必须是对象")
        source = point.get("source", "camera_gesture")
        if source not in {"camera_gesture", "camera_ocr", "camera_vision"}:
            raise DriverError("driver_gesture_invalid", f"{label}来源不允许：{source}")
        frame = calibration.get("frame") or {}
        frame_w = _finite(point.get("frameW", frame.get("width")), f"{label}帧宽")
        frame_h = _finite(point.get("frameH", frame.get("height")), f"{label}帧高")
        x = point.get("cameraX", point.get("x"))
        y = point.get("cameraY", point.get("y"))
        if x is None or y is None:
            raise DriverError("driver_gesture_invalid", f"{label}缺少 cameraX/cameraY")
        x = _finite(x, f"{label} X")
        y = _finite(y, f"{label} Y")
        if frame_w <= 0 or frame_h <= 0 or not 0 <= x <= frame_w or not 0 <= y <= frame_h:
            raise DriverError("driver_gesture_invalid", f"{label}坐标超出摄像头画面")
        resolved = target_archive.resolve_target(
            {"nx": x / frame_w, "ny": y / frame_h},
            calibration,
            profile,
        )
        return {
            "robotX": _finite(resolved["robotX"], f"{label}机械 X"),
            "robotY": _finite(resolved["robotY"], f"{label}机械 Y"),
            "cameraX": x,
            "cameraY": y,
            "frameW": frame_w,
            "frameH": frame_h,
        }

    @staticmethod
    def _apply_stroke_mm(resolved: dict, gesture: dict) -> None:
        """用户可调滑动长度（拍板 2026-09-30）：strokeMm 显式给定行程长度，
        方向保持 start→end，终点沿向量缩放；缺省不缩放（用标记点自然距离）。
        必须在端点定稿（位姿补偿）之后调用（N-1，R2 复审）。"""
        if gesture.get("strokeMm") is None:
            return
        stroke_mm = _finite(float(gesture["strokeMm"]), "滑动长度")
        if stroke_mm <= 0:
            raise DriverError("driver_gesture_invalid", "滑动长度必须为正数")
        dx0 = resolved["endRobotX"] - resolved["startRobotX"]
        dy0 = resolved["endRobotY"] - resolved["startRobotY"]
        length0 = (dx0 * dx0 + dy0 * dy0) ** 0.5
        if length0 <= 0:
            raise DriverError("driver_gesture_invalid", "滑动起终点重合，无法按长度缩放")
        scale = stroke_mm / length0
        resolved["endRobotX"] = resolved["startRobotX"] + dx0 * scale
        resolved["endRobotY"] = resolved["startRobotY"] + dy0 * scale
        resolved["strokeMm"] = stroke_mm

    async def swipe(self, gesture: dict, *, execution_guard=None) -> dict:
        """以现有视觉标定执行一次原子滑动：悬停到起点、接触、拖动、抬起。"""
        if execution_guard is not None:
            execution_guard()
        self._check_ready()
        if not isinstance(gesture, dict):
            raise DriverError("driver_gesture_invalid", "滑动参数必须是对象")
        duration_ms = _finite(gesture.get("durationMs", 600), "滑动时长")
        if not SWIPE_DURATION_MIN_MS <= duration_ms <= SWIPE_DURATION_MAX_MS:
            raise DriverError(
                "driver_gesture_invalid",
                f"滑动时长必须为 {SWIPE_DURATION_MIN_MS:g}~{SWIPE_DURATION_MAX_MS:g}ms",
            )
        generation = self._stop_generation
        start = gesture.get("start")
        end = gesture.get("end")
        calibration = await self._call(self._ensure_located)
        profile = await self._call(
            target_archive.load_profile, self.device_id, self.data_root)
        normalized = ("binding" in gesture or any(
            isinstance(p, dict) and p.get("source") == "screen_normalized" for p in (start, end)))
        binding = None
        if normalized:
            self._check_screen_motion_verifier()
            if set(gesture) - {"start", "end", "binding", "durationMs", "strokeMm"}:
                raise DriverError("driver_gesture_invalid", "任意点滑动禁止覆盖现有机械参数")
            try:
                # FIX-07：绑定比较显式 v2/v3 分支——十字段 v3 绑定走
                # require_binding_v3 语义比较（仅排除诊断 revision）；
                # 八字段旧绑定原样走 require_binding，客户端不得在
                # binding 内自带模式开关。
                requested_binding = gesture.get("binding")
                if isinstance(requested_binding, dict) and set(requested_binding) == set(
                        screen_gestures.BINDING_V3_KEYS):
                    binding = screen_gestures.require_binding_v3(requested_binding, calibration)
                else:
                    binding = screen_gestures.require_binding(requested_binding, calibration)
                start_ref = screen_gestures.resolve_point(start, calibration)
                end_ref = screen_gestures.resolve_point(end, calibration)
                screen_gestures.validate_swipe_path(start_ref, end_ref, calibration)
                profile = screen_gestures.calibrated_profile(calibration)
                if "durationMs" in gesture:
                    screen_gestures.number(gesture["durationMs"], "滑动时长")
            except (ValueError, TypeError, KeyError) as exc:
                raise DriverError("driver_gesture_invalid", str(exc)) from exc
        else:
            start_ref = await self._call(
                self._resolve_gesture_point, start, calibration, profile, label="滑动起点")
            end_ref = await self._call(
                self._resolve_gesture_point, end, calibration, profile, label="滑动终点")
        resolved = {
            "startRobotX": start_ref["robotX"],
            "startRobotY": start_ref["robotY"],
            "endRobotX": end_ref["robotX"],
            "endRobotY": end_ref["robotY"],
            "hoverZ": profile["hoverZ"],
            "tapDepth": gesture.get("tapDepth", profile["tapDepth"]),
            "tapFeed": gesture.get("tapFeed", profile["tapFeed"]),
            "durationMs": duration_ms,
        }
        # 用户可调滑动长度（拍板 2026-09-30；N-1 2026-09-30 R2 复审）：
        # strokeMm 缩放必须在位姿补偿覆写端点之后应用（真机 gate 常在，
        # 提前缩放会被 refs 补偿丢弃=静默 no-op），见下方两处应用点。
        if normalized:
            resolved["_screenBinding"] = binding
        pose = await self._check_live_calibration(wechat_send=False)
        if pose is not None:
            try:
                for ref, prefix in ((start_ref, "start"), (end_ref, "end")):
                    source = (start if prefix == "start" else end) or {}
                    raw_live = (not normalized and source.get("source") in {
                            "camera_ocr", "camera_vision"}
                            and source.get("coordinateVersion") != "calibrated_camera.v1")
                    if raw_live and float(pose.get("movementPx", float("inf"))) > 2.0:
                        raise ValueError(
                            "原始摄像头手势点缺少观测时手机姿态，请重新识别校准坐标")
                    rx, ry = screen_pose.physical_point(
                        calibration, pose, ref["cameraX"], ref["cameraY"],
                        already_live=raw_live)
                    resolved[f"{prefix}RobotX"] = rx
                    resolved[f"{prefix}RobotY"] = ry
                # N-1（R2 复审）：补偿覆写端点之后重施 strokeMm 缩放，
                # 33 段路径检查据此验证最终行程（含放大到 150mm 的出屏防护）。
                self._apply_stroke_mm(resolved, gesture)
                # The entire straight physical stroke must remain on the
                # freshly located phone, not merely its endpoints.
                model = calibration["xy"]["robotToScreen"]
                homography = np.asarray(
                    calibration["camera"]["cameraToScreen"]["homography"], dtype=float)
                inv = np.linalg.inv(homography)
                corners = np.asarray(pose["corners"], dtype=np.float32)
                import cv2
                for fraction in (i / 32 for i in range(33)):
                    rx = resolved["startRobotX"] + fraction * (
                        resolved["endRobotX"] - resolved["startRobotX"])
                    ry = resolved["startRobotY"] + fraction * (
                        resolved["endRobotY"] - resolved["startRobotY"])
                    sx, sy = calibration_bundle.screen_from_robot(model, rx, ry)
                    pixel = inv @ [sx, sy, 1]
                    cx, cy = pixel[:2] / pixel[2]
                    if cv2.pointPolygonTest(corners, (float(cx), float(cy)), True) < 0:
                        raise ValueError("滑动路径超出当前手机屏幕")
            except (ValueError, TypeError, KeyError, np.linalg.LinAlgError) as exc:
                error = DriverError("driver_uncalibrated", f"手机偏移补偿失败：{exc}")
                error.motion_not_started = True
                raise error from exc
        else:
            # 无补偿（gate=None，仿真/测试路径）：解析端点即最终端点
            self._apply_stroke_mm(resolved, gesture)
        # 端点定稿（补偿+缩放）之后统一计算行程/时长/速度
        dx = resolved["endRobotX"] - resolved["startRobotX"]
        dy = resolved["endRobotY"] - resolved["startRobotY"]
        distance = math.hypot(dx, dy)
        if normalized and "durationMs" not in gesture:
            try:
                duration_ms = screen_gestures.automatic_swipe_duration(distance)
            except ValueError as exc:
                raise DriverError("driver_gesture_invalid", str(exc)) from exc
            resolved["durationMs"] = duration_ms
        derived_feed = distance / (duration_ms / 60000.0) if distance > 0 else 0
        resolved["swipeFeed"] = gesture.get("feed", derived_feed)
        try:
            resolved["swipeFeed"] = _finite(resolved["swipeFeed"], "滑动速度")
        except DriverError:
            raise
        await self._call(self._require_motion_feature, SWIPE_FEATURE, "滑动")
        session = calibration.get("daemonSession", calibration.get("session"))
        mpos, phases = await self._with_motion_lease(
            resolved,
            with_tap=False,
            generation=generation,
            session=session,
            precheck=lambda item, current: self._precheck_swipe_motion(item, current),
            build_steps=self._build_swipe_steps,
            verifier=self._gesture_verifier(),
            validate_gesture_replies=True,
            allow_gesture_observation=True,
            contact_guard=self._contact_pose_guard(pose),
            execution_guard=execution_guard,
            command_guard=(lambda _position: execution_guard())
            if execution_guard is not None else None,
        )
        controller_completed = (bool(phases) and phases[-1]["phase"] == "swipe"
                                and all((p["verification"] or {}).get("stop_required") is not True
                                        for p in phases))
        verified = controller_completed and all(
            p["verification"] and p["verification"].get("verified") for p in phases)
        unverified = not controller_completed or any(
            p["verification"] is None or not p["verification"].get("verified") for p in phases)
        result = {
            "ok": True,
            "position": {k: mpos[k] for k in ("x", "y", "z") if k in mpos},
            "start": start,
            "end": end,
            "resolved_start_robot_xy": [resolved["startRobotX"], resolved["startRobotY"]],
            "resolved_end_robot_xy": [resolved["endRobotX"], resolved["endRobotY"]],
            "distance_mm": distance,
            "duration_ms": duration_ms,
            "swipe_executed": controller_completed,
            "controller_completed": controller_completed,
            "effect_status": "unverified",
            "phases": phases,
            "motion_verified": verified,
            "outcome_hint": "unknown" if unverified else ("success" if verified else None),
            "message": None if not unverified else
                "滑动未完整执行或摄像头未确认真实动作，结果未知（不得当作成功）",
        }
        self.action_log.append({"action": "swipe", "gesture": dict(gesture), "result": result})
        return result

    async def execute_gesture_program(self, plan: dict) -> dict:
        """连续路径程序（path1 计划）：同一租约事务内逐 stroke 执行 PATH1 宏。

        stroke 之间在悬停状态衔接（完整抬起回悬停再走位到下一起点）；
        任一 stroke 失败，剩余 stroke 不得执行（沿用 _with_motion_lease 失败路径）。
        """
        self._check_ready()
        if not isinstance(plan, dict):
            raise DriverError("driver_gesture_invalid", "连续路径计划必须是对象")
        if plan.get("execution_mode") != "path1":
            raise DriverError("driver_gesture_invalid", "连续路径计划必须是 path1 执行模式")
        strokes = plan.get("strokes")
        if not isinstance(strokes, list) or not strokes:
            raise DriverError("driver_gesture_invalid", "连续路径计划缺少 strokes")
        for index, stroke in enumerate(strokes):
            if (not isinstance(stroke, dict)
                    or not isinstance(stroke.get("segments"), list) or not stroke["segments"]
                    or not isinstance(stroke.get("mech_points"), list) or not stroke["mech_points"]):
                raise DriverError(
                    "driver_gesture_invalid",
                    f"第 {index + 1} 段 stroke 缺少 segments 或 mech_points")
        profile_in = plan.get("profile")
        if not isinstance(profile_in, dict):
            raise DriverError("driver_gesture_invalid", "连续路径计划缺少 profile")
        profile = {key: _finite(profile_in.get(key), f"连续路径 {key}")
                   for key in ("hoverZ", "tapDepth", "tapFeed")}
        try:
            check_tap(profile["tapDepth"])
            check_feed(profile["tapFeed"], axis="Z")
        except ValueError as exc:
            raise DriverError("driver_limit_exceeded", str(exc)) from exc
        payloads = []
        for index, stroke in enumerate(strokes):
            payload = {"version": 1, "depth": profile["tapDepth"],
                       "z_feed": profile["tapFeed"], "segments": stroke["segments"]}
            try:
                payloads.append(validate_path1_payload(payload))
            except ValueError as exc:
                raise DriverError(
                    "driver_gesture_invalid",
                    f"第 {index + 1} 段 PATH1 校验失败：{exc}") from exc

        self._check_screen_motion_verifier()
        generation = self._stop_generation
        calibration = await self._call(self._ensure_located)
        try:
            binding = screen_gestures.require_binding(plan.get("binding"), calibration)
        except (ValueError, TypeError, KeyError) as exc:
            raise DriverError("driver_uncalibrated", str(exc)) from exc
        # 末道门禁：profile 必须由当前标定推导（与双击/滑动同规则），
        # 外部 plan 自带的 Z 参数即使量程合法也不放行（裸位移属于调试接口）
        expected_profile = screen_gestures.calibrated_profile(calibration)
        for key in ("hoverZ", "tapDepth", "tapFeed"):
            if abs(profile[key] - float(expected_profile[key])) > 1e-9:
                raise DriverError(
                    "driver_gesture_invalid",
                    f"连续路径计划 profile.{key}={profile[key]} 与当前标定推导值 "
                    f"{expected_profile[key]} 不一致，拒绝执行外部自定 Z 参数")
        try:
            self._validate_path1_waypoints(strokes, calibration)
        except ValueError as exc:
            raise DriverError("driver_gesture_invalid", str(exc)) from exc
        pose = await self._check_live_calibration(wechat_send=False)
        if pose is not None and pose["movementPx"] > 1:
            raise DriverError("driver_uncalibrated",
                              "连续路径计划使用旧机械坐标；手机偏移后必须重新规划")
        await self._call(self._require_motion_feature, PATH1_FEATURE, "路径")

        resolved = {**profile, "_strokes": strokes, "_payloads": payloads,
                    "_screenBinding": binding}
        log_marker = len(self.action_log)
        mpos, phases = await self._with_motion_lease(
            resolved, with_tap=False,
            session=calibration.get("daemonSession", calibration.get("session")),
            generation=generation,
            precheck=lambda item, current: self._precheck_path_motion(item, current),
            build_steps=self._build_path_steps,
            verifier=self._gesture_verifier(),
            validate_gesture_replies=False,
            allow_gesture_observation=True,
            contact_guard=self._contact_pose_guard(pose),
        )
        stroke_commands = [cmd for _label, cmd in
                           ((p["phase"], p["cmd"]) for p in phases)
                           if str(cmd).startswith("PATH1 ")]
        controller_completed = (bool(phases)
                                and phases[-1]["phase"].startswith("path_stroke_")
                                and len(stroke_commands) == len(strokes)
                                and all((p["verification"] or {}).get("stop_required") is not True
                                        for p in phases))

        def fail_unknown(message: str) -> DriverError:
            # 回包无法证明完成：按结果未知处理，强制重连后才允许新的动作。
            transport = self._transport
            if transport is not None:
                transport.close()
            self._connected = False
            return DriverError("driver_disconnected", message)

        stroke_results: list[dict] = []
        if controller_completed:
            path_entries = [entry for entry in self.action_log[log_marker:]
                            if isinstance(entry, dict)
                            and str(entry.get("cmd", "")).startswith("PATH1 ")]
            if len(path_entries) != len(strokes):
                raise fail_unknown("PATH1 回包记录与 stroke 数不符，结果未知，禁止重放动作")
            for stroke, entry in zip(strokes, path_entries):
                reply_text = entry.get("resp")
                try:
                    reply = json.loads(reply_text) if isinstance(reply_text, str) else None
                except ValueError:
                    reply = None
                try:
                    self._validate_path1_reply(reply, len(stroke["segments"]))
                except DriverError as exc:
                    raise fail_unknown(exc.message) from exc
                # 逐段终点核对：回包自带实际坐标，中间 stroke 漂移不能只靠
                # 最终位姿 0.05mm 兜底（否则末段之前的偏差会被末段走位掩盖）
                end = stroke["mech_points"][-1]
                end_x = _finite(end.get("robotX"), "PATH1 段终点 X")
                end_y = _finite(end.get("robotY"), "PATH1 段终点 Y")
                reply_x, reply_y = reply.get("x"), reply.get("y")
                if (not isinstance(reply_x, (int, float))
                        or not isinstance(reply_y, (int, float))
                        or abs(reply_x - end_x) > GESTURE_POSITION_TOLERANCE_MM
                        or abs(reply_y - end_y) > GESTURE_POSITION_TOLERANCE_MM):
                    deltas = (f"x={reply_x - end_x:.3f}" if isinstance(reply_x, (int, float))
                              else "x=缺失",
                              f"y={reply_y - end_y:.3f}" if isinstance(reply_y, (int, float))
                              else "y=缺失")
                    raise fail_unknown(
                        f"PATH1 第 {len(stroke_results) + 1} 段终点与预期不符"
                        f"（{deltas[0]}mm，{deltas[1]}mm），结果未知，禁止重放动作")
                stroke_results.append({
                    "index": len(stroke_results),
                    "segments_completed": reply.get("segments_completed"),
                    "path_completed": reply.get("path_completed"),
                    "contact_released": reply.get("contact_released"),
                })
            last = strokes[-1]["mech_points"][-1]
            expected = {"x": _finite(last.get("robotX"), "PATH1 终点 X"),
                        "y": _finite(last.get("robotY"), "PATH1 终点 Y"),
                        "z": profile["hoverZ"]}
            if any(abs(mpos[axis] - expected[axis]) > GESTURE_POSITION_TOLERANCE_MM
                   for axis in ("x", "y", "z")):
                deltas = ", ".join(f"{axis}={mpos[axis] - expected[axis]:.3f}"
                                   for axis in ("x", "y", "z"))
                raise fail_unknown(f"PATH1 终点与预期不符（{deltas}mm），结果未知，禁止重放动作")

        verified = controller_completed and all(
            p["verification"] and p["verification"].get("verified") for p in phases)
        unverified = not controller_completed or any(
            p["verification"] is None or not p["verification"].get("verified") for p in phases)
        result = {
            "ok": True,
            "position": {k: mpos[k] for k in ("x", "y", "z") if k in mpos},
            "execution_mode": "path1",
            "stroke_count": len(strokes) if controller_completed else 0,
            "controller_status": "completed" if controller_completed else "unknown",
            "controller_completed": controller_completed,
            "stroke_results": stroke_results,
            "effect_status": "unverified",
            "phases": phases,
            "motion_verified": verified,
            "outcome_hint": "unknown" if unverified else ("success" if verified else None),
            "message": None if not unverified else
            "连续路径未完整执行或摄像头未确认真实动作，结果未知（不得当作成功）",
        }
        self.action_log.append({"action": "execute_gesture_program", "result": result})
        return result

    async def test_calibrated_double_tap(self, params: dict) -> dict:
        """校准双击自检（规范第 22.17 节）：复用任意点双击的原子事务路径。

        只是把结果包装成自检回执；旧 double_tap 的返回语义不变。
        """
        required = {"run_id", "trial_id", "plan_id", "binding", "point", "interval_ms"}
        if not isinstance(params, dict) or set(params) != required:
            raise DriverError(
                "driver_gesture_invalid",
                "校准双击自检参数有且仅有 run_id/trial_id/plan_id/binding/point/interval_ms")
        result = await self.double_tap({
            "point": params["point"],
            "binding": params["binding"],
            "intervalMs": params["interval_ms"],
        })
        controller_completed = result.get("controller_completed") is True
        return {
            "plan_id": params["plan_id"],
            "run_id": params["run_id"],
            "trial_id": params["trial_id"],
            "binding": params["binding"],
            "controller_status": "completed" if controller_completed else "unknown",
            "final_mpos": result.get("position"),
            "phases": result.get("phases", []),
            # 底层结果未知（恰是笔尖可能停在屏上的情形）时不得伪造已抬离证据
            "contact_released": True if controller_completed else None,
            "motion_verified": result.get("motion_verified"),
            "outcome_hint": result.get("outcome_hint"),
        }

    async def move_target(self, target_id: str, *, expected_layout=_LAYOUT_UNSET) -> dict:
        """移动到设备目标并停在悬停高度（只移动，不执行 TAP）。"""
        return await self._move_only(target_id, expected_layout=expected_layout)

    async def park(self) -> dict:
        """驶回 SAFE PARK（校准档案确认的停止位）。

        走业务租约与现有运动链（预检/分段/会话锚点），不依赖调试台——
        调试台停靠会占 debug 运动租约 ~90s，与后续业务动作互斥。终点以
        MPos 对账：到位才算 ok。
        """
        self._check_ready()
        generation = self._stop_generation
        calibration = await self._call(self._ensure_located)
        park = calibration.get("safePark") or {}
        if not park.get("confirmed") or park.get("parkX") is None:
            raise DriverError("driver_policy_rejected",
                              "SAFE PARK 未在校准档案确认，拒绝回停")
        profile = await self._call(
            target_archive.load_profile, self.device_id, self.data_root)
        resolved = {
            "robotX": float(park["parkX"]),
            "robotY": float(park["parkY"]),
            "hoverZ": float(park.get("parkZ", 0.0)),
            "tapDepth": float(profile.get("tapDepth") or 0.0),
            "tapFeed": float(profile.get("tapFeed") or 1500.0),
        }

        def _build_park_steps(res: dict, current: dict) -> list[tuple[str, str]]:
            steps = []
            dx = res["robotX"] - current["x"]
            dy = res["robotY"] - current["y"]
            if abs(dx) > 0.05 or abs(dy) > 0.05:
                steps.append(("park_travel_xy",
                              f"XY {dx:.3f} {dy:.3f} F{CALIBRATED_XY_FEED:g}"))
            dz = res["hoverZ"] - current["z"]
            if abs(dz) >= 0.05:
                for chunk in split_segments(dz, HOVER_SEGMENT_MAX_MM):
                    steps.append(("park_z", f"Z {chunk:.3f} F1500"))
            return steps

        await self._check_live_calibration(wechat_send=False,
                                           screen_edge_advisory=True)
        mpos, phases = await self._with_motion_lease(
            resolved, with_tap=False, generation=generation,
            session=calibration.get("daemonSession", calibration.get("session")),
            build_steps=_build_park_steps,
            verifier=self.motion_verifier,
            validate_gesture_replies=False)
        arrived = all(abs(mpos.get(axis, 1e9) - resolved[key]) <= 0.5
                      for axis, key in (("x", "robotX"), ("y", "robotY"),
                                        ("z", "hoverZ")))
        self.action_log.append({"action": "park", "result": {"mpos": mpos,
                                                              "arrived": arrived}})
        return {"ok": arrived, "position": {k: mpos[k] for k in ("x", "y", "z")
                                            if k in mpos},
                "park": True, "arrived": arrived, "phases": phases}

    async def _move_only(self, target_id: str, *, expected_layout=_LAYOUT_UNSET) -> dict:
        self._check_ready()
        generation = self._stop_generation
        target = {"target_id": target_id}
        if expected_layout is not _LAYOUT_UNSET:
            target["_layout_binding"] = expected_layout
        target_id, entry, resolved, _calib, layout, _dynamic_snapshot = \
            await self._resolve_action_target(target)
        pose = await self._check_live_calibration(wechat_send=False)
        if pose is not None:
            frame = _calib["frame"]
            self._compensate_point(
                resolved, _calib, pose, entry["nx"] * frame["width"],
                entry["ny"] * frame["height"])
        contact_guard, command_guard = self._layout_motion_guards(
            layout, target_id, resolved, _calib, pose,
            dynamic_snapshot=_dynamic_snapshot)
        mpos, phases = await self._with_motion_lease(
            resolved, with_tap=False, generation=generation,
            session=_calib.get("daemonSession", _calib.get("session")),
            contact_guard=contact_guard, command_guard=command_guard)
        # B 组补充（向后兼容）：结果带出目标动作模式与“未点击”标记，
        # 供插件在 park 步骤里核验 hover_only；坐标解析与运动分段逻辑未变。
        return {
            "ok": True,
            "position": {k: mpos[k] for k in ("x", "y", "z") if k in mpos},
            "target_id": target_id,
            "target_display": entry.get("name_display") or TARGET_DISPLAY_NAMES.get(target_id, target_id),
            "target_mode": entry.get("targetMode") or DEFAULT_TARGET_MODES.get(target_id, "tap"),
            "tap_executed": False,
            "target_source": entry.get("target_source", "fixed_target"),
            "resolved_robot_xy": [resolved["robotX"], resolved["robotY"]],
            "phases": phases,
            "motion_verified": bool(phases) and all(
                p["verification"] and p["verification"].get("verified") for p in phases),
            "outcome_hint": "success" if phases and all(
                p["verification"] and p["verification"].get("verified") for p in phases) else "unknown",
            "message": "已移动到目标并停在悬停高度（未执行点击）",
            **({"layout_state": layout.state, **layout.selected.references} if layout is not None else {}),
        }

    async def go_home(self) -> dict:
        """无限位设备：停止并保持当前位置，不宣称硬件回零（设计 v1.2 §11.5）。"""
        self._check_ready()
        mpos = await self._call(self._mpos)
        self.action_log.append({"action": "go_home", "home_mode": "hold_position", "position": mpos})
        return {
            "ok": True,
            "position": {k: mpos[k] for k in ("x", "y", "z") if k in mpos},
            "home_mode": "保持当前位置（无限位设备不执行回零）",
            "message": "聊天结束停靠请由插件显式调用 move_target('ime_delete_key')",
        }

    async def move_preset(self, position_id: str) -> dict:
        raise DriverError(
            "driver_policy_rejected",
            "move_preset 属于设备上线前的调试/验收流程（G 阶段），不属于业务运行时能力；"
            "正式标定与方向验证通过对应 daemon 的调试接口（电机调试台）执行")

    def _control_cmd(self, line: str, *, reset: bool = False) -> dict:
        transport = DaemonTCPTransport(self.daemon_addr, timeout=CONTROL_TIMEOUT_S)
        deadline = time.monotonic() + CONTROL_TIMEOUT_S

        def command(text):
            return json_loads(transport.cmd(
                text, timeout=transport._remaining(deadline)))

        try:
            transport.open()
            status = command("STATUS")
            self._check_identity(status)
            response = command(line)
            self.action_log.append({"cmd": line, "resp": response, "control_connection": True})
            if reset:
                status = command("STATUS")
                self._check_identity(status)
                if (response.get("estop_locked") is not False
                        or status.get("estopLocked") is not False
                        or status.get("connected") is not True
                        or not isinstance(status.get("session"), str)
                        or not status["session"].strip()):
                    raise DriverError("driver_estop_latched", "daemon 急停复位未确认")
                position = self._position(command("MPOS"))
                if position.get("state") != "Idle":
                    raise DriverError("driver_estop_latched", "控制器未处于 Idle，保持本地急停锁定")
            return response
        except OSError as exc:
            raise DriverError("driver_disconnected", "daemon 控制指令超时，结果未知") from exc
        finally:
            transport.close()

    def _interrupt_motion(self, *, latch: bool = False) -> None:
        with self._safety_lock:
            self._stop_generation += 1
            if latch:
                self._estop_latched = True
        if self._motion_lock.locked() and self._transport is not None:
            self._transport.close()
            self._connected = False

    async def stop(self) -> dict:
        """软停止：daemon STOP（进给保持）。不受租约/锁定阻塞。"""
        self._interrupt_motion()
        if self._transport is None:
            message = "daemon 连接未建立，无法发送停止命令；结果未知"
            return {
                "ok": False, "stopped": False, "outcome_hint": "unknown",
                "error": message, "message": message,
            }
        try:
            resp = await asyncio.to_thread(self._control_cmd, "STOP")
        except DriverError as exc:
            return {
                "ok": False, "stopped": False, "outcome_hint": "unknown",
                "error": exc.message, "message": f"停止命令失败：{exc.message}",
            }
        self.action_log.append({"action": "stop", "resp": resp})
        stopped = resp.get("stopped") is True
        outcome_hint = resp.get("outcome_hint")
        error = resp.get("error")
        if not stopped and outcome_hint is None:
            outcome_hint = "unknown"
        message = ("已发送进给保持；如需彻底停止请使用硬件急停"
                   if stopped else f"停止命令未确认：{error or '结果未知'}")
        return {"ok": stopped, "stopped": stopped, "outcome_hint": outcome_hint,
                "error": error, "message": message}

    async def estop(self) -> dict:
        """急停：daemon 级锁定（所有客户端普通运动被拒）+ 平台本地 latch。"""
        self._interrupt_motion(latch=True)
        self.action_log.append({"action": "estop"})
        daemon_locked = False
        error = None
        try:
            response = await asyncio.to_thread(self._control_cmd, "ESTOP_LOCK 平台业务急停")
            daemon_locked = response.get("estop_locked") is True
            stopped = response.get("stopped") is True
            outcome_hint = response.get("outcome_hint")
            error = response.get("error")
        except DriverError as exc:
            error = exc.message
            stopped = False
            outcome_hint = "unknown"
        else:
            if not stopped and outcome_hint is None:
                outcome_hint = "unknown"
        return {
            "ok": daemon_locked and stopped,
            "estop_latched": True,
            "daemon_locked": daemon_locked,
            "stopped": stopped,
            "outcome_hint": outcome_hint,
            "hardware_estop_required": True,
            "error": error,
            "message": (("daemon 急停锁定与停止均已确认；" if daemon_locked and stopped
                         else "daemon 急停锁定已确认，但停止下发失败，结果未知；"
                         if daemon_locked else "仅本地锁定，daemon 急停未确认；")
                       + "现场请立即使用硬件急停/断电，"
                       "复位需人工调用 reset_estop(operator)"),
        }

    def reset_estop(self, operator: str) -> dict:
        """人工复位急停（daemon 级 + 平台本地），复位后强制重新检查状态。"""
        if not isinstance(operator, str) or not operator.strip() or "\r" in operator or "\n" in operator:
            raise DriverError("driver_policy_rejected", "急停复位必须记录操作者")
        with self._safety_lock:
            self._estop_latched = True
            reset_generation = self._stop_generation
        if self._transport is None:
            raise DriverError("driver_estop_latched", "daemon 连接未初始化，保持本地急停锁定")
        daemon_reset = self._control_cmd(f"RESET_ESTOP {operator}", reset=True)
        with self._safety_lock:
            if reset_generation != self._stop_generation:
                raise DriverError(
                    "driver_estop_latched",
                    "急停复位期间发生了新的停止/急停，保持本地急停锁定",
                )
            self._estop_latched = False
        self.action_log.append({"action": "reset_estop", "operator": operator, "daemon": daemon_reset})
        return {"estop_latched": False, "reset_by": operator, "daemon": daemon_reset,
                "advice": "复位后必须重新读取状态并确认控制器 Idle、定位有效"}
