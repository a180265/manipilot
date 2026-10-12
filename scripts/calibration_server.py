"""屏幕触摸标定服务器：手机浏览器打开标定页，机械臂点击，页面回传触摸坐标。

用法（电脑与手机同一局域网）：
  .venv/bin/python scripts/calibration_server.py [--host 0.0.0.0] [--port 8791]

接口：
- GET  /calibrate  标定页（全屏黑底十字线；每次触摸 POST /tap 上报坐标）
- POST /tap        记录一次触摸 {x, y, w, h, dpr, t}（CSS 像素 + 视口信息）
- POST /ready      页面人工点"就绪"后上报（脚本等到它才开始走机械臂）
- POST /reset      清空已记录触摸（锚定原点后调用，保证样本干净）
- GET  /status     {ready, viewport, taps_count}
- GET  /taps       触摸列表 JSON

安全：页面除"就绪"按钮外无任何交互元素，机械臂点击不会触发手机上任何其他功能；
服务仅限局域网使用，标定完成后请关闭。
"""
from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOCK = threading.Lock()
TAPS: list[dict] = []
READY: dict = {}
GRID_POINTS: dict = {}  # 网格注册页的十字标记（页面坐标已知），供图像↔屏幕注册

GRID_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>Arm Fleet 网格注册页</title>
<style>
  html, body { margin:0; padding:0; height:100%; background:#000; overflow:hidden;
    touch-action:none; -webkit-user-select:none; user-select:none; }
  #hud { position:fixed; top:env(safe-area-inset-top,6px); left:6px; right:6px;
    font-size:13px; color:#ff0; pointer-events:none; }
  .mark { position:fixed; width:70px; height:70px; margin:-35px 0 0 -35px; pointer-events:none; }
  .mark::before, .mark::after { content:""; position:absolute; background:#0f0; }
  .mark::before { left:33px; top:0; width:4px; height:70px; }
  .mark::after { top:33px; left:0; height:4px; width:70px; }
  .lbl { position:fixed; color:#0f0; font-size:16px; font-weight:bold; pointer-events:none;
    text-shadow:0 0 4px #000; }
</style>
</head>
<body>
<div id="hud">网格注册页：机械臂拍照用，请勿触碰</div>
<script>
const w = window.innerWidth, h = window.innerHeight;
const xs = [0.16, 0.5, 0.84].map(v => Math.round(v * w));
const ys = [0.14, 0.38, 0.62, 0.86].map(v => Math.round(v * h));
const pts = [];
let idx = 1;
ys.forEach(y => xs.forEach(x => pts.push({i: idx++, x, y})));
pts.forEach(p => {
  const m = document.createElement("div");
  m.className = "mark";
  m.style.left = p.x + "px"; m.style.top = p.y + "px";
  document.body.appendChild(m);
  const l = document.createElement("div");
  l.className = "lbl";
  l.style.left = (p.x + 14) + "px"; l.style.top = (p.y - 10) + "px";
  l.textContent = p.i;
  document.body.appendChild(l);
});
fetch("/gridpoints", {method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({viewport:{w, h, dpr: window.devicePixelRatio || 1}, points: pts})});
// 就绪心跳：网格页同样维持就绪状态，机械臂验证点击后页面会上报坐标
setInterval(() => fetch("/ready", {method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({w, h, dpr: window.devicePixelRatio || 1})}).catch(()=>{}), 5000);
fetch("/ready", {method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({w, h, dpr: window.devicePixelRatio || 1})});
document.addEventListener("touchmove", e => e.preventDefault(), {passive:false});
// 网格页也上报触摸：用于闭环验证（机械臂点击十字 → 页面回报坐标 → 与已知坐标比对）
document.addEventListener("touchstart", e => {
  const t = e.changedTouches[0];
  fetch("/tap", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({x:t.clientX, y:t.clientY, w, h, dpr: window.devicePixelRatio || 1, t: Date.now()})});
}, {passive:false});
</script>
</body>
</html>"""

PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>Arm Fleet 触摸标定</title>
<style>
  html, body { margin:0; padding:0; height:100%; background:#000; color:#0f0;
    font-family:-apple-system,"PingFang SC",sans-serif; overflow:hidden;
    touch-action:none; -webkit-user-select:none; user-select:none; }
  #stage { position:fixed; inset:0; }
  #hud { position:fixed; top:env(safe-area-inset-top,8px); left:8px; right:8px;
    font-size:14px; line-height:1.5; pointer-events:none; }
  #dot { position:fixed; width:44px; height:44px; margin:-22px 0 0 -22px; border:2px solid #0f0;
    border-radius:50%; display:none; pointer-events:none; }
  #readyBtn { position:fixed; left:50%; bottom:12%; transform:translateX(-50%);
    font-size:20px; padding:14px 34px; border-radius:10px; border:2px solid #0f0;
    background:transparent; color:#0f0; }
</style>
</head>
<body>
<div id="stage"></div>
<div id="hud">Arm Fleet 触摸标定页<br>状态：等待就绪</div>
<div id="dot"></div>
<button id="readyBtn">就绪（开始标定）</button>
<script>
const hud = document.getElementById("hud");
const dot = document.getElementById("dot");
const stage = document.getElementById("stage");
document.addEventListener("gesturestart", e => e.preventDefault());
document.addEventListener("touchmove", e => e.preventDefault(), {passive:false});

function viewport() {
  return { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio || 1 };
}

document.getElementById("readyBtn").addEventListener("click", async e => {
  e.stopPropagation();
  await sendReady();
  e.target.disabled = true;
  e.target.textContent = "已就绪 ✓（可刷新页面重新就绪）";
  hud.innerHTML = "已就绪。等待机械臂点击……<br>收到 0 点";
});

async function sendReady() {
  await fetch("/ready", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(viewport())});
}
// 就绪心跳：服务器重启或被重置后 5 秒内自动恢复，无需用户再点
setInterval(() => {
  if (document.getElementById("readyBtn") && document.getElementById("readyBtn").disabled) {
    sendReady().catch(() => {});
  }
}, 5000);

async function report(x, y) {
  const payload = {x, y, ...viewport(), t: Date.now()};
  try {
    await fetch("/tap", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)});
    dot.style.left = x + "px"; dot.style.top = y + "px"; dot.style.display = "block";
    setTimeout(() => dot.style.display = "none", 600);
    if (navigator.vibrate) navigator.vibrate(40);
    const n = hud.innerHTML.match(/收到 (\\d+) 点/);
    const count = n ? parseInt(n[1]) + 1 : 1;
    hud.innerHTML = "已就绪。等待机械臂点击……<br>收到 " + count + " 点，最近 (" +
      Math.round(x) + ", " + Math.round(y) + ")";
  } catch (err) {
    hud.innerHTML = "上报失败：" + err.message;
  }
}

stage.addEventListener("touchstart", e => {
  const t = e.changedTouches[0];
  report(t.clientX, t.clientY);
}, {passive:false});
stage.addEventListener("mousedown", e => report(e.clientX, e.clientY));

// 保持屏幕常亮（支持时）
try { navigator.wakeLock && navigator.wakeLock.request("screen"); } catch (e) {}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict) -> None:
        self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self) -> None:
        if self.path == "/calibrate":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/grid":
            self._send(200, GRID_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/gridpoints":
            with _LOCK:
                self._json(dict(GRID_POINTS))
        elif self.path == "/status":
            with _LOCK:
                self._json({
                    "ready": bool(READY),
                    "viewport": READY.get("viewport"),
                    "taps_count": len(TAPS),
                    "ready_at": READY.get("at"),
                })
        elif self.path == "/taps":
            with _LOCK:
                self._json({"taps": list(TAPS)})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if self.path == "/tap":
            data = self._body()
            if "x" in data and "y" in data:
                data["received_at"] = datetime.now().isoformat(timespec="milliseconds")
                with _LOCK:
                    TAPS.append(data)
                self._send(200, b"ok", "text/plain")
            else:
                self._send(400, b"missing x/y", "text/plain")
        elif self.path == "/ready":
            # 设置就绪状态；不清空触摸（页面就绪后每 5 秒心跳续期，
            # 服务器重启或 /reset 之后会自动恢复，无需用户再点按钮）
            data = self._body()
            with _LOCK:
                READY["viewport"] = {"w": data.get("w"), "h": data.get("h"), "dpr": data.get("dpr")}
                READY["at"] = datetime.now().isoformat(timespec="seconds")
            self._send(200, b"ok", "text/plain")
        elif self.path == "/gridpoints":
            data = self._body()
            if data.get("points") and data.get("viewport"):
                with _LOCK:
                    GRID_POINTS.clear()
                    GRID_POINTS.update(data)
                self._send(200, b"ok", "text/plain")
            else:
                self._send(400, b"missing points/viewport", "text/plain")
        elif self.path == "/reset":
            # 清空触摸与就绪状态：保证下一次标定只认"重置之后"新的页面就绪
            with _LOCK:
                TAPS.clear()
                READY.clear()
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（手机需能访问；默认全部网卡）")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"标定服务器已启动：手机浏览器打开 http://<本机局域网IP>:{args.port}/calibrate")
    print("页面点「就绪」后，运行 scripts/g_calibrate_screen.py 开始机械臂标定。")
    server.serve_forever()


if __name__ == "__main__":
    main()
