"""R7-S2：正式 WX 浏览器链编排（隔离 docker 栈 + 真实 Chrome）。

栈（全部临时、仅隔离网络内互通；宿主只暴露回环 18799）：
- r7-wx：ragflow 镜像（含 PIL/numpy/sqlite3）跑仓库 WX 后端
  （仓库只读挂载 /wx、临时数据卷 /data、init-admin 测试凭据、0.0.0.0:18787
  仅容器网络内可达）。
- r7-fleet：nginx 假上游（回显 method/path）。
- r7-main：仓库 deploy/nginx-software.conf 测试副本（仅上游端点替换为
  隔离 IP——上游端点是部署输入；路由/白名单/Cookie 语义为被测物），
  前端正式 dist 只读挂载，发布 127.0.0.1:18799。

浏览器：本机 Chrome（channel）+ playwright-core，浏览器自管 Cookie。
产出：browser-json-v1 报告 + 截图（repair-round7/logs、artifacts）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import os

import pytest

REPO = Path(__file__).resolve().parents[2]
ROUND7 = REPO / "reports/wx-console-v1.10/repair-round7"
NGINX_IMAGE = "nginx:1.28-alpine"
PY_IMAGE = "infiniflow/ragflow:v0.26.4"
BASE_IP = "172.28.100."
PORT = 18799
ADMIN_PASS = "r7-admin-pass-2026"
STAFF_PASS = "r7-staff-temp-2026"


def _docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          check=check)


def _available():
    if shutil.which("docker") is None or shutil.which("node") is None:
        return False
    if not Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists():
        return False
    for image in (NGINX_IMAGE, PY_IMAGE):
        r = _docker("images", "-q", image, check=False)
        if r.returncode != 0 or not r.stdout.strip():
            return False
    if not (REPO / "frontend/node_modules/playwright-core/package.json").exists():
        return False
    if not (REPO / "frontend/dist/index.html").exists():
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _available(), reason="缺 docker/镜像/Chrome/node/playwright-core/dist 之一")


@pytest.fixture(scope="module")
def stack():
    suffix = uuid.uuid4().hex[:6]
    net = f"r7net-{suffix}"
    names = [f"r7-wx-{suffix}", f"r7-fleet-{suffix}", f"r7-main-{suffix}"]
    ip_wx, ip_fleet, ip_main = (f"{BASE_IP}20", f"{BASE_IP}21", f"{BASE_IP}22")
    work = Path(tempfile.mkdtemp(prefix="r7-stack-"))
    confd = work / "conf.d"
    confd.mkdir()
    try:
        _docker("network", "create", "--subnet", f"{BASE_IP}0/24", net)
        # WX 后端（容器内 python，仓库只读 + 临时数据卷）。
        # 账号认证按正式语义开启：CSRF secret（≥32B 测试值，仅隔离栈）、
        # 回环 HTTP 许可（隔离网络明文）、公共源=浏览器实际入口。
        wxdata = work / "wxdata"
        wxdata.mkdir()
        (wxdata / "csrf-secret.txt").write_text("ab" * 32 + "\n", encoding="ascii")
        _docker("run", "-d", "--rm", "--name", names[0], "--network", net,
                "--ip", ip_wx,
                "-e", f"R7_ADMIN_PASSWORD={ADMIN_PASS}",
                "-e", "WX_ACCOUNT_CSRF_SECRET_FILE=/data/csrf-secret.txt",
                "-e", "WX_ACCOUNT_ALLOW_LOOPBACK_HTTP=true",
                "-e", f"WX_PUBLIC_ORIGIN=http://127.0.0.1:{PORT}",
                "-v", f"{REPO}:/wx:ro",
                "-v", f"{wxdata}:/data",
                "--entrypoint", "python3", PY_IMAGE,
                "/wx/arm_fleet/tests/r7_wx_launcher.py")
        # 假 Fleet
        # 假 Fleet=外部 IO 替身：按契约形状返回（否则前端 .filter 崩溃，
        # 替身形状失真会被误报成产品缺陷）
        (confd / "fleet.conf").write_text(
            'server { listen 80;\n'
            'location = /v1/devices { default_type application/json; return 200 '
            '\'{"devices":[{"device_id":"arm-01","status":"ready","binding_mode":"simulation",'
            '"calibration_status":"validated","home_ok":true,"lease_id":null}]}\'; }\n'
            'location = /v1/system/status { default_type application/json; return 200 '
            '\'{"status":"ok","hardware_mode":"simulation","version":"r7-fake"}\'; }\n'
            'location = /v1/capabilities { default_type application/json; return 200 '
            '\'{"capabilities":["arm.tap","arm.double_tap","arm.swipe"]}\'; }\n'
            'location = /v1/takeovers { default_type application/json; return 200 \'{"items":[]}\'; }\n'
            'location / { default_type application/json; return 200 \'{}\'; }\n'
            '}\n', encoding="utf-8")
        _docker("run", "-d", "--rm", "--name", names[1], "--network", net,
                "--ip", ip_fleet,
                "-v", f"{confd / 'fleet.conf'}:/etc/nginx/conf.d/default.conf:ro",
                NGINX_IMAGE)
        # 主 nginx：模板测试副本（仅上游端点替换）+ 正式 dist
        template = (REPO / "deploy" / "nginx-software.conf").read_text(
            encoding="utf-8") \
            .replace("host.docker.internal:8788", f"{ip_fleet}:80") \
            .replace("host.docker.internal:18887", f"{ip_wx}:18787") \
            .replace('"http://127.0.0.1:5183" 1;',
                     f'"http://127.0.0.1:5183" 1;\n    "http://127.0.0.1:{PORT}" 1;')
        (confd / "software.conf").write_text(template, encoding="utf-8")
        (work / "environment-public.json").write_text(
            json.dumps({"id": "r7-software-acceptance", "name": "software-acceptance",
                        "stage": "software-acceptance", "hardware_enabled": False,
                        "customer_send_enabled": False, "production_ready": False}),
            encoding="utf-8")
        _docker("run", "-d", "--rm", "--name", names[2], "--network", net,
                "--ip", ip_main, "-p", f"127.0.0.1:{PORT}:80",
                "-v", f"{confd / 'software.conf'}:/etc/nginx/conf.d/default.conf:ro",
                "-v", f"{REPO / 'frontend/dist'}:/usr/share/nginx/html:ro",
                "-v", f"{work / 'environment-public.json'}:/etc/wx/environment-public.json:ro",
                NGINX_IMAGE)
        # 就绪必须探到 WX 上游真通（amd64 仿真启动慢，environment.json 只证
        # nginx 本地路由）；session 匿名 401/403 都证明上游已响应。
        base = f"http://127.0.0.1:{PORT}"
        import urllib.error
        import urllib.request
        deadline = time.monotonic() + 120
        ok = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(base + "/environment.json", timeout=2) as r:
                    env_ok = r.status == 200
                if env_ok:
                    req = urllib.request.Request(
                        base + "/sales-api/internal/wx-auth/v1/session")
                    try:
                        urllib.request.urlopen(req, timeout=3)
                    except urllib.error.HTTPError as e:
                        if e.code in (401, 403):
                            ok = True
                            break
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(1.5)
        if not ok:
            logs = _docker("logs", names[0], check=False).stdout[-1500:]
            raise RuntimeError(f"栈未就绪；WX 日志尾：\n{logs}")
        yield {"base": base, "suffix": suffix, "work": work}
    finally:
        for n in names:
            _docker("rm", "-f", n, check=False)
        _docker("network", "rm", net, check=False)
        shutil.rmtree(work, ignore_errors=True)


def _s5_evidence_dir(name: str, tmp_path):
    """S5 输出隔离（证据卡）：WX_TEST_EVIDENCE_DIR=run 专属目录（拒绝覆盖）；
    默认临时根。修复历轮固定路径（重跑曾覆盖 r7-s2-browser-wx.json 等
    artifacts——已在 runs.json artifact_note 如实注明并以重跑产物归档）。"""
    env = os.environ.get("WX_TEST_EVIDENCE_DIR", "").strip()
    root = Path(env) / name if env else tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if (root / "r7-s2-browser-wx.json").exists():
        raise AssertionError(f"证据文件已存在，拒绝覆盖：{root}")
    return root


def test_r7_s2_wx_browser_chain(stack, tmp_path):
    ev = _s5_evidence_dir("browser-json", tmp_path)
    report = ev / "r7-s2-browser-wx.json"
    proc = subprocess.run(
        ["node", str(Path(__file__).with_name("r7_browser_wx_chain.mjs")),
         stack["base"], str(report), str(ev), ADMIN_PASS, STAFF_PASS,
         str(REPO / "frontend")],
        cwd=str(REPO / "frontend"), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-3000:])
    data = json.loads(report.read_text(encoding="utf-8"))
    passed = [c for c in data["cases"] if c["status"] == "passed"]
    assert len(passed) == len(data["cases"]) == 11, \
        [c for c in data["cases"] if c["status"] != "passed"]
    # 请求轨迹关键断言（服务端视角经代理真实往返）
    urls = {(r["method"], r["url"], r["status"]) for r in data["requests"]}
    assert any(m == "POST" and u.endswith("/wx-auth/v1/login") and s == 200
               for m, u, s in urls), "登录请求未成功往返"
    assert any(u.endswith("/wx-users/v1/users") and s in (200,)
               for _, u, s in urls), "管理员用户管理未成功往返"
    assert (ev / "03-staff-created.png").exists(), "截图缺失"
