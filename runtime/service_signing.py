"""通用服务签名原语：机器对机器 API 的 HMAC 请求验签（与具体业务无关）。

签名原文固定 METHOD + "\\n" + 原始 path_and_query + "\\n" + UTC 时间 + "\\n" + nonce +
"\\n" + SHA256(原始请求体字节)（GET 空体 hash=SHA256(b""））；不先 URL decode 再验签。
key 文件格式：{"format_version":1, "keys":[{"key_id","secret_hex","enabled"}], "active_key_id"}。
线协议头名沿用历史命名（X-WX-*），属已冻结的既有协议值，不代表业务绑定；
nonce 一次性持久化、授权认领等台账语义在 extensions/wx_navigation/auth.py（可选扩展）。
"""
from __future__ import annotations

import hashlib
import hmac
import json

SIGNATURE_FIELDS = ("X-WX-Key-Id", "X-WX-Time", "X-WX-Nonce", "X-WX-Signature")
SKEW_PAST_S = 60
SKEW_FUTURE_S = 5


class ServiceAuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def load_keys(path: str) -> dict[str, tuple[bytes, bool]]:
    """key_id -> (secret, enabled)。"""
    doc = json.loads(open(path, encoding="utf-8").read())
    if doc.get("format_version") != 1:
        raise ServiceAuthError(503, "authorization_unavailable",
                               "服务 key 文件版本未知")
    return {k["key_id"]: (bytes.fromhex(k["secret_hex"]), bool(k.get("enabled", True)))
            for k in doc.get("keys") or []}


def signature_base(method: str, path_and_query: str, timestamp: str,
                   nonce: str, body: bytes) -> bytes:
    return (method.encode("ascii") + b"\n" + path_and_query.encode("utf-8")
            + b"\n" + timestamp.encode("ascii") + b"\n" + nonce.encode("ascii")
            + b"\n" + hashlib.sha256(body or b"").hexdigest().encode("ascii"))


# ---------------- 请求验签（nonce 一次性台账，核心自持） ----------------
import sqlite3
from datetime import datetime, timedelta, timezone

NONCE_RETENTION_S = 120

SERVICE_NONCE_DDL = [
    """CREATE TABLE IF NOT EXISTS service_auth_nonces (
  record_json TEXT NOT NULL CHECK(json_valid(record_json)),
  receiver TEXT GENERATED ALWAYS AS (json_extract(record_json, '$.receiver')) STORED NOT NULL,
  key_id TEXT GENERATED ALWAYS AS (json_extract(record_json, '$.key_id')) STORED NOT NULL,
  nonce TEXT GENERATED ALWAYS AS (json_extract(record_json, '$.nonce')) STORED NOT NULL,
  expires_at TEXT GENERATED ALWAYS AS (json_extract(record_json, '$.expires_at')) STORED,
  UNIQUE (receiver, key_id, nonce)
)""",
    """CREATE INDEX IF NOT EXISTS idx_service_auth_nonces_expires
ON service_auth_nonces (expires_at)""",
]


def _ms(ts=None) -> str:
    if ts is None:
        ts = datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ensure_service_auth_schema(conn: sqlite3.Connection) -> None:
    for stmt in SERVICE_NONCE_DDL:
        conn.execute(stmt)
    conn.commit()


def verify_service_signature(conn: sqlite3.Connection,
                             keys: dict[str, tuple[bytes, bool]],
                             method: str, path_and_query: str, body: bytes,
                             headers, receiver: str = "fleet",
                             now: datetime | None = None) -> str:
    """通用服务签名验签：返回 key_id。任何失败零动作；
    nonce 先持久化再处理，失败也消耗（重放拒绝）。"""
    def _header(name: str):
        value = headers.get(name)
        if value is None and hasattr(headers, "keys"):
            lowered = name.lower()
            for key in headers.keys():
                if str(key).lower() == lowered:
                    value = headers[key]
                    break
        return value

    missing = [f for f in SIGNATURE_FIELDS if not _header(f)]
    if missing:
        raise ServiceAuthError(401, "authorization_unavailable",
                               f"缺少服务签名头：{', '.join(missing)}")
    key_id = _header("X-WX-Key-Id")
    entry = keys.get(key_id)
    if entry is None:
        raise ServiceAuthError(401, "authorization_unavailable", f"未知 key {key_id}")
    secret, enabled = entry
    if not enabled:
        raise ServiceAuthError(401, "authorization_unavailable", f"key {key_id} 已停用")
    timestamp = _header("X-WX-Time")
    nonce = _header("X-WX-Nonce")
    signature = _header("X-WX-Signature")
    try:
        request_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise ServiceAuthError(401, "authorization_unavailable", "时间戳不可解析")
    now = now or datetime.now(timezone.utc)
    if not (now - timedelta(seconds=SKEW_PAST_S) <= request_time
            <= now + timedelta(seconds=SKEW_FUTURE_S)):
        raise ServiceAuthError(401, "authorization_unavailable",
                               "时间偏移超出 -60s..+5s 窗口")
    expected = hmac.new(secret,
                        signature_base(method, path_and_query, timestamp,
                                       nonce, body),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ServiceAuthError(401, "authorization_invalid", "签名校验失败")
    expires = _ms(now + timedelta(seconds=NONCE_RETENTION_S))
    try:
        conn.execute(
            "INSERT INTO service_auth_nonces (record_json) VALUES (?)",
            (json.dumps({"format_version": 1, "record_type": "nonce",
                         "receiver": receiver, "key_id": key_id, "nonce": nonce,
                         "request_digest": hashlib.sha256(
                             signature_base(method, path_and_query, timestamp,
                                            nonce, body)).hexdigest(),
                         "received_at": _ms(), "expires_at": expires},
                        ensure_ascii=False, sort_keys=True),))
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ServiceAuthError(401, "nonce_replayed",
                               "nonce 已被使用（重放拒绝）") from exc
    conn.commit()
    conn.execute("DELETE FROM service_auth_nonces WHERE expires_at < ?",
                 (_ms(now - timedelta(seconds=NONCE_RETENTION_S)),))
    conn.commit()
    return key_id
