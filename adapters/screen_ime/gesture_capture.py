"""手机连续采样会话协调器 gesture-capture.v1（规范第 5、22.2、22.3 节）。

边界：
- 只管理手机侧会话/命令/证据，不发任何机械指令；不导入 daemon/grbl/serial。
- 同设备状态归约放在一把锁内；锁不跨网络/相机等待（等待经 executor）。
- 与旧 CalibrationCoordinator 的设备级互斥：prepare_trial 检查旧布防，
  旧 arm 路由检查本协调器活动采样（读取方在 service/routes 注入）。
- 业务状态持久化在证据目录：轨迹先原子落盘再写 Receipt。
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import jsonschema

from adapters.screen_ime.service import ScreenImeApiError, now_iso


def _iso_after_ms(ms: int) -> str:
    """now + ms 的 ISO 时间戳（契约 Command.expiresAt = issuedAt + 10000ms）。

    与 now_iso 同格式（毫秒精度 + Z），独立取当前时刻并前推 ms。"""
    from datetime import datetime, timedelta, timezone
    target = datetime.now(timezone.utc) + timedelta(milliseconds=ms)
    return target.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
from drivers.gesture_program import canonical_hash, canonical_json

CONTRACT_VERSION = "gesture-capture.v1"

# 进程级唯一协调器：第一个创建的实例自动成为全局单例，
# 后续 ScreenImeService（无论 get_default_service 返回哪个）复用同一实例。
# 解决：HTTP 路由和插件看到不同协调器导致手机 Ready 无法到达 runner 的问题。
_GLOBAL_COORDINATOR: "GestureCaptureCoordinator | None" = None


def get_global_coordinator() -> "GestureCaptureCoordinator | None":
    return _GLOBAL_COORDINATOR


def _set_global_coordinator(coordinator: "GestureCaptureCoordinator") -> None:
    global _GLOBAL_COORDINATOR
    _GLOBAL_COORDINATOR = coordinator
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "api" / "contracts" \
    / "gesture-capture.v1.schema.json"

CLIENT_TTL_S = 35.0
HEARTBEAT_FRESHNESS_S = 6.0  # 2.0→6.0：WiFi 抖动下 500ms hello 偶发超窗误判 not_foreground（2026-09-30 真机）
COMMAND_TTL_S = 35.0
MAX_PAGE_EVENTS = 64

TRIAL_ACTIVE_STATES = ("prepared", "ready", "executing", "collecting")
TRIAL_TERMINAL_STATES = ("passed", "failed", "unknown", "cancelled")

_SCHEMA_CACHE: dict | None = None


def _schema() -> dict:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE


def _validate_fragment(instance: Any, fragment: str | None) -> None:
    schema = _schema()
    if fragment:
        schema = {"$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    try:
        jsonschema.Draft202012Validator(schema).validate(instance)
    except jsonschema.ValidationError as exc:
        raise ScreenImeApiError(400, "invalid_payload", exc.message) from exc


def _uuid() -> str:
    return str(uuid.uuid4())


def _require(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScreenImeApiError(400, "invalid_request", f"{field} is required")
    return value


class _Trial:
    def __init__(self, session: "_Session", trial_id: str, command_spec: dict):
        self.trial_id = trial_id
        self.command = {
            "sessionId": session.session_id,
            "trialId": trial_id,
            "commandId": _uuid(),
            "commandSeq": session.next_command_seq(),
            "clientInstanceId": session.client_instance_id,
            "calibrationFingerprint": _require(
                command_spec.get("calibrationFingerprint"), "calibrationFingerprint"),
            "coordinateFrame": command_spec.get("coordinateFrame"),
            "nonce": secrets.token_hex(32),
            "kind": command_spec.get("kind"),
            "payload": command_spec.get("payload") or {},
            "issuedAt": now_iso(),
            # 契约 gesture-capture.v1：expiresAt = issuedAt + 10000ms（对外
            # 字段必须自洽；服务端寿命判据用 self.issued_monotonic 单调钟）。
            "expiresAt": _iso_after_ms(10000),
        }
        self.issued_monotonic = session.coordinator._clock()
        self.state = "prepared"
        self.ready: dict | None = None
        self.ready_hash: str | None = None
        self.ready_event = threading.Event()
        self.evidence_event = threading.Event()
        self.trace: dict | None = None
        self.receipts: dict[str, dict] = {}
        self.page_events: list[dict] = []

    def expired(self, clock: Callable[[], float]) -> bool:
        return clock() - self.issued_monotonic >= COMMAND_TTL_S

    def view(self) -> dict:
        return {
            "trialId": self.trial_id,
            "commandId": self.command["commandId"],
            "kind": self.command["kind"],
            "state": self.state,
            "ready": bool(self.ready),
            "traceId": self.trace.get("traceId") if self.trace else None,
            "page_events": [dict(item) for item in self.page_events],
        }


class _Session:
    def __init__(self, coordinator: "GestureCaptureCoordinator", session_id: str,
                 device_id: str, ime_device_id: str, binding: dict,
                 actions: list, run_id: str, client_instance_id: str):
        self.coordinator = coordinator
        self.session_id = session_id
        self.device_id = device_id
        self.ime_device_id = ime_device_id
        self.binding = binding
        self.actions = list(actions)
        self.run_id = run_id
        self.client_instance_id = client_instance_id
        self.status = "active"
        self.trial: _Trial | None = None
        self._command_seq = 0

    def next_command_seq(self) -> int:
        self._command_seq += 1
        return self._command_seq

    def view(self) -> dict:
        return {"sessionId": self.session_id, "deviceId": self.device_id,
                "imeDeviceId": self.ime_device_id, "runId": self.run_id,
                "status": self.status,
                "trial": self.trial.view() if self.trial else None}


class GestureCaptureCoordinator:
    """手机手势采样会话（内存态 + 证据落盘；不发机械指令）。"""

    def __init__(self, *, data_root: Path | None = None,
                 legacy_armed_provider: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] | None = None):
        self._data_root = Path(data_root) if data_root else None
        # 布防↔采样跨子系统互斥（B5）：与旧校准 arming 路由共享的锁
        self.cross_mutex = threading.RLock()
        self._legacy_armed = legacy_armed_provider or (lambda _device: False)
        self._lock = threading.RLock()
        self._clock = clock or time.monotonic
        self._sessions: dict[str, _Session] = {}
        self._clients: dict[str, dict] = {}
        global _GLOBAL_COORDINATOR
        if _GLOBAL_COORDINATOR is None:
            _GLOBAL_COORDINATOR = self
        # 校准来源按设备和会话保留，不随当前手机页面或客户端 TTL 消失。
        self._legacy_metadata: dict[tuple[str, str], dict] = {}
        self._metadata_root = (
            self._data_root / "gesture_legacy_metadata" if self._data_root else None)

    def close(self) -> None:
        """B8（2026-10-01 backlog）：清空指向自己的全局引用——同进程重建
        app（测试/热重建）时，插件不得继续持已关闭的协调器。"""
        global _GLOBAL_COORDINATOR
        with self._lock:
            self._sessions.clear()
            self._clients.clear()
        if _GLOBAL_COORDINATOR is self:
            _GLOBAL_COORDINATOR = None

    def configure_metadata_storage(self, data_root: Path) -> None:
        """HTTP 挂载时使用输入法服务自己的数据目录，不回退到现场默认目录。"""
        with self._lock:
            if self._metadata_root is None:
                self._metadata_root = Path(data_root) / "gesture_legacy_metadata"

    def _metadata_path(self, ime_device_id: str, session_id: str) -> Path | None:
        if self._metadata_root is None:
            return None
        return self._metadata_root / f"{canonical_hash([ime_device_id, session_id])}.json"

    # ---------- 内部 ----------

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.get(_require(session_id, "sessionId"))
        if session is None:
            raise ScreenImeApiError(404, "session_not_found",
                                    "Gesture session not found")
        return session

    def _active_trial(self, session: _Session, trial_id: str) -> _Trial:
        trial = session.trial
        if trial is None or trial.trial_id != trial_id:
            raise ScreenImeApiError(409, "trial_not_ready",
                                    "本次试验尚未准备好")
        return trial

    def _client(self, ime_device_id: str) -> dict | None:
        client = self._clients.get(ime_device_id)
        if not client:
            return None
        if self._clock() - client["received_monotonic"] > CLIENT_TTL_S:
            self._clients.pop(ime_device_id, None)
            return None
        return client

    def _evidence_dir(self, session: _Session, trial_id: str) -> Path:
        # 绝对路径兜底：相对 Path("data") 依赖进程 CWD，与其他模块的仓库锚点不一致
        base = self._data_root or Path(__file__).resolve().parents[1] / "data"
        return base / "devices" / session.device_id / "gesture_validation_evidence" \
            / session.run_id / trial_id

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.",
                                            suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    # ---------- 手机侧接口（HTTP 路由调用） ----------

    async def register_client(self, ime_device_id: str, hello: Any) -> dict:
        _require(ime_device_id, "deviceId")
        _validate_fragment(hello, "ClientHello")
        page = hello["page"]
        legacy_session = page.get("legacyCalibrationSessionId")
        if hello["deviceId"] != ime_device_id:
            raise ScreenImeApiError(400, "invalid_payload", "设备编号与请求路径不一致")
        if bool(legacy_session) != (page["pageId"] == "legacy-calibration"):
            raise ScreenImeApiError(400, "invalid_payload", "校准元数据必须来自对应的原校准页面")
        with self._lock:
            existing = self._clients.get(ime_device_id)
            if (existing and hello["clientInstanceId"] == existing["clientInstanceId"]
                    and hello["heartbeatSeq"] <= existing["heartbeatSeq"]):
                # 重复/倒序心跳不刷新新鲜度（第 22.3 节）
                return {"registered": True,
                        "clientInstanceId": existing["clientInstanceId"]}
            if legacy_session:
                metadata = {
                    "deviceId": ime_device_id,
                    "sessionId": legacy_session,
                    "page": deepcopy(page),
                    "clientInstanceId": hello["clientInstanceId"],
                }
                key = (ime_device_id, legacy_session)
                previous = self.legacy_metadata(ime_device_id, legacy_session)
                if previous != metadata:
                    path = self._metadata_path(*key)
                    if path is not None:
                        self._atomic_write(path, metadata)
                    self._legacy_metadata[key] = metadata
            self._clients[ime_device_id] = {
                "clientInstanceId": hello["clientInstanceId"],
                "heartbeatSeq": hello["heartbeatSeq"],
                "page": deepcopy(page),
                "capabilities": list(hello["capabilities"]),
                "received_monotonic": self._clock(),
            }
            return {"registered": True,
                    "clientInstanceId": hello["clientInstanceId"]}

    def legacy_metadata(self, ime_device_id: str, session_id: str) -> dict | None:
        """只返回指定设备/原校准会话的证据，不从当前页面补造。"""
        with self._lock:
            key = (_require(ime_device_id, "deviceId"), _require(session_id, "sessionId"))
            found = self._legacy_metadata.get(key)
            if found is None:
                path = self._metadata_path(*key)
                if path is None:
                    return None
                try:
                    found = json.loads(path.read_text(encoding="utf-8"))
                    _validate_fragment(found["page"], "PageMetadata")
                    _require(found.get("clientInstanceId"), "clientInstanceId")
                except (OSError, ValueError, KeyError, TypeError, ScreenImeApiError):
                    return None
            if (not isinstance(found, dict)
                    or found.get("deviceId") != ime_device_id
                    or found.get("sessionId") != session_id
                    or found.get("page", {}).get("pageId") != "legacy-calibration"
                    or found["page"].get("legacyCalibrationSessionId") != session_id):
                return None
            self._legacy_metadata[key] = found
            return deepcopy(found)

    async def next_command(self, ime_device_id: str,
                           client_instance_id: str) -> dict | None:
        _require(ime_device_id, "deviceId")
        _require(client_instance_id, "clientInstanceId")
        with self._lock:
            client = self._client(ime_device_id)
            if client is None or client["clientInstanceId"] != client_instance_id:
                return None
            for session in self._sessions.values():
                if session.ime_device_id != ime_device_id:
                    continue
                if session.status != "active":
                    continue
                if session.client_instance_id != client_instance_id:
                    continue
                if session.trial is None:
                    continue
                trial = session.trial
                if trial.state not in TRIAL_ACTIVE_STATES:
                    continue
                if trial.expired(self._clock):
                    if trial.state == "prepared":
                        trial.state = "cancelled"
                    continue
                if trial.ready is not None:
                    continue
                return dict(trial.command)
            return None

    async def accept_ready(self, session_id: str, ready: Any) -> dict:
        _validate_fragment(ready, "Ready")
        body_hash = None
        with self._lock:
            session = self._session(session_id)
            if session.status != "active":
                raise ScreenImeApiError(409, "session_finished",
                                        "会话已结束")
            trial = self._active_trial(session, ready["trialId"])
            command = trial.command
            if (ready["commandId"] != command["commandId"]
                    or ready["commandSeq"] != command["commandSeq"]
                    or ready["clientInstanceId"] != command["clientInstanceId"]
                    or ready["nonce"] != command["nonce"]):
                raise ScreenImeApiError(409, "trial_not_ready",
                                        "Ready 与当前试验不匹配")
            # B3（2026-10-01 backlog）：契约必填的 coordinateFrameHash 此前
            # 从未比对——陈旧/旋转后的页面可通过握手，用错误 fixtureRegion
            # 引导评估。命令侧坐标桥带哈希时必须一致。
            expected_hash = ((command.get("coordinateFrame") or {})
                             .get("coordinate_frame_hash"))
            if expected_hash and ready.get("coordinateFrameHash") != expected_hash:
                raise ScreenImeApiError(409, "coordinate_frame_changed",
                                        "Ready 的坐标桥哈希与命令不一致，撤销未执行资格")
            if trial.ready is not None:
                body_hash = canonical_json(ready)
                if body_hash == trial.ready_hash:
                    return {"accepted": True, "duplicate": True}
                raise ScreenImeApiError(
                    409, "ready_payload_conflict",
                    "同一命令的 Ready 内容冲突，撤销未执行资格")
            client = self._client(session.ime_device_id)
            # B4（2026-10-01 backlog）：注册缺失/TTL 过期/心跳过期/非前台
            # 拆分 reason_code——同码折叠曾把 500ms hello 超窗误判成
            # not_foreground，排障盲区。runner 只认 client_not_foreground
            # 做重试，故"非前台"保留原码，其余三因换独立码（不触发重试）。
            if client is None or client["clientInstanceId"] != command["clientInstanceId"]:
                raise ScreenImeApiError(
                    409, "client_unknown",
                    "测试页客户端未注册或已换实例（ClientHello 未达/被替换），动作不得开始")
            if self._clock() - client["received_monotonic"] > HEARTBEAT_FRESHNESS_S:
                raise ScreenImeApiError(
                    409, "client_stale",
                    f"测试页心跳超过新鲜度窗（>{HEARTBEAT_FRESHNESS_S:.0f}s），动作不得开始")
            if not client["page"].get("resumed") or not client["page"].get("hasFocus"):
                raise ScreenImeApiError(
                    409, "client_not_foreground",
                    "测试页不在前台，动作不得开始")
            if trial.expired(self._clock):
                trial.state = "cancelled"
                raise ScreenImeApiError(409, "trial_not_ready",
                                        "命令已过期，试验取消")
            trial.ready = ready
            trial.ready_hash = canonical_json(ready)
            trial.state = "ready"
            trial.ready_event.set()
            return {"accepted": True, "duplicate": False}

    async def accept_trace(self, session_id: str, trace: Any) -> tuple[dict, bool]:
        _validate_fragment(trace, None)  # 根 schema 即 Trace
        with self._lock:
            session = self._session(session_id)
            trial = self._active_trial(session, trace["trialId"])
            if (trace["sessionId"] != session_id
                    or trace["clientInstanceId"] != trial.command["clientInstanceId"]
                    or trace["nonce"] != trial.command["nonce"]):
                raise ScreenImeApiError(409, "trial_not_ready",
                                        "轨迹归属与当前试验不匹配")
            if trial.state not in ("ready", "executing", "collecting", "unknown"):
                raise ScreenImeApiError(409, "trial_not_ready",
                                        "本次试验尚未 Ready")
            trace_id = trace["traceId"]
            payload_hash = __import__("hashlib").sha256(
                canonical_json(trace).encode("utf-8")).hexdigest()
            existing = trial.receipts.get(trace_id)
            if existing is not None:
                if existing["payloadHash"] != payload_hash:
                    trial.state = "unknown"
                    raise ScreenImeApiError(
                        409, "trace_payload_conflict",
                        "同编号轨迹内容冲突，试验转 unknown")
                return dict(existing), True
            # 先原子落盘证据，再写 Receipt（第 22.2 节）
            evidence_dir = self._evidence_dir(session, trial.trial_id)
            self._atomic_write(evidence_dir / f"{trace_id}.json", trace)
            accepted = trial.state != "unknown"
            receipt = {
                "traceId": trace_id, "trialId": trial.trial_id,
                "payloadHash": payload_hash, "received": True,
                "acceptedForEvaluation": accepted,
                "reasonCode": None if accepted else "trial_unknown",
            }
            trial.receipts[trace_id] = receipt
            trial.trace = trace
            if accepted and trial.state in ("ready", "executing"):
                trial.state = "collecting"
            trial.evidence_event.set()
            return dict(receipt), False

    async def accept_page_event(self, session_id: str, event: Any) -> dict:
        _validate_fragment(event, "PageEvent")
        with self._lock:
            session = self._session(session_id)
            trial = self._active_trial(session, event["trialId"])
            if (event["clientInstanceId"] != trial.command["clientInstanceId"]
                    or event["nonce"] != trial.command["nonce"]):
                raise ScreenImeApiError(409, "trial_not_ready",
                                        "页面事件与当前试验不匹配")
            trial.page_events.append(dict(event))
            del trial.page_events[:-MAX_PAGE_EVENTS]
            trial.evidence_event.set()
            return {"accepted": True, "count": len(trial.page_events)}

    async def receipt(self, session_id: str, trace_id: str) -> dict | None:
        with self._lock:
            session = self._session(session_id)
            if session.trial and session.trial.trial_id:
                found = session.trial.receipts.get(trace_id)
                return dict(found) if found else None
            return None

    # ---------- 内部协调接口（自检任务调用，第 22.2 节） ----------

    async def create_session(self, device_id: str, ime_device_id: str,
                             binding: dict, actions: list, run_id: str,
                             *, session_id: str | None = None) -> dict:
        _require(device_id, "device_id")
        _require(ime_device_id, "ime_device_id")
        _require(run_id, "run_id")
        with self._lock:
            client = self._client(ime_device_id)
            if client is None:
                raise ScreenImeApiError(
                    409, "client_not_foreground",
                    "手机还未打开「手势测试页」（或已退到后台超过 30 秒）；"
                    "请在手机上打开手势测试页并保持前台后重试")
            for session in self._sessions.values():
                if (session.ime_device_id == ime_device_id
                        and session.status == "active"):
                    raise ScreenImeApiError(
                        409, "session_active", "该设备已有活动手势会话")
            session = _Session(self, session_id or _uuid(), device_id,
                               ime_device_id, binding, actions, run_id,
                               client["clientInstanceId"])
            self._sessions[session.session_id] = session
            return session.view()

    async def prepare_trial(self, session_id: str, trial_id: str,
                            command_spec: dict) -> dict:
        _require(session_id, "sessionId")
        _require(trial_id, "trialId")
        if not isinstance(command_spec, dict):
            raise ScreenImeApiError(400, "invalid_request",
                                    "command spec must be an object")
        # B5（2026-10-01 backlog）：布防↔采样互斥共用 cross_mutex——两侧
        # 各自持锁的 check-then-act 存在竞窗（并发下旧点布防与手势采样
        # 并存），检查与动作必须处于同一临界区。
        with self.cross_mutex, self._lock:
            # 不变量：cross_mutex 临界区内只做同步字典操作（不得 await 让出），
            # 与 routes.arm_calibration 共享此锁（见该处注释）。
            session = self._session(session_id)
            if session.status != "active":
                raise ScreenImeApiError(409, "session_finished", "会话已结束")
            if session.trial is not None and session.trial.state in TRIAL_ACTIVE_STATES:
                raise ScreenImeApiError(
                    409, "trial_active", "同会话一次只允许一个活动试验")
            if self._legacy_armed(session.ime_device_id):
                raise ScreenImeApiError(
                    409, "calibration_active",
                    "旧校准布防存在，手势布防被拒绝")
            session.trial = _Trial(session, trial_id, command_spec)
            return session.trial.view()

    async def wait_ready(self, session_id: str, trial_id: str,
                         timeout_ms: int) -> dict:
        with self._lock:
            session = self._session(session_id)
            trial = self._active_trial(session, trial_id)
            if trial.ready is not None:
                return {"ready": True, "readyPayload": trial.ready}
        got = await asyncio.get_event_loop().run_in_executor(
            None, trial.ready_event.wait, timeout_ms / 1000.0)
        if not got:
            return {"ready": False, "readyPayload": None}
        return {"ready": True, "readyPayload": trial.ready}

    async def wait_evidence(self, session_id: str, trial_id: str,
                            timeout_ms: int) -> dict:
        with self._lock:
            session = self._session(session_id)
            trial = self._active_trial(session, trial_id)
            if trial.trace is not None:
                return {"ready": True, "trace": trial.trace,
                        "pageEvents": list(trial.page_events)}
        got = await asyncio.get_event_loop().run_in_executor(
            None, trial.evidence_event.wait, timeout_ms / 1000.0)
        with self._lock:
            if trial.trace is not None or trial.page_events:
                return {"ready": True, "trace": trial.trace,
                        "pageEvents": list(trial.page_events)}
        return {"ready": False, "trace": None, "pageEvents": []}

    async def mark_executing(self, session_id: str, trial_id: str) -> None:
        with self._lock:
            session = self._session(session_id)
            trial = self._active_trial(session, trial_id)
            if trial.state != "ready":
                raise ScreenImeTrialNotReady()
            trial.state = "executing"

    async def finish_trial(self, session_id: str, trial_id: str,
                           state: str) -> dict:
        if state not in TRIAL_TERMINAL_STATES:
            raise ScreenImeApiError(400, "invalid_request", "非法试验终态")
        with self._lock:
            session = self._session(session_id)
            trial = self._active_trial(session, trial_id)
            trial.state = state
            trial.ready_event.set()
            trial.evidence_event.set()
            return trial.view()

    async def finish_session(self, session_id: str, outcome: str) -> dict:
        with self._lock:
            session = self._session(session_id)
            session.status = "finished"
            trial = session.trial
            if trial is not None and trial.state in TRIAL_ACTIVE_STATES:
                if trial.state in ("executing", "collecting"):
                    trial.state = "unknown"
                else:
                    trial.state = "cancelled"
                trial.ready_event.set()
                trial.evidence_event.set()
            view = session.view()
            view["outcome"] = outcome
            self._prune_finished_sessions()
            return view

    def _prune_finished_sessions(self, keep: int = 16) -> None:
        # 会话表只增不删会无界累积（每条含 page_events）；保留最近 keep 条
        # finished 供事后查询，更早的删除（调用方持有锁）
        finished = [sid for sid, item in self._sessions.items()
                    if item.status == "finished"]
        for sid in finished[:-keep] if keep else []:
            del self._sessions[sid]

    # ---------- 互斥与观测 ----------

    def has_active_sampling(self, ime_device_id: str) -> bool:
        with self._lock:
            return any(
                session.ime_device_id == ime_device_id
                and session.status == "active"
                and session.trial is not None
                and session.trial.state in TRIAL_ACTIVE_STATES
                for session in self._sessions.values())

    def trial_view(self, session_id: str, trial_id: str) -> dict:
        with self._lock:
            session = self._session(session_id)
            trial = self._active_trial(session, trial_id)
            return trial.view()


class ScreenImeTrialNotReady(ScreenImeApiError):
    def __init__(self):
        super().__init__(409, "trial_not_ready", "试验未处于 Ready 状态")
