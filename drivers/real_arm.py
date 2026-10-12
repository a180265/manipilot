"""真机机械臂驱动：BACHIN-3C-TA4 / ESP32 / Grbl_ESP32 1.3b（串口 G-code）。

现场安全策略（docs：《给其他AI的机械臂现场总说明》《机械臂参数清单》，2026-08-30）：
- 只有硬件急停/断电是可信急停；软件 estop = 停止发送 + 锁定 + 要求人工复位；
- 禁止 $H/$HX/$HY/$HZ 自动回零与自动寻找 Z 限位（Z 无可靠限位）；
- 运动只允许具名模板（相对小行程 G91）与会话原点归位（连接时记录 MPos）；
- 逐行发送、逐条确认 ok；小批量；Z 禁止 F300；
- ok/MPos 只代表控制器接受，真实移动必须由摄像头验证（motion_verifier），
  验证不一致时结果按"结果未知"处理（outcome_hint=unknown），不得报成功。

实例化前必须通过 configs/硬件输入确认表.json 全量确认（real_stubs.require_confirmed_hardware）。
"""
from __future__ import annotations

import asyncio
import time

from drivers.arm import ArmDriver, DriverError
from drivers.grbl import (
    BAUD_RATE,
    SERIAL_PORT_DEFAULT,
    EmulatedGrblController,
    GrblPolicyError,
    GrblTransport,
    PyserialGrblTransport,
    parse_status_report,
    validate_line,
)
from drivers.real_stubs import require_confirmed_hardware

# 具名运动模板（现场文档已验证的固定动作；不允许自由生成 G-code）
PRESET_TEMPLATES: dict[str, list[str]] = {
    "z_first_direction_verify": ["G91", "G1 Z-1 F30", "G1 Z1 F30", "G90"],
    "z_recip_3mm_verified": ["G91", "G1 Z-3 F2000", "G1 Z3 F2000", "G90"],
    "z_lift_3mm": ["G91", "G1 Z-3 F2000", "G90"],
    "z_lower_3mm": ["G91", "G1 Z3 F2000", "G90"],
    "z_lift_1mm_conservative": ["G91", "G1 Z-1 F30", "G90"],
    "z_lower_1mm_conservative": ["G91", "G1 Z1 F30", "G90"],
    # 六方向逐一验证模板：X/Y 初次验证用保守速度 F100，单次行程 1mm
    "x_plus_1mm_conservative": ["G91", "G1 X1 F100", "G90"],
    "x_minus_1mm_conservative": ["G91", "G1 X-1 F100", "G90"],
    "y_plus_1mm_conservative": ["G91", "G1 Y1 F100", "G90"],
    "y_minus_1mm_conservative": ["G91", "G1 Y-1 F100", "G90"],
    # 方向/比例测量模板：3mm 单向（文档允许的单次测试行程上限），用于建立图像方向映射
    "x_plus_3mm_measure": ["G91", "G1 X3 F100", "G90"],
    "x_minus_3mm_measure": ["G91", "G1 X-3 F100", "G90"],
    "y_plus_3mm_measure": ["G91", "G1 Y3 F100", "G90"],
    "y_minus_3mm_measure": ["G91", "G1 Y-3 F100", "G90"],
    # Z 探针步进（屏幕反馈标定找接触高度）：0.25mm 小步，F30 保守速度
    "z_lower_0p25_conservative": ["G91", "G1 Z0.25 F30", "G90"],
    "z_lift_0p25_conservative": ["G91", "G1 Z-0.25 F30", "G90"],
}


