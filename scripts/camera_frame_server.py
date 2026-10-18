"""本机多摄像头帧接收服务。

正式链路：
CameraHelper.app -> POST /cameras/{camera_id}/frame（X-Frame-Source: camera_helper）
Arm Fleet       -> GET  /cameras/{camera_id}/latest.jpg

Chrome 页面和 /frame、/latest.jpg、/status 只保留单路兼容用途；兼容来源帧必须
携带 X-Frame-Source: chrome_compat 标记，不与正式 CameraHelper 双写同一通道。
业务多设备一律使用 /cameras/{camera_id}/... 分路接口；scripts/g_live_motion.py
等旧脚本引用的 /latest.jpg 属单路调试兼容用途（见 docs/部署与环境变量.md）。

配置（环境变量或 configure_state 参数，均可测试覆盖）：
CAMERA_STALE_AFTER_SECONDS      画面过期阈值（默认 1.5 秒）
CAMERA_UNAVAILABLE_AFTER_SECONDS 不可用阈值（默认 10 秒）
CAMERA_MAX_FRAME_BYTES          单帧最大 JPEG 字节（默认 8MB）
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAMERA_ID = "cam-01"
DEFAULT_CAMERA_IDS = tuple(f"cam-{index:02d}" for index in range(1, 11))
DEFAULT_MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_STALE_AFTER_SECONDS = 1.5
DEFAULT_UNAVAILABLE_AFTER_SECONDS = 10.0
DEFAULT_BINDINGS_PATH = ROOT / "data" / "camera_helper" / "bindings.json"
DEFAULT_DEVICES_PATH = ROOT / "configs" / "devices.json"
DEFAULT_EVENTS_PATH = ROOT / "data" / "camera_helper" / "events.jsonl"

# 帧来源标记（内部键 -> 中文显示）。兼容来源不得伪装成正式来源。
FRAME_SOURCES: dict[str, str] = {
    "camera_helper": "CameraHelper 正式采集",
    "chrome_compat": "Chrome 兼容降级桥",
    "legacy": "旧单路兼容来源",
}
FRAME_LOCK = threading.RLock()
LEGACY_CAMERA_ID: str | None = None
BINDINGS_PATH = DEFAULT_BINDINGS_PATH
# §4（camera-binding 附件，B01）：CAMERA_CONTROL_TOKEN_FILE 配置后启用 v2 写
# 身份与绑定 v2 路由；未配置=纯 v1 兼容行为（测试/旧部署不受影响）。
CONTROL_TOKEN_FILE = Path(os.environ.get("CAMERA_CONTROL_TOKEN_FILE", "")) \
    if os.environ.get("CAMERA_CONTROL_TOKEN_FILE") else None
V2_ENABLED = CONTROL_TOKEN_FILE is not None
EVENTS_PATH = DEFAULT_EVENTS_PATH
BINDING_REVISION = 0
BINDINGS: dict[str, dict] = {}
CAMERA_CONFIGS: dict[str, dict] = {}
CHANNELS: dict[str, dict] = {}
MAX_FRAME_BYTES = DEFAULT_MAX_FRAME_BYTES
STALE_AFTER_SECONDS = DEFAULT_STALE_AFTER_SECONDS
UNAVAILABLE_AFTER_SECONDS = DEFAULT_UNAVAILABLE_AFTER_SECONDS
# FIX-05（§4.1）：安装文件锁根，必须与 Fleet/调试台从同一 data_root 派生
# （默认按 BINDINGS_PATH 反推：<data_root>/camera_helper/bindings.json → data_root）。
INSTALLATION_DATA_ROOT = Path(DEFAULT_BINDINGS_PATH).parents[1]
# FIX-06（B03）：v2 帧桥证据归档。配置 fleet_db_path + evidence_dir 后，帧桥收到
# 合法 v2 帧即通过 runtime.evidence.EvidenceStore.save_source_frame_v2 归档
# camera_source_v2 证据（Fleet API 三条只读来源路由读同一份归档）。
SOURCE_EVIDENCE_DB: Path | None = None
SOURCE_EVIDENCE_DIR: Path | None = None
_SOURCE_EVIDENCE_STORE = None
# 通道事件审计（内存环形 + JSONL 落盘），按时间倒序可查
CHANNEL_EVENTS: list[dict] = []
MAX_CHANNEL_EVENTS = 500

EVENT_DISPLAY: dict[str, str] = {
    "service_restart": "帧服务重启",
    "binding_saved": "绑定已保存",
    "binding_deleted": "绑定已删除",
    "binding_conflict": "绑定冲突被拒绝",
    "frame_source_changed": "帧来源切换",
    "camera_source_conflict": "来源冲突被拒绝",
    "resolution_mismatch": "分辨率不匹配",
    "frame_stale": "画面过期",
    "frame_unavailable": "画面不可用",
    "frame_recovered": "画面恢复",
    "binding_mismatch": "上传身份与绑定不一致",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _camera_id(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if not value:
        raise ValueError("camera_id 不能为空")
    if not value.startswith("cam-") or not value[4:].isdigit():
        raise ValueError("camera_id 必须使用 cam-数字格式")
    return value


def _load_device_configs(path: Path) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        payload = {"devices": []}
    configs: dict[str, dict] = {}
    for raw in payload.get("devices", []):
        camera_id = str(raw.get("camera_id") or "").strip().lower()
        if not camera_id:
            continue
        configs[camera_id] = {
            "camera_id": camera_id,
            "device_id": str(raw.get("device_id") or ""),
            "expected_width": raw.get("expected_frame_width"),
            "expected_height": raw.get("expected_frame_height"),
        }
    # Only synthesize the ten-channel compatibility layout when no device
    # configuration exists at all.  A deliberate single-device production
    # config must not silently reintroduce arm-02..arm-10 as selectable
    # simulation channels.
    if not configs:
        for camera_id in DEFAULT_CAMERA_IDS:
            suffix = camera_id.rsplit("-", 1)[-1]
            configs.setdefault(camera_id, {
                "camera_id": camera_id,
                "device_id": f"arm-{suffix}",
                "expected_width": None,
                "expected_height": None,
            })
    return configs


def _load_bindings(path: Path) -> tuple[int, dict[str, dict]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0, {}
    bindings: dict[str, dict] = {}
    for raw in payload.get("bindings", []):
        try:
            camera_id = _camera_id(raw.get("cameraId"))
        except ValueError:
            continue
        unique_id = str(raw.get("deviceUniqueId") or "").strip()
        if not unique_id:
            continue
        bindings[camera_id] = {
            "camera_id": camera_id,
            "device_unique_id": unique_id,
            "display_name": str(raw.get("displayName") or "摄像头"),
            "bound_at": raw.get("boundAt"),
            "bound_by": raw.get("boundBy"),
        }
    return int(payload.get("revision") or 0), bindings


def _new_channel(camera_id: str) -> dict:
    cfg = CAMERA_CONFIGS.get(camera_id) or {}
    suffix = camera_id.rsplit("-", 1)[-1]
    return {
        "camera_id": camera_id,
        "device_id": str(cfg.get("device_id") or f"arm-{suffix}"),
        "frame": b"",
        "seq": 0,
        "source_seq": None,
        "captured_at": None,
        "received_at": None,
        "received_epoch": None,
        "width": None,
        "height": None,
        "source": None,
        "last_status": None,
        "last_error": None,
    }


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _audit_event(kind: str, camera_id: str | None = None, detail: dict | None = None) -> dict:
    """通道/服务级事件审计：内存环形缓冲 + JSONL 落盘（重启后可查历史文件）。"""
    event = {
        "at": _utc_iso(),
        "kind": kind,
        "kind_display": EVENT_DISPLAY.get(kind, kind),
        "camera_id": camera_id,
        "detail": detail or {},
    }
    with FRAME_LOCK:
        CHANNEL_EVENTS.append(event)
        if len(CHANNEL_EVENTS) > MAX_CHANNEL_EVENTS:
            del CHANNEL_EVENTS[:len(CHANNEL_EVENTS) - MAX_CHANNEL_EVENTS]
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return event


def _events_for(camera_id: str | None = None, limit: int = 50) -> list[dict]:
    with FRAME_LOCK:
        events = [dict(item) for item in CHANNEL_EVENTS
                  if camera_id is None or item.get("camera_id") == camera_id]
    events.reverse()
    return events[:max(1, min(int(limit), MAX_CHANNEL_EVENTS))]


def configure_state(
    *,
    bindings_path: Path | None = None,
    devices_path: Path | None = None,
    legacy_camera_id: str | None = None,
    stale_after_seconds: float | None = None,
    unavailable_after_seconds: float | None = None,
    max_frame_bytes: int | None = None,
    events_path: Path | None = None,
    record_restart: bool = True,
    data_root: Path | None = None,
    fleet_db_path: Path | None = None,
    evidence_dir: Path | None = None,
) -> None:
    """重新装载服务状态；测试用临时文件时也从这里进入。

    重启语义：绑定文件可恢复，但所有通道重建为空帧（已配置/等待新帧），
    旧帧与旧实时标记一律不保留；内存事件审计同时清空（JSONL 历史保留），
    服务重启时写入 service_restart 审计事件（模块导入时不写，避免测试副作用）。

    FIX-05/06：data_root 派生安装锁根（installation_locks/{device_id}.lock）；
    fleet_db_path+evidence_dir 启用 v2 帧桥证据归档（camera_source_v2 → Fleet
    EvidenceStore，三条只读来源路由消费同一份归档）。
    """
    global BINDINGS_PATH, BINDING_REVISION, BINDINGS, CAMERA_CONFIGS
    global CHANNELS, LEGACY_CAMERA_ID, MAX_FRAME_BYTES
    global STALE_AFTER_SECONDS, UNAVAILABLE_AFTER_SECONDS, EVENTS_PATH
    global INSTALLATION_DATA_ROOT, SOURCE_EVIDENCE_DB, SOURCE_EVIDENCE_DIR
    global _SOURCE_EVIDENCE_STORE
    BINDINGS_PATH = bindings_path or DEFAULT_BINDINGS_PATH
    EVENTS_PATH = events_path or DEFAULT_EVENTS_PATH
    if data_root is not None:
        INSTALLATION_DATA_ROOT = Path(data_root)
    else:
        # 与默认部署一致：bindings.json 位于 <data_root>/camera_helper/ 下；
        # 测试扁平路径（tmp/bindings.json）回退到绑定文件所在目录。
        INSTALLATION_DATA_ROOT = (BINDINGS_PATH.parent.parent
                                  if BINDINGS_PATH.parent.name == "camera_helper"
                                  else BINDINGS_PATH.parent)
    SOURCE_EVIDENCE_DB = Path(fleet_db_path) if fleet_db_path else None
    SOURCE_EVIDENCE_DIR = Path(evidence_dir) if evidence_dir else None
    _SOURCE_EVIDENCE_STORE = None
    CAMERA_CONFIGS = _load_device_configs(devices_path or DEFAULT_DEVICES_PATH)
    BINDING_REVISION, BINDINGS = _load_bindings(BINDINGS_PATH)
    LEGACY_CAMERA_ID = _camera_id(legacy_camera_id) if legacy_camera_id else None
    MAX_FRAME_BYTES = int(max_frame_bytes or _env_int("CAMERA_MAX_FRAME_BYTES", DEFAULT_MAX_FRAME_BYTES))
    STALE_AFTER_SECONDS = float(stale_after_seconds or _env_float("CAMERA_STALE_AFTER_SECONDS", DEFAULT_STALE_AFTER_SECONDS))
    UNAVAILABLE_AFTER_SECONDS = float(unavailable_after_seconds or _env_float("CAMERA_UNAVAILABLE_AFTER_SECONDS", DEFAULT_UNAVAILABLE_AFTER_SECONDS))
    with FRAME_LOCK:
        CHANNELS = {
            camera_id: _new_channel(camera_id)
            for camera_id in sorted(CAMERA_CONFIGS)
        }
        CHANNEL_EVENTS.clear()
    if record_restart:
        _audit_event("service_restart", detail={
            "binding_revision": BINDING_REVISION,
            "bound_cameras": sorted(BINDINGS),
            "note": "服务重启：绑定已恢复，所有通道回到已配置/等待新帧状态，旧帧不冒充实时",
        })


def _persist_bindings() -> None:
    payload = {
        "schemaVersion": 1,
        "revision": BINDING_REVISION,
        "bindings": [
            {
                "cameraId": camera_id,
                "deviceUniqueId": item["device_unique_id"],
                "displayName": item["display_name"],
                "boundAt": item.get("bound_at"),
                "boundBy": item.get("bound_by"),
            }
            for camera_id, item in sorted(BINDINGS.items())
        ],
    }
    BINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BINDINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(BINDINGS_PATH)


# ---- FIX-05/06 共享件：安装锁、v2 证据归档、采集会话（§4/§4.1/§5） ----

def channel_device_id(camera_id: str) -> str:
    """通道 → 机械臂设备号（安装锁按 device_id 派生；未配置通道回退通道名）。"""
    return str((CAMERA_CONFIGS.get(camera_id) or {}).get("device_id")
               or camera_id)


# ---- R3-08/R3-10 受控来源证明：本机宿主代际 + helper 摘要 + 时钟包络核验 ----

def register_active_server(server) -> None:
    """帧桥进程在真实监听后登记实例（main()；进程内调试台据此判断同进程事实）。"""
    with FRAME_LOCK:
        _ACTIVE_SERVERS.append(server)


def active_server_ports() -> list[int]:
    """本进程当前真实绑定端口的帧桥实例端口列表。"""
    with FRAME_LOCK:
        return sorted({int(server.server_port) for server in _ACTIVE_SERVERS
                       if getattr(server, "server_port", None) is not None})


def _helper_exec_path() -> Path | None:
    raw = (os.environ.get("CAMERA_HELPER_EXEC") or "").strip()
    return Path(raw) if raw else None


def _host_installation_id_file() -> Path:
    raw = (os.environ.get("CAMERA_HOST_INSTALLATION_ID_FILE") or "").strip()
    if raw:
        return Path(raw)
    return INSTALLATION_DATA_ROOT / "camera_source" / "host_installation_id"


def _clock_verification_tools():
    """加载 tools/verify_camera_clock.py（同机受控时钟核验实现，单一来源）。"""
    import importlib.util
    path = ROOT / "tools" / "verify_camera_clock.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "_verify_camera_clock_frame", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _local_host_clock_id() -> str | None:
    """§6：从本机 OS 读取宿主代际指纹（安装 UUID + kern.boottime）→ clock id。

    两个独立进程从相同 OS 事实分别计算，不是服务器拿客户端 host_id 比一下。
    缺安装 UUID/坏值 → None（严格模式下拒绝登记，不临时重建）。
    """
    tools = _clock_verification_tools()
    if tools is None:
        return None
    try:
        raw = _host_installation_id_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if len(raw) != 36 or raw != raw.lower():
        return None
    try:
        fingerprint = tools.host_fingerprint(raw)
    except Exception:
        return None
    return str(tools.source_clock_id(**fingerprint))


def _session_proof_strict() -> bool:
    """受控证明基础设施是否在位：配置了受控 helper，或已有归档时钟核验。

    未配置=旧部署兼容（不校验证明字段）；配置后新会话必须携带受控证明。
    """
    helper = _helper_exec_path()
    if helper is not None and helper.is_file():
        return True
    return (INSTALLATION_DATA_ROOT / "camera_source"
            / "clock_registration.json").exists()


def _controlled_clock_proof(helper_sha256: str, source_clock_id: str) -> dict:
    """§6：帧桥在本机实际启动受控 helper --clock-proof（20 次包络核验）。

    同（helper 摘要, 宿主代际）进程内只核验一次并归档 clock_registration；
    核验失败 → source_timing_unproven（不从成功子集拼证据）。
    """
    import secrets as _secrets
    key = (helper_sha256, source_clock_id)
    with _PROOF_LOCK:
        cached = _PROOF_CACHE.get(key)
    if cached is not None:
        return cached
    helper = _helper_exec_path()
    if helper is None or not helper.is_file():
        raise _FrameReject(503, "source_timing_unproven",
                           "受控 helper 未配置：帧桥无法在本机核验时钟证明；"
                           "请设置 CAMERA_HELPER_EXEC 指向受控 helper 可执行文件"
                           "（取值见 tools/wx_service_console.py），或改经启停台启动帧桥"
                           "（手工起桥三件套=CAMERA_HELPER_EXEC+CAMERA_CONTROL_TOKEN_FILE"
                           "+CAMERA_HOST_INSTALLATION_ID_FILE；helper 被拒后须重启才会重试）")
    digest = v2_file_sha256(helper)
    if not _secrets.compare_digest(digest, helper_sha256):
        raise _FrameReject(503, "source_timing_unproven",
                           "会话申报 helper 摘要与受控安装二进制不一致："
                           "重装后须重新通过自检")
    tools = _clock_verification_tools()
    if tools is None:
        raise _FrameReject(503, "source_timing_unproven",
                           "时钟核验工具缺失（tools/verify_camera_clock.py）")
    envelope = tools.verify_helper_clock(str(helper))
    registration = {
        "contract_version": "clock-registration.v1",
        "source_clock_id": source_clock_id,
        "helper_path": str(helper),
        "helper_sha256": digest,
        "timing_provenance_required": TIMING_PROVENANCE,
        "envelope_ok": bool(envelope.get("ok")),
        "envelope_runs": envelope.get("runs"),
        "checked_at": _now_iso(),
        "ok": bool(envelope.get("ok")),
        "reason": envelope.get("reason"),
    }
    if not registration["ok"]:
        raise _FrameReject(503, "source_timing_unproven",
                           f"跨进程时钟包络核验未通过：{envelope.get('reason')}")
    archive = INSTALLATION_DATA_ROOT / "camera_source" / "clock_registration.json"
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(json.dumps(registration, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    except OSError:
        pass  # 归档失败不改变核验事实；进程内缓存仍有效
    with _PROOF_LOCK:
        _PROOF_CACHE[key] = registration
    return registration


def v2_file_sha256(path: Path) -> str:
    import scripts.camera_binding_v2 as _v2
    return str(_v2.file_sha256(Path(path)) or "")


def _verify_session_proof(body: dict, source_clock_id: str) -> tuple[str, str]:
    """R3-08/R3-10：会话登记绑定受控本机证明，返回 (format_id, helper_sha256)。

    自报字符串不算可信登记：时钟 id 必须等于帧桥从本机 OS 独立计算的宿主
    代际指纹；helper 摘要必须与受控安装二进制一致；格式必须是实际管线四段式。
    """
    import secrets as _secrets
    actual_clock_id = _local_host_clock_id()
    if actual_clock_id is None or \
            not _secrets.compare_digest(str(source_clock_id or ""), actual_clock_id):
        raise _FrameReject(503, "source_timing_unproven",
                           "sourceClockId 与本机宿主代际指纹不符：不接受自报时钟")
    format_id = str(body.get("formatId") or "").strip()
    if not FORMAT_ID_PATTERN.match(format_id):
        raise _FrameReject(422, "camera_request_invalid",
                           "formatId 必须是 fourcc:<8位小写hex>;"
                           "size:<宽>x<高>;pixel-aspect:<n>/<d>（实际采集格式生成）")
    helper_sha256 = str(body.get("helperSha256") or "").strip().lower()
    if len(helper_sha256) != 64 or \
            any(c not in "0123456789abcdef" for c in helper_sha256):
        raise _FrameReject(422, "camera_request_invalid",
                           "helperSha256 必须是 64 位小写 hex（受控安装二进制摘要）")
    _controlled_clock_proof(helper_sha256, actual_clock_id)
    return format_id, helper_sha256


# ---- R3-11：排他锁内的当前 daemon 安全事实（Fleet 只读服务签名查询） ----

def _fleet_safety_config() -> tuple[str, Path, str] | None:
    """Fleet 安全查询配置；未配置=旧部署（保持既有本机账本保护）。"""
    base = (os.environ.get("CAMERA_FLEET_SAFETY_BASE_URL") or "").strip().rstrip("/")
    keys = (os.environ.get("CAMERA_FLEET_SAFETY_KEYS_FILE")
            or os.environ.get("ARMFLEET_WX_NAV_KEYS_FILE") or "").strip()
    if not base or not keys:
        return None
    key_id = (os.environ.get("CAMERA_FLEET_SAFETY_KEY_ID")
              or "camera-frame-server").strip()
    return base, Path(keys), key_id


def _require_fleet_device_safe(device_id: str) -> None:
    """R3-11：换绑前核对持久在途任务/未解决未知/当前 daemon 安全事实。

    使用受信本机版本化 Fleet 安全查询（GET /v1/devices/{id}/
    wx-navigation-safety，服务签名）；读不全/过期/断网/任一阻断 → 保守拒绝。
    只读 STATUS，绝不代为 CONNECT/STOP/回零。
    """
    config = _fleet_safety_config()
    if config is None:
        # R4-11-B：缺配置不得绕过换绑保护——安全查询未配置时 bind/unbind/
        # renew 一律拒绝（fail-closed），旧"保持既有账本保护"的静默放行取消。
        raise _FrameReject(
            409, "device_safety_unresolved",
            "Fleet 安全查询未配置（CAMERA_FLEET_SAFETY_BASE_URL/"
            "CAMERA_FLEET_SAFETY_KEYS_FILE 或 ARMFLEET_WX_NAV_KEYS_FILE）："
            "拒绝换绑，请完成部署配置后重试")
    base, keys_file, key_id = config
    path = f"/v1/devices/{quote(device_id, safe='')}/safety-view"
    try:
        from runtime import service_signing as wna
        keys = wna.load_keys(str(keys_file))
        entry = keys.get(key_id)
        if entry is None or not entry[1]:
            raise _FrameReject(409, "device_safety_unresolved",
                               f"Fleet 安全查询 key 不可用：{key_id}")
        secret = entry[0]
        timestamp = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        nonce = uuid.uuid4().hex
        signature = hmac.new(
            secret, wna.signature_base("GET", path, timestamp, nonce, b""),
            hashlib.sha256).hexdigest()
        request = urllib.request.Request(base + path, headers={
            "X-WX-Key-Id": key_id, "X-WX-Time": timestamp,
            "X-WX-Nonce": nonce, "X-WX-Signature": signature})
        with urllib.request.urlopen(
                request, timeout=FLEET_SAFETY_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                raise _FrameReject(409, "device_safety_unresolved",
                                   f"Fleet 安全查询返回 HTTP {resp.status}")
            view = json.loads(resp.read())
    except _FrameReject:
        raise
    except Exception as exc:
        raise _FrameReject(409, "device_safety_unresolved",
                           f"Fleet 安全查询不可达/失败（保守拒绝换绑）：{exc}") from exc
    _assert_safety_confirmed(device_id, view)


def _assert_safety_confirmed(device_id: str, view) -> None:
    """安全视图缺项/过期/阻断逐项拒绝（任一未知 → 不换绑）。"""
    def reject(reason: str) -> None:
        raise _FrameReject(409, "device_safety_unresolved",
                           f"设备 {device_id} 安全事实不满足换绑条件：{reason}")

    if not isinstance(view, dict):
        reject("安全视图不是对象")
    if view.get("contract_version") != "device-safety-view.v1":
        reject(f"契约版本未知：{view.get('contract_version')!r}")
    if str(view.get("device_id") or "") != device_id:
        reject("设备身份不匹配")
    try:
        observed_dt = datetime.fromisoformat(
            str(view.get("observed_at")).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - observed_dt).total_seconds()
    except (ValueError, TypeError, AttributeError):
        reject("observed_at 不可解析")
    if age < -5 or age > FLEET_SAFETY_MAX_AGE_SECONDS:
        reject(f"安全快照过期（{age:.1f}s > {FLEET_SAFETY_MAX_AGE_SECONDS:.0f}s）")
    if view.get("mechanical_safe") != "confirmed":
        reject(f"mechanical_safe={view.get('mechanical_safe')!r}")
    if view.get("in_flight_task_ids") != []:
        reject(f"存在持久在途任务 {view.get('in_flight_task_ids')}")
    if view.get("unresolved_task_ids") != []:
        reject(f"存在未解决机械/输入未知 {view.get('unresolved_task_ids')}")
    if view.get("held_session_id") is not None:
        reject(f"存在未解决持有会话 {view.get('held_session_id')}")
    if view.get("lease_active"):
        reject("Fleet 动作租约未释放")
    if view.get("blocking_reasons"):
        reject(f"存在阻断原因 {view.get('blocking_reasons')}")


# ---- 来源帧保留策略（用户拍板 2026-10-10）：只留业务消费过的帧 ----
# 帧桥收到 v2 帧即归档（身份链不变），清扫线程按 TTL 删除无 consumed_at 标记的
# 过期帧；Fleet 侧业务读取（latest/read bytes）时打标记，标记一次永久保留。
# TTL 环境变量 CAMERA_SOURCE_RETENTION_TTL_SECONDS（默认 900 秒）。

def _source_retention_ttl_seconds() -> float:
    raw = (os.environ.get("CAMERA_SOURCE_RETENTION_TTL_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 900.0
    return value if value > 30 else 900.0


def _sweep_source_retention_once() -> tuple[int, int]:
    """单轮清扫：返回 (扫描数, 删除数)。只清无标记且超过 TTL 的帧。"""
    store = _source_evidence_store()
    if store is None or SOURCE_EVIDENCE_DIR is None:
        return 0, 0
    from datetime import datetime, timezone
    ttl = _source_retention_ttl_seconds()
    root = Path(SOURCE_EVIDENCE_DIR) / "camera_source_v2"
    if not root.exists():
        return 0, 0
    now = datetime.now(timezone.utc)
    scanned = deleted = 0
    for device_dir in sorted(root.iterdir()):
        if not device_dir.is_dir():
            continue
        for sidecar in sorted(device_dir.glob("*.json")):
            scanned += 1
            try:
                record = json.loads(sidecar.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not isinstance(record, dict) or record.get("consumed_at"):
                continue
            captured = str(record.get("captured_at") or "")
            try:
                captured_dt = datetime.fromisoformat(
                    captured.replace("Z", "+00:00"))
                if captured_dt.tzinfo is None:
                    continue
            except ValueError:
                continue
            if (now - captured_dt).total_seconds() <= ttl:
                continue
            evidence_id = str(record.get("evidence_id") or "")
            if evidence_id and store.purge_source_frame(evidence_id):
                deleted += 1
    return scanned, deleted


def _start_source_retention_loop() -> None:
    def _loop() -> None:
        while True:
            time.sleep(60)
            try:
                _sweep_source_retention_once()
            except Exception:
                pass  # 清扫失败不影响采集主链路；下一轮重试

    threading.Thread(target=_loop, daemon=True,
                     name="source-frame-retention").start()


def _source_evidence_store():
    """懒构造 Fleet 同源 EvidenceStore（帧桥进程内；与 API 读同一 DB/目录）。"""
    global _SOURCE_EVIDENCE_STORE
    if SOURCE_EVIDENCE_DB is None or SOURCE_EVIDENCE_DIR is None:
        return None
    if _SOURCE_EVIDENCE_STORE is None:
        from runtime.evidence import EvidenceStore
        from runtime.store import Store
        SOURCE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        _SOURCE_EVIDENCE_STORE = EvidenceStore(
            Store(SOURCE_EVIDENCE_DB), SOURCE_EVIDENCE_DIR)
    return _SOURCE_EVIDENCE_STORE


_SERVER_INSTANCE_ID = str(uuid.uuid4())  # R2-18：schema uuid 形（capture_session_view）
TIMING_PROVENANCE = "camera_pts_host_clock.v1"
# §5（B02/帧桥）：原帧年龄≤2 秒、未来容差≤0.5 秒、边≤8192、像素≤2400 万；
# JPEG 字节上限沿用 MAX_FRAME_BYTES（≤8MiB）。
SOURCE_MAX_AGE_SECONDS = 2.0
SOURCE_FUTURE_TOLERANCE_SECONDS = 0.5
SOURCE_MAX_EDGE_PIXELS = 8192
SOURCE_MAX_TOTAL_PIXELS = 24_000_000
SOURCE_MAX_CLOCK_UNCERTAINTY_MS = 20

# ---- R3-08/R3-10：受控来源证明（helper 二进制摘要/宿主代际/时钟包络核验） ----
# §3：format_id 由 helper 从实际采集格式生成（固定四段式）；§6：时钟证明由
# 帧桥在本机实际启动受控 helper --clock-proof 核验，不接受 HTTP 自报。
FORMAT_ID_PATTERN = re.compile(
    r"^fourcc:[0-9a-f]{8};size:(\d+)x(\d+);pixel-aspect:(\d+)/(\d+)$")
# §3 source_geometry 固定字段（几何摘要=全对象规范化 JSON SHA-256）
GEOMETRY_REQUIRED_KEYS = ("contract_version", "input_size", "format_id", "crop",
                          "rotation_degrees", "mirror_x", "mirror_y",
                          "output_size", "resampler", "zoom_milli", "stabilization")
FLEET_SAFETY_TIMEOUT_SECONDS = 3.0   # R3-11：安全查询超时可退出（锁内有时限）
FLEET_SAFETY_MAX_AGE_SECONDS = 15.0  # 安全快照新鲜界限；过期保守拒绝
# 会话证明缓存：(helper_sha256, source_clock_id) -> clock_registration
#（进程内按宿主代际/二进制摘要键控；宿主重启/换 helper 后自动重核验）
_PROOF_CACHE: dict = {}
_PROOF_LOCK = threading.Lock()
# R3-09：本进程真实服务中的帧桥 HTTP 实例注册表（调试台据此判断“同进程
# 帧桥”是否事实存在——V2_ENABLED 配置不是同进程事实）。
_ACTIVE_SERVERS: list = []


class _FrameReject(Exception):
    """帧桥结构化拒绝：携带 HTTP 状态与错误码（不掩盖为 200/500）。"""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _binding_entry_for(camera_id: str) -> dict:
    import scripts.camera_binding_v2 as v2
    try:
        payload = v2.load_bindings_v2(BINDINGS_PATH)
    except v2.CameraBindingError as exc:
        raise _FrameReject(503, exc.code, exc.message) from exc
    entry = next((b for b in payload["bindings"]
                  if b.get("cameraId") == camera_id), None)
    if entry is None:
        raise _FrameReject(409, "source_binding_changed",
                           "通道当前无 v2 绑定：先完成绑定再上传来源帧")
    return entry


def _reload_bindings_view() -> None:
    """R2-09（B 包）：v2 绑定写后同步进程内绑定视图。

    /status 的 device_unique_id/binding_revision 来自该视图；v2 写只落盘时，
    陈旧视图会让调试台状态/事实提供器误报 unbound（v2 写与旧内存视图脱节）。
    """
    global BINDING_REVISION, BINDINGS
    with FRAME_LOCK:
        BINDING_REVISION, BINDINGS = _load_bindings(BINDINGS_PATH)


def register_capture_session(camera_id: str, *, client_instance_id: str,
                             camera_binding_id: str, geometry_id: str,
                             source_clock_id: str,
                             replaces_generation_id: str | None = None,
                             pts_provenance: str | None = None,
                             capture_uncertainty_ms: int | None = None,
                             format_id: str | None = None,
                             helper_sha256: str | None = None) -> dict:
    """§4/§5：注册采集会话（新会话原子撤销旧来源并清帧）。

    R3-08/R3-10：受控证明基础设施在位时（_session_proof_strict），会话登记
    必须绑定真实采集格式/受控 helper 摘要/本机宿主代际时钟指纹/误差界限；
    自报证明字符串不构成登记。
    """
    return _register_capture_session(
        camera_id, client_instance_id=client_instance_id,
        camera_binding_id=camera_binding_id, geometry_id=geometry_id,
        source_clock_id=source_clock_id,
        replaces_generation_id=replaces_generation_id,
        pts_provenance=pts_provenance,
        capture_uncertainty_ms=capture_uncertainty_ms,
        format_id=format_id, helper_sha256=helper_sha256)


def _verify_geometry_digest(geometry, geometry_id: str) -> str:
    """§3/§4：geometry 必须是实际管线完整对象，且摘要=source_geometry_id。"""
    import scripts.camera_binding_v2 as _v2
    if not isinstance(geometry, dict):
        raise _FrameReject(422, "camera_request_invalid",
                           "geometry 必须为实际管线生成的对象")
    geometry_id = str(geometry_id or "").strip()
    if not geometry_id:
        raise _FrameReject(422, "camera_request_invalid",
                           "缺少实际管线生成的 source_geometry_id")
    missing = [key for key in GEOMETRY_REQUIRED_KEYS if key not in geometry]
    if missing:
        # 严格模式下不接受只报 id 的自报几何：必须携带完整来源几何事实
        if _session_proof_strict():
            raise _FrameReject(422, "camera_request_invalid",
                               f"geometry 缺少实际管线字段：{', '.join(missing)}")
        return geometry_id
    # 身份字段不参与几何摘要（§3：摘要是几何事实对象的规范化 JSON SHA-256）
    facts = {key: value for key, value in geometry.items()
             if key not in ("source_geometry_id", "geometryId")}
    if _v2.canonical_digest(facts) != geometry_id:
        raise _FrameReject(422, "camera_request_invalid",
                           "source_geometry_id 与 geometry 规范摘要不符（不接受自报几何）")
    return geometry_id


def _register_capture_session(camera_id: str, *, client_instance_id: str,
                              camera_binding_id: str, geometry_id: str,
                              source_clock_id: str,
                              replaces_generation_id: str | None = None,
                              pts_provenance: str | None = None,
                              capture_uncertainty_ms: int | None = None,
                              format_id: str | None = None,
                              helper_sha256: str | None = None) -> dict:
    import scripts.camera_binding_v2 as v2
    if int(capture_uncertainty_ms if capture_uncertainty_ms is not None else 999) \
            > SOURCE_MAX_CLOCK_UNCERTAINTY_MS or not pts_provenance:
        raise _FrameReject(503, "source_timing_unproven",
                           "缺少 PTS 来源证明或总 uncertainty>20ms：不得登记可动作来源")
    # R4-11-A：缺受控证明基础设施（helper 未配且无归档核验）= 不能产生动作
    # 可信会话；冷启动装好受控 helper 后经正式 --clock-proof 核验即恢复。
    if not _session_proof_strict():
        raise _FrameReject(
            503, "source_timing_unproven",
            "受控证明基础设施缺失（未配置 helper 且无归档时钟核验）："
            "拒绝登记动作可信来源会话；请安装受控 helper 并完成正式核验")
    if _session_proof_strict():
        if not format_id or not helper_sha256:
            raise _FrameReject(503, "source_timing_unproven",
                               "会话登记缺少受控证明（formatId/helperSha256）："
                               "自报证明字符串不构成登记")
        format_id, helper_sha256 = _verify_session_proof(
            {"formatId": format_id, "helperSha256": helper_sha256},
            source_clock_id)
    entry = _binding_entry_for(camera_id)
    if entry.get("cameraBindingId") != camera_binding_id:
        raise _FrameReject(409, "source_binding_changed",
                           "会话绑定实例与当前绑定不一致")
    request_digest = hashlib.sha256(json.dumps(
        {"client": client_instance_id, "binding": camera_binding_id,
         "geometry": geometry_id, "clock": source_clock_id,
         "format": format_id, "helper": helper_sha256,
         "uncertainty": capture_uncertainty_ms,
         "provenance": pts_provenance},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    with FRAME_LOCK:
        channel = _channel(camera_id)
        current = channel.get("v2_session")
        if current is not None \
                and current["client_instance_id"] == client_instance_id \
                and current["request_digest"] == request_digest:
            return dict(current["response"])  # 同请求同内容：原响应
        if current is not None and replaces_generation_id != current["source_generation_id"]:
            raise _FrameReject(409, "source_session_conflict",
                               "replaces_generation_id 与当前来源会话不一致")
        generation = str(uuid.uuid4())  # R2-18：schema uuid 形（source_generation_id）
        retired = (current or {}).get("source_generation_id")
        response = {
            "contract": v2.CONTRACT,
            "server_instance_id": _SERVER_INSTANCE_ID,
            "client_instance_id": client_instance_id,
            "camera_id": camera_id,
            "camera_binding_id": camera_binding_id,
            "source_geometry_id": geometry_id,
            "source_clock_id": source_clock_id,
            "source_generation_id": generation,
            "retired_generation_id": retired,
            "timing_provenance": TIMING_PROVENANCE,
            "capture_uncertainty_ms": int(capture_uncertainty_ms),
        }
        if format_id:
            response["format_id"] = format_id
        if helper_sha256:
            response["helper_sha256"] = helper_sha256
        # 接管原子撤销旧代际：清帧、清回执、旧上传此后因代际不匹配拒绝
        channel["frame"] = b""
        channel["seq"] = 0
        channel["source_seq"] = None
        channel["captured_at"] = None
        channel["received_at"] = None
        channel["received_epoch"] = None
        channel["v2_last_seq"] = -1
        channel["v2_last_captured_ns"] = None
        channel["v2_generation_id"] = generation
        channel["v2_session"] = {
            "client_instance_id": client_instance_id,
            "request_digest": request_digest,
            "source_generation_id": generation,
            "camera_binding_id": camera_binding_id,
            "source_geometry_id": geometry_id,
            "source_clock_id": source_clock_id,
            "timing_provenance": pts_provenance,
            "capture_uncertainty_ms": int(capture_uncertainty_ms),
            "format_id": format_id,
            "helper_sha256": helper_sha256,
            "registered_at": _now_iso(),
            "response": response,
        }
        for key in [k for k in _V2_FRAME_RECEIPTS
                    if k[0] and k[0] != generation]:
            _V2_FRAME_RECEIPTS.pop(key, None)
        _audit_event("frame_source_changed", camera_id=camera_id, detail={
            "from": retired, "to": generation,
            "note": "采集会话注册：旧代际来源已原子撤销"})
    return response


def current_capture_session(camera_id: str) -> dict | None:
    """§4：当前服务启动代际/来源会话只读视图（无会话返回 None）。"""
    with FRAME_LOCK:
        session = _channel(camera_id).get("v2_session")
        if session is None:
            return None
        view = {k: v for k, v in session["response"].items() if k != "request_digest"}
        view["registered_at"] = session["registered_at"]
        return view


def channel_generation(camera_id: str) -> str | None:
    """B05（T31）：当前采集代际只读视图（无 v2 会话时 None）。"""
    with FRAME_LOCK:
        return _channel(camera_id).get("v2_generation_id")


def _channel(camera_id: str) -> dict:
    if camera_id not in CAMERA_CONFIGS:
        raise KeyError(camera_id)
    with FRAME_LOCK:
        return CHANNELS.setdefault(camera_id, _new_channel(camera_id))


def _frame_age_ms(channel: dict) -> int | None:
    if channel.get("received_epoch") is None:
        return None
    return max(0, int((time.time() - float(channel["received_epoch"])) * 1000))


_STATUS_ADVICE = {
    "unbound": "请在 CameraHelper 或兼容页选择物理摄像头并保存绑定",
    "wrong_binding": "上传端摄像头与保存绑定不一致：检查换线/换摄像头后重新绑定",
    "resolution_mismatch": "上传分辨率与设备配置不一致：统一为 1280x720 后重新推流",
    "configured": "已配置，等待 CameraHelper 推流",
    "stale": "画面超过新鲜阈值：检查 CameraHelper 推流或网络",
    "unavailable": "画面长时间未更新：确认摄像头供电/连接后重启通道",
    "streaming": "",
}


def _channel_status(camera_id: str) -> dict:
    channel = _channel(camera_id)
    binding = BINDINGS.get(camera_id)
    cfg = CAMERA_CONFIGS[camera_id]
    with FRAME_LOCK:
        age_ms = _frame_age_ms(channel)
        error = channel.get("last_error")
        if not binding and not (camera_id == LEGACY_CAMERA_ID and channel["frame"]):
            status, display = "unbound", "未绑定"
        elif error and error.get("code") == "camera_binding_mismatch":
            status, display = "wrong_binding", "绑定错误"
        elif error and error.get("code") == "camera_resolution_changed":
            status, display = "resolution_mismatch", "分辨率不匹配"
        elif not channel["frame"]:
            status, display = "configured", "已配置"
        elif age_ms is not None and age_ms <= int(STALE_AFTER_SECONDS * 1000):
            status, display = "streaming", "推流中"
        elif age_ms is not None and age_ms <= int(UNAVAILABLE_AFTER_SECONDS * 1000):
            status, display = "stale", "画面过期"
        else:
            status, display = "unavailable", "不可用"
        _record_status_transition(camera_id, channel, status, display)
        return {
            "camera_id": camera_id,
            "device_id": channel["device_id"],
            "device_unique_id": binding.get("device_unique_id") if binding else None,
            "display_name": binding.get("display_name") if binding else None,
            "bound_by": binding.get("bound_by") if binding else None,
            "binding_revision": BINDING_REVISION,
            "status": status,
            "status_display": display,
            "status_advice": _STATUS_ADVICE.get(status, ""),
            "has_frame": bool(channel["frame"]),
            "frame_seq": channel["seq"],
            "source_frame_seq": channel["source_seq"],
            "source": channel.get("source"),
            "source_display": FRAME_SOURCES.get(channel.get("source") or "", "未知来源"),
            "captured_at": channel["captured_at"],
            "received_at": channel["received_at"],
            "age_ms": age_ms,
            "width": channel["width"],
            "height": channel["height"],
            "expected_width": cfg.get("expected_width"),
            "expected_height": cfg.get("expected_height"),
            "error": error,
        }


def _record_status_transition(camera_id: str, channel: dict, status: str, display: str) -> None:
    """状态变化写入通道事件审计（过期/不可用/恢复/绑定错误等）。"""
    previous = channel.get("last_status")
    channel["last_status"] = status
    if previous is None or previous == status:
        return
    transitions = {
        ("streaming", "stale"): "frame_stale",
        ("stale", "unavailable"): "frame_unavailable",
        ("stale", "streaming"): "frame_recovered",
        ("unavailable", "streaming"): "frame_recovered",
    }
    kind = transitions.get((previous, status))
    if kind:
        _audit_event(kind, camera_id=camera_id, detail={
            "from": previous, "to": status, "status_display": display})


def _channel_listing() -> dict:
    return {
        "service": "camera-frame-server",
        "service_version": "multi-camera-v2",
        "binding_revision": BINDING_REVISION,
        "legacy_camera_id": LEGACY_CAMERA_ID,
        "cameras": [
            _channel_status(camera_id)
            for camera_id in sorted(CAMERA_CONFIGS)
        ],
    }


def _streaming_camera_ids() -> list[str]:
    return [
        camera_id
        for camera_id in sorted(CAMERA_CONFIGS)
        if _channel_status(camera_id)["status"] == "streaming"
    ]


def _save_binding(camera_id: str, payload: dict) -> dict:
    global BINDING_REVISION
    unique_id = str(payload.get("deviceUniqueId") or "").strip()
    if not unique_id:
        raise ValueError("deviceUniqueId 不能为空")
    with FRAME_LOCK:
        current = BINDINGS.get(camera_id)
        # Re-saving the same selection is a safe no-op. Rebinding to another
        # physical camera still requires stopping the channel first.
        if (_channel_status(camera_id)["status"] == "streaming"
                and current
                and current["device_unique_id"] == unique_id):
            return {**_channel_status(camera_id), "binding_unchanged": True}
        if _channel_status(camera_id)["status"] == "streaming":
            raise RuntimeError("camera_still_streaming")
        for other_id, item in BINDINGS.items():
            if other_id != camera_id and item["device_unique_id"] == unique_id:
                _audit_event("binding_conflict", camera_id=camera_id, detail={
                    "device_unique_id": unique_id, "occupied_by": other_id})
                raise RuntimeError("camera_unique_id_conflict")
        BINDINGS[camera_id] = {
            "camera_id": camera_id,
            "device_unique_id": unique_id,
            "display_name": str(payload.get("displayName") or "摄像头"),
            "bound_at": _utc_iso(),
            "bound_by": str(payload.get("operator") or "local-operator"),
        }
        BINDING_REVISION += 1
        CHANNELS[camera_id] = _new_channel(camera_id)
        _persist_bindings()
    _audit_event("binding_saved", camera_id=camera_id, detail={
        "device_unique_id": unique_id,
        "bound_by": BINDINGS[camera_id]["bound_by"],
        "binding_revision": BINDING_REVISION})
    return _channel_status(camera_id)


def _delete_binding(camera_id: str) -> None:
    global BINDING_REVISION
    with FRAME_LOCK:
        if _channel_status(camera_id)["status"] == "streaming":
            raise RuntimeError("camera_still_streaming")
        BINDINGS.pop(camera_id, None)
        BINDING_REVISION += 1
        CHANNELS[camera_id] = _new_channel(camera_id)
        _persist_bindings()
    _audit_event("binding_deleted", camera_id=camera_id,
                 detail={"binding_revision": BINDING_REVISION})


def _decode_jpeg(data: bytes) -> np.ndarray | None:
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


def _normalize_source(raw: str | None, *, legacy_without_identity: bool) -> str:
    value = (raw or "").strip().lower()
    if value in FRAME_SOURCES:
        if not legacy_without_identity and value == "legacy":
            raise RuntimeError("camera_source_invalid")
        return value
    # 无身份的旧 /frame 客户端只能被标记为 legacy；分路正式接口必须
    # 明确声明 camera_helper 或 chrome_compat，禁止把未知来源默认为正式采集。
    if legacy_without_identity:
        return "legacy"
    raise RuntimeError("camera_source_invalid")


_V2_FRAME_RECEIPTS: dict[tuple, dict] = {}


def _validate_strict_session(meta: dict, session: dict,
                             uncertainty_header: str | None) -> _FrameReject | None:
    """已登记采集会话的逐帧严格校验（§5）：代际/几何/时钟/PTS 证明/新鲜度。

    旧代际、旧安装（换绑）、未知 clock、自报 geometry、过期与未来帧均拒绝；
    错误码按附件映射：会话/几何/代际/绑定 409，时钟证明与来源过期 503。
    """
    if meta["source_generation_id"] != session["source_generation_id"]:
        return _FrameReject(409, "source_generation_rejected",
                            "上传代际与当前采集会话不一致：旧代际来源已撤销")
    if meta["source_geometry_id"] != session["source_geometry_id"]:
        return _FrameReject(409, "source_geometry_changed",
                            "来源几何与会话登记不一致（不接受上传方自报几何）")
    if meta["source_clock_id"] != session["source_clock_id"]:
        return _FrameReject(503, "source_timing_unproven",
                            "来源时钟未在当前会话登记（未知 clock）：拒绝")
    provenance = str(meta.get("timing_provenance") or "").strip()
    if provenance != TIMING_PROVENANCE or \
            session.get("timing_provenance") != TIMING_PROVENANCE:
        return _FrameReject(503, "source_timing_unproven",
                            f"缺少 PTS host-clock 来源证明（固定 {TIMING_PROVENANCE}）")
    try:
        uncertainty = int(str(uncertainty_header or "").strip())
    except ValueError:
        return _FrameReject(503, "source_timing_unproven",
                            "缺少/非法的采样不确定度声明")
    if uncertainty < 0 or uncertainty > SOURCE_MAX_CLOCK_UNCERTAINTY_MS:
        return _FrameReject(503, "source_timing_unproven",
                            f"采样不确定度 {uncertainty}ms 超过 "
                            f"{SOURCE_MAX_CLOCK_UNCERTAINTY_MS}ms：不得接纳")
    # R3-10②：逐帧误差声明必须在会话登记的误差界限内（登记≠自报字符串，
    # 登记绑定受控证明；超出登记界限的帧不得按原界限接纳）
    registered = session.get("capture_uncertainty_ms")
    if isinstance(registered, int) and uncertainty > registered:
        return _FrameReject(503, "source_timing_unproven",
                            f"采样不确定度 {uncertainty}ms 超出会话登记界限 "
                            f"{registered}ms：拒绝")
    try:
        captured = datetime.fromisoformat(
            str(meta["captured_at"]).replace("Z", "+00:00"))
        if captured.tzinfo is None:
            raise ValueError("naive")
    except ValueError:
        return _FrameReject(422, "camera_request_invalid",
                            "X-Captured-At 必须是带时区的 UTC 时刻")
    age = (datetime.now(timezone.utc) - captured).total_seconds()
    if age > SOURCE_MAX_AGE_SECONDS:
        return _FrameReject(503, "source_unavailable",
                            f"原帧年龄 {age:.2f}s 超过 "
                            f"{SOURCE_MAX_AGE_SECONDS:.0f}s：过期拒绝")
    if age < -SOURCE_FUTURE_TOLERANCE_SECONDS:
        return _FrameReject(503, "source_unavailable",
                            f"原帧时间来自未来（{-age:.2f}s）：拒绝")
    return None


def _accept_frame_v2(handler, camera_id: str) -> None:
    """§5 帧桥 v2（FIX-06）：绑定实例/来源会话/几何/时钟/序号/时间/解码尺寸
    同锁校验；同代际+序号+完整摘要重传返回原回执与原证据（不增桥序号、
    不刷新时间）；同号异内容 409；低序号 409；解码尺寸必须实测且越界 422。
    配置证据归档后，接纳帧即通过 runtime.evidence.save_source_frame_v2
    归档 camera_source_v2 证据（先文件/fsync 再引用；失败 503 不更新最新帧）。
    """
    headers = handler.headers
    required = {
        "X-Frame-Source": "frame_source",
        "X-Camera-Binding-Id": "camera_binding_id",
        "X-Source-Geometry-Id": "source_geometry_id",
        "X-Source-Generation-Id": "source_generation_id",
        "X-Source-Frame-Seq": "source_capture_seq",
        "X-Source-Clock-Id": "source_clock_id",
        "X-Source-Captured-Monotonic-Ns": "source_captured_monotonic_ns",
        "X-Captured-At": "captured_at",
    }
    meta = {}
    missing = [header for header in required if not headers.get(header)]
    if missing:
        handler._error(422, "camera_request_invalid",
                       f"缺少 v2 必需头：{', '.join(missing)}")
        return
    for header, field in required.items():
        meta[field] = headers.get(header)
    # v2 正式来源须由受控 Helper 显式声明，且仍须通过下方会话/时钟核验；
    # 不将缺失或兼容来源补成 Helper，重传也不得绕过来源检查。
    if str(meta["frame_source"]).strip().lower() != "camera_helper":
        handler._error(422, "camera_request_invalid",
                       "v2 帧来源必须为 camera_helper")
        return
    meta["timing_provenance"] = headers.get("X-Source-Timing-Provenance")
    data = handler._read_body(MAX_FRAME_BYTES)
    if data is None:
        return
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].lower()
    if content_type and content_type != "image/jpeg":
        handler._error(415, "content_type_invalid", "帧必须使用 image/jpeg")
        return
    # 绑定实例核对：严格解析绑定文件（坏档 503 不变空），实例不一致=换绑后旧上传
    try:
        entry = _binding_entry_for(camera_id)
    except _FrameReject as exc:
        handler._error(exc.status, exc.code, exc.message)
        return
    if entry.get("cameraBindingId") != meta["camera_binding_id"]:
        handler._error(409, "source_binding_changed",
                       "上传绑定实例与当前绑定不一致：换绑清帧后旧上传拒绝")
        return
    try:
        seq = int(str(meta["source_capture_seq"]).strip())
        captured_monotonic_ns = int(str(meta["source_captured_monotonic_ns"]).strip())
    except ValueError:
        handler._error(422, "camera_request_invalid",
                       "源序号/单调采样时间必须为十进制整数（纳秒由后端按整数处理）")
        return
    if seq < 0 or captured_monotonic_ns < 0:
        handler._error(422, "camera_request_invalid",
                       "源序号/单调采样时间不能为负")
        return
    with FRAME_LOCK:
        session = _channel(camera_id).get("v2_session")
    if session is None:
        # R3-10①：v2 帧无有效会话立即拒绝——身份字段齐全不等于时钟已证明，
        # 未握手来源不得成为可信证据
        handler._error(503, "source_unavailable",
                       "无有效采集会话：先完成 capture-sessions 握手再上传 v2 帧")
        return
    rejection = _validate_strict_session(
        meta, session, headers.get("X-Source-Capture-Uncertainty-Ms"))
    if rejection is not None:
        handler._error(rejection.status, rejection.code, rejection.message)
        return
    # 实际解码尺寸：不信上传者自报；边≤8192、总像素≤2400 万，越界 422
    frame = _decode_jpeg(data)
    if frame is None:
        handler._error(422, "camera_frame_decode_failed", "请求体不是有效 JPEG")
        return
    height, width = int(frame.shape[0]), int(frame.shape[1])
    if (width > SOURCE_MAX_EDGE_PIXELS or height > SOURCE_MAX_EDGE_PIXELS
            or width * height > SOURCE_MAX_TOTAL_PIXELS):
        handler._error(422, "camera_request_invalid",
                       f"解码尺寸 {width}x{height} 越界（边≤{SOURCE_MAX_EDGE_PIXELS}"
                       f"，总像素≤{SOURCE_MAX_TOTAL_PIXELS}）")
        return
    jpeg_sha = hashlib.sha256(data).hexdigest()
    receipt_key = (meta["source_generation_id"], seq)
    with FRAME_LOCK:
        prior = _V2_FRAME_RECEIPTS.get(receipt_key)
        if prior is not None:
            if prior["jpeg_sha256"] == jpeg_sha:
                handler._json(200, prior)  # 重传：原回执+原证据+首次时间，不续鲜
                return
            handler._error(409, "camera_source_conflict",
                           "同代际同序号不同内容：拒绝")
            return
        channel = _channel(camera_id)
        last_seq = int(channel.get("v2_last_seq") or -1)
        if seq <= last_seq:
            handler._error(409, "camera_source_conflict",
                           f"源序号倒退（当前 {last_seq}）：低序号拒绝")
            return
        # R3-10③：逐帧时钟核对——同代际内 PTS 单调采样时间不得倒退
        #（倒退=时钟基准变化，按冻结规则本代际失效）
        last_captured_ns = channel.get("v2_last_captured_ns")
        if last_captured_ns is not None \
                and captured_monotonic_ns < int(last_captured_ns):
            handler._error(503, "source_timing_unproven",
                           "PTS 单调采样时间较上一接纳帧倒退：时钟基准变化，拒绝")
            return
        received_at = _now_iso()
        device_id = channel_device_id(camera_id)
        # R2-18：桥序号在归档记录构造前分配（接纳快照字段 source_frame_seq）
        bridge_seq = channel["seq"] + 1
        # FIX-06②：合法 v2 帧 → 归档 camera_source_v2 证据（同代际+序号幂等复用；
        # 失败 503 且不更新最新帧：先文件/fsync 再引用，失败无可用证据）
        evidence_id = None
        store = _source_evidence_store()
        if store is not None:
            reused = store.find_source_frame_v2(
                device_id, meta["source_generation_id"], seq)
            if reused is not None:
                evidence_id = reused.get("evidence_id")
            else:
                record = {
                    "contract_version": "camera-binding.v2",
                    "evidence_type": "camera_source_v2",
                    "kind": "camera_source_v2",
                    "device_id": device_id,
                    "camera_id": camera_id,
                    "camera_unique_id": entry.get("deviceUniqueId"),
                    "camera_binding_id": meta["camera_binding_id"],
                    "source_geometry_id": meta["source_geometry_id"],
                    "source_generation_id": meta["source_generation_id"],
                    "source_capture_seq": seq,
                    "source_clock_id": meta["source_clock_id"],
                    "source_captured_monotonic_ns": captured_monotonic_ns,
                    # R2-18：诊断修订与桥序号为接纳时冻结快照，不得事后补写
                    "binding_revision": int(BINDING_REVISION),
                    "source_frame_seq": bridge_seq,
                    "captured_at": meta["captured_at"],
                    "received_at": received_at,
                    "jpeg_sha256": jpeg_sha,
                    "frame_width": width,
                    "frame_height": height,
                    "timing_provenance": (meta.get("timing_provenance")
                                          or None),
                    "capture_uncertainty_ms": (
                        int(headers.get("X-Source-Capture-Uncertainty-Ms"))
                        if str(headers.get("X-Source-Capture-Uncertainty-Ms") or "")
                        .strip().lstrip("-").isdigit() else None),
                    "captured_by": "camera-frame-server",
                }
                try:
                    saved = store.save_source_frame_v2(
                        device_id=device_id,
                        generation=meta["source_generation_id"], seq=seq,
                        jpeg=data, record=record)
                except OSError as exc:
                    handler._error(503, "camera_storage_failed",
                                   f"来源证据归档失败（不更新最新帧）：{exc}")
                    return
                evidence_id = saved.get("evidence_id")
        channel.update({
            "frame": data, "seq": bridge_seq,
            "source": "camera_helper",
            "v2_last_seq": seq,
            "v2_last_captured_ns": captured_monotonic_ns,
            "v2_generation": meta["source_generation_id"],
            "v2_binding_id": meta["camera_binding_id"],
            "v2_geometry_id": meta["source_geometry_id"],
            "v2_clock_id": meta["source_clock_id"],
            "v2_captured_monotonic_ns": captured_monotonic_ns,
            "v2_uncertainty_ms": (
                int(headers.get("X-Source-Capture-Uncertainty-Ms"))
                if str(headers.get("X-Source-Capture-Uncertainty-Ms") or "")
                .strip().lstrip("-").isdigit() else None),
            "v2_timing_provenance": meta.get("timing_provenance"),
            "v2_captured_at": meta["captured_at"],
            "v2_jpeg_sha256": jpeg_sha,
            "v2_evidence_id": evidence_id,
            "source_seq": str(seq), "captured_at": meta["captured_at"],
            "received_at": received_at,
            "received_epoch": time.time(),
            "width": width, "height": height,
        })
        receipt = {
            "accepted": True, "camera_id": camera_id,
            "device_id": device_id,
            "bridge_seq": bridge_seq, "source_capture_seq": seq,
            "camera_binding_id": meta["camera_binding_id"],
            "source_geometry_id": meta["source_geometry_id"],
            "source_generation_id": meta["source_generation_id"],
            "source_clock_id": meta["source_clock_id"],
            "jpeg_sha256": jpeg_sha, "received_at": received_at,
            "frame_width": width, "frame_height": height,
            "evidence_id": evidence_id,
        }
        _V2_FRAME_RECEIPTS[receipt_key] = receipt
        handler._json(200, receipt)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _accept_frame(
    camera_id: str,
    *,
    data: bytes,
    unique_id: str | None,
    captured_at: str | None,
    source_seq: str | None,
    frame_source: str | None,
    legacy_without_identity: bool,
) -> dict:
    binding = BINDINGS.get(camera_id)
    if not legacy_without_identity:
        if not binding:
            raise RuntimeError("camera_unbound")
        if not unique_id or unique_id != binding["device_unique_id"]:
            _channel(camera_id)["last_error"] = {
                "code": "camera_binding_mismatch",
                "message": "上传摄像头物理身份与保存绑定不一致",
            }
            _audit_event("binding_mismatch", camera_id=camera_id, detail={
                "expected_unique_id": (binding or {}).get("device_unique_id"),
                "received_unique_id": unique_id})
            raise RuntimeError("camera_binding_mismatch")
    # R3-08⑤/R3-10①（§5）：活动 v2 来源会话的通道归 v2 来源所有；无 v2 头的
    # 旧上传仅可进入兼容只读通道，不得覆盖活动 v2 会话/冒充 v2 可用。
    with FRAME_LOCK:
        if _channel(camera_id).get("v2_session") is not None:
            _audit_event("camera_source_conflict", camera_id=camera_id, detail={
                "incoming_source": frame_source,
                "advice": "该通道存在活动 v2 采集会话：legacy/兼容上传被拒绝"})
            raise RuntimeError("camera_source_conflict")
    # R3-08⑤：v2 启用后，正式来源（camera_helper）必须携带 v2 会话身份上传；
    # 无 v2 头的 camera_helper 上传一律拒绝（不得退回 legacy 冒充 v2 可用）。
    # chrome_compat 兼容降级桥仍可显式使用（不冒充正式来源）。
    if V2_ENABLED and (frame_source or "").strip().lower() == "camera_helper":
        raise RuntimeError("v2_helper_identity_required")
    source = _normalize_source(frame_source, legacy_without_identity=legacy_without_identity)
    if not legacy_without_identity:
        try:
            captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - captured).total_seconds()
            if captured.tzinfo is None or not -.5 <= age <= 2:
                raise ValueError("stale")
        except (AttributeError, ValueError) as exc:
            raise RuntimeError("frame_capture_time_invalid") from exc
        if not str(source_seq or "").strip().isdigit():
            raise RuntimeError("frame_sequence_invalid")
        channel = _channel(camera_id)
        previous_source = channel.get("source")
        previous_seq = str(channel.get("source_seq") or "").strip()
        # 同一来源的源序号必须递增，防止乱序/重放旧帧覆盖最新画面。
        # 来源切换经过前面的 stale/conflict 门禁后允许从新序列开始。
        # CameraHelper 停止后重新启动可能从 0 或较小序号开始。
        # 只有同一轮仍在推流的新鲜会话要求严格递增；画面已过期时，
        # 将首帧视为新采集会话，不应因旧会话序号残留返回 409。
        frame_age = (
            time.time() - float(channel.get("received_epoch"))
            if channel.get("received_epoch") is not None else None
        )
        if (previous_source == source and previous_seq.isdigit()
                and frame_age is not None
                and frame_age <= STALE_AFTER_SECONDS
                and int(source_seq) <= int(previous_seq)):
            raise RuntimeError("frame_sequence_out_of_order")
    frame = _decode_jpeg(data)
    if frame is None:
        raise ValueError("camera_frame_decode_failed")
    height, width = int(frame.shape[0]), int(frame.shape[1])
    cfg = CAMERA_CONFIGS[camera_id]
    expected_width = cfg.get("expected_width")
    expected_height = cfg.get("expected_height")
    mismatch = bool(
        expected_width and expected_height
        and (width != int(expected_width) or height != int(expected_height))
    )
    now = _utc_iso()
    channel = _channel(camera_id)
    with FRAME_LOCK:
        # 来源冲突保护：正式 CameraHelper 推流期间，兼容来源不得抢占/双写；
        # 任何来源切换都必须先让当前推流停止（超过新鲜阈值）或重新保存绑定。
        if (channel.get("source") and channel["source"] != source
                and channel.get("received_epoch") is not None
                and (time.time() - float(channel["received_epoch"])) <= STALE_AFTER_SECONDS):
            _audit_event("camera_source_conflict", camera_id=camera_id, detail={
                "current_source": channel.get("source"),
                "incoming_source": source,
                "advice": "先停止当前来源推流或重新保存绑定，再切换来源"})
            raise RuntimeError("camera_source_conflict")
        source_changed = bool(channel.get("source") and channel["source"] != source)
        channel["frame"] = data
        channel["seq"] += 1
        channel["source_seq"] = source_seq
        channel["captured_at"] = captured_at or now
        channel["received_at"] = now
        channel["received_epoch"] = time.time()
        channel["width"] = width
        channel["height"] = height
        channel["source"] = source
        channel["last_error"] = (
            {
                "code": "camera_resolution_changed",
                "message": f"实际分辨率 {width}x{height} 与配置不一致",
            }
            if mismatch
            else None
        )
        if mismatch:
            _audit_event("resolution_mismatch", camera_id=camera_id,
                         detail={"width": width, "height": height,
                                 "expected_width": expected_width, "expected_height": expected_height})
        if source_changed:
            _audit_event("frame_source_changed", camera_id=camera_id, detail={
                "from": previous_source, "to": source,
                "to_display": FRAME_SOURCES[source]})
        return {
            "camera_id": camera_id,
            "device_id": channel["device_id"],
            "frame_seq": channel["seq"],
            "received_at": now,
            "width": width,
            "height": height,
            "source": source,
            "source_display": FRAME_SOURCES[source],
            "resolution_mismatch": mismatch,
        }


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Arm Fleet 摄像头兼容取流</title>
<style>
body{font-family:-apple-system,\"PingFang SC\",sans-serif;background:#111;color:#eee;margin:24px}
video{width:min(720px,100%);border-radius:6px;background:#000;display:block;margin-top:14px}
select,button{font-size:15px;padding:6px 10px;margin-right:8px}
</style>
</head>
<body>
<h2>Arm Fleet 摄像头兼容取流（降级桥）</h2>
<p>该页面只用于 CameraHelper 不可用或权限暂时无法授予时的回滚；帧来源将标记为
<b>Chrome 兼容降级桥</b>，不伪装成 CameraHelper 正式来源；CameraHelper 正在推流的通道会被拒绝。</p>
<label>通道 <select id="channel"></select></label>
<label>摄像头 <select id="devices"></select></label>
<button id="start">启动取流</button>
<video id="preview" autoplay playsinline muted></video>
<div id="status">未启动</div>
<script>
async function boot(){
 const grant=await navigator.mediaDevices.getUserMedia({video:true});
 grant.getTracks().forEach(function(t){t.stop()});
 const result=await fetch("/cameras").then(function(r){return r.json()});
 const channel=document.getElementById("channel");
 (result.cameras||[]).forEach(function(c){
   const o=document.createElement("option");o.value=c.camera_id;
   o.textContent=c.camera_id+" · "+c.device_id;channel.appendChild(o)
 });
 const devices=await navigator.mediaDevices.enumerateDevices();
 const select=document.getElementById("devices");
 devices.filter(function(d){return d.kind==="videoinput"}).forEach(function(d){
   const o=document.createElement("option");o.value=d.deviceId;o.textContent=d.label||d.deviceId;
   select.appendChild(o)
 });
 document.getElementById("status").textContent="设备枚举完成"
}
boot().catch(function(e){document.getElementById("status").textContent="枚举失败："+e.message});
document.getElementById("start").onclick=async function(){
 const uniqueId=document.getElementById("devices").value;
 const cameraId=document.getElementById("channel").value;
 const label=document.getElementById("devices").selectedOptions[0].textContent;
 await fetch("/cameras/"+cameraId+"/binding",{method:"PUT",headers:{"Content-Type":"application/json"},
   body:JSON.stringify({deviceUniqueId:uniqueId,displayName:label,operator:"chrome-compat"})});
 const stream=await navigator.mediaDevices.getUserMedia({video:{deviceId:{exact:uniqueId},width:{ideal:1280},height:{ideal:720}}});
 const video=document.getElementById("preview");video.srcObject=stream;
 const canvas=document.createElement("canvas");let busy=false,frames=0;
 async function tick(){
  if(busy||!video.videoWidth)return;busy=true;
  try{
   canvas.width=video.videoWidth;canvas.height=video.videoHeight;
   canvas.getContext("2d").drawImage(video,0,0);
   const blob=await new Promise(function(done){canvas.toBlob(done,"image/jpeg",0.85)});
   const response=await fetch("/frame",{method:"POST",body:blob,headers:{
     "X-Camera-Id":cameraId,"X-Camera-Unique-Id":uniqueId,"X-Captured-At":new Date().toISOString(),
     "X-Source-Frame-Seq":String(frames+1),
     "X-Frame-Source":"chrome_compat"
   }});
   document.getElementById("status").textContent=response.ok?
     cameraId+" 已推送 "+(++frames)+" 帧":"推送失败："+response.status
  }finally{busy=false}
 }
 setInterval(tick,100);document.getElementById("start").disabled=true
}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CameraFrameServer/2.0"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, code: str, message: str, **details) -> None:
        if status >= 500:
            print(f"[reject] {self.command} {self.path} -> {status} {code}: {message}",
                  flush=True)
        self._json(status, {
            "ok": False,
            "error": {"code": code, "message": message, "details": details},
        })

    def _read_body(self, maximum: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "content_length_invalid", "Content-Length 无效")
            return None
        if length <= 0:
            self._error(400, "empty_body", "请求体不能为空")
            return None
        if length > maximum:
            self._error(413, "frame_too_large", f"请求体超过 {maximum} 字节")
            return None
        return self.rfile.read(length)

    @staticmethod
    def _route_camera(path: str, suffix: str) -> str | None:
        prefix = "/cameras/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        return path[len(prefix):-len(suffix)].strip("/") or None

    def _send_frame(self, camera_id: str) -> None:
        try:
            status = _channel_status(camera_id)
        except KeyError:
            self._error(404, "camera_not_found", "摄像头通道不存在", camera_id=camera_id)
            return
        if status["status"] in ("wrong_binding", "resolution_mismatch"):
            error = status.get("error") or {}
            self._error(409, error.get("code", "camera_unavailable"), status["status_display"])
            return
        if status["status"] != "streaming":
            self._error(
                503,
                "camera_frame_stale",
                "摄像头没有可用实时帧",
                camera_id=camera_id,
                camera_status=status["status"],
            )
            return
        channel = _channel(camera_id)
        binding = BINDINGS.get(camera_id) or {}
        with FRAME_LOCK:
            frame = bytes(channel["frame"])
            headers = {
                "X-Camera-Id": camera_id,
                "X-Device-Id": channel["device_id"],
                "X-Camera-Unique-Id": binding.get("device_unique_id", ""),
                "X-Binding-Revision": str(BINDING_REVISION),
                "X-Frame-Seq": str(channel["seq"]),
                "X-Frame-At": str(channel["captured_at"] or ""),
                "X-Captured-At": str(channel["captured_at"] or ""),
                "X-Received-At": str(channel["received_at"] or ""),
                "X-Frame-Width": str(channel["width"] or ""),
                "X-Frame-Height": str(channel["height"] or ""),
                "X-Frame-Source": str(channel.get("source") or ""),
                # R2-08（附件 §5 头表）：冻结帧包 v2 头逐层透传——帧桥只转发
                # 接纳快照字段，不接受消费者推断；兼容帧对应值为空。
                "X-Camera-Binding-Id": str(channel.get("v2_binding_id") or ""),
                "X-Source-Geometry-Id": str(channel.get("v2_geometry_id") or ""),
                "X-Source-Generation-Id": str(channel.get("v2_generation") or ""),
                "X-Source-Frame-Seq": str(channel.get("source_seq") or ""),
                "X-Source-Clock-Id": str(channel.get("v2_clock_id") or ""),
                "X-Source-Captured-Monotonic-Ns":
                    str(channel.get("v2_captured_monotonic_ns") or ""),
                "X-Source-Capture-Uncertainty-Ms":
                    str(channel.get("v2_uncertainty_ms")
                        if channel.get("v2_uncertainty_ms") is not None else ""),
                "X-Source-Timing-Provenance":
                    str(channel.get("v2_timing_provenance") or ""),
            }
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(frame)

    def _binding_v2_routes_get(self, path: str) -> bool:
        """B01（§4）：绑定 v2 只读路由。返回 True 表示已处理。"""
        import scripts.camera_binding_v2 as v2
        if path.startswith("/camera-binding-operations/"):
            request_id = path.rsplit("/", 1)[-1]
            record = v2.load_operation(BINDINGS_PATH, request_id)
            if record is None:
                self._error(404, "camera_operation_not_found", "操作记录不存在")
            elif record.get("state") in ("prepared", "unknown"):
                self._error(503, "camera_commit_unknown",
                            "绑定提交意图未对账：禁止签发新来源/可运动上下文")
            else:
                self._json(200, record)
            return True
        if path.startswith("/cameras/") and path.endswith("/binding-v2"):
            camera_id = path[len("/cameras/"):-len("/binding-v2")]
            try:
                payload = v2.load_bindings_v2(BINDINGS_PATH)
            except v2.CameraBindingError as exc:
                self._error(503, exc.code, exc.message)
                return True
            entry = next((b for b in payload["bindings"]
                          if b.get("cameraId") == camera_id), None)
            if entry is None:
                self._error(404, "camera_not_found", "通道无 v2 绑定")
                return True
            self._json(200, {"contract": v2.CONTRACT, "revision": payload["revision"],
                             "binding": entry})
            return True
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if self._binding_v2_routes_get(path):
            return
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/health":
            self._json(200, {
                "ok": True,
                "service": "camera-frame-server",
                "service_version": "multi-camera-v2",
                "binding_revision": BINDING_REVISION,
            })
            return
        if path == "/cameras":
            self._json(200, _channel_listing())
            return
        if path == "/latest.jpg":
            if not LEGACY_CAMERA_ID:
                self._error(410, "legacy_mode_disabled", "旧单路接口未启用")
                return
            streaming = _streaming_camera_ids()
            if len(streaming) > 1:
                self._error(409, "legacy_route_ambiguous", "存在多路实时画面", cameras=streaming)
                return
            self._send_frame(LEGACY_CAMERA_ID)
            return
        if path == "/status":
            if not LEGACY_CAMERA_ID:
                self._error(410, "legacy_mode_disabled", "旧单路接口未启用")
                return
            self._json(200, _channel_status(LEGACY_CAMERA_ID))
            return
        camera_id = self._route_camera(path, "/capture-sessions/current")
        if camera_id:
            # §4：capture-sessions/current 只读视图（同机后台认证）
            try:
                camera_id = _camera_id(camera_id)
            except ValueError as exc:
                self._error(400, "camera_id_invalid", str(exc))
                return
            import scripts.camera_binding_v2 as v2
            if not V2_ENABLED:
                self._error(503, "source_unavailable", "v2 帧桥未启用")
                return
            try:
                v2.verify_helper_request(self, CONTROL_TOKEN_FILE)
            except v2.CameraBindingError as exc:
                self._error(403, exc.code, exc.message)
                return
            view = current_capture_session(camera_id)
            if view is None:
                self._json(200, {"contract": v2.CONTRACT, "camera_id": camera_id,
                                 "session": None,
                                 "server_instance_id": _SERVER_INSTANCE_ID})
            else:
                self._json(200, {"contract": v2.CONTRACT,
                                 "server_instance_id": _SERVER_INSTANCE_ID,
                                 "session": view})
            return
        camera_id = self._route_camera(path, "/status")
        if camera_id:
            try:
                self._json(200, _channel_status(_camera_id(camera_id)))
            except ValueError as exc:
                self._error(400, "camera_id_invalid", str(exc))
            except KeyError:
                self._error(404, "camera_not_found", "摄像头通道不存在")
            return
        camera_id = self._route_camera(path, "/events")
        if camera_id:
            try:
                events = _events_for(_camera_id(camera_id))
            except ValueError as exc:
                self._error(400, "camera_id_invalid", str(exc))
                return
            except KeyError:
                self._error(404, "camera_not_found", "摄像头通道不存在")
                return
            self._json(200, {"camera_id": camera_id, "events": events})
            return
        if path == "/events":
            self._json(200, {"events": _events_for(None)})
            return
        camera_id = self._route_camera(path, "/latest.jpg")
        if camera_id:
            try:
                self._send_frame(_camera_id(camera_id))
            except ValueError as exc:
                self._error(400, "camera_id_invalid", str(exc))
            return
        self._error(404, "not_found", "接口不存在")

    def do_PUT(self) -> None:
        path_only = urlparse(self.path).path
        if path_only.startswith("/cameras/") and path_only.endswith("/binding-v2"):
            self._binding_v2_write(path_only, action="bind")
            return
        camera_id = self._route_camera(urlparse(self.path).path, "/binding")
        if not camera_id:
            self._error(404, "not_found", "接口不存在")
            return
        try:
            camera_id = _camera_id(camera_id)
            _channel(camera_id)
        except ValueError as exc:
            self._error(400, "camera_id_invalid", str(exc))
            return
        except KeyError:
            self._error(404, "camera_not_found", "摄像头通道不存在")
            return
        data = self._read_body(64 * 1024)
        if data is None:
            return
        if V2_ENABLED:
            # §4（camera-binding 附件）：v2 绑定写接口启用后，旧浏览器绑定写口
            # 关闭（403）——不能成为绕过 helper 身份/安装锁/安装 UUID 的入口；
            # 兼容画面仍可读。
            self._error(403, "camera_permission_denied",
                        "v2 绑定写已启用：旧浏览器绑定写接口已关闭，请使用 binding-v2")
            return
        try:
            import scripts.camera_binding_v2 as v2
            # FIX-05（§4.1）：兼容模式下的绑定写同样取本设备排他安装锁，
            # 不允许无锁写绑定。
            with v2.installation_lock(INSTALLATION_DATA_ROOT,
                                      channel_device_id(camera_id),
                                      exclusive=True):
                payload = json.loads(data)
                result = _save_binding(camera_id, payload)
        except v2.CameraBindingError as exc:
            status = 409 if exc.code == "device_maintenance_busy" else 503
            self._error(status, exc.code, exc.message)
            return
        except json.JSONDecodeError:
            self._error(400, "json_invalid", "请求 JSON 无效")
            return
        except ValueError as exc:
            self._error(400, "binding_invalid", str(exc))
            return
        except RuntimeError as exc:
            code = str(exc)
            message = {
                "camera_still_streaming": "摄像头仍在推流，必须先停止",
                "camera_unique_id_conflict": "物理摄像头已绑定其他通道",
            }.get(code, code)
            self._error(409, code, message)
            return
        self._json(200, {"ok": True, **result})

    def _binding_v2_write(self, path: str, *, action: str) -> None:
        """§4：绑定 v2 写编排（helper 身份 → 意图 → 原子替换 → committed）。"""
        import scripts.camera_binding_v2 as v2
        try:
            v2.verify_helper_request(self, CONTROL_TOKEN_FILE)
        except v2.CameraBindingError as exc:
            self._error(403, exc.code, exc.message)
            return
        data = self._read_body(64 * 1024)
        if data is None:
            return
        try:
            body = json.loads(data)
        except json.JSONDecodeError:
            self._error(422, "camera_request_invalid", "请求 JSON 无效")
            return
        if not isinstance(body, dict):
            self._error(422, "camera_request_invalid", "请求体必须为对象")
            return
        channel = path[len("/cameras/"):].split("/")[0]
        try:
            # FIX-05（§4.1）：bind/unbind/renew 取本设备排他安装锁（非阻塞，
            # 忙=动作进行中 → 立即 409，不排队）；锁内完成意图/原子替换/committed
            # 与换绑清帧，持有到绑定及响应生成结束。
            with v2.installation_lock(INSTALLATION_DATA_ROOT,
                                      channel_device_id(channel), exclusive=True):
                # R3-11：拿到锁不代表没有未知动作——排他锁内先核对持久在途
                # 任务/未解决机械与输入未知/当前 daemon 安全事实（Fleet 只读
                # 服务签名查询；读不全/过期/断网保守拒绝），再提交变更。
                _require_fleet_device_safe(channel_device_id(channel))
                intent = v2.save_binding_v2(BINDINGS_PATH, {
                    "request_id": body.get("requestId"),
                    "action": action,
                    "camera_id": channel,
                    "device_unique_id": body.get("deviceUniqueId"),
                    "display_name": body.get("displayName"),
                    "bound_by": body.get("boundBy"),
                    "principal_id": f"camera-helper:{os.getuid()}",
                })
                # 换绑/renew：清该通道旧帧/来源会话并签发新代际（§4.1 提交保护内；
                # 旧代际上下文/帧链立即失效=换绑清帧断流 T34）
                if action in ("bind", "renew"):
                    new_generation = intent["response"].get("sourceGenerationId")
                    with FRAME_LOCK:
                        channel_state = _channel(channel)
                        channel_state["frame"] = b""
                        channel_state["seq"] = 0
                        channel_state["source_seq"] = None
                        channel_state["captured_at"] = None
                        channel_state["received_at"] = None
                        channel_state["received_epoch"] = None
                        channel_state["v2_generation_id"] = new_generation
                        channel_state["v2_last_seq"] = -1
                        channel_state["v2_last_captured_ns"] = None
                        channel_state["v2_session"] = None  # 会话随换绑失效
                        # R2-18：时钟证明随会话/绑定失效清空，不得跨绑复用
                        channel_state["v2_uncertainty_ms"] = None
                        channel_state["v2_timing_provenance"] = None
                        for key in [k for k in _V2_FRAME_RECEIPTS
                                    if k[0] and k[0] != new_generation]:
                            _V2_FRAME_RECEIPTS.pop(key, None)
                # R2-09：绑定提交完成后同步进程内视图（bind/renew/unbind 一致）
                _reload_bindings_view()
        except v2.CameraBindingError as exc:
            status = {"camera_request_invalid": 422,
                      "camera_unique_id_conflict": 409,
                      "request_conflict": 409,
                      "camera_not_found": 404,
                      "camera_storage_failed": 503,
                      "camera_commit_unknown": 503,
                      "device_maintenance_busy": 409}.get(exc.code, 409)
            self._error(status, exc.code, exc.message)
            return
        except _FrameReject as exc:
            # R3-11：安全事实核对的结构化拒绝（保守拒绝换绑，零动作）
            self._error(exc.status, exc.code, exc.message)
            return
        except Exception as exc:
            self._error(500, "camera_internal_error", str(exc))
            return
        self._json(200, intent["response"])

    def do_DELETE(self) -> None:
        camera_id = self._route_camera(urlparse(self.path).path, "/binding")
        if not camera_id:
            self._error(404, "not_found", "接口不存在")
            return
        if V2_ENABLED:
            # §4：v2 启用后旧浏览器绑定删除口同样关闭（unbind 走 binding-v2）
            self._error(403, "camera_permission_denied",
                        "v2 绑定写已启用：旧浏览器绑定写接口已关闭，请使用 binding-v2/unbind")
            return
        try:
            camera_id = _camera_id(camera_id)
            _channel(camera_id)
            import scripts.camera_binding_v2 as v2
            # FIX-05（§4.1）：兼容模式删除绑定同取排他安装锁
            with v2.installation_lock(INSTALLATION_DATA_ROOT,
                                      channel_device_id(camera_id),
                                      exclusive=True):
                _delete_binding(camera_id)
        except v2.CameraBindingError as exc:
            status = 409 if exc.code == "device_maintenance_busy" else 503
            self._error(status, exc.code, exc.message)
            return
        except ValueError as exc:
            self._error(400, "camera_id_invalid", str(exc))
            return
        except KeyError:
            self._error(404, "camera_not_found", "摄像头通道不存在")
            return
        except RuntimeError as exc:
            self._error(409, str(exc), "摄像头仍在推流，必须先停止")
            return
        self._json(200, {
            "ok": True,
            "camera_id": camera_id,
            "binding_revision": BINDING_REVISION,
        })

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/cameras/") and (
                parsed.path.endswith("/binding-v2/renew")
                or parsed.path.endswith("/binding-v2/unbind")):
            action = "renew" if parsed.path.endswith("/renew") else "unbind"
            self._binding_v2_write(parsed.path, action=action)
            return
        if parsed.path.startswith("/cameras/") \
                and parsed.path.endswith("/capture-sessions"):
            self._capture_session_post(parsed.path)
            return
        legacy = parsed.path == "/frame"
        if legacy:
            raw = (
                self.headers.get("X-Camera-Id")
                or parse_qs(parsed.query).get("camera_id", [None])[0]
                or LEGACY_CAMERA_ID
            )
            if not raw:
                self._error(410, "legacy_mode_disabled", "旧单路接口未启用")
                return
            camera_id = raw
        else:
            camera_id = self._route_camera(parsed.path, "/frame")
            if not camera_id:
                self._error(404, "not_found", "接口不存在")
                return
        try:
            camera_id = _camera_id(camera_id)
            _channel(camera_id)
        except ValueError as exc:
            self._error(400, "camera_id_invalid", str(exc))
            return
        except KeyError:
            self._error(404, "camera_not_found", "摄像头通道不存在")
            return
        header_camera = self.headers.get("X-Camera-Id")
        if header_camera and header_camera != camera_id:
            self._error(409, "camera_binding_mismatch", "路径和请求头 camera_id 不一致")
            return
        if self.headers.get("X-Camera-Binding-Contract") == "camera-binding.v2":
            if not V2_ENABLED:
                self._error(503, "source_unavailable", "v2 帧桥未启用")
                return
            _accept_frame_v2(self, camera_id)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "image/jpeg":
            self._error(415, "content_type_invalid", "帧必须使用 image/jpeg")
            return
        data = self._read_body(MAX_FRAME_BYTES)
        if data is None:
            return
        unique_id = self.headers.get("X-Camera-Unique-Id")
        try:
            result = _accept_frame(
                camera_id,
                data=data,
                unique_id=unique_id,
                captured_at=self.headers.get("X-Captured-At") or self.headers.get("X-Frame-At"),
                source_seq=self.headers.get("X-Source-Frame-Seq"),
                frame_source=self.headers.get("X-Frame-Source"),
                legacy_without_identity=legacy and not unique_id,
            )
        except ValueError:
            self._error(400, "camera_frame_decode_failed", "请求体不是有效 JPEG")
            return
        except RuntimeError as exc:
            code = str(exc)
            message = {
                "camera_unbound": "摄像头通道尚未绑定物理设备",
                "camera_binding_mismatch": "摄像头物理身份与保存绑定不一致",
            "camera_source_conflict": "该通道正由其他帧来源推流，先停止当前来源或重新保存绑定，"
                                          "不允许双写覆盖（正式 CameraHelper 优先）",
                "camera_source_invalid": "帧来源标记无效；分路上传必须声明 camera_helper 或 chrome_compat",
                "v2_helper_identity_required": "v2 帧桥已启用：正式采集必须先完成采集会话握手并携带 v2 身份头上传（不得退回 legacy）",
                "frame_sequence_invalid": "分路上传缺少有效源帧序号",
                "frame_sequence_out_of_order": "分路上传源帧序号未递增，已拒绝旧帧或乱序帧",
            }.get(code, code)
            self._error(409, code, message)
            return
        if result["resolution_mismatch"]:
            self._error(409, "camera_resolution_changed", "上传帧分辨率与设备配置不一致", **result)
            return
        if legacy:
            self._send(200, b"ok", "text/plain; charset=utf-8")
        else:
            self._json(200, {"ok": True, **result})

    def _capture_session_post(self, path: str) -> None:
        """§4：POST /cameras/{id}/capture-sessions——登记采集会话（helper 身份）。

        携带当前 server_instance_id/client_instance_id/旧代际；同请求同内容且
        仍为当前会话返回原响应；实例/代际不匹配 409；缺时钟证明 503。
        """
        import scripts.camera_binding_v2 as v2
        if not V2_ENABLED:
            self._error(503, "source_unavailable", "v2 帧桥未启用")
            return
        try:
            v2.verify_helper_request(self, CONTROL_TOKEN_FILE)
        except v2.CameraBindingError as exc:
            self._error(403, exc.code, exc.message)
            return
        data = self._read_body(64 * 1024)
        if data is None:
            return
        try:
            body = json.loads(data)
        except json.JSONDecodeError:
            self._error(422, "camera_request_invalid", "请求 JSON 无效")
            return
        if not isinstance(body, dict):
            self._error(422, "camera_request_invalid", "请求体必须为对象")
            return
        camera_id = path[len("/cameras/"):-len("/capture-sessions")].strip("/")
        try:
            camera_id = _camera_id(camera_id)
        except ValueError as exc:
            self._error(400, "camera_id_invalid", str(exc))
            return
        if body.get("serverInstanceId") != _SERVER_INSTANCE_ID:
            self._error(409, "source_session_expired",
                        "服务实例代际不匹配：请重新查询 current 会话后握手")
            return
        geometry = body.get("geometry") or {}
        try:
            geometry_id = _verify_geometry_digest(
                geometry, str(geometry.get("source_geometry_id")
                                 or geometry.get("geometryId") or ""))
        except _FrameReject as exc:
            self._error(exc.status, exc.code, exc.message)
            return
        client_instance_id = str(body.get("clientInstanceId") or "").strip()
        if not client_instance_id:
            self._error(422, "camera_request_invalid", "缺少 clientInstanceId")
            return
        uncertainty = body.get("captureUncertaintyMs")
        try:
            uncertainty_value = int(uncertainty) if uncertainty is not None else None
        except (TypeError, ValueError):
            self._error(422, "camera_request_invalid",
                        "captureUncertaintyMs 必须为整数毫秒")
            return
        try:
            response = register_capture_session(
                camera_id,
                client_instance_id=client_instance_id,
                camera_binding_id=str(body.get("cameraBindingId") or "").strip(),
                geometry_id=geometry_id,
                source_clock_id=str(body.get("sourceClockId") or "").strip(),
                replaces_generation_id=(str(body.get("replacesGenerationId")).strip()
                                        if body.get("replacesGenerationId") else None),
                pts_provenance=str(body.get("timingProvenance")
                                   or TIMING_PROVENANCE),
                capture_uncertainty_ms=uncertainty_value,
                format_id=(str(body.get("formatId") or "").strip() or None),
                helper_sha256=(str(body.get("helperSha256") or "").strip().lower()
                               or None),
            )
        except _FrameReject as exc:
            self._error(exc.status, exc.code, exc.message)
            return
        except KeyError:
            self._error(404, "camera_not_found", "摄像头通道不存在")
            return
        self._json(200, response)

    def log_message(self, _format, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Arm Fleet 本机多摄像头帧接收服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--legacy-camera-id", default=os.environ.get("CAMERA_LEGACY_ID") or None)
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS_PATH)
    parser.add_argument("--devices", type=Path, default=DEFAULT_DEVICES_PATH)
    parser.add_argument("--data-root", type=Path, default=None,
                        help="安装锁根（默认由 bindings 路径派生，须与 Fleet/调试台一致）")
    parser.add_argument("--fleet-db", type=Path, default=None,
                        help="Fleet SQLite 路径；配置后 v2 帧桥归档 camera_source_v2 证据")
    parser.add_argument("--evidence-dir", type=Path, default=None,
                        help="证据目录（与 Fleet evidence_dir 同值）")
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        raise SystemExit("摄像头帧服务只允许监听 127.0.0.1")
    configure_state(
        bindings_path=args.bindings,
        devices_path=args.devices,
        legacy_camera_id=args.legacy_camera_id or None,
        data_root=args.data_root,
        fleet_db_path=args.fleet_db,
        evidence_dir=args.evidence_dir,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    register_active_server(server)
    _start_source_retention_loop()
    print(
        f"多摄像头帧服务已启动：http://{args.host}:{args.port} "
        f"（旧单路：{LEGACY_CAMERA_ID or '关闭'}）",
        flush=True,
    )
    server.serve_forever()


configure_state(record_restart=False)


if __name__ == "__main__":
    main()
