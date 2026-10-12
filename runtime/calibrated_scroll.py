"""calibrated_scroll：校准直驱上下滑动（2026-10-03 规格 §5/§6）。

新任务 calibrated-scroll.v1 只由当前 schemaVersion=3 自动校准计算上滑/下滑；
不读手势证书、导航模板、顶部模板或 automatic_gestures.json（§5.3）。
机械执行与内容效果分离表达（§6.4）：mechanical_status（机械是否完成）与
effect_status（内容是否滚动）互不冒充。计划复用 drivers/screen_gestures，
光流测量复用平台 camera.measure_scroll 能力（drivers/scroll_observation），
本模块不复制图像算法、不创建第二套串口执行器。

FIX-08（滑动附件 §3-7，2026-10-04 实施单）：单次动作使用连续光流链——
动作前持久动作编号/快照与≤4400ms 机械预算；前帧年龄≤400ms；前帧接纳即开
采样协程（t0+n*200ms 调度）与唯一一次 arm.swipe 并行；链帧同代际/严格递增
序号/间隔≤400ms/总≤30 帧；完成栅栏后 3 帧 2 对静止判定；归档已采帧不另拍；
纯视觉未知持久暂停不伪造机械未知；一步不建子任务、不抢第二租约。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from drivers import screen_gestures
from drivers.arm import DriverError, gesture_motion_confirmed
from drivers.camera_broker import frame_chain_identity
from tasks.plugin_api import PluginViolationError

CONTRACT_VERSION = "calibrated-scroll.v1"
PLANNING_VERSION = "calibrated-scroll-planning.v1"
OBSERVATION_VERSION = "calibrated-scroll-observation.v1"

ACTION_RECORD_VERSION = "calibrated-scroll-action.v1"
# 滑动附件 CONTRACT §4：动作状态机（prepared/claimed/completed/
# review_required/reviewed/aborted_before_motion），与 records.schema 一致。
ACTION_STATES = ("prepared", "claimed", "completed", "review_required",
                 "reviewed", "aborted_before_motion")

SCROLL_ACTIONS = ("swipe_up", "swipe_down")
ACTION_DISPLAY = {"swipe_up": "上滑", "swipe_down": "下滑"}
# §5.1：按手指方向命名；摆正屏幕坐标中上滑 dy<0、下滑 dy>0。
ACTION_EXPECTED_DY_SIGN = {"swipe_up": -1, "swipe_down": 1}

# §6.2：默认观察 ROI（屏幕归一化），观察区域不是允许点击范围。
DEFAULT_OBSERVATION_ROI = {"x": 0.10, "y": 0.20, "width": 0.80, "height": 0.60}

# §7.3.2（D04）：定向查找每轮预算；换方向/新任务编号/重启都不重置。
ROUND_BUDGET = {"max_actions": 8, "window_seconds": 90}

# §6.3 / 滑动附件 §3.1 本版采样初始值：待实测的保守能力边界，
# 不能据此加速机械或跳过收尾。
SAMPLING_SUPPORT = {"interval_ms": 200, "max_pair_gap_ms": 400,
                    "max_frames": 30, "max_window_ms": 5800,
                    "observation_budget_ms": 4400,
                    "before_wait_timeout_ms": 2000, "before_max_age_ms": 400,
                    "after_wait_timeout_ms": 1000, "capture_timeout_ms": 400}

# FIX-08 ①：完整机械准备→安全收尾预算（滑动附件 §3.1「长计划提前拒绝」）。
# 滑动时长来自计划自动时长；悬停走位与安全收尾取保守分段估计。
# 预算无法计算（unproven）或 >4400ms（exceeds_window）→ 零动作、能力不可用；
# 不得缩短滑动、加速机械臂或拆分观察来迁就窗口。
MOTION_BUDGET_MARGINS_MS = {"approach_ms": 500, "safe_cleanup_ms": 1100}

# 任务输入拒绝的字段（§5.2：客户端只选方向，机械参数/证书/模板一律非法）。
FORBIDDEN_PAYLOAD_FIELDS = (
    "gesture_certificate_id", "gesture_profile_version", "top_proof_available",
    "template_id", "point", "start", "end", "duration_ms", "interval_ms",
    "depth_mm", "feed", "speed", "ready", "accepted", "skip_gates",
)

_PRE_MOTION_REJECTIONS = {
    "driver_policy_rejected", "driver_uncalibrated", "driver_gesture_invalid",
    "driver_limit_exceeded", "driver_estop_latched", "driver_camera_waiting",
}


class ScrollCapabilityError(Exception):
    """能力查询/计划阶段的确定性拒绝（携带阻断码列表）。"""

    def __init__(self, reasons: list[dict]):
        self.reasons = reasons
        super().__init__("；".join(str(item.get("message") or item.get("code"))
                                   for item in reasons) or "calibrated_scroll 不可用")


class ScrollActionStoreError(Exception):
    """动作/核对存储的持久边界失败（携带稳定码：storage_unavailable /
    scroll_record_invalid / scroll_review_conflict 等）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# R2-07（滑动附件 CONTRACT §4 / README §7.2）：持久动作/核对存储
# ---------------------------------------------------------------------------

