"""gesture-program.v1 规划器与坐标桥（规范第 4、6、22.9 节）。

纯函数：验证路径、坐标变换、编译动作计划、计划摘要。
不连接硬件、不写档案、不读取全局当前设备；运行时负责加载冻结快照后传入。
点反解复用 screen_gestures.resolve_point() 与当前校准，不复制第二套 H/XY 算法。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from drivers import calibration_bundle as cb
from drivers import screen_gestures as sg
from drivers.daemon_protocol import (
    PATH1_HOLD_MS_MAX, SWIPE_DURATION_MAX_MS, SWIPE_DURATION_MIN_MS,
    SWIPE_FEED_MAX, SWIPE_TOTAL_MAX_MM, XY_FEED_MIN, check_feed, check_swipe,
    check_tap,
)
from drivers.gesture_verdict import evaluate_gesture_program

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "api" / "contracts"
DEFAULT_LIMITS: dict[str, Any] = {
    "path_feature_available": False,
    "contact_feed_max": SWIPE_FEED_MAX,
    "feed_min": XY_FEED_MIN,
    "min_segment_ms": 20,
    "max_mech_segments": 256,
    "bisection_depth": 8,
    "chord_tolerance_px": 2.0,
    "sample_step_mm": 1.0,
    "max_total_contact_mm": SWIPE_TOTAL_MAX_MM,
    "stroke_contact_max_ms": 5000,
    "program_contact_max_ms": 8000,
}

COORDINATE_FRAME_FILENAME = "gesture_coordinate_frame.json"


class GestureProgramError(Exception):
    """带 reason_code 的规划拒绝；代码见规范第 14.1 节。"""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        self.message = message
        super().__init__(f"{reason_code}: {message}")


# ---------- 规范化 JSON 与哈希（第 22.1 节） ----------

def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def program_hash(program: dict) -> str:
    return canonical_hash(program)


def coordinate_frame_hash(frame: dict) -> str:
    payload = {key: value for key, value in frame.items()
               if key not in {"coordinate_frame_hash", "collected_at"}}
    return canonical_hash(payload)


# ---------- 程序语义校验 ----------

def _assert_finite_numbers(value: Any, path: str = "program") -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise GestureProgramError("invalid_program", f"{path} 含非有限数字")
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_finite_numbers(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_finite_numbers(item, f"{path}[{index}]")


def _program_schema() -> dict:
    return json.loads((CONTRACT_DIR / "gesture-program.v1.schema.json")
                      .read_text(encoding="utf-8"))


def validate_program(program: Any, limits: dict | None = None) -> dict:
    """schema + 序列语义（首点 0、严格递增、末段 gap 0、接触预算）。

    limits 覆盖接触预算上限（默认 DEFAULT_LIMITS）；调用方传更严限值时
    不再被硬编码默认值忽略。
    """
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    _assert_finite_numbers(program)
    try:
        jsonschema.Draft202012Validator(_program_schema()).validate(program)
    except jsonschema.ValidationError as exc:
        raise GestureProgramError(
            "invalid_program", f"手势程序不符合 gesture-program.v1：{exc.message}") from exc
    strokes = program["strokes"]
    for index, stroke in enumerate(strokes):
        points = stroke["points"]
        if points[0]["t_ms"] != 0:
            raise GestureProgramError(
                "invalid_program", f"第 {index + 1} 段首点 t_ms 必须为 0")
        for prev, cur in zip(points, points[1:]):
            if cur["t_ms"] <= prev["t_ms"]:
                raise GestureProgramError(
                    "invalid_program", f"第 {index + 1} 段 t_ms 必须严格递增")
        contact = points[-1]["t_ms"] + stroke["hold_before_ms"] + stroke["hold_after_ms"]
        if contact > limits["stroke_contact_max_ms"]:
            raise GestureProgramError(
                "invalid_program",
                f"第 {index + 1} 段接触计划 {contact}ms 超过 "
                f"{DEFAULT_LIMITS['stroke_contact_max_ms']}ms")
    if strokes[-1]["gap_after_ms"] != 0:
        raise GestureProgramError(
            "invalid_program", "最后一段 gap_after_ms 必须为 0")
    total = sum(stroke["points"][-1]["t_ms"] + stroke["hold_before_ms"]
                + stroke["hold_after_ms"] + stroke["gap_after_ms"]
                for stroke in strokes)
    if total > limits["program_contact_max_ms"]:
        raise GestureProgramError(
            "invalid_program",
            f"整程序计划 {total}ms 超过 {DEFAULT_LIMITS['program_contact_max_ms']}ms")
    return program


# ---------- 坐标桥（第 4.2/4.3/22.4 节） ----------

def build_coordinate_frame(*, binding: dict, calibration_run_id: str,
                           root_width_px: int, root_height_px: int,
                           display_width_px: int, display_height_px: int,
                           root_origin_display_px: list[int], density: float,
                           source_client_instance_id: str) -> dict:
    frame = {
        "schema_version": 1,
        "binding": binding,
        "calibration_run_id": calibration_run_id,
        "root_width_px": root_width_px,
        "root_height_px": root_height_px,
        "display_width_px": display_width_px,
        "display_height_px": display_height_px,
        "root_origin_display_px": list(root_origin_display_px),
        "rotation": 0,
        "density": float(density),
        "coordinate_space_version": 2,
        "source_client_instance_id": source_client_instance_id,
    }
    frame["coordinate_frame_hash"] = coordinate_frame_hash(frame)
    return frame


def build_calibration_coordinate_frame(bundle: dict) -> dict:
    """Derive an internal screen frame; never infer Android display or insets."""
    binding = sg.binding_snapshot(bundle)
    screen = bundle.get("screen") or {}
    width, height = screen.get("screenWidth"), screen.get("screenHeight")
    if (type(width) is not int or type(height) is not int
            or width <= 0 or height <= 0):
        raise GestureProgramError("coordinate_frame_invalid",
                                  "当前校准屏幕尺寸无效")
    frame = {
        "schema_version": 1,
        "frame_kind": "calibration_screen",
        "coordinate_space_version": 3,
        "binding": binding,
        "root_width_px": width,
        "root_height_px": height,
        "source": "calibration_bundle",
    }
    frame["coordinate_frame_hash"] = coordinate_frame_hash(frame)
    return frame


def is_internal_coordinate_frame(frame: Any) -> bool:
    return isinstance(frame, dict) and frame.get("frame_kind") == "calibration_screen"


def frame_for_internal_gesture(bundle: dict, frame: dict | None) -> dict:
    """Use a real legacy frame when valid, otherwise derive only an internal frame.

    A stale legacy binding is never rewritten or reused as an Android edge frame.
    Corrupted hashes and unsupported formats remain errors.
    """
    if frame is None:
        return build_calibration_coordinate_frame(bundle)
    try:
        return validate_coordinate_frame(frame, bundle)
    except GestureProgramError as exc:
        if exc.reason_code != "gesture_binding_changed":
            raise
        if (not isinstance(frame, dict)
                or frame.get("coordinate_frame_hash") != coordinate_frame_hash(frame)):
            raise
        return build_calibration_coordinate_frame(bundle)


def validate_coordinate_frame(frame: Any, bundle: dict) -> dict:
    if not isinstance(frame, dict):
        raise GestureProgramError("coordinate_frame_missing", "缺少本次校准的坐标桥旁档")
    if is_internal_coordinate_frame(frame):
        expected = build_calibration_coordinate_frame(bundle)
        if frame.get("schema_version") != 1 or frame.get("coordinate_space_version") != 3:
            raise GestureProgramError("coordinate_frame_unsupported",
                                      "内部屏幕坐标版本不受支持")
        if canonical_json(frame) != canonical_json(expected):
            raise GestureProgramError("gesture_binding_changed",
                                      "内部屏幕坐标与当前校准不一致")
        return frame
    if frame.get("schema_version") != 1 or frame.get("coordinate_space_version") != 2:
        raise GestureProgramError("coordinate_frame_unsupported",
                                  "坐标桥版本不受支持")
    if frame.get("rotation") != 0:
        raise GestureProgramError("coordinate_frame_unsupported",
                                  "当前仅支持竖屏旋转 0 的坐标桥")
    if frame.get("coordinate_frame_hash") != coordinate_frame_hash(frame):
        raise GestureProgramError(
            "gesture_binding_changed", "坐标桥哈希校验失败，旁档内容被改动")
    if (frame.get("root_width_px"), frame.get("root_height_px")) != \
            (bundle["screen"].get("screenWidth"), bundle["screen"].get("screenHeight")):
        raise GestureProgramError(
            "gesture_binding_changed",
            "坐标桥 root 尺寸与当前校准屏幕不一致，校准或设备绑定已变化")
    current_binding = sg.binding_snapshot(bundle)
    if canonical_json(frame.get("binding")) != canonical_json(current_binding):
        raise GestureProgramError(
            "gesture_binding_changed", "坐标桥绑定与当前校准绑定不一致")
    return frame


def display_to_calibration_px(display_x: float, display_y: float,
                              frame: dict) -> tuple[float, float]:
    origin_x, origin_y = frame["root_origin_display_px"]
    return display_x - origin_x, display_y - origin_y


def calibration_px_to_normalized(cx: float, cy: float, frame: dict) -> tuple[float, float]:
    return cx / frame["root_width_px"], cy / frame["root_height_px"]


def normalized_to_calibration_px(x: float, y: float, frame: dict) -> tuple[float, float]:
    return x * frame["root_width_px"], y * frame["root_height_px"]


def coordinate_frame_path(device_id: str, data_root: Path | None = None) -> Path:
    base = Path(data_root) if data_root else cb.default_data_root()
    return base / "devices" / device_id / COORDINATE_FRAME_FILENAME


def load_coordinate_frame(device_id: str, data_root: Path | None = None) -> dict | None:
    path = coordinate_frame_path(device_id, data_root)
    if not path.exists():
        return None
    try:
        frame = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GestureProgramError(
            "coordinate_frame_invalid", f"坐标桥旁档损坏：{exc}") from exc
    if not isinstance(frame, dict):
        raise GestureProgramError("coordinate_frame_invalid", "坐标桥旁档不是对象")
    return frame


def save_coordinate_frame(device_id: str, frame: dict,
                          data_root: Path | None = None) -> None:
    """原子写旁档：临时文件 + fsync + replace；损坏报错不覆盖。"""
    validate_coordinate_frame_shape(frame)
    path = coordinate_frame_path(device_id, data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(frame, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def validate_coordinate_frame_shape(frame: dict) -> None:
    required = {"schema_version", "binding", "calibration_run_id", "root_width_px",
                "root_height_px", "display_width_px", "display_height_px",
                "root_origin_display_px", "rotation", "density",
                "coordinate_space_version", "source_client_instance_id",
                "coordinate_frame_hash"}
    missing = required - set(frame)
    if missing:
        raise GestureProgramError(
            "coordinate_frame_invalid", f"坐标桥缺少字段：{sorted(missing)}")


# ---------- 规划与编译 ----------

def _resolve_normalized(x: float, y: float, bundle: dict, frame: dict) -> dict:
    cx, cy = normalized_to_calibration_px(x, y, frame)
    width = bundle["screen"]["screenWidth"]
    height = bundle["screen"]["screenHeight"]
    try:
        resolved = sg.resolve_point(
            {"source": "screen_normalized", "x": cx / width, "y": cy / height},
            bundle)
    except (ValueError, TypeError, KeyError) as exc:
        raise GestureProgramError("invalid_program", str(exc)) from exc
    return {"screenX": cx, "screenY": cy,
            "robotX": resolved["robotX"], "robotY": resolved["robotY"]}


def _screen_at(model: dict, rx: float, ry: float) -> tuple[float, float]:
    return cb.screen_from_robot(model, rx, ry)


def _chord_error(model: dict, a_robot: tuple[float, float], b_robot: tuple[float, float],
                 a_screen: tuple[float, float], b_screen: tuple[float, float],
                 fraction: float) -> float:
    rx = a_robot[0] + (b_robot[0] - a_robot[0]) * fraction
    ry = a_robot[1] + (b_robot[1] - a_robot[1]) * fraction
    sx, sy = _screen_at(model, rx, ry)
    # 点到直线段 AB 的距离
    ax, ay = a_screen
    bx, by = b_screen
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(sx - ax, sy - ay)
    t = ((sx - ax) * dx + (sy - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(sx - (ax + t * dx), sy - (ay + t * dy))


def _segment_needs_subdivision(model: dict, a_robot, b_robot, a_screen, b_screen,
                               tolerance: float) -> bool:
    for fraction in (0.25, 0.5, 0.75):
        if _chord_error(model, a_robot, b_robot, a_screen, b_screen,
                        fraction) > tolerance:
            return True
    return False


def _walk_segment_bounds(model: dict, a_robot, b_robot, bundle: dict,
                         step_mm: float) -> None:
    """沿机械段每 step_mm 取样映回屏幕，检查边界（不扩大既有容差）。"""
    distance = math.hypot(b_robot[0] - a_robot[0], b_robot[1] - a_robot[1])
    width = bundle["screen"]["screenWidth"]
    height = bundle["screen"]["screenHeight"]
    steps = max(1, int(math.ceil(distance / step_mm)))
    for index in range(steps + 1):
        t = index / steps
        sx, sy = _screen_at(model, a_robot[0] + (b_robot[0] - a_robot[0]) * t,
                            a_robot[1] + (b_robot[1] - a_robot[1]) * t)
        if not (0 <= sx <= width and 0 <= sy <= height):
            raise GestureProgramError(
                "invalid_program",
                f"机械段采样点映回屏幕越界：({sx:.1f},{sy:.1f})")


def _plan_stroke_swipe(stroke: dict, resolved: list[dict], bundle: dict,
                       profile: dict, limits: dict) -> dict:
    start, end = resolved[0], resolved[1]
    duration = stroke["points"][1]["t_ms"]
    dx = end["robotX"] - start["robotX"]
    dy = end["robotY"] - start["robotY"]
    distance = math.hypot(dx, dy)
    if distance <= 0.05:
        raise GestureProgramError(
            "invalid_program", "两点路径距离过小，应使用停留而不是滑动")
    feed = distance / duration * 60000 if duration > 0 else math.inf
    if (feed > limits["contact_feed_max"] or feed < limits["feed_min"]
            or not SWIPE_DURATION_MIN_MS <= duration <= SWIPE_DURATION_MAX_MS):
        min_ms = math.ceil(distance / limits["contact_feed_max"] * 60000)
        max_ms = math.floor(min(SWIPE_DURATION_MAX_MS,
                                distance / limits["feed_min"] * 60000))
        raise GestureProgramError(
            "path_timing_unachievable",
            f"请求时长 {duration}ms 不可满足：进给需在 "
            f"F{limits['feed_min']:g}-F{limits['contact_feed_max']:g}，"
            f"可行时长约 {min_ms}-{max_ms}ms")
    try:
        check_swipe(dx, dy, profile["tapDepth"], feed, profile["tapFeed"])
    except ValueError as exc:
        raise GestureProgramError("invalid_program", str(exc)) from exc
    return {
        "index": 0,
        "execution": "swipe",
        "duration_ms": int(duration),
        "distance_mm": round(distance, 3),
        "feed": round(feed, 3),
        "screen_points": [{"x": p["screenX"], "y": p["screenY"]} for p in resolved],
        "mech_points": [{"robotX": p["robotX"], "robotY": p["robotY"]} for p in resolved],
        "segments": None,
    }


def _subdivide(model, a_robot, b_robot, a_screen, b_screen, t_start, t_end,
               tolerance, depth, max_depth, out):
    """屏幕直线→非线性机械路径的递归细分；新增点时间按屏幕线段参数插值。"""
    if depth >= max_depth or not _segment_needs_subdivision(
            model, a_robot, b_robot, a_screen, b_screen, tolerance):
        out.append((b_robot, b_screen, t_end))
        return
    ax, ay = a_screen
    bx, by = b_screen
    seg_len = math.hypot(bx - ax, by - ay)
    mid_robot = ((a_robot[0] + b_robot[0]) / 2, (a_robot[1] + b_robot[1]) / 2)
    mid_screen = _screen_at(model, *mid_robot)
    # 机械中点映回屏幕后落在屏幕线段上的参数 u（截取到 [0,1]）
    proj = 0.5 if seg_len <= 1e-12 else max(
        0.0, min(1.0, ((mid_screen[0] - ax) * (bx - ax)
                       + (mid_screen[1] - ay) * (by - ay)) / (seg_len * seg_len)))
    t_mid = t_start + (t_end - t_start) * proj
    _subdivide(model, a_robot, mid_robot, a_screen, mid_screen, t_start, t_mid,
               tolerance, depth + 1, max_depth, out)
    _subdivide(model, mid_robot, b_robot, mid_screen, b_screen, t_mid, t_end,
               tolerance, depth + 1, max_depth, out)


def _plan_stroke_path1(stroke: dict, resolved: list[dict], bundle: dict,
                       profile: dict, limits: dict) -> dict:
    model = bundle["xy"]["robotToScreen"]
    nodes: list[tuple[tuple[float, float], tuple[float, float], int]] = []
    first = resolved[0]
    nodes.append(((first["robotX"], first["robotY"]),
                  (first["screenX"], first["screenY"]),
                  stroke["points"][0]["t_ms"]))
    for index in range(1, len(resolved)):
        prev = resolved[index - 1]
        cur = resolved[index]
        out: list = []
        _subdivide(model, (prev["robotX"], prev["robotY"]),
                   (cur["robotX"], cur["robotY"]),
                   (prev["screenX"], prev["screenY"]),
                   (cur["screenX"], cur["screenY"]),
                   stroke["points"][index - 1]["t_ms"],
                   stroke["points"][index]["t_ms"],
                   limits["chord_tolerance_px"], 0, limits["bisection_depth"], out)
        for robot, screen, t_ms in out:
            nodes.append((robot, screen, t_ms))
    # 量化到 0.001mm，dx/dy 由量化后的绝对点相减（第 22.9 节）
    quantized = [((round(rx, 3), round(ry, 3)), screen, t) for (rx, ry), screen, t in nodes]
    segments: list[dict] = [{"op": "down"}]
    if stroke["hold_before_ms"]:
        segments.append({"op": "hold", "ms": int(stroke["hold_before_ms"])})
    total_distance = 0.0
    for (prev_robot, _prev_screen, prev_t), (cur_robot, _cur_screen, cur_t) in \
            zip(quantized, quantized[1:]):
        dx = round(cur_robot[0] - prev_robot[0], 3)
        dy = round(cur_robot[1] - prev_robot[1], 3)
        duration = cur_t - prev_t
        if dx == 0 and dy == 0:
            if duration > 0:
                if duration > PATH1_HOLD_MS_MAX:
                    # 生成型 hold（量化重合点）与声明型同受控制器 G4 上限，
                    # 否则规划通过、执行期才被 driver_gesture_invalid 打断
                    raise GestureProgramError(
                        "path_timing_unachievable",
                        f"重合点静止段 {duration}ms 超过 hold 上限 "
                        f"{PATH1_HOLD_MS_MAX:g}ms，请调整点间隔")
                segments.append({"op": "hold", "ms": int(duration)})
            continue
        if duration < limits["min_segment_ms"]:
            raise GestureProgramError(
                "path_timing_unachievable",
                f"细分段时长 {duration}ms 低于最小 {limits['min_segment_ms']}ms，"
                "不填 0ms，请调整点间隔")
        distance = math.hypot(dx, dy)
        feed = distance / duration * 60000
        if feed > limits["contact_feed_max"] or feed < limits["feed_min"]:
            raise GestureProgramError(
                "path_timing_unachievable",
                f"细分段进给 F{feed:.0f} 超出 F{limits['feed_min']:g}-"
                f"F{limits['contact_feed_max']:g}，请放慢点间隔或缩短路径")
        try:
            check_feed(feed, axis="X")
        except ValueError as exc:
            raise GestureProgramError("invalid_program", str(exc)) from exc
        total_distance += distance
        segments.append({"op": "move", "dx": dx, "dy": dy, "feed": round(feed, 3)})
    if stroke["hold_after_ms"]:
        segments.append({"op": "hold", "ms": int(stroke["hold_after_ms"])})
    segments.append({"op": "up"})
    # daemon 侧 PATH1_MAX_SEGMENTS 是含首 down/末 up 的总数口径，
    # 规划期必须按同口径拒绝（否则执行期才被 driver_gesture_invalid 打断）
    if len(segments) > limits["max_mech_segments"]:
        raise GestureProgramError(
            "path_feature_unavailable",
            f"编译后 PATH1 段数 {len(segments)}（含首 down/末 up/hold）"
            f"超过上限 {limits['max_mech_segments']}，不自动降精度")
    for (prev_robot, _, _), (cur_robot, _, _) in zip(quantized, quantized[1:]):
        _walk_segment_bounds(model, prev_robot, cur_robot, bundle,
                             limits["sample_step_mm"])
    duration_ms = stroke["points"][-1]["t_ms"] + stroke["hold_before_ms"] \
        + stroke["hold_after_ms"]
    return {
        "index": 0,
        "execution": "path1",
        "duration_ms": int(duration_ms),
        "distance_mm": round(total_distance, 3),
        "feed": None,
        "screen_points": [{"x": p["screenX"], "y": p["screenY"]} for p in resolved],
        "mech_points": [{"robotX": rx, "robotY": ry}
                        for (rx, ry), _s, _t in quantized],
        "segments": segments,
    }


def plan_program(program: Any, bundle: dict, coordinate_frame: dict | None,
                 limits: dict | None = None) -> dict:
    """编译动作计划；不连接硬件、机械点不向 WX 业务任务暴露。"""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    validate_program(program)
    if coordinate_frame is None:
        raise GestureProgramError("coordinate_frame_missing",
                                  "缺少本次校准的坐标桥")
    validate_coordinate_frame(coordinate_frame, bundle)
    try:
        binding = sg.binding_snapshot(bundle)
        profile = sg.calibrated_profile(bundle)
        check_tap(profile["tapDepth"])
        check_feed(profile["tapFeed"], axis="Z")
    except ValueError as exc:
        # check_*/binding_snapshot 抛裸 ValueError 会逃出 GestureProgramError
        # 契约（插件按 unknown 归类）；统一按校准/参数无效归类
        raise GestureProgramError(
            "coordinate_frame_invalid" if "校准" in str(exc) else "invalid_program",
            str(exc)) from exc

    strokes = program["strokes"]
    resolved_strokes = [
        [_resolve_normalized(point["x"], point["y"], bundle, coordinate_frame)
         for point in stroke["points"]]
        for stroke in strokes]

    swipe_eligible = (
        len(strokes) == 1 and len(strokes[0]["points"]) == 2
        and strokes[0]["hold_before_ms"] == 0 and strokes[0]["hold_after_ms"] == 0)
    if swipe_eligible:
        model = bundle["xy"]["robotToScreen"]
        a, b = resolved_strokes[0]
        needs_subdivision = _segment_needs_subdivision(
            model, (a["robotX"], a["robotY"]), (b["robotX"], b["robotY"]),
            (a["screenX"], a["screenY"]), (b["screenX"], b["screenY"]),
            limits["chord_tolerance_px"])
        swipe_eligible = not needs_subdivision

    stroke_plans: list[dict] = []
    if swipe_eligible:
        stroke_plans.append(_plan_stroke_swipe(strokes[0], resolved_strokes[0],
                                               bundle, profile, limits))
        execution_mode = "swipe"
    else:
        if not limits["path_feature_available"]:
            raise GestureProgramError(
                "path_feature_unavailable",
                "控制器未支持连续多段路径（path.v1），禁止拆成多次 SWIPE 兜底")
        for stroke, resolved in zip(strokes, resolved_strokes):
            stroke_plans.append(_plan_stroke_path1(stroke, resolved, bundle,
                                                   profile, limits))
        execution_mode = "path1"

    total_distance = sum(stroke["distance_mm"] for stroke in stroke_plans)
    if total_distance > limits["max_total_contact_mm"]:
        raise GestureProgramError(
            "invalid_program",
            f"总接触移动距离 {total_distance:.1f}mm 超过现有上限 "
            f"{limits['max_total_contact_mm']:g}mm，不新放宽")
    estimated = sum(stroke["duration_ms"] for stroke in stroke_plans) + sum(
        stroke["gap_after_ms"] for stroke in strokes[:-1])
    return {
        "plan_id": f"plan-{uuid.uuid4().hex[:12]}",
        "contract_version": "gesture-program.v1",
        "program_hash": program_hash(program),
        "program": json.loads(canonical_json(program)),
        "binding": binding,
        "coordinate_frame_hash": coordinate_frame["coordinate_frame_hash"],
        "calibration_fingerprint": binding["calibration_fingerprint"],
        "profile": profile,
        "execution_mode": execution_mode,
        "strokes": stroke_plans,
        "estimated_duration_ms": int(estimated),
        "total_contact_distance_mm": round(total_distance, 3),
        "calculated": True,
        "rejection_reasons": [],
    }


def public_plan_summary(plan: dict) -> dict:
    """对外计划摘要：不含机械坐标与执行段（不向 WX 业务任务暴露）。"""
    strokes = [
        {key: value for key, value in stroke.items()
         if key not in {"mech_points", "segments"}}
        for stroke in plan["strokes"]]
    return {
        "plan_id": plan["plan_id"],
        "contract_version": plan["contract_version"],
        "program_hash": plan["program_hash"],
        "binding": plan["binding"],
        "coordinate_frame_hash": plan["coordinate_frame_hash"],
        "execution_mode": plan["execution_mode"],
        "estimated_duration_ms": plan["estimated_duration_ms"],
        "total_contact_distance_mm": plan["total_contact_distance_mm"],
        "strokes": strokes,
    }


def evaluate_program(plan: dict, controller: Any, phone_trace: Any,
                     visual: Any, policy: dict | None) -> dict:
    """计划 + 控制器/触点/视觉证据 → 三层结果（gesture_verdict.v1）。"""
    return evaluate_gesture_program(plan, controller, phone_trace, visual, policy)
