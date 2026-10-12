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
