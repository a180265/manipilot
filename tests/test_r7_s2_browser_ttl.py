"""R7-S2 第三批：真实 TTL 绝对过期（独立栈 TTL=6s，端口 18802）。

独立于 auth2（默认 TTL）栈：本栈 WX 容器带 WX_ACCOUNT_SESSION_TTL_SECONDS=6
（部署输入/测试时钟缝——8h 绝对到期经真实登录/会话校验链推进；生产不配置
=28800 默认不变）。禁用/重置/重启在 auth2 栈验证，互不替代。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROUND7 = REPO / "reports/wx-console-v1.10/repair-round7"
NGINX_IMAGE = "nginx:1.28-alpine"
PY_IMAGE = "infiniflow/ragflow:v0.26.4"
BASE_IP = "172.28.102."
PORT = 18802
ADMIN_PASS = "r7c-admin-pass-2026"
STAFF_PASS = "r7c-staff-temp-2026"


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
    net = f"r7cnet-{suffix}"
    wx_name = f"r7c-wx-{suffix}"
    names = [wx_name, f"r7c-fleet-{suffix}", f"r7c-main-{suffix}"]
    ip_wx, ip_fleet, ip_main = (f"{BASE_IP}20", f"{BASE_IP}21", f"{BASE_IP}22")
    work = Path(tempfile.mkdtemp(prefix="r7c-stack-"))
    confd = work / "conf.d"
    confd.mkdir()
    try:
        _docker("network", "create", "--subnet", f"{BASE_IP}0/24", net)
        wxdata = work / "wxdata"
        wxdata.mkdir()
        (wxdata / "csrf-secret.txt").write_text("cd" * 32 + "\n", encoding="ascii")
        # 不加 --rm：docker restart 的 stop 阶段会触发 --rm 自动清除，
        # 重启用例将失去容器；清理由 teardown 显式 rm -f 承担
        _docker("run", "-d", "--name", wx_name, "--network", net,
                "--ip", ip_wx,
                "-e", f"R7_ADMIN_PASSWORD={ADMIN_PASS}",
                "-e", "WX_ACCOUNT_CSRF_SECRET_FILE=/data/csrf-secret.txt",
                "-e", "WX_ACCOUNT_ALLOW_LOOPBACK_HTTP=true",
                "-e", f"WX_PUBLIC_ORIGIN=http://127.0.0.1:{PORT}",
                "-e", "WX_ACCOUNT_SESSION_TTL_SECONDS=6",
                "-v", f"{REPO}:/wx:ro",
                "-v", f"{wxdata}:/data",
                "--entrypoint", "python3", PY_IMAGE,
                "/wx/arm_fleet/tests/r7_wx_launcher.py")
        (confd / "fleet.conf").write_text(
            "server { listen 80;\n"
            "location = /v1/devices { default_type application/json; return 200 "
            "'{\"devices\":[{\"device_id\":\"arm-01\",\"status\":\"ready\","
            "\"binding_mode\":\"simulation\",\"calibration_status\":\"validated\","
            "\"home_ok\":true,\"lease_id\":null}]}'; }\n"
            "location = /v1/system/status { default_type application/json; "
            "return 200 '{\"status\":\"ok\",\"hardware_mode\":\"simulation\"}'; }\n"
            "location = /v1/capabilities { default_type application/json; "
            "return 200 '{\"capabilities\":[\"arm.tap\"]}'; }\n"
            "location = /v1/takeovers { default_type application/json; "
            "return 200 '{\"items\":[]}'; }\n"
            "location / { default_type application/json; return 200 '{}'; }\n"
            "}\n", encoding="utf-8")
        _docker("run", "-d", "--rm", "--name", names[1], "--network", net,
                "--ip", ip_fleet,
                "-v", f"{confd / 'fleet.conf'}:/etc/nginx/conf.d/default.conf:ro",
                NGINX_IMAGE)
        template = (REPO / "deploy" / "nginx-software.conf").read_text(
            encoding="utf-8") \
            .replace("host.docker.internal:8788", f"{ip_fleet}:80") \
            .replace("host.docker.internal:18887", f"{ip_wx}:18787") \
            .replace('"http://127.0.0.1:5183" 1;',
                     f'"http://127.0.0.1:5183" 1;\n    "http://127.0.0.1:{PORT}" 1;')
        (confd / "software.conf").write_text(template, encoding="utf-8")
        (work / "environment-public.json").write_text(
            json.dumps({"id": "r7b-software-acceptance", "name": "software-acceptance",
                        "stage": "software-acceptance", "hardware_enabled": False,
                        "customer_send_enabled": False, "production_ready": False}),
            encoding="utf-8")
        _docker("run", "-d", "--rm", "--name", names[2], "--network", net,
                "--ip", ip_main, "-p", f"127.0.0.1:{PORT}:80",
                "-v", f"{confd / 'software.conf'}:/etc/nginx/conf.d/default.conf:ro",
                "-v", f"{REPO / 'frontend/dist'}:/usr/share/nginx/html:ro",
                "-v", f"{work / 'environment-public.json'}:/etc/wx/environment-public.json:ro",
                NGINX_IMAGE)
        base = f"http://127.0.0.1:{PORT}"
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
            logs = _docker("logs", wx_name, check=False).stdout[-1500:]
            raise RuntimeError(f"栈未就绪；WX 日志尾：\n{logs}")
        yield {"base": base, "wx_name": wx_name}
    finally:
        for n in names:
            _docker("rm", "-f", n, check=False)
        _docker("network", "rm", net, check=False)
        shutil.rmtree(work, ignore_errors=True)


def test_r7_s2_browser_ttl(stack, tmp_path):
    logs = ROUND7 / "logs"
    shots = None  # 归 _ev（输出隔离）
    import os as _os
    _env = _os.environ.get("WX_TEST_EVIDENCE_DIR", "").strip()
    _ev = (Path(_env) / "browser-ttl") if _env else (tmp_path / "browser-ttl")
    _ev.mkdir(parents=True, exist_ok=True)
    if (_ev / "r7-s2-browser-ttl.json").exists():
        raise AssertionError(f"证据文件已存在，拒绝覆盖：{_ev}")
    report = _ev / "r7-s2-browser-ttl.json"
    proc = subprocess.run(
        ["node", str(Path(__file__).with_name("r7_browser_wx_ttl.mjs")),
         stack["base"], str(report), str(shots), ADMIN_PASS,
         str(REPO / "frontend")],
        cwd=str(REPO / "frontend"), capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-3000:])
    data = json.loads(report.read_text(encoding="utf-8"))
    passed = [c for c in data["cases"] if c["status"] == "passed"]
    assert len(passed) == len(data["cases"]) == 1, \
        [c for c in data["cases"] if c["status"] != "passed"]
