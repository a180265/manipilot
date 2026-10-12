"""摄像头并发保护（实施文档 5.7.1）：每摄像头唯一物理采集入口。

- 同一 camera_id 的物理取帧串行（底层 VideoCapture 不保证支持并发 read）；
- 监听器、质量巡检、实时查看都可读"最近一帧"缓存，不各自驱动底层采集；
- 实时查看只能订阅 broker，不得再打开第二个摄像头句柄；
- 摄像头断开时，所有订阅者收到同一个 camera_unavailable 状态，不各自重连争抢。
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Callable

from runtime.status import CAMERA_QUALITY, display

# FIX-08（滑动附件 §3-4）：链帧身份。R2-08（camera-binding 附件 §5/§6）收紧：
# 只有携带可证明采样时序的真实源帧才可用于动作链——
# source_generation_id / source_capture_seq / source_captured_monotonic_ns /
# source_timing_provenance=camera_pts_host_clock.v1 四者齐备（同帧 PTS 经受控
# 时钟换算产生）。缺失任一项或时钟证明不符 → 明确不可用于动作（光流前帧、
# 相邻间隔、完成栅栏尾帧判定一律拒绝），不回落读取时间、不混入旧协议分支。
SOURCE_TIMING_PROVENANCE = "camera_pts_host_clock.v1"
# 兼容分支诊断标记：broker 在接纳现场补的服务侧身份（物理读完成时刻），
# 只作诊断/旧链审计，不构成动作可用的采样时序证明。
BROKER_TIMING_PROVENANCE = "broker_acceptance.v1"


QUALITY_REASON_NOTES = {
    "screen_roi_not_found": "画面中无法定位手机屏幕",
    "camera_capture_failed": "摄像头取流失败，当前画面不可用",
    "camera_quality_check_failed": "画面质量检查异常，原因尚未确认",
    "camera_frame_decode_failed": "画面解码失败",
    "camera_calibration_binding_mismatch": "画面与校准绑定不符",
    "camera_calibration_screen_missing": "校准缺少屏幕区域",
    "camera_quality_geometry_invalid": "校准屏幕区域无效",
    "camera_detail_insufficient": "画面细节不足，清晰度未达到要求",
}
_QUALITY_EXCEPTION_CODES = {
    QUALITY_REASON_NOTES[code]: code for code in (
        "camera_frame_decode_failed", "camera_calibration_binding_mismatch",
        "camera_calibration_screen_missing", "camera_quality_geometry_invalid",
    )
}


def quality_failure_diagnostics(exc: Exception, *, capture_failed: bool = False) -> dict:
    """Only fixed diagnostic codes and Chinese notes leave the quality check."""
    code = "camera_capture_failed" if capture_failed else getattr(exc, "reason_code", None)
    if not isinstance(code, str) or code not in QUALITY_REASON_NOTES:
        code = _QUALITY_EXCEPTION_CODES.get(str(exc), "camera_quality_check_failed")
    return {"reason_code": code, "note": QUALITY_REASON_NOTES[code], "metrics": {}}


def quality_diagnostics(result: dict) -> dict:
    """Additive display fields only; never used to classify a frame or permit motion."""
    code = result.get("reason_code")
    if result.get("quality") == "clear":
        code = None
    elif not isinstance(code, str) or code not in QUALITY_REASON_NOTES:
        code = "camera_quality_check_failed" if result.get("quality") == "abnormal" else None
    source_metrics = result.get("metrics")
    metrics = {}
    if isinstance(source_metrics, dict):
        for key in ("brightness", "texture", "blur_variance"):
            value = source_metrics.get(key)
            if type(value) not in (int, float):
                continue
            try:
                if math.isfinite(value) and value >= 0:
                    metrics[key] = round(value, 2)
            except OverflowError:
                pass
    return {"reason_code": code, "note": QUALITY_REASON_NOTES.get(code), "metrics": metrics}


class CameraQualityCheckError(RuntimeError):
    """Preserve the broker's exception path while attaching safe diagnostics."""

    def __init__(self, diagnostics: dict):
        self.diagnostics = quality_diagnostics(diagnostics)
        self.reason_code = self.diagnostics["reason_code"]
        super().__init__(self.diagnostics["note"])


