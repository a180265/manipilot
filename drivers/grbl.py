"""Grbl 串口层：传输抽象、安全策略和测试用模拟控制器。

依据现场文档（2026-08-30，BACHIN-3C-TA4 / ESP32 / Grbl_ESP32 1.3b）：
- 串口 /dev/cu.usbserial-212420，115200 8N1，无流控；
- 逐行发送、逐条等待 ok；小批量发送，不一次灌入几十行；
- 禁止 $H/$HX/$HY/$HZ 自动回零（历史回零方向曾与机构不匹配）；
- 不自动寻找 Z 限位（无可靠 Z 限位）；FORBIDDEN_Z_FEEDS 机制保留（当前为空）；
- 不使用历史文档中的绝对坐标；会话内归位只允许回到连接时记录的 MPos；
- MPos/ok 只代表控制器接受，不代表机械真实移动（必须摄像头确认）。

真机驱动不得绕过本模块的策略校验直接发送 G-code。
"""
from __future__ import annotations

import re
import os
import time
from typing import Callable

# ---------- 常量（现场文档确认值） ----------

SERIAL_PORT_DEFAULT = "/dev/cu.usbserial-212420"
BAUD_RATE = 115200

# 禁止的命令（现场文档明确禁止项）
FORBIDDEN_COMMANDS: tuple[str, ...] = ("$H", "$HX", "$HY", "$HZ")
# 2026-09-18 设备更换后解除 F300 历史失步禁令；复现时可改回 (300.0,)
FORBIDDEN_Z_FEEDS: tuple[float, ...] = ()

# Z 轴进给范围（mm/min）：保守 30 ～ 已验证 2000；XY：保守 100 ～ 已验证 7000
Z_FEED_MIN, Z_FEED_MAX = 30.0, 2000.0
XY_FEED_MIN, XY_FEED_MAX = 100.0, 7000.0
# 单次行程软上限：Z 无限位时优先 1-3 mm；XY 业务要求一次直达，
# 由上层总位移 300mm 门禁控制。
Z_STROKE_MAX_MM = 10.0
XY_STROKE_MAX_MM = 300.0
Z_FEED_CONSERVATIVE = 30.0
Z_FEED_CLICK = 1500.0
Z_FEED_VERIFIED = 2000.0
XY_FEED_CONSERVATIVE = 100.0
XY_FEED_VERIFIED = 7000.0

# 允许的指令词：模式字（G90/G91/G21/G94/G1）与轴/进给字（X/Y/Z/F + 数值）
# G92 已从白名单移除：本固件 G92 只偏移 WPos 不改 MPos，且会给 G90 目标引入
# 隐藏偏移；平台锚定一律用软件记录 MPos，连接时仅允许 G92.1 清除历史偏移。
_WORD_RE = re.compile(r"^(?:G9[01]|G21|G94|G1|[XYZF][-+]?\d+(?:\.\d+)?)$")
_MAX_WORDS_PER_LINE = 6


class GrblPolicyError(Exception):
    """违反现场安全策略的命令（必须先拦截，不得下发到串口）。"""

    def __init__(self, message: str, line: str = ""):
        self.line = line
        super().__init__(message)


def validate_line(
    line: str,
    *,
    distance_mode: str = "G91",
    allow_session_home: bool = False,
) -> None:
    """单行命令策略校验；违反立即抛 GrblPolicyError。

    - $ 系统命令除只读查询外全部拒绝，$H* 明确禁止；
    - 运动指令仅允许 G90/G91/G21/G94/G1 + 轴字 + F 字；
    - Z 轴进给必须在已验证范围内（禁用值表当前为空）；
    - 单次行程不得超过软上限；
    - 绝对坐标运动（G90 模式下的 G1）默认禁止，仅归位回会话原点时放行。
    """
    text = line.strip()
    if not text:
        return
    upper = text.upper()
    if upper.startswith("$"):
        for forbidden in FORBIDDEN_COMMANDS:
            if upper.startswith(forbidden):
                raise GrblPolicyError(f"现场文档明确禁止的命令：{forbidden}", line)
        if not upper.startswith("?"):
            raise GrblPolicyError("除状态查询 ? 外禁止发送 $ 系统命令", line)
        return
    if upper in ("!", "\x85"):
        return  # 进给保持（软停止），由驱动在确认流程中使用
    if upper == "?":
        return  # 只读状态查询
    if upper == "G92.1":
        return  # 清除 G92 偏移（不产生运动；连接时由驱动发送，保证坐标系干净）
    words = upper.split()
    if not words or len(words) > _MAX_WORDS_PER_LINE or not all(_WORD_RE.match(w) for w in words):
        raise GrblPolicyError("仅允许 G90/G91/G21/G94/G1 模板指令与 X/Y/Z/F 轴进给字", line)
    if words[0] == "G1":
        is_absolute = distance_mode.upper() == "G90"
        _validate_g1(
            words[1:],
            allow_session_home=allow_session_home and is_absolute,
            absolute_move=is_absolute,
        )