_ACTION_RECORD_DDL = """
CREATE TABLE IF NOT EXISTS scroll_action_records (
  task_id TEXT NOT NULL,
  action_instance_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  state TEXT NOT NULL,
  revision INTEGER NOT NULL,
  json TEXT NOT NULL,
  PRIMARY KEY (task_id, action_instance_id)
);
CREATE INDEX IF NOT EXISTS scroll_action_device_state
  ON scroll_action_records(device_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS scroll_action_step
  ON scroll_action_records(task_id, json_extract(json, '$.step_key'));
CREATE UNIQUE INDEX IF NOT EXISTS scroll_action_claim
  ON scroll_action_records(json_extract(json, '$.claim_id'))
  WHERE json_extract(json, '$.claim_id') IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS scroll_action_issue
  ON scroll_action_records(json_extract(json, '$.pause_issue_id'))
  WHERE json_extract(json, '$.pause_issue_id') IS NOT NULL;
CREATE TABLE IF NOT EXISTS scroll_review_receipts (
  device_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  json TEXT NOT NULL,
  PRIMARY KEY (device_id, request_id)
);
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


class ScrollActionStore:
    """逐动作持久状态机：prepare → claim → finalize/progress；review/resume；
    启动扫描重建阻断。事务 BEGIN IMMEDIATE + state/revision CAS；
    claim 提交（fsync/WAL+synchronous=FULL）确认后才允许调用驱动。

    测试故障注入点（arm_faults，LayoutStateStore.arm_faults 同款思路）：
    prepare/claim/finalize/pause/progress/review/resume/scan——命中即在
    持久边界抛 ScrollActionStoreError("storage_unavailable")，不替换任何
    正常逻辑。
    """

    def __init__(self, db_path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False,
                                     isolation_level=None, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # 动作认领是下发顺序点：崩溃后不允许“成功但丢失”。
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_ACTION_RECORD_DDL)
        self._fault_points: set[str] = set()
        self.scan_summary: dict = {}
        self._scanned = False

    # ---- 基础 ----
    def arm_faults(self, points) -> None:
        self._fault_points = set(points)

    def _fault(self, point: str) -> None:
        if point in self._fault_points:
            raise ScrollActionStoreError(
                "storage_unavailable", f"故障注入点 {point}")

    def close(self):
        with self._lock:
            self._conn.close()

    @staticmethod
    def _decode(row) -> dict:
        record = json.loads(row["json"])
        record["revision"] = int(row["revision"])
        record["state"] = row["state"]
        return record

    def _load(self, conn, task_id: str, action_instance_id: str) -> dict | None:
        row = conn.execute(
            "SELECT state, revision, json FROM scroll_action_records "
            "WHERE task_id=? AND action_instance_id=?",
            (task_id, action_instance_id)).fetchone()
        return self._decode(row) if row is not None else None

    # ---- prepare ----
    def prepare(self, record: dict) -> dict:
        """prepared（revision=1）。同键同内容幂等复用；异内容冲突拒绝。"""
        self._fault("prepare")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._load(self._conn, record["task_id"],
                                      record["action_instance_id"])
                if existing is not None:
                    if existing.get("state") != "prepared":
                        return existing  # 已认领/终态：不可重放，调用方按原记录处理
                    if existing.get("request_sha256") != record.get("request_sha256"):
                        raise ScrollActionStoreError(
                            "scroll_record_invalid",
                            "动作编号已存在且内容不同：拒绝覆盖（重试不换内容）")
                    self._conn.execute("COMMIT")
                    return existing
                row_json = json.dumps(record, ensure_ascii=False, allow_nan=False)
                self._conn.execute(
                    "INSERT INTO scroll_action_records "
                    "(task_id, action_instance_id, device_id, state, revision, json) "
                    "VALUES (?, ?, ?, 'prepared', 1, ?)",
                    (record["task_id"], record["action_instance_id"],
                     record["device_id"], row_json))
                self._conn.execute("COMMIT")
            except ScrollActionStoreError:
                self._conn.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise ScrollActionStoreError(
                    "scroll_record_invalid", f"动作记录唯一性冲突：{exc}") from exc
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise ScrollActionStoreError(
                    "storage_unavailable", f"动作记录写入失败：{exc}") from exc
        return self.get_action(record["device_id"], record["action_instance_id"])

    # ---- claim ----
    def claim(self, task_id: str, action_instance_id: str, *,
              expected_revision: int, claim_id: str, claimed_at: str) -> dict:
        """prepared → claimed（恰一次）。提交确认后才允许 arm.swipe。"""
        self._fault("claim")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                record = self._load(self._conn, task_id, action_instance_id)
                if record is None:
                    raise ScrollActionStoreError(
                        "scroll_record_invalid", "动作记录不存在：禁止认领")
                if record["state"] == "claimed":
                    if record.get("claim_id") == claim_id:
                        self._conn.execute("COMMIT")
                        return record  # 同一认领的幂等重放
                    raise ScrollActionStoreError(
                        "scroll_record_invalid",
                        "动作已被其他认领凭据认领：不可重放（认领后未知不可重试）")
                if record["state"] != "prepared":
                    raise ScrollActionStoreError(
                        "scroll_record_invalid",
                        f"动作状态为 {record['state']}：不可认领（不重进准备态）")
                if int(record["revision"]) != int(expected_revision):
                    raise ScrollActionStoreError(
                        "scroll_record_invalid",
                        f"认领 revision 冲突：期望 {expected_revision}，"
                        f"当前 {record['revision']}")
                record.update({
                    "state": "claimed", "revision": int(record["revision"]) + 1,
                    "claim_id": claim_id, "claimed_at": claimed_at,
                    "updated_at": _utc_iso(),
                })
                self._conn.execute(
                    "UPDATE scroll_action_records SET state='claimed', "
                    "revision=?, json=? WHERE task_id=? AND action_instance_id=?",
                    (record["revision"], json.dumps(record, ensure_ascii=False,
                                                    allow_nan=False),
                     task_id, action_instance_id))
                self._conn.execute("COMMIT")
            except ScrollActionStoreError:
                self._conn.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise ScrollActionStoreError(
                    "storage_unavailable", f"认领提交失败：{exc}") from exc
        return record

    # ---- progress / finalize ----
    def record_progress(self, task_id: str, action_instance_id: str, *,
                        expected_revision: int, patch: dict) -> dict | None:
        """claimed 记录上只追加本动作进度（driver_invoked/采样事实）。"""
        self._fault("progress")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                record = self._load(self._conn, task_id, action_instance_id)
                if record is None or record["state"] != "claimed":
                    self._conn.execute("COMMIT")
                    return None
                if int(record["revision"]) != int(expected_revision):
                    self._conn.execute("COMMIT")
                    return record  # 并发进度：以已存事实为准（诊断不覆盖）
                record.update(patch)
                record["revision"] = int(record["revision"]) + 1
                record["updated_at"] = _utc_iso()
                self._conn.execute(
                    "UPDATE scroll_action_records SET revision=?, json=? "
                    "WHERE task_id=? AND action_instance_id=? AND state='claimed'",
                    (record["revision"], json.dumps(record, ensure_ascii=False,
                                                    allow_nan=False),
                     task_id, action_instance_id))
                self._conn.execute("COMMIT")
                return record
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise ScrollActionStoreError(
                    "storage_unavailable", f"进度写入失败：{exc}") from exc

    def finalize(self, task_id: str, action_instance_id: str, *,
                 expected_revision: int, state: str, patch: dict) -> dict:
        """claimed → completed | review_required（结果与暂停同一事务）。"""
        if state not in ("completed", "review_required"):
            raise ScrollActionStoreError(
                "scroll_record_invalid", f"finalize 目标状态非法：{state}")
        self._fault("pause" if patch.get("pause_issue_id") else "finalize")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                record = self._load(self._conn, task_id, action_instance_id)
                if record is None:
                    raise ScrollActionStoreError(
                        "scroll_record_invalid", "动作记录不存在：禁止收尾")
                if record["state"] == state and \
                        record.get("task_outcome") == patch.get("task_outcome"):
                    self._conn.execute("COMMIT")
                    return record  # 幂等重放（同内容）
                if record["state"] not in ("claimed",):
                    raise ScrollActionStoreError(
                        "scroll_record_invalid",
                        f"动作状态为 {record['state']}：只能从 claimed 收尾")
                if int(record["revision"]) != int(expected_revision):
                    raise ScrollActionStoreError(
                        "scroll_record_invalid",
                        f"收尾 revision 冲突：期望 {expected_revision}，"
                        f"当前 {record['revision']}")
                record.update(patch)
                record["state"] = state
                record["revision"] = int(record["revision"]) + 1
                record["updated_at"] = _utc_iso()
                self._conn.execute(
                    "UPDATE scroll_action_records SET state=?, revision=?, json=? "
                    "WHERE task_id=? AND action_instance_id=?",
                    (state, record["revision"],
                     json.dumps(record, ensure_ascii=False, allow_nan=False),
                     task_id, action_instance_id))
                self._conn.execute("COMMIT")
            except ScrollActionStoreError:
                self._conn.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise ScrollActionStoreError(
                    "storage_unavailable", f"结果提交失败：{exc}") from exc
        return record

    # ---- 查询 ----
    def get_action(self, device_id: str, action_instance_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state, revision, json FROM scroll_action_records "
                "WHERE action_instance_id=?", (action_instance_id,)).fetchone()
        record = self._decode(row) if row is not None else None
        if record is not None and record.get("device_id") != device_id:
            return None
        return record

    def find_by_task(self, device_id: str, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state, revision, json FROM scroll_action_records "
                "WHERE task_id=? ORDER BY rowid LIMIT 1", (task_id,)).fetchone()
        record = self._decode(row) if row is not None else None
        if record is not None and record.get("device_id") != device_id:
            return None
        return record

    def issues(self, device_id: str) -> list[dict]:
        """本设备全部未恢复的视觉/未知问题（review_required/reviewed，
        manual_resume_required 在独立恢复前保持 true）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, revision, json FROM scroll_action_records "
                "WHERE device_id=? AND state IN ('review_required','reviewed') "
                "ORDER BY rowid", (device_id,)).fetchall()
        return [self._decode(row) for row in rows]

    # ---- review / resume ----
    def review_action(self, device_id: str, issue_id: str, *, request: dict,
                      reviewed_by: str) -> dict:
        """人工核对：只解本条视觉暂停，不改原结果、不重放、不自动恢复。
        幂等：同 (device, request_id) 同摘要返回原回执；异摘要 409 冲突。"""
        self._fault("review")
        request_digest = _canonical_sha256(
            {"device_id": device_id, "issue_id": issue_id, "request": request})
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT json FROM scroll_review_receipts "
                    "WHERE device_id=? AND request_id=?",
                    (device_id, str(request.get("request_id")))).fetchone()
                if row is not None:
                    receipt = json.loads(row["json"])
                    if receipt.get("request_sha256") != request_digest:
                        raise ScrollActionStoreError(
                            "scroll_review_conflict",
                            "同编号核对请求内容不同：疑似重放/冲突")
                    self._conn.execute("COMMIT")
                    return receipt
                record = self._load_by_issue(self._conn, device_id, issue_id)
                if record is None:
                    raise ScrollActionStoreError(
                        "scroll_review_not_found",
                        "设备/问题不匹配：核对请求与持久问题不一致")
                if record["state"] not in ("review_required", "reviewed"):
                    raise ScrollActionStoreError(
                        "scroll_review_conflict",
                        f"问题状态为 {record['state']}：不可核对")
                if request.get("acknowledge_no_replay") is not True:
                    raise ScrollActionStoreError(
                        "scroll_review_conflict",
                        "核对请求必须显式 acknowledge_no_replay=true")
                in_flight = self._conn.execute(
                    "SELECT COUNT(*) FROM scroll_action_records "
                    "WHERE device_id=? AND state IN ('prepared','claimed')",
                    (device_id,)).fetchone()[0]
                if int(in_flight):
                    raise ScrollActionStoreError(
                        "device_busy", "设备有执行中动作：禁止核对")
                record["state"] = "reviewed"
                record["revision"] = int(record["revision"]) + 1
                record["review"] = {"reviewed_at": _utc_iso(),
                                    "reviewed_by": str(reviewed_by),
                                    "request": dict(request)}
                record["updated_at"] = _utc_iso()
                # manual_resume_required 保持 true：核对≠自动恢复
                self._conn.execute(
                    "UPDATE scroll_action_records SET state='reviewed', "
                    "revision=?, json=? WHERE task_id=? AND action_instance_id=?",
                    (record["revision"], json.dumps(record, ensure_ascii=False,
                                                    allow_nan=False),
                     record["task_id"], record["action_instance_id"]))
                receipt = {
                    "contract_version": "calibrated-scroll-review.v1",
                    "device_id": device_id, "issue_id": issue_id,
                    "task_id": record["task_id"],
                    "action_instance_id": record["action_instance_id"],
                    "revision": record["revision"],
                    "reviewed_at": record["review"]["reviewed_at"],
                    "reviewed_by": str(reviewed_by),
                    "original_outcome": record.get("task_outcome"),
                    "issue_resolved": True,
                    "automatic_resume_performed": False,
                    "remaining_visual_issue_ids": [
                        item["pause_issue_id"]
                        for item in self._open_issues(self._conn, device_id)
                        if item.get("pause_issue_id")
                        and item["pause_issue_id"] != issue_id],
                    "request_sha256": request_digest,
                }
                self._conn.execute(
                    "INSERT INTO scroll_review_receipts "
                    "(device_id, request_id, issue_id, request_sha256, json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (device_id, str(request.get("request_id")), issue_id,
                     request_digest,
                     json.dumps(receipt, ensure_ascii=False, allow_nan=False)))
                self._conn.execute("COMMIT")
            except ScrollActionStoreError:
                self._conn.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise ScrollActionStoreError(
                    "storage_unavailable", f"核对提交失败：{exc}") from exc
        return receipt

    def resume_reviewed(self, device_id: str, *, principal: str) -> int:
        """独立恢复：所有视觉问题已 reviewed、无执行中动作时，
        仅清除 reviewed 记录的 manual_resume_required（逐行 revision+1）。"""
        self._fault("resume")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                in_flight = self._conn.execute(
                    "SELECT COUNT(*) FROM scroll_action_records "
                    "WHERE device_id=? AND state IN ('prepared','claimed')",
                    (device_id,)).fetchone()[0]
                if int(in_flight):
                    raise ScrollActionStoreError(
                        "device_busy", "设备有执行中动作：禁止恢复")
                rows = self._conn.execute(
                    "SELECT state, revision, json FROM scroll_action_records "
                    "WHERE device_id=? AND state IN "
                    "('review_required','reviewed')", (device_id,)).fetchall()
                records = [self._decode(row) for row in rows]
                if any(item["state"] != "reviewed" for item in records):
                    raise ScrollActionStoreError(
                        "manual_resume_required",
                        "存在未核对的视觉问题：先逐条核对再独立恢复")
                changed = 0
                for record in records:
                    if record.get("manual_resume_required") is not True:
                        continue
                    record["manual_resume_required"] = False
                    record["revision"] = int(record["revision"]) + 1
                    record["updated_at"] = _utc_iso()
                    record.setdefault("resume", []).append(
                        {"at": _utc_iso(), "principal": str(principal)})
                    self._conn.execute(
                        "UPDATE scroll_action_records SET revision=?, json=? "
                        "WHERE task_id=? AND action_instance_id=?",
                        (record["revision"],
                         json.dumps(record, ensure_ascii=False, allow_nan=False),
                         record["task_id"], record["action_instance_id"]))
                    changed += 1
                self._conn.execute("COMMIT")
            except ScrollActionStoreError:
                self._conn.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise ScrollActionStoreError(
                    "storage_unavailable", f"恢复提交失败：{exc}") from exc
        return changed

    # ---- 启动扫描（先于派发；README §7.3）----
    def startup_scan(self) -> dict:
        self._fault("scan")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT state, revision, json FROM scroll_action_records "
                    "WHERE state IN ('prepared','claimed')").fetchall()
                summary = {"prepared_aborted": 0, "claimed_to_review": 0}
                now = _utc_iso()
                for row in rows:
                    record = self._decode(row)
                    if record["state"] == "prepared":
                        # 动作前中断：不建已动作的视觉暂停、不自动接着滑
                        record["state"] = "aborted_before_motion"
                        record["revision"] = int(record["revision"]) + 1
                        record["task_outcome"] = "failed"
                        record["reason_code"] = None
                        record["result"] = {
                            "outcome": "failed", "reason_code": None,
                            "note": "服务重启：动作准备后、认领前中断，"
                                    "不自动续滑（原编号只读可查）"}
                        record["updated_at"] = now
                        self._write_record(self._conn, record,
                                           "aborted_before_motion")
                        summary["prepared_aborted"] += 1
                    else:
                        # 认领后崩溃：一律按可能已动作处理（机械未知保护）
                        record["state"] = "review_required"
                        record["revision"] = int(record["revision"]) + 1
                        record["motion_started"] = None
                        record["mechanical_status"] = "unknown"
                        record["safe_cleanup_confirmed"] = False
                        record["task_outcome"] = "unknown"
                        record["reason_code"] = "mechanical_result_unknown"
                        record["updated_at"] = now
                        self._write_record(self._conn, record, "review_required")
                        summary["claimed_to_review"] += 1
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise ScrollActionStoreError(
                    "storage_unavailable", f"启动扫描失败：{exc}") from exc
        self.scan_summary = summary
        self._scanned = True
        return summary

    # ---- 内部 ----
    @staticmethod
    def _write_record(conn, record: dict, state: str) -> None:
        conn.execute(
            "UPDATE scroll_action_records SET state=?, revision=?, json=? "
            "WHERE task_id=? AND action_instance_id=?",
            (state, record["revision"],
             json.dumps(record, ensure_ascii=False, allow_nan=False),
             record["task_id"], record["action_instance_id"]))

    @staticmethod
    def _load_by_issue(conn, device_id: str, issue_id: str) -> dict | None:
        row = conn.execute(
            "SELECT state, revision, json FROM scroll_action_records "
            "WHERE device_id=? AND json_extract(json, '$.pause_issue_id')=?",
            (device_id, issue_id)).fetchone()
        if row is None:
            return None
        record = json.loads(row["json"])
        record["revision"] = int(row["revision"])
        record["state"] = row["state"]
        return record

    @staticmethod
    def _open_issues(conn, device_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT state, revision, json FROM scroll_action_records "
            "WHERE device_id=? AND state IN ('review_required','reviewed')",
            (device_id,)).fetchall()
        return [ScrollActionStore._decode(row) for row in rows]


