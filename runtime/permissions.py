"""权限模型：只读、运维、应急三级。

- 只读（readonly）：查询类接口与实时画面查看。
- 运维（operator）：暂停/恢复/隔离/解除隔离/归位/取消任务/查看画面。
- 应急（responder）：全部运维权限 + 全局急停与急停复位。

第一版同机部署仅监听 127.0.0.1，不做应用层鉴权，但后端仍必须区分并
拒绝越权操作（前端隐藏无权限操作，后端必须再次拒绝）。
"""
from __future__ import annotations

from runtime.errors import ApiError

ROLE_DISPLAY: dict[str, str] = {
    "readonly": "只读",
    "operator": "运维",
    "responder": "应急",
    "system": "系统",
    "wx": "外部系统（WX）",
}

# 操作 -> 允许的最低角色（层级：readonly/wx < operator < responder）
ROLE_LEVEL: dict[str, int] = {"readonly": 0, "wx": 1, "operator": 1, "responder": 2, "system": 3}

OPERATION_ROLES: dict[str, str] = {
    "device_pause": "operator",
    "device_resume": "operator",
    "device_quarantine": "operator",
    "device_unquarantine": "operator",
    "device_home_check": "operator",
    "device_recover_communication": "operator",
    "device_park": "operator",
    "device_operator_recovery_review": "operator",
    "device_reset_home": "operator",
    "device_status_query": "operator",
    "gesture_settings_update": "operator",
    "input_settings_update": "operator",
    "navigation_template_register": "operator",
    "automatic_gesture_validate": "operator",
    "device_register": "operator",
    "device_attach": "operator",
    "device_remove": "operator",
    "device_binding_update": "operator",
    "device_binding_disable": "operator",
    "device_binding_enable": "operator",
    "supervision_start": "operator",
    "supervision_stop": "operator",
    "supervision_configure": "operator",
    "task_cancel": "operator",
    "task_submit": "operator",
    "view_session_create": "readonly",
    "view_session_renew": "readonly",
    "view_session_close": "readonly",
    "human_takeover_request": "operator",
    "plugin_install": "operator",
    "plugin_lifecycle": "operator",
    "alert_resolve": "operator",
    "emergency_stop": "responder",
    "emergency_stop_reset": "responder",
}

# 危险操作：必须二次确认（confirmation_token）
DANGEROUS_OPERATIONS: set[str] = {
    "device_quarantine",
    "device_attach",
    "device_remove",
    "device_binding_update",
    "device_unquarantine",
    "device_reset_home",
    "device_recover_communication",
    "device_park",
    "device_operator_recovery_review",
    "human_takeover_request",
    "emergency_stop",
    "emergency_stop_reset",
    "plugin_uninstall",
    "plugin_upgrade",
    "plugin_rollback",
}

REQUIRED_CONFIRMATION_TOKEN = "确认执行"

# 写操作的必填字段
WRITE_REQUIRED_FIELDS = ("operator_id", "reason", "correlation_id", "client_request_id")


def check_operation(operation: str, context: dict) -> None:
    """统一写操作校验：必填字段、非空文本原因、角色权限、二次确认。

    context: {operator_id, role, reason, correlation_id, client_request_id, confirmation_token}
    """
    for field in WRITE_REQUIRED_FIELDS:
        if not context.get(field):
            raise ApiError("missing_field", f"写操作缺少必填字段：{field}")
    if not isinstance(context["reason"], str):
        raise ApiError("invalid_request", "操作原因必须是文本")
    if not context["reason"].strip():
        raise ApiError("missing_field", "写操作缺少必填字段：reason")
    role = context.get("role", "readonly")
    required = OPERATION_ROLES.get(operation, "operator")
    if ROLE_LEVEL.get(role, 0) < ROLE_LEVEL[required]:
        raise ApiError("permission_denied", f"角色「{ROLE_DISPLAY.get(role, role)}」无权执行「{required}」级操作")
    if operation in DANGEROUS_OPERATIONS and context.get("confirmation_token") != REQUIRED_CONFIRMATION_TOKEN:
        raise ApiError("confirmation_required", f"「{operation}」为危险操作，必须携带确认令牌「{REQUIRED_CONFIRMATION_TOKEN}」")


def role_display(role: str) -> str:
    return ROLE_DISPLAY.get(role, role)