def _validate_g1(words: list[str], *, allow_session_home: bool, absolute_move: bool) -> None:
    axis_values: dict[str, float] = {}
    feed: float | None = None
    for word in words:
        head = word[0].upper()
        try:
            value = float(word[1:])
        except ValueError as exc:
            raise GrblPolicyError(f"无法解析运动字：{word}", "G1 " + " ".join(words)) from exc
        if head in ("X", "Y", "Z"):
            axis_values[head] = value
        elif head == "F":
            feed = value
        else:
            raise GrblPolicyError(f"运动行包含未允许的字：{word}", "G1 " + " ".join(words))
    if not axis_values and feed is None:
        raise GrblPolicyError("空的 G1 行", "G1")
    if "Z" in axis_values:
        z = axis_values["Z"]
        if not absolute_move and abs(z) > Z_STROKE_MAX_MM:
            raise GrblPolicyError(f"Z 单次行程 {abs(z)}mm 超过软上限 {Z_STROKE_MAX_MM}mm（Z 无可靠限位）", "G1 Z")
        if feed is not None:
            if any(abs(feed - banned) < 1e-6 for banned in FORBIDDEN_Z_FEEDS):
                raise GrblPolicyError(f"Z 轴进给 F{feed:g} 属历史失步参数，禁止使用", "G1 Z")
            if not (Z_FEED_MIN <= feed <= Z_FEED_MAX):
                raise GrblPolicyError(f"Z 轴进给 F{feed:g} 超出已验证范围 {Z_FEED_MIN}-{Z_FEED_MAX}", "G1 Z")
    for axis in ("X", "Y"):
        if axis in axis_values:
            value = abs(axis_values[axis])
            if not absolute_move and value > XY_STROKE_MAX_MM:
                raise GrblPolicyError(f"{axis} 单次行程 {value}mm 超过软上限 {XY_STROKE_MAX_MM}mm（机械边界未完全确认）", f"G1 {axis}")
            if feed is not None and not (XY_FEED_MIN <= feed <= XY_FEED_MAX):
                raise GrblPolicyError(f"{axis} 轴进给 F{feed:g} 超出范围 {XY_FEED_MIN}-{XY_FEED_MAX}", f"G1 {axis}")
    if not absolute_move:
        return  # 相对小行程模板放行
    if not allow_session_home:
        raise GrblPolicyError("绝对坐标运动仅允许用于回到会话原点（归位）", "G90 G1")


# ---------- 传输抽象 ----------

class GrblTransport:
    """串口传输抽象：真机用 pyserial，测试用 EmulatedGrblController。"""

    def open(self) -> str:  # -> 启动信息
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def send_line(self, line: str, *, timeout: float = 10.0) -> list[str]:
        """发送一行并等待响应（ok / error / 状态报文），返回响应行列表。"""
        raise NotImplementedError

    def is_open(self) -> bool:
        raise NotImplementedError


