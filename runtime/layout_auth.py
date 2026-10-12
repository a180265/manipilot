"""R2-23：布局路由唯一可信身份解析与授权（契约 §2 权限规则）。

安全默认（执行文档 §7.1）：
- 服务端 principal 不由正文 operator_id/principal_id/role 默认或覆盖；
  自报角色不是认证（runtime/permissions.py 旧模型不用于布局鉴权）。
- 身份来源只有两类，按序解析：
  1. 进程内已认证上下文：受信认证层（部署级人工登录/受信网关中间件）在
     request.state 上写入的 `layout_principal`（附 `layout_principal_source`）。
     本轮部署未提供该层——保留接缝，不自创协议。
  2. 受信同机转发：复用同机 helper 令牌约定（与 CAMERA_CONTROL_TOKEN_FILE
     同型）：Fleet 与调试台进程配置同一 `ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE`
     （0600、同机令牌路径约定）；请求携带 `X-Layout-Forwarder-Token` 且与文件
     值恒等比对通过后，才接受转发头 `X-Layout-Console-Principal` 中的调试台
     已认证会话主体。未配置令牌文件 → 解析失败（安全默认）。
- 未配置身份提供器时：写/取证/导出/导入/设备资料读类入口拒绝（403）并给出
  明确前置说明；只读共享模板（列表/版本/资产）按现状放行（契约 §2）。
- 跨身份读取操作/包/预览：403 且不泄漏其结果。

R3-14（身份附件 L1/L2）：本机调试台免登录边界（本模块提供全部可独立测试
函数；motor_debug_server.py 的接线由总集成人合入）：
- 回环管理端（仅 127.0.0.1/::1 peer）、精确 Host/Origin/Sec-Fetch-Site 校验；
- 随机浏览器会话 Cookie + CSRF（仅防跨站：无账号、无密码、无角色）；
  Cookie HttpOnly/SameSite=Strict/Path=/，服务端只存摘要，每次进程启动失效；
  写操作要求精确 Origin + `X-Debug-CSRF`；`Sec-Fetch-Site: cross-site` 与
  null Origin 拒绝；token 不进 URL；不接受 `Origin:*` 或"任意 localhost 端口"；
- `actor_kind=local_console` 只由受信后端构造；浏览器自报 principal/role
  一律剥离、不作身份；资源归属一致检查；历史主体迁移 dry-run/commit 纯函数；
  导出只含白名单布局资产。

待部署决定的协议（前置，不由实施者拍板）：人工登录会话的具体载体、设备权限
来源（当前单一调试台主体=既有全部设备调试权限）、凭据轮换/撤销。详见
R2-D2 报告「待 I 合入/前置」节。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

TOKEN_ENV = "ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE"
TOKEN_HEADER = "x-layout-forwarder-token"
PRINCIPAL_HEADER = "x-layout-console-principal"
IDENTITY_STATE_KEY = "layout_principal"
IDENTITY_SOURCE_KEY = "layout_principal_source"

PREREQ_MESSAGE = (
    "布局入口缺可信身份：本部署未配置已认证人工登录会话或受信转发身份。"
    "部署前置：为 Fleet 与调试台配置同一 ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE"
    "（同机 helper 令牌约定）并由受信转发方声明已认证主体，或接入部署级登录"
    "认证层；请求正文中的 operator_id/principal_id/role 不构成身份，"
    "不会作为默认或覆盖。")

PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# 令牌文件读取缓存（同 (path, mtime_ns, size) 不重复读盘；令牌属机密，
# 不进日志/错误）
_TOKEN_CACHE: tuple | None = ()


@dataclass(frozen=True)
class LayoutIdentity:
    principal: str
    source: str


def load_forwarder_token(path: str | None) -> str | None:
    """读取同机转发令牌（strip 后非空且 ≥32 字符才视为已配置）。"""
    if not path:
        return None
    try:
        full = Path(path)
        stat = full.stat()
        key = (str(full), stat.st_mtime_ns, stat.st_size)
        global _TOKEN_CACHE
        if _TOKEN_CACHE and _TOKEN_CACHE[0] == key:
            return _TOKEN_CACHE[1]
        raw = full.read_text(encoding="utf-8").strip()
        if len(raw) < 32:
            return None
        _TOKEN_CACHE = (key, raw)
        return raw
    except (OSError, ValueError):
        return None


def configured_token() -> str | None:
    return load_forwarder_token(os.environ.get(TOKEN_ENV))


def resolve_identity(request) -> LayoutIdentity | None:
    """单点解析：进程内已认证上下文 → 受信同机转发。失败返回 None。"""
    state = getattr(request, "state", None)
    principal = getattr(state, IDENTITY_STATE_KEY, None)
    if isinstance(principal, str) and PRINCIPAL_RE.match(principal):
        return LayoutIdentity(principal,
                              str(getattr(state, IDENTITY_SOURCE_KEY,
                                          "authenticated_session")))
    expected = configured_token()
    if expected:
        supplied = request.headers.get(TOKEN_HEADER)
        if isinstance(supplied, str) and hmac.compare_digest(supplied, expected):
            claimed = request.headers.get(PRINCIPAL_HEADER)
            if isinstance(claimed, str) and PRINCIPAL_RE.match(claimed):
                return LayoutIdentity(claimed, "console_forwarder")
    return None


# ---- 授权分类（契约 §2）：共享模板只读按现状放行；其余需要身份 ----
SHARED_READ_ROUTES = frozenset({
    "template_list", "template_version", "template_asset",
})


def authorize(identity: LayoutIdentity | None, *, route_id: str,
              owner_principal: str | None = None,
              shared_read: bool = False) -> None:
    """按资源归属授权；跨身份 403（不泄漏他人资源内容）。"""
    if identity is None:
        if shared_read:
            return
        raise PermissionError(PREREQ_MESSAGE)
    if owner_principal is not None and owner_principal != identity.principal:
        raise PermissionError(
            f"资源属于其他身份，当前主体 {identity.principal} 无权访问")


# ==================== R3-14：本机调试台免登录边界（附件 L1/L2） ====================

DEBUG_COOKIE_NAME = "wx_debug_console_v1"
DEBUG_CSRF_HEADER = "x-debug-csrf"
DEFAULT_DEBUG_TTL_SECONDS = 4 * 3600
LEGACY_CONSOLE_PRINCIPAL = "motor-debug-console"

# 导出白名单：只允许布局资料资源（L2：导出只含白名单布局资产，
# 不含 phone/WX/Helper 密钥）
EXPORT_ALLOWED_RESOURCE_KINDS = frozenset(
    {"template", "region", "adaptation"})
EXPORT_FORBIDDEN_KEY_RE = re.compile(
    r"token|secret|password|credential|cookie|authorization|api_?key|"
    r"session|private_key", re.IGNORECASE)

# 浏览器自报身份字段/头（一律剥离，不作身份；不生成 WX human proof）
SELF_REPORTED_IDENTITY_FIELDS = frozenset({
    "principal", "principal_id", "role", "roles", "actor", "actor_kind",
    "operator_id", "machine", "machine_id", "operator"})
SELF_REPORTED_IDENTITY_HEADERS = frozenset({
    "x-debug-principal", "x-debug-role", "x-layout-console-principal",
    "x-actor-kind", "x-forwarded-user", "x-machine-id"})


class DebugBoundaryError(Exception):
    """L1/L2 边界拒绝（服务器映射 403/401；不泄漏内部细节）。"""

    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def is_loopback_address(peer: str | None) -> bool:
    """L1：管理监听只接受回环 peer（127.0.0.0/8、::1、::ffff:127.0.0.0/8）。"""
    if not peer or not isinstance(peer, str):
        return False
    text = peer.strip().strip("[]")
    if text.lower().startswith("::ffff:"):
        text = text[7:]
    if ":" in text:
        return text == "::1"
    parts = text.split(".")
    if len(parts) != 4:
        return False
    try:
        values = [int(p) for p in parts]
    except ValueError:
        return False
    return all(0 <= v <= 255 for v in values) and values[0] == 127


def _split_authority(authority: str) -> tuple[str, str] | None:
    """host[:port] → (小写 host, 端口原样)；缺端口/空 host → None。"""
    if not authority or not isinstance(authority, str):
        return None
    text = authority.strip()
    if text.startswith("["):  # IPv6 [::1]:8791
        end = text.find("]")
        if end < 0:
            return None
        host = text[1:end]
        port = text[end + 1:]
        if not port.startswith(":"):
            return None
        return (host.lower(), port[1:])
    if ":" not in text:
        return None
    host, _, port = text.rpartition(":")
    if not host or not port:
        return None
    return (host.lower(), port)


def host_allowed(host_header: str | None, allowed_hosts) -> bool:
    """L1：Host 仅允许配置的本机名称与**准确端口**；不接受任意解析到
    loopback 的域名；大小写不敏感（host 部分），端口必须逐字一致。"""
    got = _split_authority(host_header or "")
    if got is None:
        return False
    for candidate in allowed_hosts or []:
        want = _split_authority(candidate)
        if want is not None and want == got:
            return True
    return False


def normalize_origin(origin: str | None) -> str | None:
    """scheme://host[:port] 精确归一化（host 小写）；非该形状 → None。"""
    if not origin or not isinstance(origin, str):
        return None
    text = origin.strip()
    if text.lower() == "null":
        return "null"
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://([^/?#]+)$", text)
    if not match:
        return None
    scheme, authority = match.group(1).lower(), match.group(2)
    parsed = _split_authority(authority)
    if parsed is None:
        return None
    host, port = parsed
    if port in ("80", "443") and scheme in ("http", "https"):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def allowed_origins(allowed_hosts, scheme: str = "http") -> list[str]:
    """配置的本机 host:port → 允许 Origin 集合（精确，无通配）。"""
    result = []
    for candidate in allowed_hosts or []:
        parsed = _split_authority(candidate)
        if parsed is None:
            continue
        host, port = parsed
        if (scheme == "http" and port == "80") \
                or (scheme == "https" and port == "443"):
            result.append(f"{scheme}://{host}")
        else:
            result.append(f"{scheme}://{host}:{port}")
    return result


