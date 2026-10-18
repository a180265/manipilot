"""fake 机械臂驱动：仿真用，支持故障注入。

故障注入钩子（测试用）：
- fail_connect / fail_move / fail_home / fail_click / fail_move_target：对应动作直接失败
- disconnect_after：某动作后模拟连接中断（用于“结果未知”场景）
- estop() 锁定急停，复位前拒绝一切运动
"""
from __future__ import annotations

import asyncio

from drivers.arm import ArmDriver, DriverError
from drivers.daemon_protocol import (
    DEFAULT_TARGET_MODES,
    PATH1_FEATURE,
    TARGET_DISPLAY_NAMES,
    TARGET_MODE_HOVER_ONLY,
)


class FakeArmDriver(ArmDriver):
    DRIVER_VERSION = "fake-arm-1.1.0"

    # 平台基础能力 -> 支持
    SUPPORTED = {
        "arm.home", "arm.park", "arm.move_preset", "arm.move_target", "arm.click",
        "arm.double_tap", "arm.swipe", "arm.stop", "arm.estop",
    }

    def __init__(self, device_id: str, move_seconds: float = 0.02):
        self.device_id = device_id
        self._connected = False
        self._estop_latched = False
        self._position = "home"
        self._move_seconds = move_seconds
        self.faults: dict[str, bool] = {}
        self.action_log: list[dict] = []
        self.click_callback = None  # 仿真联动：点击后由 fake 窗口更新画面

    @property
    def driver_version(self) -> str:
        return self.DRIVER_VERSION

    def supports(self, capability_id: str) -> bool:
        return capability_id in self.SUPPORTED

    def _check_connection(self) -> None:
        if self._estop_latched:
            # 急停门拒绝发生在任何运动之前：收据按 not_started 记录（unknown 会误触
            # 机械未知保护）；与真机驱动"复位前拒绝一切运动"的未开始语义一致。
            error = DriverError("driver_estop_latched", "急停已锁定，复位前拒绝一切运动")
            error.motion_not_started = True
            raise error
        if not self._connected:
            raise DriverError("driver_disconnected", "机械臂连接已中断")

    def _fault(self, key: str) -> bool:
        return bool(self.faults.get(key))

    async def connect(self) -> dict:
        if self._fault("fail_connect"):
            raise DriverError("driver_connect_failed", "模拟机械臂连接失败")
        self._connected = True
        self.action_log.append({"action": "connect"})
        return {"connected": True, "driver_version": self.DRIVER_VERSION}

    async def disconnect(self) -> dict:
        self._connected = False
        return {"connected": False}

    async def read_state(self) -> dict:
        return {
            "connected": self._connected,
            "position": self._position,
            "estop_latched": self._estop_latched,
            "limit_ok": True,
            "error": None,
            "motion_features": ["tap2.v1", "tap2.short-lift.v1", "swipe.v1",
                                PATH1_FEATURE],
        }

    async def _move(self, action: str, target: str, *, input_claim: dict | None = None) -> dict:
        if self._fault(f"fail_{action}"):
            raise DriverError("driver_motion_failed", f"模拟{action}运动失败")
        self._check_connection()
        await asyncio.sleep(self._move_seconds)
        if input_claim:
            # §4.8.4 + R3-02：等待结束后、模拟接触（action_log）之前，
            # 先最终几何门、后最后认领（与 daemon_arm 同序）；任一失败
            # =零动作，拒绝在接触发生前抛出。仿真链路不缺 R3-02 最终门。
            gate = input_claim.get("geometry_gate")
            if callable(gate):
                gate()
            callback = input_claim.get("callback")
            if callable(callback):
                callback()
        self._position = target
        self.action_log.append({"action": action, "target": target})
        if self._fault("disconnect_after"):
            self._connected = False
        return {"ok": True, "position": self._position}

    async def go_home(self) -> dict:
        if self._fault("fail_home"):
            self._check_connection()
            error = DriverError("driver_home_failed", "模拟归位失败，动作未开始")
            error.motion_not_started = True
            raise error
        return await self._move("home", "home")

    async def park(self) -> dict:
        from runtime.parking import calibration_safe_park_record

        result = await self._move("park", "safe_park")
        return {**result, **calibration_safe_park_record(None),
                "park": True, "arrived": result.get("ok") is True, "tap_executed": False}

    async def move_preset(self, position_id: str) -> dict:
        return await self._move("move_preset", position_id)

    async def move_target(self, target_id: str) -> dict:
        """与真实驱动同语义：仅移动并停在悬停高度，绝不 TAP。

        hover_only 目标才允许停靠；动作日志使用独立 move_target 动作键，
        便于测试断言“停靠没有产生任何 click/TAP”。
        """
        mode = DEFAULT_TARGET_MODES.get(target_id)
        if mode != TARGET_MODE_HOVER_ONLY:
            display = TARGET_DISPLAY_NAMES.get(target_id, target_id)
            raise DriverError(
                "driver_policy_rejected",
                f"目标 {target_id}（{display}）的动作模式为 {mode or '未登记'}，"
                "move_target 仅允许 hover_only（仅悬停）目标，已拒绝执行")
        result = await self._move("move_target", target_id)
        result.update({
            "target_id": target_id,
            "target_display": TARGET_DISPLAY_NAMES.get(target_id, target_id),
            "target_mode": TARGET_MODE_HOVER_ONLY,
            "tap_executed": False,
            "message": "已移动到目标并停在悬停高度（未执行点击）",
        })
        return result

    async def click(self, target: dict) -> dict:
        input_claim = target.pop("input_claim", None) if isinstance(target, dict) else None
        result = await self._move("click", str(target.get("target_id", "unknown")),
                                  input_claim=input_claim)
        if self.click_callback:
            # 新回调接收完整目标以模拟动态 OCR 会话；兼容旧测试回调的字符串签名。
            try:
                self.click_callback(dict(target))
            except TypeError:
                self.click_callback(str(target.get("target_id", "")))
        # 记录完整参数（坐标点击时含 robot_x/robot_y 等），供仿真核对
        self.action_log[-1]["params"] = dict(target)
        source = str((target.get("target_ref") or {}).get("source") or "fixed_target")
        source_display = {
            "camera_ocr": "摄像头 OCR 自动定位",
            "vision_back_button": "视觉规则自动定位返回键",
            "fixed_target": "设备固定目标",
        }.get(source, "其他")
        self.action_log[-1]["target_source"] = source
        self.action_log[-1]["target_source_display"] = source_display
        result.update({
            "target_id": str(target.get("target_id", "unknown")),
            "target_source": source,
            "target_source_display": source_display,
        })
        return result

    async def double_tap(self, target: dict) -> dict:
        if self._fault("fail_double_tap"):
            raise DriverError("driver_motion_failed", "模拟双击运动失败")
        self._check_connection()
        target_id = str(target.get("target_id", "unknown"))
        await asyncio.sleep(self._move_seconds)
        self.action_log.append({"action": "double_tap", "target": target_id, "params": dict(target)})
        if self._fault("disconnect_after"):
            self._connected = False
        return {
            "ok": True,
            "target_id": target_id,
            "tap_count": 2,
            "motion_verified": self._connected,
            "outcome_hint": "success" if self._connected else "unknown",
            "effect_status": "unverified",
        }

    async def swipe(self, gesture: dict, *, execution_guard=None) -> dict:
        if execution_guard is not None:
            execution_guard()
        if self._fault("fail_swipe"):
            raise DriverError("driver_motion_failed", "模拟滑动运动失败")
        self._check_connection()
        if not isinstance(gesture, dict):
            raise DriverError("driver_gesture_invalid", "滑动参数必须是对象")
        start = gesture.get("start")
        end = gesture.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            raise DriverError("driver_gesture_invalid", "滑动必须提供 start 和 end")
        await asyncio.sleep(self._move_seconds)
        if execution_guard is not None:
            execution_guard()
        self.action_log.append({"action": "swipe", "start": dict(start), "end": dict(end),
                                "params": dict(gesture)})
        if self._fault("disconnect_after"):
            self._connected = False
        return {
            "ok": True,
            "start": dict(start),
            "end": dict(end),
            "swipe_executed": True,
            "motion_verified": self._connected,
            "outcome_hint": "success" if self._connected else "unknown",
            "effect_status": "unverified",
        }

    async def execute_gesture_program(self, plan: dict) -> dict:
        """连续路径程序（path1 计划）：仿真执行并记录 strokes。"""
        if self._fault("fail_path"):
            raise DriverError("driver_motion_failed", "模拟连续路径运动失败")
        self._check_connection()
        if not isinstance(plan, dict) or plan.get("execution_mode") != "path1":
            raise DriverError("driver_gesture_invalid", "连续路径计划必须是 path1 执行模式")
        strokes = plan.get("strokes")
        if not isinstance(strokes, list) or not strokes:
            raise DriverError("driver_gesture_invalid", "连续路径计划缺少 strokes")
        await asyncio.sleep(self._move_seconds)
        self.action_log.append({"action": "execute_gesture_program",
                                "strokes": [dict(stroke) for stroke in strokes]})
        if self._fault("disconnect_after"):
            self._connected = False
        return {
            "ok": True,
            "execution_mode": "path1",
            "stroke_count": len(strokes),
            "controller_status": "completed",
            "controller_completed": True,
            "position": {"x": 0.0, "y": 0.0, "z": -8.0, "state": "Idle"},
            # 与真驱动同口径：逐 stroke 回执 + 断连时不伪造已抬离证据
            "stroke_results": [
                {"index": index,
                 "segments_completed": len(stroke.get("segments") or []),
                 "path_completed": True,
                 "contact_released": True if self._connected else None}
                for index, stroke in enumerate(strokes)],
            "motion_verified": self._connected,
            "outcome_hint": "success" if self._connected else "unknown",
            "effect_status": "unverified",
        }

    async def test_calibrated_double_tap(self, params: dict) -> dict:
        """校准双击自检（规范第 22.17 节）：仿真执行并记录回执。"""
        if self._fault("fail_double_tap"):
            raise DriverError("driver_motion_failed", "模拟校准双击自检运动失败")
        self._check_connection()
        await asyncio.sleep(self._move_seconds)
        self.action_log.append({"action": "test_calibrated_double_tap",
                                "params": dict(params)})
        if self._fault("disconnect_after"):
            self._connected = False
        return {
            "ok": True,
            "controller_status": "completed",
            "phases": [{"phase": "double_tap"}],
            "final_mpos": {"x": 0.0, "y": 0.0, "z": -8.0, "state": "Idle"},
            # 断连后结果未知：不伪造已抬离证据（与真驱动同口径）
            "contact_released": True if self._connected else None,
            "motion_verified": self._connected,
            "outcome_hint": "success" if self._connected else "unknown",
        }

    async def stop(self) -> dict:
        self.action_log.append({"action": "stop"})
        return {"ok": True, "stopped": True}

    async def estop(self) -> dict:
        self._estop_latched = True
        self._connected = False
        self.action_log.append({"action": "estop"})
        return {"ok": True, "estop_latched": True}

    def reset_estop(self, operator: str) -> dict:
        """人工复位急停（必须由人工确认后调用）。"""
        self._estop_latched = False
        return {"estop_latched": False, "reset_by": operator}

    def reconnect(self) -> dict:
        """模拟恢复连接（人工处理后）。"""
        self._connected = True
        return {"connected": True}

    @property
    def connected(self) -> bool:
        return self._connected