class PyserialGrblTransport(GrblTransport):
    """真机串口传输（pyserial，延迟导入）。"""

    def __init__(self, port: str = SERIAL_PORT_DEFAULT, baud: int = BAUD_RATE):
        self.port = port
        self.baud = baud
        self._serial = None

    def open(self) -> str:
        if os.environ.get("ARMFLEET_ALLOW_DIRECT_SERIAL") != "1":
            raise GrblPolicyError(
                "直连串口默认禁用：正式运行必须通过 daemon。"
                "离线验收需人工停止对应 daemon、确认现场安全后，"
                "仅为本次验收设置 ARMFLEET_ALLOW_DIRECT_SERIAL=1")
        import serial  # 延迟导入：仿真环境无需安装 pyserial

        try:
            self._serial = serial.Serial(
                port=self.port, baudrate=self.baud, bytesize=8, parity="N",
                stopbits=1, xonxoff=False, rtscts=False, dsrdtr=False, timeout=2,
                write_timeout=2,
            )
        except Exception as exc:
            raise GrblPolicyError(f"打开串口 {self.port} 失败：{exc}") from exc
        # 不清缓冲直接读：Grbl 在端口打开/复位时输出启动信息，先清缓冲会把
        # 已到达的横幅冲掉；控制器常驻运行时也可能没有横幅（此时以 ? 为准）。
        banner = self._read_until_idle(timeout=2.5)
        return banner or ""

    def _read_until_idle(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        collected: list[str] = []
        while time.monotonic() < deadline:
            raw = self._serial.readline()
            if not raw:
                continue
            try:
                collected.append(raw.decode("utf-8", "replace").strip())
            except Exception:
                continue
            if any(line.startswith("Grbl") or "Grbl" in line for line in collected):
                break
        return "\n".join(line for line in collected if line)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def send_line(self, line: str, *, timeout: float = 10.0) -> list[str]:
        if self._serial is None:
            raise GrblPolicyError("串口未打开")
        self._serial.reset_input_buffer()
        payload = (line.strip() + "\n").encode("utf-8")
        self._serial.write(payload)
        self._serial.flush()
        responses: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self._serial.readline()
            if not raw:
                continue
            text = raw.decode("utf-8", "replace").strip()
            if text:
                responses.append(text)
            if text in ("ok",) or text.startswith("error") or text.startswith("<"):
                break
        return responses

    def is_open(self) -> bool:
        return self._serial is not None and getattr(self._serial, "is_open", False)


class EmulatedGrblController(GrblTransport):
    """进程内 Grbl 模拟器：按真实固件语义响应，支持故障注入（测试用）。

    故障注入：
    - alarm_on_connect：连接即处于 Alarm 状态；
    - drop_ack_next：下一条运动命令不回 ok（模拟丢行/超时）；
    - disconnect_after_lines：发送 N 行后串口消失（模拟拔线）；
    - phantom_motion：控制器坐标变化但画面不移动（复现"失步"，用于
      验证"ok/MPos 变化不能证明真实移动"的摄像头确认规则）。
    """

    BANNER = "Grbl_ESP32 1.3b ['$' for help]"

    def __init__(self, *, initial_mpos: tuple[float, float, float] = (0.0, 0.0, 403.7)):
        self.mpos = list(initial_mpos)
        self.state = "Idle"
        self._open = False
        self._distance_mode = "G90"
        self.sent_lines: list[str] = []
        self.faults: dict[str, object] = {}
        self.on_motion: Callable[[], None] | None = None  # 挂钩：模拟摄像头画面随机械移动
        self._drop_ack = False
        self._lines_after_open = 0

    # ---- 传输接口 ----
    def open(self) -> str:
        if self.faults.get("alarm_on_connect"):
            self.state = "Alarm"
        self._open = True
        self._lines_after_open = 0
        return self.BANNER

    def close(self) -> None:
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def _maybe_disconnect(self) -> None:
        limit = self.faults.get("disconnect_after_lines")
        if limit is not None and self._lines_after_open >= int(limit):
            self._open = False
            raise GrblPolicyError("串口已断开（模拟拔线）")

    def send_line(self, line: str, *, timeout: float = 10.0) -> list[str]:
        if not self._open:
            raise GrblPolicyError("串口未打开")
        self._maybe_disconnect()
        self._lines_after_open += 1
        text = line.strip()
        self.sent_lines.append(text)
        upper = text.upper()
        if upper == "?":
            return [f"<{self.state}|MPos:{self.mpos[0]:.3f},{self.mpos[1]:.3f},{self.mpos[2]:.3f}|FS:0,0>"]
        if upper == "!":
            self.state = "Hold" if self.state == "Run" else self.state
            return ["ok"]
        if upper.startswith("$"):
            return ["ok"]
        # G-code 行
        if self._drop_ack:
            self._drop_ack = False
            return []  # 模拟丢行：无响应直至超时
        if self.faults.get("drop_ack_next"):
            self.faults.pop("drop_ack_next")
            self._drop_ack = True
            return []
        self._apply_gcode(upper)
        return ["ok"]

    def _apply_gcode(self, upper: str) -> None:
        words = upper.split()
        if not words:
            return
        if words[0] in ("G90", "G91"):
            self._distance_mode = words[0]
            return
        if words[0] == "G92":
            # G92 不产生运动：把当前位置读数改为给定值（标定坐标用）
            for word in words[1:]:
                head = word[0]
                try:
                    value = float(word[1:])
                except ValueError:
                    continue
                if head in ("X", "Y", "Z"):
                    self.mpos[{"X": 0, "Y": 1, "Z": 2}[head]] = value
            return
        if words[0] != "G1":
            return
        target = list(self.mpos)
        for word in words[1:]:
            head = word[0]
            try:
                value = float(word[1:])
            except ValueError:
                continue
            if head in ("X", "Y", "Z"):
                idx = {"X": 0, "Y": 1, "Z": 2}[head]
                target[idx] = target[idx] + value if self._distance_mode == "G91" else value
        # ok 只代表控制器接受并计算了运动；phantom_motion 故障模拟
        # "坐标变化但机械未动"（失步），此时不触发画面联动。
        self.state = "Run"
        self.mpos = target
        if self.on_motion is not None and not self.faults.get("phantom_motion"):
            self.on_motion()
        self.state = "Idle"


def parse_status_report(line: str) -> dict | None:
    """解析 Grbl 状态报文 <State|MPos:x,y,z|FS:f,s>。无 MPos 视为无效（半行/噪声）。"""
    text = line.strip()
    if not (text.startswith("<") and text.endswith(">")):
        return None
    fields = text[1:-1].split("|")
    report: dict = {"state": fields[0]}
    for field in fields[1:]:
        if field.startswith("MPos:"):
            try:
                x, y, z = (float(v) for v in field[5:].split(","))
            except ValueError:
                return None
            report["mpos"] = {"x": x, "y": y, "z": z}
    if "mpos" not in report:
        return None
    return report
