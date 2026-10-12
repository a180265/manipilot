"""R3-17/D02：手机一次性配对与凭据撤销（ime-pairing.v1）。

Fleet 是手机配对、凭据与撤销的唯一事实源（附件 P）。持久记录（自身 SQLite
事务）：invitation / pairing / credential / phone / management_operation /
确认失败限流窗口 / 时钟水位。服务器只存凭据摘要，不存可返回的长期明文 token。

状态机（附件 P1）：
- invitation：offered → consumed | expired(5min) | revoked
- pairing：pending → active | expired(activation_deadline 10min) | revoked
- credential：pending → active | revoked（pending 不能领取任务/报几何）

本模块不改变 input_binding、输入开关、校准资格或 unknown 状态；身份与绑定
分别校验（配对成功不自行取得输入/发送权限）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

CONTRACT_VERSION = "ime-pairing.v1"
INVITATION_TTL_S = 300          # P2：有效期固定 5 分钟，服务重启不延长
ACTIVATION_DEADLINE_S = 600     # P3：首次成功 confirm 后 10 分钟
CONFIRM_RATE_LIMIT = 10         # 每可信来源每分钟
CONFIRM_RATE_WINDOW_S = 60.0
CLOCK_GUARD_BACK_MS = 300_000   # 相对持久水位回退超过 5 分钟 = 时钟不可信

# ime-pairing-error.v1 闭集（附件 P4）
ERROR_STATUS = {
    "invalid_request": 400,
    "authentication_required": 401,
    "phone_scope_denied": 403,
    "pairing_not_found": 404,
    "method_not_allowed": 405,
    "invitation_consumed": 409,
    "phone_already_paired": 409,
    "pairing_pending": 409,
    "revision_conflict": 409,
    "idempotency_conflict": 409,
    "invitation_expired": 410,
    "pairing_expired": 410,
    "rate_limited": 429,
    "persistence_unavailable": 503,
    "clock_untrusted": 503,
}

DDL = [
    """CREATE TABLE IF NOT EXISTS ime_pairing_kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS ime_pairing_invitations (
  record_json TEXT NOT NULL CHECK(json_valid(record_json)),
  invitation_id TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.invitation_id')) STORED NOT NULL,
  status TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.status')) STORED NOT NULL,
  secret_digest TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.secret_digest')) STORED NOT NULL,
  UNIQUE (invitation_id)
)""",
    """CREATE TABLE IF NOT EXISTS ime_pairing_phones (
  record_json TEXT NOT NULL CHECK(json_valid(record_json)),
  ime_device_id TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.ime_device_id')) STORED NOT NULL,
  phone_instance_id TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.phone_instance_id')) STORED NOT NULL,
  state TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.state')) STORED NOT NULL,
  revision INTEGER GENERATED ALWAYS AS
    (json_extract(record_json, '$.revision')) STORED NOT NULL,
  UNIQUE (ime_device_id), UNIQUE (phone_instance_id)
)""",
    """CREATE TABLE IF NOT EXISTS ime_pairing_pairings (
  record_json TEXT NOT NULL CHECK(json_valid(record_json)),
  pairing_id TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.pairing_id')) STORED NOT NULL,
  ime_device_id TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.ime_device_id')) STORED NOT NULL,
  state TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.state')) STORED NOT NULL,
  revision INTEGER GENERATED ALWAYS AS
    (json_extract(record_json, '$.revision')) STORED NOT NULL,
  UNIQUE (pairing_id)
)""",
    """CREATE TABLE IF NOT EXISTS ime_pairing_credentials (
  record_json TEXT NOT NULL CHECK(json_valid(record_json)),
  credential_id TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.credential_id')) STORED NOT NULL,
  ime_device_id TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.ime_device_id')) STORED NOT NULL,
  token_digest TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.token_digest')) STORED NOT NULL,
  state TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.state')) STORED NOT NULL,
  generation INTEGER GENERATED ALWAYS AS
    (json_extract(record_json, '$.generation')) STORED NOT NULL,
  UNIQUE (credential_id), UNIQUE (token_digest)
)""",
    """CREATE TABLE IF NOT EXISTS ime_pairing_operations (
  record_json TEXT NOT NULL CHECK(json_valid(record_json)),
  request_id TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.request_id')) STORED NOT NULL,
  operation TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.operation')) STORED NOT NULL,
  content_digest TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.content_digest')) STORED NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT GENERATED ALWAYS AS
    (json_extract(record_json, '$.created_at')) STORED NOT NULL,
  UNIQUE (operation, request_id)
)""",
    """CREATE TABLE IF NOT EXISTS ime_pairing_confirm_limits (
  source TEXT PRIMARY KEY,
  window_started_mono REAL NOT NULL,
  count INTEGER NOT NULL
)""",
]


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


def digest_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_invite_secret() -> str:
    """32 字节安全随机 → base64url 无填充（43 字符）。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def new_credential_token() -> str:
    return secrets.token_urlsafe(32)


