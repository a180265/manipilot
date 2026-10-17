#!/usr/bin/env python3
"""IME 受控 LAN HTTPS 入口（身份附件 N1/N2；deploy/ime-lan-tls.template.md）。

- 仅绑定 LAN IP（非 0.0.0.0），TLS 终止后转发 127.0.0.1:8788；
- 按方法和完整路径只放行 Android 手机协议（身份校验在应用层）；
  其余一律 403——Fleet 管理口不透传 LAN；
- 剥离可伪造的转发/身份头（X-Ime-Forwarded/X-Layout-Forwarder-Token/X-Debug-*）；
- 每请求 Connection: close（不维护上游 keep-alive；OkHttp 兼容）。

启动：env IME_TLS_HOST=<LAN_IP> IME_TLS_PORT=8443 IME_TLS_CERT=... IME_TLS_KEY=... \
  python -B -m scripts.ime_lan_tls_proxy
"""
from __future__ import annotations

import http.client
import os
import re
import select
import socket
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

# Match full encoded paths and HTTP methods, never a management-bearing prefix.
# IDs are the URL-safe installation/session/task identifiers used by Android.
_ID = r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*"
PHONE_ROUTES = {
    "GET": re.compile(
        rf"/api/v1/(?:health|gesture-protocol|pairings/{_ID}|"
        rf"devices/{_ID}/(?:tasks/next|calibration|geometry/requests/next|gesture-command)|"
        rf"tasks/{_ID}/control/next|gesture-sessions/{_ID}/receipts/{_ID})"),
    "POST": re.compile(
        rf"/api/v1/(?:pairings/(?:confirm|{_ID}/activate)|"
        rf"devices/{_ID}/(?:heartbeat|geometry(?:/sessions)?|gesture-client)|"
        rf"tasks/{_ID}/(?:status|control/ack)|calibrations/{_ID}/(?:touch|client)|"
        rf"gesture-sessions/{_ID}/(?:ready|trace|page-event))"),
}
UPSTREAM_CONNECT_TIMEOUT = 5.0
UPSTREAM_READ_TIMEOUT = 30.0  # Server wait=20s; Android read=25s.

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
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def _watch_disconnect(self, upstream_socket, finished, disconnected) -> None:
        # The request body is fully read and this connection is one-request-only.
        # Reading TLS close_notify/EOF works for SSLSocket; MSG_PEEK does not.
        while not finished.is_set():
            try:
                readable, _, _ = select.select([self.connection], [], [], .1)
                if not readable:
                    continue
                self.connection.recv(1)
                # EOF or unexpected pipelined input ends this one-request channel.
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            except OSError:
                pass
            if not finished.is_set():
                disconnected.set()
                try:
                    # close() alone cannot interrupt HTTPResponse's makefile reader.
                    upstream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return

    def _proxy(self, method: str) -> None:
        self.close_connection = True
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        allowed = PHONE_ROUTES.get(method)
        if (parsed.scheme or parsed.netloc or parsed.params or parsed.fragment or
                allowed is None or allowed.fullmatch(path) is None):
            self._reject(403, "lan_not_allowed", "LAN 入口仅开放手机白名单路由")
            return
        forward_path = path + ("?" + parsed.query if parsed.query else "")
        if self.headers.get("Transfer-Encoding"):
            self._reject(400, "invalid_body", "不支持分块请求体")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reject(400, "invalid_body", "请求体长度无效")
            return
        if length < 0 or length > MAX_BODY:
            self._reject(413, "body_too_large", "请求体超出上限")
            return
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            return
        headers = {}
        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in HOP_HEADERS or lowered in STRIP_HEADERS or lowered.startswith("x-debug-"):
                continue
            headers[name] = value
        headers["Connection"] = "close"
        conn = http.client.HTTPConnection(
            UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_CONNECT_TIMEOUT)
        finished, disconnected = threading.Event(), threading.Event()
        watcher = None
        downstream_timeout = self.connection.gettimeout()
        try:
            conn.connect()
            upstream_socket = conn.sock
            upstream_socket.settimeout(UPSTREAM_READ_TIMEOUT)
            conn.request(method, forward_path, body=body, headers=headers)
            self.connection.settimeout(.1)
            watcher = threading.Thread(
                target=self._watch_disconnect,
                args=(upstream_socket, finished, disconnected), daemon=True)
            watcher.start()
            resp = conn.getresponse()
            resp_body = resp.read()
            finished.set()
            watcher.join()
            self.connection.settimeout(downstream_timeout)
            if disconnected.is_set():
                return
            self.send_response(resp.status)
            for name, value in resp.getheaders():
                if name.lower() in HOP_HEADERS:
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(resp_body)
        except (OSError, http.client.HTTPException):
            finished.set()
            if watcher is not None:
                watcher.join()
            self.connection.settimeout(downstream_timeout)
            if not disconnected.is_set():
                self._reject(502, "upstream_unavailable", "上游请求失败")
        finally:
            finished.set()
            if watcher is not None:
                watcher.join()
            self.connection.settimeout(downstream_timeout)
            conn.close()

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
          f"→ {UPSTREAM_HOST}:{UPSTREAM_PORT}（手机协议精确白名单）",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
