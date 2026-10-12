"""L04/§7.1/§9：布局持久状态库（typed-state 六列表 + 幂等台账 + 对账）。

R2-19（2026-10-04 第二轮）：布局发布可持久幂等恢复。
- 唯一新增状态表 `layout_typed_state`：DDL 与 docs/specs/layout-reuse/typed-state.sql
  逐列一致（kind 15 类闭集、state_key=64 位小写 sha256、归属两列、revision 1..2^53、
  canonical JSON 文本、主键 (kind,state_key)、归属索引）。
- state_key = sha256(canonical([kind, ...身份分量]))；canonical = 排序键紧凑 JSON。
- CAS 固定 SQL：INSERT revision=1（expected=0）；UPDATE `revision=revision+1` 且
  影响行数恰为 1，否则整笔事务回滚 409 layout_revision_conflict。
- 一次消费类：consumed_by=null,revision=1 → 仅允许变为
  consumed_by=原提交client_request_id,revision=2，J 其他字段全部不变。
- 操作台账 `layout_review_records`（kind 仅 operation/issue/review）+
  `layout_operation_requests`（client_request_id → operation_id）+
  intent（typed kind=intent）。发布按 §9 顺序：prepared 意图 → 文件 → 指针 →
  最终 Store 事务（typed mutations + 消费记录 + committed 原响应）。
- 启动对账按 §9 崩溃矩阵落 aborted/committed/unknown；unknown 阻断新写。
- 旧自由 KV 只经离线备份/校验迁移：逐记录源摘要+键映射；缺身份/来源、旧自报
  verified、半提交、未知键原样归档待复核；可转换旧定义只生成草稿，不继承执行资格。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from runtime.util import iso

STATE_KINDS = (
    "draft", "session", "capture", "evidence", "region_draft",
    "region_preview", "adaptation_preview", "rollback_preview",
    "package_import_preview", "package_export", "cursor",
    "review_revision", "adaptation_asset", "catalog", "intent",
)

# records:mutation.table 逻辑标签 → typed kind（不拼 SQL 表名）
TABLE_TO_KIND = {
    "drafts": "draft", "sessions": "session", "captures": "capture",
    "catalog": "catalog", "exports": "package_export",
    "region_drafts": "region_draft", "review_revisions": "review_revision",
    "adaptation_assets": "adaptation_asset",
}

MAX_REVISION = 9007199254740991

# 与 docs/specs/layout-reuse/typed-state.sql 逐字一致（不含注释）。
# 运行时建表用幂等变体（IF NOT EXISTS）；列/约束逐字相同（冻结校验见测试）。
TYPED_STATE_DDL = """
CREATE TABLE layout_typed_state (
    kind TEXT NOT NULL CHECK (kind IN (
        'draft', 'session', 'capture', 'evidence', 'region_draft',
        'region_preview', 'adaptation_preview', 'rollback_preview',
        'package_import_preview', 'package_export', 'cursor',
        'review_revision', 'adaptation_asset', 'catalog', 'intent'
    )),
    state_key TEXT NOT NULL CHECK (
        length(state_key) = 64 AND state_key NOT GLOB '*[^0-9a-f]*'
    ),
    device_id TEXT,
    principal_id TEXT,
    revision INTEGER NOT NULL CHECK (
        typeof(revision) = 'integer' AND revision BETWEEN 1 AND 9007199254740991
    ),
    json TEXT NOT NULL CHECK (typeof(json) = 'text' AND length(json) > 0),
    PRIMARY KEY (kind, state_key)
);
CREATE INDEX layout_typed_state_owner
    ON layout_typed_state (device_id, principal_id, kind);
"""

TYPED_STATE_DDL_IF_NOT_EXISTS = TYPED_STATE_DDL.replace(
    "CREATE TABLE layout_typed_state",
    "CREATE TABLE IF NOT EXISTS layout_typed_state").replace(
    "CREATE INDEX layout_typed_state_owner",
    "CREATE INDEX IF NOT EXISTS layout_typed_state_owner")

_REVIEW_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS layout_review_records (
    record_id TEXT NOT NULL,
    device_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('operation', 'issue', 'review')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    json TEXT NOT NULL CHECK (length(json) > 0),
    PRIMARY KEY (kind, record_id)
);
"""

_OPERATION_DDL = """
CREATE TABLE IF NOT EXISTS layout_operation_requests (
    request_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS layout_adaptation_pointer (
    device_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    selected_version INTEGER,
    selected_sha256 TEXT,
    updated_at TEXT NOT NULL,
    operation_id TEXT NOT NULL
);
"""

_MIGRATION_DDL = """
CREATE TABLE IF NOT EXISTS layout_kv_migration (
    source_key TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    disposition TEXT NOT NULL,
    target_kind TEXT,
    target_state_key TEXT,
    reason TEXT NOT NULL,
    migrated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS layout_kv_migration_archive (
    source_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    source_json TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (source_key, attempt)
);
"""


