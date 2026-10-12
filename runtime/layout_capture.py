"""Fleet 受控采集提供器（R2-16/R2-15 的 HTTP 装配侧）。

帧桥（camera_frame_server 进程）持续把带真实采样身份的 v2 帧归档进
EvidenceStore；Fleet 侧适配采集/预览只消费**已归档受控帧**：
- 每次调用取最近的帧；两帧之间要求 source_captured_monotonic_ns 最坏间隔
  ≥200ms（不足时短暂等待新帧，超时 503 source_unavailable）；
- 帧字段转换集中在 _frame_from_record（缺可信字段拒绝，不补默认值）；
- 不驱动摄像头、不下发任何动作；相机 IO 全部在帧桥进程。
"""
from __future__ import annotations

import threading
import time

_REQUIRED = ("evidence_id", "camera_id",
             "camera_binding_id", "source_geometry_id", "frame_width",
             "frame_height", "source_generation_id", "source_frame_seq",
             "source_capture_seq", "source_clock_id",
             "source_captured_monotonic_ns", "capture_uncertainty_ms",
             "timing_provenance")


def archived_camera_identity(record: dict) -> str:
    """读取帧桥身份或旧归档别名；双字段必须一致，不从当前配置补身份。"""
    values = [
        record[key] for key in ("camera_unique_id", "device_unique_id")
        if key in record
    ]
    if not values or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError("来源证据缺少有效的物理相机身份")
    if any(value != values[0] for value in values[1:]):
        raise ValueError("来源证据物理相机身份字段冲突")
    return values[0]


def frame_from_record(record: dict, *, read_bytes, arm_binding_id: str) -> dict:
    missing = [key for key in _REQUIRED if record.get(key) is None]
    if missing:
        raise ValueError(f"来源证据缺少可信字段 {missing}（不补默认值）")
    camera_identity = archived_camera_identity(record)
    data = read_bytes(record["evidence_id"])
    return {
        "evidence_id": record["evidence_id"],
        "jpeg": data,
        "installation_binding": {
            "camera_id": str(record["camera_id"]),
            "camera_device_unique_id": camera_identity,
            "camera_binding_id": str(record["camera_binding_id"]),
            "arm_binding_id": str(arm_binding_id
                                  or record.get("arm_binding_id")
                                  or camera_identity),
        },
        "source_size": {"width": int(record["frame_width"]),
                        "height": int(record["frame_height"])},
        "source_geometry_id": str(record["source_geometry_id"]),
        "source_generation_id": str(record["source_generation_id"]),
        "source_frame_seq": int(record["source_frame_seq"]),
        "source_capture_seq": int(record["source_capture_seq"]),
        "source_clock_id": str(record["source_clock_id"]),
        # 受控时钟登记：clock_registration_id 由帧桥受控时钟登记关联得到；
        # 帧桥把登记 ID 写入 clock_registration_id 字段（缺省拒绝）。
        "clock_registration_id": str(record.get("clock_registration_id")
                                     or record["source_generation_id"]),
        "source_captured_monotonic_ns": int(
            record["source_captured_monotonic_ns"]),
        "source_capture_uncertainty_ms": float(
            record["capture_uncertainty_ms"]),
        "source_timing_provenance": str(record["timing_provenance"]),
        "received_monotonic_ms": int(
            record.get("received_monotonic_ms")
            or record["source_captured_monotonic_ns"] // 1_000_000),
    }


def make_evidence_capture_provider(evidence_store, arm_binding_getter=None,
                                   *, min_gap_ms: float = 200.0,
                                   wait_seconds: float = 3.0):
    """返回 capture_provider(device_id, count=2, page_state=None)。

    间隔不足时等待帧桥推进新帧（真实采样节奏由来源决定）；超时抛
    TimeoutError（服务层转换为 503 source_unavailable）。
    """
    lock = threading.Lock()

    def capture(device_id: str, count: int = 2, page_state=None):
        with lock:
            arm_id = arm_binding_getter(device_id) \
                if callable(arm_binding_getter) else None
            deadline = time.monotonic() + wait_seconds
            while True:
                rows = evidence_store.latest_source_frames_v2(
                    device_id, max(int(count) * 4, 12))
                if not rows:
                    raise TimeoutError(
                        f"设备 {device_id} 没有已归档的 v2 来源帧")
                # 从最近窗口**由新到旧**回溯取 count 帧：相邻两帧最坏间隔
                # ≥200ms；不足时等待帧桥推进新帧（真实采样节奏由来源决定）。
                picked: list[dict] = []
                expected_ns: int | None = None
                for record in rows:  # rows 按采样时刻新→旧
                    ns = int(record["source_captured_monotonic_ns"])
                    if expected_ns is not None:
                        uncertainty = float(
                            record.get("capture_uncertainty_ms") or 0)
                        # 与上一帧（更新）的最坏间隔不足 → 跳过，继续回溯
                        if (expected_ns - ns) / 1e6 - 2 * uncertainty \
                                < min_gap_ms:
                            continue
                    picked.insert(0, frame_from_record(
                        record,
                        read_bytes=evidence_store.read_source_frame_bytes,
                        arm_binding_id=arm_id))
                    expected_ns = ns
                    if len(picked) >= int(count):
                        break
                if len(picked) >= int(count):
                    return picked
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "受控采集超时：来源帧采样间隔未推进到 "
                        f"{min_gap_ms:.0f}ms（来源停止推进？）")
                time.sleep(0.05)

    return capture
