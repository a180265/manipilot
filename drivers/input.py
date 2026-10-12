"""输入设备驱动抽象。

输入() 不得修改文本：驱动只负责把给定文本原样输入。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class InputDriver(ABC):
    @abstractmethod
    async def type_text(self, text: str, max_chars: int | None = None) -> dict:
        """输入(文本) -> 动作结果；文本必须原样输入，不得修改。"""

    @abstractmethod
    async def press_key(self, key: str) -> dict:
        """按键(键名) -> 动作结果。"""

    @abstractmethod
    def typed_text(self) -> str:
        """已输入文本缓存（用于核对外观文本哈希，仅限驱动内部与运行时取证）。"""

    @abstractmethod
    def clear(self) -> None:
        """清空输入缓存。"""

    @property
    @abstractmethod
    def driver_version(self) -> str:
        """驱动版本。"""
