"""校准向导共享词汇叶模块（Auto_CS#8 拆分，ADR-0003）。

阶段常量、验收清单与异常家族被手动向导与 v1.1 自动 runner 两侧共用；
独立成叶模块保证依赖 DAG 无环：calibration_wizard 与 calibration_auto_runner
都从本模块取共享词汇，二者之间互不 import（wizard 另从 runner 取 AutoRunner
与 v1.1 常量 re-export）。
"""
from __future__ import annotations

import time


TAP_DWELL_MAX_MS = 2000


# ---------- 阶段（实施方案 §5） ----------
STAGE_IDLE = "idle"
STAGE_PRECHECK = "precheck"
STAGE_PHONE_OPEN = "phone_calibration_open"
STAGE_Z_READY = "z_ready"
STAGE_XY = "xy_calibrating"
STAGE_SCREEN_GEOMETRY = "screen_geometry_calibrating"
STAGE_TARGET = "target_calibrating"
STAGE_DEBUG_ACCEPTANCE = "debug_acceptance"
STAGE_WECHAT_ACCEPTANCE = "wechat_acceptance"
STAGE_READY = "ready"
STAGE_INVALIDATED = "invalidated"

STAGE_DISPLAY: dict[str, str] = {
    STAGE_IDLE: "未开始",
    STAGE_PRECHECK: "现场预检",
    STAGE_PHONE_OPEN: "等待手机校准页打开",
    STAGE_Z_READY: "Z 轴与点击参数校准",
    STAGE_XY: "XY 屏幕关系校准",
    STAGE_SCREEN_GEOMETRY: "屏幕边缘与摄像头校准",
    STAGE_TARGET: "视觉目标校准",
    STAGE_DEBUG_ACCEPTANCE: "调试台自动验收",
    STAGE_WECHAT_ACCEPTANCE: "历史兼容阶段（无需微信验收）",
    STAGE_READY: "校准已保存",
    STAGE_INVALIDATED: "已失效",
}


# ---------- 目标档位（Auto_CS#20 纯观测通道） ----------
# 向导的线性阶段机为动作校准设计；观测只需要相机/屏幕几何，不要求串口会话。
# goal=observation 时：start 不要求机械臂连接，手机校准页 open 直接进入
# SCREEN_GEOMETRY（屏幕坐标域取本会话校准页 open 上报），运动域操作整体禁用，
# finish 从屏幕几何阶段直达 observation_ready（跳过 Z/XY/标靶/验收动作阶段）。
GOAL_MOTION = "motion"
GOAL_OBSERVATION = "observation"
GOAL_DISPLAY: dict[str, str] = {
    GOAL_MOTION: "动作校准",
    GOAL_OBSERVATION: "纯观测校准",
}


DEBUG_ACCEPTANCE_CHECKS: tuple[str, ...] = (
    "ime_input_tap",     # 输入法：下发固定文本 → 点击候选 → 文字进入输入框
    "ime_delete_clear",  # 输入法：删除/长按清除 → 输入框被清空
)
WECHAT_ACCEPTANCE_CHECKS: tuple[str, ...] = (
    "input_text", "clear_input", "send_message", "back_exit",
)


# runner 状态（run.state；phase 单独记录）
AUTO_STATE_IDLE = "IDLE"
AUTO_STATE_RUNNING = "RUNNING"
AUTO_STATE_STOPPING = "STOPPING"
AUTO_STATE_PAUSED_RETRY = "PAUSED_RETRY"
AUTO_STATE_BOOTSTRAP_REQUIRED = "BOOTSTRAP_REQUIRED"
AUTO_STATE_FAILED = "FAILED"
AUTO_STATE_ABORTED = "ABORT"
AUTO_STATE_INVALIDATED = "INVALIDATED"
AUTO_STATE_STOPPED = "STOPPED"
AUTO_STATE_PROMOTED = "PROMOTED"
AUTO_ACTIVE_STATES = (AUTO_STATE_RUNNING, AUTO_STATE_STOPPING, AUTO_STATE_PAUSED_RETRY)


class WizardError(RuntimeError):
    """向导操作被拒绝（阶段不对、参数非法、前置缺失）。"""


class PoseUnlocatable(WizardError):
    """姿态定位不可得（摄像头/检测器不可用、画面中定位不到四边形）。

    用户拍板 2026-09-29：操作员监督下的 zSafe 确认点击不被"姿态定位不可得"
    拦截（缺席留痕，以触点回报为准）。该容忍只覆盖"定位不到"这一族错误；
    手机位移/帧冻结/尺寸不一致等质量门失败不属于本类，仍拒绝接触。
    """


class TouchReportTimeout(WizardError):
    """机械动作完成但未在超时内收到匹配触点回报（touch_report_timeout）。"""


class AutoRunError(RuntimeError):
    """自动校准 run 的终局失败（FAILED/ABORT/BOOTSTRAP_REQUIRED/INVALIDATED）。

    classification 记录错误归属（§12.3），决定样本/模型动作与失败路由；
    park=True 表示收尾时尽力抬 Z 与 SAFE PARK。
    """

    def __init__(self, code: str, message: str, *, state: str = AUTO_STATE_FAILED,
                 classification: str = "ambiguous", park: bool = True,
                 routed_to: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.state = state
        self.classification = classification
        self.park = park
        self.routed_to = routed_to


class _AutoCancelled(Exception):
    """操作员 stop / 向导取消：runner 安全停止（STOPPED，不判失败）。"""


class _AutoPauseRetry(Exception):
    """单点重采样耗尽（SOFT_FAIL）：暂停等待操作员重试或停止。"""

    def __init__(self, phase: str, target_id: str, reason: str):
        super().__init__(f"{phase}/{target_id}: {reason}")
        self.phase = phase
        self.target_id = target_id
        self.reason = reason


class ImeError(RuntimeError):
    """输入法服务返回错误。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
