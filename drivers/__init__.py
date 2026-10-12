"""驱动层：封装具体 SDK 的抽象接口与实现。

驱动层负责单位换算、速度/加速度上限、软限位、碰撞/过载错误、超时和断连；
上层插件不得直接调用 SDK，只能通过运行时下发的能力调用。
接口语义遵循 docs/独立Agent实施交接清单.md 第五节、第六节的最小接口。
"""
from drivers.arm import ArmDriver, DriverError
from drivers.camera import CameraDriver
from drivers.input import InputDriver
from drivers.fake_arm import FakeArmDriver
from drivers.fake_camera import FakeCameraDriver
from drivers.fake_input import FakeInputDriver

__all__ = [
    "ArmDriver",
    "CameraDriver",
    "InputDriver",
    "DriverError",
    "FakeArmDriver",
    "FakeCameraDriver",
    "FakeInputDriver",
]
