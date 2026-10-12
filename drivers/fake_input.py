"""fake 输入设备驱动：原样输入文本，支持中断注入。"""
from __future__ import annotations

from drivers.arm import DriverError
from drivers.input import InputDriver


class FakeInputDriver(InputDriver):
    DRIVER_VERSION = "fake-input-1.0.0"

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._buffer: list[str] = []
        self.faults: dict = {}

    @property
    def driver_version(self) -> str:
        return self.DRIVER_VERSION

    async def type_text(self, text: str, max_chars: int | None = None,
                        input_claim: dict | None = None) -> dict:
        if input_claim:
            # §4.8.4：修改输入缓冲前最后认领；失败=零字符下发。
            input_claim["callback"]()
        fail_after = self.faults.get("disconnect_after_chars")
        typed: list[str] = []
        for ch in text:
            if fail_after is not None and len(typed) >= int(fail_after):
                # 输入中途连接中断：动作已部分发生，结果无法确认
                raise DriverError("driver_disconnected", "输入过程中连接中断，已输入内容无法确认")
            typed.append(ch)
        self._buffer.append("".join(typed))
        return {"ok": True, "typed_chars": len(typed), "text_typed": "".join(typed)}

    async def press_key(self, key: str) -> dict:
        if self.faults.get("fail_press_key"):
            raise DriverError("driver_disconnected", "按键时连接中断")
        return {"ok": True, "key": key}

    def typed_text(self) -> str:
        return "".join(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()
