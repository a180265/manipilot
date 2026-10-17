"""保存校准工作状态不能改变点击落点，也不能恢复旧会话的执行资格。"""
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb
from drivers.calibration_wizard import CalibrationWizard


QUADRATIC_MODEL = {
    "a": 10.0, "b": 0.5, "c": 100.0,
    "d": 0.3, "e": 8.0, "f": 1300.0,
    "qx2": 0.02, "qxy": -0.01, "qy2": 0.005,
    "px2": -0.004, "pxy": 0.008, "py2": 0.015,
}


class NoHardware:
    def __getattr__(self, name):
        raise AssertionError(f"保存校准资料不应访问硬件：{name}")


@pytest.mark.parametrize(
    "status,rebind",
    [("draft", False), (cb.MOTION_READY_STATUS, False),
     (cb.MOTION_READY_STATUS, True)],
    ids=["draft", "motion-ready", "session-rebind"],
)
def test_saving_wizard_settings_preserves_calibrated_destinations(tmp_path, status, rebind):
    wizard = CalibrationWizard(
        device="model-persistence", daemon=NoHardware(), ime=NoHardware(),
        data_root=tmp_path, real_hardware=False,
    )
    wizard.state["xy"]["fit"] = {
        **QUADRATIC_MODEL, "residualRmsPx": 0.2, "maxResidualPx": 0.4,
    }
    wizard.state["sessionRebind"].update({
        "required": rebind,
        "pendingScopes": {"z": rebind, "xy": rebind, "park": rebind},
    })
    bundle = cb.new_bundle("model-persistence")
    bundle["status"] = status
    bundle["xy"].update({
        "robotToScreen": deepcopy(QUADRATIC_MODEL),
        "residualRmsPx": 0.2, "maxResidualPx": 0.4,
    })
    cb.save_bundle(bundle, tmp_path)

    # 使用不触发动作的公开设置入口保存。重绑后的第一次保存降为草稿，
    # 第二次保存曾只写仿射项，导致相同屏幕目标的机械落点变化。
    wizard.park_clear()
    wizard.park_clear()
    saved = cb.load_bundle("model-persistence", tmp_path)

    # 手算对应点，覆盖中心及不同方向；落点读取走生产反解函数。
    for screen, robot in [
        ((424.0, 1476.2), (30.0, 20.0)),
        ((779.5, 1765.1), (60.0, 50.0)),
        ((59.875, 537.775), (-5.0, -125.0)),
    ]:
        assert cb.robot_from_screen(saved["xy"]["robotToScreen"], *screen) == pytest.approx(
            robot, abs=0.01,
        )
    assert saved["xy"]["robotToScreen"] == QUADRATIC_MODEL
    assert saved["xy"]["residualRmsPx"] == 0.2
    assert saved["xy"]["maxResidualPx"] == 0.4
    assert saved["status"] == ("draft" if rebind else status)
    assert saved["sessionRebind"]["required"] is rebind
    assert saved["sessionRebind"]["pendingScopes"] == {
        "z": rebind, "xy": rebind, "park": rebind,
    }
