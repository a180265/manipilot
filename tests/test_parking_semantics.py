from runtime.parking import (
    BUSINESS_UI_PARK,
    CALIBRATION_SAFE_PARK,
    business_ui_park_record,
    calibration_safe_park_record,
)


def test_calibration_and_business_parking_roles_are_distinct():
    safe = calibration_safe_park_record({"parkX": 1.0, "parkY": 2.0, "parkZ": 3.0})
    business = business_ui_park_record("ime_delete_key", "输入法删除键位置")

    assert safe["role"] == CALIBRATION_SAFE_PARK
    assert safe["role_display"] == "校准安全停车位"
    assert business["role"] == BUSINESS_UI_PARK
    assert business["role_display"] == "业务界面停靠位"
    assert safe["role"] != business["role"]