_SCROLL_STORE_LOCK = threading.Lock()


def get_scroll_action_store(runtime) -> ScrollActionStore:
    """Runtime 级滑动动作存储（同库单实例；首次访问即启动扫描，
    扫描在派发之前完成——启动在重建阻断后才允许派发）。"""
    store = getattr(runtime, "_scroll_action_store", None)
    if store is None:
        with _SCROLL_STORE_LOCK:
            store = getattr(runtime, "_scroll_action_store", None)
            if store is None:
                store = ScrollActionStore(runtime.config.db_path)
                store.startup_scan()
                runtime._scroll_action_store = store
    return store


def _bundle(runtime, device_id: str) -> dict | None:
    from drivers import calibration_bundle as cb
    try:
        root = runtime.devices.settings_data_root(device_id)
    except Exception:
        return None
    try:
        return cb.load_bundle(device_id, root)
    except (OSError, ValueError, TypeError):
        return None


def binding_view(runtime, device_id: str) -> dict | None:
    """当前 v3 绑定快照；v2/损坏/缺失一律 None（不得加字段升级续用）。"""
    bundle = _bundle(runtime, device_id)
    if not bundle:
        return None
    try:
        return screen_gestures.binding_snapshot_v3(bundle)
    except (ValueError, TypeError, KeyError):
        return None


def motion_budget(plan: dict | None) -> dict:
    """FIX-08 ①：计划级机械观察预算（滑动附件 motion_budget 语义）。

    within_window：估计总时长≤observation_budget_ms（4400ms）；
    exceeds_window：超出本版观察窗口（scroll_observation_window_insufficient）；
    unproven：无法从计划证明（时长缺失），estimated_total_ms=null。
    不得改速度或改路径让慢机构过关。
    """
    duration = plan.get("durationMs") if isinstance(plan, dict) else None
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        return {"status": "unproven", "estimated_total_ms": None}
    total = float(duration) + MOTION_BUDGET_MARGINS_MS["approach_ms"] \
        + MOTION_BUDGET_MARGINS_MS["safe_cleanup_ms"]
    status = ("within_window"
              if total <= SAMPLING_SUPPORT["observation_budget_ms"]
              else "exceeds_window")
    return {"status": status, "estimated_total_ms": int(round(total))}


