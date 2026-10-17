"""电机调试台：人工点动 X/Y/Z + 统一校准向导 + 点哪打哪。

定位（与既有链路的关系）：
- 标定唯一路径 = 统一校准向导（含 v1.1 纯代码自动视觉闭环），产出
  calibration_bundle.json；旧版手动仿射标定（标定样本 + /api/calib/*）已删除，
  点哪打哪只认统一档案。
- 标定与会话锚点绑定：锚点来自 arm_daemon3 的 STATUS 查询（人工点「连接设备」
  打开串口那一刻生成；断开/拔线/守护进程重启都会更换）。锚点一致 -> 映射继续
  有效；不一致 -> 前端亮"需重标"，点哪打哪拒绝执行。
- 串口由 arm_daemon3（TCP 127.0.0.1:8795）独占，本服务只是它的 TCP 客户端。
- 摄像头帧走当前设备绑定的 CameraHelper 分路 URL，本服务代理给页面并做探头尖检测。

持久化（data/ 下，服务重启不丢）：
- devices/<设备>/calibration_bundle.json  统一校准档案（唯一映射事实）
- motor_debug_ops.jsonl                   全部操作日志（每行一个 JSON，可导出）

运行：nohup .venv/bin/python scripts/motor_debug_server.py > /tmp/motor_debug.log 2>&1 &
页面：http://127.0.0.1:8792
"""
from __future__ import annotations

import json
import asyncio
import hashlib
import math
import os
import re
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))

from adapters.screen_ime.camera_calibration import detect_probe_tip  # noqa: E402
LISTEN_PORT = 8792
LISTEN_HOST = os.environ.get("MOTOR_DEBUG_HOST", "127.0.0.1")
# 临时数据根（隔离实例/测试）：MOTOR_DEBUG_DATA_DIR 指向独立目录时，
# ops 日志/设备档案/向导状态全部落临时根，不写仓库 data/。默认不变。
DATA_DIR = Path(os.environ.get("MOTOR_DEBUG_DATA_DIR", "") or ROOT / "data")
OPS_PATH = DATA_DIR / "motor_debug_ops.jsonl"
PAGE_PATH = Path(__file__).resolve().parent / "motor_debug_page.html"
DEFAULT_DEVICE_ID = "arm-01"
# 统一校准向导：手机输入法服务（/api/v1/* 校准会话协议）地址
IME_BASE_URL = os.environ.get("IME_BASE_URL", "http://127.0.0.1:8788")

from drivers import calibration_bundle  # noqa: E402
from drivers import target_archive  # noqa: E402
from drivers import ui_targets  # noqa: E402
from drivers import gesture_profiles  # noqa: E402
from drivers import screen_gestures  # noqa: E402
from drivers import saved_screen_gestures  # noqa: E402
from drivers.daemon_arm import DaemonArmDriver  # noqa: E402
from drivers.real_camera import HttpFrameSource, RealCameraDriver  # noqa: E402
from drivers.probe_motion import ProbeMotionVerifier as CameraMotionVerifier, register_probe  # noqa: E402
from runtime.calibration_gate import check_live_calibration  # noqa: E402
from drivers.arm import gesture_motion_confirmed  # noqa: E402
from drivers import screen_edge  # noqa: E402
from drivers.calibration_wizard import (  # noqa: E402
    DEFAULT_REQUIRED_TARGETS,
    ImeCalibrationClient,
    CalibrationWizard,
    ImeError,
)
from adapters.vision.ocr import AppleVisionOcrEngine  # noqa: E402
from drivers.daemon_protocol import (  # noqa: E402
    DEFAULT_TARGET_MODES,
    split_segments,
    DOUBLE_TAP_FEATURE,
    LEASE_OWNER_DEBUG,
    TARGET_DISPLAY_NAMES,
    SWIPE_FEATURE,
    XY_STROKE_MAX,
    check_feed,
    check_stroke,
    check_tap,
)


def normal_device(raw) -> str:
    """设备编号清洗：只允许字母数字下划线连字符，空则回退默认设备。"""
    import re as _re

    text = _re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))
    return text or DEFAULT_DEVICE_ID


def strict_debug_device(raw) -> str:
    if not isinstance(raw, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw) is None:
        raise ValueError("设备编号无效，禁止自动切换到其他设备")
    device_config(raw)
    return raw

# 与 arm_daemon3 一致的安全限制
Z_FEED_RANGE = (30.0, 2000.0)
XY_FEED_RANGE = (100.0, 7000.0)
Z_STROKE_MAX = 10.0
# 人工点动仍限制为 30mm；画面 goto 使用 daemon_protocol 的 300mm 直达上限。
MANUAL_JOG_XY_MAX_MM = 30.0
# 点哪打哪单次总位移上限（映射错误时宁可拒绝也不能乱跑）
GOTO_TOTAL_MAX_MM = 300.0
# 已完成映射后的 XY 走位速度；Z 点击进给仍由点击参数单独控制。
CALIBRATED_TRAVEL_FEED = 5000.0

_LOCK = threading.RLock()
_REQUEST_CONTEXT = threading.local()
CONTEXT_TTL_SECONDS = 15 * 60
CONTEXTS: dict[str, dict] = {}
_BASIC_TICKETS: dict[str, dict] = {}
_BASIC_ACTIVE: set[str] = set()
BASIC_TICKET_SECONDS = 120

_ERROR_HTTP_STATUS = {
    "device_context_required": 409,
    "device_context_mismatch": 409,
    "device_maintenance_busy": 409,  # FIX-05（§4.1）：安装占用中，立即拒绝不排队
    "device_context_expired": 410,
    "input_acceptance_removed": 410,
    "camera_binding_mismatch": 409,
    "camera_device_mismatch": 409,
    "camera_unique_id_changed": 409,
    "camera_binding_revision_changed": 409,
    "camera_resolution_changed": 409,
    "camera_frame_stale": 503,
    "daemon_unreachable": 503,
    "daemon_device_mismatch": 409,
    "daemon_session_changed": 409,
    "device_removed": 409,
    "device_offline": 409,
    "device_paused": 409,
    "device_quarantined": 409,
    "device_maintenance": 409,
    "daemon_command_timeout": 504,
    "status_read_busy": 409,
    "status_read_cancelled": 409,
    "saved_gesture_invalid": 400,
    "saved_gesture_not_found": 404,
    "saved_gesture_archive_invalid": 409,
    "saved_gesture_calibration_invalid": 409,
    "saved_gesture_version_conflict": 409,
    "saved_gesture_binding_changed": 409,
    "upstream_error": 502,
    # R2-15/R2-17：布局冻结错误闭集（layout-error.v1）状态映射——上游状态
    # 原样透传给页面，不再统一折叠成 502（页面按 code 提示）。
    "layout_request_invalid": 422,
    "layout_permission_denied": 403,
    "layout_resource_missing": 404,
    "layout_revision_conflict": 409,
    "layout_request_conflict": 409,
    "layout_binding_changed": 409,
    "layout_device_busy": 409,
    "layout_preview_expired": 409,
    "layout_items_unverified": 409,
    "layout_source_unavailable": 503,
    "layout_archive_invalid": 503,
    "layout_storage_failed": 503,
    "camera_request_invalid": 422,
    "camera_not_found": 404,
}


def api_error(exc: Exception) -> tuple[int, dict]:
    message = str(exc)
    code = getattr(exc, "code", None) or (
        message if message in _ERROR_HTTP_STATUS else "debug_request_failed"
    )
    return _ERROR_HTTP_STATUS.get(code, 400), {
        "ok": False,
        "error": {"code": code, "message": message, "details": {}},
    }


# ---- FIX-05（camera-binding 附件 §4.1）：安装文件锁 ----
# data_root/installation_locks/{device_id}.lock：调试台全部动作入口（含旧模式与
# 自动校准）在动作认领前非阻塞取得共享锁，持有到最后动作/安全收尾结束；绑定写
# 取排他锁。锁根必须与 Fleet/帧服务从同一 data_root 派生（默认 ROOT/data）。
# STOP/急停/急停复位/输入关闭/只读查询不取此锁；获取失败立即 busy，不排队等待。
INSTALLATION_LOCK_ROOT = Path(os.environ.get("MOTOR_DEBUG_DATA_ROOT", "") or DATA_DIR)

# 动作端点（物理运动或向 Fleet 提交会产生物理动作的任务）：
_INSTALLATION_LOCKED_POST_PATHS = frozenset({
    "/api/recovery/attempt",      # 恢复复核（真实走位锚点验证）
    "/api/jog", "/api/tap", "/api/resume", "/api/hover", "/api/park", "/api/goto",
    "/api/gestures/test", "/api/gestures/review",
    "/api/auto-gestures/validate",  # Fleet 任务执行入口（排队物理动作）
    "/api/auto-navigate",           # Fleet 任务执行入口（提交导航任务）
    "/api/ui-targets/test", "/api/ui-targets/delete-test",
    "/api/gesture-profiles/test",
})
_INSTALLATION_LOCKED_GET_PATHS = frozenset({
    "/api/calibrated-scroll",     # Fleet 任务执行入口（提交校准直驱滑动任务）
})
# 自动校准入口：向导中会驱动机械/进入采集-动作闭环的 action（状态/保存/确认/
# 停止类不取锁——auto-state、cancel、auto-stop 等必须随时可用）。
_WIZARD_INSTALLATION_LOCKED_ACTIONS = frozenset({
    "start", "capture-hover", "z-test", "xy-test", "xy-fit", "xy-validate",
    "xy-finish", "edge-capture", "camera-fit", "visual-verify", "visual-verify-all",
    "target-test", "acceptance-execute", "finish", "auto-start", "auto-retry",
    "auto-validate", "auto-acceptance-resume", "auto-motion-resume",
    "auto-acceptance-execute",
})


def _shared_installation_lock(device: str):
    """本设备共享安装文件锁（动作侧）；busy 时抛 device_maintenance_busy。"""
    import scripts.camera_binding_v2 as camera_v2
    return camera_v2.installation_lock(INSTALLATION_LOCK_ROOT, device,
                                       exclusive=False)


def _enter_installation_guard(device: str, *, path: str):
    """动作端点在认领前进入共享安装锁；不取锁的路径返回 None（try/finally 释放）。"""
    if path not in _INSTALLATION_LOCKED_POST_PATHS \
            and path not in _INSTALLATION_LOCKED_GET_PATHS:
        return None
    lock = _shared_installation_lock(device)
    lock.__enter__()
    return lock


# ---------- 自动手势/自动导航代理（§13：调试台与正式走同一后端 8788） ----------
# 8788 提供 automatic-gestures / automatic-navigation / navigation-runs / 任务接口；
# 调试台只转发：操作者上下文由这里注入（§13.1 不要求操作者输入）。
# 代理写端点（POST 与 PUT）统一 require_context(motion=False)：跨站简单请求
# 伪造不了页面 contextId（评审 P1-15/N4）；本进程自身不触发串口/相机。
AUTO_PROXY_PREFIXES = ("/api/auto-gestures", "/api/auto-navigation", "/api/auto-navigate",
                       "/api/navigation-runs", "/api/navigation-template")
AUTO_NAVIGATION_ACTIONS = {"search_next", "search_previous", "return_top",
                           "back_to_list", "home"}
AUTO_NAVIGATION_PROFILE_ID = "wx_chat_list_auto"


def is_auto_proxy_path(path: str) -> bool:
    return path.startswith(AUTO_PROXY_PREFIXES)


class UpstreamProxyError(RuntimeError):
    """8788 上游失败：保留上游 code/status（布局冻结错误原样透传）。"""

    def __init__(self, message: str, *, status: int = 502,
                 code: str = "upstream_error"):
        super().__init__(message)
        self.message = message
        self.status = int(status)
        self.code = code


