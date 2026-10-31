"""SQLite 持久化：设备、任务、租约、证据、审计、能力、插件、查看会话、告警。

- 任务和步骤游标落库，重启后按记录恢复，不能因重启重复执行动作。
- Mac mini / Agent 重启后所有租约失效（启动时清空 leases）。
- 连接加线程锁：API 事件循环与后台巡检可能来自不同线程，所有读写串行化。
- 业务查询一律走 JSON（json_extract），冗余列仅为排查便利。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    status TEXT,
    json TEXT NOT NULL
);
-- 注意：tasks.status 故意**不建索引**（2026-10-31 实测）。派发循环按
-- `status NOT IN (...)` 过滤，SQLite 对取反列表谓词不做范围查找（EXPLAIN 恒为
-- SCAN tasks）；加索引后实测反而从 0.0135 ms 劣化到 0.868 ms（维护 + 页开销），
-- 且全仓没有任何 tasks.status 的正向过滤查询能受益。收益来自"少解析 N 行 JSON"。
CREATE TABLE IF NOT EXISTS leases (
    device_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capabilities (
    capability_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plugins (
    plugin_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS view_sessions (
    view_session_id TEXT PRIMARY KEY,
    device_id TEXT,
    status TEXT,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    device_id TEXT,
    resolved INTEGER DEFAULT 0,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_dedup (
    client_request_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS callback_outbox (
    callback_id TEXT PRIMARY KEY,
    status TEXT,
    next_attempt_at TEXT,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS input_records (
    record_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_input_records_device_kind
    ON input_records (device_id, kind);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # 输入控制记录（owner/effect/guard/settings_write）要求提交持久性：
            # 这些记录承载撤销边界与动作认领顺序点，崩溃后不允许"成功但丢失"。
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.commit()

    # ---- 底层（全部加锁） ----
    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _put(self, table: str, pk_col: str, pk: str, data: dict) -> None:
        self._write(
            f"INSERT INTO {table} ({pk_col}, json) VALUES (?, ?) "
            f"ON CONFLICT({pk_col}) DO UPDATE SET json=excluded.json",
            (pk, json.dumps(data, ensure_ascii=False)),
        )

    def _get(self, table: str, pk_col: str, pk: str) -> dict | None:
        row = self._query_one(f"SELECT json FROM {table} WHERE {pk_col}=?", (pk,))
        return json.loads(row["json"]) if row else None

    def _all(self, table: str, order_by: str = "") -> list[dict]:
        sql = f"SELECT json FROM {table}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        return [json.loads(r["json"]) for r in self._query_all(sql)]

    def _delete(self, table: str, pk_col: str, pk: str) -> None:
        self._write(f"DELETE FROM {table} WHERE {pk_col}=?", (pk,))

    # ---- 设备 ----
    def save_device(self, device: dict) -> None:
        self._put("devices", "device_id", device["device_id"], device)

    def get_device(self, device_id: str) -> dict | None:
        return self._get("devices", "device_id", device_id)

    def all_devices(self) -> list[dict]:
        return self._all("devices", "device_id")

    # ---- 任务 ----
    def create_task(self, task: dict) -> tuple[dict, bool]:
        """原子创建；任务编号与幂等键均不可覆盖，重复请求返回原任务。"""
        from runtime.errors import ApiError

        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT json FROM tasks WHERE idempotency_key=?", (task["idempotency_key"],)
            ).fetchone()
            if row:
                existing = json.loads(row["json"])
                if existing.get("payload_hash") != task.get("payload_hash"):
                    raise ApiError("idempotency_conflict", "幂等键已绑定不同请求内容")
                return existing, False
            row = self._conn.execute(
                "SELECT task_id FROM tasks WHERE task_id=?", (task["task_id"],)
            ).fetchone()
            if row:
                raise ApiError("task_id_conflict", "任务编号已存在，禁止覆盖原任务")
            self._conn.execute(
                "INSERT INTO tasks (task_id, idempotency_key, status, json) VALUES (?, ?, ?, ?)",
                (task["task_id"], task["idempotency_key"], task["status"],
                 json.dumps(task, ensure_ascii=False)),
            )
            return task, True

    def save_task(self, task: dict) -> None:
        self._write(
            "INSERT INTO tasks (task_id, idempotency_key, status, json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET idempotency_key=excluded.idempotency_key, "
            "status=excluded.status, json=excluded.json",
            (
                task["task_id"],
                task.get("idempotency_key"),
                task.get("status"),
                json.dumps(task, ensure_ascii=False),
            ),
        )

    def get_task(self, task_id: str) -> dict | None:
        return self._get("tasks", "task_id", task_id)

    def find_task_by_idempotency_key(self, key: str) -> dict | None:
        row = self._query_one("SELECT json FROM tasks WHERE idempotency_key=?", (key,))
        return json.loads(row["json"]) if row else None

    def all_tasks(self) -> list[dict]:
        return self._all("tasks")

    def active_tasks(self) -> list[dict]:
        """只返回**非终态**任务（派发循环专用）。

        2026-10-31 空转修复：`_dispatch_loop` 每 0.05 秒调一次 `all_tasks()`，
        而 `_all()` 是 `SELECT json FROM tasks` + 逐行 `json.loads`。现场 95 个
        任务全部是终态时，单次 13.78 ms × 20 Hz ≈ 单核 28%，机器持续发热，
        却没有任何可派发的工作。

        `_dispatch_once` 只消费 `awaiting_device`（孤儿对账）与 `queued`（派发），
        两者都是非终态，因此这里按 `TERMINAL_STATUSES` 反向过滤即可：
        语义不变、派发延迟不变，空闲时扫描行数由 N 降为 0。

        status 为 NULL/空时 `NULL NOT IN (...)` 结果为 NULL（WHERE 视为不满足），
        会把历史/异常行**静默漏掉**——宁可多扫一行，也不能漏派发。
        """
        from .executor import TERMINAL_STATUSES

        terminal = sorted(TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        rows = self._query_all(
            f"SELECT json FROM tasks WHERE status IS NULL OR status = '' "
            f"OR status NOT IN ({placeholders})", tuple(terminal))
        return [json.loads(row["json"]) for row in rows]

    def delete_task(self, task_id: str) -> None:
        self._delete("tasks", "task_id", task_id)

    # ---- 租约 ----
    def save_lease(self, lease: dict) -> None:
        self._put("leases", "device_id", lease["device_id"], lease)

    def get_lease(self, device_id: str) -> dict | None:
        return self._get("leases", "device_id", device_id)

    def all_leases(self) -> list[dict]:
        return self._all("leases")

    def clear_leases(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS n FROM leases")
        n = row["n"] if row else 0
        self._write("DELETE FROM leases")
        return int(n)

    # ---- 证据 ----
    def save_evidence(self, evidence: dict) -> None:
        self._put("evidence", "evidence_id", evidence["evidence_id"], evidence)

    def get_evidence(self, evidence_id: str) -> dict | None:
        return self._get("evidence", "evidence_id", evidence_id)

    def evidence_for_task(self, task_id: str) -> list[dict]:
        rows = self._query_all(
            "SELECT json FROM evidence WHERE json_extract(json, '$.task_id')=? ORDER BY evidence_id",
            (task_id,),
        )
        return [json.loads(r["json"]) for r in rows]

    # ---- 审计 ----
    def append_audit(self, event: dict) -> int:
        cur = self._write(
            "INSERT INTO audit_events (occurred_at, json) VALUES (?, ?)",
            (event["occurred_at"], json.dumps(event, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def audit_events(self, limit: int = 200, device_id: str | None = None) -> list[dict]:
        sql = "SELECT json FROM audit_events"
        params: tuple = ()
        if device_id:
            sql += " WHERE json_extract(json, '$.device_id')=?"
            params = (device_id,)
        sql += " ORDER BY seq DESC LIMIT ?"
        params = params + (limit,)
        return [json.loads(r["json"]) for r in self._query_all(sql, params)]

    # ---- 能力 ----
    def save_capability(self, capability: dict) -> None:
        self._put("capabilities", "capability_id", capability["capability_id"], capability)

    def get_capability(self, capability_id: str) -> dict | None:
        return self._get("capabilities", "capability_id", capability_id)

    def all_capabilities(self) -> list[dict]:
        return self._all("capabilities", "capability_id")

    # ---- 插件 ----
    def save_plugin(self, plugin: dict) -> None:
        self._put("plugins", "plugin_id", plugin["plugin_id"], plugin)

    def get_plugin(self, plugin_id: str) -> dict | None:
        return self._get("plugins", "plugin_id", plugin_id)

    def all_plugins(self) -> list[dict]:
        return self._all("plugins", "plugin_id")

    # ---- 查看会话 ----
    def save_view_session(self, session: dict) -> None:
        self._write(
            "INSERT INTO view_sessions (view_session_id, device_id, status, json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(view_session_id) DO UPDATE SET device_id=excluded.device_id, "
            "status=excluded.status, json=excluded.json",
            (
                session["view_session_id"],
                session.get("device_id"),
                session.get("status"),
                json.dumps(session, ensure_ascii=False),
            ),
        )

    def get_view_session(self, view_session_id: str) -> dict | None:
        return self._get("view_sessions", "view_session_id", view_session_id)

    def all_view_sessions(self) -> list[dict]:
        return self._all("view_sessions")

    # ---- 回调持久队列（§10 回调可靠性） ----
    def save_callback(self, item: dict) -> None:
        self._write(
            "INSERT INTO callback_outbox (callback_id, status, next_attempt_at, json) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(callback_id) DO UPDATE SET status=excluded.status, "
            "next_attempt_at=excluded.next_attempt_at, json=excluded.json",
            (item["callback_id"], item.get("status"), item.get("next_attempt_at"),
             json.dumps(item, ensure_ascii=False)),
        )

    def get_callback(self, callback_id: str) -> dict | None:
        return self._get("callback_outbox", "callback_id", callback_id)

    def all_callbacks(self) -> list[dict]:
        return self._all("callback_outbox", "callback_id")

    def callbacks_due(self, now_iso: str) -> list[dict]:
        rows = self._query_all(
            "SELECT json FROM callback_outbox WHERE status IN ('pending','retry') "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY callback_id",
            (now_iso,),
        )
        return [json.loads(r["json"]) for r in rows]

    def delete_view_session(self, view_session_id: str) -> None:
        self._delete("view_sessions", "view_session_id", view_session_id)

    # ---- 告警 ----
    def save_alert(self, alert: dict) -> None:
        self._write(
            "INSERT INTO alerts (alert_id, device_id, resolved, json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(alert_id) DO UPDATE SET device_id=excluded.device_id, "
            "resolved=excluded.resolved, json=excluded.json",
            (
                alert["alert_id"],
                alert.get("device_id"),
                1 if alert.get("resolved") else 0,
                json.dumps(alert, ensure_ascii=False),
            ),
        )

    def get_alert(self, alert_id: str) -> dict | None:
        return self._get("alerts", "alert_id", alert_id)

    def all_alerts(self, include_resolved: bool = True) -> list[dict]:
        rows = self._all("alerts")
        if not include_resolved:
            rows = [a for a in rows if not a.get("resolved")]
        return sorted(rows, key=lambda a: a.get("created_at", ""), reverse=True)

    # ---- KV ----
    def kv_set(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def kv_get(self, key: str) -> str | None:
        row = self._query_one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    # ---- 运维操作幂等 ----
    def save_op_result(self, client_request_id: str, payload: dict) -> None:
        self._write(
            "INSERT OR IGNORE INTO ops_dedup (client_request_id, json) VALUES (?, ?)",
            (client_request_id, json.dumps(payload, ensure_ascii=False)),
        )

    def get_op_result(self, client_request_id: str) -> dict | None:
        row = self._query_one("SELECT json FROM ops_dedup WHERE client_request_id=?", (client_request_id,))
        return json.loads(row["json"]) if row else None

    # ---- 输入控制记录（§4.8.2 input_records；受限事务，不覆写任务历史） ----
    # 这些方法不调用 _put/_write 之外的通用写路径；记录一旦写入，任务旧副本
    # （save_task 整份覆盖）永远不能改写它们——记录只经本节 CAS 方法演化。

    def input_record_get(self, record_id: str) -> dict | None:
        row = self._query_one(
            "SELECT json FROM input_records WHERE record_id=?", (record_id,))
        return json.loads(row["json"]) if row else None

    def input_records_by_device(self, device_id: str, kind: str | None = None) -> list[dict]:
        if kind is None:
            rows = self._query_all(
                "SELECT json FROM input_records WHERE device_id=? ORDER BY record_id",
                (device_id,))
        else:
            rows = self._query_all(
                "SELECT json FROM input_records WHERE device_id=? AND kind=? ORDER BY record_id",
                (device_id, kind))
        return [json.loads(r["json"]) for r in rows]

    def input_record_insert(self, record: dict) -> None:
        """首次写入（revision≥1）；主键已存在即抛（不覆盖既有记录）。"""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO input_records (record_id, device_id, kind, revision, json) "
                "VALUES (?, ?, ?, ?, ?)",
                (record["record_id"], record["device_id"], record["kind"],
                 record["revision"], json.dumps(record, ensure_ascii=False)),
            )

    def input_record_cas(self, record_id: str, expected_revision: int, mutate) -> dict:
        """条件演化：期望 revision 不符抛 InputRecordConflict；mutate 内不得换 kind/设备。"""
        from runtime.input_control import InputRecordConflict

        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT revision, json FROM input_records WHERE record_id=?",
                (record_id,)).fetchone()
            if row is None:
                raise KeyError(record_id)
            record = json.loads(row["json"])
            if record["revision"] != expected_revision:
                raise InputRecordConflict(
                    record_id, expected_revision, record["revision"])
            mutate(record)
            record["revision"] = expected_revision + 1
            record["updated_at"] = _utc_now_iso()
            self._conn.execute(
                "UPDATE input_records SET revision=?, json=? WHERE record_id=?",
                (record["revision"], json.dumps(record, ensure_ascii=False), record_id),
            )
            return record

    def input_record_apply_settings(
            self, *, write_record_id: str, write_expected_revision: int,
            original_response: dict | None,
            guard_record_id: str, guard_expected_revision: int,
            checkpoint_revision: int, checkpoint_digest: str) -> tuple[dict, dict]:
        """R2-02（§4.8.3-6）：settings_write→applied 与 guard.settings_checkpoint
        必须同一事务提交——两者间崩溃不得留下 applied-无-checkpoint 半状态。
        两个期望 revision 任一不符即整体回滚。"""
        from runtime.input_control import InputRecordConflict

        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            write_row = self._conn.execute(
                "SELECT revision, json FROM input_records WHERE record_id=?",
                (write_record_id,)).fetchone()
            guard_row = self._conn.execute(
                "SELECT revision, json FROM input_records WHERE record_id=?",
                (guard_record_id,)).fetchone()
            if write_row is None or guard_row is None:
                raise KeyError(write_record_id if write_row is None else guard_record_id)
            write_record = json.loads(write_row["json"])
            guard = json.loads(guard_row["json"])
            if write_record["revision"] != write_expected_revision:
                raise InputRecordConflict(
                    write_record_id, write_expected_revision, write_record["revision"])
            if guard["revision"] != guard_expected_revision:
                raise InputRecordConflict(
                    guard_record_id, guard_expected_revision, guard["revision"])
            if write_record.get("state") != "prepared":
                raise InputRecordConflict(
                    write_record_id, write_expected_revision, write_record["revision"])
            write_record["state"] = "applied"
            if original_response is not None:
                write_record["original_response"] = json.loads(json.dumps(
                    original_response, ensure_ascii=False))
            write_record["revision"] = write_expected_revision + 1
            write_record["updated_at"] = _utc_now_iso()
            guard["settings_checkpoint"] = {"revision": checkpoint_revision,
                                            "digest": checkpoint_digest}
            guard["revision"] = guard_expected_revision + 1
            guard["updated_at"] = _utc_now_iso()
            self._conn.execute(
                "UPDATE input_records SET revision=?, json=? WHERE record_id=?",
                (write_record["revision"], json.dumps(write_record, ensure_ascii=False),
                 write_record_id))
            self._conn.execute(
                "UPDATE input_records SET revision=?, json=? WHERE record_id=?",
                (guard["revision"], json.dumps(guard, ensure_ascii=False),
                 guard_record_id))
            return write_record, guard

    def input_record_admit_owner(self, owner: dict) -> tuple[dict, bool]:
        """单事务接纳 owner（主键冲突返回既有记录=幂等重放；身份不同抛冲突）。"""
        from runtime.input_control import InputRecordConflict

        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT json FROM input_records WHERE record_id=?",
                (owner["record_id"],)).fetchone()
            if row is not None:
                existing = json.loads(row["json"])
                if existing.get("payload_digest") != owner.get("payload_digest") \
                        or existing.get("device_id") != owner.get("device_id"):
                    raise InputRecordConflict(
                        owner["record_id"], owner["revision"], existing["revision"])
                return existing, False
            self._conn.execute(
                "INSERT INTO input_records (record_id, device_id, kind, revision, json) "
                "VALUES (?, ?, ?, ?, ?)",
                (owner["record_id"], owner["device_id"], owner["kind"],
                 owner["revision"], json.dumps(owner, ensure_ascii=False)),
            )
            return owner, True

    def input_record_admit_owner_with_task(self, owner: dict, task: dict) -> tuple[dict, dict, bool]:
        """P03：同一事务内创建 Fleet 任务 + owner，防止只有任务没有许可记录。

        任务按 create_task 的幂等规则（编号/幂等键不可覆盖）；owner 主键冲突
        按身份比对返回原记录。返回 (owner, task, created)。
        """
        from runtime.errors import ApiError
        from runtime.input_control import InputRecordConflict

        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            owner_row = self._conn.execute(
                "SELECT json FROM input_records WHERE record_id=?",
                (owner["record_id"],)).fetchone()
            if owner_row is not None:
                existing_owner = json.loads(owner_row["json"])
                if existing_owner.get("payload_digest") != owner.get("payload_digest"):
                    raise InputRecordConflict(
                        owner["record_id"], owner["revision"],
                        existing_owner["revision"])
            task_row = self._conn.execute(
                "SELECT json FROM tasks WHERE idempotency_key=?",
                (task["idempotency_key"],)).fetchone()
            if task_row is not None:
                existing = json.loads(task_row["json"])
                if existing.get("payload_hash") != task.get("payload_hash"):
                    raise ApiError("idempotency_conflict", "幂等键已绑定不同请求内容")
                if owner_row is not None:
                    return existing_owner, existing, False
                raise InputRecordConflict(
                    owner["record_id"], owner["revision"], -1)
            task_id_row = self._conn.execute(
                "SELECT task_id FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
            if task_id_row is not None:
                raise ApiError("task_id_conflict", "任务编号已存在，禁止覆盖原任务")
            self._conn.execute(
                "INSERT INTO tasks (task_id, idempotency_key, status, json) VALUES (?, ?, ?, ?)",
                (task["task_id"], task["idempotency_key"], task["status"],
                 json.dumps(task, ensure_ascii=False)),
            )
            if owner_row is None:
                self._conn.execute(
                    "INSERT INTO input_records (record_id, device_id, kind, revision, json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (owner["record_id"], owner["device_id"], owner["kind"],
                     owner["revision"], json.dumps(owner, ensure_ascii=False)),
                )
            return owner, task, True


def _utc_now_iso() -> str:
    from runtime.util import iso, utc_now
    return iso(utc_now())