def origin_allowed(origin: str | None, allowed) -> bool:
    """精确 Origin 匹配；`Origin:*`/null/通配一律不接受。"""
    got = normalize_origin(origin)
    if got is None or got == "null":
        return False
    for candidate in allowed or []:
        want = normalize_origin(candidate)
        if want is not None and want == got:
            return True
    return False


def sec_fetch_site_allowed(sec_fetch_site: str | None, *,
                           write: bool) -> bool:
    """L1：`cross-site` 及 null 拒绝；写操作必须是显式 same-origin
    （顶层导航 "none" 不构成写通道）。None（非浏览器/旧客户端）在写通道
    由调用方按"精确 Origin+CSRF 必需"另行拒绝，读通道允许（顶层打开）。"""
    text = (sec_fetch_site or "").strip().lower()
    if text in ("cross-site", "null"):
        return False
    if write:
        return text == "same-origin"
    return True


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DebugConsoleSessions:
    """L1：随机浏览器会话（HttpOnly Cookie + 配套 CSRF）。

    这只是防跨站机制——没有账号、密码或角色选择。服务端只存令牌摘要
    （不存明文）；每次进程启动失效（内存态）；Cookie 属性固定
    HttpOnly/SameSite=Strict/Path=/；token 不进 URL。"""

    def __init__(self, *, ttl_seconds: int = DEFAULT_DEBUG_TTL_SECONDS,
                 clock=None):
        self._ttl = int(ttl_seconds)
        self._clock = clock or time.monotonic
        self._sessions: dict[str, dict] = {}  # cookie 摘要 → 会话

    def issue(self) -> tuple[str, str]:
        """签发 (cookie_token, csrf_token)；令牌只经本返回值交付一次。"""
        cookie_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = self._clock()
        self._sessions[_digest(cookie_token)] = {
            "csrf_digest": _digest(csrf_token),
            "created": now, "expires": now + self._ttl}
        return cookie_token, csrf_token

    def _live(self, cookie_token: str | None) -> dict | None:
        if not cookie_token or not isinstance(cookie_token, str):
            return None
        session = self._sessions.get(_digest(cookie_token))
        if session is None or self._clock() >= session["expires"]:
            self._sessions.pop(_digest(cookie_token), None) \
                if session else None
            return None
        return session

    def verify_session(self, cookie_token: str | None) -> bool:
        return self._live(cookie_token) is not None

    def verify_csrf(self, cookie_token: str | None,
                    csrf_header: str | None) -> bool:
        session = self._live(cookie_token)
        if session is None or not csrf_header \
                or not isinstance(csrf_header, str):
            return False
        return hmac.compare_digest(_digest(csrf_header),
                                   session["csrf_digest"])

    @staticmethod
    def cookie_value(token: str) -> str:
        """Set-Cookie 值（属性固定；不含 Secure——回环开发 HTTP 显式允许，
        LAN 部署须在前置 TLS 层加 Secure）。"""
        return (f"{DEBUG_COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age={DEFAULT_DEBUG_TTL_SECONDS}")

    def revoke_all(self) -> None:
        self._sessions.clear()