def plan_action(runtime, device_id: str, action: str) -> dict:
    """由当前校准计算一次上滑/下滑计划（§5.3：客户端只选方向）。"""
    if action not in SCROLL_ACTIONS:
        raise ScrollCapabilityError([{"code": "invalid_request",
                                      "message": f"action 只允许 {'/'.join(SCROLL_ACTIONS)}"}])
    bundle = _bundle(runtime, device_id)
    if not bundle:
        raise ScrollCapabilityError([{"code": "calibration_not_ready",
                                      "message": "当前校准档案不可读，无法计算滑动计划"}])
    try:
        from drivers import calibration_bundle as _cb
        # v3 档案的机械几何与 v2 同构；计划计算用掩码副本走既有规划器
        # （指纹等身份字段以 binding_snapshot_v3 的原始 v3 计算为准）。
        plans = screen_gestures.default_plans(
            {**bundle, "schemaVersion": _cb.V2_SCHEMA_VERSION})
    except (ValueError, TypeError, KeyError) as exc:
        raise ScrollCapabilityError([{"code": "calibration_not_ready",
                                      "message": f"校准无法生成滑动计划：{exc}"}]) from exc
    plan = next((item for item in plans if item.get("id") == action), None)
    if not plan:
        raise ScrollCapabilityError([{"code": "plan_unavailable",
                                      "message": f"校准未提供 {ACTION_DISPLAY[action]} 计划"}])
    if plan.get("calculated") is not True:
        raise ScrollCapabilityError(
            [{"code": "plan_unsafe",
              "message": f"{ACTION_DISPLAY[action]}路径在当前几何下不安全："
                         f"{'；'.join(plan.get('blockingReasons') or [])}"}])
    return plan


def _direction_reasons(runtime, device_id: str) -> dict[str, list[dict]]:
    reasons: dict[str, list[dict]] = {}
    for action in SCROLL_ACTIONS:
        try:
            plan_action(runtime, device_id, action)
            reasons[action] = []
        except ScrollCapabilityError as exc:
            reasons[action] = exc.reasons
    return reasons


def capability_view(runtime, device_id: str) -> dict:
    """§5.2 只读能力查询：零动作、不发证、不写校准。"""
    reasons: list[dict] = []
    try:
        runtime.devices.get(device_id)
    except Exception:
        return {"contract_version": CONTRACT_VERSION, "device_id": device_id,
                "binding": None, "usable": False,
                "actions": {action: {"usable": False,
                                     "blocking_reasons": [{"code": "device_not_found",
                                                           "message": "设备不存在"}]}
                            for action in SCROLL_ACTIONS},
                "blocking_reasons": [{"code": "device_not_found", "message": "设备不存在"}],
                "planning_version": PLANNING_VERSION}
    if runtime.estop.active:
        reasons.append({"code": "estop_active", "message": "全局急停中"})
    binding = binding_view(runtime, device_id)
    if binding is None:
        reasons.append({"code": "calibration_v3_unavailable",
                        "message": "需要实际验证完成的 schemaVersion=3 校准；"
                                   "旧 v2 档案不得加字段升级续用"})
    plugin_ready = True
    try:
        instance, _manifest = runtime.plugins.instance_for_task_type("calibrated_scroll")
        plugin_ready = instance is not None
    except Exception:
        plugin_ready = False
    if not plugin_ready:
        reasons.append({"code": "plugin_not_installed",
                        "message": "没有插件承载 calibrated_scroll 任务类型"})
    direction = _direction_reasons(runtime, device_id) if binding is not None else {
        action: [{"code": "calibration_v3_unavailable",
                  "message": "无 v3 校准绑定"}] for action in SCROLL_ACTIONS}
    # FIX-08 ①/G03-A20：逐方向机械观察预算——超限/无法证明即本版不可用，
    # 显式区分「本版观察窗口不足」与「完整时长待验证」，零动作。
    budgets: dict[str, dict] = {}
    for action in SCROLL_ACTIONS:
        blocks = list(direction[action])
        budget = {"status": "unproven", "estimated_total_ms": None}
        if not blocks:
            try:
                budget = motion_budget(plan_action(runtime, device_id, action))
            except ScrollCapabilityError:
                budget = {"status": "unproven", "estimated_total_ms": None}
        budgets[action] = budget
        if budget["status"] == "exceeds_window":
            blocks.append({"code": "scroll_observation_window_insufficient",
                           "message": "本版观察窗口不足：完整机械准备到安全收尾预算"
                                      "超过 4400ms，零动作（不调快机械参数）"})
        elif budget["status"] == "unproven":
            blocks.append({"code": "scroll_observation_window_insufficient",
                           "message": "完整时长待验证：无法从当前计划证明观察窗口预算"})
        direction[action] = blocks
    actions = {action: {"usable": plugin_ready and binding is not None and not blocks,
                        "blocking_reasons": blocks,
                        "motion_budget": budgets[action]}
               for action, blocks in direction.items()}
    if not actions["swipe_up"]["usable"] and not actions["swipe_down"]["usable"]:
        # 两方向共享几何；把方向级原因并入总体原因（按 code 去重）。
        seen = {item["code"] for item in reasons}
        for item in direction["swipe_up"]:
            if item["code"] not in seen:
                reasons.append(item)
    return {"contract_version": CONTRACT_VERSION, "device_id": device_id,
            "binding": binding,
            "usable": bool(binding) and plugin_ready and not runtime.estop.active
                      and all(actions[a]["usable"] for a in SCROLL_ACTIONS),
            "actions": actions,
            "blocking_reasons": reasons,
            "planning_version": PLANNING_VERSION,
            "calibration_version": (_bundle(runtime, device_id) or {}).get("schemaVersion"),
            "round_budget": dict(ROUND_BUDGET),
            "sampling_support": dict(SAMPLING_SUPPORT)}


def validate_payload(task: dict, device: dict) -> dict:
    """任务入参校验（§5.2）：客户端只传方向 + 查询所得完整绑定。"""
    payload = task.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload 必须为对象")
    illegal = sorted(set(payload) - {"action", "binding"})
    if illegal:
        raise ValueError(f"payload 包含未允许字段（客户端只选方向）：{', '.join(illegal)}")
    hit = [field for field in FORBIDDEN_PAYLOAD_FIELDS if field in payload]
    if hit:
        raise ValueError(f"禁止客户端覆盖机械参数或携带证书/模板字段：{', '.join(hit)}")
    if payload.get("action") not in SCROLL_ACTIONS:
        raise ValueError(f"action 只允许 {'/'.join(SCROLL_ACTIONS)}")
    binding = payload.get("binding")
    if not isinstance(binding, dict) or set(binding) != set(screen_gestures.BINDING_V3_KEYS):
        raise ValueError("payload.binding 必须为能力查询返回的完整 v3 绑定（十字段）")
    if binding.get("device_id") != task.get("device_id") or device.get("device_id") != task.get("device_id"):
        raise ValueError("任务与设备绑定不一致")
    return payload


def _canonical_sha256(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    import hashlib
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deepcopy_binding(task: dict) -> dict | None:
    binding = (task.get("payload") or {}).get("binding")
    return dict(binding) if isinstance(binding, dict) else None


def _evidence(record, task) -> dict:
    import re
    if (not isinstance(record, dict)
            or record.get("device_id") != task["device_id"]
            or record.get("task_id") != task["task_id"]
            or not isinstance(record.get("evidence_id"), str)
            or not record.get("evidence_id").strip()
            or not isinstance(record.get("captured_at"), str)
            or not record.get("captured_at").strip()
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])):
        raise ValueError("缺少本任务设备的完整截图证据")
    return {key: record[key] for key in ("evidence_id", "sha256", "captured_at")}


# ---------------------------------------------------------------------------
# FIX-08：连续光流链执行（§3.2 实施顺序 / §5 采样时间线）
# ---------------------------------------------------------------------------

class _ChainBroken(Exception):
    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


