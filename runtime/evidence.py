"""证据存储：截图落盘 + SHA-256 哈希 + 受控引用。

- 证据文件按任务归档：evidence/store/{task_id}/{evidence_id}.png
- 证据记录包含：设备、任务、类型、中文说明、采集时间、哈希、分辨率。
- 客户原文不进入通用日志，只保存受控引用与哈希。
"""
from __future__ import annotations

from pathlib import Path

import os

import json
import os
from pathlib import Path

from runtime.util import iso, new_id, sha256_bytes, utc_now


class EvidenceStore:
    def __init__(self, store, evidence_dir: Path):
        self._store = store
        self._dir = Path(evidence_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # FIX-08（滑动附件 §4.1）：帧证据的采样身份字段——存在才落记录，
    # 不从当前系统时间或绑定补造。
    _SAMPLING_IDENTITY_KEYS = (
        "source_frame_seq", "source_captured_at", "source_received_at",
        "source_legacy_mode", "source_generation_id", "source_capture_seq",
        "source_clock_id", "source_captured_monotonic_ns",
        "source_capture_uncertainty_ms", "source_timing_provenance",
        "broker_generation_id", "broker_seq", "broker_monotonic_ns",
        "camera_binding_id", "source_geometry_id", "binding_revision",
        "device_unique_id",
    )

    @staticmethod
    def _sampling_identity(frame: dict) -> dict:
        return {key: frame[key] for key in EvidenceStore._SAMPLING_IDENTITY_KEYS
                if key in frame and frame[key] is not None}

    def archive_camera_frame(
        self,
        frame: dict,
        observation_context: dict | None = None,
        *,
        device_id: str,
        task_id: str | None,
        label: str,
        action_instance_id: str | None = None,
        sample_index: int | None = None,
        phase: str | None = None,
        camera_id: str | None = None,
        operator: str = "runtime",
        kind: str = "frame",
        kind_display: str = "观察帧",
    ) -> dict:
        """归档**已经采到**的不可变帧（滑动附件 §3.3）：本方法内不再采集。

        帧的身份（来源序号/PTS/broker 接纳序号与单调时刻）原样进入记录；
        observation_context 携带动作归属（task/action/动作编号/采样序号/相位），
        由调用方在采样时冻结，不重拍、不重编码、不用当前绑定补写旧帧。
        """
        if not isinstance(frame, dict):
            raise ValueError("归档帧必须是冻结的帧对象")
        image = frame.get("image_bytes") or frame.get("image")
        if not image:
            raise ValueError("归档帧缺少原字节，拒绝伪造证据")
        evidence_id = new_id("evd")
        task_part = task_id or "device"
        target_dir = self._dir / task_part
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{evidence_id}.png"
        file_path.write_bytes(image)
        on_disk_hash = sha256_bytes(file_path.read_bytes())
        frame_hash = frame.get("sha256")
        if frame_hash is not None and frame_hash != on_disk_hash:
            raise RuntimeError("归档帧哈希与原字节不一致，拒绝引用该证据")
        record = {
            "evidence_id": evidence_id,
            "task_id": task_id,
            "device_id": device_id,
            "camera_id": camera_id,
            "kind": kind,
            "kind_display": kind_display,
            "label": label,
            "captured_at": frame.get("captured_at"),
            "sha256": on_disk_hash,
            "file_path": str(file_path.relative_to(self._dir)),
            "width": frame.get("width"),
            "height": frame.get("height"),
            "captured_by": operator,
            "sampling": self._sampling_identity(frame),
        }
        if kind != "screenshot":
            # 新观察帧走 evidence_type=frame；旧 screenshot 记录保持原字段集。
            record["evidence_type"] = kind
        if action_instance_id is not None:
            record["action_instance_id"] = action_instance_id
        if sample_index is not None:
            record["sample_index"] = sample_index
        if phase is not None:
            record["phase"] = phase
        if observation_context:
            record["observation_context"] = dict(observation_context)
        self._store.save_evidence(record)
        return record

    def capture_screenshot(
        self,
        *,
        device_id: str,
        camera,
        task_id: str | None,
        label: str,
        operator: str = "runtime",
        observation_context: dict | None = None,
    ) -> dict:
        """用指定摄像头采集一帧并归档为证据（老入口：委托 archive_camera_frame）。

        FIX-08：记录补充帧的采样身份（source/broker 序号与单调时刻，存在才落），
        不再丢弃截图与采样时序的关联。

        observation_context：步骤归属等结构化上下文（如 step_no / step_action），
        原样落进证据记录，供事后按步骤检索，不再靠 label 中文串反推步骤号。
        """
        frame = camera.capture_sync()
        # Shared brokers expose camera_id; legacy drivers use device_id.
        # Read the source actually used for this capture, never the task target.
        camera_id = getattr(camera, "camera_id", None) or getattr(camera, "device_id", None)
        return self.archive_camera_frame(
            frame,
            observation_context,
            device_id=device_id, task_id=task_id, label=label,
            camera_id=camera_id, operator=operator,
            kind="screenshot", kind_display="截图",
        )

    def save_file_evidence(
        self, *, device_id: str, task_id: str | None, data: bytes, label: str, suffix: str = ".bin"
    ) -> dict:
        """保存非截图类证据（例如导出的回报原文）。"""
        evidence_id = new_id("evd")
        task_part = task_id or "device"
        target_dir = self._dir / task_part
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{evidence_id}{suffix}"
        file_path.write_bytes(data)
        record = {
            "evidence_id": evidence_id,
            "task_id": task_id,
            "device_id": device_id,
            "kind": "file",
            "kind_display": "文件",
            "label": label,
            "captured_at": iso(utc_now()),
            "sha256": sha256_bytes(data),
            "file_path": str(file_path.relative_to(self._dir)),
            "captured_by": "runtime",
        }
        self._store.save_evidence(record)
        return record

    # ---- camera_source_v2（camera-binding B03）：同代际+序号幂等复用 ----
    def _source_frame_dir(self, device_id: str) -> Path:
        path = self._dir / "camera_source_v2" / device_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def find_source_frame_v2(self, device_id: str, generation: str,
                             seq: int) -> dict | None:
        index = self._source_frame_dir(device_id) / f"{generation}-{seq}.json"
        if not index.exists():
            return None
        try:
            return json.loads(index.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def latest_source_frame_v2(self, device_id: str) -> dict | None:
        # 注意：此处不打保留标记——预览/新鲜度轮询不属于业务消费；
        # 保留标记只在 read_source_frame_bytes（观察/粗框等业务读原图）时打。
        directory = self._source_frame_dir(device_id)
        candidates = sorted(directory.glob("*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for index in candidates:
            try:
                return json.loads(index.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
        return None

    def latest_source_frames_v2(self, device_id: str, limit: int = 8) -> list[dict]:
        """最近归档的至多 limit 条 camera_source_v2 记录（新→旧）。

        R2-16/R2-15（布局受控采集/粗框新鲜帧）消费端：与
        latest_source_frame_v2 同一归档目录、同一 mtime 排序，只读不改档。
        """
        directory = self._source_frame_dir(device_id)
        candidates = sorted(directory.glob("*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        records: list[dict] = []
        for index in candidates:
            if len(records) >= max(1, int(limit)):
                break
            try:
                record = json.loads(index.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def read_source_frame_bytes(self, evidence_id: str) -> bytes:
        record = self.get(evidence_id)
        if record is None or "file_path" not in record:
            raise FileNotFoundError(evidence_id)
        self.mark_source_frame_consumed(record)
        return (self._dir / record["file_path"]).read_bytes()

    # ---- 来源帧保留策略（用户拍板 2026-10-10）：只有被业务流程消费过的帧保留 ----
    def mark_source_frame_consumed(self, record: dict) -> None:
        """业务读取即打保留标记：DB 行 + 落盘 sidecar 同步写 consumed_at。

        帧桥清扫线程只清无标记且超过保留期的帧；标记一次即永久保留
        （粗框锚定帧/新消息观察帧等长期引用由此满足）。
        """
        evidence_id = record.get("evidence_id")
        if not evidence_id or record.get("consumed_at"):
            return
        record["consumed_at"] = iso(utc_now())
        try:
            self._store.save_evidence(record)
        except Exception:
            return  # DB 失败不影响读取；文件标记仍尽量写
        file_path = record.get("file_path")
        if file_path:
            sidecar = (self._dir / file_path).with_suffix(".json")
            try:
                # record 可能携带帧字节（测试桥/部分来源即含）：sidecar 只担
                # 元数据与标记，bytes 用 default=str 降级，不因序列化炸掉标记。
                sidecar.write_text(json.dumps(record, ensure_ascii=False,
                                              indent=2, default=str),
                                   encoding="utf-8")
            except (OSError, TypeError):
                pass  # 标记落盘失败不阻塞业务；DB 标记已生效

    def purge_source_frame(self, evidence_id: str) -> bool:
        """清扫线程专用：删除无保留标记的过期来源帧（文件+sidecar+DB 行）。

        删除前重读 sidecar 二次核对标记，避免与业务标记并发竞态误删。
        """
        record = self._store.get_evidence(evidence_id)
        if record is None:
            return False
        file_path = record.get("file_path")
        if file_path:
            sidecar = (self._dir / file_path).with_suffix(".json")
            if sidecar.exists():
                try:
                    fresh = json.loads(sidecar.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    fresh = None
                if isinstance(fresh, dict) and fresh.get("consumed_at"):
                    return False  # 业务刚标记：保留
                try:
                    sidecar.unlink()
                except OSError:
                    return False
            try:
                (self._dir / file_path).unlink()
            except OSError:
                return False
        try:
            self._store._write("DELETE FROM evidence WHERE evidence_id=?",
                               (evidence_id,))
        except Exception:
            return False
        return True

    def save_source_frame_v2(self, *, device_id: str, generation: str,
                             seq: int, jpeg: bytes, record: dict) -> dict:
        """§5.1：先保存文件/fsync、再保存引用（失败无可用证据）。

        归档不可变原 JPEG：不得重编码、不得用当前绑定补写旧帧 metadata；
        调用方保证 record 中的身份/时间字段来自该帧的冻结包。
        """
        directory = self._source_frame_dir(device_id)
        jpeg_path = directory / f"{generation}-{seq}.jpg"
        with open(jpeg_path, "wb") as handle:
            handle.write(jpeg)
            handle.flush()
            os.fsync(handle.fileno())
        evidence_id = record.get("evidence_id") or new_id("csf")
        record = {**record, "evidence_id": evidence_id,
                  "file_path": str(jpeg_path.relative_to(self._dir))}
        index = directory / f"{generation}-{seq}.json"
        with open(index, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        self._store.save_evidence(record)
        return record

    def get(self, evidence_id: str) -> dict | None:
        return self._store.get_evidence(evidence_id)

    def for_task(self, task_id: str) -> list[dict]:
        return self._store.evidence_for_task(task_id)

    def read_bytes(self, record: dict) -> bytes:
        path = self._dir / record["file_path"]
        return path.read_bytes()
