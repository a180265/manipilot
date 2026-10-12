"""系统键盘布局模板：机械臂直接在手机系统键盘上逐字打字。

思路（用户方案"让它直接在系统键盘上打字"）：
- 校准给出 机械坐标 ↔ 屏幕像素 的仿射映射（geometry.py）；
- 布局模板给出 每个字符 → 键位中心（全屏归一化 0..1）；
- 打字 = 逐字符：键位归一化坐标 → 屏幕像素 → 逆变换 → 机械臂绝对坐标点击。

边界（务必遵守，否则点错键）：
- 布局模板必须与手机实际显示的键盘一致：机型、屏幕方向、输入法类型（26键/九宫格/英文）、
  候选栏状态。任一变化都要重新确认布局或重新校准。
- 布局坐标是"全屏归一化"，与校准时使用的屏幕元数据同基准。
- 24 键、26 键、九宫格等任何布局都可用同一数据格式描述；内置模板仅为起点，
  可按机型截图微调（权重即按键相对宽度，行内按权重分配列宽）。
"""
from __future__ import annotations

from pathlib import Path
import json


class KeyboardLayoutError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# 内置模板：iOS/Android 系统英文 26 键 QWERTY（竖屏）。
# region 为键盘区域在全屏的归一化包围盒（u0,v0)=左上 (u1,v1)=右下；
# rows 每行是 (键名, 相对宽度权重)，行内按权重分列。
# ⚠ 不同机型/系统版本有差异，首次使用请按截图微调 region 与权重。
IOS_QWERTY_26KEY_SPEC: dict = {
    "layout_id": "ios-qwerty-26key",
    "keyboard_display": "系统英文 26 键 QWERTY（竖屏）",
    "region": {"u0": 0.0, "v0": 0.78, "u1": 1.0, "v1": 1.0},
    "rows": [
        [["q", 1], ["w", 1], ["e", 1], ["r", 1], ["t", 1], ["y", 1], ["u", 1], ["i", 1], ["o", 1], ["p", 1]],
        [["a", 1], ["s", 1], ["d", 1], ["f", 1], ["g", 1], ["h", 1], ["j", 1], ["k", 1], ["l", 1]],
        [["SHIFT", 1.4], ["z", 1], ["x", 1], ["c", 1], ["v", 1], ["b", 1], ["n", 1], ["m", 1], ["BACKSPACE", 1.4]],
        [["123", 1.2], ["SPACE", 4.4], ["RETURN", 2.0]],
    ],
}

# 常见别名：文本里的字符 → 布局特殊键
_CHAR_ALIASES = {" ": "SPACE", "\n": "RETURN"}


class KeyboardLayout:
    """字符 → 键位中心（全屏归一化坐标）。special 保存 SHIFT/SPACE 等功能键。"""

    def __init__(self, layout_id: str, keyboard_display: str,
                 keys: dict[str, tuple[float, float]], special: dict[str, tuple[float, float]],
                 region: dict):
        self.layout_id = layout_id
        self.keyboard_display = keyboard_display
        self.keys = keys
        self.special = special
        self.region = region

    # ---- 构造 ----
    @classmethod
    def from_spec(cls, spec: dict) -> "KeyboardLayout":
        for field in ("layout_id", "region", "rows"):
            if field not in spec:
                raise KeyboardLayoutError("invalid_layout", f"布局缺少字段：{field}")
        region = spec["region"]
        try:
            u0, v0 = float(region["u0"]), float(region["v0"])
            u1, v1 = float(region["u1"]), float(region["v1"])
        except (KeyError, TypeError, ValueError):
            raise KeyboardLayoutError("invalid_layout", "region 必须是 u0/v0/u1/v1 数字")
        if not (0 <= u0 < u1 <= 1 and 0 <= v0 < v1 <= 1):
            raise KeyboardLayoutError("invalid_layout", "region 必须满足 0<=u0<u1<=1 且 0<=v0<v1<=1")

        keys: dict[str, tuple[float, float]] = {}
        special: dict[str, tuple[float, float]] = {}
        row_count = len(spec["rows"])
        row_height = (v1 - v0) / row_count
        for row_index, row in enumerate(spec["rows"]):
            total_weight = sum(float(weight) for _, weight in row)
            if total_weight <= 0:
                raise KeyboardLayoutError("invalid_layout", f"第 {row_index + 1} 行权重非法")
            cursor = u0
            center_v = v0 + (row_index + 0.5) * row_height
            for label, weight in row:
                width = (u1 - u0) * (float(weight) / total_weight)
                center_u = cursor + width / 2
                cursor += width
                if len(label) == 1:
                    # 单字符（含数字/字母/符号）都是普通字符键
                    keys[label.lower()] = (center_u, center_v)
                else:
                    special[label] = (center_u, center_v)
        return cls(spec.get("layout_id", "layout"), spec.get("keyboard_display", ""),
                   keys, special, {"u0": u0, "v0": v0, "u1": u1, "v1": v1})

    @classmethod
    def builtin_qwerty(cls) -> "KeyboardLayout":
        return cls.from_spec(IOS_QWERTY_26KEY_SPEC)

    @classmethod
    def from_file(cls, path: Path) -> "KeyboardLayout":
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_spec(spec)

    # ---- 查询 ----
    def center_for(self, char: str) -> tuple[float, float]:
        """单字符 → 键位中心（归一化）。未知字符抛错（错误里带可用范围）。"""
        if len(char) != 1:
            raise KeyboardLayoutError("layout_unknown_char", "一次只查一个字符")
        lowered = char.lower()
        if lowered in self.keys:
            return self.keys[lowered]
        alias = _CHAR_ALIASES.get(char)
        if alias and alias in self.special:
            return self.special[alias]
        raise KeyboardLayoutError(
            "layout_unknown_char",
            f"布局 {self.layout_id} 没有字符「{char}」的键位；"
            f"支持字母 {sorted(self.keys)} 与功能键 {sorted(self.special)}")

    def plan_for_text(self, text: str) -> list[dict]:
        """文本 → 点击动作序列（含大写字母的 SHIFT 处理）。

        动作：{"kind": "tap", "key": "a", "u": .., "v": ..}
              {"kind": "shift", "key": "SHIFT", "u": .., "v": ..}（点击后系统自动回落大写）
        """
        actions: list[dict] = []
        for char in text:
            lowered = char.lower()
            if lowered in self.keys:
                if char.isupper():
                    if "SHIFT" in self.special:
                        u, v = self.special["SHIFT"]
                        actions.append({"kind": "shift", "key": "SHIFT", "u": u, "v": v})
                    else:
                        char = lowered
                u, v = self.keys[lowered]
                actions.append({"kind": "tap", "key": lowered, "u": u, "v": v})
                continue
            alias = _CHAR_ALIASES.get(char)
            if alias and alias in self.special:
                u, v = self.special[alias]
                actions.append({"kind": "tap", "key": alias, "u": u, "v": v})
                continue
            raise KeyboardLayoutError(
                "layout_unknown_char",
                f"布局 {self.layout_id} 无法输入字符「{char}」；"
                f"支持字母 {sorted(self.keys)} 与功能键 {sorted(self.special)}")
        return actions

    def to_dict(self) -> dict:
        return {"layoutId": self.layout_id, "keyboardDisplay": self.keyboard_display,
                "keys": {k: list(v) for k, v in self.keys.items()},
                "special": {k: list(v) for k, v in self.special.items()},
                "region": self.region}