def evaluate_browser_request(*, method: str, host: str | None,
                             origin: str | None,
                             sec_fetch_site: str | None,
                             allowed_hosts, scheme: str = "http",
                             sessions: DebugConsoleSessions | None = None,
                             cookie_token: str | None = None,
                             csrf_header: str | None = None) -> dict:
    """L1 边界判定（服务器接线点；本函数不含 IO）。

    - Host 必须精确匹配配置本机名:端口；
    - `Sec-Fetch-Site: cross-site`/null 拒绝（读、写都拒绝）；
    - 写（POST/PUT/DELETE）：精确 Origin + 会话 Cookie + `X-Debug-CSRF`；
    - 读：有 Origin 时必须精确匹配；无 Origin（顶层导航/同源 GET）允许。
    通过返回 {"ok": True, "authenticated": bool}；否则 DebugBoundaryError。
    """
    if not host_allowed(host, allowed_hosts):
        raise DebugBoundaryError(
            "origin_rejected", "Host 与本机调试台配置不符")
    write = str(method).upper() not in ("GET", "HEAD", "OPTIONS")
    if not sec_fetch_site_allowed(sec_fetch_site, write=write):
        raise DebugBoundaryError(
            "origin_rejected", "跨站请求被拒绝（Sec-Fetch-Site）")
    authenticated = bool(sessions is not None
                         and sessions.verify_session(cookie_token))
    if write:
        if not origin_allowed(origin, allowed_origins(allowed_hosts, scheme)):
            raise DebugBoundaryError(
                "origin_rejected",
                "写操作要求精确本机 Origin（不接受跨站/通配/null）")
        if sessions is None:
            raise DebugBoundaryError(
                "authentication_required", "调试台会话未初始化", status=401)
        if not authenticated:
            raise DebugBoundaryError(
                "authentication_required", "缺少本机调试台会话 Cookie",
                status=401)
        if not sessions.verify_csrf(cookie_token, csrf_header):
            raise DebugBoundaryError(
                "csrf_invalid", "X-Debug-CSRF 缺失或不匹配")
    elif origin is not None and \
            not origin_allowed(origin, allowed_origins(allowed_hosts, scheme)):
        raise DebugBoundaryError(
            "origin_rejected", "读请求 Origin 与本机调试台不符")
    return {"ok": True, "authenticated": authenticated}


