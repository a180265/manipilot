"""设备统一资格计算。

资格不是设备物理状态的替代品：设备可能在线但未校准，或已校准但输入法离线。
所有人工输入、托管启动和自动任务入口都应使用这里的结果。
"""
from __future__ import annotations

INPUT_TASK_TYPES = frozenset({"chat_send", "wx_session_send", "screen_input", "screen_type"})


def compute_device_eligibility(device: dict, *, ime_status: dict | None = None,
                               require_input: bool = False, require_keyboard_active: bool = True,
                               require_ime_ready: bool = True) -> dict:
    blocking: list[str] = []
    status = device.get("status")
    if device.get("presence_state") == "removed":
        blocking.append("device_removed")
    if device.get("binding_enabled") is False:
        blocking.append("device_binding_disabled")
    if status in {"offline", "starting", "busy", "returning_home", "paused", "quarantined", "maintenance", "degraded"}:
        blocking.append(f"device_{status}")
    if device.get("estop_latched"):
        blocking.append("estop_active")
    if not device.get("home_ok", True):
        blocking.append("home_not_confirmed")
    waiting_for_image = device.get("camera_quality") == "waiting"
    if waiting_for_image:
        blocking.append("camera_waiting")

    calibration_status = str(device.get("calibration_status") or "unknown")
    calibrated = calibration_status == "validated"
    if not calibrated:
        blocking.append("calibration_due")

    if require_input:
        # A-开关（2026-10-03 规格 §4.6）：E 验收不再决定输入资格——改由
        # 操作者的「允许输入与发送」持久开关；开关关=统一阻断码。
        from runtime.input_settings import is_enabled, settings_path
        data_root = device.get("_settings_data_root")
        if not is_enabled(device.get("device_id", ""), data_root):
            blocking.append("input_switch_disabled")
        if not require_ime_ready and not require_keyboard_active:
            # Focus-only navigation may wake a hidden IME. Input effects still
            # use the strict default at their final claim boundary.
            if not device.get("ime_device_id"):
                blocking.append("ime_not_bound")
        elif ime_status is not None:
            if not ime_status.get("online"):
                blocking.append("ime_offline")
            if require_keyboard_active and not ime_status.get("keyboardActive", ime_status.get("keyboard_active", True)):
                blocking.append("ime_keyboard_inactive")
            # FIX-15（§4.9.3）：输入控制心跳能力门——开关可保存开启，
            # 当前绑定手机控制版本缺失/不支持/跨服务重启/过期时执行资格 false。
            if ime_status.get("online") and \
                    ime_status.get("inputControlCompatible") is False:
                blocking.append("ime_control_incompatible")
        elif not device.get("ime_device_id"):
            blocking.append("ime_not_bound")
        else:
            blocking.append("ime_offline")

    # 摄像头查看/校准与机械臂执行解耦：串口 offline 但物理设备仍在位且
    # 摄像头有新鲜画面时，允许查看和校准；任务资格仍由 device_offline 阻断。
    frame_age = device.get("camera_frame_age_ms")
    fresh_camera = isinstance(frame_age, (int, float)) and frame_age <= 3000
    can_view = (
        status != "quarantined"
        and device.get("presence_state") != "removed"
        and (status != "offline" or fresh_camera)
    )
    can_calibrate = can_view and not device.get("estop_latched", False) and not waiting_for_image
    can_manual_send = require_input and not blocking
    can_managed = can_manual_send and device.get("device_mode") in {"ready_manual", "managed", "unmanaged"}
    return {
        "can_view": can_view,
        "can_calibrate": can_calibrate,
        "can_manual_send": can_manual_send,
        "can_managed": can_managed,
        "can_accept_task": can_manual_send if require_input else not blocking,
        "calibration_status": calibration_status,
        "calibration_status_reason": device.get("calibration_status_reason"),
        "calibration_checks": device.get("calibration_checks", []),
        "blocking_checks": blocking,
    }
