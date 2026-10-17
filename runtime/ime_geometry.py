"""L07/§11.13：IME 键盘几何协议 ime-geometry.v1。

会话握手（client_instance 撤销替换）、快照上报（seq 幂等/冲突、retired view、
nonce 规则）、nonce 请求创建与只读长轮询领取。全部零机械动作、零文本副作用。
服务重启全部会话失效（内存态即为规格要求：重启后 geometry_valid=false 须重新握手）。
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone

CONTRACT_VERSION = "ime-geometry.v1"
IME_GEOMETRY_VERSION = 1
NONCE_TTL_S = 2.0
MAX_WAIT_S = 20
REASONS = ("layout_ready", "layout_changed", "task_changed",
           "hidden", "destroyed", "snapshot")
TARGET_IDS = ("ime_candidate_bar", "ime_delete_key")


class ImeGeometryError(Exception):
    """409 ime_geometry_session_stale/sequence_conflict/nonce_expired、422 invalid。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _now() -> float:
    return time.monotonic()


def _iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(),
                                tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def _check_bounds(bounds, display: dict, field: str) -> None:
    if not isinstance(bounds, dict):
        raise ImeGeometryError(422, "ime_geometry_invalid", f"{field} 必须是对象")
    for key in ("left", "top", "right", "bottom"):
        v = bounds.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) \
                or not math.isfinite(v):
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   f"{field}.{key} 必须是有限数")
    w, h = display["width_px"], display["height_px"]
    if not (0 <= bounds["left"] < bounds["right"] <= w):
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               f"{field} 左右边界须满足 0<=left<right<={w}")
    if not (0 <= bounds["top"] < bounds["bottom"] <= h):
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               f"{field} 上下边界须满足 0<=top<bottom<={h}")


def validate_snapshot(body: dict) -> None:
    """§11.13.1 快照完整字段与闭合校验，拒绝额外字段按宽松忽略处理（未知键不动语义）。"""
    if not isinstance(body, dict):
        raise ImeGeometryError(422, "ime_geometry_invalid", "快照必须是对象")
    if body.get("contract_version") != CONTRACT_VERSION:
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               f"contract_version 必须为 {CONTRACT_VERSION}")
    if not _is_uuid(body.get("runtime_session_id")):
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "runtime_session_id 必须是 UUID")
    if not _is_uuid(body.get("view_instance_id")):
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "view_instance_id 必须是 UUID")
    seq = body.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ImeGeometryError(422, "ime_geometry_invalid", "seq 必须是 >=1 整数")
    uptime = body.get("captured_uptime_ms")
    if not isinstance(uptime, (int, float)) or isinstance(uptime, bool) or uptime < 0:
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "captured_uptime_ms 必须 >=0")
    display = body.get("display")
    if not isinstance(display, dict):
        raise ImeGeometryError(422, "ime_geometry_invalid", "display 必须是对象")
    if not isinstance(display.get("display_id"), int) \
            or isinstance(display.get("display_id"), bool) \
            or display["display_id"] < 0:
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "display.display_id 必须 >=0 整数")
    for key in ("width_px", "height_px"):
        v = display.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   f"display.{key} 必须 >0 整数")
    if display.get("rotation") not in (0, 90, 180, 270):
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "display.rotation 必须 0|90|180|270")
    density = display.get("density")
    if not isinstance(density, (int, float)) or isinstance(density, bool) \
            or not math.isfinite(density) or density <= 0:
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "display.density 必须有限且 >0")
    if not isinstance(body.get("layout_version"), str) \
            or not body["layout_version"]:
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "layout_version 必须非空字符串")
    for key in ("keyboard_active", "geometry_valid"):
        if not isinstance(body.get(key), bool):
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   f"{key} 必须是布尔")
    kb = body.get("keyboard_bounds")
    if kb is not None:
        _check_bounds(kb, display, "keyboard_bounds")
    task_id = body.get("current_task_id")
    if task_id is not None and not isinstance(task_id, str):
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "current_task_id 必须是字符串或 null")
    reason = body.get("reason")
    if reason not in REASONS:
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               f"reason 必须属于 {REASONS}")
    nonce = body.get("request_nonce")
    if reason == "snapshot":
        if not _is_uuid(nonce):
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   "reason=snapshot 必须携带 nonce UUID")
    elif nonce is not None:
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               "非 snapshot 原因的 request_nonce 必须为 null")
    targets = body.get("targets")
    if not isinstance(targets, list):
        raise ImeGeometryError(422, "ime_geometry_invalid", "targets 必须是数组")
    ids = [t.get("id") for t in targets if isinstance(t, dict)]
    if sorted(ids) != sorted(TARGET_IDS):
        raise ImeGeometryError(422, "ime_geometry_invalid",
                               f"targets 必须恰好包含 {TARGET_IDS} 且不重复")
    for t in targets:
        for key in ("visible", "enabled", "clickable"):
            if not isinstance(t.get(key), bool):
                raise ImeGeometryError(422, "ime_geometry_invalid",
                                       f"targets[{t.get('id')}].{key} 必须是布尔")
        if not t.get("visible"):
            if t.get("bounds") is not None:
                raise ImeGeometryError(422, "ime_geometry_invalid",
                                       "不可见目标 bounds 必须为 null")
            if t.get("clickable"):
                raise ImeGeometryError(422, "ime_geometry_invalid",
                                       "不可见目标 clickable 必须为 false")
        elif t.get("bounds") is not None:
            _check_bounds(t["bounds"], display, f"targets[{t['id']}].bounds")
    if not body["geometry_valid"]:
        for t in targets:
            if t.get("bounds") is not None or t.get("clickable"):
                raise ImeGeometryError(422, "ime_geometry_invalid",
                                       "geometry_valid=false 不得携带可执行目标框")