def _agent_json(method: str, path: str, body: dict | None = None) -> dict:
    """转发到平台 agent（8788）。失败统一 upstream_error。"""
    import urllib.error
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(IME_BASE_URL + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {"error": f"HTTP {exc.code}"}
        raise RuntimeError(payload.get("error") or payload.get("detail")
                           or f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(f"平台 agent 不可达（{IME_BASE_URL}）：{exc}") from exc


def _agent_passthrough(method: str, path: str, body: dict | None) -> tuple[int, dict]:
    """input-settings 代理（§4.2.3）：保留上游 HTTP 状态与错误键/trace_id，
    不把上游错误吞成统一 400；上游不可达=502 upstream_error。"""
    import urllib.error
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(IME_BASE_URL + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {"error": {"key": "upstream_error", "title": f"HTTP {exc.code}",
                                 "suggestion": "平台 agent 返回了无法解析的错误体"}}
        return exc.code, payload
    except Exception as exc:
        return 502, {"error": {"key": "upstream_error", "title": "平台 agent 不可达",
                               "suggestion": f"无法连接 {IME_BASE_URL}：{exc}", "trace_id": None}}


def serial_ports_payload(device: str) -> dict:
    """换口检测（用户拍板 2026-09-30）：经平台 discovery 扫描，列出全部可见
    串口并标注绑定/未绑定状态，供设备区下拉选择后一键重绑。"""
    scan = _agent_json(
        "POST", "/v1/devices/discovery/scan",
        {"operator_id": "调试台", "reason": "串口扫描",
         "correlation_id": "console-serial-scan",
         "client_request_id": f"scan-{int(time.time() * 1000)}"})
    ports = []
    absent = []
    for item in scan.get("unknown_ports") or []:
        ports.append({"path": item.get("path"),
                      "fingerprint": item.get("fingerprint"),
                      "vid": item.get("vid"), "pid": item.get("pid"),
                      "serial_number": item.get("serial_number"),
                      "bound_device": None, "state": "unbound"})
    for b in scan.get("bindings") or []:
        entry = {"path": b.get("serial_port"),
                 "fingerprint": b.get("hardware_fingerprint"),
                 "vid": b.get("vid"), "pid": b.get("pid"),
                 "serial_number": b.get("serial_number"),
                 "bound_device": b.get("device_id"),
                 "state": "present" if b.get("present") else "absent"}
        # 只列出物理在场的端口——拔掉/断电的绑定端口不可选（用户拍板 2026-09-30）
        if b.get("present"):
            ports.append(entry)
        else:
            absent.append(entry)
    hint = None
    for b in scan.get("bindings") or []:
        if b.get("device_id") == device and not b.get("present"):
            hint = "绑定串口当前不在场（已拔出或断电）；插上后点「扫描串口」即可发现"
    return {"device": device, "ports": ports, "absent_ports": absent,
            "hint": hint, "probe_available": scan.get("probe_available", True)}


_PROMOTE_SYNCED: dict[str, str] = {}


def _post_promote_platform_sync(device: str, run_id: str, *, background: bool = True) -> dict:
    """A5（2026-10-01 拍板）：PROMOTE 后自动执行平台同步链——
    calibration-status validated → home-check → resume。

    暗屏时 home-check 会失败并把设备闩进 degraded（home_not_confirmed 连
    resume 都拒）——P1-3（第五轮复审）：home-check 前先读设备
    camera_quality，waiting 则跳过 home-check+resume，保持 ready+camera_waiting
    交给 A6 自动唤醒；任何一步失败记录 ops 日志即止，不重试不阻塞页面轮询。
    每个成功 PROMOTE 的 run 只同步一次（_PROMOTE_SYNCED 去重）。"""
    steps = (
        ("calibration-status", {"status": "validated",
                                "operator_id": "自动同步",
                                "reason": f"PROMOTE {run_id} 后自动同步校准状态",
                                "correlation_id": run_id}),
        ("home-check", {"operator_id": "自动同步",
                        "reason": f"PROMOTE {run_id} 后自动归位检查",
                        "correlation_id": run_id}),
        ("resume", {"operator_id": "自动同步",
                    "reason": f"PROMOTE {run_id} 归位检查通过，自动恢复运行",
                    "correlation_id": run_id}),
    )

    def _run() -> dict:
        results: dict[str, str] = {}
        camera_waiting: bool | None = None  # 惰性探测：只在 home-check 前读一次
        for path, body in steps:
            if path == "home-check":
                # P1-3（第五轮复审）：暗屏时 home-check 必败并把设备闩进
                # degraded（home_not_confirmed 连 resume 都拒）——保持
                # ready+camera_waiting 交给 A6 自动唤醒；校准状态同步不受影响。
                if camera_waiting is None:
                    try:
                        device_view = _agent_json("GET", f"/v1/devices/{device}", None)
                        camera_waiting = (device_view or {}).get("camera_quality") == "waiting"
                    except Exception:
                        camera_waiting = False
                if camera_waiting:
                    results["home-check"] = "skipped: camera_waiting（暗屏，留待 A6 自动唤醒）"
                    results["resume"] = "skipped: camera_waiting"
                    log_op("post_promote_sync",
                           {"device": device, "run_id": run_id, "step": "home-check",
                            "skipped": "camera_waiting"}, ok=True,
                           detail="暗屏：跳过归位检查与 resume，保持 ready+camera_waiting 交 A6")
                    return results
            try:
                _agent_json("POST", f"/v1/devices/{device}/{path}", body)
                results[path] = "ok"
                log_op("post_promote_sync", {"device": device, "run_id": run_id,
                                             "step": path}, ok=True, detail="")
            except Exception as exc:
                results[path] = f"failed: {exc}"
                log_op("post_promote_sync", {"device": device, "run_id": run_id,
                                             "step": path}, ok=False, detail=str(exc))
                break
        return results

    if background:
        threading.Thread(target=_run, daemon=True,
                         name=f"post-promote-sync-{run_id}").start()
        return {"started": True}
    return _run()


def _maybe_post_promote_sync(device: str, view: dict) -> None:
    """auto-state 轮询钩子：观测到 PROMOTED 即触发一次平台同步链。"""
    if view.get("state") != "PROMOTED" or not view.get("runId"):
        return
    run_id = str(view["runId"])
    if _PROMOTE_SYNCED.get(run_id) == device:
        return
    _PROMOTE_SYNCED[run_id] = device
    _post_promote_platform_sync(device, run_id)


def _validate_serial_port_form(port: str) -> None:
    """B2（2026-10-01 backlog）：免 device-context 写入口——port 必须是
    串口设备形态，防误操作把任意字符串写进绑定表并触发 attach 致设备离线。"""
    import re as _re
    if not _re.fullmatch(r"/dev/(?:cu|tty)\.[A-Za-z0-9._\-]+", port or ""):
        raise ValueError(f"串口路径形态不合法（须为 /dev/cu.* 或 /dev/tty.*）：{port!r}")


def serial_bind_payload(device: str, port: str) -> dict:
    """换口重绑：持久化 devices.json 串口 + 经平台 attach 热重挂新端口。

    B1（2026-10-01 backlog）：只改静态绑定表中目标设备一条——合并视图
    （静态白名单+注册中心快照）里的易变运行时字段绝不回写磁盘；动态注册
    设备（静态表无条目）不在此端点换口。"""
    if not device or not port:
        raise ValueError("serial-bind 需要 device 与 port")
    _validate_serial_port_form(port)
    configs = _merged_device_configs()
    cfg = configs.get(device)
    if cfg is None:
        raise ValueError(f"设备 {device} 未登记")
    old_port = cfg.get("serial_port")
    from runtime.config import load_device_configs, save_device_configs, PROJECT_ROOT
    disk_entries = load_device_configs(PROJECT_ROOT)
    target = next((item for item in disk_entries
                   if isinstance(item, dict) and item.get("device_id") == device), None)
    if target is None:
        raise ValueError(
            f"设备 {device} 不在静态绑定表（configs/devices.json），不能在此换口；"
            "动态注册设备的串口改绑请走平台注册流程")
    # 写前最后一次重读（上面 load 即最终读）：最小化与 8788 平台整表写的竞窗
    target["serial_port"] = port
    save_device_configs(PROJECT_ROOT, disk_entries)
    log_op("serial_bind", {"device": device, "port": port,
                           "old_port": old_port}, ok=True)
    try:
        _agent_json("POST", f"/v1/devices/{device}/attach", {
            "serial_port": port,
            "operator_id": "调试台",
            "reason": f"串口换口重绑 {old_port} → {port}",
            "correlation_id": "serial-bind",
            "client_request_id": f"bind-{int(time.time() * 1000)}",
        })
    except Exception as exc:
        print(f"attach 代理失败（daemon 将由监督器按新配置重启）：{exc}", flush=True)
    return {"ok": True, "device": device, "serial_port": port,
            "old_serial_port": old_port,
            "note": "绑定已更新；daemon 将在新端口重连，随后走正常校准流程"}


_LAYOUT_TOKEN_ENV = "ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE"


def _layout_identity_headers() -> dict:
    """R2-23：调试台作为受信同机转发方声明已认证会话主体。

    令牌来自 ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE（与 Fleet 同值配置，同机
    helper 令牌约定）；未配置时不发送身份头（Fleet 侧安全拒绝并给出前置）。
    """
    token_file = os.environ.get(_LAYOUT_TOKEN_ENV) or ""
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if len(token) < 32:
        return {}
    # R3-14（L2）：只由受信后端构造 actor_kind=local_console 稳定主体；
    # 浏览器自报 principal/role 不作身份、不进入转发头。
    actor = _la.local_console_actor(_debug_console_id(),
                                    f"proxy-{uuid.uuid4()}")
    return {"X-Layout-Forwarder-Token": token,
            "X-Layout-Console-Principal": actor["local_console_id"]}


_LAYOUT_PATH_PREFIX = "/v1/"
# 调试台会经代理调用的布局 GET 路由公共头（routes.json 权威版本的镜像）
_LAYOUT_GET_VERSIONS = (
    ("/phone-search-region/versions/", "phone-search-region-read.v1"),
    ("/phone-search-region", "phone-search-region-read.v1"),
    ("/layout-adaptation/versions/", "device-layout-read.v1"),
    ("/layout-adaptation", "device-layout-read.v1"),
)


def _layout_get_contract_version(path: str) -> str | None:
    # /phone-search-region/versions/{n}：路径以版本号结尾，用包含匹配
    # （与列表/读取同一 read.v1 契约版本）。
    if "/phone-search-region/versions" in path:
        return "phone-search-region-read.v1"
    for suffix, version in _LAYOUT_GET_VERSIONS:
        if path.endswith(suffix):
            return version
    return None


def _auto_proxy(method: str, path: str, body: dict | None = None) -> dict:
    """urllib 转发到 IME_BASE_URL（5s 超时）；失败保留上游冻结错误码/状态。"""
    import urllib.error
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(IME_BASE_URL + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if path.startswith(_LAYOUT_PATH_PREFIX):
        for key, value in _layout_identity_headers().items():
            req.add_header(key, value)
        if method in ("GET", "DELETE"):
            # Fleet 契约公共头：关联 ID 代理生成（DELETE 契约亦必填）
            req.add_header("X-Correlation-Id", str(uuid.uuid4()))
            if method == "GET":
                version = _layout_get_contract_version(path)
                if version:
                    req.add_header("X-Layout-Contract-Version", version)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("code"):
                # 布局冻结错误外壳：原样透传 code/status/message
                raise UpstreamProxyError(
                    str(error.get("message") or error.get("code")),
                    status=exc.code, code=str(error["code"])) from exc
            if isinstance(payload, dict) and payload.get("code") \
                    and payload.get("contract_version") == "layout-error.v1":
                raise UpstreamProxyError(
                    str(payload.get("message") or payload["code"]),
                    status=exc.code, code=str(payload["code"])) from exc
            if payload.get("detail"):
                raise UpstreamProxyError(
                    f"上游拒绝（HTTP {exc.code}）：{payload['detail']}",
                    status=exc.code) from exc
        raise UpstreamProxyError(f"上游拒绝：HTTP {exc.code}",
                                 status=exc.code) from exc
    except OSError as exc:
        raise UpstreamProxyError(f"上游服务不可达（{IME_BASE_URL}）：{exc}",
                                 status=502) from exc


def _region_source_frame(device: str) -> dict:
    """FIX-12：粗框源预览——只取当前绑定相机的最新 v2 源帧（证据身份+原字节）。

    画框对象是本机械臂当前绑定相机的源预览：先读证据元数据（含真实采样时刻
    与来源身份），再按 evidence_id 取原字节；不做自由选相机入口。
    """
    import base64 as _b64
    import urllib.request as _rq
    meta = _auto_proxy("GET", f"/v1/devices/{device}/camera-source-frame")
    evidence_id = str(meta.get("evidence_id") or "")
    if not evidence_id:
        raise UpstreamProxyError("上游未返回可用的源帧证据")
    req = _rq.Request(
        f"{IME_BASE_URL}/v1/devices/{device}/camera-source-frame/{evidence_id}/image")
    try:
        with _rq.urlopen(req, timeout=8) as resp:
            raw = resp.read()
    except Exception as exc:
        raise UpstreamProxyError(f"源帧原图获取失败：{exc}") from exc
    source = dict(meta.get("source_frame") or {})
    return {"contract_version": "phone-search-region-source.v1",
            "device_id": device,
            "evidence_id": evidence_id,
            "jpeg_sha256": meta.get("jpeg_sha256"),
            "jpeg_base64": _b64.b64encode(raw).decode("ascii"),
            "source_frame": source,
            "captured_at": source.get("captured_at") or meta.get("captured_at")}


def _auto_operator_context(body: dict, default_reason: str) -> dict:
    """操作者上下文注入：页面不填写 operator/reason（§13.1）。"""
    return {
        "operator_id": "motor-debug-console",
        "reason": str(body.get("reason") or "").strip() or default_reason,
        "correlation_id": f"mdebug-{uuid.uuid4().hex[:16]}",
        "client_request_id": f"mdebug-{uuid.uuid4().hex[:16]}",
    }


def _utc_expires(minutes: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds")


def _submit_debug_navigation(device: str, body: dict) -> dict:
    """调试台动作按钮：提交单步 wx_auto_navigate（§13.4 同一 task_type，不另写动作循环）。

    页面只提供 action/navigationId/binding；任务外壳（task_id、幂等键、expires_at、
    correlation_id）与操作者上下文全部由代理注入，step_index 固定为 1。
    """
    action = str(body.get("action") or "").strip()
    if action not in AUTO_NAVIGATION_ACTIONS:
        raise ValueError(f"未知导航动作：{action or '(空)'}")
    binding = body.get("binding")
    if not isinstance(binding, dict) or not binding:
        raise ValueError("缺少自动导航绑定（binding），请先读取资格")
    navigation_id = str(body.get("navigationId") or "").strip()
    if not navigation_id:
        raise ValueError("缺少 navigationId（稳定请求编号）")
    payload = {
        "navigation_id": navigation_id,
        "binding": binding,
        "navigation_profile": {
            "profile_id": AUTO_NAVIGATION_PROFILE_ID,
            "version": int(body.get("navigationProfileVersion") or 1),
            "contract_version": "automatic-gesture-profile.v1",
        },
        "action": action,
        "step_index": 1,
    }
    if action in ("search_next", "search_previous"):
        target = body.get("target")
        if (isinstance(target, dict) and target.get("visible_name")
                and target.get("external_conversation_id")):
            payload["target"] = {"visible_name": str(target["visible_name"]),
                                 "external_conversation_id":
                                     str(target["external_conversation_id"])}
    task_id = f"navdebug-{uuid.uuid4().hex[:12]}"
    context = _auto_operator_context(body, "调试台单步导航")
    context["client_request_id"] = f"{task_id}-{navigation_id}"
    envelope = {
        "task_type": "wx_auto_navigate",
        "contract_version": "wx-auto-navigation.v1",
        "device_id": device,
        "task_id": task_id,
        "idempotency_key": context["client_request_id"],
        "payload": payload,
        "expires_at": _utc_expires(15),
        "correlation_id": context["correlation_id"],
        # API 在 schema 校验前取走 source，并用于 submitted_by/提交审计。
        # 完整原因随合法 policy_context 持久保存；请求编号与审计回退的幂等键一致。
        "source": context["operator_id"],
        "policy_context": context,
    }
    result = _auto_proxy("POST", "/v1/tasks", envelope)
    log_op("auto_navigate", {"device": device}, ok=True,
           detail={**context, "task_id": result.get("task_id"),
                   "action": action, "navigation_id": navigation_id})
    return result


def _reject_motion_during_auto_run(device: str, action: str) -> None:
    """校准 run 活动期间拒绝调试台运动（运动链审计 R6）：runner 持有运动权，
    交错指令会破坏采样/验证，且调试台 Z 基准（profile）独立于 run zSafe。
    STOP/ESTOP 不经此门禁，始终可用。"""
    try:
        wizard = get_wizard(device)
    except Exception:
        return
    try:
        active = wizard.auto_run_active()
    except Exception:
        return
    if active:
        raise RuntimeError(f"校准 run 进行中：{action} 已拒绝——等 run 结束或停止 run 后再操作"
                           "（急停/软停止不受影响）")


def _load_device_config_map() -> dict[str, dict]:
    from runtime.config import load_device_configs

    items = [dict(item) for item in load_device_configs() if item.get("device_id")]
    _validate_device_configs(items)
    return {str(item["device_id"]): item for item in items}


def _runtime_device_records() -> dict[str, dict]:
    """读取 Arm Fleet 设备注册中心的持久化快照。

    调试台继续支持直接注入/替换 DEVICE_CONFIGS（旧测试和离线调试路径），
    但真实运行时优先合并 armfleet.db 中的动态注册、attach 和 remove 结果。
    """
    try:
        from runtime.config import load_platform_config

        db_path = Path(os.environ.get("ARMFLEET_DB_PATH", str(load_platform_config().db_path)))
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT device_id, json FROM devices").fetchall()
        finally:
            conn.close()
        records = {}
        for device_id, payload in rows:
            try:
                item = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict) and item.get("device_id"):
                records[str(device_id)] = item
        return records
    except Exception:
        return {}


def _runtime_config(record: dict) -> dict:
    """把注册中心记录转换成调试台所需的兼容配置。"""
    device_id = str(record.get("device_id"))
    camera_id = str(record.get("camera_id") or f"cam-{device_id.removeprefix('arm-')}")
    daemon_addr = record.get("daemon_addr")
    return {
        "device_id": device_id,
        "name_display": record.get("name_display") or f"设备 {device_id}",
        "arm_slot": record.get("arm_slot"),
        "camera_id": camera_id,
        "camera_mode": record.get("camera_mode", "multi_channel"),
        "camera_frame_url": record.get("camera_frame_url") or f"http://127.0.0.1:8790/cameras/{camera_id}/latest.jpg",
        "camera_status_url": record.get("camera_status_url") or f"http://127.0.0.1:8790/cameras/{camera_id}/status",
        "daemon_addr": daemon_addr,
        "driver_mode": record.get("driver_mode", "daemon" if daemon_addr else "simulation"),
        "serial_port": record.get("serial_port"),
        "runtime_status": record.get("status"),
        "presence_state": record.get("presence_state", "present"),
        "device_mode": record.get("device_mode", "unmanaged"),
        "calibration_status": record.get("calibration_status", "unknown"),
        "ime_device_id": record.get("ime_device_id"),
        "enforce_input_gate": record.get("enforce_input_gate", True),
        "dynamic": True,
    }


def _merged_device_configs() -> dict[str, dict]:
    """动态注册中心覆盖同名静态配置，未知动态设备追加到清单。"""
    merged = {key: dict(value) for key, value in DEVICE_CONFIGS.items()}
    for device_id, record in _runtime_device_records().items():
        current = merged.get(device_id, {})
        current.update({k: v for k, v in _runtime_config(record).items() if v not in (None, "")})
        merged[device_id] = current
    return merged


def _validate_device_configs(items: list[dict]) -> None:
    seen_devices: set[str] = set()
    seen_cameras: set[str] = set()
    seen_real_daemons: set[str] = set()
    for item in items:
        device = str(item.get("device_id") or "").strip()
        camera = str(item.get("camera_id") or "").strip()
        if device in seen_devices:
            raise RuntimeError(f"设备配置重复 device_id：{device}")
        if camera in seen_cameras:
            raise RuntimeError(f"设备配置重复 camera_id：{camera}")
        seen_devices.add(device)
        seen_cameras.add(camera)

        real = item.get("binding_mode") == "real" or item.get("driver_mode") in ("real", "daemon")
        if not real:
            forbidden = [key for key in ("daemon_addr", "camera_frame_url",
                                         "camera_status_url", "camera_mode")
                         if item.get(key)]
            if forbidden:
                raise RuntimeError(
                    f"仿真设备 {device} 禁止配置真实硬件字段：{', '.join(forbidden)}")
            continue

        required = ("camera_id", "camera_mode", "camera_frame_url", "camera_status_url",
                    "expected_frame_width", "expected_frame_height", "expected_frame_fps",
                    "daemon_addr")
        missing = [key for key in required if item.get(key) in (None, "")]
        if missing:
            raise RuntimeError(f"真机设备 {device} 配置缺失：{', '.join(missing)}")
        mode = str(item["camera_mode"])
        if mode == "legacy_single" and device != "arm-01":
            raise RuntimeError("legacy_single 只允许 arm-01")
        if mode not in ("multi_channel", "legacy_single"):
            raise RuntimeError(f"真机设备 {device} camera_mode 无效：{mode}")
        if mode == "multi_channel":
            for key in ("camera_frame_url", "camera_status_url"):
                if f"/cameras/{camera}/" not in str(item[key]):
                    raise RuntimeError(f"真机设备 {device} 的 {key} 未指向 {camera}")
        daemon_addr = str(item["daemon_addr"])
        if daemon_addr in seen_real_daemons:
            raise RuntimeError(f"两台真机禁止共用 daemon_addr：{daemon_addr}")
        seen_real_daemons.add(daemon_addr)


DEVICE_CONFIGS = _load_device_config_map()


def device_config(device: str) -> dict:
    device = normal_device(device)
    cfg = _merged_device_configs().get(device)
    if cfg is None:
        raise RuntimeError(f"设备 {device} 未在 configs/devices.json 中登记")
    return cfg


def runtime_device_record(device: str) -> dict:
    """返回设备管理器当前持久化快照；找不到时返回空字典。"""
    return _runtime_device_records().get(normal_device(device), {})


def _sync_runtime_calibration(device: str, status: str, reason: str) -> None:
    """把调试台校准向导的最终结果同步回 Arm Fleet 设备注册中心。

    调试台是独立进程，不能直接持有 Runtime 对象；这里仅更新 devices 表中的
    JSON 快照，等运行时下一次读取/心跳时自然看到新的资格状态。历史校准文件
    仍由向导自己保存，二者不会互相覆盖。
    """
    if status not in {"validated", "due"}:
        return
    try:
        from runtime.config import load_platform_config

        db_path = Path(os.environ.get("ARMFLEET_DB_PATH", str(load_platform_config().db_path)))
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT json FROM devices WHERE device_id=?", (normal_device(device),)).fetchone()
            if not row:
                return
            record = json.loads(row[0])
            record["calibration_status"] = status
            record["calibration_status_display"] = "已校准" if status == "validated" else "待校准"
            record["calibration_validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if status == "validated" else None
            if status == "validated":
                record["device_mode"] = "ready_manual"
                record["device_mode_display"] = "可人工输入"
            else:
                record["device_mode"] = "unmanaged"
                record["device_mode_display"] = "未托管"
            record["status_reason"] = reason
            conn.execute("UPDATE devices SET json=? WHERE device_id=?",
                         (json.dumps(record, ensure_ascii=False), normal_device(device)))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # 校准结果文件已经保存，注册中心同步失败不能让调试台误报校准失败；
        # 状态轮询会继续显示本地档案，日志记录具体原因供现场排查。
        log_op("runtime_calibration_sync", {"status": status}, ok=False, detail=str(exc))


def _daemon_address(cfg: dict) -> tuple[str, int]:
    raw = str(cfg.get("daemon_addr") or "").strip()
    if not raw:
        slot = int(cfg.get("arm_slot") or 1)
        raw = f"127.0.0.1:{8794 + slot}"
    host, sep, port = raw.rpartition(":")
    if not sep or not host:
        raise RuntimeError(f"设备 {cfg.get('device_id')} daemon_addr 无效：{raw}")
    return host, int(port)


def camera_frame_url_for(device: str) -> str:
    cfg = device_config(device)
    camera_id = str(cfg.get("camera_id") or "")
    return str(cfg.get("camera_frame_url") or
               f"http://127.0.0.1:8790/cameras/{camera_id}/latest.jpg")


def camera_status_url_for(device: str) -> str:
    cfg = device_config(device)
    camera_id = str(cfg.get("camera_id") or "")
    return str(cfg.get("camera_status_url") or
               f"http://127.0.0.1:8790/cameras/{camera_id}/status")


def _context_view(context: dict) -> dict:
    return {
        "contextId": context["context_id"],
        "deviceId": context["device_id"],
        "cameraId": context.get("camera_id"),
        "cameraDeviceUniqueId": context.get("camera_device_unique_id"),
        "cameraFrameUrl": context.get("camera_frame_url"),
        "cameraStatusUrl": context.get("camera_status_url"),
        "daemonAddr": context.get("daemon_addr"),
        "daemonSession": context.get("daemon_session"),
        "bindingRevision": context.get("binding_revision"),
        "frameWidth": context.get("frame_width"),
        "frameHeight": context.get("frame_height"),
        "calibrationPath": str(device_dir_of(context["device_id"])) + "/calibration_bundle.json",
        "readyForRead": bool(context.get("ready_for_read")),
        "readyForMotion": bool(context.get("ready_for_motion")),
        "expiresInSeconds": max(0, int(context["expires_at"] - time.time())),
        "checks": list(context.get("checks") or []),
    }


def _load_camera_control_token() -> str | None:
    """跨进程查询采集会话视图所需的同机 helper 令牌（§4，与帧桥同配置）。"""
    path = os.environ.get("CAMERA_CONTROL_TOKEN_FILE", "").strip()
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _frame_bridge_serving_locally(device: str) -> bool:
    """R3-09①：本进程是否实际拥有、且正在服务配置指向的帧桥端口。

    只有真实绑定端口的帧桥模块实例才算同进程事实；V2_ENABLED 配置开关
    不是同进程事实（配置可以与帧桥部署在不同进程）。
    """
    frame = server_module_ref()
    ports_fn = getattr(frame, "active_server_ports", None)
    if not callable(ports_fn):
        return False
    try:
        ports = {int(port) for port in ports_fn()}
    except Exception:
        return False
    if not ports:
        return False
    try:
        parts = urllib.parse.urlsplit(camera_status_url_for(device))
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except Exception:
        return False
    host = (parts.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0") and port in ports


def _valid_session_view(session, camera_id: str) -> dict | None:
    """按冻结 current 接口的 session 结构严格校验；缺任一身份字段=缺事实。

    绑定/geometry/generation 任一缺失、身份不匹配、版本未知 → None（不手填）。
    """
    if not isinstance(session, dict):
        return None
    if str(session.get("camera_id") or "") != camera_id:
        return None
    for key in ("source_generation_id", "source_geometry_id", "camera_binding_id"):
        if not str(session.get(key) or "").strip():
            return None
    return session


def _camera_capture_session(device: str, cfg: dict) -> dict | None:
    """R3-09（FIX-07）：当前采集会话只读事实（受信来源几何/代际/绑定实例）。

    ① 本进程帧桥：只有当本进程模块确实持有该通道的完整活动会话时才采信
    （V2_ENABLED 配置不是事实；本进程空通道≠权威"无会话"，继续跨进程查询）。
    ② 跨进程：带机器身份的冻结 current 接口（helper 令牌），按真实
    `session` 响应结构解析并校验完整绑定/geometry/generation。
    查询失败/401/坏响应/无会话返回 None——不手填字段、不采用旧缓存。
    """
    frame = server_module_ref()
    camera_id = str(cfg.get("camera_id") or "")
    if not camera_id:
        return None
    if bool(getattr(frame, "V2_ENABLED", False)):
        try:
            local = frame.current_capture_session(camera_id)
        except Exception:
            local = None
        local_view = _valid_session_view(local, camera_id)
        if local_view is not None:
            return local_view
    token = _load_camera_control_token()
    if not token:
        return None
    try:
        base = camera_status_url_for(device).split("/cameras/", 1)[0]
        req = urllib.request.Request(
            f"{base}/cameras/{urllib.parse.quote(camera_id, safe='')}"
            "/capture-sessions/current",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            view = json.loads(resp.read())
    except Exception:
        return None
    if not isinstance(view, dict):
        return None
    # 按真实响应结构解析：会话在 `session` 字段下（不是顶层）
    return _valid_session_view(view.get("session"), camera_id)


def _v2_entry_from_file(bindings_path: Path, camera_id: str) -> dict | None:
    """从受信安装的 v2 绑定文件读取通道条目（文件事实，不读通道状态）。"""
    import scripts.camera_binding_v2 as v2
    try:
        payload = v2.load_bindings_v2(Path(bindings_path))
        return next((b for b in payload.get("bindings", [])
                     if b.get("cameraId") == camera_id), None)
    except (v2.CameraBindingError, OSError, ValueError):
        return None


def _remote_v2_binding_entry(device: str, camera_id: str) -> dict | None:
    """跨进程读取当前通道绑定实例（帧桥只读 binding-v2 接口；无缓存）。"""
    import scripts.camera_binding_v2 as v2
    token = _load_camera_control_token()
    if not token or not camera_id:
        return None
    try:
        base = camera_status_url_for(device).split("/cameras/", 1)[0]
        req = urllib.request.Request(
            f"{base}/cameras/{urllib.parse.quote(camera_id, safe='')}/binding-v2",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read())
    except Exception:
        return None
    if not isinstance(payload, dict) \
            or payload.get("contract") != v2.CONTRACT \
            or not isinstance(payload.get("binding"), dict):
        return None
    entry = payload["binding"]
    if not str(entry.get("cameraBindingId") or "").strip():
        return None
    return entry


def _camera_v2_binding(device: str, cfg: dict) -> dict:
    """B05+R3-09：读取当前通道的 v2 身份（binding/geometry/generation）。

    会话事实（geometry/generation）只来自 `_camera_capture_session` 的同一
    身份读取；绑定实例跨进程走受信远端只读接口，同进程部署（V2 配置开且
    无跨进程查询途径）读帧桥模块绑定文件。任一来源失败=缺事实（None），
    不手填、不用旧缓存；完全无受信来源时 enabled=False（旧部署兼容）。
    """
    frame = server_module_ref()
    camera_id = str(cfg.get("camera_id") or "")
    token = _load_camera_control_token()
    v2_config_on = bool(getattr(frame, "V2_ENABLED", False))
    if not v2_config_on and token is None:
        return {"enabled": False}
    session = _camera_capture_session(device, cfg)
    if v2_config_on and token is None:
        # 同进程部署：帧桥模块绑定文件即受信安装事实
        entry = _v2_entry_from_file(Path(frame.BINDINGS_PATH), camera_id)
    elif v2_config_on:
        # 跨进程：远端只读接口优先；失败才回退本机绑定文件（同机安装事实）
        entry = _remote_v2_binding_entry(device, camera_id)
        if entry is None:
            entry = _v2_entry_from_file(Path(frame.BINDINGS_PATH), camera_id)
    else:
        entry = _remote_v2_binding_entry(device, camera_id)
    return {"enabled": True,
            "cameraBindingId": (entry or {}).get("cameraBindingId"),
            "sourceGeometryId": (session or {}).get("source_geometry_id"),
            "sourceGenerationId": (session or {}).get("source_generation_id")}


def server_module_ref():
    import scripts.camera_frame_server as frame_server
    return frame_server


def create_device_context(device: str) -> dict:
    cfg = device_config(device)
    runtime_record = runtime_device_record(device)
    camera = camera_status(device)
    client = daemon_for(device)
    daemon = client.status()
    checks: list[dict] = []
    if camera.get("status") != "streaming":
        checks.append({"code": "camera_not_ready", "message": "对应摄像头没有实时画面"})
    if camera.get("camera_id") != cfg.get("camera_id"):
        checks.append({"code": "camera_binding_mismatch", "message": "摄像头逻辑身份不匹配"})
    if camera.get("device_id") != device:
        checks.append({"code": "camera_device_mismatch", "message": "摄像头设备身份不匹配"})
    if not camera.get("device_unique_id"):
        checks.append({"code": "camera_unbound", "message": "摄像头尚未绑定物理设备"})
    if camera.get("binding_revision") is None:
        checks.append({"code": "camera_binding_revision_missing", "message": "摄像头绑定版本缺失"})
    expected_width = cfg.get("expected_frame_width")
    expected_height = cfg.get("expected_frame_height")
    if expected_width and int(camera.get("width") or 0) != int(expected_width):
        checks.append({"code": "camera_resolution_changed", "message": "摄像头画面宽度与配置不一致"})
    if expected_height and int(camera.get("height") or 0) != int(expected_height):
        checks.append({"code": "camera_resolution_changed", "message": "摄像头画面高度与配置不一致"})
    expected_daemon = ":".join(map(str, _daemon_address(cfg)))
    actual_daemon = daemon.get("daemon_addr")
    if actual_daemon != expected_daemon:
        checks.append({"code": "daemon_address_mismatch", "message": "daemon 地址与设备配置不一致"})
    if daemon.get("connected") and daemon.get("device_id") != device:
        checks.append({"code": "daemon_device_mismatch", "message": "daemon 设备身份不匹配"})
    if runtime_record.get("presence_state") == "removed":
        checks.append({"code": "device_removed", "message": "设备已从设备管理器移除，请先重新挂载"})
    if runtime_record.get("status") in {"offline", "paused", "quarantined", "maintenance"}:
        status = runtime_record.get("status")
        checks.append({"code": f"device_{status}", "message": f"设备当前状态为 {status}，调试台只允许查看或校准"})
    context_id = "ctx-" + os.urandom(16).hex()
    # B05（T31/T32/T33）：v2 上下文携带单通道实例与采集代际——迟到回包按
    # 代际隔离不串机；缺来源（binding/geometry/generation 任一缺失）时
    # 不得生成 ready 上下文（只给查看/校准资格）。
    v2_binding = _camera_v2_binding(device, cfg)
    if v2_binding.get("enabled") and (
            not v2_binding.get("cameraBindingId")
            or not v2_binding.get("sourceGeometryId")
            or not v2_binding.get("sourceGenerationId")):
        checks.append({"code": "camera_source_missing",
                       "message": "v2 来源身份缺失：请完成采集会话握手后再校准"})
    context = {
        "context_id": context_id,
        "device_id": device,
        "camera_id": cfg.get("camera_id"),
        "camera_device_unique_id": camera.get("device_unique_id"),
        "camera_binding_id": v2_binding.get("cameraBindingId"),
        "source_geometry_id": v2_binding.get("sourceGeometryId"),
        "source_generation_id": v2_binding.get("sourceGenerationId"),
        "camera_frame_url": camera_frame_url_for(device),
        "camera_status_url": camera_status_url_for(device),
        "daemon_addr": expected_daemon,
        "daemon_session": daemon.get("session"),
        "binding_revision": camera.get("binding_revision"),
        "frame_width": camera.get("width"),
        "frame_height": camera.get("height"),
        "created_at": time.time(),
        "last_used_at": time.time(),
        "expires_at": time.time() + CONTEXT_TTL_SECONDS,
        "ready_for_read": not any(item["code"] in ("camera_binding_mismatch", "camera_device_mismatch") for item in checks),
        "ready_for_motion": not checks and bool(daemon.get("connected") and daemon.get("session")),
        "checks": checks,
        "presence_state": runtime_record.get("presence_state", "present"),
        "runtime_status": runtime_record.get("status"),
        "device_mode": runtime_record.get("device_mode", "unmanaged"),
        "calibration_status": runtime_record.get("calibration_status", "unknown"),
        "enforce_input_gate": runtime_record.get("enforce_input_gate", False),
    }
    with client._lock, _LOCK:
        CONTEXTS[context_id] = context
    return _context_view(context)


def _release_debug_lease_if_unbound(device: str) -> None:
    """Only release the shared debug lease after the last context is gone."""
    with _DAEMONS_LOCK:
        daemon = DAEMONS.get(device)
    if daemon is None:
        return
    with daemon._lock:
        now = time.time()
        with _LOCK:
            has_live_context = any(
                context.get("device_id") == device
                and context.get("expires_at", 0) > now
                for context in CONTEXTS.values()
            )
            lease_id = daemon.lease_id
        if has_live_context or not lease_id:
            return
        try:
            daemon.cmd("RELEASE", timeout=8)
        except Exception:
            pass
        daemon.lease_id, daemon.lease_expires = None, 0.0


def close_device_context(context_id: str) -> dict:
    with _LOCK:
        context = CONTEXTS.pop(context_id, None)
    if context:
        _release_debug_lease_if_unbound(context["device_id"])
    return {"closed": bool(context), "contextId": context_id}


def require_context(device: str, context_id: str | None, *, motion: bool = False,
                    emergency: bool = False) -> dict:
    context = require_context_local(device, context_id)
    # STOP/ESTOP 只核对本地目标绑定；摄像头故障和状态查询不能阻挡急停。
    if emergency:
        return context
    # 热插拔重新挂载后，旧页面上下文可能仍保留拔出时的静态门禁；
    # 设备已恢复 present/ready 时清理这些过期阻断项，避免必须切换设备。
    runtime_record = runtime_device_record(device)
    runtime_status = runtime_record.get("status") if runtime_record else None
    if runtime_status in {"paused", "quarantined", "maintenance"}:
        code = f"device_{runtime_status}"
        with _LOCK:
            stored = CONTEXTS.get(context_id)
            if stored:
                stored["checks"] = [
                    item for item in stored.get("checks", [])
                    if item.get("code") != code
                ] + [{"code": code,
                      "message": f"设备当前状态为 {runtime_status}，调试台只允许查看或校准"}]
                stored["runtime_status"] = runtime_status
                stored["ready_for_motion"] = False
        if motion:
            raise RuntimeError(code)
    # 兼容旧版/测试上下文：这类上下文没有摄像头绑定信息，不能在完整
    # 身份校验前被“daemon 已恢复”的快速路径放行。
    if motion and not context.get("camera_id"):
        for item in context.get("checks") or []:
            if item.get("code") in {"device_removed", "device_offline", "device_paused",
                                     "device_quarantined", "device_maintenance"}:
                raise RuntimeError(str(item["code"]))
    if runtime_record and runtime_record.get("presence_state") != "removed" \
            and runtime_record.get("status") not in {"offline", "paused", "quarantined", "maintenance"}:
        with _LOCK:
            stored = CONTEXTS.get(context_id)
            if stored:
                stored["checks"] = [item for item in stored.get("checks", [])
                                    if item.get("code") not in {
                                        "device_removed", "device_offline", "device_paused",
                                        "device_quarantined", "device_maintenance",
                                    }]
                stored["presence_state"] = runtime_record.get("presence_state", "present")
                stored["runtime_status"] = runtime_record.get("status")
                stored["ready_for_motion"] = True
                context = dict(stored)
    camera = camera_status(device)
    if camera.get("camera_id") != context.get("camera_id"):
        raise RuntimeError("camera_binding_mismatch")
    if camera.get("device_id") != device:
        raise RuntimeError("camera_device_mismatch")
    if camera.get("device_unique_id") != context.get("camera_device_unique_id"):
        raise RuntimeError("camera_unique_id_changed")
    if camera.get("binding_revision") != context.get("binding_revision"):
        raise RuntimeError("camera_binding_revision_changed")
    if camera.get("width") != context.get("frame_width") \
            or camera.get("height") != context.get("frame_height"):
        raise RuntimeError("camera_resolution_changed")
    daemon = daemon_for(device)
    daemon_status = daemon.status()
    # 设备管理器的持久化镜像和调试台请求存在最多一个心跳周期的延迟。
    # 如果 daemon 已明确报告“设备在位且已连接”，则拔插后残留的
    # device_removed/device_offline 只视为过期门禁；真正的会话变化仍由
    # daemon_session_changed 拦截，校准不会被绕过。
    if (
        runtime_record
        and daemon_status.get("connected")
        and daemon_status.get("device_present")
        and (
            runtime_record.get("presence_state") == "removed"
            or runtime_record.get("status") == "offline"
        )
    ):
        with _LOCK:
            stored = CONTEXTS.get(context_id)
            if stored:
                stored["checks"] = [item for item in stored.get("checks", [])
                                    if item.get("code") not in {"device_removed", "device_offline"}]
                stored["presence_state"] = "present"
                stored["runtime_status"] = "recovering"
                stored["ready_for_motion"] = True
                context = dict(stored)
    bound_session = context.get("daemon_session")
    current_session = daemon_status.get("session")
    # 手动点击“断开”后 daemon 仍可通过同一调试上下文重新连接；
    # 只有在 daemon 已有新会话时才判定为会话漂移并拒绝旧上下文。
    if bound_session and current_session is not None and current_session != bound_session:
        raise RuntimeError("daemon_session_changed")
    if daemon_status.get("device_id") != device:
        raise RuntimeError("daemon_device_mismatch")
    if motion:
        blocked = {"device_removed", "device_offline", "device_paused", "device_quarantined", "device_maintenance"}
        for item in context.get("checks") or []:
            if item.get("code") in blocked:
                raise RuntimeError(str(item["code"]))
        if camera.get("status") != "streaming":
            raise RuntimeError("camera_frame_stale")
        if (not daemon_status.get("connected") or not daemon_status.get("device_present")
                or not current_session):
            raise RuntimeError("daemon_unreachable")
    with _LOCK:
        stored = CONTEXTS.get(context_id)
        if stored:
            if not stored.get("daemon_session") and current_session:
                stored["daemon_session"] = current_session
            stored["last_used_at"] = time.time()
            stored["expires_at"] = time.time() + CONTEXT_TTL_SECONDS
    return context


def require_context_local(device: str, context_id: str | None) -> dict:
    """Validate only local context identity before entering a slow status query."""
    if not context_id:
        raise RuntimeError("device_context_required")
    with _LOCK:
        context = CONTEXTS.get(context_id)
        if context:
            context = dict(context)
    if not context:
        raise RuntimeError("device_context_expired")
    if context.get("device_id") != device:
        raise RuntimeError("device_context_mismatch")
    if context.get("expires_at", 0) <= time.time():
        with _LOCK:
            expired = CONTEXTS.pop(context_id, None)
        if expired:
            _release_debug_lease_if_unbound(device)
        raise RuntimeError("device_context_expired")
    bound_addr = context.get("daemon_addr")
    current_addr = ":".join(map(str, _daemon_address(device_config(device))))
    if bound_addr and bound_addr != current_addr:
        raise RuntimeError("device_context_mismatch")
    return context


# ---------- 守护进程 TCP 客户端 ----------

class DaemonCommandError(RuntimeError):
    """daemon 指令失败；动作类超时不能被解释为已失败或自动重放。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DaemonClient:
    """daemon TCP 客户端，分离普通、状态和急停通道。

    普通通道绑定运动租约并按请求串行；状态查询使用一次性只读连接，
    急停/停止/复位使用独立控制连接；恢复属于普通运动，必须持有租约。
    任何命令都只发送一次；断线或超时后绝不自动重放运动。
    """

    CONNECT_TIMEOUT = 3.0
    STATUS_TIMEOUT = 1.0
    CONTROL_TIMEOUT = 2.0
    # 0.25s 曾在 USB 链路抖动时高频脱靶（业主 2026-10-16 拍板放宽）：
    # 状态查询宁慢勿误，单拍失败=「状态查询中」闪断+坐标动作锁一拍。
    MPOS_TIMEOUT = 0.6
    MPOS_WAIT_TIMEOUT = 3.0
    STATUS_CANCEL_INTERVAL = 0.05

    def __init__(self, device_id: str, daemon_addr: tuple[str, int]) -> None:
        self.device_id = device_id
        self.daemon_addr = daemon_addr
        self.sock: socket.socket | None = None
        self.f = None
        self.session: str | None = None
        self.last_error: str | None = None
        self.lease_id: str | None = None
        self.lease_expires = 0.0
        self.last_motion_at = 0.0  # 闲置自动释放租约，把串口让给业务桥
        self._lock = threading.RLock()
        self._motion_state_lock = threading.RLock()
        self._motion_depth = 0
        self.motion_revision = 0
        self._retired = threading.Event()
        threading.Thread(target=self._lease_renew_loop, daemon=True).start()

    @property
    def motion_active(self) -> bool:
        # 位置读取也使用同一把串行锁，但不应被报告为真实运动。
        return self._motion_depth > 0

    @contextmanager
    def motion_scope(self):
        """整组运动串行；同线程嵌套允许，状态和急停不取普通锁。"""
        with self._lock, self._motion_state_lock:
            self._motion_depth += 1
            self.motion_revision += 1
            try:
                yield
            finally:
                self._motion_depth -= 1
                self.motion_revision += 1

    def _lease_renew_loop(self) -> None:
        """持有 debug 租约期间每 20 秒续期；闲置超过 60 秒自动释放，避免长期占住串口。"""
        while not self._retired.wait(20):
            if not self._lock.acquire(blocking=False):
                continue
            try:
                if self._retired.is_set() or self.lease_id is None:
                    continue
                if time.time() - self.last_motion_at > 60:
                    try:
                        self.cmd("RELEASE", timeout=8)
                        print("调试台闲置，运动租约已释放（让给业务桥）", flush=True)
                    finally:
                        self.lease_id, self.lease_expires = None, 0.0
                    continue
                try:
                    self.cmd("RENEW", timeout=8)
                    self.lease_expires = time.time() + 80
                except Exception:
                    self.lease_id, self.lease_expires = None, 0.0
            except Exception:
                self.lease_id, self.lease_expires = None, 0.0
            finally:
                self._lock.release()

    def ensure_debug_lease(self) -> None:
        """运动前确认本服务真的持有 debug 租约；失联后的本地缓存不能当作事实。"""
        with self._lock:
            self._ensure_debug_lease_locked()

    def _ensure_debug_lease_locked(self) -> None:
        self.last_motion_at = time.time()
        if self.lease_id:
            try:
                # daemon 按 TCP 连接绑定租约；串口/daemon 重连后，本地 lease_id
                # 可能仍然存在，但服务端已经回收。用 RENEW 做一次事实校验。
                json.loads(self.cmd("RENEW", timeout=8))
                self.lease_expires = time.time() + 80
                return
            except Exception:
                self.lease_id, self.lease_expires = None, 0.0
        try:
            # Unleased TCP channels may have expired while the console was
            # idle. Open a fresh channel before CLAIM, never replay motion.
            self._close_ordinary_channel()
            resp = json.loads(self.cmd(f"CLAIM {LEASE_OWNER_DEBUG} motor_debug_server"))
            self.lease_id = resp.get("lease_id")
            self.lease_expires = time.time() + 80
        except DaemonCommandError:
            self.lease_id, self.lease_expires = None, 0.0
            raise
        except RuntimeError as exc:
            self.lease_id, self.lease_expires = None, 0.0
            raise RuntimeError(f"调试运动被拒绝：{exc}") from exc

    def daemon_estop(self, reason: str) -> dict:
        return json.loads(self._control_cmd(f"ESTOP_LOCK {reason}"))

    def daemon_estop_reset(self, operator: str) -> dict:
        self.lease_id, self.lease_expires = None, 0.0
        return json.loads(self._control_cmd(f"RESET_ESTOP {operator}"))

    def _open_channel(self, timeout: float, *, deadline: float | None = None,
                      cancel_event: threading.Event | None = None
                      ) -> tuple[socket.socket, object]:
        self._check_current_binding()
        sock = f = None
        try:
            if deadline is not None:
                timeout = min(timeout, self._query_remaining(deadline, cancel_event))
            sock = socket.create_connection(self.daemon_addr, timeout=timeout)
            f = sock.makefile("rwb")
            welcome_bytes = (f.readline() if deadline is None else
                             self._read_query_line(sock, deadline, cancel_event))
            welcome = welcome_bytes.decode(errors="strict").strip()
            if not welcome.startswith("arm-daemon3"):
                raise RuntimeError(f"守护进程欢迎行异常：{welcome!r}")
            actual = next((part.removeprefix("device=") for part in welcome.split()
                           if part.startswith("device=")), None)
            if actual != self.device_id:
                raise RuntimeError(
                    f"daemon 设备身份不匹配：期望 {self.device_id}，实际 {actual or '未返回'}")
            self._check_current_binding()
            return sock, f
        except Exception:
            self._close_channel(sock, f)
            raise

    @staticmethod
    def _close_channel(sock: socket.socket | None, f) -> None:
        if sock is not None:
            try:
                # 先中断读写，避免 buffered close 再次冲刷失败的运动指令。
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _connect(self) -> None:
        self.close()
        sock, f = self._open_channel(self.CONNECT_TIMEOUT)
        self.sock, self.f = sock, f
        self.session = None
        self.last_error = None

    def close(self) -> None:
        with self._lock:
            self._close_ordinary_channel()

    def _close_ordinary_channel(self) -> None:
        self.lease_id, self.lease_expires = None, 0.0
        sock, f = self.sock, self.f
        self.sock, self.f, self.session = None, None, None
        self._close_channel(sock, f)

    def retire(self) -> None:
        """地址重绑定时终止旧连接和续租，不等待普通运动锁。"""
        self._retired.set()
        self._close_ordinary_channel()

    def _check_current_binding(self) -> None:
        if self._retired.is_set():
            raise DaemonCommandError("device_context_mismatch", "daemon 绑定已变化，旧连接已停用")

    def cmd(self, line: str, *, timeout: float = 60.0) -> str:
        """在普通通道发送一次指令；失败后关闭通道且不自动重放。"""
        self._validate_line(line)
        is_motion = line.split()[0].upper() in {"XY", "Z", "TAP", "RESUME"}
        with self._lock, self.motion_scope() if is_motion else nullcontext():
            self._check_current_binding()
            sent = False
            try:
                if self.sock is None:
                    self._connect()
                assert self.f is not None and self.sock is not None
                sock, f = self.sock, self.f
                sock.settimeout(timeout)
                sent = True
                f.write((line + "\n").encode())
                f.flush()
                resp = f.readline()
                if not resp or not resp.endswith(b"\n"):
                    raise RuntimeError("守护进程连接中断")
                text = resp.decode(errors="ignore").strip()
            except socket.timeout as exc:
                self.close()
                message = (
                    f"守护进程指令响应超时（{timeout:g}秒）：{line}；"
                    + ("指令未自动重放，动作结果未知" if sent else "连接未建立，指令未发送")
                )
                self.last_error = message
                raise DaemonCommandError("daemon_command_timeout", message) from exc
            except (OSError, RuntimeError) as exc:
                self.close()
                self.last_error = str(exc)
                raise DaemonCommandError(
                    "daemon_unreachable",
                    f"守护进程通信失败：{exc}；"
                    + ("指令未自动重放，动作结果未知" if sent else "指令未发送"),
                ) from exc
            if text.startswith("ERROR:"):
                raise RuntimeError(text[len("ERROR:"):])
            return text

    @staticmethod
    def _validate_line(line: str) -> None:
        if not line.strip() or "\n" in line or "\r" in line:
            raise ValueError("daemon 指令必须是单行，禁止嵌入换行")

    def mpos(self, *, wait: bool = True, timeout: float | None = None,
             cancel_event: threading.Event | None = None) -> dict:
        """只重试本地等锁；取得资格后只发送一次 MPOS，绝不重放运动。"""
        budget = self.MPOS_WAIT_TIMEOUT if timeout is None else float(timeout)
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("位置读取等待时间必须是有限正数")
        deadline = time.monotonic() + (budget if wait else min(budget, self.MPOS_TIMEOUT))
        while True:
            self._check_current_binding()
            self._check_query_cancel(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                acquired = False
            elif wait:
                acquired = self._motion_state_lock.acquire(
                    timeout=min(self.STATUS_CANCEL_INTERVAL, remaining))
            else:
                acquired = self._motion_state_lock.acquire(blocking=False)
            if acquired:
                break
            if not wait or time.monotonic() >= deadline:
                raise DaemonCommandError(
                    "status_read_busy",
                    "状态查询忙（其他查询或运动占用），当前位置暂不可确认；未发送位置或运动指令",
                )
        try:
            self._check_query_cancel(cancel_event)
            return json.loads(self._status_cmd(
                "MPOS", timeout=self.MPOS_TIMEOUT,
                deadline=min(deadline, time.monotonic() + self.MPOS_TIMEOUT),
                cancel_event=cancel_event))
        finally:
            self._motion_state_lock.release()

    @staticmethod
    def _check_query_cancel(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DaemonCommandError(
                "status_read_cancelled", "位置只读等待已取消，当前位置未确认")

    def _query_remaining(self, deadline: float,
                         cancel_event: threading.Event | None) -> float:
        self._check_current_binding()
        self._check_query_cancel(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout()
        return remaining

    def _read_query_line(self, sock, deadline: float,
                         cancel_event: threading.Event | None) -> bytes:
        # 同一连接分段读取；慢速逐字回包也不能刷新总预算。
        data = bytearray()
        while b"\n" not in data:
            remaining = self._query_remaining(deadline, cancel_event)
            sock.settimeout(min(self.STATUS_CANCEL_INTERVAL, remaining))
            try:
                part = sock.recv(4096)
            except socket.timeout:
                continue
            if not part:
                raise RuntimeError("守护进程状态连接中断")
            data.extend(part)
            if len(data) > 65536:
                raise RuntimeError("守护进程状态响应过长")
        self._query_remaining(deadline, cancel_event)
        return bytes(data).split(b"\n", 1)[0] + b"\n"

    def status(self) -> dict:
        try:
            resp = json.loads(self._status_cmd("STATUS", timeout=self.STATUS_TIMEOUT))
            actual_device = resp.get("deviceId")
            if actual_device != self.device_id:
                raise RuntimeError(
                    f"daemon 设备身份不匹配：期望 {self.device_id}，"
                    f"实际 {actual_device or '未返回'}"
                )
            self.session = resp.get("session")
            connected = bool(resp.get("connected"))
            return {"connected": connected, "session": self.session,
                    "device_id": self.device_id,
                    "daemon_addr": f"{self.daemon_addr[0]}:{self.daemon_addr[1]}",
                    "device_present": bool(resp.get("devicePresent")),
                    "estop_locked": bool(resp.get("estopLocked")),
                    "lease": resp.get("lease"),
                    "motion_features": resp.get("motionFeatures") or [],
                    "last_error": None,
                    "error_code": None}
        except Exception as exc:
            self.session = None
            self.last_error = str(exc)
            return {"connected": False, "session": None,
                    "device_id": self.device_id,
                    "daemon_addr": f"{self.daemon_addr[0]}:{self.daemon_addr[1]}",
                    "device_present": False, "last_error": self.last_error,
                    "status_stale": True,
                    "error_code": getattr(exc, "code", "daemon_unreachable")}

    def _status_cmd(self, line: str, *, timeout: float,
                    deadline: float | None = None,
                    cancel_event: threading.Event | None = None) -> str:
        """在独立短连接上完成只读查询，不触碰普通通道。"""
        self._validate_line(line)
        sock = f = None
        try:
            sock, f = self._open_channel(
                min(self.CONNECT_TIMEOUT, timeout),
                deadline=deadline, cancel_event=cancel_event)
            sock.settimeout(timeout if deadline is None else
                            self._query_remaining(deadline, cancel_event))
            f.write((line + "\n").encode())
            f.flush()
            resp = (f.readline() if deadline is None else
                    self._read_query_line(sock, deadline, cancel_event))
            if not resp or not resp.endswith(b"\n"):
                raise RuntimeError("守护进程状态连接中断")
            text = resp.decode(errors="ignore").strip()
            if text.startswith("ERROR:"):
                raise RuntimeError(text[len("ERROR:"):])
            return text
        except DaemonCommandError:
            raise
        except socket.timeout as exc:
            raise DaemonCommandError(
                "daemon_command_timeout",
                f"守护进程状态查询超时（{timeout:g}秒），当前状态不可确认",
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise DaemonCommandError("daemon_unreachable", f"守护进程状态查询失败：{exc}") from exc
        finally:
            self._close_channel(sock, f)

    def _control_cmd(self, line: str, *, timeout: float | None = None) -> str:
        """急停控制通道：新建独立 TCP 连接，完全绕过普通通道锁。"""
        self._validate_line(line)
        sock = f = None
        timeout = self.CONTROL_TIMEOUT if timeout is None else timeout
        try:
            sock, f = self._open_channel(min(self.CONNECT_TIMEOUT, timeout))
            sock.settimeout(timeout)
            f.write((line + "\n").encode())
            f.flush()
            resp = f.readline()
            if not resp or not resp.endswith(b"\n"):
                raise RuntimeError("守护进程控制连接中断")
            text = resp.decode(errors="ignore").strip()
            if text.startswith("ERROR:"):
                raise RuntimeError(text[len("ERROR:"):])
            return text
        except socket.timeout as exc:
            raise DaemonCommandError(
                "daemon_command_timeout",
                f"守护进程控制指令超时（{timeout:g}秒）：{line}；"
                "指令未自动重放，当前结果未知",
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise DaemonCommandError(
                "daemon_unreachable", f"守护进程控制指令通信失败：{exc}；"
                "指令未自动重放，当前结果未知"
            ) from exc
        finally:
            self._close_channel(sock, f)

    def stop(self) -> dict:
        return json.loads(self._control_cmd("STOP"))

    def resume(self) -> dict:
        with self._lock:
            status = self.status()
            if not status.get("connected") or not status.get("session"):
                raise RuntimeError("daemon_unreachable")
            if status.get("estop_locked"):
                raise RuntimeError("急停锁定中，拒绝恢复运动")
            self.ensure_debug_lease()
            return json.loads(self.cmd("RESUME", timeout=self.CONTROL_TIMEOUT))

    def connect_arm(self) -> dict:
        """人工连接机械臂（打开串口，生成新会话锚点）。"""
        resp = json.loads(self.cmd("CONNECT"))
        self.session = resp.get("session")
        return resp

    def disconnect_arm(self) -> dict:
        resp = json.loads(self.cmd("DISCONNECT"))
        self.session = None
        return resp


DAEMONS: dict[str, DaemonClient] = {}
_DAEMONS_LOCK = threading.RLock()


def daemon_for(device: str) -> DaemonClient:
    device = normal_device(device)
    addr = _daemon_address(device_config(device))
    old = None
    with _DAEMONS_LOCK:
        daemon = DAEMONS.get(device)
        if daemon is None or daemon.daemon_addr != addr:
            old = daemon
            if old is not None:
                old._retired.set()
            daemon = DaemonClient(device, addr)
            DAEMONS[device] = daemon
            _STATUS_CACHES.pop(device, None)
            WIZARDS.pop(device, None)
    if old is not None:
        old.retire()
    return daemon

# ---------- 状态快照（防连接池耗尽） ----------

_STATUS_LOCKS: dict[str, threading.Lock] = {}
_STATUS_CACHES: dict[str, dict] = {}


def _stale_status(snapshot: dict, message: str) -> dict:
    snapshot = dict(snapshot)
    snapshot["daemon"] = {
        **(snapshot.get("daemon") or {}), "status_stale": True, "last_error": message,
    }
    snapshot["runtimeCalibration"] = {
        **(snapshot.get("runtimeCalibration") or {}), "usable": False,
    }
    snapshot.update(status_stale=True, anchorMatch=False, usable=False, mpos=None)
    return snapshot


def status_snapshot(device: str, context_id: str | None = None) -> dict:
    """/api/status 快照：同一时刻只允许一个真实查询进入 daemon 通道。

    现场：连续点动/点击时状态查询会排在运动指令后面（daemon 通道串行），
    页面 1.5s 一次的状态轮询逐个挂起，把浏览器对本站的连接池占满
    （还有一路 MJPEG 常驻流），导致向导状态/PX 回报反而刷不出来。
    有查询在途时，其余轮询立即返回上次快照。
    """
    status_lock = _STATUS_LOCKS.setdefault(device, threading.Lock())
    daemon = daemon_for(device)
    if not status_lock.acquire(blocking=False):
        cached = _STATUS_CACHES.get(device)
        return _stale_status(cached or {
            "daemon": {"connected": False, "session": None, "device_id": device,
                       "device_present": False},
            "device": device, "camera": {}, "runtimeCalibration": None,
        }, "状态查询进行中，当前状态暂不可确认")
    try:
        if context_id is not None:
            require_context(device, context_id)
        revision = daemon.motion_revision
        daemon_status = daemon.status()
        mpos = None
        status_consistent = True
        if (daemon_status["connected"] and daemon_status.get("device_present")
                and daemon_status["session"] and not daemon.motion_active):
            try:
                mpos = daemon.mpos(wait=False)
                after = daemon.status()
                status_consistent = (
                    after.get("connected") is True
                    and after.get("device_present") is True
                    and after.get("device_id") == daemon_status.get("device_id")
                    and after.get("session") == daemon_status.get("session")
                )
                daemon_status = after
                if not status_consistent:
                    daemon_status["last_error"] = "状态查询期间 daemon 会话或设备状态发生变化"
                    daemon_status["error_code"] = "daemon_session_changed"
            except Exception as exc:
                daemon_status["last_error"] = str(exc)
                daemon_status["error_code"] = getattr(exc, "code", "daemon_unreachable")
        runtime_record = runtime_device_record(device)
        data = {"daemon": daemon_status, "mpos": mpos, "device": device,
                "camera": camera_status(device), **calibration_state(device, daemon_status),
                "profile": target_archive.load_profile(device, DATA_DIR),
                "runtime": {
                    "registered": bool(runtime_record),
                    "presenceState": runtime_record.get("presence_state", "present"),
                    "status": runtime_record.get("status"),
                    "deviceMode": runtime_record.get("device_mode", "unmanaged"),
                    "calibrationStatus": runtime_record.get("calibration_status", "unknown"),
                    "enforceInputGate": bool(runtime_record.get("enforce_input_gate", False)),
                }}
        # “未 CONNECT”是已知的正常待机状态，不等于 daemon 状态不可确认。
        # 只有状态查询失败、查询期间发生竞态，或已连接设备的 MPOS 无法读取，
        # 才标记 status_stale；否则调试台仍应允许人工点击 CONNECT。
        status_stale = bool(daemon_status.get("status_stale")) \
            or not status_consistent \
            or daemon.motion_active \
            or daemon.motion_revision != revision \
            or (daemon_status.get("connected") and daemon_status.get("device_present")
                and mpos is None)
        if status_stale:
            data = _stale_status(data, daemon_status.get("last_error")
                                 or "当前位置暂不可确认")
        if daemon_status.get("estop_locked"):
            data["usable"] = False
            data["runtimeCalibration"]["usable"] = False
        with _DAEMONS_LOCK:
            if DAEMONS.get(device) is daemon:
                _STATUS_CACHES[device] = data
            else:
                data = _stale_status(data, "daemon 绑定已变化，旧状态已失效")
        return data
    finally:
        status_lock.release()


# ---------- 持久化 ----------

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def log_op(kind: str, params: dict, ok: bool, detail) -> None:
    device = normal_device(getattr(_REQUEST_CONTEXT, "device", None))
    daemon = DAEMONS.get(device)
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "device": device,
              "session": daemon.session if daemon else None,
              "kind": kind, "ok": ok}
    if params:
        record["params"] = params
    if isinstance(detail, dict):
        record.update(detail)
    else:
        record["detail"] = str(detail)
    try:
        with _LOCK:
            OPS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with OPS_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------- 标定 ----------

def calibration_state(device: str, daemon_status: dict | None = None) -> dict:
    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
    daemon_st = daemon_status if daemon_status is not None else daemon_for(device).status()
    bundle_camera = ((bundle or {}).get("camera") or {}).get("cameraToScreen") or {}
    bundle_xy = ((bundle or {}).get("xy") or {}).get("robotToScreen")
    bundle_anchor_match = bool(
        bundle and daemon_st.get("session") and bundle.get("daemonSession")
        and bundle.get("daemonSession") == daemon_st.get("session"))
    unified_usable = bool(
        bundle and bundle.get("status") in {"ready", calibration_bundle.MOTION_READY_STATUS}
        and bundle_camera.get("homography") and bundle_xy and bundle_anchor_match)
    return {
        "runtimeCalibration": {"present": bool(bundle), "usable": unified_usable},
        "anchorMatch": bundle_anchor_match,
        "usable": unified_usable,
    }


def camera_to_robot(device: str, camera_x: float, camera_y: float) -> tuple[float, float]:
    daemon_status = daemon_for(device).status()
    daemon_session = daemon_status.get("session") if daemon_status.get("connected") else None
    # 统一校准档案：camera -> screen -> robot。这样调试台的“点哪打哪”
    # 与统一向导生成的 calibration_bundle.json 使用同一套映射事实。
    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
    frame = (bundle or {}).get("frame") or {}
    width, height = float(frame.get("width") or 0), float(frame.get("height") or 0)
    camera = (bundle or {}).get("camera") or {}
    c2s = camera.get("cameraToScreen") or {}
    robot_to_screen = ((bundle or {}).get("xy") or {}).get("robotToScreen")
    if not bundle or width <= 0 or height <= 0 or not c2s.get("homography") or not robot_to_screen:
        raise RuntimeError("尚未拟合标定映射：请完成统一校准（向导 AUTO 区块）")
    if not daemon_session or not bundle.get("daemonSession") \
            or bundle["daemonSession"] != daemon_session:
        raise RuntimeError("机械臂会话锚点未确认或不匹配，统一映射失效：请重新校准")
    return _camera_point_to_robot(bundle, robot_to_screen, camera_x, camera_y)


def _camera_point_to_robot(bundle: dict, robot_to_screen: dict,
                           camera_x: float, camera_y: float) -> tuple[float, float]:
    """相机点 → 屏幕点（生产同款边界检查）→ 机械点。

    必须走 screen_gestures.point_from_camera：它做屏幕轮廓/相机画面/边缘
    精度/反解收敛全套校验，屏幕外目标直接拒绝——曾因缺这道闸把探针派到
    屏幕外撞边框导致 X 轴丢步。
    """
    from drivers import screen_gestures as sg

    frame = bundle.get("frame") or {}
    normalized = sg.point_from_camera(
        {"cameraX": float(camera_x), "cameraY": float(camera_y),
         "frameW": int(frame["width"]), "frameH": int(frame["height"])}, bundle)
    screen = bundle.get("screen") or {}
    sx = normalized["x"] * float(screen["screenWidth"])
    sy = normalized["y"] * float(screen["screenHeight"])
    return calibration_bundle.robot_from_screen(robot_to_screen, sx, sy)


# ---------- 摄像头 ----------

def fetch_camera_frame(device: str) -> tuple[bytes, int]:
    """校准链取帧唯一入口：经 HttpFrameSource 单实现（Auto_CS#6）。

    与观测链同一套契约（身份/必填头/新鲜度校验，失败同样收敛为
    RuntimeError）；返回桥上原字节（证据存档与 MJPEG 不二次编码）。
    """
    cfg = device_config(device)
    camera_id = str(cfg.get("camera_id") or "")
    source = HttpFrameSource(
        camera_frame_url_for(device),
        expected_camera_id=camera_id,
        expected_device_id=device,
        allow_legacy=cfg.get("camera_mode") == "legacy_single",
    )
    try:
        data, seq = source.fetch_bytes()
    except Exception as exc:
        raise RuntimeError(
            f"设备 {device} 取流失败：{exc}（摄像头 {camera_id}）"
        ) from exc
    if not data:
        raise RuntimeError("取流服务尚无画面（请检查 CameraHelper 对应通道）")
    return data, int(seq or 0)


def camera_status(device: str) -> dict:
    cfg = device_config(device)
    expected_camera = str(cfg.get("camera_id") or "")
    try:
        with urllib.request.urlopen(camera_status_url_for(device), timeout=3) as resp:
            result = json.loads(resp.read())
        legacy = cfg.get("camera_mode") == "legacy_single"
        if not legacy and result.get("camera_id") != expected_camera:
            raise RuntimeError(
                f"摄像头状态身份不匹配：期望 {expected_camera}，实际 {result.get('camera_id')}"
            )
        if not legacy and result.get("device_id") != device:
            raise RuntimeError(
                f"摄像头状态设备不匹配：期望 {device}，实际 {result.get('device_id')}"
            )
        result.setdefault("seq", result.get("frame_seq", 0))
        result["camera_frame_url"] = camera_frame_url_for(device)
        result["camera_status_url"] = camera_status_url_for(device)
        return result
    except Exception as exc:
        return {"has_frame": False, "seq": 0, "frame_seq": 0,
                "camera_id": expected_camera, "device_id": device,
                "error": str(exc)}


def detect_tip_in_jpg(jpg: bytes) -> tuple[float, float] | None:
    import cv2

    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    return detect_probe_tip(frame)


# ---------- 统一校准向导（drivers/calibration_wizard.py 的调试台接线） ----------

def detect_tip_with_frame(jpg: bytes):
    """向导用探头尖检测：附带帧尺寸（供摄像头→屏幕单应档案记录帧基准）。"""
    import cv2

    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    h, w = frame.shape[:2]
    tip = detect_probe_tip(frame)
    if tip is None:
        return None
    return (float(tip[0]), float(tip[1]), int(w), int(h))


def detect_edge_in_jpg(jpg: bytes) -> dict:
    """向导用屏幕边缘检测：解码后取屏幕四边形（ScreenEdgeError 原样抛出）。"""
    import cv2

    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise screen_edge.ScreenEdgeError("取流桥帧解码失败")
    return screen_edge.detect_screen_quad(frame)


def detect_green_with_frame(jpg: bytes):
    """向导用标靶检测：绿框中心 + 帧尺寸（屏幕平面，供单应与视觉验证）。"""
    import cv2

    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    h, w = frame.shape[:2]
    center = screen_edge.detect_green_target(frame)
    return (float(center[0]), float(center[1]), int(w), int(h))


def detect_green_candidates_with_frame(jpg: bytes) -> dict:
    """v1.1 自动校准多候选检测：全部合法绿色标靶 + 帧尺寸（§8.2 门禁用）。"""
    import cv2

    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return {"items": [], "frameW": 0, "frameH": 0}
    h, w = frame.shape[:2]
    return {"items": screen_edge.detect_green_candidates(frame),
            "frameW": int(w), "frameH": int(h)}


WIZARDS: dict[str, CalibrationWizard] = {}
# 所有设备的向导共用一个输入法服务客户端（8788 设备清单/会话协议同一入口）
IME_CLIENT = ImeCalibrationClient(IME_BASE_URL)
# Apple Vision 在本机运行；实例复用其已编译的辅助程序，避免每台设备重复编译。
OCR_ENGINE = AppleVisionOcrEngine(timeout_seconds=10.0)


def calibration_binding_facts(device: str) -> dict:
    """返回向导可冻结的完整现场绑定；缺少实时帧时禁止开始校准。

    R2-09（FIX-07）：安装 UUID（cameraBindingId）与来源几何
    （sourceGeometryId）从受信来源读取——帧桥 binding-v2 当前安装与采集
    会话登记的 source_geometry_id；调试台已启用 v2 来源时身份必须齐备，
    否则拒绝开始校准（缺身份不输出可用 v3，不产出注定被消费门拒绝的
    v2 产物），身份不从请求任意填值。
    """
    cfg = device_config(device)
    status = camera_status(device)
    if status.get("status") != "streaming":
        raise RuntimeError(f"摄像头 {cfg.get('camera_id')} 没有实时画面")
    unique_id = status.get("device_unique_id")
    revision = status.get("binding_revision")
    width, height = status.get("width"), status.get("height")
    if not unique_id or revision is None or not width or not height:
        raise RuntimeError("摄像头绑定身份或实时帧尺寸不完整")
    facts = {
        "cameraId": str(cfg.get("camera_id") or ""),
        "cameraDeviceUniqueId": str(unique_id),
        "bindingRevision": int(revision),
        "daemonAddr": ":".join(map(str, _daemon_address(cfg))),
        "frameWidth": int(width),
        "frameHeight": int(height),
    }
    v2_binding = _camera_v2_binding(device, cfg)
    if v2_binding.get("enabled"):
        missing = [key for key in ("cameraBindingId", "sourceGeometryId")
                   if not v2_binding.get(key)]
        if missing:
            raise RuntimeError(
                "v2 来源身份缺失（" + "、".join(missing) + "）："
                "请完成相机绑定与采集会话握手后再开始校准")
        facts["cameraBindingId"] = str(v2_binding["cameraBindingId"])
        facts["sourceGeometryId"] = str(v2_binding["sourceGeometryId"])
        if v2_binding.get("sourceGenerationId"):
            facts["sourceGenerationId"] = str(v2_binding["sourceGenerationId"])
    return facts


def _auto_calibration_workspace() -> list[float] | None:
    """真机自动校准的 Arm XY 允许范围（§6.3.2：现场配置提供，缺省拒绝真机 run）。

    环境变量 MOTOR_DEBUG_AUTO_WORKSPACE_MM="xmin,xmax,ymin,ymax"。
    """
    raw = os.environ.get("MOTOR_DEBUG_AUTO_WORKSPACE_MM", "").strip()
    if not raw:
        return None
    try:
        bounds = [float(v) for v in raw.split(",")]
    except ValueError:
        return None
    return bounds if len(bounds) == 4 else None


def _arm_anchor_verify(device: str) -> bool:
    """A3-W 臂侧锚点验证器：有界走位到已校准锚点（悬停、不点击），相机核对
    探头尖落在"锚点屏幕坐标经 H 逆投影+live 姿态映射"的预测位置——
    验证 daemon 复位后机械坐标系仍有效（复位在原停车位=通过；移动后复位
    =偏差超限不通过）。任何一步失败抛错/返回 False，由调用方转人工。"""
    import cv2
    import numpy as np
    from drivers.screen_edge import locate_screen_quad
    from drivers.screen_pose import fit_pose

    import drivers.calibration_bundle as cb
    bundle = cb.load_bundle(device, DATA_DIR)
    if bundle is None:
        raise RuntimeError("无校准档案，无法臂侧核对")
    point = next((p for p in (bundle.get("xy") or {}).get("validation") or []
                  if p.get("effectiveRobotXY") and p.get("targetScreen")), None)
    if point is None:
        raise RuntimeError("档案缺少验证点（effectiveRobotXY+targetScreen），无法臂侧核对")
    rx, ry = (float(v) for v in point["effectiveRobotXY"])
    sx, sy = (float(v) for v in point["targetScreen"])
    z = bundle.get("z") or {}
    hover_z = z.get("hoverZ")
    # N2（第七轮验收）：type 严格检查（bool 是 int 子类）+ isfinite
    if type(hover_z) not in (int, float) or not math.isfinite(hover_z):
        # F5（第七轮复审）：缺键静默取 0 = 动绝对 Z 0，绝对禁止。
        raise RuntimeError("档案缺少有效 hoverZ，拒绝锚点走位（不能盲动 Z）")

    # 有界走位：do_goto_robot 内置单次上限/分段/残差检查（超限即抛），
    # 悬停不点击——验证的是坐标系，不是功能动作。
    do_goto_robot(device, rx, ry, tap=False,
                  hover_z=float(hover_z), feed_xy=1500.0,
                  tap_depth=float(z.get("tapDepth", 3.0)),
                  tap_feed=float(z.get("tapFeed", 1500.0)))

    jpg, _seq = fetch_camera_frame(device)
    tip = detect_tip_with_frame(jpg)
    if tip is None:
        raise RuntimeError("探头尖不可见，无法臂侧核对")
    homography = ((bundle.get("camera") or {}).get("cameraToScreen") or {}).get("homography")
    if not homography:
        raise RuntimeError("档案缺少 cameraToScreen 单应，无法臂侧核对")
    matrix = np.asarray(homography, dtype=float).reshape(3, 3)
    inverse = np.linalg.inv(matrix)
    vec = inverse @ np.array([sx, sy, 1.0])
    if abs(float(vec[2])) < 1e-9 or not np.isfinite(vec).all():
        raise RuntimeError("H 逆投影无效")
    saved_px = np.array([float(vec[0] / vec[2]), float(vec[1] / vec[2])])

    live = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    saved_corners = ((bundle.get("screen") or {}).get("edgeModel") or {}).get("corners")
    if live is None or not saved_corners:
        raise RuntimeError("live 帧或屏幕四边基准缺失，无法臂侧核对")
    located = locate_screen_quad(live, saved_corners, tolerance_px=32,
                                 require_fittable=True)
    pose = fit_pose(saved_corners, located["corners"])
    mapped = np.asarray(pose["matrix"], dtype=float) @ np.array(
        [saved_px[0], saved_px[1], 1.0])
    live_px = mapped[:2] / mapped[2]
    deviation = float(np.hypot(tip[0] - live_px[0], tip[1] - live_px[1]))
    tolerance = 45.0  # Z 触点验收 10px 量级 + 姿态/投影余量
    log_op("arm_anchor_verify", {"device": device}, ok=deviation <= tolerance,
           detail={"deviationPx": round(deviation, 1), "tolerancePx": tolerance})
    if deviation > tolerance:
        return False
    return True


def attempt_recovery_action(device: str) -> dict:
    """A3-W：一键恢复复核——attempt_recovery 的生产接线（调试台按钮/端点）。"""
    from drivers import gesture_program as gp
    from runtime.recovery import attempt_recovery
    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
    frame = gp.load_coordinate_frame(device, DATA_DIR)
    daemon = daemon_for(device)
    session = (daemon.status() or {}).get("session") if hasattr(daemon, "status") else None
    if not session:
        raise RuntimeError("daemon 未连接（无会话），恢复无从谈起")
    wizard = get_wizard(device)
    rebinds: list[str] = []
    result_restamped: list[str] = []

    def rebind(restored):
        # F3（第七轮复审）：恢复转正后必须清偿向导 state——_sync_bundle 的
        # motion_ready 分支无条件把 state.daemonSession 写回档案；不清偿则
        # 一次普通向导操作把恢复成果静默冲回旧会话（场景 B）或降 draft（A）。
        with wizard._lock:
            wizard.state["daemonSession"] = session
            wizard.state["sessionRebind"] = CalibrationWizard._blank_session_rebind()
            try:
                wizard._save_state()
            except Exception as exc:
                # N1（第七轮验收）：清偿落盘失败必须留痕——静默失败=一次向导
                # 操作冲掉恢复成果（_sync_bundle 会写回旧会话）
                log_op("recovery_wizard_state", {"device": device},
                       ok=False, detail=str(exc))
        try:
            ui_targets.rebind_pending(device, restored, data_root=DATA_DIR,
                                      operator="recovery_attempt")
            # F4（第七轮复审）：已启用目标也重签快照——几何/指纹已由恢复链
            # 独立复核；否则恢复后 enabled 目标仍 target_stale 到人工重标。
            restamped = ui_targets.re_stamp_after_recovery(
                device, restored, data_root=DATA_DIR,
                operator="recovery_attempt").get("stamped") or []
            rebinds.append(restored["daemonSession"])
            if restamped:
                result_restamped.extend(restamped)
        except Exception as exc:
            log_op("recovery_rebind", {"device": device}, ok=False, detail=str(exc))

    result = attempt_recovery(
        device, bundle=bundle, new_session=session, data_root=DATA_DIR,
        coordinate_frame=frame,
        phone_frame_provider=lambda: fetch_camera_frame(device)[0],
        arm_anchor_verifier=_arm_anchor_verify,
        rebind_targets=rebind)
    result["rebinds"] = rebinds
    result["restamped_targets"] = result_restamped
    return result


def wizard_search_region(device: str) -> dict | None:
    """读取本台生效粗框（经 _auto_proxy 契约代理；不直读 Fleet 库）。

    返回 region 记录（rect=source_norm/pixel_rect/来源绑定）；设备从未登记
    任何范围（status=unregistered）=legacy 旧路径（None）；已登记但缺生效
    指针/记录损坏 → 明确拒绝（不回全图）。
    """
    from runtime.layout_region import canonical_digest
    from runtime.phone_region_context import RegionContextError

    path = f"/v1/devices/{device}/phone-search-region"
    view = _auto_proxy("GET", path)
    selected = (view or {}).get("selected")
    if not selected and (view or {}).get("status") == "unregistered" \
            and not view.get("pending_draft"):
        # unregistered：未安装粗框包的旧设备（判别依据=登记状态/版本，
        # 不是适配是否为空）——保留旧全图路径。
        return None
    if (view or {}).get("status") != "verified" or not isinstance(selected, dict):
        raise RegionContextError(
            "region_context_invalid", "本设备搜索范围尚未复核或没有生效版本")
    version = selected.get("version")
    sha = selected.get("sha256")
    revision = view.get("revision")
    if not (type(version) is int and version > 0 and sha
            and type(revision) is int and revision > 0):
        raise RegionContextError(
            "region_context_invalid", "本设备已登记粗框但缺少生效版本；"
            "请在调试台完成范围复核后重新校准（不回退全图路径）")
    payload = _auto_proxy(
        "GET", f"/v1/devices/{device}/phone-search-region/versions/{version}")
    record = (payload or {}).get("record")
    if (not isinstance(record, dict) or not record.get("rect")
            or record.get("device_id") != device or record.get("version") != version
            or record.get("sha256") != sha or canonical_digest(
                {key: value for key, value in record.items() if key != "sha256"}) != sha):
        raise RegionContextError(
            "region_context_invalid", "生效粗框记录缺失或摘要不符，请重新核验")
    latest = _auto_proxy("GET", path)
    if (latest.get("status"), latest.get("selected"), latest.get("revision")) != (
            view["status"], selected, revision):
        raise RegionContextError(
            "region_context_changed", "读取期间粗框生效版本已变化，请重新核验")
    return {**record, "_selection_revision": revision}


def get_wizard(device: str) -> CalibrationWizard:
    """每设备一个向导实例（状态持久化在 data/devices/<设备>/calibration_wizard.json）。"""
    wizard = WIZARDS.get(device)
    if wizard is None:
        daemon = daemon_for(device)
        wizard = CalibrationWizard(
            device=device, daemon=daemon, position_read=daemon.mpos,
            ime=IME_CLIENT, data_root=DATA_DIR,
            # R2-04（§4.1）：后台自动校准动作的安装锁与调试台动作锁同根
            # （INSTALLATION_LOCK_ROOT），由 wizard 实际执行者持有到最后安全收尾。
            installation_lock_root=INSTALLATION_LOCK_ROOT,
            camera_fetch=lambda: fetch_camera_frame(device),
            tip_detector=detect_tip_with_frame,
            edge_detector=detect_edge_in_jpg,
            # S2-06/07：自动校准首次定位/梯度回退读本台生效粗框（经 Fleet
            # 契约代理；legacy 无版本=None；新模式缺框/损坏抛错为终态）。
            search_region_provider=lambda: wizard_search_region(device),
            target_detector=detect_green_with_frame,
            target_candidates=detect_green_candidates_with_frame,
            ocr_engine=OCR_ENGINE,
            binding_facts=lambda: calibration_binding_facts(device),
            real_hardware=True,  # 调试台面向真机：First Click 必须操作员口令确认
            # A5：钩子复用轮询侧同一实现（_PROMOTE_SYNCED 按 run 去重，
            # 钩子先行触发，轮询兜底不会双发）。
            post_promote_hook=lambda dev, run_id: _maybe_post_promote_sync(
                dev, {"state": "PROMOTED", "runId": run_id}),
            auto_config={"arm_workspace_mm": _auto_calibration_workspace()})
        WIZARDS[device] = wizard
    return wizard


def auto_calibration_view(device: str) -> dict:
    """自动校准 runner 的脱敏状态视图（§34：HTTP 只读状态，不执行动作）。"""
    return get_wizard(device).auto_view()


def list_arm_devices() -> list[dict]:
    """机械臂设备清单（下拉数据源）：configs/devices.json 为权威，data/ 目录兜底。

    一个控制台可校准多台设备：每台带 wizard 阶段与统一档案状态，便于切换核对。
    """
    items: dict[str, dict] = {}
    for dev, entry in _merged_device_configs().items():
        try:
            daemon_addr = _daemon_address(entry)
        except Exception:
            daemon_addr = ("", 0)
        items[dev] = {
            "deviceId": dev,
            "armSlot": entry.get("arm_slot"),
            "driverMode": entry.get("driver_mode", ""),
            "cameraId": entry.get("camera_id"),
            "cameraFrameUrl": camera_frame_url_for(dev),
            "cameraStatusUrl": camera_status_url_for(dev),
            "daemonAddr": f"{daemon_addr[0]}:{daemon_addr[1]}" if daemon_addr[0] else None,
            "calibrationRoot": str(device_dir_of(dev)),
            "dynamic": bool(entry.get("dynamic")),
            "presenceState": entry.get("presence_state", "present"),
            "runtimeStatus": entry.get("runtime_status"),
            "deviceMode": entry.get("device_mode", "unmanaged"),
            "calibrationStatus": entry.get("calibration_status", "unknown"),
            "imeDeviceId": entry.get("ime_device_id"),
            "enforceInputGate": bool(entry.get("enforce_input_gate", False)),
        }
    devices_root = DATA_DIR / "devices"
    if devices_root.exists():
        for d in sorted(devices_root.iterdir()):
            if d.is_dir() and d.name not in items:
                items[d.name] = {"deviceId": d.name, "armSlot": None,
                                 "driverMode": ""}
    for dev, item in items.items():
        state = _load_json(device_dir_of(dev) / "calibration_wizard.json", None)
        bundle = _load_json(device_dir_of(dev) / "calibration_bundle.json", None)
        item["wizardStage"] = (state or {}).get("stage")
        item["bundleStatus"] = (bundle or {}).get("status")
    return sorted(items.values(), key=lambda x: x["deviceId"])


def device_dir_of(device: str):
    from drivers.target_archive import device_dir

    return device_dir(device, DATA_DIR)


def list_ime_devices(device: str) -> dict:
    """8788 上已连接/已知 的输入法设备（人工核对配对用）+ 当前绑定。"""
    bound = get_wizard(device).state.get("imeDeviceId")
    raw_devices = IME_CLIENT.list_devices()
    # 不把旧版本示例值暴露给调试台，避免被误选为真实手机。
    devices = []
    seen = set()
    for item in raw_devices or []:
        device_id = str(item.get("deviceId") or "").strip()
        if not device_id or device_id.lower() in {"k40-01", "phone-demo"} or device_id in seen:
            continue
        seen.add(device_id)
        devices.append({"deviceId": device_id, "online": bool(item.get("online"))})
    if str(bound or "").strip().lower() in {"k40-01", "phone-demo"}:
        bound = None
    if devices:
        return {"devices": devices, "bound": bound}
    # 空列表时探测服务状态，区分"不可达"与"正常但尚无设备心跳"
    try:
        IME_CLIENT.get_session("probe-nonexistent")
        service = {"ok": True, "note": "服务正常但还没有输入法设备心跳"}
    except ImeError as exc:
        if exc.code == "calibration_not_found":
            service = {"ok": True, "note": "服务正常但还没有输入法设备心跳"}
        else:
            service = {"ok": False, "error": f"输入法服务异常：{exc.message}"}
    except Exception as exc:
        service = {"ok": False, "error": f"输入法服务不可达：{exc}"}
    return {"devices": [], "bound": bound, "service": service}


def wizard_http(wizard: CalibrationWizard, action: str, body: dict) -> dict:
    """向导动作分发（/api/wizard POST，action 字段路由）。"""
    # 2026-10-03 规格 §4.6：E 输入验收制度退役——旧验收写入口明确 410，
    # 不能再借旧入口把验收改成 ready；输入资格由「允许输入与发送」开关管理。
    if action in {
        "acceptance-execute", "acceptance-record", "acceptance-advance",
        "auto-acceptance-resume", "auto-acceptance-execute", "auto-acceptance-record",
    }:
        error = ValueError("输入验收制度已移除（input_acceptance_removed）："
                           "请使用「允许输入与发送」开关管理输入资格")
        error.code = "input_acceptance_removed"
        raise error
    if action == "start":
        # Auto_CS#20：goal="observation" = 纯观测校准档（零机械动作，产出
        # observation_ready）；缺省/None 沿用会话既有档位（motion 语义不变）。
        return wizard.start(body.get("imeDeviceId"), goal=body.get("goal"))
    if action == "cancel":
        return wizard.cancel()
    if action == "screen-geometry":
        # F2：四边不可见时的无机械动作标靶反算（§6.2，只布防+拍照）
        return wizard.screen_geometry_from_targets()
    if action == "precheck":
        return wizard.precheck()
    if action == "wait-phone":
        return wizard.wait_phone_open(timeout=float(body.get("timeout", 15.0)))
    if action == "capture-hover":
        return wizard.capture_hover_z()
    if action == "z-test":
        check_tap(float(body.get("depth", 3.0)),
                  float(body.get("dwellMs", 0)) / 1000.0)
        return wizard.z_tap_test(str(body.get("kind", "tap")),
                                 float(body.get("depth", 3.0)),
                                 float(body.get("feed", 1500.0)),
                                 int(body.get("dwellMs", 0)))
    if action == "z-save":
        return wizard.z_save(str(body.get("kind", "tap")))
    if action == "xy-preview":
        return wizard.xy_preview(int(body.get("index", 0)))
    if action == "xy-test":
        return wizard.xy_tap_test(int(body.get("index", 0)))
    if action == "xy-accept":
        return wizard.xy_accept(int(body.get("index", 0)),
                                accept=bool(body.get("accept", True)))
    if action == "xy-clear":
        return wizard.xy_clear()
    if action == "xy-fit":
        return wizard.xy_fit(force=bool(body.get("force", False)))
    if action == "xy-validate":
        return wizard.xy_validate_tap(int(body.get("index", 0)))
    if action == "xy-finish":
        return wizard.xy_finish()
    if action == "edge-capture":
        return wizard.edge_capture()
    if action == "camera-fit":
        return wizard.camera_fit(force=bool(body.get("force", False)))
    if action == "visual-verify":
        return wizard.visual_verify(float(body.get("u", 0.5)), float(body.get("v", 0.5)),
                                    tap=bool(body.get("tap", True)))
    if action == "visual-verify-all":
        return wizard.visual_verify_all()
    if action == "target-import":
        return wizard.target_import()
    if action == "target-save":
        return wizard.target_save(str(body.get("name", "")),
                                  float(body.get("nx", 0)), float(body.get("ny", 0)))
    if action == "target-test":
        return wizard.target_test(str(body.get("name", "")))
    if action == "target-accept":
        return wizard.target_accept(str(body.get("name", "")),
                                    accept=bool(body.get("accept", True)))
    if action == "acceptance-execute":
        return wizard.acceptance_execute(str(body.get("kind", "")),
                                         str(body.get("check", "")),
                                         body.get("nx"), body.get("ny"))
    if action == "acceptance-record":
        return wizard.acceptance_record(str(body.get("kind", "")),
                                        str(body.get("check", "")),
                                        bool(body.get("ok", False)),
                                        str(body.get("detail", "")))
    if action == "acceptance-advance":
        return wizard.acceptance_advance()
    if action == "finish":
        return wizard.finish()
    # ---------- v1.1 自动校准（§24：复用 POST /api/wizard 的 action 分发） ----------
    if action == "auto-start":
        return wizard.auto_start(
            max_iterations=body.get("maxIterations"),
            point_plan=body.get("pointPlan"),
            defer_input_acceptance=body.get("deferInputAcceptance", True))
    if action == "auto-stop":
        return wizard.auto_stop(body.get("runId"))
    if action == "auto-retry":
        return wizard.auto_retry(str(body.get("runId", "")),
                                 str(body.get("targetId", "*")))
    if action == "auto-validate":
        return wizard.auto_validate(body.get("runId"))
    if action == "auto-acceptance-resume":
        return wizard.auto_resume_acceptance(str(body.get("runId", "")))
    if action == "auto-motion-resume":
        return wizard.auto_resume_motion_qualification(str(body.get("runId", "")))
    if action == "auto-acceptance-execute":
        return wizard.auto_acceptance_execute(str(body.get("runId", "")),
                                              str(body.get("check", "")))
    if action == "auto-acceptance-record":
        return wizard.auto_acceptance_record(
            str(body.get("runId", "")), str(body.get("kind", "")),
            str(body.get("check", "")), bool(body.get("ok", False)),
            str(body.get("detail", "")))
    if action == "auto-state":
        return wizard.auto_view()
    if action == "z-confirm-safe":
        return wizard.z_confirm_safe(body.get("zSafe"), body.get("tapDepth"),
                                     body.get("tapFeed"))
    if action == "park-confirm":
        return wizard.park_confirm()
    if action == "park-clear":
        return wizard.park_clear()
    raise ValueError(f"未知向导动作：{action}")


# ---------- 运动 ----------

def _check_feed(feed: float, axis: str) -> None:
    check_feed(feed, axis=axis)


def _move_z_to(device: str, target_z: float, feed: float) -> dict:
    """把 Z 调整到悬停高度（相对分段，遵守单段 ≤10mm 软限位）。"""
    daemon = daemon_for(device)
    with daemon.motion_scope():
        mpos = daemon.mpos()
        dz = target_z - mpos["z"]
        if abs(dz) < 0.05:
            return mpos
        for chunk in split_segments(dz, Z_STROKE_MAX - 0.5):
            mpos = json.loads(daemon.cmd(f"Z {chunk:.3f} F{feed:g}"))
        return mpos


def do_jog(device: str, axis: str, delta: float, feed: float) -> dict:
    daemon = daemon_for(device)
    with daemon.motion_scope():
        if axis in ("x", "y"):
            _check_feed(feed, axis="X")
            check_stroke(delta, axis="X")
            dx, dy = (delta, 0.0) if axis == "x" else (0.0, delta)
            if abs(delta) > MANUAL_JOG_XY_MAX_MM:
                raise ValueError(f"步长 {delta}mm 超过人工点动上限 {MANUAL_JOG_XY_MAX_MM:g}mm")
            result = json.loads(daemon.cmd(f"XY {dx:.3f} {dy:.3f} F{feed:g}"))
        elif axis == "z":
            _check_feed(feed, axis="Z")
            check_stroke(delta, axis="Z")
            result = json.loads(daemon.cmd(f"Z {delta:.3f} F{feed:g}"))
        else:
            raise ValueError("axis 必须是 x/y/z")
        return result


def do_goto(device: str, camera_x: float, camera_y: float, *, tap: bool, hover_z: float,
            feed_xy: float, tap_depth: float, tap_feed: float) -> dict:
    daemon = daemon_for(device)
    with daemon.motion_scope():
        _check_feed(feed_xy, axis="X")
        _check_feed(tap_feed, axis="Z")
        rx, ry = camera_to_robot(device, camera_x, camera_y)
        mpos = daemon.mpos()
        dx, dy = rx - mpos["x"], ry - mpos["y"]
        check_stroke(dx, axis="X")
        check_stroke(dy, axis="Y")
        total = (dx * dx + dy * dy) ** 0.5
        if total > GOTO_TOTAL_MAX_MM:
            raise RuntimeError(f"目标位移 {total:.0f}mm 超过单次上限 {GOTO_TOTAL_MAX_MM:g}mm，"
                               f"映射可能异常，请重新标定")
        # 1) 回到悬停高度（先抬后走，现场约定悬停高度即安全行驶高度）
        mpos = _move_z_to(device, hover_z, feed=min(tap_feed, 1500.0))
        # 2) XY 走位：只发一条直达指令，超过 daemon 单段限值直接拒绝。
        if abs(dx) > XY_STROKE_MAX or abs(dy) > XY_STROKE_MAX:
            raise RuntimeError(f"XY 单次直达位移超过 {XY_STROKE_MAX:g}mm 上限")
        if abs(dx) > 0.05 or abs(dy) > 0.05:
            mpos = json.loads(daemon.cmd(f"XY {dx:.3f} {dy:.3f} F{feed_xy:g}"))
        dx, dy = rx - mpos["x"], ry - mpos["y"]
        residual = (dx * dx + dy * dy) ** 0.5
        if residual > 3.0:
            raise RuntimeError(f"走位后残差过大：距目标仍差 {residual:.1f}mm，映射可能异常")
        # 3) 可选点击（下压后自动回到悬停高度，即 TAP 语义）
        if tap:
            check_tap(tap_depth)
            mpos = json.loads(daemon.cmd(f"TAP {tap_depth:.3f} F{tap_feed:g}"))
        return {"targetRobot": [round(rx, 2), round(ry, 2)], "mpos": mpos,
                "movedMm": round(total, 1)}


def do_goto_robot(device: str, robot_x: float, robot_y: float, *, tap: bool,
                  hover_z: float, feed_xy: float, tap_depth: float,
                  tap_feed: float) -> dict:
    """按已算好的机械坐标执行走位（受控测试用）。

    与 do_goto 的区别：do_goto 内部经 camera_to_robot 换算，该函数优先使用
    旧版 calibration.json 仿射；本函数供"坐标已由业务运行时同一解析链
    （bundle 优先）算好"的调用方使用，保证受控测试验证的就是业务映射。
    限值/分段/残差检查与 do_goto 完全一致。
    """
    daemon = daemon_for(device)
    with daemon.motion_scope():
        _check_feed(feed_xy, axis="X")
        _check_feed(tap_feed, axis="Z")
        mpos = daemon.mpos()
        dx, dy = robot_x - mpos["x"], robot_y - mpos["y"]
        check_stroke(dx, axis="X")
        check_stroke(dy, axis="Y")
        total = (dx * dx + dy * dy) ** 0.5
        if total > GOTO_TOTAL_MAX_MM:
            raise RuntimeError(f"目标位移 {total:.0f}mm 超过单次上限 {GOTO_TOTAL_MAX_MM:g}mm，"
                               "映射可能异常，请重新标定")
        mpos = _move_z_to(device, hover_z, feed=min(tap_feed, 1500.0))
        if abs(dx) > XY_STROKE_MAX or abs(dy) > XY_STROKE_MAX:
            raise RuntimeError(f"XY 单次直达位移超过 {XY_STROKE_MAX:g}mm 上限")
        if abs(dx) > 0.05 or abs(dy) > 0.05:
            mpos = json.loads(daemon.cmd(f"XY {dx:.3f} {dy:.3f} F{feed_xy:g}"))
        dx, dy = robot_x - mpos["x"], robot_y - mpos["y"]
        residual = (dx * dx + dy * dy) ** 0.5
        if residual > 3.0:
            raise RuntimeError(f"走位后残差过大：距目标仍差 {residual:.1f}mm，映射可能异常")
        if tap:
            check_tap(tap_depth)
            mpos = json.loads(daemon.cmd(f"TAP {tap_depth:.3f} F{tap_feed:g}"))
        return {"targetRobot": [round(robot_x, 2), round(robot_y, 2)], "mpos": mpos,
                "movedMm": round(total, 1)}


def _basic_record_path(device: str) -> Path:
    return DATA_DIR / "devices" / device / "basic_gesture_test.json"


def _basic_load(device: str) -> dict | None:
    path = _basic_record_path(device)
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(result, dict) or result.get("deviceId") != device
                or result.get("outcome") not in {"pending", "confirmed", "executed", "failed", "unknown"}
                or not isinstance(result.get("binding"), dict)
                or not isinstance(result.get("requestId"), str)):
            raise ValueError("invalid record")
        return result
    except Exception as exc:
        raise ValueError("上次手势记录损坏，禁止再次测试，请人工检查") from exc


def _basic_save(device: str, record: dict) -> None:
    """Durable pending precedes motion; keep just one result per device."""
    path = _basic_record_path(device)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".basic-gesture-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _basic_binding(device: str, context: dict, session: str | None) -> tuple[dict, dict]:
    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
    if not bundle or bundle.get("status") not in {"ready", calibration_bundle.MOTION_READY_STATUS}:
        raise ValueError("现有校准尚未就绪，禁止手势测试")
    if not session or session != bundle.get("daemonSession"):
        raise ValueError("机械臂会话与现有校准不一致")
    frame = bundle.get("frame") or {}
    pairs = (("camera_id", "cameraId"), ("camera_device_unique_id", "cameraDeviceUniqueId"),
             ("binding_revision", "bindingRevision"))
    if (any(context.get(a) != bundle.get(b) for a, b in pairs)
            or (context.get("frame_width"), context.get("frame_height")) !=
            (frame.get("width"), frame.get("height"))):
        raise ValueError("当前设备上下文与校准绑定不一致，请重新标记")
    return {
        **ui_targets.binding_snapshot(bundle),
        "screenCalibrationFingerprint": screen_gestures.binding_snapshot(bundle)["calibration_fingerprint"],
        "zHash": calibration_bundle.profile_hash(bundle, keys=("z",)),
        "cameraId": bundle.get("cameraId"),
        "cameraUniqueId": bundle.get("cameraDeviceUniqueId"),
        "bindingRevision": bundle.get("bindingRevision"),
    }, bundle


def _calculated_gesture_blockers(device: str, status: dict) -> list[str]:
    reasons = []
    if not status.get("connected") or not status.get("session"):
        reasons.append("机械臂会话未连接")
    if status.get("estop_locked"):
        reasons.append("急停锁定中")
    daemon = daemon_for(device)
    lease = status.get("lease")
    if lease and (lease.get("lease_id") != daemon.lease_id
                  or lease.get("owner_type") != LEASE_OWNER_DEBUG):
        reasons.append("设备被其他任务占用")
    if daemon.motion_active:
        reasons.append("已有运动进行中，手势不会排队")
    try:
        # Do not construct a wizard while previewing: its recovery may write
        # persisted calibration state or start a phone watcher.
        wizard = WIZARDS.get(device)
        if wizard and wizard.auto_run_active():
            reasons.append("校准 run 进行中，暂不可执行手势")
        last = _basic_load(device)
        if (last and not last.get("review") and last["outcome"] in {"pending", "unknown"}
                and last["binding"].get("daemonSession") == status.get("session")):
            reasons.append("原手势结果待核对，禁止继续动作")
    except (ValueError, RuntimeError) as exc:
        reasons.append(str(exc))
    return reasons


def calculated_gesture_plan(device: str, context_id: str) -> dict:
    """Preview current calibration without changing the wizard, tickets or arm."""
    context = require_context(device, context_id, motion=False)
    status = daemon_for(device).status()
    reasons = []
    binding = profile = None
    presets = []
    try:
        _, bundle = _basic_binding(device, context, status.get("session"))
        binding = screen_gestures.binding_snapshot(bundle)
        profile = screen_gestures.calibrated_profile(bundle)
        presets = screen_gestures.default_plans(bundle)
    except (ValueError, TypeError, KeyError) as exc:
        reasons.append(str(exc))
    calibration_ready = binding is not None
    frame_seq = None
    try:
        camera = camera_status(device)
        if not camera.get("has_frame") or type(camera.get("seq")) is not int or camera["seq"] <= 0:
            reasons.append("当前摄像头没有有效新帧")
        else:
            frame_seq = camera["seq"]
    except Exception:
        reasons.append("当前摄像头状态不可用")
    context_blockers = {
        "device_paused": "设备已暂停，恢复设备后才可执行手势",
        "device_quarantined": "设备已隔离，需先核对现场状态",
        "device_maintenance": "设备处于维护状态，暂不可执行手势",
        "device_removed": "设备已移除，暂不可执行手势",
        "device_offline": "设备离线，暂不可执行手势",
    }
    for check in context.get("checks") or []:
        if check.get("code") in context_blockers:
            reasons.append(context_blockers[check["code"]])
    reasons.extend(_calculated_gesture_blockers(device, status))
    actions = {}
    for action, feature in (("double_tap", DOUBLE_TAP_FEATURE), ("swipe", SWIPE_FEATURE)):
        blocked = list(dict.fromkeys(reasons))
        matching = [item for item in presets if item["action"] == action]
        if matching and not any(item["calculated"] for item in matching):
            blocked.extend(reason for item in matching for reason in item["blockingReasons"])
        if feature not in status.get("motion_features", []):
            blocked.append("当前动作服务未加载所需手势特性")
        actions[action] = {"usable": not blocked, "blockingReasons": list(dict.fromkeys(blocked))}
    return {
        "deviceId": device, "contextId": context_id, "calibrationReady": calibration_ready,
        "binding": binding, "calibrationFingerprint": binding["calibration_fingerprint"] if binding else None,
        "frameSeq": frame_seq, "profile": profile, "presets": presets,
        "actions": actions, "blockingReasons": list(dict.fromkeys(reasons)),
    }


def preview_calculated_gesture(device: str, body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("手势预览必须是对象")
    action = body.get("action")
    keys = {"point"} if action == "double_tap" else {"start", "end"} if action == "swipe" else set()
    if (not keys or set(body) != {"deviceId", "contextId", "action", *keys}
            or body.get("deviceId") != device):
        raise ValueError("手势预览只接受当前设备的一点或两点相机标记")
    context = require_context(device, body["contextId"], motion=False)
    _, bundle = _basic_binding(device, context, daemon_for(device).status().get("session"))
    points = {key: screen_gestures.point_from_camera(body[key], bundle) for key in keys}
    result = screen_gestures.plan_gesture(action, points, bundle)
    return {"id": "custom", "label": "任意点手势", "deviceId": device,
            "contextId": body["contextId"], **result}


def _saved_basic_view(entry: dict, bundle: dict) -> dict:
    result = {**entry, "plan": None}
    if entry["usable"]:
        payload = entry["payload"]
        keys = ("point",) if entry["action"] == "double_tap" else ("start", "end")
        plan = screen_gestures.plan_gesture(
            entry["action"], {key: payload[key] for key in keys}, bundle)
        if entry["action"] == "double_tap":
            plan["intervalMs"] = payload["interval_ms"]
        result["plan"] = {"id": "custom", "label": entry["label"], **plan}
    return result


def saved_basic_gestures(device: str, context_id: str) -> dict:
    require_context(device, context_id, motion=False)
    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
    session = daemon_for(device).status().get("session")
    entries = saved_screen_gestures.list_gestures(device, bundle, session, DATA_DIR)
    return {"deviceId": device, "contextId": context_id,
            "gestures": [_saved_basic_view(entry, bundle) for entry in entries]}


def save_basic_gesture(device: str, body: dict) -> dict:
    if not isinstance(body, dict) or body.get("deviceId") != device:
        raise ValueError("保存手势必须指定当前设备")
    allowed = {"deviceId", "contextId", "binding", "label", "action", "operator",
               "point", "start", "end", "interval_ms", "gesture_id", "expected_version"}
    if set(body) - allowed or not {"deviceId", "contextId", "binding"} <= set(body):
        raise ValueError("手势保存字段不完整或含不允许的机械参数")
    context = require_context(device, body["contextId"], motion=False)
    _, bundle = _basic_binding(device, context, daemon_for(device).status().get("session"))
    screen_gestures.require_binding(body["binding"], bundle)
    definition = {key: value for key, value in body.items()
                  if key not in {"deviceId", "contextId", "binding"}}
    definition.setdefault("operator", "调试台")
    entry = saved_screen_gestures.save_gesture(device, definition, bundle, DATA_DIR)
    log_op("saved_gesture", {"gestureId": entry["gesture_id"], "version": entry["version"]},
           ok=True, detail={"message": "已保存手势路径，未执行动作", "action": entry["action"]})
    return _saved_basic_view(entry, bundle)


def basic_gesture_state(device: str, context_id: str, request_id: str | None = None) -> dict:
    context = require_context(device, context_id, motion=False)
    with _LOCK:
        last = _basic_load(device)
        ticket = _BASIC_TICKETS.get(device)
        if (request_id and ticket and ticket["requestId"] == request_id
                and (not last or last["requestId"] != request_id)
                and not (last and not last.get("review") and last["outcome"] in {"pending", "unknown"})):
            # Reconciliation revokes a still-unconsumed ticket before acknowledging
            # non-acceptance. A late POST can no longer turn this into an action.
            last = {"deviceId": device, "requestId": request_id, "requestHash": None,
                    "binding": ticket["binding"], "outcome": "failed",
                    "requestStatus": "not_accepted", "message": "请求未受理，未执行动作",
                    "evidence": {"motionConfirmed": False}, "finishedAt": calibration_bundle.now_iso()}
            _basic_save(device, last)
            _BASIC_TICKETS.pop(device, None)
        try:
            binding, _ = _basic_binding(device, context, daemon_for(device).status().get("session"))
            reasons = []
        except ValueError as exc:
            binding, reasons = None, [str(exc)]
        # A new browser context or edited coordinates cannot clear uncertainty.
        blocked = bool(last and not last.get("review") and last["outcome"] in {"pending", "unknown"}
                       and (binding is None or last["binding"].get("daemonSession") ==
                            binding["daemonSession"]))
        if blocked:
            reasons.append("上次动作进行中或结果未知，只能查询；请人工检查设备")
        now = time.monotonic()
        for key in list(_BASIC_TICKETS):
            if _BASIC_TICKETS[key]["expires"] <= now:
                _BASIC_TICKETS.pop(key, None)
        ticket = _BASIC_TICKETS.get(device)
        if reasons:
            _BASIC_TICKETS.pop(device, None)
            ticket = None
        elif not ticket or ticket["contextId"] != context_id or ticket["binding"] != binding:
            if len(_BASIC_TICKETS) >= 128:
                _BASIC_TICKETS.pop(next(iter(_BASIC_TICKETS)))
            ticket = {"requestId": secrets.token_urlsafe(24), "contextId": context_id,
                      "binding": binding, "expires": now + BASIC_TICKET_SECONDS}
            _BASIC_TICKETS[device] = ticket
        if last and last["outcome"] == "pending" and device not in _BASIC_ACTIVE and not last.get("review"):
            last = {**last, "resultUnknown": True, "message": "服务中断前的动作未确认，禁止重放"}
        return {"deviceId": device, "contextId": context_id, "binding": binding,
                "requestId": ticket["requestId"] if ticket else None,
                "expiresInMs": max(0, int((ticket["expires"] - now) * 1000)) if ticket else 0,
                "blocked": bool(reasons), "blockingReasons": reasons, "lastResult": last}


def _basic_validate(device: str, body: dict, binding: dict, bundle: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("手势参数必须是对象")
    action = body.get("action")
    fields = {"deviceId", "contextId", "action", "operator", "confirmed", "requestId",
              "binding", "frameSeq"}
    normalized = "coordinateSpace" in body
    if normalized:
        fields.add("coordinateSpace")
    if action == "double_tap":
        fields |= {"point", "intervalMs"}
    elif action == "swipe":
        fields |= {"start", "end", "durationMs"}
        # strokeMm（P0-2，2026-09-30 审查）：前端载荷无条件携带，白名单放行；
        # 缺省保持兼容（不带该键的旧载荷不报错）。取值校验与前端 IIFE 同区间。
        if "strokeMm" in body:
            fields.add("strokeMm")
    else:
        raise ValueError("仅支持双击或一次滑动")
    if set(body) != fields:
        raise ValueError("手势字段不完整或含不允许的参数；禁止覆盖现有机械参数")
    if action == "swipe" and "strokeMm" in body:
        stroke = body["strokeMm"]
        if type(stroke) not in (int, float) or not math.isfinite(stroke) \
                or not 5 <= stroke <= 150:
            raise ValueError("滑动长度须为 5~150mm 的有限数值")
    if (body["deviceId"] != device or body["confirmed"] is not True
            or not isinstance(body["operator"], str) or not body["operator"].strip()
            or len(body["operator"]) > 100):
        raise ValueError("必须明确确认真实动作并填写操作者")
    if (not isinstance(body["requestId"], str) or len(body["requestId"]) > 100
            or not isinstance(body["contextId"], str)):
        raise ValueError("请求编号或上下文无效")
    # JSON comparison also rejects booleans masquerading as numeric identity fields.
    if json.dumps(body["binding"], sort_keys=True, allow_nan=False) != json.dumps(binding, sort_keys=True):
        raise ValueError("绑定、校准或会话已变化，请重新标记")
    timing = "intervalMs" if action == "double_tap" else "durationMs"
    low, high = (50, 500) if action == "double_tap" else (150, 5000)
    if type(body[timing]) is not int or not low <= body[timing] <= high:
        raise ValueError(f"时间必须为 {low}~{high} 毫秒的整数")
    if type(body["frameSeq"]) is not int or body["frameSeq"] <= 0:
        raise ValueError("标记缺少有效摄像头帧编号")
    if normalized:
        if body["coordinateSpace"] != "screen_normalized":
            raise ValueError("校准后手势只接受正向屏幕坐标")
        keys = ("point",) if action == "double_tap" else ("start", "end")
        planned = screen_gestures.plan_gesture(action, {key: body[key] for key in keys}, bundle)
        if action == "swipe" and body[timing] != planned[timing]:
            raise ValueError("手势时长与当前校准计算不一致，请重新计算")
        result = {timing: body[timing], "binding": planned["binding"],
                  **{key: {"source": "screen_normalized", **planned[key]} for key in keys}}
        if action == "swipe" and "strokeMm" in body:
            result["strokeMm"] = body["strokeMm"]
        return result
    import cv2

    edge = (bundle.get("screen") or {}).get("edgeModel") or {}
    corners = np.asarray(edge.get("corners"), dtype=np.float32)
    inset = edge.get("safeInsetPx", calibration_bundle.DEFAULT_SAFE_INSET_PX)
    if (corners.shape != (4, 2) or not np.isfinite(corners).all()
            or not cv2.isContourConvex(corners) or type(inset) not in (int, float)
            or not math.isfinite(inset) or inset < 0):
        raise ValueError("现有屏幕安全边界不完整，禁止手势测试")
    result = {timing: body[timing]}
    for key in (("point",) if action == "double_tap" else ("start", "end")):
        point = body[key]
        if not isinstance(point, dict) or set(point) != {"cameraX", "cameraY", "frameW", "frameH"}:
            raise ValueError("标记必须包含且仅包含画面坐标和帧尺寸")
        if (any(type(point[k]) not in (int, float) or not math.isfinite(point[k])
                for k in ("cameraX", "cameraY"))
                or type(point["frameW"]) is not int or type(point["frameH"]) is not int
                or (point["frameW"], point["frameH"]) !=
                (binding["frame"]["width"], binding["frame"]["height"])):
            raise ValueError("标记坐标或帧尺寸无效，请重新标记")
        x, y = point["cameraX"], point["cameraY"]
        if (not 0 <= x < point["frameW"] or not 0 <= y < point["frameH"]
                or cv2.pointPolygonTest(corners, (float(x), float(y)), True) < inset):
            raise ValueError("标记超出现有校准的屏幕安全边界")
        result[key] = dict(point)
    if action == "swipe" and result["start"] == result["end"]:
        raise ValueError("滑动起点和终点不能重合")
    return result


def _measure_swipe_effect_vision(device: str, directory, gesture: dict,
                                 bundle: dict) -> dict:
    """滑动手机效果的视觉自动判定（用户拍板 2026-09-23：效果必须视觉交叉
    判定，不停留在人工核对）。

    对动作前后取证帧在校准屏幕 ROI 内做光流位移测量：
    - displaced 且与手指方向一致 → 视觉确认滚动效果
    - stationary → 无效果
    - 方向不一致/特征不足/任何失败 → unknown（保留人工核对，不假判）
    Android 触摸滚动内容随手指挥动 → 期望内容位移方向 ≈ 手指路径方向。
    """
    try:
        import cv2
        import numpy as np
        from drivers import scroll_observation as so

        before = cv2.imdecode(np.frombuffer(
            (directory / "before.png").read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
        after = cv2.imdecode(np.frombuffer(
            (directory / "after.png").read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
        if before is None or after is None:
            return {"status": "unknown", "reason_code": "frame_missing"}

        edge = (bundle.get("screen") or {}).get("edgeModel") or {}
        corners = np.asarray(edge.get("corners") or [], dtype=np.float32)
        if corners.size != 8:
            return {"status": "unknown", "reason_code": "screen_edge_missing"}
        inset = int(edge.get("safeInsetPx")
                    or calibration_bundle.DEFAULT_SAFE_INSET_PX)
        xs, ys = corners[:, 0], corners[:, 1]
        roi = {"x": int(max(0, xs.min() + inset)),
               "y": int(max(0, ys.min() + inset)),
               "width": int(max(32, xs.max() - xs.min() - 2 * inset)),
               "height": int(max(32, ys.max() - ys.min() - 2 * inset))}

        result = so.measure_displacement(before, after, roi)
        vision = {"status": result.get("status"),
                  "dy_px": result.get("dy_px"), "dx_px": result.get("dx_px"),
                  "reason_code": result.get("reason_code"),
                  "roi": roi, "direction_consistent": None}

        # 期望内容位移方向 = 手指路径的相机投影（内容随手指滚动）
        def _finger_cam_delta():
            start, end = gesture.get("start"), gesture.get("end")
            if not start or not end:
                return None
            if start.get("source") == "screen_normalized":
                h = ((bundle.get("camera") or {}).get("cameraToScreen")
                     or {}).get("homography")
                if not h:
                    return None
                hi = np.linalg.inv(np.asarray(h, dtype=float))
                root_w = float((bundle.get("screen") or {}).get("screenWidth") or 0)
                root_h = float((bundle.get("screen") or {}).get("screenHeight") or 0)
                if root_w <= 0 or root_h <= 0:
                    return None

                def _to_cam(p):
                    v = hi @ np.array([p["x"] * root_w, p["y"] * root_h, 1.0])
                    return v[0] / v[2], v[1] / v[2]
                c0, c1 = _to_cam(start), _to_cam(end)
                return c1[0] - c0[0], c1[1] - c0[1]
            if "cameraX" in start and "cameraX" in end:
                return (end["cameraX"] - start["cameraX"],
                        end["cameraY"] - start["cameraY"])
            return None

        delta = _finger_cam_delta()
        dy = vision.get("dy_px")
        if (vision.get("status") == "displaced" and delta is not None
                and dy is not None and delta[1] != 0):
            vision["expected_dy_px"] = round(delta[1], 1)
            vision["direction_consistent"] = (
                (dy > 0) == (delta[1] > 0))
        return vision
    except Exception as exc:  # 视觉测量失败不得影响动作结果本身
        return {"status": "unknown", "reason_code": f"vision_error:{str(exc)[:80]}"}


async def _run_basic_gesture(device: str, body: dict, gesture: dict) -> tuple[str, dict]:
    camera = arm = None
    attempted = False
    evidence = {"motionConfirmed": False, "effectStatus": "manual_confirmation_required",
                "effectDisplay": "手机唤醒或滚动效果需人工核对"}
    directory = _basic_record_path(device).parent / "basic_gesture_evidence"
    try:
        context = require_context(device, body["contextId"], motion=True)
        camera = _gesture_camera(device, context)
        directory.mkdir(parents=True, exist_ok=True)
        capture = camera.capture
        latest = {}
        after_sequence = None

        async def capture_checked():
            deadline = time.monotonic() + 1.5
            while True:
                frame = await asyncio.wait_for(capture(), timeout=max(.01, deadline - time.monotonic()))
                if (type(frame.get("source_frame_seq")) is not int
                        or frame["source_frame_seq"] < body["frameSeq"]
                        or (frame.get("width"), frame.get("height")) !=
                        (context["frame_width"], context["frame_height"])):
                    raise ValueError("测试画面过期或尺寸变化，禁止继续")
                if (not frame.get("frozen") and
                        (after_sequence is None or frame["source_frame_seq"] > after_sequence)):
                    latest.clear()
                    latest.update(frame)
                    return frame
                if time.monotonic() >= deadline:
                    raise ValueError("有界等待后仍无新鲜画面，结果未确认")
                await asyncio.sleep(.05)

        camera.capture = capture_checked

        def save_frame(phase):
            (directory / f"{phase}.png").write_bytes(latest["image_bytes"])
            return {key: latest[key] for key in ("sha256", "captured_at", "source_frame_seq")}

        class BasicVerifier(CameraMotionVerifier):
            async def before(self, label):
                nonlocal attempted, after_sequence
                after_sequence = None
                require_context(device, body["contextId"], motion=True)
                binding, _ = _basic_binding(device, context, (await arm.read_state()).get("session"))
                if binding != body["binding"]:
                    raise ValueError("动作前校准或绑定变化，禁止继续")
                try:
                    result = await super().before(label)
                except Exception:
                    evidence["blockedPhase"] = label
                    if latest and "before" not in evidence:
                        evidence["before"] = save_frame("before")
                    raise
                if "before" not in evidence:
                    evidence["before"] = save_frame("before")
                self.before_seq = latest["source_frame_seq"]
                attempted = True
                return result

            async def after(self, label):
                nonlocal after_sequence
                after_sequence = self.before_seq
                result = await super().after(label)
                evidence["after"] = save_frame("after")
                if latest["source_frame_seq"] <= self.before_seq:
                    result = {**result, "verified": False, "reason": "动作后没有新摄像头帧"}
                return result

        arm = DaemonArmDriver(
            device, daemon_addr=":".join(map(str, _daemon_address(device_config(device)))),
            data_root=DATA_DIR, owner_type=LEASE_OWNER_DEBUG,
            owner_id=f"basic-gesture:{body['requestId']}",
        )
        # Production macros finish/release before this wait; never sleep during contact.
        arm.motion_verifier = BasicVerifier(camera, settle_seconds=.15)
        arm.motion_verifier.observation_only = True
        arm.motion_verifier.device_id = device
        arm.motion_verifier.data_root = DATA_DIR
        if hasattr(arm.motion_verifier, "prepare"):
            arm.motion_verifier._evidence_dir = directory / body["requestId"]
            arm.motion_verifier._evidence_dir.mkdir(parents=True, exist_ok=True)

        async def live_gate(*, wechat_send=False, screen_edge_advisory=False):
            require_context(device, body["contextId"], motion=True)
            state = await arm.read_state()
            binding, bundle = _basic_binding(device, context, state.get("session"))
            if binding != body["binding"]:
                raise ValueError("现有校准或设备绑定已变化")
            checks, frame, _ = await check_live_calibration(
                device_id=device, camera_id=context["camera_id"], daemon_addr=arm.daemon_addr,
                daemon_session=state.get("session"),
                estop_locked=bool(state.get("estop_latched") or state.get("daemon_estop_locked")),
                camera=camera, bundle=bundle,
                screen_edge_advisory=screen_edge_advisory,
                require_pose=not screen_edge_advisory,
            )
            return {"checks": checks, "pose": frame.get("_screen_pose")}

        arm.live_calibration_gate = live_gate
        await arm.connect()  # Read-only TCP handshake; never serial CONNECT.
        target = gesture
        if body["action"] == "double_tap" and "binding" not in gesture:
            target = {"target_id": "gesture_point", "intervalMs": gesture["intervalMs"],
                      "target_ref": {"source": "camera_vision", **gesture["point"]}}
        elif body["action"] == "swipe" and body.get("strokeMm") is not None:
            # 用户可调滑动长度（拍板 2026-09-30）：mm 数随手势传入驱动，
            # 方向保持标记点 start→end，行程缩放到该长度。
            target = dict(gesture)
            target["strokeMm"] = float(body["strokeMm"])
        result = await getattr(arm, body["action"])(target)
        evidence["motion"] = result
        require_context(device, body["contextId"], motion=True)
        binding, _ = _basic_binding(device, context, (await arm.read_state()).get("session"))
        before, after = evidence.get("before", {}), evidence.get("after", {})
        confirmed = (binding == body["binding"] and result.get("ok") is True
                     and result.get("motion_verified") is True
                     and result.get("outcome_hint") != "unknown"
                     and after.get("source_frame_seq", -1) > before.get("source_frame_seq", 0))
        if "binding" in gesture:
            confirmed = (binding == body["binding"]
                         and gesture_motion_confirmed(body["action"], result)
                         and after.get("source_frame_seq", -1) > before.get("source_frame_seq", 0))
        evidence["motionConfirmed"] = confirmed
        controller_completed = (result.get("controller_completed") is True
                                and binding == body["binding"])
        evidence["controllerCompleted"] = controller_completed
        # 手机效果视觉自动判定（仅滑动；双击的效果不适合位移判据，保留人工核对）
        if body["action"] == "swipe" and confirmed:
            try:
                bundle = calibration_bundle.load_bundle(device, DATA_DIR)
            except Exception:
                bundle = None
            if bundle is not None:
                vision = _measure_swipe_effect_vision(device, directory, gesture, bundle)
                evidence["effectVision"] = vision
                if (vision.get("status") == "displaced"
                        and vision.get("direction_consistent") is not False):
                    evidence["effectStatus"] = "vision_verified"
                    evidence["effectDisplay"] = (
                        f"手机滚动效果已视觉确认（dy={vision.get('dy_px')}px，"
                        f"方向{'一致' if vision.get('direction_consistent') else '未核'}）")
                elif vision.get("status") == "stationary":
                    evidence["effectStatus"] = "no_effect"
                    evidence["effectDisplay"] = "画面无滚动位移：滑动未产生效果，需复核"
                # displaced 但方向相反 / unknown → 保持人工核对
        if not confirmed:
            failed_phase = next(
                (phase for phase in result.get("phases", [])
                 if not (phase.get("verification") or {}).get("verified")), None)
            if failed_phase and not controller_completed:
                evidence["blockedPhase"] = failed_phase["phase"]
            detail = ((failed_phase or {}).get("verification") or {}).get("reason")
            if controller_completed:
                evidence["observationNote"] = detail or "相机未独立确认实体运动"
            else:
                evidence["reason"] = (f"{detail}；结果未知，禁止重放" if detail else
                                      "机械动作或动作后画面未确认，结果未知，禁止重放")
        return ("confirmed" if confirmed else "executed" if controller_completed else "unknown"), evidence
    except Exception as exc:
        evidence["reason"] = str(exc)
        return ("unknown" if attempted else "failed"), evidence
    finally:
        if arm is not None:
            await arm.disconnect()
        if camera is not None:
            await camera.close()


def test_basic_gesture(device: str, body: dict) -> dict:
    context = require_context(device, body.get("contextId"), motion=True)
    _reject_motion_during_auto_run(device, "单次手势测试")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()
    with _LOCK:
        last = _basic_load(device)
        if last and last["requestId"] == body.get("requestId"):
            if last.get("requestHash") != digest:
                raise ValueError("同一请求编号不能修改参数")
            return last
    daemon = daemon_for(device)
    if not daemon._lock.acquire(blocking=False):
        raise ValueError("已有调试操作进行中，手势不会排队")
    try:
        if daemon.motion_active:
            raise ValueError("已有运动进行中，手势不会排队")
        status = daemon.status()
        lease = status.get("lease")
        if lease and (lease.get("lease_id") != daemon.lease_id
                      or lease.get("owner_type") != LEASE_OWNER_DEBUG):
            raise ValueError("设备被其他任务占用，不能抢占租约")
        binding, bundle = _basic_binding(device, context, status.get("session"))
        gesture = _basic_validate(device, body, binding, bundle)
        if "binding" in gesture:
            reasons = _calculated_gesture_blockers(device, status)
            feature = DOUBLE_TAP_FEATURE if body["action"] == "double_tap" else SWIPE_FEATURE
            if feature not in status.get("motion_features", []):
                reasons.append("当前动作服务未加载所需手势特性")
            if reasons:
                raise ValueError("；".join(reasons))
        camera = camera_status(device)
        sequence = camera.get("seq")
        if (not camera.get("has_frame") or type(sequence) is not int
                or not 0 <= sequence - body["frameSeq"] <= 300):
            raise ValueError("标记画面已过期，请重新标记")
        with _LOCK:
            last = _basic_load(device)
            if last and not last.get("review") and last["outcome"] in {"pending", "unknown"} and (
                    last["binding"].get("daemonSession") == binding["daemonSession"]):
                raise ValueError("上次动作进行中或结果未知，禁止再次执行")
            ticket = _BASIC_TICKETS.get(device)
            if (not ticket or ticket["expires"] <= time.monotonic()
                    or ticket["requestId"] != body["requestId"]
                    or ticket["contextId"] != body["contextId"] or ticket["binding"] != binding):
                raise ValueError("一次性请求已失效，请查询结果并重新标记，禁止重放")
            record = {"requestId": body["requestId"], "deviceId": device,
                      "requestHash": digest, "binding": binding, "action": body["action"],
                      "operator": body["operator"].strip(), "outcome": "pending",
                      "message": "动作待确认", "evidence": {},
                      "createdAt": calibration_bundle.now_iso()}
            _basic_save(device, record)
            _BASIC_TICKETS.pop(device, None)
            _BASIC_ACTIVE.add(device)
        try:
            if lease:
                released = json.loads(daemon.cmd("RELEASE", timeout=8))
                if released.get("released") is not True:
                    raise ValueError("本调试台租约释放未确认")
            daemon.lease_id, daemon.lease_expires = None, 0.0
            with daemon.motion_scope():
                outcome, evidence = asyncio.run(_run_basic_gesture(device, body, gesture))
        except Exception as exc:
            outcome, evidence = "unknown", {"motionConfirmed": False, "reason": str(exc),
                "effectStatus": "manual_confirmation_required", "effectDisplay": "手机效果需人工核对"}
        record.update(outcome=outcome, evidence=evidence, finishedAt=calibration_bundle.now_iso(),
                      message={"confirmed":
                               # 视觉已确认滚动效果时不再提示人工核对
                               ("机械动作与手机滚动效果均已视觉确认"
                                if evidence.get("effectStatus") == "vision_verified"
                                else "机械动作已确认；手机效果需人工核对"),
                               "executed": "控制器已执行完整动作；请观察实际效果",
                               "failed": "动作未下发：" + evidence.get("reason", "预检未通过"),
                               "unknown": "结果未知，禁止重放"}[outcome])
        with _LOCK:
            _basic_save(device, record)
        log_op("basic_gesture_test", {"action": body["action"], "requestId": body["requestId"]},
               ok=outcome == "confirmed", detail=record)
        return record
    finally:
        with _LOCK:
            _BASIC_ACTIVE.discard(device)
        daemon._lock.release()


def review_basic_gesture(device: str, body: dict) -> dict:
    expected = {"deviceId", "contextId", "requestId", "daemonSession", "operator", "reason", "confirmed"}
    if (not isinstance(body, dict) or set(body) != expected or body["deviceId"] != device
            or body["confirmed"] is not True or any(
                not isinstance(body[k], str) or not body[k].strip() or len(body[k]) > limit
                for k, limit in (("operator", 100), ("reason", 500), ("requestId", 100)))):
        raise ValueError("请明确确认人工核对，填写操作者、原因和原请求编号")
    context = require_context(device, body["contextId"], motion=False)
    _reject_motion_during_auto_run(device, "结束手势试验")
    daemon = daemon_for(device)
    if not daemon._lock.acquire(blocking=False):
        raise ValueError("调试操作进行中，不能结束试验")
    try:
        if daemon.motion_active or device in _BASIC_ACTIVE:
            raise ValueError("动作仍在执行，禁止人工结束")
        state = daemon.status()
        if (state.get("connected") is not True or not state.get("session")
                or state["session"] != body["daemonSession"]):
            raise ValueError("当前机械臂会话未确认或已经变化")
        if state.get("lease"):
            raise ValueError("设备仍持有租约，不能结束试验")
        position = daemon.mpos()
        if position.get("state") != "Idle":
            raise ValueError("机械臂未确认静止，不能结束试验")
        # No lease claim/release or motion here. Recheck after the position read.
        current = daemon.status()
        if (daemon.motion_active or device in _BASIC_ACTIVE
                or current.get("connected") is not True or current.get("lease")
                or current.get("session") != body["daemonSession"]):
            raise ValueError("核对期间设备状态变化，不能结束试验")
        with _LOCK:
            if daemon.motion_active or device in _BASIC_ACTIVE:
                raise ValueError("动作仍在执行，禁止人工结束")
            last = _basic_load(device)
            if last and last["requestId"] == body["requestId"]:
                if last["binding"].get("daemonSession") != body["daemonSession"]:
                    raise ValueError("核对会话不属于原请求")
                if last.get("review"):
                    return last
                if last["outcome"] not in {"pending", "unknown"}:
                    raise ValueError("该试验已明确结束，无需解除未知阻断")
            else:
                if last and last["outcome"] in {"pending", "unknown"} and not last.get("review"):
                    raise ValueError("必须核对当前未结束的原请求")
                # A lost HTTP response may mean no request arrived. Retire all issued
                # tokens too, so an in-flight old POST cannot run after this review.
                binding, _ = _basic_binding(device, context, current["session"])
                last = {"deviceId": device, "requestId": body["requestId"], "binding": binding,
                        "requestHash": None, "outcome": "unknown", "evidence": {},
                        "message": "原请求未确认；人工已结束试验，原动作不重放"}
            last["review"] = {"operator": body["operator"].strip(), "reason": body["reason"].strip(),
                              "daemonSession": current["session"], "position": position,
                              "reviewedAt": calibration_bundle.now_iso()}
            _basic_save(device, last)
            _BASIC_TICKETS.pop(device, None)
        log_op("basic_gesture_review", {"requestId": body["requestId"]}, ok=True,
               detail={"review": last["review"], "originalOutcome": last["outcome"]})
        return last
    finally:
        daemon._lock.release()


def gesture_profile_view(device: str, profile_id: str) -> dict:
    entry = gesture_profiles.load_profile(device, profile_id, DATA_DIR)
    session = daemon_for(device).status().get("session")
    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
    return {"device_id": device, "profile_id": profile_id,
            "entry": gesture_profiles.view_profile(entry, bundle, session) if entry else None}


def screen_proof_view(device):
    from drivers import screen_proof
    try:
        profile = screen_proof.load_profile(device, DATA_DIR)
        return {"profile": profile, "usable": bool(profile and profile.get("regions")),
                "message": "已登记，仍需逐项页面与发送测试" if profile else "尚未登记"}
    except (ValueError, OSError) as exc:
        return {"profile": None, "usable": False, "message": str(exc)}


def save_screen_proof(device, body, action):
    from drivers import screen_proof
    context = require_context(device, body.get("contextId") or body.get("context"), motion=False)
    if body.get("confirmed") is not True:
        raise ValueError("请人工确认当前页面和选区；登记不会执行任何机械动作")
    state = daemon_for(device).status()
    # motion_ready（自动校准晋级态）与 fleet 读端同一放行口径——
    # 否则 motion_ready 档案在调试台永远「校准尚未就绪」，登记入口死锁。
    _, bundle = screen_proof.binding(device, DATA_DIR, allow_motion_ready=True)
    if not state.get("session") or state["session"] != bundle.get("daemonSession"):
        raise ValueError("机械臂会话与校准不一致，请先重新校准")
    if state.get("lease"):
        raise ValueError("设备正在执行任务或持有租约，请结束控制后再登记页面")
    if (context.get("camera_id") != bundle.get("cameraId")
            or context.get("camera_device_unique_id") != bundle.get("cameraDeviceUniqueId")):
        raise ValueError("当前摄像头与校准不一致")
    status = camera_status(device)
    if status.get("status") != "streaming" or not status.get("has_frame"):
        raise ValueError("摄像头尚无新鲜画面")
    image, sequence = fetch_camera_frame(device)
    if type(sequence) is not int:
        raise ValueError("画面缺少帧编号")
    if action == "reference":
        screen_proof.save_reference(
            device, state=body.get("state"), label=body.get("label"),
            region=body.get("region"), image=image, operator=body.get("operator"), data_root=DATA_DIR)
    elif action == "regions":
        if body.get("inputEmptyConfirmed") is not True:
            raise ValueError("请先清空手机输入框，并明确确认已清空")
        screen_proof.save_regions(
            device, regions=body.get("regions"), image=image,
            operator=body.get("operator"), data_root=DATA_DIR)
    else:
        raise ValueError("未知页面登记操作")
    log_op("screen_proof_register", {"action": action}, ok=True,
           detail={"operator": body.get("operator"), "frame_seq": sequence})
    return screen_proof_view(device)


def _gesture_camera(device: str, context: dict):
    source = HttpFrameSource(
        camera_frame_url_for(device), expected_device_id=device,
        expected_camera_id=context["camera_id"],
        expected_device_unique_id=context["camera_device_unique_id"],
        expected_width=context["frame_width"], expected_height=context["frame_height"],
        allow_legacy=False,
    )
    return RealCameraDriver(device, frame_provider=source,
                            screen_device_id=device, data_root=DATA_DIR)


async def _gesture_evidence(camera, config: dict, path: Path) -> dict:
    import cv2
    from adapters.vision.ocr import find_text_bbox

    frame = await camera.capture()
    region = config["list_region"]
    if (frame.get("width"), frame.get("height")) != (region["frameW"], region["frameH"]):
        raise ValueError("测试画面尺寸已变化")
    if frame.get("frozen") or type(frame.get("source_frame_seq")) is not int:
        raise ValueError("缺少新鲜摄像头帧序号，不能证明测试结果")
    path.write_bytes(frame["image_bytes"])
    result = {key: frame[key] for key in ("sha256", "captured_at", "source_frame_seq")}
    result["path"] = str(path)
    if config["return_to_top"]:
        roi = config["top_anchor_region"]
        image = cv2.imdecode(np.frombuffer(frame["image_bytes"], np.uint8), cv2.IMREAD_COLOR)
        crop = image[int(roi["y1"]):int(roi["y2"]), int(roi["x1"]):int(roi["x2"])]
        ok, png = cv2.imencode(".png", crop)
        if not ok:
            raise ValueError("顶部验证区域无法解码")
        ocr = await asyncio.to_thread(camera.ocr_engine.recognize, png.tobytes(), region="account")
        if ocr.truncated:
            raise ValueError("顶部区域识别不完整，不能作为测试证据")
        result["top_anchor_found"] = find_text_bbox(
            ocr, config["top_anchor_text"], exact=True) is not None
    return result


async def _run_gesture_test(device: str, context_id: str, entry: dict) -> tuple[str, dict]:
    """One explicit test, one production swipe. Never replay an uncertain action."""
    pending = entry["pending_test"]
    config = entry["config"]
    directory = (DATA_DIR / "devices" / device / "gesture_evidence" /
                 entry["profile_id"] / str(entry["version"]) / pending["id"])
    directory.mkdir(parents=True, exist_ok=True)
    camera = arm = None
    attempted = False
    evidence = {"direction": pending["direction"], "main_ui_confirmed": False,
                "ui_guard_scope": "operator_confirmed_debug_reference",
                "production_ready": False}
    try:
        context = require_context(device, context_id, motion=True)
        camera = _gesture_camera(device, context)

        class TestVerifier(CameraMotionVerifier):
            started = False

            async def before(self, label):
                nonlocal attempted
                if not self.started:
                    require_context(device, context_id, motion=True)
                    before = evidence["before"] = await _gesture_evidence(
                        camera, config, directory / "before.png")
                    reference = before["path"]
                    if pending["direction"] == "top":
                        reference = entry["tests"]["search"]["evidence"]["before"]["path"]
                    camera.register_reference("gesture_main", Path(reference).read_bytes())
                    ui = evidence["ui_before"] = await camera.identify_ui_state()
                    if ui.get("ui_state") != "gesture_main":
                        raise ValueError("当前画面与人工确认的列表主界面不一致，禁止测试")
                    if config["return_to_top"] and (
                            before.get("top_anchor_found") is not (pending["direction"] == "search")):
                        raise ValueError("查找测试必须从顶部开始，回顶测试必须从顶部标志已离开的位置开始")
                    self.started = True
                result = await super().before(label)
                attempted = True
                return result

            async def after(self, label):
                result = await super().after(label)
                if label in {"swipe_release", "swipe"}:
                    await asyncio.sleep(config["settle_ms"] / 1000)
                    after = evidence["after"] = await _gesture_evidence(
                        camera, config, directory / "after.png")
                    require_context(device, context_id, motion=True)
                    ui = evidence["ui_after"] = await camera.identify_ui_state()
                    evidence["main_ui_confirmed"] = ui.get("ui_state") == "gesture_main"
                    before = evidence["before"]
                    valid = (evidence["main_ui_confirmed"]
                             and after["source_frame_seq"] > before["source_frame_seq"]
                             and after["sha256"] != before["sha256"])
                    if config["return_to_top"]:
                        valid = valid and after.get("top_anchor_found") is (pending["direction"] == "top")
                    if not valid:
                        result = {**result, "verified": False, "reason": "滑动后界面或顶部证据未确认"}
                return result

        cfg = device_config(device)
        arm = DaemonArmDriver(
            device, daemon_addr=":".join(map(str, _daemon_address(cfg))), data_root=DATA_DIR,
            owner_type=LEASE_OWNER_DEBUG, owner_id=f"gesture-test:{pending['id']}",
        )
        arm.motion_verifier = TestVerifier(
            camera, settle_seconds=config["settle_ms"] / 1000,
            evidence_dir=directory / "motion",
        )

        async def live_gate(*, wechat_send=False, screen_edge_advisory=False):
            require_context(device, context_id, motion=True)
            current = await arm.read_state()
            bundle = calibration_bundle.load_bundle(device, DATA_DIR)
            gesture_profiles.check_binding(entry, bundle, current.get("session"))
            checks, frame, _ = await check_live_calibration(
                device_id=device, camera_id=context["camera_id"], daemon_addr=arm.daemon_addr,
                daemon_session=current.get("session"),
                estop_locked=bool(current.get("estop_latched") or current.get("daemon_estop_locked")),
                camera=camera, bundle=bundle,
                screen_edge_advisory=screen_edge_advisory,
                require_pose=not screen_edge_advisory,
            )
            return {"checks": checks, "pose": frame.get("_screen_pose")}

        arm.live_calibration_gate = live_gate
        await arm.connect()  # TCP handshake + STATUS only, never serial CONNECT.
        result = evidence["motion"] = await arm.swipe({
            **config[f"{pending['direction']}_gesture"], "durationMs": config["swipe_duration_ms"],
        })
        before, after = evidence.get("before", {}), evidence.get("after", {})
        confirmed = (gesture_motion_confirmed("swipe", result) and evidence["main_ui_confirmed"]
                     and after.get("source_frame_seq", -1) > before.get("source_frame_seq", 0)
                     and after.get("sha256") != before.get("sha256"))
        if config["return_to_top"]:
            confirmed = confirmed and after.get("top_anchor_found") is (pending["direction"] == "top")
        if not confirmed:
            evidence["reason"] = "动作或画面变化未完整确认，禁止重复测试和启用；请人工复核"
        return ("confirmed" if confirmed else "unknown"), evidence
    except Exception as exc:
        evidence["reason"] = str(exc)
        return ("unknown" if attempted else "failed"), evidence
    finally:
        if arm is not None:
            await arm.disconnect()  # Close only this test's TCP connection.
        if camera is not None:
            await camera.close()


def test_gesture_profile(device: str, body: dict) -> dict:
    if body.get("confirmed") is not True or body.get("mainViewConfirmed") is not True:
        raise ValueError("必须明确确认真实滑动测试，并确认当前为目标列表主界面")
    context_id = body.get("contextId") or body.get("context")
    require_context(device, context_id, motion=True)
    _reject_motion_during_auto_run(device, "手势测试")
    daemon = daemon_for(device)
    if not daemon._lock.acquire(blocking=False):
        raise ValueError("已有调试操作进行中，拒绝排队执行手势")
    try:
        if daemon.motion_active:
            raise ValueError("已有运动进行中")
        status = daemon.status()
        lease = status.get("lease")
        if lease and (lease.get("lease_id") != daemon.lease_id
                      or lease.get("owner_type") != LEASE_OWNER_DEBUG):
            raise ValueError("设备已被其他任务占用，不能抢占租约")
        profile_id = body.get("profileId", "")
        operator = str(body.get("operator") or "").strip()
        entry = gesture_profiles.begin_test(
            device, profile_id, body.get("direction"),
            expected_revision=body.get("expectedRevision"), operator=operator,
            current_session=status.get("session"), data_root=DATA_DIR,
        )
        try:
            if lease:
                released = json.loads(daemon.cmd("RELEASE", timeout=8))
                if released.get("released") is not True:
                    raise ValueError("调试租约释放未确认，禁止继续")
            daemon.lease_id, daemon.lease_expires = None, 0.0
            with daemon.motion_scope():
                outcome, evidence = asyncio.run(_run_gesture_test(device, context_id, entry))
        except Exception as exc:
            outcome, evidence = "unknown", {"reason": str(exc)}
        updated = gesture_profiles.finish_test(
            device, profile_id, expected_revision=entry["revision"],
            test_id=entry["pending_test"]["id"], outcome=outcome, evidence=evidence,
            operator=operator, current_session=daemon.status().get("session"), data_root=DATA_DIR,
        )
        log_op("gesture_profile_test", {"profileId": profile_id, "direction": body.get("direction")},
               ok=outcome == "confirmed", detail={"outcome": outcome, "version": updated["version"]})
        return {**gesture_profile_view(device, profile_id), "outcome": outcome, "evidence": evidence}
    finally:
        daemon._lock.release()


# ---------- HTTP ----------

# ---- R3-14：本机调试台免登录边界（身份附件 L1/L2；函数在 runtime/layout_auth.py） ----
from runtime import layout_auth as _la

# 允许的本机 Host（精确 host:port，不接受任意解析到 loopback 的域名）。
_DEBUG_ALLOWED_HOSTS = (
    f"127.0.0.1:{LISTEN_PORT}", f"[::1]:{LISTEN_PORT}",
    f"localhost:{LISTEN_PORT}")
_DEBUG_SESSIONS = _la.DebugConsoleSessions()   # 进程生命周期；重启全部失效

# 稳定本机主体（受信后端构造 actor 的唯一来源；不从浏览器读取）。
_DEBUG_CONSOLE_ID_FILE = ROOT / "data" / "debug_console_id"


def _debug_console_id() -> str:
    """稳定 local_console_id：首次生成本地保存（0600）；不进 URL/日志。"""
    try:
        value = _DEBUG_CONSOLE_ID_FILE.read_text(encoding="utf-8").strip()
        if _la.PRINCIPAL_RE.match(value):
            return value
    except OSError:
        pass
    value = "console-" + secrets.token_hex(8)
    _DEBUG_CONSOLE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DEBUG_CONSOLE_ID_FILE.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(_DEBUG_CONSOLE_ID_FILE, 0o600)
    except OSError:
        pass
    return value


def _debug_boundary_check(handler, method: str) -> None:
    """请求前置边界：回环 peer + 精确 Host/Origin/Sec-Fetch-Site（+写 CSRF）。
    允许 Host 按本服务实际监听端口计算（测试/部署同规则，动态端口不豁免）。"""
    peer = handler.client_address[0] if handler.client_address else ""
    if not _la.is_loopback_address(peer):
        raise _la.DebugBoundaryError("origin_rejected", "仅限本机回环访问")
    try:
        port = handler.server.server_address[1]
    except Exception:
        port = LISTEN_PORT
    _la.evaluate_browser_request(
        method=method,
        host=handler.headers.get("Host"),
        origin=handler.headers.get("Origin"),
        sec_fetch_site=handler.headers.get("Sec-Fetch-Site"),
        allowed_hosts=(f"127.0.0.1:{port}", f"[::1]:{port}",
                       f"localhost:{port}"),
        sessions=_DEBUG_SESSIONS,
        cookie_token=handler._debug_cookie_token(),
        csrf_header=handler.headers.get("X-Debug-CSRF"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _query(self) -> dict:
        from urllib.parse import parse_qs, urlparse

        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _mjpeg_stream(self, device: str) -> None:
        """MJPEG 实时流：seq 变了才拉整帧下发；客户端断开即结束。"""
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last_seq = -1
        try:
            while True:
                st = camera_status(device)
                if st.get("has_frame") and int(st.get("seq", 0)) != last_seq:
                    try:
                        jpg, seq = fetch_camera_frame(device)
                    except RuntimeError:
                        # 取流桥瞬时不可用：保持这条视频连接，稍后重试
                        # （断开会让页面进入重连循环，画面整个闪断）
                        time.sleep(1.0)
                        continue
                    if seq == last_seq:
                        time.sleep(0.03)
                        continue
                    last_seq = seq
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                    self.wfile.flush()
                    continue  # 立刻回头看有没有更新的帧
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 页面关闭/刷新，正常收尾

    def _debug_cookie_token(self) -> str | None:
        from http.cookies import SimpleCookie
        raw = self.headers.get("Cookie") or ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(_la.DEBUG_COOKIE_NAME)
        return morsel.value if morsel else None

    def _ensure_debug_session(self) -> None:
        """页面装载即取得随机浏览器会话（防跨站用；无账号/无密码/无角色）。
        每次装载刷新会话（幂等），Set-Cookie 固定 HttpOnly/SameSite=Strict/Path=/。"""
        token, csrf = _DEBUG_SESSIONS.issue()
        self._pending_set_cookie = _la.DebugConsoleSessions.cookie_value(token)
        self._pending_csrf = csrf

    def do_GET(self) -> None:
        try:
            _debug_boundary_check(self, "GET")
        except _la.DebugBoundaryError as exc:
            self._json({"error": {"code": exc.code, "message": exc.message}},
                       code=exc.status)
            return
        path = self.path.split("?")[0]
        query = self._query()
        device = normal_device(query.get("deviceId") or query.get("device"))
        _REQUEST_CONTEXT.device = device
        # FIX-05（§4.1）：GET 动作入口（校准直驱滑动任务提交）同样先取共享安装锁。
        try:
            _installation_guard = _enter_installation_guard(device, path=path)
        except Exception as exc:
            status, payload = api_error(exc)
            self._json(payload, code=status)
            return
        try:
            context_id = query.get("contextId") or query.get("context")
            if path.startswith("/api/gestures/"):
                raw = query.get("deviceId") if "deviceId" in query else query.get("device")
                strict_debug_device(raw)
                if "deviceId" in query and "device" in query and query["deviceId"] != query["device"]:
                    raise ValueError("设备编号冲突，禁止自动选择")
            if path == "/api/status":
                # 先做纯本地身份校验，再由 status_snapshot 串行化慢查询；
                # 不让并发请求在快照锁外堆积摄像头/daemon 查询。
                require_context_local(device, context_id)
            elif path.startswith("/api/") and path not in (
                    "/api/devices", "/api/serial-ports",
                    "/api/debug-session") \
                    and not is_auto_proxy_path(path):
                require_context(device, context_id, motion=False)
            if path == "/" or path == "/index.html":
                self._ensure_debug_session()
                self._send(200, PAGE_PATH.read_bytes(),
                           "text/html; charset=utf-8",
                           extra={"Set-Cookie": self._pending_set_cookie})
            elif path == "/api/debug-session":
                # R3-14/L1：仅同源（边界检查已过）；CSRF 只发给本机页面
                self._ensure_debug_session()
                self._send(200, json.dumps(
                    {"csrf_token": self._pending_csrf}).encode("utf-8"),
                    "application/json; charset=utf-8",
                    extra={"Set-Cookie": self._pending_set_cookie})
            elif path == "/api/input-settings":
                # §4.2.3：同源代理——浏览器不直连 Fleet 内部地址；保留上游 HTTP。
                status, payload = _agent_passthrough(
                    "GET", f"/v1/devices/{device}/input-settings")
                self._json(payload, code=status)
            elif path == "/api/calibrated-scroll":
                # §7.1：上下滑正式任务代理——GET 能力→构造 calibrated-scroll.v1
                # 任务→提交 /v1/tasks；机械/内容两种结果分开展示。
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=True)
                action = str(body.get("action") or "")
                if action not in ("swipe_up", "swipe_down"):
                    self._error(422, "camera_request_invalid",
                                "action 只允许 swipe_up/swipe_down")
                    return
                status, capability = _agent_passthrough(
                    "GET", f"/v1/devices/{device}/calibrated-scroll")
                if status != 200 or not capability.get("usable"):
                    self._json({"ok": False, "error": {
                        "code": "capability_unavailable",
                        "message": "校准直驱滑动能力未就绪",
                        "blocking": capability.get("blocking_reasons", [])}},
                        code=503)
                    return
                task_body = {
                    "task_id": f"mdebug-scroll-{uuid.uuid4().hex[:12]}",
                    "task_type": "calibrated_scroll",
                    "contract_version": "calibrated-scroll.v1",
                    "device_id": device,
                    "idempotency_key": f"mdebug-{uuid.uuid4().hex[:12]}",
                    "correlation_id": f"mdebug-{uuid.uuid4().hex[:8]}",
                    "expires_at": _utc_expires(5),
                    "payload": {"action": action,
                                "binding": capability.get("binding")},
                }
                status, result = _agent_passthrough(
                    "POST", "/v1/tasks", task_body)
                log_op("calibrated_scroll", {"device": device, "action": action},
                       ok=status == 200, detail={"http": status})
                if status != 200:
                    self._json(result if isinstance(result, dict) else
                               {"ok": False, "error": {"message": result}}, code=status)
                    return
                task_id = (result.get("task") or result).get("task_id")
                self._json({"ok": True, "task_id": task_id, "action": action})
                return
            elif path == "/api/input-settings/review":
                # §4.8.7：人工核对出口代理（零机械动作；保留上游 HTTP/错误键）
                status, payload = _agent_passthrough(
                    "POST", f"/v1/devices/{device}/input-recovery/review",
                    body.get("review") or {})
                log_op("input_recovery_review", {"device": device},
                       ok=status == 200, detail={"http": status})
                self._json(payload, code=status)
            elif path == "/api/status":
                self._json(status_snapshot(device, context_id))
            elif path == "/api/camera/frame":
                try:
                    jpg, seq = fetch_camera_frame(device)
                except Exception as exc:
                    self._send(503, str(exc).encode("utf-8"),
                               "text/plain; charset=utf-8")
                    return
                self._send(200, jpg, "image/jpeg", {"X-Frame-Seq": str(seq)})
            elif path == "/api/camera/stream":
                self._mjpeg_stream(device)
            elif path == "/api/camera/status":
                self._json(camera_status(device))
            elif path == "/api/gestures/state":
                self._json(basic_gesture_state(device, context_id, query.get("requestId")))
            elif path == "/api/gestures/plan":
                self._json(calculated_gesture_plan(device, context_id))
            elif path == "/api/gestures/saved":
                self._json(saved_basic_gestures(device, context_id))
            elif path == "/api/gestures/evidence":
                record = _basic_load(device)
                phase = query.get("phase")
                if (not record or query.get("requestId") != record["requestId"]
                        or phase not in {"before", "after"}
                        or phase not in record.get("evidence", {})):
                    raise ValueError("此请求没有可读取的动作证据")
                evidence_path = _basic_record_path(device).parent / "basic_gesture_evidence" / f"{phase}.png"
                self._send(200, evidence_path.read_bytes(), "image/png")
            elif path == "/api/camera/detect":
                jpg, seq = fetch_camera_frame(device)
                tip = detect_tip_in_jpg(jpg)
                import cv2

                frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                h, w = frame.shape[:2] if frame is not None else (0, 0)
                self._json({"found": tip is not None, "seq": seq,
                            "frameW": w, "frameH": h,
                            "x": round(tip[0], 1) if tip else None,
                            "y": round(tip[1], 1) if tip else None})
            elif path == "/api/screen-proof":
                self._json(screen_proof_view(device))
            elif path == "/api/screen-proof/check":
                context = require_context(device, context_id, motion=False)
                camera = _gesture_camera(device, context)
                camera.screen_device_id = device
                camera.device_id = context["camera_id"]
                camera.data_root = DATA_DIR
                self._json(asyncio.run(camera.observe_regions()))
            elif path == "/api/targets":
                # 输入法发送链路的具名目标（设备级档案）：只存"画面位置"（摄像头固定 ⇒
                # 像素位置不变），机械坐标按当前标定实时换算——机械臂重启后重新标定即
                # 自动恢复，无需重标目标。
                st = calibration_state(device)
                raw = target_archive.load_targets(device, DATA_DIR)
                runtime_bundle = calibration_bundle.load_bundle(device, DATA_DIR)
                runtime_frame = (runtime_bundle or {}).get("frame") or {}
                fw = float(runtime_frame.get("width") or 0) or None
                fh = float(runtime_frame.get("height") or 0) or None
                target_calibration = runtime_bundle
                profile = target_archive.load_profile(device, DATA_DIR)
                targets = {}
                for name in TARGET_DISPLAY_NAMES:
                    t = raw.get(name)
                    if t is None:
                        continue
                    item = dict(t)
                    item["valid"] = False
                    item["robotX"] = item["robotY"] = None
                    if st["usable"] and fw and fh:
                        try:
                            resolved = target_archive.resolve_target(item, target_calibration, profile)
                            item["robotX"] = resolved["robotX"]
                            item["robotY"] = resolved["robotY"]
                            item["valid"] = True
                        except Exception:
                            pass
                    targets[name] = item
                self._json({"targets": targets, "names": TARGET_DISPLAY_NAMES,
                            "modes": DEFAULT_TARGET_MODES,
                            "session": daemon_for(device).session, "device": device, **st})
            elif path == "/api/ui-targets":
                # 校准后固定目标（ui_targets.json）：登记/测试/启停独立于基础校准档案。
                # 校准验收期（E 相位/手动验收阶段）登记窗口同样放行（bindPending）。
                registration_window = None
                try:
                    registration_window = get_wizard(device).ui_target_registration_window()
                except Exception:
                    registration_window = None
                self._json(ui_targets.list_view(device, DATA_DIR,
                                                registration_window=registration_window))
            elif path == "/api/gesture-profiles":
                view = gesture_profile_view(device, query.get("profileId", ""))
                direction, phase = query.get("evidenceDirection"), query.get("evidencePhase")
                if direction in {"search", "top"} and phase in {"before", "after"}:
                    test = (((view.get("entry") or {}).get("tests") or {}).get(direction) or {})
                    image = (test.get("evidence", {}).get(phase) or {}).get("path")
                    root = (DATA_DIR / "devices" / device / "gesture_evidence").resolve()
                    if not image or not Path(image).resolve().is_relative_to(root):
                        raise ValueError("测试画面尚未保存")
                    self._send(200, Path(image).read_bytes(), "image/png")
                else:
                    self._json(view)
            elif path == "/api/log":
                limit = int(self._query().get("limit", "50"))
                lines = []
                if OPS_PATH.exists():
                    lines = OPS_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
                records = [json.loads(line) for line in lines if line.strip()]
                self._json({"ops": list(reversed(records))})
            elif path == "/api/log/export":
                body = OPS_PATH.read_bytes() if OPS_PATH.exists() else b""
                self._send(200, body, "application/jsonl; charset=utf-8",
                           {"Content-Disposition": "attachment; filename=motor_debug_ops.jsonl"})
            elif path == "/api/wizard/state":
                wizard = get_wizard(device)
                self._json(wizard.state_view())
            elif path == "/api/devices":
                self._json({"devices": list_arm_devices()})
            elif path == "/api/serial-ports":
                # 换口检测（用户拍板 2026-09-30）：串口扫描与绑定状态
                self._json(serial_ports_payload(query.get("device") or ""))
            elif path == "/api/device-context":
                context_id = query.get("context") or query.get("contextId")
                context = require_context(device, context_id)
                self._json(_context_view(context))
            elif path == "/api/wizard/ime-devices":
                self._json(list_ime_devices(device))
            elif path == "/api/wizard/gate":
                wizard = get_wizard(device)
                query = self._query()
                targets = [t.strip() for t in (query.get("targets") or "").split(",") if t.strip()]
                result = wizard.gate(
                    required_targets=targets or None,
                    wechat_send=query.get("wechatSend") in ("1", "true", "yes"))
                _sync_runtime_calibration(
                    device,
                    "validated" if result.get("ok") else "due",
                    "调试台统一校准门禁通过" if result.get("ok") else "调试台统一校准门禁未通过",
                )
                log_op("wizard_gate", {"device": device}, ok=result["ok"],
                       detail={"checks": result["checks"]})
                self._json(result)
            elif path == "/api/phone-search-region":
                # FIX-12：粗框读取（零动作）——同源代理到 8788 三个正式路由之一
                self._json(_auto_proxy(
                    "GET", f"/v1/devices/{device}/phone-search-region"))
            elif path == "/api/phone-search-region/version":
                # 粗框版本记录读取（零动作）：页面显示当前生效版本的相机/安装
                # 绑定用；身份/契约头与 GET region 同源（R2-23 受信转发）。
                raw_version = str(query.get("version") or "").strip()
                if not raw_version.isdigit() or int(raw_version) < 1:
                    raise ValueError("version 必须为正整数")
                self._json(_auto_proxy(
                    "GET", f"/v1/devices/{device}/phone-search-region"
                           f"/versions/{int(raw_version)}"))
            elif path == "/api/phone-search-region/frame":
                # FIX-12：粗框源预览（当前绑定相机的原帧，零动作）
                self._json(_region_source_frame(device))
            elif is_auto_proxy_path(path):
                # 只读代理（§12.3）：资格/导航/设置/任务查询，GET 不创建租约不执行动作
                if path == "/api/auto-gestures":
                    self._json(_auto_proxy(
                        "GET", f"/v1/devices/{device}/automatic-gestures"))
                elif path == "/api/auto-navigation":
                    self._json(_auto_proxy(
                        "GET", f"/v1/devices/{device}/automatic-navigation/"
                               f"{AUTO_NAVIGATION_PROFILE_ID}"))
                elif path == "/api/auto-gestures/settings":
                    self._json(_auto_proxy(
                        "GET", f"/v1/devices/{device}/automatic-gesture-settings"))
                elif path == "/api/auto-navigate":
                    task_id = str(query.get("taskId") or "").strip()
                    if not task_id:
                        raise ValueError("查询任务需要 taskId")
                    self._json(_auto_proxy("GET", f"/v1/tasks/{task_id}"))
                elif path.startswith("/api/navigation-template/"):
                    template_id = path.rsplit("/", 1)[-1]
                    self._json(_auto_proxy(
                        "GET", f"/v1/devices/{device}/navigation-templates/{template_id}"))
                else:
                    self._send(404, b"not found", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:
            # GET=轮询/只读（1.5~30s 周期刷新），失败由页面徽章/横幅即时呈现，
            # 不记 ops 日志——曾把 ops 日志刷成 98.8% 轮询噪音（7.4 万行里 7.3 万
            # 行 api_error；业主 2026-10-16 拍板：轮询失败不进库）。动作/写操作
            # 的失败仍由各自动作处理器内的 log_op(kind=api_xxx) 记录。
            status, payload = api_error(exc)
            self._json(payload, code=status)
        finally:
            if _installation_guard is not None:
                _installation_guard.__exit__(None, None, None)

    def do_POST(self) -> None:
        try:
            _debug_boundary_check(self, "POST")
        except _la.DebugBoundaryError as exc:
            self._json({"error": {"code": exc.code, "message": exc.message}},
                       code=exc.status)
            return
        path = self.path.split("?")[0]
        body = self._body()
        if path in {"/api/device-context", "/api/gestures/test", "/api/gestures/review",
                    "/api/gestures/preview", "/api/gestures/saved"}:
            try:
                if not isinstance(body, dict):
                    raise ValueError("请求必须是对象")
                raw_device = body.get("deviceId") if "deviceId" in body else body.get("device")
                strict_debug_device(raw_device)
                if "deviceId" in body and "device" in body and body["deviceId"] != body["device"]:
                    raise ValueError("设备编号冲突，禁止自动选择")
                if path in {"/api/gestures/test", "/api/gestures/review", "/api/gestures/saved"}:
                    body.setdefault("operator", "调试台")
            except Exception as exc:
                status, payload = api_error(exc)
                self._json(payload, code=status)
                return
        device = normal_device(body.get("deviceId") or body.get("device"))
        _REQUEST_CONTEXT.device = device
        daemon = daemon_for(device)
        # FIX-05（§4.1）：动作端点在认领前取得本设备共享安装文件锁（非阻塞，
        # 忙则立即 409 不排队）；持有到最后动作/安全收尾结束（finally 释放）。
        # 顺序=安装锁→原动作/输入短锁；STOP/急停/输入关闭/只读不经过这里。
        try:
            _installation_guard = _enter_installation_guard(device, path=path)
        except Exception as exc:
            log_op("installation_lock", {"path": path}, ok=False, detail=str(exc))
            status, payload = api_error(exc)
            self._json(payload, code=status)
            return
        try:
            if path == "/api/device-context":
                self._json(create_device_context(device))
            elif path == "/api/recovery/attempt":
                # A3-W：一键恢复复核——attempt_recovery 的生产接线。
                # motion=True：臂侧锚点验证是真实走位（有界、悬停不点击）。
                # F1（第七轮复审）：与兄弟运动端点同构的两道护栏——
                # 自动 run 在跑时恢复动臂会插进 runner 采样时序；
                # 冷启动 daemon 刚复位=调试台租约 None/过期，不补租约
                # 第一次点必败（恰是主场景）。
                _reject_motion_during_auto_run(device, "恢复复核")
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=True)
                daemon_for(device).ensure_debug_lease()
                try:
                    result = attempt_recovery_action(device)
                except Exception as exc:
                    log_op("recovery_attempt", {"device": device}, ok=False,
                           detail=str(exc))
                    self._json({"recovered": False, "stage": "error",
                                "reason": str(exc)})
                    return
                log_op("recovery_attempt", {"device": device},
                       ok=bool(result.get("recovered")), detail=result)
                self._json(result)
            elif path == "/api/serial-bind":
                # P0-1（2026-09-30 审查）：前端以 POST 调用（原误写在 do_GET，
                # POST 必 404）。绑定属配置写操作但免 device-context：与
                # /api/serial-ports 同层的换口自愈入口，调用方可能还没有上下文。
                self._json(serial_bind_payload(str(body.get("device") or ""),
                                               str(body.get("port") or "")))
            elif path == "/api/probe/register":
                context = require_context(device, body.get("contextId"), motion=False)
                if body.get("confirmed") is not True:
                    raise ValueError("请确认登记区域是机械探头实体")
                camera = _gesture_camera(device, context)

                async def register():
                    try:
                        frame = await camera.capture()
                        return register_probe(
                            frame, body.get("roi", {}), device_id=device,
                            data_root=DATA_DIR, operator=str(body.get("operator", "")))
                    finally:
                        await camera.close()

                record = asyncio.run(register())
                log_op("probe_register", {"device": device}, ok=True, detail=record)
                self._json(record)
            elif path == "/api/gestures/test":
                self._json(test_basic_gesture(device, body))
            elif path == "/api/gestures/preview":
                self._json(preview_calculated_gesture(device, body))
            elif path == "/api/gestures/saved":
                self._json(save_basic_gesture(device, body))
            elif path == "/api/gestures/review":
                self._json(review_basic_gesture(device, body))
            elif path.startswith("/api/screen-proof/"):
                self._json(save_screen_proof(device, body, path.rsplit("/", 1)[-1]))
            elif path == "/api/jog":
                require_context(device, body.get("contextId") or body.get("context"), motion=True)
                _reject_motion_during_auto_run(device, "点动")
                daemon.ensure_debug_lease()
                axis = str(body.get("axis", "")).lower()
                delta = float(body.get("delta", 0))
                feed = float(body.get("feed", 150 if axis != "z" else 30))
                mpos = do_jog(device, axis, delta, feed)
                log_op("jog", {"axis": axis, "delta": delta, "feed": feed},
                       ok=True, detail={"mpos": mpos})
                self._json({"mpos": mpos})
            elif path == "/api/tap":
                require_context(device, body.get("contextId") or body.get("context"), motion=True)
                _reject_motion_during_auto_run(device, "点击测试")
                daemon.ensure_debug_lease()
                depth = float(body.get("depth", 3.0))
                feed = float(body.get("feed", 1500.0))
                dwell = float(body.get("dwell", 0.0))
                _check_feed(feed, axis="Z")
                check_tap(depth, dwell)
                cmd = f"TAP {depth:.3f} F{feed:g}"
                if dwell > 0:
                    cmd += f" P{dwell:g}"
                with daemon.motion_scope():
                    mpos = json.loads(daemon.cmd(cmd))
                log_op("tap", {"depth": depth, "feed": feed, "dwell": dwell},
                       ok=True, detail={"mpos": mpos})
                self._json({"mpos": mpos})
            elif path == "/api/stop":
                require_context(device, body.get("contextId") or body.get("context"), emergency=True)
                resp = daemon.stop()
                log_op("stop", {}, ok=True, detail=resp)
                self._json(resp)
            elif path == "/api/resume":
                require_context(device, body.get("contextId") or body.get("context"), motion=True)
                _reject_motion_during_auto_run(device, "恢复运动")
                resp = daemon.resume()
                log_op("resume", {}, ok=True, detail=resp)
                self._json(resp)
            elif path == "/api/estop":
                require_context(device, body.get("contextId") or body.get("context"), emergency=True)
                reason = str(body.get("reason") or "调试台人工急停")
                resp = daemon.daemon_estop(reason)
                log_op("estop_lock", {"reason": reason}, ok=True, detail=resp)
                self._json(resp)
            elif path == "/api/estop_reset":
                require_context(device, body.get("contextId") or body.get("context"), motion=False)
                operator = str(body.get("operator") or "调试台操作者")
                resp = daemon.daemon_estop_reset(operator)
                log_op("estop_reset", {"operator": operator}, ok=True, detail=resp)
                self._json(resp)
            elif path == "/api/connect":
                require_context(device, body.get("contextId") or body.get("context"), motion=False)
                resp = daemon.connect_arm()
                with _LOCK:
                    for context in CONTEXTS.values():
                        if context.get("device_id") == device:
                            context["daemon_session"] = resp.get("session")
                            context["ready_for_motion"] = bool(resp.get("session"))
                log_op("connect", {}, ok=True, detail=resp)
                self._json(resp)
            elif path == "/api/disconnect":
                require_context(device, body.get("contextId") or body.get("context"), motion=False)
                resp = daemon.disconnect_arm()
                # 断开会清除上下文里的会话锚点，允许同一设备原地重连；
                # 重连生成新会话后，旧校准仍会由向导/门禁判定为失效。
                with _LOCK:
                    for context in CONTEXTS.values():
                        if context.get("device_id") == device:
                            context["daemon_session"] = None
                            context["ready_for_motion"] = False
                _sync_runtime_calibration(device, "due", "调试台手动断开机械臂，等待重新连接并校准")
                log_op("disconnect", {}, ok=True, detail=resp)
                self._json(resp)
            elif path == "/api/hover":
                require_context(device, body.get("contextId") or body.get("context"), motion=True)
                _reject_motion_during_auto_run(device, "回悬停")
                daemon.ensure_debug_lease()
                profile = target_archive.load_profile(device, DATA_DIR)
                hover_z = float(body["hoverZ"]) if body.get("hoverZ") is not None \
                    else float(profile["hoverZ"])
                feed = float(body.get("feed", 1500.0))
                _check_feed(feed, axis="Z")
                mpos = _move_z_to(device, hover_z, feed=feed)
                log_op("hover", {"hoverZ": hover_z, "feed": feed},
                       ok=True, detail={"mpos": mpos})
                self._json({"mpos": mpos})
            elif path == "/api/park":
                # 一键驶向停止位（SAFE PARK）：优先向导现场状态，回退档案；
                # 停止位是绝对机械坐标，会话锚点变化后拒绝执行（防旧坐标乱跑）。
                require_context(device, body.get("contextId") or body.get("context"), motion=True)
                _reject_motion_during_auto_run(device, "驶向停止位")
                daemon = daemon_for(device)
                daemon.ensure_debug_lease()
                wizard = get_wizard(device)
                park = dict(wizard.state.get("safePark") or {})
                source = "wizard_state"
                if not park.get("confirmed") or park.get("parkX") is None:
                    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
                    park = dict((bundle or {}).get("safePark") or {})
                    source = "bundle"
                if not park.get("confirmed") or park.get("parkX") is None:
                    raise ValueError("停止位（SAFE PARK）尚未保存：请先在 AUTO 区块"
                                     "「确认 SAFE PARK（停止位）」后再停靠")
                current_session = daemon.status().get("session")
                # 停车位按其自身确认会话核对（P09）：顶层会话更新不能替旧
                # 停车位背书。旧档停车位无自身会话时退回顶层会话兜底。
                park_session = park.get("daemonSession")
                if park_session:
                    if current_session and park_session != current_session:
                        raise RuntimeError("停止位确认于机械坐标会话 "
                                           f"{park_session}，当前会话 {current_session}："
                                           "坐标基准可能已变，请重新确认 SAFE PARK 后再停靠")
                else:
                    expected_session = wizard.state.get("daemonSession")
                    if source == "bundle":
                        bundle_session = (calibration_bundle.load_bundle(device, DATA_DIR) or {}) \
                            .get("daemonSession")
                        expected_session = expected_session or bundle_session
                    if expected_session and current_session \
                            and expected_session != current_session:
                        raise RuntimeError("机械臂会话已变化（重启/串口重连），保存的停止位"
                                           "坐标可能失效：请重新确认 SAFE PARK 后再停靠")
                profile = target_archive.load_profile(device, DATA_DIR)
                result = do_goto_robot(
                    device, float(park["parkX"]), float(park["parkY"]), tap=False,
                    hover_z=float(park.get("parkZ")
                                  if park.get("parkZ") is not None
                                  else profile["hoverZ"]),
                    feed_xy=CALIBRATED_TRAVEL_FEED,
                    tap_depth=float(profile["tapDepth"]),
                    tap_feed=float(profile["tapFeed"]))
                log_op("park", {"parkXY": [park.get("parkX"), park.get("parkY")],
                                "source": source}, ok=True, detail=result)
                self._json(result)
            elif path == "/api/goto":
                require_context(device, body.get("contextId") or body.get("context"), motion=True)
                _reject_motion_during_auto_run(device, "画面走位")
                daemon.ensure_debug_lease()
                nx = float(body["nx"]); ny = float(body["ny"])
                bundle = calibration_bundle.load_bundle(device, DATA_DIR)
                frame = (bundle or {}).get("frame") or {}
                frame_w = float(frame.get("width") or 0)
                frame_h = float(frame.get("height") or 0)
                if frame_w <= 0 or frame_h <= 0:
                    raise RuntimeError("暂无标定帧尺寸信息：请先完成统一校准")
                if not 0 <= nx <= 1 or not 0 <= ny <= 1:
                    raise ValueError("画面坐标 nx/ny 必须在 0~1 范围内")
                cx = nx * frame_w
                cy = ny * frame_h
                profile = target_archive.load_profile(device, DATA_DIR)
                result = do_goto(
                    device,
                    cx, cy,
                    tap=bool(body.get("tap", False)),
                    hover_z=float(body["hoverZ"]) if body.get("hoverZ") is not None
                    else float(profile["hoverZ"]),
                    feed_xy=float(body.get("feedXY", CALIBRATED_TRAVEL_FEED)),
                    tap_depth=float(body["tapDepth"]) if body.get("tapDepth") is not None
                    else float(profile["tapDepth"]),
                    tap_feed=float(body["tapFeed"]) if body.get("tapFeed") is not None
                    else float(profile["tapFeed"]),
                )
                log_op("goto", {"nx": round(nx, 4), "ny": round(ny, 4),
                                "tap": bool(body.get("tap", False)),
                                "cameraXY": [round(cx, 1), round(cy, 1)]},
                       ok=True, detail=result)
                self._json(result)
            elif path == "/api/targets":
                require_context(device, body.get("contextId") or body.get("context"), motion=False)
                # 目标登记（唯一写入口，转发到 target_archive）：只存画面坐标与 Z 参数，
                # 机械坐标执行期按当前标定现算；未标定也允许先登记（标定后自动生效）。
                name = str(body.get("name", "")).strip()
                entry = target_archive.save_target(device, name, {
                    "nx": float(body["nx"]), "ny": float(body["ny"]),
                    "hoverZ": body.get("hoverZ"), "tapDepth": body.get("tapDepth"),
                    "tapFeed": body.get("tapFeed"),
                    "targetMode": DEFAULT_TARGET_MODES.get(name),
                }, DATA_DIR)
                # 目标登记不修改设备级 Z/点击校准；这些参数只能由校准/参数保存流程更新。
                profile = target_archive.load_profile(device, DATA_DIR)
                st = calibration_state(device)
                bundle = calibration_bundle.load_bundle(device, DATA_DIR)
                frame = (bundle or {}).get("frame") or {}
                resolved = None
                if st["usable"] and float(frame.get("width") or 0) > 0:
                    try:
                        rx, ry = camera_to_robot(
                            device, entry["nx"] * float(frame["width"]),
                            entry["ny"] * float(frame["height"]))
                        resolved = [round(rx, 2), round(ry, 2)]
                    except Exception:
                        resolved = None
                log_op("target_save", {"device": device, "name": name}, ok=True,
                       detail={"entry": entry, "robotXYAtSave": resolved})
                self._json({"saved": name, "entry": entry,
                            "robotXYAtSave": resolved, "profile": profile})
            elif path.startswith("/api/ui-targets/"):
                action = path.rsplit("/", 1)[-1]
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=action in ("test", "delete-test"))
                operator = str(body.get("operator") or "").strip() or "motor-debug-console"
                target_id = str(body.get("targetId", "")).strip()
                if action == "draft":
                    # 只接受页面「仅标记」的 lastMark 坐标；预置目标模式固定，
                    # 自定义目标必须显式提供模式/名称/所属插件（ui_targets 校验）。
                    # 校准验收期（E 验收先于 promote）由向导登记窗口放行 bindPending 登记。
                    registration_window = None
                    try:
                        registration_window = get_wizard(device).ui_target_registration_window()
                    except Exception:
                        registration_window = None
                    with _LOCK:
                        entry = ui_targets.register_draft(
                            device, target_id,
                            nx=float(body["nx"]), ny=float(body["ny"]),
                            target_mode=body.get("targetMode") or None,
                            name_display=body.get("nameDisplay") or None,
                            owner_plugin=body.get("ownerPlugin") or None,
                            required_ui_state=body.get("requiredUiState") or None,
                            risk_level=body.get("riskLevel") or None,
                            operator=operator, data_root=DATA_DIR,
                            registration_window=registration_window)
                    log_op("ui_target_draft", {"targetId": target_id}, ok=True,
                           detail={"version": entry["version"], "nx": entry["nx"],
                                   "ny": entry["ny"], "targetMode": entry["targetMode"]})
                    self._json({"entry": ui_targets.entry_view(
                        entry, calibration_bundle.load_bundle(device, DATA_DIR))})
                elif action == "test":
                    # 受控测试（规格书 §4.2）：tap 目标真实走位+点击一次；
                    # hover_only 目标只走位停悬停，绝不 TAP。坐标换算走业务
                    # 运行时同一解析链（bundle 优先），保证测试验证的就是
                    # 业务映射——不能用 do_goto（其 camera_to_robot 优先旧版
                    # calibration.json 仿射，两套档案并存时会验证到另一套映射）。
                    daemon = daemon_for(device)
                    daemon.ensure_debug_lease()
                    entry = ui_targets.load_entry(device, target_id, DATA_DIR)
                    if entry is None:
                        raise ValueError(f"目标 {target_id} 尚未登记")
                    if entry.get("state") != ui_targets.STATE_DRAFT:
                        raise ValueError(
                            f"目标 {target_id} 状态为 "
                            f"{ui_targets.STATE_DISPLAY.get(entry.get('state'))}，只有草稿可执行受控测试")
                    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
                    # A4②（2026-10-01 拍板）：受控测试允许 motion_ready——
                    # 机械资格完整即可验证目标映射；测试结果仍入 draft，
                    # 启用（业务门禁）维持 ready 要求不变。
                    if not bundle or bundle.get("status") not in (
                            "ready", calibration_bundle.MOTION_READY_STATUS):
                        raise ValueError("统一校准档案未就绪（非 ready/motion_ready）："
                                         "请先完成自动校准提升")
                    mismatches = ui_targets.binding_mismatches(entry, bundle)
                    if mismatches:
                        raise ValueError("目标绑定已失效（待复核）：" + "；".join(mismatches))
                    daemon_session = daemon.status().get("session")
                    if bundle.get("daemonSession") and daemon_session \
                            and bundle["daemonSession"] != daemon_session:
                        raise RuntimeError("机械臂已重启（会话锚点不匹配），统一映射失效：请重新校准")
                    calibration = target_archive.load_runtime_calibration(device, DATA_DIR)
                    profile = target_archive.load_profile(device, DATA_DIR)
                    resolved = target_archive.resolve_target(
                        ui_targets.resolve_entry(entry, bundle, require_enabled=False),
                        calibration, profile)
                    tap = resolved["targetMode"] == "tap"
                    result = do_goto_robot(
                        device, resolved["robotX"], resolved["robotY"], tap=tap,
                        hover_z=float(profile["hoverZ"]), feed_xy=CALIBRATED_TRAVEL_FEED,
                        tap_depth=float(profile["tapDepth"]),
                        tap_feed=float(profile["tapFeed"]))
                    evidence = {"kind": "tap_test" if tap else "hover_test",
                                "tapExecuted": tap,
                                "robotXY": result.get("targetRobot"),
                                "mposAfter": result.get("mpos"),
                                "movedMm": result.get("movedMm"),
                                "executedAt": calibration_bundle.now_iso()}
                    log_op("ui_target_test", {"targetId": target_id}, ok=True,
                           detail=evidence)
                    self._json({"result": result, "evidence": evidence})
                elif action == "delete-test":
                    # A4 附（2026-10-01 拍板）：长按删除键——人工查看标定映射
                    # 位置是否正确。只执行动作与留证，不做任何判定，不参与
                    # 任何门禁逻辑（正确与否由人工目视确认）。
                    daemon = daemon_for(device)
                    daemon.ensure_debug_lease()
                    if target_id != "ime_delete_key":
                        raise ValueError("delete-test 仅用于删除键（ime_delete_key）")
                    entry = ui_targets.load_entry(device, target_id, DATA_DIR)
                    if entry is None:
                        raise ValueError(f"目标 {target_id} 尚未登记")
                    bundle = calibration_bundle.load_bundle(device, DATA_DIR)
                    if not bundle or bundle.get("status") not in (
                            "ready", calibration_bundle.MOTION_READY_STATUS):
                        raise ValueError("统一校准档案未就绪：请先完成自动校准提升")
                    mismatches = ui_targets.binding_mismatches(entry, bundle)
                    if mismatches:
                        raise ValueError("目标绑定已失效（待复核）：" + "；".join(mismatches))
                    calibration = target_archive.load_runtime_calibration(device, DATA_DIR)
                    profile = target_archive.load_profile(device, DATA_DIR)
                    resolved = target_archive.resolve_target(
                        ui_targets.resolve_entry(entry, bundle, require_enabled=False),
                        calibration, profile)
                    z = bundle.get("z") or {}
                    delete_depth = float(z.get("deleteDepth", profile["tapDepth"]))
                    delete_feed = float(z.get("deleteFeed", profile["tapFeed"]))
                    hold_ms = int(z.get("deleteHoldMs", 2000))
                    result = do_goto_robot(
                        device, resolved["robotX"], resolved["robotY"], tap=False,
                        hover_z=float(profile["hoverZ"]), feed_xy=CALIBRATED_TRAVEL_FEED,
                        tap_depth=delete_depth, tap_feed=delete_feed)
                    with daemon.motion_scope():
                        result["mpos"] = json.loads(daemon.cmd(
                            f"TAP {delete_depth:.3f} F{delete_feed:g} P{hold_ms / 1000.0:g}"))
                    evidence = {"kind": "delete_long_press_preview",
                                "holdMs": hold_ms,
                                "robotXY": result.get("targetRobot"),
                                "mposAfter": result.get("mpos"),
                                "note": "人工查看：请目视确认长按落在删除键且输入框被清空；"
                                        "本动作不参与任何门禁判定"}
                    log_op("ui_target_delete_preview", {"targetId": target_id},
                           ok=True, detail=evidence)
                    self._json({"result": result, "evidence": evidence})
                elif action == "confirm":
                    with _LOCK:
                        entry = ui_targets.mark_test_result(
                            device, target_id, passed=bool(body.get("passed", True)),
                            evidence=body.get("evidence") or None,
                            operator=operator, data_root=DATA_DIR)
                    log_op("ui_target_confirm", {"targetId": target_id}, ok=True,
                           detail={"passed": bool(body.get("passed", True))})
                    self._json({"entry": ui_targets.entry_view(
                        entry, calibration_bundle.load_bundle(device, DATA_DIR))})
                elif action == "enable":
                    with _LOCK:
                        entry = ui_targets.set_enabled(
                            device, target_id, enabled=True, operator=operator,
                            data_root=DATA_DIR)
                    log_op("ui_target_enable", {"targetId": target_id}, ok=True,
                           detail={"version": entry["version"]})
                    self._json({"entry": ui_targets.entry_view(
                        entry, calibration_bundle.load_bundle(device, DATA_DIR))})
                elif action == "disable":
                    with _LOCK:
                        entry = ui_targets.set_enabled(
                            device, target_id, enabled=False, operator=operator,
                            data_root=DATA_DIR)
                    log_op("ui_target_disable", {"targetId": target_id}, ok=True,
                           detail={"version": entry["version"]})
                    self._json({"entry": ui_targets.entry_view(
                        entry, calibration_bundle.load_bundle(device, DATA_DIR))})
                else:
                    self._send(404, b"not found", "text/plain")
            elif path.startswith("/api/gesture-profiles/"):
                action = path.rsplit("/", 1)[-1]
                require_context(device, body.get("contextId") or body.get("context"), motion=False)
                profile_id = body.get("profileId", "")
                session = daemon.status().get("session")
                operator = str(body.get("operator") or "").strip()
                if action == "test":
                    self._json(test_gesture_profile(device, body))
                elif action == "draft":
                    gesture_profiles.save_draft(
                        device, profile_id, body.get("config"), operator=operator,
                        current_session=session, data_root=DATA_DIR,
                    )
                    self._json(gesture_profile_view(device, profile_id))
                elif action in {"enable", "disable"}:
                    gesture_profiles.set_enabled(
                        device, profile_id, enabled=action == "enable",
                        expected_revision=body.get("expectedRevision"), operator=operator,
                        current_session=session, human_confirmed=body.get("confirmed") is True,
                        data_root=DATA_DIR,
                    )
                    self._json(gesture_profile_view(device, profile_id))
                else:
                    raise ValueError("未知手势档案操作")
            elif path == "/api/wizard":
                action = str(body.get("action", "")).strip()
                # FIX-05（§4.1）：自动校准动作入口认领前取共享安装锁；
                # 状态/保存/确认/停止类不取锁（动作持锁期间仍可用）。
                with (_shared_installation_lock(device)
                      if action in _WIZARD_INSTALLATION_LOCKED_ACTIONS
                      else nullcontext()):
                    require_context(device, body.get("contextId") or body.get("context"),
                                    motion=False)
                    wizard = get_wizard(device)
                    result = wizard_http(wizard, action, body)
                if action == "auto-state":
                    # A5：页面轮询兜底触发 PROMOTE 平台同步（run 去重防双发）
                    try:
                        _maybe_post_promote_sync(device, result)
                    except Exception:
                        pass
                log_op(f"wizard_{action}", {"device": device}, ok=True, detail=result)
                self._json(result)
            elif path == "/api/phone-search-region/preview":
                # FIX-12/R2-15：粗框预览（零动作，只写资料）——页面原始请求
                # {deviceId, contextId, rect{x,y,width,height},
                #  source_evidence_id}；代理只补关联字段，经 R2-23 受信转发
                # 身份头发送；installation_binding/source_size 等可信字段由
                # Fleet 服务端从证据链读取，页面/代理不申报。
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=False)
                rect = body.get("rect")
                if not isinstance(rect, dict):
                    raise ValueError("预览缺少 rect（source_norm {x,y,width,height}）")
                upstream = {
                    "contract_version": "phone-search-region-preview.v1",
                    "correlation_id": str(uuid.uuid4()),
                    "rect": rect,
                    "source_evidence_id": str(body.get("source_evidence_id") or ""),
                }
                self._json(_auto_proxy(
                    "POST", f"/v1/devices/{device}/phone-search-region/preview",
                    upstream))
            elif path == "/api/phone-search-region/delete":
                require_context(device, body.get("contextId"), motion=False)
                # DELETE 契约 body 必须为 null（api:empty）；带体被 wire 拒。
                self._json(_auto_proxy(
                    "DELETE", f"/v1/devices/{device}/phone-search-region", None))

            elif path == "/api/phone-search-region/save":
                # FIX-12/R2-15：粗框保存——冻结 save_request 闭集
                # {contract_version, correlation_id, client_request_id,
                #  expected_revision, preview_id, region_digest, publish}。
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=False)
                upstream = {
                    "contract_version": "phone-search-region-write.v1",
                    "correlation_id": str(uuid.uuid4()),
                    "client_request_id": str(
                        body.get("client_request_id") or uuid.uuid4()),
                    "expected_revision": int(body.get("expected_revision") or 0),
                    "preview_id": str(body.get("preview_id") or ""),
                    "region_digest": str(body.get("region_digest") or ""),
                    "publish": body.get("publish") is True,
                }
                self._json(_auto_proxy(
                    "PUT", f"/v1/devices/{device}/phone-search-region", upstream))
            elif is_auto_proxy_path(path):
                # 代理写（§13.4）：与正式同一 task_type/端点，操作者上下文由代理注入。
                # 上下文门禁（评审 P1-15）：这些端点会排队物理动作（validate 一批
                # 最多 36 个）；跨站简单请求（text/plain 免预检）伪造不了页面
                # contextId，与运动端点同层防线。
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=False)
                if path == "/api/auto-gestures/validate":
                    upstream = _auto_operator_context(body, "调试台测试手势")
                    if isinstance(body.get("actions"), list):
                        upstream["actions"] = body["actions"]
                    result = _auto_proxy(
                        "POST", f"/v1/devices/{device}/automatic-gestures/validate",
                        upstream)
                    log_op("auto_gesture_validate", {"device": device}, ok=True,
                           detail={"task_id": result.get("task_id"),
                                   "status": result.get("status")})
                    self._json(result)
                elif path == "/api/auto-navigate":
                    result = _submit_debug_navigation(device, body)
                    self._json(result)
                elif path == "/api/navigation-runs/close":
                    navigation_id = str(body.get("navigationId") or "").strip()
                    reason = str(body.get("reason") or "").strip()
                    if not navigation_id:
                        raise ValueError("close 需要 navigationId")
                    if reason not in ("caller_finished", "caller_cancelled"):
                        raise ValueError("reason 必须为 caller_finished 或 caller_cancelled")
                    upstream = _auto_operator_context(body, "调试台收口导航轮")
                    upstream["reason"] = reason
                    result = _auto_proxy(
                        "POST",
                        f"/v1/devices/{device}/navigation-runs/{navigation_id}/close",
                        upstream)
                    log_op("navigation_run_close", {"device": device}, ok=True,
                           detail={"navigation_id": navigation_id, "reason": reason})
                    self._json(result)
                elif path == "/api/navigation-template-preview":
                    upstream = _auto_operator_context(body, "调试台采集导航模板预览")
                    if body.get("kind"):
                        upstream["kind"] = body["kind"]
                    self._json(_auto_proxy(
                        "POST", f"/v1/devices/{device}/navigation-template-previews",
                        upstream))
                else:
                    self._send(404, b"not found", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:
            log_op(path.strip("/").replace("/", "_"), body, ok=False, detail=str(exc))
            status, payload = api_error(exc)
            self._json(payload, code=status)
        finally:
            if _installation_guard is not None:
                _installation_guard.__exit__(None, None, None)

    def do_PUT(self) -> None:
        try:
            _debug_boundary_check(self, "PUT")
        except _la.DebugBoundaryError as exc:
            self._json({"error": {"code": exc.code, "message": exc.message}},
                       code=exc.status)
            return
        path = self.path.split("?")[0]
        query = self._query()
        body = self._body()
        device = normal_device(query.get("deviceId") or query.get("device")
                               or body.get("deviceId") or body.get("device"))
        _REQUEST_CONTEXT.device = device
        try:
            if path == "/api/input-settings":
                # §4.2/§4.2.3：开关写代理——页面传 enabled/expected_revision/ime_device_id，
                # 操作者上下文由此注入；client_request_id 由页面生成并在重试间保持，
                # 便于按原请求编号对账（§4.3 保存结果待核对）。
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=False)
                upstream = {
                    "contract_version": "device-input-settings.v1",
                    "enabled": body.get("enabled"),
                    "expected_revision": body.get("expectedRevision"),
                    "ime_device_id": body.get("imeDeviceId"),
                    "operator_id": str(body.get("operatorId") or "motor-debug-console"),
                    "role": "operator",
                    "reason": str(body.get("reason") or "").strip() or "调试台更新允许输入与发送开关",
                    "correlation_id": str(body.get("correlationId")
                                          or f"mdebug-{uuid.uuid4().hex[:16]}"),
                    "client_request_id": str(body.get("clientRequestId")
                                             or f"mdebug-{uuid.uuid4().hex[:16]}"),
                }
                if upstream["enabled"] and not upstream["ime_device_id"]:
                    # §4.2.1：开启必填手机编号——页面不填时注入当前唯一绑定；
                    # 绑定缺失/不一致仍由 Fleet 按 input_binding_* 拒绝。
                    try:
                        upstream["ime_device_id"] = list_ime_devices(device).get("bound")
                    except Exception:
                        upstream["ime_device_id"] = None
                status, payload = _agent_passthrough(
                    "PUT", f"/v1/devices/{device}/input-settings", upstream)
                log_op("input_settings_put", {"device": device}, ok=status == 200,
                       detail={"enabled": upstream["enabled"],
                               "expected_revision": upstream["expected_revision"],
                               "http": status})
                self._json(payload, code=status)
            elif path == "/api/auto-gestures/settings":
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=False)
                updates = body.get("updates")
                if not isinstance(updates, dict) or not updates:
                    raise ValueError("updates 必须为非空对象")
                # 页面只传 updates；operator_id/reason/correlation_id 由此注入
                upstream = {"updates": updates,
                            **_auto_operator_context(body, "调试台更新开关")}
                result = _auto_proxy(
                    "PUT", f"/v1/devices/{device}/automatic-gesture-settings", upstream)
                log_op("auto_gesture_settings", {"device": device}, ok=True,
                       detail={"updates": sorted(updates)})
                self._json(result)
            elif path.startswith("/api/navigation-template/"):
                require_context(device, body.get("contextId") or body.get("context"),
                                motion=False)
                template_id = path.rsplit("/", 1)[-1]
                upstream = {key: value for key, value in body.items()
                            if key not in ("deviceId", "device", "contextId")}
                upstream.update(_auto_operator_context(body, "调试台登记导航模板"))
                result = _auto_proxy(
                    "PUT", f"/v1/devices/{device}/navigation-templates/{template_id}",
                    upstream)
                log_op("navigation_template_put", {"device": device}, ok=True,
                       detail={"template_id": template_id})
                self._json(result)
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:
            log_op(path.strip("/").replace("/", "_"), body, ok=False, detail=str(exc))
            status, payload = api_error(exc)
            self._json(payload, code=status)

    def do_DELETE(self) -> None:
        try:
            _debug_boundary_check(self, "DELETE")
        except _la.DebugBoundaryError as exc:
            self._json({"error": {"code": exc.code, "message": exc.message}},
                       code=exc.status)
            return
        path = self.path.split("?")[0]
        query = self._query()
        device = normal_device(query.get("deviceId") or query.get("device"))
        _REQUEST_CONTEXT.device = device
        try:
            if path.startswith("/api/device-context/"):
                context_id = path.rsplit("/", 1)[-1]
                self._json(close_device_context(context_id))
            elif path == "/api/ui-targets":
                require_context(device, query.get("contextId") or query.get("context"),
                                motion=False)
                target_id = str(query.get("targetId", "")).strip()
                if not target_id:
                    raise ValueError("删除固定目标需要 targetId")
                with _LOCK:
                    ui_targets.delete_target(device, target_id,
                                             operator=str(query.get("operator") or "").strip()
                                             or "motor-debug-console", data_root=DATA_DIR)
                log_op("ui_target_delete", {"targetId": target_id}, ok=True,
                       detail="固定目标已删除（审计保留）")
                self._json({"ok": True, "deleted": target_id})
            elif path == "/api/log":
                require_context(device, query.get("contextId") or query.get("context"),
                                motion=False)
                with _LOCK:
                    OPS_PATH.write_text("", encoding="utf-8")
                self._json({"cleared": True})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:
            status, payload = api_error(exc)
            self._json(payload, code=status)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    display_host = "127.0.0.1" if LISTEN_HOST in ("0.0.0.0", "::") else LISTEN_HOST
    print(f"电机调试台已启动：http://{display_host}:{LISTEN_PORT}  "
          f"（已登记 {len(DEVICE_CONFIGS)} 台设备，摄像头与 daemon 按设备解析）", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
