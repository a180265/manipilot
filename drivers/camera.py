"""摄像头驱动抽象（最小接口）。

只回报观测和证据，不负责创建客户、不负责业务判断、不负责决定是否发送。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class CameraDriver(ABC):
    @abstractmethod
    async def open(self) -> dict:
        """打开() -> 设备信息"""

    @abstractmethod
    async def close(self) -> dict:
        """关闭() -> 关闭结果"""

    @abstractmethod
    async def capture(self) -> dict:
        """采集画面() -> 图像引用、采集时间、分辨率和哈希"""

    @abstractmethod
    async def check_quality(self) -> dict:
        """检查画面质量() -> 清晰、遮挡、过曝、冻结或异常（内部键 + 中文）"""

    @abstractmethod
    async def identify_ui_state(self) -> dict:
        """识别界面状态() -> 中文界面状态、内部兼容键和置信度"""

    @abstractmethod
    async def identify_account_and_conversation(self) -> dict:
        """识别账号和会话() -> 账号/会话线索、证据和置信度"""

    @abstractmethod
    async def confirm_main_view(self, expected_ui_state: str) -> dict:
        """确认主界面(期望界面) -> 已确认或无法确认"""

    @abstractmethod
    def is_open(self) -> bool:
        """摄像头当前是否可用。"""

    @property
    @abstractmethod
    def driver_version(self) -> str:
        """驱动版本。"""