class RealArmDriver(ArmDriver):
    DRIVER_VERSION = "real-arm-grbl-1.0.0"
    SUPPORTED = {"arm.home", "arm.move_preset", "arm.click", "arm.stop", "arm.estop"}

    def __init__(
        self,
        device_id: str,
        transport: GrblTransport | None = None,
        *,
        port: str = SERIAL_PORT_DEFAULT,
        baud: int = BAUD_RATE,
        home_feed: float = 1500.0,
        idle_timeout_seconds: float = 30.0,
    ):
        self.device_id = device_id
        self._transport = transport  # None 时 connect() 按端口创建 pyserial 传输
        self._port = port
        self._baud = baud
        self._home_feed = home_feed
        self._idle_timeout = idle_timeout_seconds
        self._connected = False
        self._estop_latched = False
        self._distance_mode = "G90"  # 控制器默认绝对模式（现场文档）
        self._session_origin: dict | None = None
        self._banner: str | None = None
        self.action_log: list[dict] = []
        self.motion_verifier = None  # 摄像头运动验证器（可选，见 real_camera.CameraMotionVerifier）
        self._io_lock = asyncio.Lock()  # 串口独占：心跳/巡检/执行器并发访问时逐个串行化

    # ---------- 基础 ----------
    @property
    def driver_version(self) -> str:
        return self.DRIVER_VERSION

    def supports(self, capability_id: str) -> bool:
        return capability_id in self.SUPPORTED

    @property
    def connected(self) -> bool:
        return self._connected and (self._transport.is_open() if self._transport else False)

    @property
    def session_origin(self) -> dict | None:
        return dict(self._session_origin) if self._session_origin else None

    def _check_ready(self) -> None:
        if self._estop_latched:
            raise DriverError("driver_estop_latched", "软件急停已锁定：现场硬件复位并人工确认前拒绝一切运动")
        if not self._connected:
            raise DriverError("driver_disconnected", "机械臂串口连接已中断")

    def _policy(self, message: str, line: str = "") -> DriverError:
        return DriverError("driver_policy_rejected", f"{message}（行：{line or 'n/a'}）")

    async def _send_motion_line(self, line: str, *, allow_session_home: bool = False) -> str:
        """策略校验 → 逐行发送 → 确认 ok。返回控制器原始响应摘要。"""
        try:
            validate_line(line, distance_mode=self._distance_mode, allow_session_home=allow_session_home)
        except GrblPolicyError as exc:
            raise self._policy(str(exc), line) from exc
        if self._transport is None:
            raise DriverError("driver_disconnected", "串口传输未初始化")
        async with self._io_lock:
            try:
                responses = await asyncio.to_thread(self._transport.send_line, line)
            except GrblPolicyError as exc:
                raise DriverError("driver_disconnected", f"串口写入失败：{exc}") from exc
        upper = line.upper().split()
        if upper and upper[0] in ("G90", "G91"):
            self._distance_mode = upper[0]
        ack = next((r for r in responses if r == "ok"), None)
        error = next((r for r in responses if r.startswith("error")), None)
        self.action_log.append({"line": line, "responses": responses, "ack": ack})
        if error:
            raise DriverError("driver_motion_failed", f"控制器拒绝命令（{error}）：{line}")
        if ack is None:
            raise DriverError(
                "driver_timeout",
                f"命令未收到 ok 确认，动作结果无法确认（该行不得重发）：{line}",
            )
        return "ok"

    async def _query_state(self) -> dict:
        if self._transport is None:
            raise DriverError("driver_disconnected", "串口传输未初始化")
        async with self._io_lock:
            try:
                responses = await asyncio.to_thread(self._transport.send_line, "?")
            except GrblPolicyError as exc:
                raise DriverError("driver_disconnected", f"状态查询失败：{exc}") from exc
        for response in responses:
            report = parse_status_report(response)
            if report:
                return report
        raise DriverError("driver_timeout", "状态查询无响应")

    async def _wait_idle(self) -> dict:
        deadline = time.monotonic() + self._idle_timeout
        report = await self._query_state()
        while report.get("state") == "Run":
            if time.monotonic() > deadline:
                raise DriverError("driver_timeout", "等待 Idle 超时，运动可能仍在进行，需现场确认")
            await asyncio.sleep(0.2)
            report = await self._query_state()
        return report

    # ---------- 生命周期 ----------
    async def connect(self) -> dict:
        require_confirmed_hardware()
        if self._estop_latched:
            raise DriverError("driver_estop_latched", "软件急停锁定中，需人工复位后才能连接")
        if self._transport is None:
            self._transport = PyserialGrblTransport(port=self._port, baud=self._baud)
        try:
            self._banner = await asyncio.to_thread(self._transport.open)
        except GrblPolicyError as exc:
            raise DriverError("driver_connect_failed", f"打开串口失败：{exc}") from exc
        # 状态查询可能读到半行噪声：无效报文自动重试（最多 3 次）
        report = None
        for _ in range(3):
            try:
                report = await self._query_state()
                break
            except DriverError:
                continue
        if report is None:
            raise DriverError("driver_connect_failed", "状态查询连续无有效响应，串口通信异常")
        state = report.get("state", "")
        if not self._banner or "Grbl" not in self._banner:
            # 已上电常驻的控制器不一定重发启动横幅；通信与状态以 ? 报文为准
            self.action_log.append({
                "action": "connect_note",
                "note": "未收到 Grbl 启动信息（控制器可能已上电运行），以 ? 状态报文为准",
            })
        if state != "Idle":
            raise DriverError(
                "driver_connect_failed",
                f"控制器状态为 {state}（非 Idle），先现场检查并复位后重试（Alarm/Door 状态禁止接入）",
            )
        # 清除历史 G92 偏移（上一会话可能残留），保证 G90 目标 = MPos 坐标系
        await self._send_motion_line("G92.1")
        report = await self._query_state()
        self._session_origin = report.get("mpos")
        self._distance_mode = "G90"
        self._connected = True
        self.action_log.append({"action": "connect", "banner": self._banner, "origin": self._session_origin})
        return {
            "connected": True,
            "driver_version": self.DRIVER_VERSION,
            "port": getattr(self._transport, "port", self._port),
            "firmware": self._banner,
            "controller_state": state,
            "session_origin_mpos": dict(self._session_origin),
        }

    async def disconnect(self) -> dict:
        if self._transport is not None:
            await asyncio.to_thread(self._transport.close)
        self._connected = False
        self.action_log.append({"action": "disconnect"})
        return {"connected": False}

    async def read_state(self) -> dict:
        if not self._connected or self._transport is None:
            return {"connected": False, "position": None, "estop_latched": self._estop_latched, "limit_ok": True, "error": "未连接"}
        try:
            report = await self._query_state()
        except DriverError as exc:
            return {"connected": False, "position": None, "estop_latched": self._estop_latched, "limit_ok": True, "error": exc.message}
        return {
            "connected": True,
            "position": report.get("mpos"),
            "controller_state": report.get("state"),
            "session_origin_mpos": self.session_origin,
            "estop_latched": self._estop_latched,
            "limit_ok": True,  # 控制器未报告限位触发；Z 无硬件限位，行程软上限在策略层拦截
            "error": None,
        }

    # ---------- 运动验证 ----------
    async def _verify_motion(self, label: str) -> dict | None:
        if self.motion_verifier is None:
            return None
        try:
            return await self.motion_verifier.after(label)
        except Exception as exc:
            return {"verified": False, "reason": f"运动验证异常（按结果未知处理）：{exc}"}

    # ---------- 平台动作 ----------
    async def go_home(self) -> dict:
        """归位 = 回到连接时记录的会话原点（MPos）。禁止 $H 自动回零。

        MPos 回到原点不等于机械回到原点：若归位前有位移，必须由摄像头
        前后帧差确认真实移动，未证实时 ok=False 并要求人工确认。
        """
        self._check_ready()
        if not self._session_origin:
            raise DriverError("driver_home_failed", "缺少会话原点：必须先成功连接并记录 MPos")
        origin = self._session_origin
        lines = [
            "G90",
            f"G1 X{origin['x']:.3f} Y{origin['y']:.3f} Z{origin['z']:.3f} F{self._home_feed:g}",
        ]
        report_before = await self._query_state()
        moved_expected = False
        before_mpos = report_before.get("mpos") or {}
        if before_mpos and any(abs(before_mpos.get(axis, 0) - origin[axis]) > 0.05 for axis in ("x", "y", "z")):
            moved_expected = True
        if moved_expected and self.motion_verifier is not None:
            await self.motion_verifier.before("go_home")
        try:
            for line in lines:
                await self._send_motion_line(line, allow_session_home=True)
            report = await self._wait_idle()
        except DriverError as exc:
            if exc.category == "driver_home_failed":
                raise
            raise DriverError("driver_home_failed", f"归位未完成：{exc.message}") from exc
        position = report.get("mpos") or {}
        at_origin = position and all(
            abs(position.get(axis, 0) - origin[axis]) <= 0.05 for axis in ("x", "y", "z")
        )
        verification = await self._verify_motion("go_home") if moved_expected else None
        verified = verification is None or bool(verification.get("verified"))
        ok = bool(at_origin) and verified
        if report.get("state") not in ("Idle", "Hold"):
            raise DriverError("driver_home_failed", f"归位后控制器状态异常：{report.get('state')}")
        self.action_log.append({"action": "go_home", "origin": dict(origin), "final_mpos": position, "verification": verification})
        return {
            "ok": ok,
            "position": position,
            "controller_state": report.get("state"),
            "state_before": report_before.get("state"),
            "motion_verified": None if verification is None else bool(verification.get("verified")),
            "verification": verification,
            "home_mode": "session_origin（无 $H，禁止自动回零）",
            "message": None if ok else (
                "归位后 MPos 与会话原点偏差超限，需人工确认"
                if not at_origin
                else "MPos 已回原点但摄像头未确认真实归位，需人工确认"
            ),
        }

    async def move_preset(self, position_id: str) -> dict:
        """执行具名运动模板（白名单），逐行发送；每条 G1 运动段做摄像头前后帧验证。

        验证按运动段进行：例如 3mm 往复的起点=终点，整体前后对比无法确认
        中间是否真实移动，必须逐段确认。
        """
        self._check_ready()
        template = PRESET_TEMPLATES.get(position_id)
        if template is None:
            raise self._policy(
                f"未登记的运动模板：{position_id}；允许的模板：{sorted(PRESET_TEMPLATES)}", position_id
            )
        verifications: list[dict] = []
        motion_index = 0
        for line in template:
            if line.upper().startswith("G1"):
                label = f"{position_id}:step{motion_index}"
                motion_index += 1
                if self.motion_verifier is not None:
                    await self.motion_verifier.before(label)
                await self._send_motion_line(line)
                report = await self._wait_idle()
                if self.motion_verifier is not None:
                    verifications.append(await self._verify_motion(label))
            else:
                await self._send_motion_line(line)
        outcome_hint = self._outcome_hint_segments(verifications)
        verified = bool(verifications) and all(v.get("verified") for v in verifications)
        self.action_log.append({"action": "move_preset", "preset": position_id, "verifications": verifications})
        return {
            "ok": True,
            "ack_ok": True,
            "position_id": position_id,
            "position": report.get("mpos"),
            "controller_state": report.get("state"),
            "motion_verified": verified,
            "verification": verifications[-1] if verifications else None,
            "verifications": verifications,
            "outcome_hint": outcome_hint,
            "message": None if outcome_hint != "unknown" else "控制器已确认命令，但摄像头未逐段确认真实移动，结果未知（禁止当作成功）",
        }

    async def set_session_origin_here(self, *, reason: str) -> dict:
        """把当前位置锚定为会话原点（零串口运动指令）。

        本固件的 G92 只偏移 WPos、不改 MPos，且会给 G90 目标引入隐藏偏移，
        因此锚定采用纯软件记录：读取当前 MPos 存为会话原点，go_home 与
        归位校验都以该值为基准。必须填写非空原因，进入审计与动作日志。
        """
        if not isinstance(reason, str) or not reason.strip():
            raise self._policy("标定会话原点必须填写原因（审计要求）")
        self._check_ready()
        report = await self._query_state()
        origin = report.get("mpos")
        if not origin:
            raise DriverError("driver_home_failed", "读取当前 MPos 失败，拒绝锚定会话原点")
        self._session_origin = dict(origin)
        self.action_log.append({"action": "set_session_origin", "reason": reason, "origin": dict(origin)})
        return {
            "ok": True,
            "session_origin_mpos": dict(self._session_origin),
            "message": f"会话原点已锚定在当前位置（{reason}）；后续归位以此为准",
        }

    @staticmethod
    def _outcome_hint_segments(verifications: list[dict]) -> str | None:
        """无验证器 → None（由上层保守处理）；任一段未证实 → unknown。"""
        if not verifications:
            return None
        if any(not v.get("verified") for v in verifications):
            return "unknown"
        return "success"

    async def click(self, target: dict) -> dict:
        """点击 = 相对 XY 定位（+可选 Z 轻触），逐段发送，摄像头验证。

        target 字段（设备相对 mm）：dx/dy 必填；tap_depth_mm 可选（下压深度）；
        feed_xy 默认保守 F100；tap 进给固定 F1500（现场点击推荐值）。
        """
        self._check_ready()
        try:
            dx = float(target["dx"])
            dy = float(target["dy"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DriverError("driver_motion_failed", f"点击目标缺少相对位移 dx/dy：{target}") from exc
        feed_xy = float(target.get("feed_xy", 100))
        tap_depth = target.get("tap_depth_mm")
        if self.motion_verifier is not None:
            await self.motion_verifier.before("click:move")
        await self._send_motion_line("G91")
        await self._send_motion_line(f"G1 X{dx:.3f} Y{dy:.3f} F{feed_xy:g}")
        await self._send_motion_line("G90")
        report = await self._wait_idle()
        move_verification = await self._verify_motion("click:move")
        phases = [{"phase": "move", "verification": move_verification}]
        if tap_depth is not None:
            depth = float(tap_depth)
            if self.motion_verifier is not None:
                await self.motion_verifier.before("click:tap")
            # Z 正方向向下（靠近屏幕）：下压再抬起，先下后上
            await self._send_motion_line("G91")
            await self._send_motion_line(f"G1 Z{depth:.3f} F1500")
            await self._send_motion_line(f"G1 Z{-depth:.3f} F1500")
            await self._send_motion_line("G90")
            report = await self._wait_idle()
            tap_verification = await self._verify_motion("click:tap")
            phases.append({"phase": "tap", "verification": tap_verification})
        verified = all(p["verification"] and p["verification"].get("verified") for p in phases)
        unverified_exists = any(p["verification"] is None or not p["verification"].get("verified") for p in phases)
        outcome_hint = "unknown" if unverified_exists else "success"
        self.action_log.append({"action": "click", "target": dict(target), "phases": phases})
        return {
            "ok": True,
            "ack_ok": True,
            "position": report.get("mpos"),
            "phases": phases,
            "motion_verified": bool(verified and phases),
            "verification": phases[-1].get("verification") if phases else None,
            "outcome_hint": outcome_hint,
            "message": None if outcome_hint != "unknown" else "命令已被控制器确认，但摄像头未确认真实移动，结果未知（禁止当作成功）",
        }

    async def double_tap(self, target: dict) -> dict:
        raise DriverError(
            "driver_policy_rejected",
            "校准后的双击必须通过 daemon 驱动桥执行；真机直连驱动不提供输入法坐标映射")

    async def swipe(self, gesture: dict, *, execution_guard=None) -> dict:
        raise DriverError(
            "driver_policy_rejected",
            "校准后的滑动必须通过 daemon 驱动桥执行；真机直连驱动不提供输入法坐标映射")

    async def stop(self) -> dict:
        """软停止：进给保持 + 状态确认。不是急停，现场安全仍以硬件急停为准。"""
        if self._transport is None or not self._connected:
            return {"ok": False, "stopped": False, "message": "串口未连接，无法发送停止命令"}
        try:
            async with self._io_lock:
                await asyncio.to_thread(self._transport.send_line, "!")
            # 锁内只做一次串口写入；状态查询另行加锁（asyncio.Lock 不可重入）
            report = await self._query_state()
        except DriverError as exc:
            return {"ok": False, "stopped": False, "message": f"停止命令失败：{exc.message}"}
        self.action_log.append({"action": "stop", "state": report.get("state")})
        return {
            "ok": True,
            "stopped": True,
            "controller_state": report.get("state"),
            "message": "已发送进给保持并确认状态；如需彻底断电请使用现场硬件急停",
        }

    async def estop(self) -> dict:
        """软件急停：停止发送 + 锁定。现场必须同时使用硬件急停/断电。"""
        self._estop_latched = True
        self.action_log.append({"action": "estop"})
        try:
            if self._transport is not None and self._connected:
                async with self._io_lock:
                    await asyncio.to_thread(self._transport.send_line, "!")
        except Exception:
            pass
        return {
            "ok": True,
            "estop_latched": True,
            "hardware_estop_required": True,
            "message": "软件侧已停止发送并锁定；现场请立即使用硬件急停/断电，复位需人工确认后调用 reset_estop",
        }

    def reset_estop(self, operator: str) -> dict:
        """人工复位软件急停（必须由人工确认后调用，复位后需重新 ? 检查状态）。"""
        self._estop_latched = False
        self.action_log.append({"action": "reset_estop", "operator": operator})
        return {"estop_latched": False, "reset_by": operator, "advice": "复位后必须发送 ? 确认控制器状态为 Idle"}
