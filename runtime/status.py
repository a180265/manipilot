"""内部兼容键与中文显示映射。

依据 docs/术语与中文关键值规范.md：
- 面向操作者的所有业务关键词必须使用中文；
- 英文键只作为协议键、稳定编号和内部兼容值；
- 所有对外契约必须同时返回内部键与中文显示字段；
- 测试必须同时断言内部键和中文显示映射。
"""
from __future__ import annotations

# 设备状态
DEVICE_STATUS: dict[str, str] = {
    "registered": "已注册",
    "starting": "启动中",
    "ready": "可用",
    "busy": "忙碌",
    "returning_home": "正在归位",
    "offline": "离线",
    "degraded": "降级",
    "paused": "暂停",
    "quarantined": "隔离",
    "maintenance": "维护中",
    "recovering": "恢复中",
}

# 设备运行模式（独立于物理状态，避免把托管语义混进状态机）
DEVICE_MODE: dict[str, str] = {
    "unmanaged": "未托管",
    "ready_manual": "可人工输入",
    "managed": "托管中",
}

# 任务状态
TASK_STATUS: dict[str, str] = {
    "queued": "已排队",
    "validating": "校验中",
    "awaiting_device": "等待设备",
    "executing": "执行中",
    "confirmed": "已发送或已完成",
    "failed": "明确失败",
    "unknown": "结果未知",
    "cancelled": "已取消",
    "expired": "已过期",
    "takeover": "已接管",
}

# 查看会话状态（属于查看会话，不是设备状态）
VIEW_SESSION_STATUS: dict[str, str] = {
    "valid": "有效",
    "waiting": "等待画面恢复",
    "expiring": "即将过期",
    "closed": "已关闭",
    "expired": "已过期",
    "unavailable": "画面不可用",
}

# 微信/外部应用界面状态（由摄像头观测回报，平台只保存标准映射）
UI_STATE: dict[str, str] = {
    "unknown": "未知",
    "wechat_main": "微信主界面",
    "conversation": "会话界面",
    "login": "登录页",
    "qrcode": "二维码页",
    "captcha": "验证码页",
    "risk_control": "风控页",
    "error": "错误页",
    "other": "其他界面",
}

# 结果类别（任务三种结果 + 接管）
RESULT_STATUS: dict[str, str] = {
    "confirmed": "已发送或已完成",
    "failed": "明确失败",
    "unknown": "结果未知",
    "cancelled": "已取消",
    "takeover": "已接管",
}

# 告警级别
ALERT_SEVERITY: dict[str, str] = {
    "info": "提示",
    "warning": "警告",
    "critical": "严重",
}

# 摄像头画面质量
CAMERA_QUALITY: dict[str, str] = {
    "clear": "清晰",
    "waiting": "等待画面恢复",
    "blocked": "遮挡",
    "overexposed": "过曝",
    "frozen": "画面冻结",
    "abnormal": "画面异常",
}


def display(table: dict[str, str], key: str) -> str:
    """取中文显示；未登记的键必须先登记再使用，禁止静默回退到英文。"""
    if key not in table:
        raise KeyError(f"未登记的内部键，必须先在中文状态字典登记：{key}")
    return table[key]


def device_status_display(key: str) -> str:
    return display(DEVICE_STATUS, key)


def task_status_display(key: str) -> str:
    return display(TASK_STATUS, key)


def ui_state_display(key: str) -> str:
    return display(UI_STATE, key)


def pair(table: dict[str, str], key: str) -> dict[str, str]:
    """返回 {key, display} 结构，供 API 契约同时返回内部键与中文显示。"""
    return {"key": key, "display": display(table, key)}