def strip_self_reported_identity(payload: dict) -> dict:
    """L2：浏览器 body 自报 principal/role/actor 等字段一律剥离（不作身份，
    不默认不覆盖）。返回去除字段后的副本（原输入不改）。"""
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items()
            if k not in SELF_REPORTED_IDENTITY_FIELDS}


def strip_self_reported_identity_headers(headers: dict) -> dict:
    if not isinstance(headers, dict):
        return {}
    return {k: v for k, v in headers.items()
            if str(k).lower() not in SELF_REPORTED_IDENTITY_HEADERS}


def local_console_actor(local_console_id: str, operation_id: str) -> dict:
    """L2：`actor_kind=local_console` 只由受信后端构造（稳定本机主体 +
    本次操作号）；调用方不得接受浏览器提供的等价字段。"""
    if not local_console_id or not operation_id:
        raise ValueError("local_console_id/operation_id 必填（受信后端构造）")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                    str(local_console_id)):
        raise ValueError("local_console_id 形状非法")
    return {"actor_kind": "local_console",
            "local_console_id": str(local_console_id),
            "operation_id": str(operation_id)}


def resource_owner(record: dict) -> str | None:
    """资源归属主体（L2 布局控制/原图/模板资产/包下载同一资源边界）。"""
    if not isinstance(record, dict):
        return None
    for key in ("owner_principal", "principal_id", "created_by",
                "owner", "reviewed_by"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    nested = record.get("owner")
    if isinstance(nested, dict) and isinstance(nested.get("id"), str):
        return nested["id"]
    return None


def ownership_allowed(record: dict, local_console_id: str, *,
                      write: bool) -> bool:
    """L2：同一资源边界读取/写入按归属一致检查；无归属共享资料只读；
    历史固定主体不被无条件升级（须走 migrate_console_owners）。"""
    owner = resource_owner(record)
    if not local_console_id:
        return False
    if owner is None:
        return not write
    return owner == local_console_id


def migrate_console_owners(records: list, *,
                           target_local_console_id: str,
                           deployment_proof: str,
                           apply: bool = False,
                           migrated_at: str | None = None) -> dict:
    """L2：历史资源主体迁移（dry-run/commit 纯函数）。

    - 仅能证明来自该本机部署的旧资源（owner==历史固定调试台主体 且
      record.deployment_fingerprint 与部署证明一致）可绑定新
      local_console_id，并保存原 owner 审计（owner_history）；
    - 其余（无法证明/其他 owner）保留只读隔离——不全量"接管所有旧 owner"；
    - apply=False 只输出计划不改任何记录；apply=True 返回迁移后的新记录
      列表（输入不动，由调用方持久化）。"""
    plan: list[dict] = []
    output: list[dict] = []
    candidates = 0
    migrated = 0
    isolated = 0
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                    str(target_local_console_id)):
        raise ValueError("target_local_console_id 形状非法")
    if not deployment_proof:
        raise ValueError("deployment_proof 必填（部署指纹/来源证明）")
    for record in records or []:
        if not isinstance(record, dict):
            plan.append({"key": None, "action": "isolated_readonly",
                         "reason": "record_not_object"})
            isolated += 1
            output.append(record)
            continue
        owner = resource_owner(record)
        proof = record.get("deployment_fingerprint") \
            or record.get("provenance")
        provable = (owner == LEGACY_CONSOLE_PRINCIPAL
                    and proof == deployment_proof)
        entry = {"key": record.get("key") or record.get("record_id"),
                 "owner_principal": owner,
                 "action": ("bind_local_console" if provable
                            else "isolated_readonly"),
                 "reason": ("provable_local_deployment" if provable
                            else "owner_or_provenance_unproven")}
        if provable:
            candidates += 1
        if provable and apply:
            new = dict(record)
            new["owner_principal"] = target_local_console_id
            history = list(record.get("owner_history") or [])
            history.append({"owner_principal": LEGACY_CONSOLE_PRINCIPAL,
                            "migrated_at": migrated_at or "",
                            "proof": deployment_proof})
            new["owner_history"] = history
            output.append(new)
            migrated += 1
        else:
            output.append(record)
            isolated += 1
    return {"apply": bool(apply), "plan": plan,
            "candidates": candidates,
            "migrated": migrated if apply else 0,
            "isolated": isolated,
            "records": (output if apply else None)}


def filter_export_entries(entries: list) -> tuple[list, list]:
    """L2：导出白名单过滤——只保留布局资料（template/region/adaptation 的
    record/asset）；任何条目含密钥类键（token/secret/凭据等）整条丢弃。
    返回 (allowed, dropped)。"""
    allowed: list = []
    dropped: list = []

    def forbidden_keys(node) -> str | None:
        if isinstance(node, dict):
            for key, value in node.items():
                if EXPORT_FORBIDDEN_KEY_RE.search(str(key)):
                    return str(key)
                found = forbidden_keys(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = forbidden_keys(item)
                if found:
                    return found
        return None

    for entry in entries or []:
        if not isinstance(entry, dict):
            dropped.append(entry)
            continue
        resource = entry.get("resource") or entry.get("owner") or {}
        kind = resource.get("kind")
        if kind not in EXPORT_ALLOWED_RESOURCE_KINDS:
            dropped.append(entry)
            continue
        bad = forbidden_keys(entry)
        if bad:
            dropped.append(entry)
            continue
        allowed.append(entry)
    return allowed, dropped