def frame_chain_identity(frame: dict) -> dict | None:
    """提取一帧的链身份：{generation, seq, captured_monotonic_ns, ...}。

    R2-08：只接受真实源采样身份（source_generation_id/source_capture_seq/
    source_captured_monotonic_ns + source_timing_provenance=
    camera_pts_host_clock.v1）。broker 接纳身份（broker_*）、读取时间或
    其他 provenance 一律不构成链身份 → 返回 None（该帧明确不可用于动作链）。
    uncertainty_ms 缺失时同样拒绝：间隔/栅栏判定必须含误差界限。
    """
    if not isinstance(frame, dict):
        return None
    provenance = frame.get("source_timing_provenance")
    if provenance != SOURCE_TIMING_PROVENANCE:
        return None
    generation = frame.get("source_generation_id")
    seq = frame.get("source_capture_seq")
    captured_ns = frame.get("source_captured_monotonic_ns")
    uncertainty = frame.get("source_capture_uncertainty_ms")
    if not generation or type(seq) is not int or seq < 0 \
            or type(captured_ns) is not int or captured_ns < 0 \
            or type(uncertainty) not in (int, float) \
            or not 0 <= float(uncertainty) <= 20:
        return None
    read_ms = frame.get("broker_read_ms")
    return {"generation": str(generation), "seq": int(seq),
            "captured_monotonic_ns": int(captured_ns),
            "uncertainty_ms": float(uncertainty),
            "read_ms": read_ms if type(read_ms) in (int, float) else 0.0,
            "provenance": provenance}


# ---- R2-18（camera-binding 附件 §5.1）：来源 API 的冻结 v2 输出对象 ----
# 生产者→帧桥→证据→API 的字段转换集中在此边界：输入只接受 camera_source_v2
# 归档记录（camera_frame_server 接纳时写入），缺可信字段拒绝并保留缺失事实，
# 不补默认值、不从当前绑定/当前时间补写旧帧。对象形状以
# docs/specs/camera-binding/contracts.schema.json 的 source_frame /
# source_frame_view / screen_source_response 为准（Draft07、封闭字段集）。

SOURCE_FRAME_CONTRACT_VERSION = "camera-source-frame.v2"
SCREEN_SOURCE_CONTRACT_VERSION = "screen-source-observation.v2"

import re as _re

_UUID_RE = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA_RE = _re.compile(r"^[0-9a-f]{64}$")
_CLOCK_ID_RE = _re.compile(r"^mach-absolute:[0-9a-f]{64}$")
_TIME_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$")


