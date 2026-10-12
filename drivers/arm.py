"""机械臂驱动抽象（最小接口，语义不得改变）。

连接/断开/读取状态/回到安全位置/移动到预设位置/点击/输入/停止/急停。
真机驱动必须继承本接口实现；硬件参数未确认前不得实例化真机驱动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# 驱动错误类别（内部键）与中文说明在 errors 字典中登记
DRIVER_ERROR_CATEGORIES: dict[str, str] = {
    "driver_connect_failed": "连接失败",
    "driver_disconnect_failed": "断开失败",
    "driver_motion_failed": "运动失败",
    "driver_home_failed": "归位失败",
    "driver_disconnected": "连接中断",
    "driver_stopped": "已停止",
    "driver_estop_latched": "急停已锁定",
    "driver_timeout": "动作超时",
    "driver_limit_exceeded": "超出软限位",
    "driver_overload": "碰撞或过载",
    "driver_policy_rejected": "安全策略拒绝",
    "driver_uncalibrated": "未完成标定",
    "driver_camera_waiting": "等待画面恢复",
    "driver_gesture_invalid": "手势参数无效",
    "input_switch_disabled": "输入与发送未允许",
    "input_authorization_revoked": "任务输入许可已失效",
    "input_authorization_missing": "任务缺少输入许可记录",
    # FIX-15（§11.13.2）：实时键盘几何不可用——候选/删除点击规划前停止，零接触
    "ime_geometry_unavailable": "实时键盘几何不可用",
}


class DriverError(Exception):
    """结构化驱动错误：携带错误类别（内部键）和中文说明。"""

    def __init__(self, category: str, message: str):
        if category not in DRIVER_ERROR_CATEGORIES:
            raise ValueError(f"未登记的驱动错误类别：{category}")
        self.category = category
        self.category_display = DRIVER_ERROR_CATEGORIES[category]
        self.message = message
        super().__init__(f"[{self.category_display}] {message}")

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "category_display": self.category_display,
            "message": self.message,
        }


def gesture_motion_confirmed(action: str, result: Any) -> bool:
    """Shared mechanical completion only; callers separately verify phone effects."""
    if not isinstance(result, dict) or result.get("ok") is not True \
            or result.get("motion_verified") is not True:
        return False
    if any(key in result and result[key] is not False
           for key in ("unknown", "stopped", "cancelled", "aborted")):
        return False
    if any(key in result and result[key] not in ("success", "confirmed", "completed")
           for key in ("outcome_hint", "outcome", "status")):
        return False
    if action == "double_tap":
        return type(result.get("tap_count")) is int and result["tap_count"] == 2
    return action == "swipe" and result.get("swipe_executed") is True


class ArmDriver(ABC):
    """机械臂驱动最小接口。"""

    @abstractmethod
    async def connect(self) -> dict:
        """连接() -> 连接结果"""

    @abstractmethod
    async def disconnect(self) -> dict:
        """断开() -> 断开结果"""

    @abstractmethod
    async def read_state(self) -> dict:
        """读取状态() -> 位置、限位、错误和急停状态"""

    @abstractmethod
    async def go_home(self) -> dict:
        """回到安全位置() -> 运动结果和证据"""

    @abstractmethod
    async def move_preset(self, position_id: str) -> dict:
        """移动到预设位置(位置编号) -> 运动结果和证据"""

    @abstractmethod
    async def click(self, target: dict) -> dict:
        """点击(目标位姿或已标定目标) -> 动作结果和证据"""

    @abstractmethod
    async def double_tap(self, target: dict) -> dict:
        """双击(目标位姿或已标定目标) -> 原子动作结果和证据"""

    @abstractmethod
    async def swipe(self, gesture: dict, *, execution_guard=None) -> dict:
        """滑动(起点、终点和接触参数) -> 原子动作结果和证据"""

    async def execute_gesture_program(self, plan: dict) -> dict:
        """连续路径程序(gesture-program.v1 的 path1 计划) -> 原子动作结果和证据。

        非抽象默认实现：不支持连续路径的驱动明确拒绝，不拆成分段动作兜底；
        具备 path.v1 特性的驱动（FakeArmDriver/DaemonArmDriver）覆写本方法。
        """
        raise DriverError("driver_policy_rejected", "当前驱动不支持连续路径（path.v1 未加载）")

    async def test_calibrated_double_tap(self, params: dict) -> dict:
        """校准双击自检(规范第 22.17 节，仅活跃手机自检 trial 可用) -> 执行回执。

        非抽象默认实现：不支持的驱动明确拒绝。params 有且仅有
        run_id/trial_id/plan_id/binding/point/interval_ms（point 为校准域
        归一化 x/y，interval_ms 固定 50）。
        """
        raise DriverError("driver_policy_rejected", "当前驱动不支持校准双击自检")

    @abstractmethod
    async def stop(self) -> dict:
        """停止() -> 停止结果"""

    @abstractmethod
    async def estop(self) -> dict:
        """急停() -> 急停结果"""

    @property
    @abstractmethod
    def driver_version(self) -> str:
        """驱动版本，用于设备注册信息。"""

    @abstractmethod
    def supports(self, capability_id: str) -> bool:
        """该驱动是否支持指定基础能力。"""