class LayoutStateError(Exception):
    """携带契约错误码的布局存储错误（layout_* 前缀闭集）。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def canonical_json(value) -> str:
    """附件 canonical()：排序键、UTF-8、紧凑分隔、不转义 Unicode、禁非有限值。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def content_sha(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def state_key(kind: str, parts: list) -> str:
    """state_key = sha256(canonical([kind, ...身份分量]))，version 保留整数。"""
    if kind not in STATE_KINDS:
        raise LayoutStateError(422, "layout_request_invalid",
                               f"未知状态类型 {kind}")
    return hashlib.sha256(
        canonical_json([kind, *parts]).encode("utf-8")).hexdigest()


def state_identity(kind: str, value: dict) -> list:
    """§7.1 身份分量（不含前缀 kind），均相对行内 J。"""
    if kind == "draft":
        return [value["draft_id"]]
    if kind == "session":
        return [value["capture_session_id"]]
    if kind == "capture":
        return [value["capture_id"]]
    if kind == "evidence":
        return [value["capture_id"], value["evidence_id"]]
    if kind == "region_draft":
        return [value["device_id"], value["ref"]["version"]]
    if kind in ("region_preview", "rollback_preview",
                "package_import_preview", "adaptation_preview"):
        return [value["preview_id"]]
    if kind == "package_export":
        return [value["package_id"]]
    if kind == "cursor":
        return [value["cursor_id"]]
    if kind == "review_revision":
        return [value["device_id"]]
    if kind == "adaptation_asset":
        return [value["owner_ref"]["id"], value["owner_ref"]["version"],
                value["record"]["asset"]["id"]]
    if kind == "catalog":
        ref = value["resource"]["ref"]
        return [value["resource"]["kind"], ref["id"], ref["version"]]
    if kind == "intent":
        return [value["operation_id"]]
    raise LayoutStateError(422, "layout_request_invalid",
                           f"类型 {kind} 未定义身份分量")


def state_ownership(kind: str, value: dict) -> tuple:
    """§7.1 归属列映射；返回 (device_id, principal_id)。"""
    if kind == "catalog":
        return (None, None) if value["resource"]["kind"] == "template" \
            else (value["resource"]["ref"]["id"], None)
    if kind == "review_revision":
        return (value["device_id"], None)
    if kind == "adaptation_asset":
        return (value["owner_ref"]["id"], None)
    if kind == "intent":
        return (value.get("device_id"), value.get("principal_id"))
    if kind == "package_import_preview":
        return (None, value["principal_id"])
    if kind == "package_export":
        return (None, value["principal_id"])
    return (value.get("device_id"), value.get("principal_id"))


class LayoutStateStore:
    """同一 SQLite 布局库上的 typed 状态、操作台账、适配指针与版本文件。"""

    def __init__(self, db_path, data_root=None, clock=None):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.data_root = Path(data_root) if data_root else db_path.parent
        self._staging = self.data_root / "layout_staging"
        self._staging.mkdir(parents=True, exist_ok=True)
        self._fault_points: set[str] = set()
        self._blocked_reasons: list[str] = []
        with self._lock:
            self._conn.executescript("BEGIN IMMEDIATE;"
                                     + TYPED_STATE_DDL_IF_NOT_EXISTS
                                     + _REVIEW_RECORDS_DDL + _OPERATION_DDL
                                     + _MIGRATION_DDL + "COMMIT;")

    # ---------------- 基础 ----------------
    def _now(self) -> str:
        return iso(self._clock())

    @contextlib.contextmanager
    def transaction(self):
        """真实写事务（BEGIN IMMEDIATE）；异常整笔回滚。
        同线程可重入：嵌套调用并入最外层事务（任一层异常整体回滚）。"""
        with self._lock:
            if getattr(self, "_tx_depth", 0) > 0:
                self._tx_depth += 1
                try:
                    yield self._conn
                except BaseException:
                    self._tx_failed = True
                    raise
                finally:
                    self._tx_depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            self._tx_failed = False
            try:
                yield self._conn
            except BaseException:
                self._tx_failed = True
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._tx_depth = 0
            self._conn.execute("COMMIT")

    def _fault(self, point: str) -> None:
        """测试故障注入点：命中即在持久边界抛错（不替换任何正常逻辑）。"""
        if point in self._fault_points:
            raise LayoutStateError(503, "layout_storage_failed",
                                   f"故障注入点 {point}")

    def arm_faults(self, points) -> None:
        self._fault_points = set(points)

    @property
    def blocked_reasons(self) -> list[str]:
        return list(self._blocked_reasons)

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------------- typed 状态行 ----------------
    def state_insert(self, kind: str, key: str, device_id, principal_id,
                     value: dict) -> str:
        """INSERT revision=1（expected=0）。同键同完整内容复用原行；
        异内容 409 layout_request_conflict（不覆盖、不加 revision 掩盖）。"""
        payload = canonical_json(value)
        try:
            with self.transaction() as conn:
                conn.execute(
                    "INSERT INTO layout_typed_state "
                    "(kind, state_key, device_id, principal_id, revision, json) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (kind, key, device_id, principal_id, payload))
            return "committed"
        except sqlite3.IntegrityError:
            row = self.state_row(kind, key)
            if row is not None and row["json_text"] == payload:
                return "reused"
            raise LayoutStateError(
                409, "layout_request_conflict",
                f"{kind} 状态行 {key[:12]}… 已存在且内容不同；"
                "不可变记录不可覆盖，请使用新版本/新编号")

    def state_update(self, kind: str, key: str, device_id, principal_id,
                     expected_revision: int, value: dict) -> int:
        """固定 CAS：UPDATE revision=revision+1；影响行数恰为 1，否则回滚 409。"""
        if not isinstance(expected_revision, int) or expected_revision < 0 \
                or expected_revision >= MAX_REVISION:
            raise LayoutStateError(422, "layout_request_invalid",
                                   "expected_revision 必须是 0..2^53-1 整数")
        payload = canonical_json(value)
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE layout_typed_state SET revision = revision + 1, json = ? "
                "WHERE kind = ? AND state_key = ? "
                "AND device_id IS ? AND principal_id IS ? AND revision = ?",
                (payload, kind, key, device_id, principal_id,
                 expected_revision))
            if cur.rowcount != 1:
                raise LayoutStateError(
                    409, "layout_revision_conflict",
                    f"{kind} 状态行 CAS 冲突（期望 revision={expected_revision}）")
            self._fault("state_update")
            return expected_revision + 1

    def state_consume(self, kind: str, key: str,
                      client_request_id: str) -> str:
        """一次消费：consumed_by=null,revision=1 → consumed_by,revision=2；
        J 其他字段全部不变；同编号重放不重复执行（"already"）。"""
        row = self.state_row(kind, key)
        if row is None:
            raise LayoutStateError(404, "layout_resource_missing",
                                   f"{kind} 状态行不存在")
        value = json.loads(row["json_text"])
        if value.get("consumed_by") == client_request_id \
                and row["revision"] == 2:
            return "already"
        if row["revision"] != 1 or value.get("consumed_by") is not None:
            raise LayoutStateError(
                409, "layout_request_conflict",
                f"{kind} 只能消费一次（已由其他请求消费）")
        value = {**value, "consumed_by": client_request_id}
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE layout_typed_state SET revision = 2, json = ? "
                "WHERE kind = ? AND state_key = ? AND revision = 1",
                (canonical_json(value), kind, key))
            if cur.rowcount != 1:
                raise LayoutStateError(409, "layout_revision_conflict",
                                       f"{kind} 消费 CAS 冲突")
        return "consumed"

    def state_row(self, kind: str, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, state_key, device_id, principal_id, revision, json "
                "FROM layout_typed_state WHERE kind=? AND state_key=?",
                (kind, key)).fetchone()
        if row is None:
            return None
        return {"kind": row["kind"], "state_key": row["state_key"],
                "device_id": row["device_id"],
                "principal_id": row["principal_id"],
                "revision": int(row["revision"]),
                "json_text": row["json"], "value": json.loads(row["json"])}

    def state_get(self, kind: str, key: str) -> dict | None:
        row = self.state_row(kind, key)
        return row["value"] if row else None

    def state_scan(self, kind: str, device_id: str | None = None) -> list[dict]:
        sql = ("SELECT kind, state_key, device_id, principal_id, revision, json "
               "FROM layout_typed_state WHERE kind=?")
        params: tuple = (kind,)
        if device_id is not None:
            sql += " AND device_id=?"
            params = (kind, device_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY state_key",
                                      params).fetchall()
        return [{"kind": r["kind"], "state_key": r["state_key"],
                 "device_id": r["device_id"],
                 "principal_id": r["principal_id"],
                 "revision": int(r["revision"]),
                 "json_text": r["json"], "value": json.loads(r["json"])}
                for r in rows]

    def apply_mutation(self, mutation: dict) -> None:
        """应用 intent 的 typed mutation（table 为逻辑标签，不拼 SQL 表名）。
        expected_revision=0 表示新行（INSERT），否则固定 CAS UPDATE。"""
        table = mutation["table"]
        kind = TABLE_TO_KIND.get(table)
        if kind is None:
            raise LayoutStateError(422, "layout_request_invalid",
                                   f"未知 mutation 表标签 {table}")
        value = mutation["value"]
        device_id, principal_id = state_ownership(kind, value)
        key = state_key(kind, state_identity(kind, value))
        expected = int(mutation.get("expected_revision") or 0)
        existing = self.state_row(kind, key)
        payload = canonical_json(value)
        if existing is None and expected == 0:
            self.state_insert(kind, key, device_id, principal_id, value)
        elif existing is not None \
                and existing["json_text"] == payload:
            return  # 同内容重放：幂等复用原行（不推进 revision）
        else:
            self.state_update(kind, key, device_id, principal_id, expected,
                              value)

    # ---------------- 资料问题（layout_review_records kind=issue） ----------------
    def issue_save(self, issue: dict, *, insert: bool) -> None:
        """问题行写入 layout_review_records（kind 仅 operation/issue/review；
        record_id=issue_id，revision=问题自身 revision）。"""
        with self.transaction() as conn:
            if insert:
                conn.execute(
                    "INSERT INTO layout_review_records "
                    "(record_id, device_id, kind, revision, json) "
                    "VALUES (?, ?, 'issue', ?, ?)",
                    (issue["issue_id"], issue["device_id"],
                     int(issue.get("revision") or 1), canonical_json(issue)))
            else:
                cur = conn.execute(
                    "UPDATE layout_review_records SET revision=?, json=? "
                    "WHERE record_id=? AND kind='issue'",
                    (int(issue["revision"]), canonical_json(issue),
                     issue["issue_id"]))
                if cur.rowcount != 1:
                    raise LayoutStateError(
                        409, "layout_revision_conflict",
                        f"问题 {issue['issue_id']} 不存在或已被并发修改")

    def issue_get(self, issue_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM layout_review_records "
                "WHERE record_id=? AND kind='issue'", (issue_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def issue_scan(self, device_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT json FROM layout_review_records "
                "WHERE kind='issue' AND device_id=? ORDER BY json",
                (device_id,)).fetchall()
        return [json.loads(r["json"]) for r in rows]

    def review_record_save(self, review_id: str, device_id: str | None,
                           payload: dict) -> None:
        """不可变 review 记录（layout_review_records kind=review）。"""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO layout_review_records "
                "(record_id, device_id, kind, revision, json) "
                "VALUES (?, ?, 'review', 1, ?)",
                (review_id, device_id, canonical_json(payload)))

    def review_record_get(self, review_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM layout_review_records "
                "WHERE record_id=? AND kind='review'", (review_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    # ---------------- 操作台账（layout_review_records kind=operation） ----------------
    def _op_key_request_hash(self, request: dict) -> str:
        return content_sha(request)

    def operation_prepare(self, *, principal_id: str, device_id: str | None,
                          resource_kind: str, resource_id: str,
                          client_request_id: str, request: dict,
                          planned_response: dict, files: list[dict],
                          pointer_before: dict | None,
                          pointer_after: dict | None,
                          mutations: list[dict],
                          version_ref: dict | None,
                          operation_id: str | None = None,
                          consume: list[dict] | None = None) -> dict:
        """§9 第 2 步：同一 Store 事务持久 operation.phase=prepared 与
        不可变 intent（完整 request/hash、planned_response、文件清单、
        指针 before/after、typed mutations、消费计划；版本/时间/响应已确定）。"""
        now = self._now()
        operation_id = operation_id or str(uuid.uuid4())
        operation = {
            "operation_id": operation_id,
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "device_id": device_id,
            "principal_id": principal_id,
            "request_id": client_request_id,
            "request_hash": content_sha(request),
            "before_revision": (pointer_before or {}).get("revision", 0)
            if isinstance(pointer_before, dict) else 0,
            "after_revision": None,
            "phase": "prepared",
            "version_ref": version_ref,
            "response": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        intent = {
            "operation_id": operation_id,
            "device_id": device_id,
            "principal_id": principal_id,
            "request": request,
            "request_hash": operation["request_hash"],
            "planned_response": planned_response,
            "files": files,
            "pointer_before": pointer_before,
            "pointer_after": pointer_after,
            "mutations": mutations,
            "consume": consume or [],
            "created_at": now,
        }
        intent_key = state_key("intent", state_identity("intent", intent))
        for mutation in mutations:
            value = mutation.get("value")
            if isinstance(value, dict) and value.get("operation_id") is None:
                value["operation_id"] = operation_id
        with self.transaction() as conn:
            taken = conn.execute(
                "SELECT operation_id FROM layout_operation_requests "
                "WHERE request_id=?", (client_request_id,)).fetchone()
            if taken is not None:
                raise LayoutStateError(
                    409, "layout_request_conflict",
                    f"client_request_id 已绑定操作 {taken['operation_id']}")
            conn.execute(
                "INSERT INTO layout_review_records "
                "(record_id, device_id, kind, revision, json) "
                "VALUES (?, ?, 'operation', 1, ?)",
                (operation_id, device_id, canonical_json(operation)))
            conn.execute(
                "INSERT INTO layout_typed_state "
                "(kind, state_key, device_id, principal_id, revision, json) "
                "VALUES ('intent', ?, ?, ?, 1, ?)",
                (intent_key, device_id, principal_id,
                 canonical_json(intent)))
            conn.execute(
                "INSERT INTO layout_operation_requests "
                "(request_id, operation_id) VALUES (?, ?)",
                (client_request_id, operation_id))
        # 故障注入点只对真实指针/资源协议操作生效（幂等台账登记不注入）
        if pointer_after is not None or mutations:
            self._fault("intent")
        return operation

    def operation_commit(self, operation_id: str, *, response: dict,
                         after_revision: int | None = None,
                         mutations: list[dict] | None = None,
                         consume: list[tuple] | None = None) -> dict:
        """§9 第 5 步：一笔 Store 事务应用 typed mutations、记录消费，
        写 committed、after_revision 和原响应。幂等：已 committed 原样返回。"""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT json FROM layout_review_records "
                "WHERE kind='operation' AND record_id=?",
                (operation_id,)).fetchone()
            if row is None:
                raise LayoutStateError(404, "layout_resource_missing",
                                       f"操作 {operation_id} 不存在")
            operation = json.loads(row["json"])
            if operation["phase"] == "committed":
                return operation
            for mutation in (mutations or []):
                table = mutation["table"]
                kind = TABLE_TO_KIND.get(table)
                if kind is None:
                    raise LayoutStateError(422, "layout_request_invalid",
                                           f"未知 mutation 表标签 {table}")
                value = mutation["value"]
                device_id, principal_id = state_ownership(kind, value)
                key = state_key(kind, state_identity(kind, value))
                expected = int(mutation.get("expected_revision") or 0)
                payload = canonical_json(value)
                existing = conn.execute(
                    "SELECT revision, json FROM layout_typed_state "
                    "WHERE kind=? AND state_key=?", (kind, key)).fetchone()
                if existing is None and expected == 0:
                    conn.execute(
                        "INSERT INTO layout_typed_state "
                        "(kind, state_key, device_id, principal_id, revision, json) "
                        "VALUES (?, ?, ?, ?, 1, ?)",
                        (kind, key, device_id, principal_id, payload))
                elif existing is not None \
                        and existing["json"] == payload:
                    continue  # 同内容重放：幂等复用原行
                else:
                    cur = conn.execute(
                        "UPDATE layout_typed_state SET revision = revision + 1, "
                        "json = ? WHERE kind=? AND state_key=? AND revision=?",
                        (payload, kind, key, expected))
                    if cur.rowcount != 1:
                        raise LayoutStateError(
                            409, "layout_revision_conflict",
                            f"mutation {table} CAS 冲突")
            for (kind, key, request_id) in (consume or []):
                cRow = conn.execute(
                    "SELECT revision, json FROM layout_typed_state "
                    "WHERE kind=? AND state_key=?", (kind, key)).fetchone()
                if cRow is None:
                    raise LayoutStateError(404, "layout_resource_missing",
                                           f"{kind} 待消费行不存在")
                value = json.loads(cRow["json"])
                if value.get("consumed_by") == request_id \
                        and cRow["revision"] == 2:
                    continue
                if cRow["revision"] != 1 \
                        or value.get("consumed_by") is not None:
                    raise LayoutStateError(
                        409, "layout_request_conflict",
                        f"{kind} 已被其他请求消费")
                value = {**value, "consumed_by": request_id}
                cur = conn.execute(
                    "UPDATE layout_typed_state SET revision = 2, json = ? "
                    "WHERE kind=? AND state_key=? AND revision=1",
                    (canonical_json(value), kind, key))
                if cur.rowcount != 1:
                    raise LayoutStateError(409, "layout_revision_conflict",
                                           f"{kind} 消费 CAS 冲突")
            operation.update(phase="committed", response=response,
                             after_revision=after_revision,
                             updated_at=self._now())
            conn.execute(
                "UPDATE layout_review_records SET revision = revision + 1, "
                "json=? WHERE kind='operation' AND record_id=?",
                (canonical_json(operation), operation_id))
            if mutations or consume:
                self._fault("final_tx")
            return operation

    def _set_operation_phase(self, operation_id: str, phase: str,
                             error: dict | None) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT json FROM layout_review_records "
                "WHERE kind='operation' AND record_id=?",
                (operation_id,)).fetchone()
            if row is None:
                return None
            operation = json.loads(row["json"])
            if operation["phase"] == "committed":
                return operation
            operation.update(phase=phase, error=error,
                             updated_at=self._now())
            conn.execute(
                "UPDATE layout_review_records SET revision = revision + 1, "
                "json=? WHERE kind='operation' AND record_id=?",
                (canonical_json(operation), operation_id))
            return operation

    def operation_abort(self, operation_id: str, error: dict) -> dict | None:
        return self._set_operation_phase(operation_id, "aborted", error)

    def operation_mark_unknown(self, operation_id: str,
                               error: dict) -> dict | None:
        return self._set_operation_phase(operation_id, "unknown", error)

    def operation_get(self, operation_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM layout_review_records "
                "WHERE kind='operation' AND record_id=?",
                (operation_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def operation_get_by_request(self, client_request_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT operation_id FROM layout_operation_requests "
                "WHERE request_id=?", (client_request_id,)).fetchone()
        if row is None:
            return None
        return self.operation_get(row["operation_id"])

    def operation_find_committed_resource(self, resource_kind: str,
                                          resource_id: str,
                                          version: int | None = None) -> dict | None:
        """R3-13 统一可见性：已提交发布事务查询。

        返回 phase=committed 且 resource_kind/resource_id 相符的 operation
        行；template 资源另核对 response.template_ref 的 id/version。
        只读共享模板的本机发布（草稿链）不经 catalog，可见性以本事务为准；
        导入等 package 操作不充当任何资源的发布事务。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT json FROM layout_review_records "
                "WHERE kind='operation'").fetchall()
        for row in rows:
            operation = json.loads(row["json"])
            if operation.get("phase") != "committed" \
                    or operation.get("resource_kind") != resource_kind \
                    or operation.get("resource_id") != resource_id:
                continue
            if version is None:
                return operation
            ref = (operation.get("response") or {}).get("template_ref") or {}
            if ref.get("id") == resource_id \
                    and ref.get("version") == version:
                return operation
        return None

    def operations_non_terminal(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT json FROM layout_review_records "
                "WHERE kind='operation' ORDER BY record_id").fetchall()
        return [json.loads(r["json"]) for r in rows
                if json.loads(r["json"])["phase"] in ("prepared", "unknown")]

    def intent_get(self, operation_id: str) -> dict | None:
        rows = self.state_scan("intent")
        for row in rows:
            if row["value"].get("operation_id") == operation_id:
                return row["value"]
        return None

    # ---------------- 适配指针（专用表，非自由 KV） ----------------
    def adaptation_pointer_get(self, device_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM layout_adaptation_pointer WHERE device_id=?",
                (device_id,)).fetchone()
        if row is None:
            return None
        return {"device_id": device_id, "revision": int(row["revision"]),
                "version": row["selected_version"],
                "sha256": row["selected_sha256"],
                "updated_at": row["updated_at"],
                "operation_id": row["operation_id"]}

    def adaptation_pointer_cas(self, device_id: str, expected_revision: int,
                               version: int, sha256: str,
                               operation_id: str) -> dict:
        """§9 第 4 步：核对 before，再原子替换 after（revision 恰 +1）。"""
        self._fault("pointer")  # 测试注入点：指针切换前故障 → 指针保持 before
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM layout_adaptation_pointer "
                "WHERE device_id=?", (device_id,)).fetchone()
            current = int(row["revision"]) if row else 0
            if current != expected_revision:
                raise LayoutStateError(
                    409, "layout_revision_conflict",
                    f"指针修订冲突（当前 {current}，期望 {expected_revision}）")
            conn.execute(
                "INSERT INTO layout_adaptation_pointer "
                "(device_id, revision, selected_version, selected_sha256, "
                " updated_at, operation_id) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET revision=excluded.revision, "
                "selected_version=excluded.selected_version, "
                "selected_sha256=excluded.selected_sha256, "
                "updated_at=excluded.updated_at, operation_id=excluded.operation_id",
                (device_id, current + 1, version, sha256, self._now(),
                 operation_id))
        return self.adaptation_pointer_get(device_id)

    # ---------------- 不可变版本文件（staging → fsync → 原子重命名） ----------------
    def _adaptation_version_dir(self, device_id: str, version: int) -> Path:
        return (self.data_root / "devices" / device_id / "layout_adaptation"
                / "versions")

    def adaptation_version_relpath(self, device_id: str, version: int) -> str:
        return f"devices/{device_id}/layout_adaptation/versions/{version}.json"

    @staticmethod
    def _version_payload(record: dict) -> bytes:
        return json.dumps(record, ensure_ascii=False, indent=1,
                          sort_keys=True).encode("utf-8")

    def plan_adaptation_version(self, device_id: str, version: int,
                                record: dict) -> dict:
        """在不落盘的前提下计算 intent 的 file_plan（路径/摘要/字节）。"""
        payload = self._version_payload(record)
        return {"relative_path": self.adaptation_version_relpath(
                    device_id, version),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload)}

    def write_adaptation_version(self, device_id: str, version: int,
                                 record: dict) -> dict:
        """暂存 → fsync → 原子重命名到不可变位置 → fsync 父目录。
        不允许覆盖同名异摘要文件；同摘要幂等复用。返回 file_plan。"""
        payload = self._version_payload(record)
        final = self._adaptation_version_dir(device_id, version) / \
            f"{version}.json"
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            existing = json.loads(final.read_text(encoding="utf-8"))
            if existing.get("sha256") == record.get("sha256"):
                return {"relative_path": self.adaptation_version_relpath(
                    device_id, version), "sha256": record["sha256"],
                    "byte_size": final.stat().st_size}
            raise LayoutStateError(
                503, "layout_archive_invalid",
                f"版本文件 {final} 已存在且内容不同；不可变文件不可覆盖")
        staging = self._staging / f"{uuid.uuid4().hex}.json"
        with open(staging, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, final)
        dir_fd = os.open(final.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        self._fault("files")
        return {"relative_path": self.adaptation_version_relpath(
                    device_id, version),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload)}

    def read_adaptation_version(self, device_id: str,
                                version: int) -> dict | None:
        final = self._adaptation_version_dir(device_id, version) / \
            f"{version}.json"
        if not final.exists():
            return None
        try:
            return json.loads(final.read_text(encoding="utf-8"))
        except ValueError:
            raise LayoutStateError(503, "layout_archive_invalid",
                                   f"版本文件 {final} 损坏")

    def list_adaptation_versions(self, device_id: str) -> dict[int, str]:
        """磁盘上已完整落盘的版本 → 内容摘要（含孤立完整档；版本预约不丢号）。"""
        directory = self._adaptation_version_dir(device_id, 1)
        result: dict[int, str] = {}
        if not directory.exists():
            return result
        for path in directory.glob("*.json"):
            try:
                version = int(path.stem)
                record = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(record.get("sha256"), str):
                result[version] = record["sha256"]
        return result

    def write_adaptation_asset(self, device_id: str, version: int,
                               asset_id: str, data: bytes) -> dict:
        final = (self.data_root / "devices" / device_id / "layout_adaptation"
                 / "versions" / str(version) / "assets" / asset_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            existing = final.read_bytes()
            if hashlib.sha256(existing).hexdigest() == \
                    hashlib.sha256(data).hexdigest():
                return {"relative_path": str(
                    final.relative_to(self.data_root)),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "byte_size": len(data)}
            raise LayoutStateError(503, "layout_archive_invalid",
                                   f"资产 {final} 已存在且字节不同")
        staging = self._staging / f"{uuid.uuid4().hex}.bin"
        with open(staging, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, final)
        return {"relative_path": str(final.relative_to(self.data_root)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_size": len(data)}

    def read_adaptation_asset(self, device_id: str, version: int,
                              asset_id: str) -> bytes | None:
        final = (self.data_root / "devices" / device_id / "layout_adaptation"
                 / "versions" / str(version) / "assets" / asset_id)
        return final.read_bytes() if final.exists() else None

    # ---------------- 版本持久预约 ----------------
    def reserved_adaptation_versions(self, device_id: str) -> set[int]:
        """prepared/unknown 操作的 intent 中已预约的版本号（崩溃不丢号）。"""
        reserved: set[int] = set()
        for operation in self.operations_non_terminal():
            if operation.get("device_id") != device_id:
                continue
            intent = self.intent_get(operation["operation_id"])
            if intent is None:
                continue
            after = intent.get("pointer_after") or {}
            if isinstance(after, dict) and isinstance(
                    after.get("version"), int):
                reserved.add(int(after["version"]))
            response = intent.get("planned_response") or {}
            ref = response.get("adaptation_ref") or {}
            if isinstance(ref, dict) and isinstance(ref.get("version"), int):
                reserved.add(int(ref["version"]))
        return reserved

    def next_adaptation_version(self, device_id: str) -> int:
        """版本 = max(指针、磁盘完整档、持久预约) + 1；允许跳号，不复用号覆盖。"""
        candidates = set(self.list_adaptation_versions(device_id))
        candidates |= self.reserved_adaptation_versions(device_id)
        pointer = self.adaptation_pointer_get(device_id)
        if pointer and isinstance(pointer.get("version"), int):
            candidates.add(int(pointer["version"]))
        return max(candidates, default=0) + 1

    # ---------------- 启动对账（§9 崩溃矩阵） ----------------
    def _files_complete(self, intent: dict) -> bool:
        for plan in intent.get("files") or []:
            path = self.data_root / plan["relative_path"]
            if not path.exists():
                return False
            data = path.read_bytes()
            if len(data) != plan.get("byte_size") \
                    or hashlib.sha256(data).hexdigest() != plan.get("sha256"):
                return False
        return True

    @staticmethod
    def _pointer_matches(pointer: dict | None, expected: dict | None) -> bool:
        no_pointer = expected is None or (
            int(expected.get("revision") or 0) == 0
            and expected.get("version") is None)
        if no_pointer:
            return pointer is None or int(pointer.get("revision", 0)) == 0
        if pointer is None:
            return False
        return int(pointer.get("revision") or 0) == int(expected["revision"]) \
            and pointer.get("version") == expected.get("version") \
            and pointer.get("sha256") == expected.get("sha256")

    def reconcile(self) -> list[dict]:
        """启动对账：未终结 operation 逐项按崩溃矩阵落 aborted/committed/unknown；
        不自动重发发布、不换指针、不采图、不加 revision。"""
        report: list[dict] = []
        self._blocked_reasons = []
        for operation in self.operations_non_terminal():
            op_id = operation["operation_id"]
            entry = {"operation_id": op_id,
                     "resource_kind": operation["resource_kind"],
                     "device_id": operation.get("device_id")}
            intent = self.intent_get(op_id)
            if intent is None:
                self.operation_abort(op_id, {
                    "code": "layout_archive_invalid",
                    "message": "prepared 操作缺少 intent，无法恢复计划"})
                entry["settled"] = "aborted"
                entry["reason"] = "intent_missing"
                report.append(entry)
                continue
            files_ok = self._files_complete(intent)
            before = intent.get("pointer_before")
            after = intent.get("pointer_after")
            pointer = self.adaptation_pointer_get(
                operation["device_id"]) if operation.get("device_id") else None
            if after is not None:
                if self._pointer_matches(pointer, after) and files_ok:
                    # 指针已精确等于 after、文件完整、Store 未最终提交 →
                    # 按原 intent 补齐 Store 事务并 committed
                    self.operation_commit(
                        op_id, response=intent["planned_response"],
                        after_revision=intent["pointer_after"]["revision"],
                        mutations=intent.get("mutations") or [],
                        consume=self._intent_consumes(intent))
                    entry["settled"] = "committed"
                    entry["reason"] = "pointer_after_files_complete"
                elif self._pointer_matches(pointer, before) and files_ok:
                    # 旧指针、文件完整、Store 未提交 → aborted；完整孤档保留
                    # 并占用版本号，不补发发布
                    self.operation_abort(op_id, {
                        "code": "layout_request_conflict",
                        "message": "发布未完成对账：文件已完整但指针未切换，"
                                   "保留孤档与版本号，请重新预览后以新编号发布"})
                    entry["settled"] = "aborted"
                    entry["reason"] = "files_complete_pointer_before"
                elif self._pointer_matches(pointer, before) and not files_ok:
                    self.operation_abort(op_id, {
                        "code": "layout_archive_invalid",
                        "message": "发布未完成对账：文件缺失或部分暂存，"
                                   "保留旧指针；暂存不入资料"})
                    entry["settled"] = "aborted"
                    entry["reason"] = "files_incomplete_pointer_before"
                else:
                    # 指针既非 before 也非 after，或 Store 与文件矛盾 →
                    # unknown/明确阻断；保留现场与历史
                    self.operation_mark_unknown(op_id, {
                        "code": "layout_archive_invalid",
                        "message": "存储完整性矛盾（指针/文件/台账），"
                                   "需修复后重新对账"})
                    self._blocked_reasons.append(
                        f"operation {op_id} unknown")
                    entry["settled"] = "unknown"
                    entry["reason"] = "pointer_or_files_contradiction"
            else:
                # 无指针操作（导入/导出/草稿/采集）：只有 Store 最终提交的
                # 记录可见。R3-13：intent 的 catalog 变更已全部原样落库 →
                # 按原意图幂等完成（补台账 committed，不重建资源/不换指针）；
                # 否则 aborted（未提交资源不可见）。
                if self._intent_catalog_applied(intent):
                    self.operation_commit(
                        op_id, response=intent.get("planned_response") or {},
                        after_revision=None,
                        mutations=intent.get("mutations") or [])
                    entry["settled"] = "committed"
                    entry["reason"] = "no_pointer_catalog_applied"
                else:
                    self.operation_abort(op_id, {
                        "code": "layout_request_conflict",
                        "message": "无指针操作未完成最终提交，已中止；"
                                   "未提交资源不可见"})
                    entry["settled"] = "aborted"
                    entry["reason"] = "no_pointer_uncommitted"
            report.append(entry)
        return report

    def _intent_catalog_applied(self, intent: dict) -> bool:
        """intent 的全部 catalog mutation 是否都已按同内容落库。"""
        mutations = intent.get("mutations") or []
        if not mutations:
            return False
        for mutation in mutations:
            if mutation.get("table") != "catalog":
                return False
            value = mutation.get("value") or {}
            try:
                kind = TABLE_TO_KIND["catalog"]
                key = state_key(kind, state_identity(kind, value))
            except LayoutStateError:
                return False
            row = self.state_row(kind, key)
            if row is None or row["json_text"] != canonical_json(value):
                return False
        return True

    @staticmethod
    def _intent_consumes(intent: dict) -> list[tuple]:
        consumes = []
        for entry in intent.get("consume") or []:
            consumes.append((entry["kind"], entry["state_key"],
                             entry["client_request_id"]))
        return consumes

    # ---------------- 旧自由 KV 离线迁移 ----------------
    _LEGACY_KNOWN_PREFIXES = (
        "adaptation_version:", "adaptation_pointer:", "review_issue:",
        "layout_op:", "layout_op_req:", "layout_package:",
        "layout_import_preview:", "layout_capture:", "layout_imported:",
        "region_version:", "template_version:", "template_asset:",
        "layout_pointer:", "region_preview:", "adaptation:",
    )

    def migrate_legacy_kv(self, kv, *, apply: bool = True) -> dict:
        """旧 layout_kv 只读迁移：逐记录源摘要+键映射输出；不合格记录原样
        归档待复核；可转换旧定义只生成草稿（不继承执行资格）；不删除/不改写
        旧 KV；同根中断后重跑按 journal 幂等续跑。"""
        now = self._now()
        report = {"processed": 0, "skipped": 0, "entries": []}

        def record_entry(source_key, source_json, disposition, reason,
                         target_kind=None, target_state_key=None):
            source_sha = hashlib.sha256(
                source_json.encode("utf-8")).hexdigest()
            entry = {"source_key": source_key, "source_sha256": source_sha,
                     "disposition": disposition, "reason": reason,
                     "target_kind": target_kind,
                     "target_state_key": target_state_key}
            report["entries"].append(entry)
            with self.transaction() as conn:
                previous = conn.execute(
                    "SELECT source_sha256, disposition FROM layout_kv_migration "
                    "WHERE source_key=?", (source_key,)).fetchone()
                if previous is not None:
                    if previous["source_sha256"] == source_sha:
                        report["skipped"] += 1
                        entry["disposition"] = "skipped"
                        return
                    # 同键内容变化：原档保留，按变化归档待复核
                    attempts = conn.execute(
                        "SELECT COALESCE(MAX(attempt),0) AS n "
                        "FROM layout_kv_migration_archive WHERE source_key=?",
                        (source_key,)).fetchone()["n"]
                    conn.execute(
                        "INSERT INTO layout_kv_migration_archive "
                        "(source_key, attempt, source_json, source_sha256, "
                        " reason, archived_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (source_key, attempts + 1, source_json, source_sha,
                         "changed_since_previous_run", now))
                    entry["disposition"] = "archived"
                    entry["reason"] = "changed_since_previous_run"
                    return
                conn.execute(
                    "INSERT INTO layout_kv_migration "
                    "(source_key, source_sha256, disposition, target_kind, "
                    " target_state_key, reason, migrated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (source_key, source_sha, entry["disposition"],
                     target_kind, target_state_key, reason, now))
                conn.execute(
                    "INSERT INTO layout_kv_migration_archive "
                    "(source_key, attempt, source_json, source_sha256, "
                    " reason, archived_at) VALUES (?, 1, ?, ?, ?, ?)",
                    (source_key, source_json, source_sha, reason, now))

        if not apply:
            # dry-run：只输出分类，不写任何表
            for source_key, value in kv.scan_all():
                source_json = canonical_json(value)
                classification = self._classify_legacy(source_key, value)
                report["processed"] += 1
                report["entries"].append({
                    "source_key": source_key,
                    "source_sha256": hashlib.sha256(
                        source_json.encode("utf-8")).hexdigest(),
                    "disposition": classification[0],
                    "reason": classification[1],
                    "target_kind": classification[2] if classification
                    else None,
                    "target_state_key": None})
            return report

        for source_key, value in kv.scan_all():
            report["processed"] += 1
            with self._lock:
                done = self._conn.execute(
                    "SELECT source_sha256 FROM layout_kv_migration "
                    "WHERE source_key=?", (source_key,)).fetchone()
            source_json = canonical_json(value)
            source_sha = hashlib.sha256(
                source_json.encode("utf-8")).hexdigest()
            if done is not None:
                if done["source_sha256"] == source_sha:
                    report["skipped"] += 1
                    report["entries"].append({
                        "source_key": source_key,
                        "source_sha256": source_sha,
                        "disposition": "skipped", "reason": "already_migrated",
                        "target_kind": None, "target_state_key": None})
                    continue
            disposition, reason, target = self._classify_legacy(
                source_key, value)
            target_kind = target_state_key = None
            if disposition == "draft":
                draft = self._legacy_to_draft(source_key, value)
                if draft is not None:
                    target_kind = "draft"
                    target_state_key = state_key(
                        "draft", state_identity("draft", draft))
                    self.state_insert("draft", target_state_key,
                                      draft.get("device_id"),
                                      draft.get("principal_id"), draft)
            record_entry(source_key, source_json, disposition, reason,
                         target_kind, target_state_key)
        return report

    def _classify_legacy(self, source_key: str, value: dict):
        """返回 (disposition, reason, convert?)。"""
        if not isinstance(value, dict):
            return ("archived", "unknown_key_not_object", None)
        if source_key.startswith("adaptation_version:"):
            parts = source_key.split(":")
            device, version = parts[1], parts[2]
            # 缺身份/来源：device_id 缺失或与键不符
            if value.get("device_id") != device:
                return ("archived", "identity_missing", None)
            if str(value.get("version")) != version:
                return ("archived", "identity_missing", None)
            if not value.get("pages") and not value.get("page_checks"):
                return ("archived", "half_committed_no_pages", None)
            sha = value.get("sha256")
            if isinstance(sha, str) and len(sha) == 64:
                expect = content_sha({k: v for k, v in value.items()
                                      if k != "sha256"})
                if sha != expect:
                    return ("archived", "digest_mismatch", None)
            # 旧自报 verified：不获得新动作资格
            if value.get("status") == "verified":
                return ("archived", "legacy_self_reported_verified", None)
            return ("draft", "convertible_definition_draft_only", True)
        if source_key.startswith("adaptation_pointer:"):
            return ("archived", "pointer_requires_manual_review", None)
        if source_key.startswith("layout_op:"):
            phase = value.get("phase")
            if phase in ("prepared", "unknown"):
                return ("archived", "half_committed_operation", None)
            return ("archived", "legacy_operation_requires_review", None)
        if source_key.startswith("layout_import_preview:"):
            return ("archived", "half_committed_import_preview", None)
        for prefix in ("review_issue:", "layout_package:", "layout_capture:",
                       "layout_imported:", "region_version:",
                       "template_version:", "template_asset:",
                       "layout_pointer:", "region_preview:", "layout_op_req:",
                       "adaptation:"):
            if source_key.startswith(prefix):
                return ("archived", "legacy_record_requires_review", None)
        return ("archived", "unknown_key", None)

    def _legacy_to_draft(self, source_key: str, value: dict) -> dict | None:
        """可转换旧定义 → 草稿（revision=1，不继承执行资格/通过状态）。"""
        states = {}
        pages = value.get("pages")
        if isinstance(pages, dict):
            states = {state: {"state": state, "definition": page}
                      for state, page in pages.items()
                      if state in PAGE_STATE_NAMES}
        elif isinstance(pages, list):
            for page in pages:
                if isinstance(page, dict) and page.get("state") \
                        in PAGE_STATE_NAMES:
                    states[page["state"]] = {"state": page["state"],
                                             "definition": page}
        if not states:
            return None
        device_id = value.get("device_id")
        draft_id = str(uuid.uuid4())
        return {
            "draft_id": draft_id,
            "device_id": device_id,
            "principal_id": None,  # 旧档无当前作者：不补当前身份
            "revision": 1,
            "created_at": self._now(),
            "states": {state: {"definition": body.get("definition"),
                               "check": None, "evidence_ids": [],
                               "accepted_at": None}
                       for state, body in states.items()},
            "metadata": {"migrated_from": source_key,
                         "migrated_note": "旧定义仅生成草稿；"
                                          "不继承执行资格/通过状态/作者/安装"},
            "status": "draft",
        }


PAGE_STATE_NAMES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty",
                    "chat_ime_text")
