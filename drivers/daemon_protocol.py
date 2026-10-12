"""daemon 协议常量与安全限值：arm_daemon3、驱动桥、调试台三处共同引用。

本模块是 daemon TCP 协议的唯一事实源：
- 指令集与欢迎行；
- 现场安全限值（与 drivers/grbl.py 现场策略一致，daemon 执行时强制、桥侧预检同规则）；
- 运动租约/事务/急停锁定指令（v1.2，中文说明见 STATUS/审计输出）；
- 分段工具（把长位移拆成 daemon 单段限值内的相对移动）。
"""
from __future__ import annotations
import math

DAEMON_WELCOME = "arm-daemon3 ready"
DEFAULT_DAEMON_ADDR = "127.0.0.1:8795"
DEFAULT_SERIAL_PORT = "/dev/cu.usbserial-212420"

# ---------- 安全限值（对齐 drivers/grbl.py 现场策略） ----------
Z_FEED_MIN, Z_FEED_MAX = 30.0, 2000.0
XY_FEED_MIN, XY_FEED_MAX = 100.0, 7000.0
Z_STROKE_MAX = 10.0          # Z 单段行程上限（Z 无可靠限位）
# XY 业务走位要求单条指令直达；300mm 同时作为总位移与单条直达上限。
# 调试台人工点动仍在 motor_debug_server 侧单独限制为 30mm。
XY_STROKE_MAX = 300.0
# 历史失步参数 F300 禁令已于 2026-09-18 随设备更换解除（用户确认）；
# Z 进给范围检查 F30-F2000 仍然强制。若新设备复现失步，
# 在 grbl.FORBIDDEN_Z_FEEDS 与本模块恢复该值即可。
DEFAULT_Z_FEED = 30.0
DEFAULT_XY_FEED = 150.0
# 统一校准通过后的业务映射走位速度；点击 Z 进给仍单独使用 tapFeed。
CALIBRATED_XY_FEED = 5000.0
DEFAULT_TAP_FEED = 1500.0
TAP_DWELL_MAX_S = 5.0  # Includes the existing delete-key long-press limit.
DOUBLE_TAP_INTERVAL_MIN_MS = 50.0
DOUBLE_TAP_INTERVAL_MAX_MS = 500.0
DOUBLE_TAP_FEATURE = "tap2.v1"
DOUBLE_TAP_SHORT_LIFT_FEATURE = "tap2.short-lift.v1"
DOUBLE_TAP_SHORT_LIFT_MAX_MM = 1.0
# Gesture-local release stroke; the calibrated contact plane stays unchanged.
DOUBLE_TAP_RELEASE_MM = 3.0
DOUBLE_TAP_DEFAULT_INTERVAL_MS = 50
DOUBLE_TAP_FAST_FEATURE = "tap2.feed-3000.v1"
DOUBLE_TAP_FEED_MAX = 3000.0
SWIPE_DURATION_MIN_MS = 150.0
SWIPE_DURATION_MAX_MS = 5000.0
SWIPE_FEED_MAX = 3000.0
SWIPE_TOTAL_MAX_MM = 150.0
SWIPE_FEATURE = "swipe.v1"

# PATH1 连续路径宏（path.v1，规范第 6.5/22.9 节）：沿用滑动限值，不新放宽。
PATH1_FEATURE = "path.v1"
PATH1_VERSION = 1
PATH1_MAX_SEGMENTS = 256          # segments 数组上限（含首 down 与末 up）
PATH1_HOLD_MS_MAX = 2000          # 单个 hold 上限（控制器 G4）
PATH1_CONTACT_TIME_MAX_MS = 5000  # 全宏 hold+move 计划时间总和上限
PATH1_LINE_MAX_BYTES = 32768      # 整行 UTF-8（含换行）字节上限

# 驱动桥运动编排限值（比 daemon 更保守一层）
HOVER_SEGMENT_MAX_MM = 9.5    # Z 回悬停的单段上限
# daemon 允许的单条 XY 指令上限；在不超过硬件软限位时直接一步到位。
# 超过该上限的目标仍按安全规则拆分，避免把保护阈值改成不可控的大步运动。
# Compatibility name retained for callers; XY no longer segments. It is now
# the maximum direct business travel accepted by the bridge/daemon.
TRAVEL_SEGMENT_MAX_MM = XY_STROKE_MAX
TRAVEL_TOTAL_MAX_MM = 300.0   # 单次点击总位移上限（超限判映射异常）

# ---------- 运动租约（v1.2） ----------
LEASE_TTL_S = 90.0
LEASE_OWNER_BUSINESS = "business"  # 业务运行时（DaemonArmDriver）
LEASE_OWNER_DEBUG = "debug"        # 电机调试台（运维/调试）
LEASE_NOTICE_BUSINESS = "业务运动租约持有中，调试台默认只读（STOP/ESTOP 不受影响）"

