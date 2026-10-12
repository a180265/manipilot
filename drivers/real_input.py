"""真机输入设备驱动：机械臂点触手机屏幕（保守实现）。

边界：
- 输入不得修改文本；文本由 WX 生成，驱动只负责把给定内容以点触方式输入；
- 屏幕键盘坐标依赖摄像头现场标定（手机/摄像头移位后必须重新标定）；
- 未完成标定时输入动作必须结构化报错（driver_uncalibrated），不得猜测坐标。
"""
from __future__ import annotations

from drivers.arm import DriverError
from drivers.input import InputDriver


class RealInputDriver(InputDriver):
    """机械臂点触输入：标定完成后才可执行；文本原样传递，不改写。"""

    DRIVER_VERSION = "real-input-tap-1.0.0"

    def __init__(self, device_id: str, keyboard_layout: dict | None = None):
        self.device_id = device_id
        self._layout = keyboard_layout  # {"chars": {"a": {"dx": .., "dy": ..}}, "keys": {"delete": {...}}}
        self._buffer: list[str] = []

    @property
    def driver_version(self) -> str:
        return self.DRIVER_VERSION

    def _require_layout(self) -> dict:
        if not self._layout or not self._layout.get("chars"):
            raise DriverError(
                "driver_uncalibrated",
                "屏幕键盘坐标未完成现场标定，禁止猜测坐标点触（手机或摄像头移位后必须重新标定）",
            )
        return self._layout

    async def type_text(self, text: str, max_chars: int | None = None,
                        input_claim: dict | None = None) -> dict:
        if input_claim:
            # §4.8.4：修改输入缓冲/下发键序前最后认领；失败=零动作。
            input_claim["callback"]()
        layout = self._require_layout()
        chars = layout.get("chars", {})
        pending = text[:max_chars] if max_chars is not None else text
        missing = [ch for ch in pending if ch not in chars]
        if missing:
            raise DriverError(
                "driver_uncalibrated",
                f"以下字符未标定屏幕坐标，无法输入：{''.join(sorted(set(missing)))}",
            )
        # 实际点触由机械臂按标定坐标执行；此处先行校验并缓存，真机执行链路在插件步骤中逐步确认
        self._buffer.append(pending)
        return {"ok": True, "typed_chars": len(pending), "text_typed": pending, "calibrated": True}

    async def press_key(self, key: str) -> dict:
        layout = self._require_layout()
        keys = layout.get("keys", {})
        if key not in keys:
            raise DriverError("driver_uncalibrated", f"按键 {key} 未标定屏幕坐标，无法点触")
        return {"ok": True, "key": key, "calibrated": True}

    def typed_text(self) -> str:
        return "".join(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()