class ImePairingError(Exception):
    """ime-pairing-error.v1 闭集异常。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = ERROR_STATUS.get(code, 500)
        self.message = message


def _content_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


class ImePairingService:
    """手机配对/凭据/撤销事实源。自带 SQLite（data_root/ime_pairing.db）。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level="DEFERRED"：`with conn:` 内的多语句写路径才是真实
        # 单事务（提交失败/崩溃整体回滚，不落半份配对状态）。原 isolation_
        # level=None 为逐语句自动提交——"事务"中途失败会留下"邀请已消费但
        # 手机/配对记录缺失"的半份状态（违反 P1/G7 存盘失败不可用契约）。
        # 读路径（SELECT）不受影响；DDL 仅在构造期执行（自动提交）。
        self._conn = sqlite3.connect(str(self.db_path), isolation_level="DEFERRED",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        for stmt in DDL:
            self._conn.execute(stmt)
        self._conn.commit()
        # 限流失败计数的独立自动提交连接（见 _confirm_count_failure）
        self._limits_conn = sqlite3.connect(str(self.db_path),
                                            isolation_level=None,
                                            check_same_thread=False)
        self._limits_conn.row_factory = sqlite3.Row
        self._limits_conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        for conn in (getattr(self, "_limits_conn", None), self._conn):
            if conn is None:
                continue
            try:
                conn.close()
            except Exception:
                pass

    # ---- 基础 ----
    def _kv(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM ime_pairing_kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _kv_set(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO ime_pairing_kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

    def _check_clock(self, conn: sqlite3.Connection) -> None:
        last = self._kv("clock_last_ms")
        now = _now_ms()
        if last is not None and now < int(last) - CLOCK_GUARD_BACK_MS:
            raise ImePairingError("clock_untrusted",
                                  "服务器时钟相对持久水位回退，拒绝配对确认")
        self._kv_set(conn, "clock_last_ms", str(max(now, int(last or 0))))

    def _record_operation(self, conn: sqlite3.Connection, operation: str,
                          request_id: str, content_digest: str,
                          response: dict) -> None:
        conn.execute(
            "INSERT INTO ime_pairing_operations"
            "(record_json, response_json) VALUES (?, ?)",
            (json.dumps({"format_version": 1, "request_id": request_id,
                         "operation": operation,
                         "content_digest": content_digest,
                         "created_at": _now_iso()}, ensure_ascii=False),
             json.dumps(response, ensure_ascii=False)))

    def _replay_operation(self, operation: str, request_id: str,
                          content_digest: str) -> dict | None:
        row = self._conn.execute(
            "SELECT record_json, response_json FROM ime_pairing_operations "
            "WHERE operation=? AND request_id=?",
            (operation, request_id)).fetchone()
        if row is None:
            return None
        rec = json.loads(row["record_json"])
        if rec.get("content_digest") != content_digest:
            raise ImePairingError(
                "idempotency_conflict",
                f"同编号请求内容不同（{operation} {request_id}）：拒绝，不覆盖原结果")
        result = json.loads(row["response_json"])
        if "replayed" in result:
            result["replayed"] = True  # 原结果原值返回，仅回放标记置真
        return result

    # ---- server_id（首次离线初始化；不静默重建） ----
    def init_server_id(self) -> str:
        conn = self._conn
        with conn:
            row = conn.execute(
                "SELECT value FROM ime_pairing_kv WHERE key='server_id'"
            ).fetchone()
            if row is not None:
                return row["value"]
            server_id = str(uuid.uuid4())
            self._kv_set(conn, "server_id", server_id)
            return server_id

    @property
    def server_id(self) -> str:
        sid = self._kv("server_id")
        if not sid:
            raise ImePairingError(
                "persistence_unavailable",
                "配对身份库缺失 server_id：须先经离线初始化，不静默重建空库")
        return sid

    # ---- 限流（持久窗口，重启不清） ----
    def _confirm_allowed(self, source: str, conn: sqlite3.Connection) -> None:
        now = time.monotonic()
        row = conn.execute(
            "SELECT window_started_mono, count FROM ime_pairing_confirm_limits "
            "WHERE source=?", (source,)).fetchone()
        if row is not None and now - row["window_started_mono"] <= CONFIRM_RATE_WINDOW_S \
                and row["count"] >= CONFIRM_RATE_LIMIT:
            raise ImePairingError("rate_limited",
                                  "确认失败次数超限（每来源每分钟 10 次），请稍后重试")

    def _confirm_count_failure(self, source: str, conn: sqlite3.Connection) -> None:
        """失败计数必须在**独立事务**提交：主 confirm 事务随后按 401 中止
        回滚（邀请/手机状态零写入），而限流事实仍持久（P4：重启不清、
        失败也计数）。专用自动提交连接写入；WAL 下与主连接并发安全。"""
        now = time.monotonic()
        limits = self._limits_conn
        row = limits.execute(
            "SELECT window_started_mono, count FROM ime_pairing_confirm_limits "
            "WHERE source=?", (source,)).fetchone()
        if row is None or now - row["window_started_mono"] > CONFIRM_RATE_WINDOW_S:
            limits.execute(
                "INSERT INTO ime_pairing_confirm_limits(source, window_started_mono, count)"
                " VALUES(?, ?, 1) ON CONFLICT(source) DO UPDATE SET"
                " window_started_mono=excluded.window_started_mono, count=1",
                (source, now))
        else:
            limits.execute(
                "UPDATE ime_pairing_confirm_limits SET count=count+1 WHERE source=?",
                (source,))
        limits.commit()

    # ---- 邀请（P2） ----
    def create_invitation(self, *, request_id: str, invite_secret: str,
                          arm_id: str | None, source: str,
                          trust: dict) -> dict:
        if not isinstance(invite_secret, str) or len(invite_secret) != 43:
            raise ImePairingError("invalid_request",
                                  "invite_secret 必须是 43 字符 base64url（32 字节）")
        if arm_id is not None and not isinstance(arm_id, str):
            raise ImePairingError("invalid_request", "arm_id 必须是字符串或 null")
        response_content = _content_digest({
            "invitation_secret_digest": digest_secret(invite_secret),
            "arm_id": arm_id})
        replayed = self._replay_operation("invitation_create", request_id,
                                          response_content)
        if replayed is not None:
            return replayed
        server_id = self.server_id
        conn = self._conn
        try:
            with conn:
                self._check_clock(conn)
                invitation_id = str(uuid.uuid4())
                issued = _now_ms()
                record = {
                    "format_version": 1,
                    "invitation_id": invitation_id,
                    "secret_digest": digest_secret(invite_secret),
                    "status": "offered",
                    "arm_id": arm_id,
                    "created_at": _now_iso(),
                    "expires_at_ms": issued + INVITATION_TTL_S * 1000,
                    "created_source": source,
                }
                conn.execute(
                    "INSERT INTO ime_pairing_invitations(record_json) VALUES (?)",
                    (json.dumps(record, ensure_ascii=False),))
                response = {
                    "contract_version": CONTRACT_VERSION,
                    "request_id": request_id,
                    "invitation_id": invitation_id,
                    "server_id": server_id,
                    "expires_at": _now_iso(),
                    "status": "offered",
                    "arm_id": arm_id,
                    "trust": dict(trust or {}),
                    # 与 confirm/activate/revoke 同族：重放标记让路由以 200
                    # 返回原邀请（掉包同号恢复可辨），原响应亦含该键。
                    "replayed": False,
                }
                self._record_operation(conn, "invitation_create", request_id,
                                       response_content, response)
                return response
        except sqlite3.Error as exc:
            raise ImePairingError("persistence_unavailable",
                                  f"邀请持久化失败：{exc}") from exc

    def _invitation(self, invitation_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT record_json FROM ime_pairing_invitations WHERE invitation_id=?",
            (invitation_id,)).fetchone()
        return json.loads(row["record_json"]) if row else None

    def get_invitation(self, invitation_id: str) -> dict:
        rec = self._invitation(invitation_id)
        if rec is None:
            raise ImePairingError("pairing_not_found", "邀请不存在")
        if rec["status"] == "offered" \
                and _now_ms() > int(rec.get("expires_at_ms") or 0):
            return {"contract_version": CONTRACT_VERSION,
                    "invitation_id": invitation_id, "status": "expired",
                    "expires_at": _now_iso(), "arm_id": rec.get("arm_id"),
                    "created_at": rec.get("created_at"),
                    "consumed_by_pairing": rec.get("consumed_by_pairing")}
        return {"contract_version": CONTRACT_VERSION,
                "invitation_id": invitation_id, "status": rec["status"],
                "expires_at": _now_iso(), "arm_id": rec.get("arm_id"),
                "created_at": rec.get("created_at"),
                "consumed_by_pairing": rec.get("consumed_by_pairing")}

    def revoke_invitation(self, invitation_id: str, *, request_id: str) -> dict:
        content = _content_digest({"invitation_id": invitation_id})
        replayed = self._replay_operation("invitation_revoke", request_id, content)
        if replayed is not None:
            return replayed
        conn = self._conn
        with conn:
            rec = self._invitation(invitation_id)
            if rec is None:
                raise ImePairingError("pairing_not_found", "邀请不存在")
            if rec["status"] == "consumed":
                raise ImePairingError(
                    "invitation_consumed",
                    "邀请已消费（不隐式撤销活动手机；如需撤销请用 ime-phones revoke）")
            if rec["status"] == "revoked":
                result = {"contract_version": CONTRACT_VERSION,
                          "request_id": request_id,
                          "invitation_id": invitation_id,
                          "status": "revoked", "replayed": True}
            else:
                rec["status"] = "revoked"
                rec["revoked_at"] = _now_iso()
                conn.execute(
                    "UPDATE ime_pairing_invitations SET record_json=? "
                    "WHERE invitation_id=?",
                    (json.dumps(rec, ensure_ascii=False), invitation_id))
                result = {"contract_version": CONTRACT_VERSION,
                          "request_id": request_id,
                          "invitation_id": invitation_id,
                          "status": "revoked", "replayed": False}
            self._record_operation(conn, "invitation_revoke", request_id,
                                   content, result)
            return result

    # ---- 手机 / 凭据（P1/P4） ----
    def list_phones(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT record_json FROM ime_pairing_phones ORDER BY ime_device_id"
        ).fetchall()
        items = []
        for row in rows:
            rec = json.loads(row["record_json"])
            items.append({
                "ime_device_id": rec["ime_device_id"],
                "phone_instance_id": rec["phone_instance_id"],
                "phone_label": rec.get("phone_label"),
                "state": rec["state"],
                "revision": int(rec["revision"]),
                "credential_generation": int(rec.get("credential_generation") or 1),
                "pairing_id": rec.get("active_pairing_id"),
                "binding_summary": rec.get("binding_summary"),
                "updated_at": rec.get("updated_at"),
            })
        return items

    def _phone(self, ime_device_id: str, conn: sqlite3.Connection) -> dict | None:
        row = conn.execute(
            "SELECT record_json FROM ime_pairing_phones WHERE ime_device_id=?",
            (ime_device_id,)).fetchone()
        return json.loads(row["record_json"]) if row else None

    def _save_phone(self, conn: sqlite3.Connection, phone: dict) -> None:
        conn.execute(
            "INSERT INTO ime_pairing_phones(record_json) VALUES (?) "
            "ON CONFLICT(ime_device_id) DO UPDATE SET record_json=excluded.record_json",
            (json.dumps(phone, ensure_ascii=False),))

    def verify_phone_token(self, token: str) -> dict | None:
        """统一身份 Provider（P4）：按当前持久 token 摘要核验。

        返回 {ime_device_id, credential_id, generation, state}；未命中/已撤销
        → None。state=pending 的凭据仅可用于查询/激活自身配对（路由层限制），
        不能领取任务或报几何（那要求 state=active）。
        撤销后旧 token 立即失效（摘要行 state=revoked）。
        """
        if not isinstance(token, str) or not token:
            return None
        row = self._conn.execute(
            "SELECT record_json FROM ime_pairing_credentials WHERE token_digest=?",
            (digest_secret(token),)).fetchone()
        if row is None:
            return None
        rec = json.loads(row["record_json"])
        if rec.get("state") not in ("active", "pending"):
            return None
        return {"ime_device_id": rec["ime_device_id"],
                "credential_id": rec["credential_id"],
                "generation": int(rec["generation"]),
                "state": rec["state"]}

    def phone_has_active_credential(self, ime_device_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM ime_pairing_credentials WHERE ime_device_id=? "
            "AND state='active' LIMIT 1", (ime_device_id,)).fetchone()
        return row is not None

    def revoke_phone(self, ime_device_id: str, *, request_id: str,
                     expected_revision: int, reason: str) -> dict:
        content = _content_digest({"ime_device_id": ime_device_id,
                                   "expected_revision": int(expected_revision),
                                   "reason": reason})
        replayed = self._replay_operation("phone_revoke", request_id, content)
        if replayed is not None:
            return replayed
        conn = self._conn
        try:
            with conn:
                self._check_clock(conn)
                phone = self._phone(ime_device_id, conn)
                if phone is None:
                    raise ImePairingError("pairing_not_found",
                                          f"手机 {ime_device_id} 未配对")
                if int(phone["revision"]) != int(expected_revision):
                    raise ImePairingError(
                        "revision_conflict",
                        f"expected_revision {expected_revision} 与当前 "
                        f"{phone['revision']} 不一致")
                new_generation = int(phone.get("credential_generation") or 1) + 1
                phone["state"] = "revoked"
                phone["revision"] = int(phone["revision"]) + 1
                phone["credential_generation"] = new_generation
                phone["revoked_at"] = _now_iso()
                phone["revoked_reason"] = reason
                phone["active_pairing_id"] = None
                self._save_phone(conn, phone)
                # active+pending 凭据全部撤销（同事务）；旧 token 立即失效
                conn.execute(
                    "UPDATE ime_pairing_credentials SET record_json=json_set("
                    "record_json, '$.state', 'revoked', '$.revoked_at', ?) "
                    "WHERE ime_device_id=? AND state IN ('active','pending')",
                    (_now_iso(), ime_device_id))
                conn.execute(
                    "UPDATE ime_pairing_pairings SET record_json=json_set("
                    "record_json, '$.state', 'revoked') "
                    "WHERE ime_device_id=? AND state IN ('pending','active')",
                    (ime_device_id,))
                result = {"contract_version": CONTRACT_VERSION,
                          "request_id": request_id,
                          "ime_device_id": ime_device_id,
                          "state": "revoked",
                          "revision": int(phone["revision"]),
                          "credential_generation": new_generation,
                          "replayed": False}
                self._record_operation(conn, "phone_revoke", request_id,
                                       content, result)
                return result
        except sqlite3.Error as exc:
            raise ImePairingError("persistence_unavailable",
                                  f"撤销事务失败：{exc}") from exc

    # ---- confirm / activate（P3） ----
    def confirm(self, *, request_id: str, invitation_id: str,
                invite_secret: str, phone_instance_id: str, phone_label: str,
                credential_id: str, credential_token: str,
                source: str = "lan") -> dict:
        for field, value in (("request_id", request_id),
                             ("invitation_id", invitation_id),
                             ("invite_secret", invite_secret),
                             ("phone_instance_id", phone_instance_id),
                             ("phone_label", phone_label),
                             ("credential_id", credential_id),
                             ("credential_token", credential_token)):
            if not isinstance(value, str) or not value:
                raise ImePairingError("invalid_request", f"{field} 必须是非空字符串")
        if len(invite_secret) != 43:
            raise ImePairingError("invalid_request",
                                  "invite_secret 必须是 43 字符 base64url")
        try:
            uuid.UUID(credential_id)
        except ValueError as exc:
            raise ImePairingError("invalid_request",
                                  "credential_id 必须是 UUID") from exc
        content = _content_digest({
            "invitation_id": invitation_id,
            "secret_digest": digest_secret(invite_secret),
            "phone_instance_id": phone_instance_id,
            "phone_label": phone_label,
            "credential_id": credential_id,
            "token_digest": digest_secret(credential_token)})
        replayed = self._replay_operation("pairing_confirm", request_id, content)
        if replayed is not None:
            return replayed  # 同原请求重放：不消耗限流名额
        conn = self._conn
        try:
            with conn:
                # 守卫顺序（锁契约）：限流核对与失败计数先行——失败计数经
                # 独立连接提交，必须发生在主连接取得写锁（首个 DML）之前，
                # 否则 WAL 单写者互锁。时钟水位写入随后（首个主连接 DML）。
                self._confirm_allowed(source, conn)
                secret_digest = digest_secret(invite_secret)
                invitation = self._invitation(invitation_id)
                if invitation is None or invitation["secret_digest"] != secret_digest:
                    self._confirm_count_failure(source, conn)
                    # 无效秘密统一 401，不泄漏他人配对资料
                    raise ImePairingError("authentication_required",
                                          "邀请不存在或秘密无效")
                if invitation["status"] == "consumed":
                    raise ImePairingError("invitation_consumed", "邀请已被消费")
                if invitation["status"] == "revoked":
                    raise ImePairingError("invitation_consumed", "邀请已撤销")
                if _now_ms() > int(invitation.get("expires_at_ms") or 0):
                    raise ImePairingError("invitation_expired",
                                          "邀请已过 5 分钟有效期")
                self._check_clock(conn)
                # 同手机实例的稳定映射：同实例推进代际；新实例新 ime_device_id
                row = conn.execute(
                    "SELECT record_json FROM ime_pairing_phones "
                    "WHERE phone_instance_id=?", (phone_instance_id,)).fetchone()
                if row is not None:
                    phone = json.loads(row["record_json"])
                    ime_device_id = phone["ime_device_id"]
                else:
                    ime_device_id = "k40-" + uuid.uuid4().hex[:12]
                    phone = None
                if phone is not None and phone["state"] == "active":
                    raise ImePairingError(
                        "phone_already_paired",
                        "该手机实例已有活动配对；须由本机明确撤销后重新配对")
                # 未过期 pending 阻断：不创建两份候选凭据
                pending_row = conn.execute(
                    "SELECT record_json FROM ime_pairing_pairings "
                    "WHERE ime_device_id=? AND state='pending'",
                    (ime_device_id,)).fetchone()
                if pending_row is not None:
                    pending = json.loads(pending_row["record_json"])
                    if _now_ms() <= int(pending.get("activation_deadline_ms") or 0):
                        raise ImePairingError(
                            "pairing_pending",
                            "该手机已有未过期 pending 配对；等待到期或由本机撤销")
                generation = int((phone or {}).get("credential_generation") or 0) + 1
                now_ms = _now_ms()
                phone = {
                    "ime_device_id": ime_device_id,
                    "phone_instance_id": phone_instance_id,
                    "phone_label": phone_label,
                    "state": "pending",
                    "revision": int((phone or {}).get("revision") or 0) + 1,
                    "credential_generation": generation,
                    "pending_pairing_id": request_id,
                    "active_pairing_id": None,
                    "updated_at": _now_iso(),
                }
                self._save_phone(conn, phone)
                pairing = {
                    "pairing_id": request_id,  # P1：pairing_id=确认request_id
                    "invitation_id": invitation_id,
                    "ime_device_id": ime_device_id,
                    "phone_instance_id": phone_instance_id,
                    "credential_id": credential_id,
                    "request_digest": content,
                    "state": "pending",
                    "revision": 1,
                    "activation_deadline_ms": now_ms + ACTIVATION_DEADLINE_S * 1000,
                    "created_at": _now_iso(),
                }
                conn.execute(
                    "INSERT INTO ime_pairing_pairings(record_json) VALUES (?) "
                    "ON CONFLICT(pairing_id) DO UPDATE SET record_json=excluded.record_json",
                    (json.dumps(pairing, ensure_ascii=False),))
                credential = {
                    "credential_id": credential_id,
                    "ime_device_id": ime_device_id,
                    "token_digest": digest_secret(credential_token),
                    "state": "pending",
                    "generation": generation,
                    "pairing_id": request_id,
                    "created_at": _now_iso(),
                }
                conn.execute(
                    "INSERT INTO ime_pairing_credentials(record_json) VALUES (?) "
                    "ON CONFLICT(credential_id) DO UPDATE SET record_json=excluded.record_json",
                    (json.dumps(credential, ensure_ascii=False),))
                invitation["status"] = "consumed"
                invitation["consumed_by_pairing"] = request_id
                invitation["consumed_at"] = _now_iso()
                conn.execute(
                    "UPDATE ime_pairing_invitations SET record_json=? "
                    "WHERE invitation_id=?",
                    (json.dumps(invitation, ensure_ascii=False), invitation_id))
                response = {
                    "contract_version": CONTRACT_VERSION,
                    "request_id": request_id,
                    "pairing_id": request_id,
                    "ime_device_id": ime_device_id,
                    "state": "pending",
                    "revision": int(phone["revision"]),
                    "activation_deadline": _now_iso(),
                    "server_id": self.server_id,
                    "replayed": False,
                }
                self._record_operation(conn, "pairing_confirm", request_id,
                                       content, response)
                return response
        except sqlite3.Error as exc:
            raise ImePairingError("persistence_unavailable",
                                  f"配对确认事务失败：{exc}") from exc

    def get_pairing(self, pairing_id: str, *, token: str) -> dict:
        credential = self.verify_phone_token(token)
        if credential is None:
            raise ImePairingError("authentication_required",
                                  "缺少或无效手机凭据（x-ime-phone-token）")
        row = self._conn.execute(
            "SELECT record_json FROM ime_pairing_pairings WHERE pairing_id=?",
            (pairing_id,)).fetchone()
        if row is None:
            raise ImePairingError("pairing_not_found", "配对不存在")
        pairing = json.loads(row["record_json"])
        if pairing["ime_device_id"] != credential["ime_device_id"]:
            raise ImePairingError("phone_scope_denied",
                                  "配对不属于该手机（A 凭据不能用 B 路径）")
        return self._pairing_view(pairing)

    def activate(self, pairing_id: str, *, request_id: str,
                 expected_revision: int, token: str) -> dict:
        content = _content_digest({"pairing_id": pairing_id,
                                   "expected_revision": int(expected_revision)})
        replayed = self._replay_operation("pairing_activate", request_id, content)
        if replayed is not None:
            return replayed
        credential = self.verify_phone_token(token)
        if credential is None:
            raise ImePairingError("authentication_required",
                                  "缺少或无效手机凭据（x-ime-phone-token）")
        conn = self._conn
        try:
            with conn:
                self._check_clock(conn)
                row = conn.execute(
                    "SELECT record_json FROM ime_pairing_pairings WHERE pairing_id=?",
                    (pairing_id,)).fetchone()
                if row is None:
                    raise ImePairingError("pairing_not_found", "配对不存在")
                pairing = json.loads(row["record_json"])
                if pairing["ime_device_id"] != credential["ime_device_id"]:
                    raise ImePairingError("phone_scope_denied",
                                          "配对不属于该手机")
                # 激活必须持本配对自身的（pending）凭据，不得用其他凭据代激活
                if credential["credential_id"] != pairing.get("credential_id"):
                    raise ImePairingError("phone_scope_denied",
                                          "激活须持本配对签发的凭据")
                if pairing["state"] == "revoked":
                    raise ImePairingError("pairing_not_found",
                                          "配对已撤销（迟到激活不得恢复身份）")
                if pairing["state"] == "active":
                    # 同号重放原结果（不再推进 revision、不改历史）
                    cred_row = conn.execute(
                        "SELECT record_json FROM ime_pairing_credentials "
                        "WHERE credential_id=?",
                        (pairing["credential_id"],)).fetchone()
                    cred = json.loads(cred_row["record_json"]) if cred_row else {}
                    result = {"contract_version": CONTRACT_VERSION,
                              "request_id": request_id,
                              "pairing_id": pairing_id,
                              "ime_device_id": pairing["ime_device_id"],
                              "state": "active",
                              "credential_id": pairing.get("credential_id"),
                              "generation": int(cred.get("generation") or 0),
                              "revision": int(pairing["revision"]),
                              "replayed": True}
                    return result
                if pairing["state"] != "pending":
                    raise ImePairingError("pairing_expired", "配对不在 pending 状态")
                if _now_ms() > int(pairing.get("activation_deadline_ms") or 0):
                    raise ImePairingError(
                        "pairing_expired", "pending 已超 10 分钟激活期限，请重新配对")
                phone = self._phone(pairing["ime_device_id"], conn)
                if phone is None or phone["state"] == "revoked":
                    raise ImePairingError("phone_scope_denied",
                                          "手机身份已撤销（迟到激活不得恢复）")
                if int(phone["revision"]) != int(expected_revision):
                    raise ImePairingError(
                        "revision_conflict",
                        f"expected_revision {expected_revision} 与当前 "
                        f"{phone['revision']} 不一致")
                # 凭据归属/代际核验：pending 凭据必须仍是本手机当前代际
                cred_row = conn.execute(
                    "SELECT record_json FROM ime_pairing_credentials "
                    "WHERE credential_id=?",
                    (pairing["credential_id"],)).fetchone()
                if cred_row is None:
                    raise ImePairingError("pairing_not_found", "配对凭据缺失")
                cred = json.loads(cred_row["record_json"])
                if cred["state"] != "pending" \
                        or int(cred["generation"]) != int(phone["credential_generation"]) \
                        or cred["ime_device_id"] != phone["ime_device_id"]:
                    raise ImePairingError("phone_scope_denied",
                                          "凭据代际已推进或被撤销，不能激活旧代际")
                other = conn.execute(
                    "SELECT 1 FROM ime_pairing_credentials "
                    "WHERE ime_device_id=? AND state='active' AND credential_id<>?",
                    (phone["ime_device_id"], cred["credential_id"])).fetchone()
                if other is not None:
                    raise ImePairingError("phone_already_paired",
                                          "该手机已存在其他活动凭据")
                cred["state"] = "active"
                cred["activated_at"] = _now_iso()
                conn.execute(
                    "UPDATE ime_pairing_credentials SET record_json=? "
                    "WHERE credential_id=?",
                    (json.dumps(cred, ensure_ascii=False), cred["credential_id"]))
                pairing["state"] = "active"
                pairing["revision"] = int(pairing["revision"]) + 1
                pairing["activated_at"] = _now_iso()
                conn.execute(
                    "UPDATE ime_pairing_pairings SET record_json=? WHERE pairing_id=?",
                    (json.dumps(pairing, ensure_ascii=False), pairing_id))
                phone["state"] = "active"
                phone["revision"] = int(phone["revision"]) + 1
                phone["active_pairing_id"] = pairing_id
                phone["pending_pairing_id"] = None
                self._save_phone(conn, phone)
                result = {"contract_version": CONTRACT_VERSION,
                          "request_id": request_id,
                          "pairing_id": pairing_id,
                          "ime_device_id": phone["ime_device_id"],
                          "state": "active",
                          "credential_id": cred["credential_id"],
                          "generation": int(cred["generation"]),
                          "revision": int(phone["revision"]),
                          "replayed": False}
                self._record_operation(conn, "pairing_activate", request_id,
                                       content, result)
                return result
        except sqlite3.Error as exc:
            raise ImePairingError("persistence_unavailable",
                                  f"激活事务失败：{exc}") from exc

    def _pairing_view(self, pairing: dict) -> dict:
        return {"contract_version": CONTRACT_VERSION,
                "pairing_id": pairing["pairing_id"],
                "ime_device_id": pairing["ime_device_id"],
                "state": pairing["state"],
                "revision": int(pairing["revision"]),
                "activation_deadline": _now_iso()
                if pairing["state"] == "pending" else None,
                "credential_id": pairing.get("credential_id"),
                "generation": None}
