#!/usr/bin/env python3
"""配对调试台页面生成器 + 静态服务（R3-17 简化投递；ime-pairing.v1）。

用法：
  python -B -m scripts.pairing_console_page --dir <邀请目录> --render   # 生成 index.html
  python -B -m scripts.pairing_console_page --dir <邀请目录> --serve    # LAN 只读静态服务

邀请目录里每个 ``invitation-*.json`` 是一份完整配对码对象（8 字段，与手机端
PairingCode 严格 schema 同形）。页面按卡片渲染：16 位标识（invite_secret 公开
前缀，43 位中的前 16 位，不可反推余量）、k40pair:// 二维码（相机扫码直达 App
预填）、以及长按复制的兜底文本。不存任何私钥；CA 证书本就是公开材料。
"""
from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import secrets
import uuid
import qrcode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

BADGE_LEN = 16
FIELDS = ("contract_version", "invitation_id", "invite_secret", "server_id",
          "https_origin", "ca_certificate_base64", "server_spki_sha256",
          "expires_at")


FLEET_HOST = os.environ.get("PAIRING_CONSOLE_FLEET_HOST", "127.0.0.1")
FLEET_PORT = int(os.environ.get("PAIRING_CONSOLE_FLEET_PORT", "8788"))
TRUST_FILE = os.environ.get(
    "ARMFLEET_IME_PAIRING_TRUST_FILE",
    "/Users/ye/Desktop/WX/.local/secrets/ime-pairing-trust.json")


def deep_link_url(code: dict) -> str:
    compact = json.dumps(code, ensure_ascii=False, separators=(",", ":"))
    return f"k40pair://pair?c={quote(compact, safe='')}"


def badge_of(code: dict) -> str:
    return str(code.get("invite_secret", ""))[:BADGE_LEN]


