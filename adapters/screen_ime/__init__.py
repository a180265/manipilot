"""屏幕输入法适配（screen_ime）：K40 输入法任务服务与屏幕校准的移植与承载。

模块：
- service：任务服务核心（任务/心跳/取消控制/长轮询，SQLite 持久化）
- calibration：屏幕校准会话协调器（内存态，服务重启失效）
- geometry：机械臂→屏幕仿射拟合与映射注册表（按 deviceId 持久化）
- routes：/api/v1/* 设备侧协议路由（与 K40TaskServer 路径字段一致）
- ios/：iOS 键盘扩展与 macOS 兼容的 Swift 协议客户端（FleetIMEKit）

边界：本适配只负责"把文本安全送到输入法候选栏、把屏幕采样变成机械坐标映射"；
机械臂真实运动仍由平台运行时、能力调用与硬件安全门禁统一管控。
"""
from __future__ import annotations

from adapters.screen_ime.geometry import (
    CalibrationMathError,
    MappingRegistry,
    ScreenRobotTransform,
    fit_affine,
)
from adapters.screen_ime.service import (
    ScreenImeApiError,
    ScreenImeService,
    get_default_service,
    set_default_service,
)

__all__ = [
    "CalibrationMathError",
    "MappingRegistry",
    "ScreenImeApiError",
    "ScreenImeService",
    "ScreenRobotTransform",
    "fit_affine",
    "get_default_service",
    "set_default_service",
]