# ---------- 业务固定目标（协议键稳定，控制台/审计显示中文） ----------
TARGET_DISPLAY_NAMES: dict[str, str] = {
    "ime_candidate_bar": "输入法候选栏",
    "ime_delete_key": "输入法删除键位置",
    "input_box": "微信输入框",
    "send_button": "发送按钮",
    "back_button": "返回主界面",
    "conversation_item": "微信会话入口",
}
# Dynamic-only points must not appear in fixed-target calibration presets.
DYNAMIC_TARGET_DISPLAY_NAMES = {"gesture_point": "视觉手势点"}
# 目标默认动作模式：tap=移动+下压抬起；hover_only=仅移动并停在悬停高度（不执行 TAP）
DEFAULT_TARGET_MODES: dict[str, str] = {
    "ime_candidate_bar": "tap",
    "ime_delete_key": "hover_only",
    "input_box": "tap",
    "send_button": "tap",
    "back_button": "tap",
    "conversation_item": "tap",
}
TARGET_MODE_TAP = "tap"
TARGET_MODE_HOVER_ONLY = "hover_only"


def check_feed(feed: float, *, axis: str) -> None:
    """进给限值校验；违反抛 ValueError（中文说明，daemon 与桥共用）。"""
    lo, hi = (Z_FEED_MIN, Z_FEED_MAX) if axis == "Z" else (XY_FEED_MIN, XY_FEED_MAX)
    if not lo <= feed <= hi:
        raise ValueError(f"{axis} 进给 F{feed:g} 超出安全范围 {lo:g}-{hi:g}")


def check_stroke(value: float, *, axis: str) -> None:
    """单段行程校验。"""
    if not math.isfinite(value):
        raise ValueError(f"{axis} 行程必须是有限数值")
    limit = Z_STROKE_MAX if axis == "Z" else XY_STROKE_MAX
    if abs(value) > limit:
        raise ValueError(f"{axis} 单段行程 {abs(value):g}mm 超过软上限 {limit:g}mm")


def check_tap(depth: float, dwell: float = 0.0) -> None:
    check_stroke(depth, axis="Z")
    if depth <= 0:
        raise ValueError("点击深度必须大于 0")
    if not math.isfinite(dwell) or not 0 <= dwell <= TAP_DWELL_MAX_S:
        raise ValueError(f"点击停留必须为 0-{TAP_DWELL_MAX_S:g} 秒的有限数值")


def check_double_tap_interval(interval_s: float) -> None:
    if not DOUBLE_TAP_INTERVAL_MIN_MS / 1000 <= interval_s <= DOUBLE_TAP_INTERVAL_MAX_MS / 1000:
        raise ValueError(
            f"双击抬起停顿必须为 {DOUBLE_TAP_INTERVAL_MIN_MS:g}-"
            f"{DOUBLE_TAP_INTERVAL_MAX_MS:g} 毫秒的有限数值")


def check_double_tap_feed(feed: float) -> None:
    """Only TAP2 may use the user-requested F3000 envelope."""
    if not math.isfinite(feed) or not Z_FEED_MIN <= feed <= DOUBLE_TAP_FEED_MAX:
        raise ValueError(f"双击进给必须为 F{Z_FEED_MIN:g}~F{DOUBLE_TAP_FEED_MAX:g}")


def check_swipe(dx: float, dy: float, depth: float, feed: float, tap_feed: float) -> None:
    check_stroke(dx, axis="X")
    check_stroke(dy, axis="Y")
    check_tap(depth)
    check_feed(tap_feed, axis="Z")
    check_feed(feed, axis="X")
    if not 0.05 < math.hypot(dx, dy) <= SWIPE_TOTAL_MAX_MM:
        raise ValueError(f"滑动总距离必须大于 0.05 且不超过 {SWIPE_TOTAL_MAX_MM:g}mm")
    if feed > SWIPE_FEED_MAX:
        raise ValueError(f"滑动速度不得超过 F{SWIPE_FEED_MAX:g}")


def split_segments(value: float, limit: float) -> list[float]:
    """把位移拆成 ≤limit 的等分段（daemon 单段软限位）。"""
    if not math.isfinite(value) or not math.isfinite(limit) or limit <= 0:
        raise ValueError("位移与分段上限必须为有限数值，且上限大于 0")
    if abs(value) <= limit:
        return [value]
    count = int(abs(value) / limit) + (1 if abs(value) % limit > 1e-9 else 0)
    return [value / count] * count


# ---------- PATH1 连续路径宏（path.v1，规范第 6.5/22.9 节） ----------

def strict_object_pairs(pairs):
    """json.loads 的 object_pairs_hook：拒绝重复键（daemon 宏入口共用）。"""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象存在重复键：{key}")
        result[key] = value
    return result


def reject_json_constant(name: str):
    """json.loads 的 parse_constant：拒绝 NaN/Infinity/-Infinity。"""
    raise ValueError(f"JSON 不允许常量：{name}")


def _path1_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字（禁止字符串数字或布尔值）")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数值")
    return number


def _assert_no_control_strings(value, path: str = "PATH1") -> None:
    """递归检查 JSON 里任何字符串值/键不含换行/回车（指令注入防护）。"""
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise ValueError(f"{path} 含换行/回车，疑似指令注入")
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_no_control_strings(key, f"{path} 键")
            _assert_no_control_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_control_strings(item, f"{path}[{index}]")