def qr_png_data_uri(text: str) -> str:
    import base64
    import io
    img = qrcode.make(text, error_correction=qrcode.constants.ERROR_CORRECT_L,
                      box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def render_card(code: dict) -> str:
    missing = [k for k in FIELDS if not code.get(k)]
    badge = badge_of(code)
    if missing:
        return (f"<div class='card bad'>邀请缺少字段 {html.escape(str(missing))}，"
                f"已跳过渲染</div>")
    qr = qr_png_data_uri(deep_link_url(code))
    raw = json.dumps(code, ensure_ascii=False, separators=(",", ":"))
    return f"""
<div class='card'>
  <div class='badge'>配对标识 <b>{html.escape(badge)}</b></div>
  <div class='meta'>origin {html.escape(str(code.get('https_origin')))} ·
      有效期至 {html.escape(str(code.get('expires_at')))}
      （过期作废，需重新生成）</div>
  <img class='qr' src='{qr}' alt='配对二维码'>
  <div class='hint'>手机相机扫码 → 点「打开 K40输入法」→ 核对标识一致 → 点确认配对。<br>
  扫码不可用时的手动兜底：在 App「任务服务器地址」填本页地址（浏览器地址栏的
  IP:端口）→ 点「获取配对码」→ 核对标识 → 点确认配对。也可长按下方文本全选复制粘贴。</div>
  <textarea readonly rows='4'>{html.escape(raw)}</textarea>
</div>"""


def _fleet_call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """本机回环调用 Fleet 管理口（配对管理=回环放行；无转发令牌部署）。"""
    conn = http.client.HTTPConnection(FLEET_HOST, FLEET_PORT, timeout=10)
    headers = {"Content-Type": "application/json"}
    body = json.dumps(payload).encode() if payload is not None else None
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        doc = json.loads(data.decode("utf-8"))
    except ValueError:
        doc = {"raw": data.decode("utf-8", "replace")[:400]}
    return resp.status, doc


def _fleet_list_phones() -> list[dict]:
    try:
        status, doc = _fleet_call("GET", "/v1/ime-phones")
    except OSError:
        return []
    return doc.get("items") or [] if status == 200 else []


def _load_trust() -> dict:
    return json.loads(Path(TRUST_FILE).read_text(encoding="utf-8"))


def _auto_pair(payload: dict) -> tuple[int, str, dict]:
    """零操作配对：服务端自生成密钥创建邀请，完整配对码只返回给请求方。

    用户核对 = 手机端与调试台「已连接设备」列表的设备 ID 比对；
    任务只派发到 arm 配置绑定的 ime_device_id，未绑定设备收不到任何任务；
    异常设备可在调试台吊销。邀请单次有效、5 分钟过期。
    """
    secret = secrets.token_urlsafe(32)
    request = {"request_id": str(uuid.uuid4()), "invite_secret": secret,
               "arm_id": payload.get("arm_id")}
    try:
        status, doc = _fleet_call("POST", "/v1/ime-pairing/invitations", request)
    except OSError as exc:
        return 502, "application/json", {"error": f"fleet_unreachable: {exc}"}
    if status not in (200, 201):
        return 502, "application/json", {"error": "fleet_rejected", "detail": doc}
    trust = doc.get("trust") or {}
    code = {
        "contract_version": "ime-pairing.v1",
        "invitation_id": doc["invitation_id"],
        "invite_secret": secret,
        "server_id": doc["server_id"],
        "https_origin": trust.get("https_origin"),
        "ca_certificate_base64": trust.get("ca_cert_b64"),
        "server_spki_sha256": trust.get("spki_sha256"),
        "expires_at": doc.get("expires_at"),
    }
    missing = [k for k, v in code.items() if not v]
    if missing:
        return 502, "application/json", {"error": f"incomplete_code:{missing}"}
    return 200, "application/json", code


def render_page(codes: list[dict], phones: list[dict] | None = None) -> str:
    cards = "\n".join(render_card(code) for code in codes) or \
        "<div class='card'>当前没有待配对邀请（自动配对不需要邀请卡）。</div>"
    phone_rows = ""
    for phone in (phones or []):
        state = html.escape(str(phone.get("state")))
        color = "#4cd964" if state == "active" else "#ffcc00"
        phone_rows += (
            f"<div class='card'><div class='badge'>设备 "
            f"<b>{html.escape(str(phone.get('phone_instance_id')))}</b></div>"
            f"<div class='meta'>服务端编号 {html.escape(str(phone.get('ime_device_id')))} · "
            f"状态 <span style='color:{color}'>{state}</span> · "
            f"更新 {html.escape(str(phone.get('updated_at')))}</div>"
            f"<div class='hint'>请在手机端核对「设备 ID」与此处一致。</div></div>")
    phones_html = (f"<h2>已连接设备（核对 ID）</h2>{phone_rows}" if phones
                   else "<h2>已连接设备（核对 ID）</h2><div class='card'>还没有设备连接。</div>")
    return f"""<!doctype html><html lang=zh><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Arm Fleet 配对调试台</title><style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;background:#111;color:#eee;
margin:0 auto;max-width:560px;padding:16px}}
h1{{font-size:20px}} .card{{background:#1c1c1e;border-radius:12px;padding:14px;
margin:12px 0}} .badge{{font-size:22px;letter-spacing:1px}}
.badge b{{font-family:Menlo,monospace;color:#4cd964;font-size:26px}}
.bad{{color:#ff6b6b}} .meta{{color:#999;font-size:12px;margin:6px 0}}
.qr{{display:block;background:#fff;padding:8px;border-radius:8px;width:260px}}
.hint{{font-size:12px;color:#bbb;margin:8px 0}}
textarea{{width:100%;background:#000;color:#aaa;border:1px solid #333;
border-radius:6px;font-size:10px;font-family:Menlo,monospace}}
</style><h1>Arm Fleet 配对调试台</h1>
<p class=meta>每台手机一张卡；标识 = 配对密钥前 16 位公开前缀，两端比对一致即确认。</p>
{cards}{phones_html}</html>"""


def load_codes(directory: Path) -> list[dict]:
    codes = []
    for path in sorted(directory.glob("invitation-*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(doc, dict):
            codes.append(doc)
    return codes


def main() -> None:
    parser = argparse.ArgumentParser(description="配对调试台页面")
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default=os.environ.get("IME_TLS_HOST", ""))
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    args.dir.mkdir(parents=True, exist_ok=True)
    if args.render or not args.serve:
        page = render_page(load_codes(args.dir))
        (args.dir / "index.html").write_text(page, encoding="utf-8")
        print(f"已生成 {args.dir / 'index.html'}")
    if args.serve:
        if not args.host:
            raise SystemExit("--serve 必须显式指定 --host（LAN IP），拒绝 0.0.0.0")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlparse_path(self.path)
                if path in ("/", "/index.html"):
                    # 动态渲染：邀请卡 + 已连接设备（核对 ID）
                    phones = _fleet_list_phones()
                    body = render_page(load_codes(args.dir), phones).encode("utf-8")
                    self._respond(200, body, "text/html; charset=utf-8")
                elif path == "/api/pairing-code":
                    # 手动兜底：App「获取配对码」按钮拉取当前待配对邀请
                    # （单邀请语义：最新一份；多台手机并行时以扫码为准）。
                    codes = load_codes(args.dir)
                    if not codes:
                        self._respond(404, b'{"error":"no_pending_invitation"}',
                                      "application/json")
                        return
                    raw = json.dumps(codes[-1], ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")
                    self._respond(200, raw, "application/json")
                else:
                    self.send_error(404)

            def _respond(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                path = urlparse_path(self.path)
                if path != "/api/v1/pairings/auto":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                code, ctype, out = _auto_pair(payload)
                self._respond(code, json.dumps(out, ensure_ascii=False).encode("utf-8"), ctype)

            def log_message(self, *_args) -> None:
                pass

        def urlparse_path(path: str) -> str:
            from urllib.parse import urlparse
            return urlparse(path).path

        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"配对调试台已启动：http://{args.host}:{args.port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