class ImeGeometryService:
    """单进程内存态。Fleet 规划点击时 create_nonce_request；IME 客户端握手/上报/长轮询。

    FIX-15（§11.13）：三个端点处理路径手机 ID 之前必须先经可信身份反查
    （set_identity_verifier 注入；由当前 Runtime 按 IME 仓储唯一绑定反查）。
    未注入=独立部署（保持原合同）；注入后零绑定/多绑定/路径与唯一绑定不符
    一律 403，任意 body/path 不能取得或撤销另一手机的会话。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}      # runtime_session_id -> session
        self._active_by_device: dict[str, str] = {}  # ime_device_id -> session_id
        self._client_session: dict[tuple, str] = {}  # (device, client_instance) -> session
        self._replaced_clients: set[tuple] = set()
        self._nonces: dict[str, dict] = {}        # nonce -> request record
        self._identity_verifier = None

    def set_identity_verifier(self, verifier) -> None:
        """注入身份验证回调：verifier(ime_device_id, credentials) 不通过须抛
        ImeGeometryError(403/401)。

        FIX-15/R2-21（§11.13+§7.1）：Fleet 挂载时由当前 Runtime 提供（禁止跨
        Runtime 全局 service）。R2-21 起验证器必须核对**经过验证的手机身份**
        （每机独立可撤销凭据），只查路径绑定计数不构成鉴权。
        """
        self._identity_verifier = verifier

    def _verify_identity(self, ime_device_id: str, credentials=None) -> None:
        verifier = self._identity_verifier
        if verifier is None:
            return  # 独立部署（无验证器注入）保持原合同
        verifier(ime_device_id, credentials)

    # ---- 握手 ----
    def handshake(self, ime_device_id: str, body: dict,
                  credentials=None) -> dict:
        self._verify_identity(ime_device_id, credentials)
        if not isinstance(body, dict) \
                or body.get("contract_version") != CONTRACT_VERSION:
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   f"contract_version 必须为 {CONTRACT_VERSION}")
        if body.get("imeGeometryVersion") != IME_GEOMETRY_VERSION:
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   f"imeGeometryVersion 必须为 {IME_GEOMETRY_VERSION}")
        client = body.get("client_instance_id")
        if not _is_uuid(client):
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   "client_instance_id 必须是 UUID")
        with self._lock:
            key = (ime_device_id, client)
            if key in self._client_session:
                return {"contract_version": CONTRACT_VERSION,
                        "runtime_session_id": self._client_session[key]}
            if key in self._replaced_clients:
                raise ImeGeometryError(409, "ime_geometry_session_stale",
                                       "该 client_instance_id 已被更新实例替换，不能抢回会话")
            # 新实例：撤销该设备全部旧会话并登记被替换客户端
            active = self._active_by_device.get(ime_device_id)
            if active is not None:
                old = self._sessions[active]
                old_client = (ime_device_id, old["client_instance_id"])
                self._replaced_clients.add(old_client)
                self._client_session.pop(old_client, None)
                old["status"] = "revoked"
            session_id = str(uuid.uuid4())
            self._sessions[session_id] = {
                "session_id": session_id, "ime_device_id": ime_device_id,
                "client_instance_id": client, "status": "active",
                "max_seq": 0, "digests": {}, "responses": {},
                "current_view": None, "retired_views": [],
                "last_snapshot": None,
                "created_at": _iso(), "created_mono": _now(),
            }
            self._active_by_device[ime_device_id] = session_id
            self._client_session[key] = session_id
            return {"contract_version": CONTRACT_VERSION,
                    "runtime_session_id": session_id}

    # ---- 会话解析 ----
    def _session(self, ime_device_id: str, session_id: str) -> dict:
        session = self._sessions.get(session_id)
        if session is None or session["ime_device_id"] != ime_device_id:
            raise ImeGeometryError(409, "ime_geometry_session_stale",
                                   "会话不存在或不属于该设备")
        if session["status"] != "active":
            raise ImeGeometryError(409, "ime_geometry_session_stale",
                                   "会话已被更新实例撤销")
        return session

    # ---- 快照上报 ----
    def report(self, ime_device_id: str, body: dict,
               credentials=None) -> dict:
        self._verify_identity(ime_device_id, credentials)
        validate_snapshot(body)
        session_id = body["runtime_session_id"]
        nonce = body.get("request_nonce")
        digest = json.dumps(
            {k: v for k, v in body.items() if k != "server_received_at"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        received = time.time()
        with self._lock:
            session = self._session(ime_device_id, session_id)
            seq, view = body["seq"], body["view_instance_id"]
            # 幂等重放判定先于 nonce 复用判定：响应掉包后客户端按原样重发
            # 同一帧（同 seq 同摘要=同 nonce），必须返回原响应原时刻（掉包
            # 同号恢复），不得当作 nonce 复用拒绝；重放不更新任何快照状态
            # （不续鲜）。不同摘要的同 seq 或携带已答 nonce 的新帧仍按原
            # 语义拒绝。
            if seq in session["digests"]:
                if session["digests"][seq] == digest:
                    return dict(session["responses"][seq])  # 幂等：原响应原时刻
                raise ImeGeometryError(409, "ime_geometry_sequence_conflict",
                                       f"seq {seq} 摘要与首收不一致")
            if nonce is not None:
                self._check_nonce_for_report(session, nonce)
            if seq < session["max_seq"]:
                raise ImeGeometryError(409, "ime_geometry_sequence_conflict",
                                       f"seq {seq} 低于已收最高 {session['max_seq']}")
            if view in session["retired_views"]:
                raise ImeGeometryError(409, "ime_geometry_session_stale",
                                       "视图已被替换，迟到帧不能抢回当前视图")
            if session["current_view"] is not None and view != session["current_view"]:
                session["retired_views"].append(session["current_view"])
            session["current_view"] = view
            session["max_seq"] = seq
            session["digests"][seq] = digest
            response = {"contract_version": CONTRACT_VERSION,
                        "accepted_seq": seq,
                        "geometry_valid": body["geometry_valid"],
                        "server_received_at": _iso(received)}
            session["responses"][seq] = response
            session["last_snapshot"] = dict(body)
            session["last_snapshot_mono"] = received
            if nonce is not None:
                self._nonces[nonce]["snapshot_seq"] = seq
                self._nonces[nonce]["answered_mono"] = _now()
            return dict(response)

    def _check_nonce_for_report(self, session: dict, nonce: str) -> None:
        rec = self._nonces.get(nonce)
        if rec is None:
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   "request_nonce 不是本服务器签发")
        if rec["session_id"] != session["session_id"]:
            raise ImeGeometryError(409, "ime_geometry_nonce_expired",
                                   "nonce 不属于当前会话")
        if _now() - rec["issued_mono"] > NONCE_TTL_S:
            raise ImeGeometryError(409, "ime_geometry_nonce_expired",
                                   "nonce 已过 2 秒有效期")
        if not rec["claimed"]:
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   "nonce 尚未被长轮询领取")
        if rec["snapshot_seq"] is not None:
            raise ImeGeometryError(409, "ime_geometry_nonce_expired",
                                   "nonce 对应快照已收答，不可复用")

    # ---- nonce 请求（Fleet 规划侧） ----
    def create_nonce_request(self, ime_device_id: str,
                             purpose: str = "action_snapshot") -> str:
        with self._lock:
            # P2 卫生：签发前清理已过期且已答/已领的 nonce，防长进程缓涨
            now = _now()
            expired = [k for k, v in self._nonces.items()
                       if now - v["issued_mono"] > NONCE_TTL_S * 2]
            for k in expired:
                self._nonces.pop(k, None)
            nonce = str(uuid.uuid4())
            self._nonces[nonce] = {
                "nonce": nonce, "ime_device_id": ime_device_id,
                "purpose": purpose, "issued_at": _iso(),
                "issued_mono": _now(), "claimed": False,
                "session_id": self._active_by_device.get(ime_device_id),
                "snapshot_seq": None, "answered_mono": None,
                "click_consumed": False,
            }
            return nonce

    # ---- 只读长轮询 ----
    def poll_next(self, ime_device_id: str, runtime_session_id: str,
                  wait_seconds: int = 0, credentials=None) -> dict | None:
        self._verify_identity(ime_device_id, credentials)  # 鉴权在领取 nonce 之前
        self._assert_pollable(ime_device_id, runtime_session_id, wait_seconds)
        deadline = _now() + wait_seconds
        while True:
            claimed = self._claim_pending_nonce(ime_device_id, runtime_session_id)
            if claimed is not None:
                return claimed
            if _now() >= deadline:
                return None
            time.sleep(min(0.05, max(0.0, deadline - _now())))

    async def poll_next_async(self, ime_device_id: str, runtime_session_id: str,
                              wait_seconds: int = 0, credentials=None) -> dict | None:
        """R2-03：正式路由使用的可取消异步长轮询——不占事件循环/线程，
        取消立即退出且无遗留等待者（nonce 记录由创建方管理，等待者仅为循环）。"""
        import asyncio
        self._verify_identity(ime_device_id, credentials)
        self._assert_pollable(ime_device_id, runtime_session_id, wait_seconds)
        deadline = _now() + wait_seconds
        while True:
            claimed = self._claim_pending_nonce(ime_device_id, runtime_session_id)
            if claimed is not None:
                return claimed
            if _now() >= deadline:
                return None
            await asyncio.sleep(min(0.05, max(0.0, deadline - _now())))

    def _assert_pollable(self, ime_device_id: str, runtime_session_id: str,
                         wait_seconds: int) -> None:
        if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= MAX_WAIT_S:
            raise ImeGeometryError(422, "ime_geometry_invalid",
                                   f"waitSeconds 必须是 0-{MAX_WAIT_S} 整数")
        with self._lock:
            self._session(ime_device_id, runtime_session_id)  # 先核对会话再等候

    def _claim_pending_nonce(self, ime_device_id: str,
                             runtime_session_id: str) -> dict | None:
        """单次领取：恰领取一个到期前未领 nonce（只领取一次契约不变）。"""
        with self._lock:
            self._session(ime_device_id, runtime_session_id)
            for rec in self._nonces.values():
                if rec["ime_device_id"] != ime_device_id or rec["claimed"] \
                        or rec["session_id"] != runtime_session_id:
                    continue
                if _now() - rec["issued_mono"] > NONCE_TTL_S:
                    continue  # 过期请求不派发，留给创建方按 nonce_expired 处理
                rec["claimed"] = True
                return {"contract_version": CONTRACT_VERSION,
                        "runtime_session_id": runtime_session_id,
                        "request_nonce": rec["nonce"]}
            return None

    # ---- 动作前新鲜快照复核（§11.13.2） ----
    def fresh_snapshot(self, ime_device_id: str, nonce: str,
                       now: float | None = None) -> tuple[bool, str]:
        """返回 (是否新鲜, 原因)。接收距现在<=1s、nonce 距发出<=2s、会话/视图未失效。"""
        ok, reason, _snapshot = self.checked_snapshot(ime_device_id, nonce, now=now)
        return ok, reason

    def checked_snapshot(self, ime_device_id: str, nonce: str,
                         now: float | None = None,
                         target_id: str | None = None) -> tuple[bool, str, dict | None]:
        """R3-02-3：原子一致的已验证快照读取——校验与取快照同一把几何锁内
        完成，规划侧不能再"验证后另取 latest"（两次读取之间新快照到达会造成
        验证对象与使用对象不一致）。返回 (是否新鲜, 原因, 验证过的快照)。"""
        now = _now() if now is None else now
        with self._lock:
            session = self._sessions.get(self._active_by_device.get(ime_device_id) or "")
            if session is None or session["status"] != "active":
                return False, "session_stale", None
            rec = self._nonces.get(nonce)
            if rec is None or not rec["claimed"]:
                return False, "nonce_missing", None
            if now - rec["issued_mono"] > NONCE_TTL_S:
                return False, "nonce_expired", None
            if rec["snapshot_seq"] is None or rec["answered_mono"] is None:
                return False, "snapshot_missing", None
            if now - rec["answered_mono"] > 1.0:
                return False, "snapshot_stale", None
            snap = session["last_snapshot"] or {}
            if not snap.get("geometry_valid"):
                return False, "geometry_invalid", None
            if snap.get("runtime_session_id") != session["session_id"]:
                return False, "session_stale", None
            if snap.get("view_instance_id") != session["current_view"]:
                return False, "view_stale", None
            if rec["snapshot_seq"] != session["max_seq"]:
                return False, "snapshot_superseded", None
            if target_id is not None:
                targets = {t.get("id"): t for t in snap.get("targets") or []
                           if isinstance(t, dict)}
                target = targets.get(target_id) or {}
                if not (target.get("visible") and target.get("clickable")):
                    return False, "target_not_executable", None
            return True, "ok", dict(snap)

    def wait_checked_snapshot(self, ime_device_id: str, nonce: str,
                              timeout_s: float = NONCE_TTL_S,
                              target_id: str | None = None,
                              ) -> tuple[bool, str, dict | None]:
        """R3-02-3：规划侧等待的原子版——返回**验证过的同一份快照**，不另取
        latest。不持任何外层锁（输入短锁不占用），关闭/取消不被阻塞。"""
        deadline = _now() + max(0.0, float(timeout_s))
        while True:
            ok, reason, snapshot = self.checked_snapshot(
                ime_device_id, nonce, target_id=target_id)
            if ok:
                return True, reason, snapshot
            if reason not in ("snapshot_missing", "nonce_missing", "nonce_expired") \
                    or _now() >= deadline:
                return False, reason, None
            time.sleep(0.02)

    def consume_for_click(self, ime_device_id: str, nonce: str, *,
                          target_id: str, expected_task: str | None,
                          expected_session: str, expected_display: dict,
                          guard=None, now: float | None = None) -> dict:
        """R3-02：TAP 下发边界的最终几何校验+一次性消费。

        与快照接收/会话撤销同一把几何锁内完成：快照年龄<=1s、仍为最高有效
        seq/当前 View、目标可见可点、runtime_session 与 display 与规划一致、
        候选 task 未变化（expected_task 规划时绑定；候选栏必须非空）、nonce
        未被消费（同一 nonce 不能授权第二次点击）。guard（凭据/绑定核对）
        也在锁内执行，杜绝"检查与消费之间接纳隐藏/新 seq 后仍授权旧快照"。
        成功即标记 click_consumed；失败抛 ImeGeometryError（不消费）。
        """
        now = _now() if now is None else now
        with self._lock:
            if callable(guard):
                guard()
            session = self._sessions.get(self._active_by_device.get(ime_device_id) or "")
            if session is None or session["status"] != "active":
                raise ImeGeometryError(409, "ime_geometry_session_stale",
                                       "最终接触核验：会话已失效")
            rec = self._nonces.get(nonce)
            if rec is None or not rec["claimed"] \
                    or rec["snapshot_seq"] is None or rec["answered_mono"] is None:
                raise ImeGeometryError(409, "ime_geometry_nonce_expired",
                                       "最终接触核验：nonce 无已回答快照")
            if now - rec["issued_mono"] > NONCE_TTL_S:
                raise ImeGeometryError(409, "ime_geometry_nonce_expired",
                                       "最终接触核验：nonce 已过 2 秒期限")
            if rec.get("click_consumed"):
                raise ImeGeometryError(409, "ime_geometry_nonce_expired",
                                       "最终接触核验：同一 nonce 不授权第二次点击")
            if now - rec["answered_mono"] > 1.0:
                raise ImeGeometryError(409, "ime_geometry_snapshot_stale",
                                       "最终接触核验：快照距接收已超过 1 秒")
            snap = session["last_snapshot"] or {}
            if not snap.get("geometry_valid") \
                    or snap.get("runtime_session_id") != session["session_id"] \
                    or snap.get("runtime_session_id") != expected_session:
                raise ImeGeometryError(409, "ime_geometry_session_stale",
                                       "最终接触核验：会话/几何有效性已变化")
            if snap.get("view_instance_id") != session["current_view"]:
                raise ImeGeometryError(409, "ime_geometry_view_stale",
                                       "最终接触核验：视图已被替换（新 View/隐藏）")
            if rec["snapshot_seq"] != session["max_seq"]:
                raise ImeGeometryError(409, "ime_geometry_sequence_conflict",
                                       "最终接触核验：快照已被更新 seq 取代")
            display = snap.get("display") or {}
            if (display.get("width_px"), display.get("height_px")) != \
                    (expected_display.get("width_px"), expected_display.get("height_px")):
                raise ImeGeometryError(409, "ime_geometry_view_stale",
                                       "最终接触核验：display 尺寸与规划不一致")
            if any(key in expected_display and display.get(key) != expected_display[key]
                   for key in ("display_id", "rotation", "density")):
                raise ImeGeometryError(409, "ime_geometry_view_stale",
                                       "最终接触核验：display 方向、密度或显示器与规划不一致")
            current_task = snap.get("current_task_id")
            if current_task != expected_task:
                raise ImeGeometryError(409, "ime_geometry_task_changed",
                                       "最终接触核验：候选 task 已变化，旧目标作废")
            targets = {t.get("id"): t for t in snap.get("targets") or []
                       if isinstance(t, dict)}
            target = targets.get(target_id) or {}
            if not (target.get("visible") and target.get("clickable")):
                raise ImeGeometryError(409, "ime_geometry_target_not_executable",
                                       "最终接触核验：目标不可见或不可点击（键盘隐藏/销毁）")
            rec["click_consumed"] = True
            return dict(snap)

    def wait_for_snapshot(self, ime_device_id: str, nonce: str,
                          timeout_s: float = NONCE_TTL_S) -> tuple[bool, str]:
        """Fleet 规划侧等待：等手机领取 nonce 并回快照，直到新鲜或超期。

        不持任何锁（输入短锁/几何锁均不占用），关闭/取消不被阻塞（F15-4）。
        已到达但不满足条件（geometry_invalid/view_stale 等）立即失败，不空等。
        """
        deadline = _now() + max(0.0, float(timeout_s))
        while True:
            ok, reason = self.fresh_snapshot(ime_device_id, nonce)
            if ok:
                return True, reason
            if reason not in ("snapshot_missing", "nonce_missing", "nonce_expired") \
                    or _now() >= deadline:
                return False, reason
            time.sleep(0.02)

    def latest_snapshot(self, ime_device_id: str) -> dict | None:
        with self._lock:
            session = self._sessions.get(self._active_by_device.get(ime_device_id) or "")
            return dict(session["last_snapshot"]) if session else None


_SHARED: ImeGeometryService | None = None
_SHARED_LOCK = threading.Lock()


def shared_service() -> ImeGeometryService:
    """进程级共享单例：fleet app 与 screen_ime 路由共用同一会话/nonce 状态。"""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = ImeGeometryService()
        return _SHARED