def validate_path1_payload(payload: dict) -> dict:
    """PATH1 宏载荷预验证：全部通过才返回规范化副本，失败抛 ValueError。

    规则（规范第 6.5/22.9 节）：只允许 version/depth/z_feed/segments 顶层键；
    每段只允许一种 op；恰好一个 down（首项）与一个 up（末项）；depth/z_feed
    复用 check_tap/check_feed；每 move 复用 check_stroke/check_feed 且不超过
    SWIPE_FEED_MAX；总距离 ≤ SWIPE_TOTAL_MAX_MM；hold 与 move 计划时间总和
    ≤ PATH1_CONTACT_TIME_MAX_MS；无字符串数字、无换行注入。
    """
    if not isinstance(payload, dict):
        raise ValueError("PATH1 载荷必须是 JSON 对象")
    _assert_no_control_strings(payload)
    allowed = {"version", "depth", "z_feed", "segments"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"PATH1 未知顶层键：{sorted(unknown)}")
    missing = allowed - set(payload)
    if missing:
        raise ValueError(f"PATH1 缺少顶层键：{sorted(missing)}")
    if (isinstance(payload["version"], bool) or not isinstance(payload["version"], int)
            or payload["version"] != PATH1_VERSION):
        raise ValueError(f"PATH1 version 必须为 {PATH1_VERSION}")
    depth = _path1_number(payload["depth"], "PATH1 depth")
    z_feed = _path1_number(payload["z_feed"], "PATH1 z_feed")
    check_tap(depth)
    check_feed(z_feed, axis="Z")

    segments = payload["segments"]
    if not isinstance(segments, list):
        raise ValueError("PATH1 segments 必须是数组")
    if not 1 <= len(segments) <= PATH1_MAX_SEGMENTS:
        raise ValueError(f"PATH1 segments 数量必须在 1-{PATH1_MAX_SEGMENTS} 之间")

    normalized: list[dict] = []
    total_distance = 0.0
    total_ms = 0.0
    down_count = up_count = 0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"PATH1 第 {index} 段必须是对象")
        _assert_no_control_strings(segment, f"PATH1 第 {index} 段")
        op = segment.get("op")
        if op == "down":
            if set(segment) != {"op"}:
                raise ValueError(f"PATH1 第 {index} 段 down 只允许 op 字段")
            if index != 0:
                raise ValueError("PATH1 down 必须是首段")
            down_count += 1
            normalized.append({"op": "down"})
        elif op == "move":
            if set(segment) != {"op", "dx", "dy", "feed"}:
                raise ValueError(f"PATH1 第 {index} 段 move 只允许 op/dx/dy/feed 字段")
            dx = _path1_number(segment["dx"], f"PATH1 第 {index} 段 dx")
            dy = _path1_number(segment["dy"], f"PATH1 第 {index} 段 dy")
            feed = _path1_number(segment["feed"], f"PATH1 第 {index} 段 feed")
            check_stroke(dx, axis="X")
            check_stroke(dy, axis="Y")
            check_feed(feed, axis="X")
            if feed > SWIPE_FEED_MAX:
                raise ValueError(f"PATH1 接触移动速度不得超过 F{SWIPE_FEED_MAX:g}")
            distance = math.hypot(dx, dy)
            total_distance += distance
            total_ms += distance / feed * 60000.0
            normalized.append({"op": "move", "dx": dx, "dy": dy, "feed": feed})
        elif op == "hold":
            if set(segment) != {"op", "ms"}:
                raise ValueError(f"PATH1 第 {index} 段 hold 只允许 op/ms 字段")
            ms = segment["ms"]
            if isinstance(ms, bool) or not isinstance(ms, int):
                raise ValueError(f"PATH1 第 {index} 段 hold ms 必须是整数毫秒")
            if not 0 <= ms <= PATH1_HOLD_MS_MAX:
                raise ValueError(
                    f"PATH1 hold 必须为 0-{PATH1_HOLD_MS_MAX:g} 毫秒整数")
            total_ms += ms
            normalized.append({"op": "hold", "ms": ms})
        elif op == "up":
            if set(segment) != {"op"}:
                raise ValueError(f"PATH1 第 {index} 段 up 只允许 op 字段")
            if index != len(segments) - 1:
                raise ValueError("PATH1 up 必须是末段")
            up_count += 1
            normalized.append({"op": "up"})
        else:
            raise ValueError(f"PATH1 未知 op：{op!r}")
    if down_count != 1 or up_count != 1:
        raise ValueError("PATH1 必须恰好一个 down 与一个 up")
    if total_distance > SWIPE_TOTAL_MAX_MM:
        raise ValueError(
            f"PATH1 总移动距离 {total_distance:.1f}mm 超过 {SWIPE_TOTAL_MAX_MM:g}mm 上限")
    if total_ms > PATH1_CONTACT_TIME_MAX_MS:
        raise ValueError(
            f"PATH1 计划接触时间 {total_ms:.0f}ms 超过 "
            f"{PATH1_CONTACT_TIME_MAX_MS:g}ms 上限")
    return {"version": PATH1_VERSION, "depth": depth, "z_feed": z_feed,
            "segments": normalized}
