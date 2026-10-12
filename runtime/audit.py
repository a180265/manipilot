"""审计日志：所有运维操作、任务提交、插件生命周期和自动安全反应必须留痕。

审计事件包含：操作者、角色、动作（键+中文）、时间、设备、任务、操作原因、
关联编号、结果（成功/拒绝/失败）和详情。
"""
from __future__ import annotations

# 运维与平台动作的中文显示
ACTION_DISPLAY: dict[str, str] = {
    "device_pause": "暂停设备",
    "device_resume": "恢复设备",
    "device_quarantine": "隔离设备",
    "device_unquarantine": "解除隔离",
    "vision_swipe_selftest": "视觉滑动自检",
    "device_home_check": "归位检查",
    "device_reset_home": "重置归位",
    "device_status_query": "人工状态查询",
    "device_register": "注册设备",
    "device_attach": "挂载/重绑定设备",
    "device_remove": "移除设备",
    "device_binding_update": "设备绑定变更",
    "navigation_template_register": "导航模板登记",
    "device_binding_disable": "禁用设备绑定",
    "device_binding_enable": "恢复设备绑定",
    "input_settings_update": "输入开关变更",
    "input_recovery_review": "输入恢复人工核对",
    "supervision_start": "开始微信托管",
    "supervision_stop": "停止微信托管",
    "supervision_configure": "配置微信托管名单",
    "emergency_stop": "全局急停",
    "emergency_stop_reset": "急停复位",
    "task_submit": "提交任务",
    "task_dispatch_failed": "派发失败（任务未执行）",
    "task_dispatch_storage_error": "派发早段失败（任务保持排队重试）",
    "executor_early_error": "执行协程早期异常收尾",
    "task_cancel": "取消任务",
    "task_duplicate_blocked": "重复任务拦截",
    "task_result_reported": "任务结果回报",
    "customer_message_reported": "客户消息回报",
    "task_lease_retained": "任务租约保留待人工处理",
    "task_lease_released_after_review": "人工确认后释放任务租约",
    "home_verification_failed": "主界面或归位确认失败",
    "auto_pause_device": "自动暂停设备",
    "auto_quarantine_device": "自动隔离设备",
    "task_marked_reviewed": "人工标记已核对",
    "scroll_startup_scan": "滑动动作存储启动扫描",
    "scroll_startup_scan_failed": "滑动动作存储启动扫描失败（保持阻断）",
    "visual_pause_lease_released": "纯视觉未知释放任务租约（存储阻断承担）",
    "wx_navigation_safety_view": "WX 恢复排空安全视图查询",
    "wx_review_receipt_issued": "WX 标读核对回执签发",
    "plugin_install": "安装插件",
    "plugin_activate": "启用插件",
    "plugin_deactivate": "停用插件",
    "plugin_uninstall": "卸载插件",
    "plugin_upgrade": "升级插件",
    "plugin_rollback": "回滚插件",
    "plugin_violation_blocked": "插件越权拦截",
    "view_session_create": "创建实时画面查看会话",
    "view_session_renew": "续期查看会话",
    "view_session_close": "关闭查看会话",
    "view_session_expired": "查看会话过期",
    "human_takeover_request": "请求 WX 人工接管",
    "alert_resolve": "告警人工处理",
    "heartbeat_lost": "心跳丢失",
    "camera_check": "摄像头检查",
    "driver_selfcheck": "驱动自检",
    "capability_call": "能力调用",
    # 2026-09-07 真机事故补登：daemon 掉线事件走审计时键未登记，
    # ValueError 未捕获直接杀死 8788 agent 进程（监控上报不得击穿宿主）。
    "daemon_unavailable": "设备守护不可用",
    "supervision_exit_conversation": "托管退出会话",
    "calibration_restored": "恢复既有校准资格",
    "screen_wake_manual_confirm": "人工唤醒单次现场确认",
}

OUTCOME_DISPLAY: dict[str, str] = {
    "success": "成功",
    "rejected": "已拒绝",
    "failed": "失败",
}


class AuditLog:
    def __init__(self, store, events):
        self._store = store
        self._events = events

    def record(
        self,
        action: str,
        *,
        operator_id: str = "system",
        operator_role: str = "system",
        outcome: str = "success",
        device_id: str | None = None,
        task_id: str | None = None,
        reason: str | None = None,
        correlation_id: str | None = None,
        client_request_id: str | None = None,
        detail: dict | None = None,
        occurred_at: str | None = None,
    ) -> dict:
        if action not in ACTION_DISPLAY:
            # 2026-09-07 事故加固：未知键降级为原键落盘+告警事件，绝不抛异常
            # ——审计是留痕通道，没有权力杀死调用方（曾因缺键 ValueError
            # 让 daemon 掉线监控把整个 8788 agent 打死）。
            self._events.publish("audit.unregistered_action", {"action": action})
        from runtime.util import iso, utc_now

        event = {
            "audit_id": None,
            "action": action,
            "action_display": ACTION_DISPLAY.get(action, action),
            "operator_id": operator_id,
            "operator_role": operator_role,
            "outcome": outcome,
            "outcome_display": OUTCOME_DISPLAY.get(outcome, outcome),
            "device_id": device_id,
            "task_id": task_id,
            "reason": reason,
            "correlation_id": correlation_id,
            "client_request_id": client_request_id,
            "detail": detail or {},
            "occurred_at": occurred_at or iso(utc_now()),
        }
        seq = self._store.append_audit(event)
        event["audit_id"] = seq
        self._events.publish("audit.created", {"audit_id": seq, "action": action})
        return event

    def list_events(self, limit: int = 200, device_id: str | None = None) -> list[dict]:
        return self._store.audit_events(limit=limit, device_id=device_id)
