#!/usr/bin/env python3
"""IME 受控 LAN HTTPS 入口（身份附件 N1/N2；deploy/ime-lan-tls.template.md）。

- 仅绑定 LAN IP（非 0.0.0.0），TLS 终止后转发 127.0.0.1:8788；
- 只放行手机白名单三类路由：/api/v1/pairings|devices|tasks（身份校验在应用层）；
  其余一律 403——Fleet 管理口不透传 LAN；
- 剥离可伪造的转发/身份头（X-Ime-Forwarded/X-Layout-Forwarder-Token/X-Debug-*）；
- 每请求 Connection: close（不维护上游 keep-alive；OkHttp 兼容）。

启动：env IME_TLS_HOST=<LAN_IP> IME_TLS_PORT=8443 IME_TLS_CERT=... IME_TLS_KEY=... \
  python -B -m scripts.ime_lan_tls_proxy
"""
from __future__ import annotations

import http.client
import os
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("IME_UPSTREAM_PORT", "8788"))
LISTEN_HOST = os.environ.get("IME_TLS_HOST", "")
LISTEN_PORT = int(os.environ.get("IME_TLS_PORT", "8443"))
CERT_FILE = os.environ.get("IME_TLS_CERT", "")
KEY_FILE = os.environ.get("IME_TLS_KEY", "")

# 手机白名单（与模板一致；逐条前缀，不做通配）
ALLOWED_PREFIXES = ("/api/v1/pairings", "/api/v1/devices", "/api/v1/tasks", "/api/v1/calibrations")
# 不向下游透传的可伪造头（大小写不敏感比较）
STRIP_HEADERS = {"x-ime-forwarded", "x-layout-forwarder-token", "x-debug-forwarded-for",
                 "x-forwarded-for", "x-forwarded-proto", "x-real-ip",
                 "x-layout-forwarder", "x-debug"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host"}

MAX_BODY = 1 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ime-lan-tls/1"

    def log_message(self, _format, *args) -> None:
        pass  # 不记日志（路径含设备号；审计在应用层）

    def _reject(self, status: int, code: str, message: str) -> None:
        body = (f'{{"code":"{code}","message":"{message}"}}').encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _proxy(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if not any(path == p or path.startswith(p + "/") for p in ALLOWED_PREFIXES):
            self._reject(403, "lan_not_allowed", "LAN 入口仅开放手机白名单路由")
            return
        if method == "GET" and parsed.query:
            forward_path = path + "?" + parsed.query
        else:
            forward_path = path
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            self._reject(413, "body_too_large", "请求体超出上限")
            return
        body = self.rfile.read(length) if length else b""
        headers = {}
        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in HOP_HEADERS or lowered in STRIP_HEADERS:
                continue
            headers[name] = value
        headers["Connection"] = "close"
        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=15)
            conn.request(method, forward_path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            self.send_response(resp.status)
            for name, value in resp.getheaders():
                lowered = name.lower()
                if lowered in HOP_HEADERS or lowered == "transfer-encoding":
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(resp_body)
            except OSError:
                pass
            conn.close()
        except OSError as exc:
            self._reject(502, "upstream_unavailable", f"上游不可达：{exc}")

    def do_GET(self) -> None:
        self._proxy("GET")

    def do_POST(self) -> None:
        self._proxy("POST")


def main() -> None:
    if not LISTEN_HOST:
        raise SystemExit("必须显式指定 IME_TLS_HOST（LAN IP），拒绝 0.0.0.0")
    if not CERT_FILE or not KEY_FILE:
        raise SystemExit("必须指定 IME_TLS_CERT / IME_TLS_KEY")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"IME LAN TLS 入口已启动：https://{LISTEN_HOST}:{LISTEN_PORT} "
          f"→ {UPSTREAM_HOST}:{UPSTREAM_PORT}（白名单 {'/'.join(p.strip('/') for p in ALLOWED_PREFIXES)}）",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