async def execute_scroll(ctx) -> dict:
    """插件 execute_step 委托：单次上下滑的固定顺序执行（§5.4）。

    只执行一次动作；幂等编号由持久动作记录保证（R2-07：prepare/claim 事务
    持久成功后才下发恰一次 arm.swipe；认领后未知不可重放，同编号只读原
    事实）。机械结论与光流效果分开表达；效果未知不重放（retryable=false）。
    一步不建子任务、不抢第二租约；内部调用方共用本函数。
    """
    task = ctx.task
    action = (task.get("payload") or {}).get("action")
    display = ACTION_DISPLAY.get(action, action)
    device_id = task["device_id"]
    action_instance_id = f"{task.get('task_id')}:{action}:1"
    interval_ms = SAMPLING_SUPPORT["interval_ms"]
    max_gap_ms = SAMPLING_SUPPORT["max_pair_gap_ms"]
    max_frames = SAMPLING_SUPPORT["max_frames"]
    max_window_ms = SAMPLING_SUPPORT["max_window_ms"]

    # R2-07：逐动作持久状态机（首次访问即启动扫描：重建阻断先于派发）
    store = get_scroll_action_store(ctx._runtime)

    detail = {
        "contract_version": CONTRACT_VERSION, "action": action,
        "action_display": display, "device_id": device_id,
        "binding": deepcopy_binding(task), "planning_version": PLANNING_VERSION,
        "mechanical_status": "not_started",
        "mechanical_status_display": "机械动作未开始",
        "effect_status": "not_started", "effect_status_display": "内容未滚动（未执行）",
        "direction_consistent": None, "dy_px": None, "dx_px": None,
        "valid_points": None, "inlier_ratio": None, "reason_code": None,
        "motion_started": False, "retryable": False,
        "before_evidence": None, "after_evidence": None,
        "frame_chain_evidence": None, "observation_roi": dict(DEFAULT_OBSERVATION_ROI),
        "manual_review_required": False,
        "action_instance_id": action_instance_id,
        "observation_version": OBSERVATION_VERSION, "flow_status": None,
        "safe_cleanup_confirmed": None, "observation": None, "pause_issue_id": None,
        # FIX-08 新增事实字段
        "motion_budget": None, "pause_scope": None,
        "timeline": {}, "sampling_diagnostics": [], "settle_tail": None,
        # R2-07 持久状态机事实
        "claim_id": None, "record_state": None, "record_persist_failed": False,
    }

    def _failed(category: str, message: str) -> dict:
        return {"outcome": "failed", "error_category": category, "message": message,
                "detail": detail}

    def _unknown(message: str, *, category: str = "result_unknown_pending") -> dict:
        return {"outcome": "unknown", "error_category": category, "message": message,
                "detail": detail}

    # ---- 同编号重放：只读原事实，不重进执行、不产生第二次动作 ----
    existing = store.find_by_task(device_id, str(task.get("task_id")))
    if existing is not None and existing.get("state") != "prepared":
        detail["record_state"] = existing["state"]
        detail["claim_id"] = existing.get("claim_id")
        detail["manual_review_required"] = \
            existing.get("manual_resume_required") is True \
            or existing["state"] == "review_required"
        if existing["state"] == "aborted_before_motion":
            return _failed("scroll_record_invalid",
                           "原动作在认领前已中断（原编号只读查询），不重放")
        outcome = existing.get("task_outcome") or "unknown"
        result_view = existing.get("result") or {}
        message = result_view.get("message") or \
            f"原动作记录（{existing['state']}）：只读返回原事实，不重放"
        shell = {"outcome": outcome, "message": message, "detail": detail}
        if outcome == "confirmed":
            shell["outcome"] = "success"
        elif outcome == "failed":
            shell["error_category"] = (result_view.get("reason_code")
                                       or "scroll_record_invalid")
        return shell

    # ---- 未恢复的视觉/未知问题阻断新动作（调度/最后认领查询未核对记录）----
    blocking = [item for item in store.issues(device_id)
                if item.get("manual_resume_required") is True
                and item.get("action_instance_id") != action_instance_id]
    if blocking:
        return _failed(
            "manual_resume_required",
            f"设备存在未恢复的滑动核对问题（{blocking[0].get('pause_issue_id')}）："
            "请先人工核对并独立恢复，期间零滑动")

    runtime = ctx._runtime
    bundle = _bundle(runtime, device_id)
    if bundle is None:
        return _failed("calibration_not_ready", "当前校准档案不可读，未执行滑动")
    from runtime.scroll_context import ScrollContext
    from runtime.phone_region_context import RegionContextError, context_pointer
    try:
        pinned_context = ScrollContext(runtime, device_id, bundle=bundle)
    except RegionContextError as exc:
        return _failed(exc.code, exc.message)
    try:
        current_binding = screen_gestures.binding_snapshot_v3(bundle)
        plan = plan_action(runtime, device_id, action)
        requested = (task.get("payload") or {}).get("binding")
        # FIX-07：v3 语义比较（十字段语义，仅诊断 revision 不比）——
        # 请求绑定与当前校准不一致即拒绝，不悄悄换绑执行。
        screen_gestures.require_binding_v3(requested, bundle)
    except ScrollCapabilityError as exc:
        return _failed("calibration_not_ready", f"无法生成{display}计划：{exc}")
    except (ValueError, TypeError) as exc:
        return _failed("binding_changed", f"{exc}")
    detail["binding"] = dict(current_binding)

    # ① 动作前预算：无法证明或超限 → 零动作（G03-A20）
    budget = motion_budget(plan)
    detail["motion_budget"] = budget
    if budget["status"] != "within_window":
        message = ("完整机械准备到安全收尾预算无法证明：零动作"
                   if budget["status"] == "unproven"
                   else f"完整机械预算 {budget['estimated_total_ms']}ms 超过本版观察窗口"
                        f" {SAMPLING_SUPPORT['observation_budget_ms']}ms：零动作，"
                        "不调快机械参数（scroll_observation_window_insufficient）")
        return _failed("scroll_observation_window_insufficient", message)

    # ① 动作前持久动作编号/快照（计划/校准绑定/采样配置冻结）——在滑动
    # 存储层经事务持久（R2-07）；准备失败明确零下发，不再吞异常继续。
    frozen_params = {**SAMPLING_SUPPORT, "roi": dict(DEFAULT_OBSERVATION_ROI)}
    request_sha256 = _canonical_sha256({
        "task_id": task.get("task_id"), "step_key": "single-scroll",
        "action": action, "binding": dict(current_binding),
        "plan": {"start": dict(plan["start"]), "end": dict(plan["end"]),
                 "durationMs": plan["durationMs"]},
        "config": frozen_params})
    config_snapshot = {
        "record_version": ACTION_RECORD_VERSION,
        "task_id": task.get("task_id"),
        "action_instance_id": action_instance_id, "device_id": device_id,
        "revision": 1, "state": "prepared",
        "origin": "external_task", "step_key": "single-scroll",
        "request_sha256": request_sha256,
        "action": action, "binding": dict(current_binding),
        "planning_version": PLANNING_VERSION,
        "observation_version": OBSERVATION_VERSION,
        "config_sha256": _canonical_sha256(frozen_params),
        "plan_snapshot": {"start": dict(plan["start"]), "end": dict(plan["end"]),
                          "durationMs": plan["durationMs"]},
        "config": frozen_params,
        "created_at": _utc_iso(), "updated_at": _utc_iso(),
        "lease_id": task.get("lease_id"), "claim_id": None, "claimed_at": None,
        "driver_invoked_at": None, "swipe_call_count": None,
        "motion_started": False, "mechanical_status": "not_started",
        "safe_cleanup_confirmed": False,
        "sample_evidence_ids": [], "timeline": {}, "sampling_diagnostics": [],
        "observation": None, "task_outcome": None, "reason_code": None,
        "pause_issue_id": None, "pause_scope": None,
        "manual_resume_required": False, "result": None, "review": None,
    }
    try:
        prepared = store.prepare(config_snapshot)
        if prepared["state"] != "prepared":
            # prepare 与上面 replay 检查之间被并发完成：按原记录只读返回
            detail["record_state"] = prepared["state"]
            return _failed("scroll_record_invalid",
                           "动作记录已离开准备态：不重放（原编号只读可查）")
    except ScrollActionStoreError as exc:
        return _failed("storage_unavailable",
                       f"滑动准备未持久化，零下发：{exc}")
    record_revision = int(prepared["revision"])
    detail["record_state"] = "prepared"

    try:
        broker = runtime.devices.camera_broker(device_id)
    except Exception:
        return _failed("camera_unavailable", "动作前相机通道不可用，未执行滑动")
    evidence_store = runtime.evidence
    _region_pointer = context_pointer(pinned_context.region)
    observation_context = {
        "roi": dict(DEFAULT_OBSERVATION_ROI), "roi_frame": "screen_norm",
        **pinned_context.archive_context(),
        "interval_ms": interval_ms, "observation_version": OBSERVATION_VERSION,
        "config_sha256": config_snapshot["config_sha256"],
    }
    timeline = detail["timeline"]
    diagnostics: list[dict] = []
    chain: list[dict] = []  # 已接纳链帧：sample_index/evidence/身份/相位

    async def _grab(sample_index: int, phase: str, label: str) -> dict:
        """有界取帧（broker 唯一物理入口）+ 归档已采到的帧（不另拍）。"""
        pinned_context.ensure_current()
        read_started = time.monotonic_ns()
        frame = await asyncio.wait_for(broker.capture(),
                                       timeout=SAMPLING_SUPPORT["capture_timeout_ms"] / 1000
                                       + 2.0)
        pinned_context.ensure_current()
        pinned_context.validate_frame(frame)
        read_ms = (time.monotonic_ns() - read_started) / 1e6
        record = await asyncio.to_thread(
            evidence_store.archive_camera_frame, frame, dict(observation_context),
            device_id=device_id, task_id=task.get("task_id"), label=label,
            action_instance_id=action_instance_id, sample_index=sample_index,
            phase=phase, camera_id=getattr(broker, "camera_id", None),
            operator=f"plugin:{ctx.plugin_id}")
        return {"record": record, "identity": frame_chain_identity(frame),
                "read_ms": read_ms}

    # ② 前帧预检与新鲜度：未认领前允许在预检预算内重取过旧前帧（F08-4）
    before_ms = SAMPLING_SUPPORT["before_max_age_ms"]
    before_budget_ms = SAMPLING_SUPPORT["before_wait_timeout_ms"]
    precheck_started = time.monotonic_ns()
    accepted_before = None
    while True:
        try:
            attempt = await _grab(0, "before", f"{display}前帧（观察链 sample 0）")
        except RegionContextError as exc:
            return _failed(exc.code, exc.message)
        except Exception:
            return _failed("camera_unavailable", "动作前画面不可用，未执行滑动")
        identity = attempt["identity"]
        if identity is None:
            return _failed("source_metadata_missing",
                           "动作前帧缺少可证明的采样身份（序号/时钟），缺来源时序不能先动再补证据")
        age_ms = (time.monotonic_ns() - identity["captured_monotonic_ns"]) / 1e6
        if age_ms <= before_ms:
            accepted_before = attempt
            break
        # 过旧前帧：淘汰尝试不进入有效链——诊断列表保留证据编号与年龄，
        # 认领前的重取不产生第二份链帧（F08-4：成功后仅认领一次）。
        diagnostics.append({"tick": 0, "result": "rejected",
                            "reason": "source_frame_stale",
                            "evidence_id": attempt["record"]["evidence_id"],
                            "age_ms": round(age_ms, 1)})
        if (time.monotonic_ns() - precheck_started) / 1e6 >= before_budget_ms:
            return _failed("source_frame_stale",
                           "动作前帧持续过期（年龄>400ms）：未认领、未执行滑动")
        continue

    assert accepted_before is not None
    # 调度锚点 = 前帧接纳时刻（物理读完成），与帧内捕获时刻分开：
    # 200ms 网格自接纳时刻起算，不按"上次返回再睡"累计漂移。
    accepted_ns = time.monotonic_ns()
    t0_ns = accepted_ns
    chain.append({"sample_index": 0, "phase": "before",
                  "evidence_id": accepted_before["record"]["evidence_id"],
                  "sha256": accepted_before["record"]["sha256"],
                  "captured_at": accepted_before["record"]["captured_at"],
                  **accepted_before["identity"]})
    detail["before_evidence"] = {
        "evidence_id": accepted_before["record"]["evidence_id"],
        "sha256": accepted_before["record"]["sha256"],
        "captured_at": accepted_before["record"]["captured_at"]}
    generation = accepted_before["identity"]["generation"]
    deadline_ns = accepted_ns + int(max_window_ms * 1e6)

    # ③ 前帧接纳即开采样协程（t0+n*200ms 调度；不重叠读、不追赶补读）
    stop_sampling = asyncio.Event()
    stop_reason: dict = {}
    tick_grace_ns = int(25 * 1e6)  # 事件循环抖动容差；超过即视为错过 tick

    async def _sampler() -> None:
        last = dict(accepted_before["identity"])
        accepted = 1
        for tick in range(1, max_frames):
            if stop_sampling.is_set():
                return
            target_ns = accepted_ns + tick * int(interval_ms * 1e6)
            now_ns = time.monotonic_ns()
            if now_ns < target_ns:
                await asyncio.sleep((target_ns - now_ns) / 1e9)
            elif now_ns > target_ns + tick_grace_ns:
                # 错过 tick（前一次读取未排空/调度迟到）：只记诊断，不发追赶读
                diagnostics.append({"tick": tick, "result": "skipped"})
                continue
            if stop_sampling.is_set() or time.monotonic_ns() >= deadline_ns:
                if time.monotonic_ns() >= deadline_ns and not stop_reason:
                    stop_reason["reason"] = "observation_timeout"
                return
            try:
                attempt = await _grab(accepted, "during",
                                      f"{display}观察帧（sample {accepted}）")
            except RegionContextError as exc:
                diagnostics.append({"tick": tick, "result": "rejected", "reason": exc.code})
                if not stop_reason:
                    stop_reason["reason"] = exc.code
                return
            except (Exception, asyncio.TimeoutError) as exc:
                diagnostics.append({"tick": tick, "result": "timeout",
                                    "reason": str(exc) or "capture_timeout"})
                if not stop_reason:
                    stop_reason["reason"] = "capture_timeout"
                return
            identity = attempt["identity"]
            if identity is None:
                diagnostics.append({"tick": tick, "result": "rejected",
                                    "reason": "source_metadata_missing",
                                    "evidence_id": attempt["record"]["evidence_id"]})
                if not stop_reason:
                    stop_reason["reason"] = "source_metadata_missing"
                return
            if identity["generation"] != generation:
                diagnostics.append({"tick": tick, "result": "rejected",
                                    "reason": "source_generation_changed",
                                    "evidence_id": attempt["record"]["evidence_id"]})
                if not stop_reason:
                    stop_reason["reason"] = "source_frame_replayed"
                return
            if identity["seq"] <= last["seq"]:
                # 同源序号重复/倒退：只记诊断、不进链、不刷新时间，仍耗预算
                diagnostics.append({"tick": tick, "result": "duplicate",
                                    "seq": identity["seq"],
                                    "evidence_id": attempt["record"]["evidence_id"]})
                continue
            gap_ms = (identity["captured_monotonic_ns"]
                      - last["captured_monotonic_ns"]) / 1e6
            uncertainty = max(identity.get("uncertainty_ms", 0),
                              last.get("uncertainty_ms", 0))
            # 相邻间隔上限：400ms 契约值 + 上一次物理读的实际时长（broker 实测、
            # 有界）。慢提供器（如仿真逐像素渲染）由此获得不多读、不补拍的宽容，
            # 采样协程的调度延迟不计入；读取之间的真实空档仍判 frame_gap_exceeded。
            prev_read_ms = float(last.get("read_ms") or 0.0)
            tolerance_ms = max_gap_ms + uncertainty + min(prev_read_ms, 1000.0)
            if gap_ms > tolerance_ms:
                diagnostics.append({"tick": tick, "result": "gap",
                                    "gap_ms": round(gap_ms, 1),
                                    "tolerance_ms": round(tolerance_ms, 1),
                                    "evidence_id": attempt["record"]["evidence_id"]})
                if not stop_reason:
                    stop_reason["reason"] = "frame_gap_exceeded"
                return
            last = dict(identity)
            chain.append({"sample_index": accepted, "phase": "during",
                          "evidence_id": attempt["record"]["evidence_id"],
                          "sha256": attempt["record"]["sha256"],
                          "captured_at": attempt["record"]["captured_at"],
                          **identity})
            accepted += 1
        if not stop_reason:
            stop_reason["reason"] = "frame_chain_limit"

    sampler_task = asyncio.create_task(_sampler())

    payload = task["payload"]
    params = {"binding": dict(current_binding),
              "start": {"source": "screen_normalized", **plan["start"]},
              "end": {"source": "screen_normalized", **plan["end"]},
              "durationMs": plan["durationMs"]}

    def _persist_record(patch: dict, *, state: str, fault_pause: bool) -> bool:
        """认领后记录收尾（结果/暂停同一事务）；失败不得伪装已保存。"""
        nonlocal record_revision
        try:
            record = store.finalize(
                str(task.get("task_id")), action_instance_id,
                expected_revision=record_revision, state=state, patch=patch)
            record_revision = int(record["revision"])
            detail["record_state"] = state
            return True
        except ScrollActionStoreError:
            detail["record_persist_failed"] = True
            detail["manual_review_required"] = True
            return False

    def _pause(issue_reason: str) -> str | None:
        """纯视觉未知持久暂停（§7.2）：结果与 pause_issue 同一事务先落库，
        再交由任务结束释放租约；写失败不得伪装已保存（pause_issue_id 置空，
        保护经 manual_review_required/unknown 结果保留）。"""
        issue_id = f"scroll-{task.get('task_id')}:{action_instance_id}"
        persisted = _persist_record({
            "task_outcome": "unknown", "reason_code": str(detail.get("reason_code")),
            "pause_issue_id": issue_id, "pause_scope": "automatic_actions",
            "manual_resume_required": True,
            "motion_started": detail["motion_started"],
            "mechanical_status": detail["mechanical_status"],
            "safe_cleanup_confirmed": detail["safe_cleanup_confirmed"],
            "result": {"outcome": "unknown", "reason_code": str(detail.get("reason_code")),
                       "pause_reason": issue_reason},
            "sample_evidence_ids": list(detail.get("frame_chain_evidence") or []),
            "observation": detail.get("observation"),
            "timeline": dict(detail.get("timeline") or {}),
            "sampling_diagnostics": list(detail.get("sampling_diagnostics") or []),
        }, state="review_required", fault_pause=True)
        if not persisted:
            detail["pause_issue_id"] = None
            detail["pause_scope"] = None
            detail["manual_review_required"] = True
            return None
        detail["pause_issue_id"] = issue_id
        detail["pause_scope"] = "automatic_actions"
        detail["manual_review_required"] = True
        return issue_id

    detail.update(mechanical_status="unknown",
                  mechanical_status_display="机械动作结果未知",
                  motion_started=None, manual_review_required=True)
    # ②→③ 最终认领：前帧年龄复核（≤400ms）→ 持久 claim 事务 → 恰一次
    # arm.swipe；认领提交失败明确零下发；认领后不得重进执行（失败保守进入
    # 不可重放对账）。
    claim_age_ms = (time.monotonic_ns()
                    - accepted_before["identity"]["captured_monotonic_ns"]) / 1e6
    context_error = None
    try:
        pinned_context.ensure_current()
    except RegionContextError as exc:
        context_error = exc
    if claim_age_ms > before_ms or context_error is not None:
        stop_sampling.set()
        await asyncio.gather(sampler_task, return_exceptions=True)
        timeline["sampling_finished_ns"] = time.monotonic_ns()
        try:
            store.finalize(str(task.get("task_id")), action_instance_id,
                           expected_revision=record_revision,
                           state="aborted_before_motion",
                           patch={"task_outcome": "failed", "reason_code": None,
                                  "result": {"outcome": "failed",
                                             "note": "认领时原帧或上下文已失效，未认领未执行"}})
        except ScrollActionStoreError:
            detail["record_persist_failed"] = True
        if context_error is not None:
            return _failed(context_error.code, context_error.message)
        return _failed("source_frame_stale",
                       f"认领时前帧已过期（{claim_age_ms:.0f}ms>400ms）：未认领、未执行滑动")

    claim_id = f"claim-{uuid.uuid4()}"
    try:
        claimed = store.claim(str(task.get("task_id")), action_instance_id,
                              expected_revision=record_revision,
                              claim_id=claim_id, claimed_at=_utc_iso())
        record_revision = int(claimed["revision"])
        detail["claim_id"] = claimed["claim_id"]
        detail["record_state"] = "claimed"
    except ScrollActionStoreError as exc:
        stop_sampling.set()
        await asyncio.gather(sampler_task, return_exceptions=True)
        timeline["sampling_finished_ns"] = time.monotonic_ns()
        detail["record_persist_failed"] = True
        return _failed("storage_unavailable",
                       f"滑动认领未持久化，零下发：{exc}")

    driver_invoked_ns = time.monotonic_ns()
    timeline["driver_invoked_ns"] = driver_invoked_ns
    try:
        with pinned_context.action(task["task_id"]):
            pinned_context.motion_guard()
            result = await ctx.call_capability("arm.swipe", params)
    except PluginViolationError as exc:
        detail.update(mechanical_status="not_started",
                      mechanical_status_display="机械动作未开始",
                      motion_started=False, manual_review_required=False)
        stop_sampling.set()
        await asyncio.gather(sampler_task, return_exceptions=True)
        timeline["sampling_finished_ns"] = time.monotonic_ns()
        # 认领后能力拒绝：保守进入不可重放对账（不重进准备态）
        _persist_record({
            "task_outcome": "unknown", "reason_code": "interrupted_after_claim",
            "motion_started": False,
            "mechanical_status": "not_started",
            "result": {"outcome": "unknown",
                       "note": f"认领后能力调用被拒绝，按不可重放对账：{exc}"}},
            state="review_required", fault_pause=False)
        return _unknown(f"能力调用前拒绝（已认领，按不可重放对账）：{exc}",
                        category="interrupted_after_claim")
    except DriverError as exc:
        if (exc.category in _PRE_MOTION_REJECTIONS
                and getattr(exc, "motion_not_started", False) is True):
            detail.update(mechanical_status="not_started",
                          mechanical_status_display="机械动作未开始",
                          motion_started=False, manual_review_required=False)
            stop_sampling.set()
            await asyncio.gather(sampler_task, return_exceptions=True)
            timeline["sampling_finished_ns"] = time.monotonic_ns()
            _persist_record({
                "task_outcome": "unknown", "reason_code": "interrupted_after_claim",
                "motion_started": False,
                "mechanical_status": "not_started",
                "result": {"outcome": "unknown",
                           "note": f"认领后动作前拒绝，按不可重放对账：{exc.message}"}},
                state="review_required", fault_pause=False)
            return _unknown(
                f"动作前安全校验拒绝（已认领，按不可重放对账，未执行滑动）：{exc.message}",
                category="interrupted_after_claim")
        result = None
    timeline["driver_returned_ns"] = time.monotonic_ns()
    # 驱动调用事实只追加进 claimed 记录（诊断；失败不伪装也不阻断安全收尾）
    try:
        progressed = store.record_progress(
            str(task.get("task_id")), action_instance_id,
            expected_revision=record_revision,
            patch={"driver_invoked_at": _utc_iso(), "swipe_call_count": 1,
                   "timeline": dict(timeline)})
        if progressed is not None:
            record_revision = int(progressed["revision"])
    except ScrollActionStoreError:
        detail["record_persist_failed"] = True

    completed = gesture_motion_confirmed("swipe", result)
    if completed:
        detail.update(mechanical_status="confirmed",
                      mechanical_status_display="机械动作已完成",
                      motion_started=True, safe_cleanup_confirmed=True)
    elif isinstance(result, dict) and result.get("ok") is True:
        # 有回执但不确定（stopped/cancelled 等）
        detail["motion_started"] = True

    # ⑤ 完成栅栏：机械完成 + 安全收尾确认后才成立；栅栏后取 3 帧 2 对静止
    completion_fence_ns = None
    if completed and detail["safe_cleanup_confirmed"]:
        completion_fence_ns = time.monotonic_ns()
        timeline["mechanical_completed_ns"] = completion_fence_ns
        timeline["safe_cleanup_confirmed_ns"] = completion_fence_ns
        timeline["completion_fence_ns"] = completion_fence_ns

    settled = False
    tail_measure = None
    if completion_fence_ns is not None:
        # R2-08：尾帧按采样时刻含误差判断——只有可证明晚于栅栏（采样时刻
        # −不确定度 > fence）的帧才能进入尾部静止判定；完成前采样、完成后
        # 才送达的帧不得当尾帧（不因接收/读取时间续鲜）。
        def _provably_after_fence(frame: dict) -> bool:
            sampled_ns = frame["captured_monotonic_ns"]
            uncertainty_ns = int(float(frame.get("uncertainty_ms") or 0.0) * 1e6)
            return sampled_ns - uncertainty_ns > completion_fence_ns

        after_budget_ms = SAMPLING_SUPPORT["after_wait_timeout_ms"]
        settle_deadline_ns = min(
            completion_fence_ns + int(after_budget_ms * 1e6), deadline_ns)
        measured_chain_len = -1
        while True:
            # 先评估已接纳帧，再检查等待期限：期限约束“继续等新帧”的时长，
            # 不剥夺对栅栏后已到帧的尾部判定。
            post = [f for f in chain if _provably_after_fence(f)]
            if len(post) >= 3:
                tail = chain[-3:]
                if all(_provably_after_fence(f) for f in tail) \
                        and len(chain) != measured_chain_len:
                    # 仅在出现新帧后重测尾部，不空转重复归并
                    measured_chain_len = len(chain)
                    try:
                        tail_measure = await ctx.call_capability("camera.measure_scroll", {
                            "op": "measure",
                            "evidence_ids": [f["evidence_id"] for f in tail],
                            "roi": dict(DEFAULT_OBSERVATION_ROI),
                            "roi_frame": "screen_norm",
                            "search_region_pointer": _region_pointer})
                    except Exception:
                        tail_measure = None
                    if isinstance(tail_measure, dict) \
                            and tail_measure.get("status") == "stationary":
                        settled = True
                        break
            if time.monotonic_ns() >= settle_deadline_ns or stop_sampling.is_set():
                break
            await asyncio.sleep(0.02)
        timeline["settle_finished_ns"] = time.monotonic_ns()
    detail["settle_tail"] = {
        "completion_fence_ns": completion_fence_ns,
        "settled": settled,
        "tail_status": (tail_measure or {}).get("status")
        if isinstance(tail_measure, dict) else None,
        "required": "完成后 3 帧、最后 2 对静止（after_wait≤1000ms，受总窗口限制）"}

    # ⑥ 停止采样并等待协程真实排空（取消后不遗留读线程给下一动作）
    stop_sampling.set()
    try:
        await asyncio.wait_for(asyncio.shield(sampler_task),
                               timeout=SAMPLING_SUPPORT["capture_timeout_ms"] / 1000 + 2.0)
    except (asyncio.TimeoutError, Exception):
        pass
    timeline["sampling_finished_ns"] = time.monotonic_ns()

    if chain:
        chain[-1]["phase"] = "after" if completion_fence_ns is not None else "during"
    detail["frame_chain_evidence"] = [f["evidence_id"] for f in chain]
    detail["frame_chain"] = [{key: f[key] for key in
                              ("sample_index", "phase", "evidence_id", "sha256",
                               "captured_at", "generation", "seq",
                               "captured_monotonic_ns")}
                             for f in chain]
    detail["sampling_diagnostics"] = diagnostics
    post_frames = ([f for f in chain
                    if f["captured_monotonic_ns"]
                    - int(float(f.get("uncertainty_ms") or 0.0) * 1e6)
                    > completion_fence_ns]
                   if completion_fence_ns is not None else [])
    if post_frames:
        last_frame = post_frames[-1]
        detail["after_evidence"] = {
            "evidence_id": last_frame["evidence_id"],
            "sha256": last_frame["sha256"],
            "captured_at": last_frame["captured_at"]}

    # 机械未完成：保留已执行事实，效果按未知处理（先测量已得帧作诊断）。
    # 机械未知走现有机械阻断/保留租约规则，不注册视觉暂停问题。
    if not completed:
        detail.update(effect_status="unknown", effect_status_display="内容效果未知")
        flow_status = None
        if len(chain) >= 2:
            try:
                measured = await ctx.call_capability("camera.measure_scroll", {
                    "op": "measure",
                    "evidence_ids": [f["evidence_id"] for f in chain],
                    "search_region_pointer": _region_pointer,
                    "roi": dict(DEFAULT_OBSERVATION_ROI), "roi_frame": "screen_norm"})
                if isinstance(measured, dict):
                    flow_status = measured.get("status")
                    detail["observation"] = measured
            except Exception:
                flow_status = None
        detail["flow_status"] = flow_status
        detail["manual_review_required"] = True
        _persist_record({
            "task_outcome": "unknown", "reason_code": "mechanical_result_unknown",
            "motion_started": detail["motion_started"],
            "mechanical_status": "unknown", "safe_cleanup_confirmed": False,
            "result": {"outcome": "unknown",
                       "reason_code": "mechanical_result_unknown"},
            "sample_evidence_ids": list(detail.get("frame_chain_evidence") or []),
            "observation": detail.get("observation"),
            "timeline": dict(timeline),
            "sampling_diagnostics": list(diagnostics)},
            state="review_required", fault_pause=False)
        return _unknown("滑动停止、不完整或机械结果未核验：保留已执行事实，禁止重试",
                        category="mechanical_result_unknown")

    # ④/⑥ 全链归并（共享纯归并函数经平台能力；已采帧直接归并，不另拍）
    if stop_reason.get("reason"):
        # 链完整性失败（断帧/换代/采样超时/缺身份）覆盖候选结论：
        # 观察作废即未知，先于「链不足两帧」的通用判定。
        reason = stop_reason["reason"]
        detail["reason_code"] = reason
        detail["effect_status"] = "unknown"
        detail["effect_status_display"] = "内容效果未知"
        _pause(reason)
        return _unknown(f"采样链不完整（{reason}）：保留机械完成事实，"
                        "内容效果未知，停止自动续滑")
    if len(chain) < 2:
        _pause("frame_chain_invalid")
        return _unknown("观察链不足两帧：内容效果未知，停止自动续滑")
    try:
        measure = await ctx.call_capability("camera.measure_scroll", {
            "op": "measure",
            "evidence_ids": [f["evidence_id"] for f in chain],
            "roi": dict(DEFAULT_OBSERVATION_ROI), "roi_frame": "screen_norm",
            "search_region_pointer": _region_pointer})
    except Exception as exc:
        _pause("observation_failed")
        return _unknown(f"内容位移测量不可用：{exc}")
    if not isinstance(measure, dict) or measure.get("status") not in {
            "displaced", "stationary", "unknown"}:
        _pause("observation_invalid")
        return _unknown("内容位移测量结果无效：效果未知，禁止重试")
    detail["observation"] = measure
    detail["effect_status"] = measure["status"]
    detail["effect_status_display"] = {"displaced": "内容已滚动", "stationary": "内容无明显位移",
                                       "unknown": "内容效果未知"}[measure["status"]]
    detail["flow_status"] = measure["status"]
    detail["observation_version"] = measure.get("gesture_rules_version")
    for key in ("dy_px", "dx_px", "valid_points", "inlier_ratio", "reason_code"):
        detail[key] = measure.get(key)
    if measure["status"] == "displaced":
        expected = ACTION_EXPECTED_DY_SIGN.get(action)
        dy = measure.get("dy_px")
        # 防御性符号判定：0 视为方向不明（不与任何预期一致）
        detail["direction_consistent"] = (
            isinstance(dy, (int, float)) and math.isfinite(dy) and dy != 0
            and expected is not None and expected != 0
            and (dy > 0) == (expected > 0))
    else:
        detail["direction_consistent"] = None

    # ⑤ 完成栅栏后的稳定尾部：无稳定尾即视觉未知（G03-A13）
    if completion_fence_ns is not None and not settled:
        _pause("after_not_settled")
        return _unknown("机械完成后观察区内未稳定（无 3 帧 2 对静止尾部）："
                        "保留机械完成事实，内容效果未知，停止自动续滑")

    # ⑦/终态映射（§6.4）：纯视觉未知持久暂停；静止=failed，不自动重试
    if detail["effect_status"] == "displaced" and detail["direction_consistent"] is not True:
        _pause("direction_mismatch")
        return _unknown("内容位移方向与本次动作不符：保留已执行事实，停止后续自动滑动")
    if detail["effect_status"] == "unknown":
        _pause(str(measure.get("reason_code") or "chain_uncertain"))
        return _unknown("内容效果无法确认（特征不足/遮挡/帧链失败）：保留已执行事实，停止自动续滑")
    if detail["effect_status"] == "stationary":
        # 机械确认完成但内容没动：不是「没做过」，也不自动重试、不宣称到顶
        detail["manual_review_required"] = False
        persisted = _persist_record({
            "task_outcome": "failed", "reason_code": "scroll_no_displacement",
            "motion_started": detail["motion_started"],
            "mechanical_status": detail["mechanical_status"],
            "safe_cleanup_confirmed": detail["safe_cleanup_confirmed"],
            "result": {"outcome": "failed",
                       "reason_code": "scroll_no_displacement"},
            "sample_evidence_ids": list(detail.get("frame_chain_evidence") or []),
            "observation": detail.get("observation"),
            "timeline": dict(timeline),
            "sampling_diagnostics": list(diagnostics)},
            state="completed", fault_pause=False)
        if not persisted:
            return _unknown("结果持久化失败：按结果未知保护，不重放",
                            category="storage_unavailable")
        return {"outcome": "failed", "error_category": "scroll_no_displacement",
                "message": "机械滑动已完成，但观察区内容无明显位移（不自动重试、不判定到顶/到底）",
                "detail": detail}
    detail["manual_review_required"] = False
    persisted = _persist_record({
        "task_outcome": "confirmed", "reason_code": None,
        "motion_started": detail["motion_started"],
        "mechanical_status": detail["mechanical_status"],
        "safe_cleanup_confirmed": detail["safe_cleanup_confirmed"],
        "result": {"outcome": "confirmed"},
        "sample_evidence_ids": list(detail.get("frame_chain_evidence") or []),
        "observation": detail.get("observation"),
        "timeline": dict(timeline),
        "sampling_diagnostics": list(diagnostics)},
        state="completed", fault_pause=False)
    if not persisted:
        return _unknown("结果持久化失败：按结果未知保护，不重放",
                        category="storage_unavailable")
    return {"outcome": "success",
            "message": f"机械{display}完成，内容已按预期方向滚动",
            "detail": detail}
