"""R3-14：本机调试台免登录边界（身份附件 L1/L2；layout_auth.py 纯函数）。

直接测 layout_auth 的可独立测试函数（motor_debug_server.py 的接线由总集成
人合入，见交付 patch）。断言：
- L1：回环 peer、精确 Host/Origin（不接受任意解析到 loopback 的域名/通配/
  任意端口）、Sec-Fetch-Site cross-site 与 null 拒绝；写必须精确 Origin +
  会话 Cookie + X-Debug-CSRF；读有 Origin 必须匹配、顶层打开允许；
- L1：随机浏览器会话（HttpOnly/SameSite=Strict/Path=/，服务端只存摘要，
  重启失效，token 不进 URL）——无账号/无密码/无角色；
- L2：`actor_kind=local_console` 只由受信后端构造；浏览器自报 principal/
  role 一律剥离；资源归属一致检查（两主体不串读串写）；历史主体迁移
  dry-run/commit（仅可证明的本机旧资源绑定新主体并保留原 owner 审计，
  其余只读隔离）；导出白名单（密钥类条目整条丢弃）。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from runtime.layout_auth import (
    DEBUG_COOKIE_NAME,
    DebugBoundaryError,
    DebugConsoleSessions,
    allowed_origins,
    evaluate_browser_request,
    filter_export_entries,
    host_allowed,
    is_loopback_address,
    local_console_actor,
    migrate_console_owners,
    normalize_origin,
    origin_allowed,
    ownership_allowed,
    resource_owner,
    sec_fetch_site_allowed,
    strip_self_reported_identity,
    strip_self_reported_identity_headers,
)

ALLOWED_HOSTS = ("127.0.0.1:8791", "[::1]:8791")
ALLOWED_ORIGINS = allowed_origins(ALLOWED_HOSTS)


# ---------------- L1：回环 / Host / Origin / Sec-Fetch-Site ----------------
def test_r3_14_loopback_only():
    assert is_loopback_address("127.0.0.1")
    assert is_loopback_address("127.9.9.9")
    assert is_loopback_address("::1")
    assert is_loopback_address("[::1]")
    assert is_loopback_address("::ffff:127.0.0.1")
    assert not is_loopback_address("192.168.1.5")
    assert not is_loopback_address("10.0.0.2")
    assert not is_loopback_address("::ffff:192.168.0.1")
    assert not is_loopback_address("example.com")
    assert not is_loopback_address(None)
    assert not is_loopback_address("127.0.0.1:8791")  # peer 不带端口


def test_r3_14_exact_host_matching():
    assert host_allowed("127.0.0.1:8791", ALLOWED_HOSTS)
    assert host_allowed("LOCALHOST:8791", ("localhost:8791",))
    assert host_allowed("[::1]:8791", ALLOWED_HOSTS)
    # 任意解析到 loopback 的域名不接受；任意端口不接受；缺端口不接受
    assert not host_allowed("evil.example:8791", ALLOWED_HOSTS)
    assert not host_allowed("127.0.0.1:8792", ALLOWED_HOSTS)
    assert not host_allowed("127.0.0.1", ALLOWED_HOSTS)
    assert not host_allowed(None, ALLOWED_HOSTS)


def test_r3_14_exact_origin_matching():
    assert normalize_origin("http://127.0.0.1:8791") == \
        "http://127.0.0.1:8791"
    assert normalize_origin("null") == "null"
    assert normalize_origin("*") is None
    assert normalize_origin("http://127.0.0.1") is None  # 缺端口非本机形状
    assert origin_allowed("http://127.0.0.1:8791", ALLOWED_ORIGINS)
    assert origin_allowed("http://[::1]:8791", ALLOWED_ORIGINS)
    assert not origin_allowed("http://127.0.0.1:8792", ALLOWED_ORIGINS)
    assert not origin_allowed("*", ALLOWED_ORIGINS)
    assert not origin_allowed("null", ALLOWED_ORIGINS)
    assert not origin_allowed("http://evil.example:8791", ALLOWED_ORIGINS)


def test_r3_14_sec_fetch_site_rules():
    assert sec_fetch_site_allowed("same-origin", write=True)
    assert not sec_fetch_site_allowed("cross-site", write=True)
    assert not sec_fetch_site_allowed("cross-site", write=False)
    assert not sec_fetch_site_allowed("null", write=False)
    assert not sec_fetch_site_allowed("none", write=True)  # 顶层导航非写通道
    assert sec_fetch_site_allowed("none", write=False)
    assert not sec_fetch_site_allowed(None, write=True)


# ---------------- L1：浏览器会话（防跨站；无账号无角色） ----------------
def test_r3_14_session_issue_verify_and_expiry():
    ticks = {"now": 1000.0}
    sessions = DebugConsoleSessions(ttl_seconds=3600,
                                    clock=lambda: ticks["now"])
    token_a, csrf_a = sessions.issue()
    token_b, csrf_b = sessions.issue()
    assert token_a != token_b and csrf_a != csrf_b
    assert len(token_a) >= 32 and len(csrf_a) >= 32
    assert sessions.verify_session(token_a)
    assert sessions.verify_csrf(token_a, csrf_a)
    assert not sessions.verify_csrf(token_a, csrf_b)
    assert not sessions.verify_csrf(token_b, csrf_a)
    assert not sessions.verify_session("tampered")
    assert not sessions.verify_session(None)
    # 过期
    ticks["now"] = 1000.0 + 3601
    assert not sessions.verify_session(token_a)
    assert not sessions.verify_csrf(token_a, csrf_a)
    # 服务端只存摘要：明文令牌不出现在内部表
    sessions2 = DebugConsoleSessions()
    cookie, _ = sessions2.issue()
    stored = [v for v in sessions2._sessions.values()]
    assert all(cookie not in str(v) for v in stored)
    # 重启失效：新实例不认旧会话
    fresh = DebugConsoleSessions()
    assert not fresh.verify_session(token_a)
    # Cookie 属性固定；token 不进 URL（属性串不用于任何 URL 拼接）
    set_cookie = DebugConsoleSessions.cookie_value(cookie)
    assert set_cookie.startswith(DEBUG_COOKIE_NAME + "=")
    assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie \
        and "Path=/" in set_cookie
    assert "Domain=" not in set_cookie


def test_r3_14_evaluate_browser_write_boundary():
    ticks = {"now": 0.0}
    sessions = DebugConsoleSessions(clock=lambda: ticks["now"])
    token, csrf = sessions.issue()

    def attempt(**kwargs):
        base = dict(method="POST", host="127.0.0.1:8791",
                    origin="http://127.0.0.1:8791",
                    sec_fetch_site="same-origin",
                    allowed_hosts=ALLOWED_HOSTS,
                    sessions=sessions, cookie_token=token, csrf_header=csrf)
        base.update(kwargs)
        try:
            return evaluate_browser_request(**base)
        except DebugBoundaryError as exc:
            return exc

    ok = attempt()
    assert ok["ok"] is True and ok["authenticated"] is True
    # 写通道逐项拒绝
    assert attempt(origin=None).code == "origin_rejected"
    assert attempt(origin="http://127.0.0.1:8792").code == "origin_rejected"
    assert attempt(origin="null").code == "origin_rejected"
    assert attempt(origin="http://evil.example:8791").code == "origin_rejected"
    assert attempt(sec_fetch_site="cross-site").code == "origin_rejected"
    assert attempt(host="evil.example:8791").code == "origin_rejected"
    assert attempt(csrf_header=None).code == "csrf_invalid"
    assert attempt(csrf_header="wrong").code == "csrf_invalid"
    assert attempt(cookie_token=None).code == "authentication_required"
    assert attempt(cookie_token="bogus").code == "authentication_required"


def test_r3_14_evaluate_browser_read_boundary():
    ticks = {"now": 0.0}
    sessions = DebugConsoleSessions(clock=lambda: ticks["now"])
    token, csrf = sessions.issue()

    def attempt(**kwargs):
        base = dict(method="GET", host="127.0.0.1:8791", origin=None,
                    sec_fetch_site=None, allowed_hosts=ALLOWED_HOSTS)
        base.update(kwargs)
        try:
            return evaluate_browser_request(**base)
        except DebugBoundaryError as exc:
            return exc

    # 顶层打开/同源无 Origin 允许（免登录读）
    assert attempt()["ok"] is True
    assert attempt(origin="http://127.0.0.1:8791",
                   sec_fetch_site="same-origin")["ok"] is True
    # 跨站/外站 Origin 读拒绝
    assert attempt(origin="http://evil.example:8791",
                   sec_fetch_site="cross-site").code == "origin_rejected"
    assert attempt(origin="null",
                   sec_fetch_site="null").code == "origin_rejected"
    assert attempt(origin="http://127.0.0.1:8792").code == "origin_rejected"
    assert attempt(host="evil.example:8791").code == "origin_rejected"


# ---------------- L2：受信后端主体 / 自报剥离 / 归属 / 迁移 / 导出 ----------------
def test_r3_14_local_console_actor_and_self_reported_stripping():
    actor = local_console_actor("debug-console-local", "op-123")
    assert actor == {"actor_kind": "local_console",
                     "local_console_id": "debug-console-local",
                     "operation_id": "op-123"}
    with pytest.raises(ValueError):
        local_console_actor("", "op-1")
    with pytest.raises(ValueError):
        local_console_actor("bad principal; drop table", "op-1")
    payload = {"deviceId": "arm-01", "principal_id": "root",
               "role": "admin", "actor_kind": "local_console",
               "operator_id": "boss", "reason": "保存粗框"}
    stripped = strip_self_reported_identity(payload)
    assert stripped == {"deviceId": "arm-01", "reason": "保存粗框"}
    assert payload["principal_id"] == "root"  # 原输入不改
    headers = strip_self_reported_identity_headers({
        "X-Debug-Principal": "root", "X-Debug-Role": "admin",
        "X-Layout-Console-Principal": "root", "Content-Type": "application/json"})
    assert headers == {"Content-Type": "application/json"}


def test_r3_14_ownership_consistent_for_two_consoles():
    record_a = {"key": "tpl-1", "owner_principal": "console-a"}
    record_shared = {"key": "tpl-shared"}
    assert ownership_allowed(record_a, "console-a", write=True)
    assert ownership_allowed(record_a, "console-a", write=False)
    assert not ownership_allowed(record_a, "console-b", write=False), \
        "两主体不得串读"
    assert not ownership_allowed(record_a, "console-b", write=True), \
        "两主体不得串写"
    assert ownership_allowed(record_shared, "console-b", write=False)
    assert not ownership_allowed(record_shared, "console-b", write=True)
    assert not ownership_allowed(record_a, "", write=False)


def test_r3_14_migration_dry_run_then_apply_with_audit():
    records = [
        {"key": "tpl-legacy-1", "owner_principal": "motor-debug-console",
         "deployment_fingerprint": "deploy-abc"},
        {"key": "tpl-legacy-2", "owner_principal": "motor-debug-console",
         "provenance": "deploy-abc"},  # 备用键名也可证明
        {"key": "tpl-foreign", "owner_principal": "someone-else",
         "deployment_fingerprint": "deploy-abc"},
        {"key": "tpl-noproof", "owner_principal": "motor-debug-console"},
    ]
    snapshot = [dict(r) for r in records]
    dry = migrate_console_owners(
        records, target_local_console_id="console-new",
        deployment_proof="deploy-abc", apply=False)
    assert dry["apply"] is False and dry["records"] is None
    assert dry["candidates"] == 2
    assert records == snapshot, "dry-run 不得改动任何记录"
    applied = migrate_console_owners(
        records, target_local_console_id="console-new",
        deployment_proof="deploy-abc", apply=True, migrated_at="2026-10-05")
    assert applied["migrated"] == 2 and applied["candidates"] == 2
    migrated = [r for r in applied["records"]
                if r.get("key") in ("tpl-legacy-1", "tpl-legacy-2")]
    for record in migrated:
        assert record["owner_principal"] == "console-new"
        history = record["owner_history"]
        assert history[-1]["owner_principal"] == "motor-debug-console"
        assert history[-1]["proof"] == "deploy-abc"
    # 无法证明/他人资源保持只读隔离（不全量接管）
    untouched = [r for r in applied["records"]
                 if r.get("key") in ("tpl-foreign", "tpl-noproof")]
    assert [r["owner_principal"] for r in untouched] == \
        ["someone-else", "motor-debug-console"]
    assert all("owner_history" not in r for r in untouched)
    with pytest.raises(ValueError):
        migrate_console_owners(records, target_local_console_id="bad id",
                               deployment_proof="deploy-abc", apply=True)


def test_r3_14_export_whitelist_drops_secrets():
    entries = [
        {"path": "templates/tpl-1/1/record.json", "kind": "record",
         "resource": {"kind": "template",
                      "ref": {"id": "tpl-1", "version": 1}}},
        {"path": "templates/tpl-1/1/assets/a.png", "kind": "asset",
         "owner": {"kind": "template",
                   "ref": {"id": "tpl-1", "version": 1}},
         "asset": {"id": "a"}},
        {"path": "adaptations/arm-01/1/record.json", "kind": "record",
         "resource": {"kind": "adaptation",
                      "ref": {"id": "arm-01", "version": 1}}},
        {"path": "devices/arm-01/ime-credential.json", "kind": "record",
         "resource": {"kind": "phone",
                      "ref": {"id": "arm-01", "version": 1}}},
        {"path": "helper/token.json", "kind": "record",
         "resource": {"kind": "template",
                      "ref": {"id": "tpl-h", "version": 1}},
         "forwarder_token": "xyz"},
        {"path": "wx/session.json", "kind": "record",
         "resource": {"kind": "region",
                      "ref": {"id": "arm-01", "version": 1}},
         "session_cookie": "abc"},
    ]
    allowed, dropped = filter_export_entries(entries)
    assert [e["path"] for e in allowed] == [
        "templates/tpl-1/1/record.json",
        "templates/tpl-1/1/assets/a.png",
        "adaptations/arm-01/1/record.json"]
    assert len(dropped) == 3
