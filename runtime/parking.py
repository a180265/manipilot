"""设备停车语义的唯一事实源。

校准安全停车位和业务 UI 停靠位都可能是机械坐标，但用途、触发条件和
安全含义不同，禁止在状态或界面中把二者统称为“归位”。
"""
from __future__ import annotations

CALIBRATION_SAFE_PARK = "calibration_safe_park"
BUSINESS_UI_PARK = "business_ui_park"

PARKING_ROLE_DISPLAY = {
    CALIBRATION_SAFE_PARK: "校准安全停车位",
    BUSINESS_UI_PARK: "业务界面停靠位",
}


def parking_role_display(role: str | None) -> str:
    return PARKING_ROLE_DISPLAY.get(str(role or ""), "未分类停车位")


def calibration_safe_park_record(park: dict | None) -> dict:
    value = dict(park or {})
    value.setdefault("role", CALIBRATION_SAFE_PARK)
    value.setdefault("role_display", parking_role_display(CALIBRATION_SAFE_PARK))
    return value


def business_ui_park_record(target_id: str, target_display: str) -> dict:
    return {
        "target_id": target_id,
        "target_display": target_display,
        "role": BUSINESS_UI_PARK,
        "role_display": parking_role_display(BUSINESS_UI_PARK),
    }
