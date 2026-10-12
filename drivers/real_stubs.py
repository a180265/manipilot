"""真机驱动适配器（占位实现）。

约束（docs/独立Agent实施交接清单.md 第三节）：
- 机械臂型号、SDK、连接方式、坐标标定、摄像头型号、微信环境、账号绑定、
  急停策略、证据保存策略等均未由项目方确认（configs/硬件输入确认表.json）。
- 在确认表 all_confirmed=true 且所有条目“已确认”之前，真机驱动必须拒绝启用，
  系统停留在仿真模式；不得用默认值猜测硬件参数并驱动真机。

接入真机时：按 drivers/arm.py、drivers/camera.py、drivers/input.py 的最小接口
实现 RealArmDriver / RealCameraDriver / RealInputDriver，并把
configs/硬件输入确认表.json 逐项填写为“已确认”。
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIRMATION_TABLE = Path(__file__).resolve().parents[1] / "configs" / "硬件输入确认表.json"


def hardware_confirmed(table_path: Path | None = None) -> tuple[bool, str]:
    """检查硬件输入确认表：全部“已确认”才允许真机模式。

    table_path 为 None 时按调用时的模块级 CONFIRMATION_TABLE 解析
    （便于测试用临时确认表替换）。
    """
    table_path = table_path or CONFIRMATION_TABLE
    if not table_path.exists():
        return False, "缺少 configs/硬件输入确认表.json"
    try:
        table = json.loads(table_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"确认表解析失败：{exc}"
    if not table.get("all_confirmed"):
        pending = [item["input"] for item in table.get("items", []) if item.get("status") != "已确认"]
        return False, f"以下硬件输入待项目方确认：{pending}"
    for item in table.get("items", []):
        if item.get("status") != "已确认":
            return False, f"硬件输入未确认：{item.get('input')}"
    return True, "硬件输入已全部确认"


class RealHardwareUnavailable(RuntimeError):
    """真机参数未确认时抛出，驱动拒绝启用。"""


def require_confirmed_hardware():
    confirmed, reason = hardware_confirmed()
    if not confirmed:
        raise RealHardwareUnavailable(f"真机硬件参数未确认，拒绝启用真机驱动（{reason}）")


class RealArmDriverStub:
    """真机机械臂驱动占位：任何动作调用都会拒绝，直到硬件参数确认。"""

    driver_version = "real-arm-stub-0.0.1"

    def __init__(self, device_id: str):
        self.device_id = device_id

    def _refuse(self) -> None:
        require_confirmed_hardware()

    async def connect(self):
        self._refuse()

    async def read_state(self):
        self._refuse()

    async def go_home(self):
        self._refuse()

    async def move_preset(self, position_id):
        self._refuse()

    async def click(self, target):
        self._refuse()

    async def stop(self):
        self._refuse()

    async def estop(self):
        self._refuse()

    async def disconnect(self):
        return {"connected": False}