class SourceFrameContractError(ValueError):
    """来源证据缺少可信字段/字段不合规：拒绝输出，不补默认值。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "source_unavailable"


def _contract_field(record: dict, *keys: str, description: str):
    """按序取第一个存在的别名键；全缺 → 拒绝（改键不改值，不造值）。"""
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    raise SourceFrameContractError(f"来源证据缺少可信字段：{description}")


def _contract_int(value, description: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SourceFrameContractError(
            f"来源证据字段不合规：{description}={value!r}")
    return value


def _contract_str(value, description: str, *, pattern: _re.Pattern | None = None,
                  max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise SourceFrameContractError(
            f"来源证据字段不合规：{description}={value!r}")
    if pattern is not None and not pattern.match(value):
        raise SourceFrameContractError(
            f"来源证据字段不合规：{description}={value!r}")
    return value


def build_source_frame(record: dict) -> dict:
    """从 camera_source_v2 归档记录构建冻结 schema 的 source_frame 对象。

    诊断修订（binding_revision）、桥序号（source_frame_seq）、时钟误差
    （source_capture_uncertainty_ms）与 PTS 证明必须来自该帧接纳时的冻结
    快照；任一缺失/不合规即拒绝（缺字段=协议不支持，不是可用旧帧）。
    """
    if not isinstance(record, dict):
        raise SourceFrameContractError("来源证据记录必须是对象")
    legacy_mode = record.get("source_legacy_mode")
    if legacy_mode:
        raise SourceFrameContractError(
            "兼容分支帧不能输出 camera-source-frame.v2 来源对象")
    # 帧桥接纳记录字段名为 capture_uncertainty_ms（source_* 为 schema 形别名）
    uncertainty = _contract_field(record, "capture_uncertainty_ms",
                                  "source_capture_uncertainty_ms",
                                  description="source_capture_uncertainty_ms")
    if type(uncertainty) not in (int, float) or isinstance(uncertainty, bool) \
            or not 0 <= float(uncertainty) <= 20:
        raise SourceFrameContractError(
            f"来源证据缺少/不合规的采样时钟误差：{uncertainty!r}")
    provenance = _contract_str(record.get("timing_provenance"),
                               "timing_provenance")
    if provenance != SOURCE_TIMING_PROVENANCE:
        raise SourceFrameContractError(
            f"来源证据缺少 PTS host-clock 采样证明：{provenance!r}（不可用于来源对象）")
    return {
        "device_id": _contract_str(_contract_field(
            record, "device_id", description="device_id"), "device_id"),
        "camera_id": _contract_str(_contract_field(
            record, "camera_id", description="camera_id"), "camera_id"),
        "device_unique_id": _contract_str(_contract_field(
            record, "device_unique_id", "camera_unique_id",
            description="device_unique_id"), "device_unique_id"),
        "camera_binding_id": _contract_str(_contract_field(
            record, "camera_binding_id", description="camera_binding_id"),
            "camera_binding_id", pattern=_UUID_RE),
        "source_geometry_id": _contract_str(_contract_field(
            record, "source_geometry_id", description="source_geometry_id"),
            "source_geometry_id", pattern=_SHA_RE),
        "binding_revision": _contract_int(_contract_field(
            record, "binding_revision", description="binding_revision"),
            "binding_revision", minimum=0, maximum=9007199254740991),
        "source_frame_seq": _contract_int(_contract_field(
            record, "source_frame_seq", description="source_frame_seq"),
            "source_frame_seq", minimum=1, maximum=9007199254740991),
        "source_captured_at": _contract_str(_contract_field(
            record, "captured_at", description="captured_at"),
            "captured_at", pattern=_TIME_RE),
        "source_received_at": _contract_str(_contract_field(
            record, "received_at", description="received_at"),
            "received_at", pattern=_TIME_RE),
        "source_legacy_mode": False,
        "width": _contract_int(_contract_field(
            record, "frame_width", "width", description="frame_width"),
            "width", minimum=1, maximum=9007199254740991),
        "height": _contract_int(_contract_field(
            record, "frame_height", "height", description="frame_height"),
            "height", minimum=1, maximum=9007199254740991),
        "source_generation_id": _contract_str(_contract_field(
            record, "source_generation_id", description="source_generation_id"),
            "source_generation_id", pattern=_UUID_RE),
        "source_capture_seq": _contract_int(_contract_field(
            record, "source_capture_seq", description="source_capture_seq"),
            "source_capture_seq", minimum=0, maximum=18446744073709551615),
        "source_clock_id": _contract_str(_contract_field(
            record, "source_clock_id", description="source_clock_id"),
            "source_clock_id", pattern=_CLOCK_ID_RE),
        "source_captured_monotonic_ns": _contract_int(_contract_field(
            record, "source_captured_monotonic_ns",
            description="source_captured_monotonic_ns"),
            "source_captured_monotonic_ns", minimum=0,
            maximum=18446744073709551615),
        "source_capture_uncertainty_ms": float(uncertainty),
        "source_timing_provenance": provenance,
    }


def build_source_frame_view(record: dict) -> dict:
    """完整外壳：camera-source-frame.v2 = {contract_version, evidence_id,
    jpeg_sha256, source_frame}（原图 endpoint 另按 evidence_id 返回原字节）。"""
    evidence_id = _contract_str(_contract_field(
        record, "evidence_id", description="evidence_id"), "evidence_id")
    jpeg_sha256 = _contract_str(_contract_field(
        record, "jpeg_sha256", description="jpeg_sha256"),
        "jpeg_sha256", pattern=_SHA_RE)
    return {"contract_version": SOURCE_FRAME_CONTRACT_VERSION,
            "evidence_id": evidence_id,
            "jpeg_sha256": jpeg_sha256,
            "source_frame": build_source_frame(record)}


def build_screen_source_response(record: dict, *, observation: dict,
                                 coordinate_frame_hash) -> dict:
    """screen-source-observation.v2：外层 source 为指定原帧的完整
    source_frame_view；observation 为 screen-observation.v1 严格投影，
    由调用方（runtime.screen_observation）从同一原帧派生。§5.1：内层
    frame_sha256/frame_seq/captured_at/物理身份/尺寸必须对应外层 source。"""
    coordinate = _contract_str(coordinate_frame_hash,
                               "coordinate_frame_hash", pattern=_SHA_RE)
    if not isinstance(observation, dict) or not observation:
        raise SourceFrameContractError("观察投影缺失：不能凭空构造 observation")
    view = build_source_frame_view(record)
    source_frame = view["source_frame"]
    expected = {
        "device_id": source_frame["device_id"],
        "camera_id": source_frame["camera_id"],
        "camera_device_unique_id": source_frame["device_unique_id"],
        "binding_revision": source_frame["binding_revision"],
        "captured_at": source_frame["source_captured_at"],
        "frame_sha256": view["jpeg_sha256"],
        "frame_seq": source_frame["source_frame_seq"],
        "frame_width": source_frame["width"],
        "frame_height": source_frame["height"],
    }
    mismatched = [key for key, value in expected.items()
                  if observation.get(key) != value]
    if mismatched:
        raise SourceFrameContractError(
            "观察投影与指定原帧不是同一帧（字段不符）："
            f"{', '.join(mismatched)}")
    return {"contract_version": SCREEN_SOURCE_CONTRACT_VERSION,
            "source": view,
            "coordinate_frame_hash": coordinate,
            "observation": observation}


@dataclass
class BrokerEvent:
    camera_id: str
    status: str  # frame / camera_unavailable
    detail: str = ""
    frame: dict | None = None
    reason_code: str | None = None


class CameraFrameBroker:
    """包装一个具备 capture_sync() 的驱动，串行化物理采集并缓存最近一帧。"""

    def __init__(self, driver, *, camera_id: str, max_age_seconds: float = 1.5):
        self._driver = driver
        self.camera_id = camera_id
        self.max_age_seconds = max(0.0, float(max_age_seconds))
        self._lock = asyncio.Lock()
        # 同步证据/实时查看也必须与异步监听共用同一个物理读锁。
        self._sync_lock = threading.Lock()
        self._latest: dict | None = None
        self._subscribers: list[Callable[[BrokerEvent], None]] = []
        self.unavailable = False
        self.last_error: str = ""
        # FIX-08：server 侧链身份。generation 在采集会话（重连/恢复）启动时
        # 更换——代际变化立即中断当前光流链；seq 为本进程严格递增接纳序号。
        self.generation_id = f"broker-gen-{uuid.uuid4().hex}"
        self._broker_seq = 0
        self.last_read_ms = 0.0
        self.max_read_ms = 0.0
        # 并发观测（诊断/测试用）
        self.max_concurrent_grabs = 0
        self._active_grabs = 0
        self.grab_count = 0

    def restart_generation(self) -> str:
        """采集会话重启（断流恢复/换绑重开）时更换链代际。"""
        self.generation_id = f"broker-gen-{uuid.uuid4().hex}"
        self._broker_seq = 0
        return self.generation_id

    @property
    def latest(self) -> dict | None:
        return self._latest

    def subscribe(self, callback: Callable[[BrokerEvent], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[BrokerEvent], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _broadcast(self, event: BrokerEvent) -> None:
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:
                pass

    async def capture(self) -> dict:
        """新鲜采集一帧（唯一物理入口，串行）。失败广播 camera_unavailable 并抛出。"""
        async with self._lock:
            return await asyncio.to_thread(self.capture_sync)

    def capture_sync(self) -> dict:
        """同步取帧入口，和 ``capture`` 共用底层读锁与缓存。"""
        with self._sync_lock:
            self._active_grabs += 1
            self.max_concurrent_grabs = max(self.max_concurrent_grabs, self._active_grabs)
            read_started = time.monotonic()
            try:
                frame = self._driver.capture_sync()
            except Exception as exc:
                self.unavailable = True
                diagnostics = quality_failure_diagnostics(exc, capture_failed=True)
                self.last_error = diagnostics["note"]
                self._broadcast(BrokerEvent(camera_id=self.camera_id, status="camera_unavailable",
                                            detail=self.last_error,
                                            reason_code=diagnostics["reason_code"]))
                raise
            finally:
                self._active_grabs -= 1
            # FIX-08：物理读时长（driver 调用本身，不含排队/调用方开销）。
            # max_read_ms 是本通道已证实的读耗时上界（滚动最大值）：按需渲染
            # 的提供器在场景变化时才产生慢读，链间隔宽容据此保持稳定；采样
            # 协程的调度延迟与测试注入的读取间空档不计入。
            read_ms = (time.monotonic() - read_started) * 1000.0
            self.last_read_ms = read_ms
            self.max_read_ms = max(self.max_read_ms, read_ms)
            frame = self._normalize(frame)
            self.grab_count += 1
            if self.unavailable:
                # 采集会话重启（断流恢复）：更换链代际，旧链立即失效。
                self.restart_generation()
                self._broadcast(BrokerEvent(camera_id=self.camera_id, status="camera_recovered"))
            self.unavailable = False
            self.last_error = ""
            frame = dict(frame)
            self._broker_seq += 1
            frame.setdefault("broker_generation_id", self.generation_id)
            frame["broker_seq"] = self._broker_seq
            frame["broker_monotonic_ns"] = time.monotonic_ns()
            frame["broker_read_ms"] = round(self.max_read_ms, 3)
            frame.setdefault("source_timing_provenance", BROKER_TIMING_PROVENANCE)
            self._latest = frame
            self._broadcast(BrokerEvent(camera_id=self.camera_id, status="frame", frame=frame))
            return frame

    @staticmethod
    def _normalize(frame: dict) -> dict:
        """统一帧字段：capture_sync 产出 image，驱动异步接口产出 image_bytes。

        监听器/观测器统一读 image_bytes，避免调用方分叉。
        """
        if "image_bytes" not in frame and "image" in frame:
            return dict(frame) | {"image_bytes": frame["image"]}
        return frame

    async def latest_or_capture(self) -> dict:
        """证据/巡检场景：优先最近帧，缓存为空才触发一次采集。"""
        if self._latest is not None and not self._is_stale(self._latest):
            return self._latest
        return await self.capture()

    def _is_stale(self, frame: dict) -> bool:
        """缓存新鲜度：优先按可证明采样时刻含误差判断（R2-08）。

        携带 camera_pts_host_clock.v1 证明的帧按采样时刻（含不确定度裕量）
        判断——刚接收不能让早已采样的帧续鲜；接收时间只进诊断。
        旧协议兼容帧（无采样时钟证明）保持原接收/采集时间判断，
        不给兼容分支签发新模式身份。
        """
        if self.max_age_seconds <= 0:
            return False
        captured_ns = frame.get("source_captured_monotonic_ns") \
            if frame.get("source_timing_provenance") == SOURCE_TIMING_PROVENANCE \
            else None
        if type(captured_ns) is int:
            uncertainty = frame.get("source_capture_uncertainty_ms")
            uncertainty_s = (float(uncertainty) / 1000.0
                             if type(uncertainty) in (int, float) else 0.0)
            age = (time.monotonic_ns() - captured_ns) / 1e9 - uncertainty_s
            return age > self.max_age_seconds
        value = frame.get("received_at") or frame.get("captured_at")
        if not value:
            return True
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds()
            return age > self.max_age_seconds
        except (TypeError, ValueError):
            return True

    async def check_quality(self) -> dict:
        """复用驱动的质量判定，但不额外驱动底层采集（用最近帧缓存）。

        缓存为空时先经 broker.capture() 取一帧，保证底层采集仍走唯一入口。
        """
        analyze = getattr(self._driver, "_analyze", None)
        classify = getattr(self._driver, "_classify", None)
        if analyze is None or classify is None:
            checker = getattr(self._driver, "check_quality", None)
            if checker is not None:
                try:
                    result = await checker()
                except Exception as exc:
                    raise CameraQualityCheckError(quality_failure_diagnostics(exc)) from None
                return result | quality_diagnostics(result)
        try:
            frame = await self.latest_or_capture()
        except Exception as exc:
            raise CameraQualityCheckError(
                quality_failure_diagnostics(exc, capture_failed=True)) from None
        frame = self._normalize(frame)
        if analyze is None or classify is None:
            return {"quality": "clear", "quality_display": "清晰", "frozen": frame.get("frozen", False),
                    **quality_diagnostics({"quality": "clear"})}
        image = frame.get("image_bytes", frame.get("image"))
        try:
            geometry = getattr(self._driver, "_quality_geometry", None)
            if geometry is not None:
                corners, tolerance, search_region = geometry(frame)
                metrics = analyze(image, expected_corners=corners,
                                  tolerance_px=tolerance,
                                  search_region=search_region)
            else:
                metrics = analyze(image)
            quality = classify(metrics, bool(frame.get("frozen")))
        except Exception as exc:
            # A fresh frame with uncertain phone geometry is not a dead stream.
            return {"quality": "abnormal", "quality_display": display(CAMERA_QUALITY, "abnormal"),
                    "frozen": frame.get("frozen", False), **quality_failure_diagnostics(exc)}
        return {
            "quality": quality,
            "quality_display": display(CAMERA_QUALITY, quality),
            **quality_diagnostics({
                "quality": quality, "metrics": metrics,
                "reason_code": "camera_detail_insufficient" if quality == "abnormal" else None,
            }),
            "frozen": frame.get("frozen", False),
        }
