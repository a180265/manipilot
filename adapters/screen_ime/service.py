"""屏幕输入法任务服务：K40TaskServer（Java）的 Python 异步移植。

移植来源：AI_demo 工作区 K40TaskServer（TaskRepository / TaskHttpServer）。
协议边界与原版保持一致，已有手机端输入法（Android K40 输入法、iOS 键盘扩展）
无需任何改动即可接入：

- 长轮询：任务下发 GET /api/v1/devices/{id}/tasks/next、取消控制
  GET /api/v1/tasks/{id}/control/next，waitSeconds 上限 20 秒
- 心跳：最近 45 秒内有心跳且 keyboard_active=true 才允许创建任务
- 状态机：pending → displayed → typing → inserted；失败 typing_failed；
  取消 pending→cancelled、displayed/typing→cancel_requested→cancelled
- 幂等：同 taskId 同内容重放返回原任务；不同内容 409 task_conflict
- 文本边界：content 最长 10000 字符；输入文本必须原样下发，服务端不改写
- 错误响应：{"error": <code>, "message": <message>}，错误码与 HTTP 状态同原版

安全边界（继承原设计）：本服务无认证，仅允许部署在可信局域网；
校准会话只存内存，服务重启即失效；任务不自动重试、不自动清理。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- 协议常量（与 Java 版一致） ----
MAX_CONTENT_LENGTH = 10_000
MAX_PAGE_SIZE = 100
ONLINE_WINDOW = timedelta(seconds=45)
MAX_BODY_BYTES = 64 * 1024
TYPING_SPEEDS = ("slow", "normal", "fast", "random")

TASK_STATUSES = (
    "pending", "displayed", "typing", "inserted",
    "typing_failed", "cancel_requested", "cancelled",
)
TERMINAL_STATUSES = frozenset({"inserted", "typing_failed", "cancelled"})

# 状态迁移表（TaskStatus.canTransitionTo）
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"displayed", "cancel_requested", "cancelled"}),
    "displayed": frozenset({"typing", "cancel_requested", "cancelled"}),
    "typing": frozenset({"inserted", "typing_failed", "cancel_requested", "cancelled"}),
    "cancel_requested": frozenset({"cancelled"}),
    "inserted": frozenset(),
    "typing_failed": frozenset(),
    "cancelled": frozenset(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    target_user TEXT NOT NULL,
    content TEXT NOT NULL,
    requested_speed TEXT NOT NULL,
    resolved_speed TEXT NOT NULL,
    status TEXT NOT NULL,
    typed_character_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ime_tasks_device_status_created
    ON tasks(device_id, status, created_at);
CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    typed_character_count INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    app_version TEXT NOT NULL,
    keyboard_active INTEGER NOT NULL,
    current_task_id TEXT,
    last_seen_at TEXT NOT NULL,
    input_task_control_version INTEGER,
    input_control_reported_at TEXT,
    input_control_runtime_instance TEXT
);
CREATE TABLE IF NOT EXISTS ime_deliveries (
    task_id TEXT PRIMARY KEY,
    delivered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    command TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE TABLE IF NOT EXISTS service_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


from runtime.input_control import InputControlError


class ScreenImeApiError(Exception):
    """与 K40TaskServer 协议兼容的错误（{"error","message"} + HTTP 状态码）。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def now_iso() -> str:
    """Java Instant 风格 UTC 时间（毫秒精度 + Z 后缀），保持协议兼容。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScreenImeApiError(400, "invalid_request", f"{field} must not be blank")
    return value


def _blank_to_none(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resolve_speed(requested) -> tuple[str, str, int]:
    """解析 typingSpeed：返回 (requested, resolved)。random 随机取一档。"""
    if requested is None or (isinstance(requested, str) and not requested.strip()):
        requested_name = "random"
    elif isinstance(requested, str) and requested.strip().lower() in TYPING_SPEEDS:
        requested_name = requested.strip().lower()
    else:
        raise ScreenImeApiError(400, "invalid_typing_speed",
                                "typingSpeed must be slow, normal, fast, or random")
    if requested_name != "random":
        return requested_name, requested_name
    return "random", random.choice(("slow", "normal", "fast"))


def _normalize_source(source) -> str:
    if not isinstance(source, str) or not source.strip():
        return "api"
    return "web" if source.strip().lower() == "web" else "api"


class ImeCancellationWorker:
    """FIX-02（§4.8.6）：每设备取消工作器（本仓储为单挂载组）。

    - 启动即扫描一次；关闭写入/回执到达可经 notify() 提前唤醒；
    - 每 interval_seconds（默认 2 秒）兜底扫描一次，即使丢失全部内存通知；
    - 单工作线程串行扫描，永不重叠；扫描状态完全取自持久 IME DB。
    interval 只是调度安排，不是判定手机已取消/安全的超时阈值。
    """

    def __init__(self, repo: "ImeTaskRepository", *, interval_seconds: float = 2.0,
                 time_func=time.monotonic, poll_seconds: float = 0.25):
        self._repo = repo
        self._interval = float(interval_seconds)
        self._time = time_func
        self._poll = max(float(poll_seconds), 0.001)
        self._wake = threading.Event()
        self._stop_flag = False
        self._scan_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._scans = 0

    @property
    def running(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and not self._stop_flag)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_flag = False
            self._thread = threading.Thread(
                target=self._run, name="ime-cancellation-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            self._stop_flag = True
            self._wake.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def notify(self) -> None:
        """关闭写入/回执到达等事件提前唤醒下一轮扫描。"""
        self._wake.set()

    @property
    def scan_count(self) -> int:
        with self._scan_lock:
            return self._scans

    def scan_once(self) -> int:
        """同步触发一次扫描（与工作线程共用扫描锁，永不重叠）。"""
        with self._scan_lock:
            self._scans += 1
            return self._repo.sweep_cancellations()

    def _run(self) -> None:
        next_scan = self._time() + self._interval
        while not self._stop_flag:
            try:
                self.scan_once()
            except Exception:
                pass  # 扫描失败不终止工作器；下一周期重试
            if self._stop_flag:
                return
            while not self._stop_flag:
                if self._wake.is_set():
                    break  # 提前唤醒
                now = self._time()
                if now >= next_scan:
                    break  # 周期兜底到点
                self._wake.wait(timeout=min(max(next_scan - now, 0.0), self._poll))
            self._wake.clear()
            next_scan = self._time() + self._interval


class ImeTaskRepository:
    """任务/设备/控制命令仓储（TaskRepository 的移植，asyncio 长轮询）。"""

    def __init__(self, db_path: Path, control_hooks: dict | None = None):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # §4.8.5：Fleet 挂载时注入输入控制钩子（typing_claim/deliver/cancel/ack）；
        # 未注入=独立服务，保持原合同。构造期注入不启动取消工作器/挂载标记，
        # 生产装配走 set_control_hooks（P07）。
        self._control_hooks = dict(control_hooks or {})
        self._thread_lock = threading.RLock()
        self._cancel_mutex = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._thread_lock:
            self._conn.executescript(SCHEMA)
            self._ensure_control_columns()
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()
        self._condition = asyncio.Condition()
        # FIX-02：事件循环绑定 + 挂载元信息 + 取消工作器（仅 Fleet 挂载启动）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: ImeCancellationWorker | None = None
        self._closed = False
        # FIX-15（§4.9.3）：服务端本次启动 UUID。历史心跳即使仍在 45 秒
        # ONLINE_WINDOW 内也不能跨服务重启复用；比较用内存值，service_meta
        # 落库仅为可观测（不作为资格来源）。
        self.runtime_instance = str(uuid.uuid4())
        self._set_meta("input_control_runtime_instance", self.runtime_instance)

    def _ensure_control_columns(self) -> None:
        """FIX-15：旧库补齐三列 nullable 控制版本列（新库由 SCHEMA 直接建出）。"""
        existing = {row["name"] for row in
                    self._conn.execute("PRAGMA table_info(devices)").fetchall()}
        for column in ("input_task_control_version INTEGER",
                       "input_control_reported_at TEXT",
                       "input_control_runtime_instance TEXT"):
            name = column.split()[0]
            if name not in existing:
                self._conn.execute(f"ALTER TABLE devices ADD COLUMN {column}")

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        worker = self._worker
        if worker is not None:
            worker.stop()
            self._worker = None
        with self._thread_lock:
            with contextlib.suppress(Exception):
                self._conn.close()

    def _ensure_open(self) -> None:
        if getattr(self, "_closed", False):
            raise ScreenImeApiError(503, "service_closed", "输入法服务已关闭")

    def set_control_hooks(self, hooks: dict) -> None:
        """Fleet 装配时注入（P07）；传空字典恢复独立服务原合同。

        注入非空钩子=持久 Fleet 挂载标记（§4.8.5-10 service_meta）并启动
        取消工作器（启动扫描 + 2 秒兜底）；清空钩子=撤销标记并停工作器。
        """
        hooks = dict(hooks or {})
        self._control_hooks = hooks
        self._set_meta("fleet_mounted", "1" if hooks else "0")
        if hooks and self._worker is None:
            recover = hooks.get("recover_direct")
            if recover is not None:
                # 装配恢复先撤销旧直发任务，再启动取消扫描；查询结束后释放
                # IME 线程锁，回调可按设备短锁 -> Store 的顺序核对许可。
                rows = self._query_all(
                    "SELECT task_id, device_id, status, EXISTS(SELECT 1 FROM ime_deliveries d"
                    " WHERE d.task_id=tasks.task_id) AS delivered FROM tasks WHERE status IN"
                    " ('pending','displayed','typing','cancel_requested')")
                for row in rows:
                    recover(row["task_id"], row["device_id"],
                            delivered=bool(row["delivered"] or row["status"] != "pending"))
            self._worker = ImeCancellationWorker(self)
            self._worker.start()
        elif not hooks and self._worker is not None:
            self._worker.stop()
            self._worker = None

    @property
    def cancellation_worker(self) -> ImeCancellationWorker | None:
        return self._worker

    # ---- 底层 ----
    def _bind_loop(self) -> None:
        """记录当前事件循环（线程安全唤醒长轮询等待者用）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is None or self._loop.is_closed():
            self._loop = loop

    def wake_waiters(self) -> None:
        """线程安全唤醒所有长轮询等待者（取消工作器线程可调用）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _notify_on_loop():
            async def _notify():
                async with self._condition:
                    self._condition.notify_all()
            try:
                asyncio.ensure_future(_notify())
            except RuntimeError:
                pass

        try:
            loop.call_soon_threadsafe(_notify_on_loop)
        except RuntimeError:
            pass  # 事件循环已关闭

    def _wake_worker(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.notify()

    def _get_meta(self, key: str) -> str | None:
        row = self._query_one("SELECT value FROM service_meta WHERE key=?", (key,))
        return row["value"] if row is not None else None

    def _set_meta(self, key: str, value: str) -> None:
        with self._thread_lock:
            self._ensure_open()
            self._conn.execute(
                "INSERT INTO service_meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn.commit()

    def _require_control_ready(self, action: str) -> None:
        """§4.8.5-10：Fleet 挂载标记存在但缺 InputControl 注入 → 业务输入不可用。

        只拦 create/next/typing 等业务输入路径；取消、状态、心跳照常。
        """
        if self._control_hooks:
            return
        if self._get_meta("fleet_mounted") == "1":
            raise ScreenImeApiError(
                503, "input_control_not_ready",
                f"Fleet 挂载输入法服务缺少输入控制注入，{action} 暂不可用")

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """事务内语句：不提交（由 _transaction 统一提交/回滚）。"""
        return self._conn.execute(sql, params)

    @contextlib.contextmanager
    def _transaction(self):
        """§4.8.5-7：取消命令/任务状态/历史（或 ack 三件）一次 IME DB 事务。

        全提交或全未提交；故障注入/崩溃不留下"命令已确认、任务未取消"中间态。
        """
        with self._thread_lock:
            self._ensure_open()
            if self._conn.in_transaction:
                raise RuntimeError("IME DB 事务嵌套")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._rollback_quietly()
                raise
            else:
                try:
                    self._conn.commit()
                except BaseException:
                    # 提交失败同样回滚：连接上不留半笔未决写入
                    self._rollback_quietly()
                    raise

    def _rollback_quietly(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.rollback()

    def _write(self, sql: str, params: tuple = ()):
        with self._thread_lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._thread_lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._thread_lock:
            return self._conn.execute(sql, params).fetchone()

    def _task_view(self, row: sqlite3.Row) -> dict:
        return {
            "taskId": row["task_id"],
            "deviceId": row["device_id"],
            "targetUser": row["target_user"],
            "content": row["content"],
            "typingSpeed": row["requested_speed"],
            "resolvedTypingSpeed": row["resolved_speed"],
            "status": row["status"],
            "typedCharacterCount": row["typed_character_count"],
            "failureReason": row["failure_reason"],
            "source": row["source"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _insert_history(self, task_id: str, status: str, typed: int, reason, now: str) -> None:
        self._write(
            "INSERT INTO task_history(task_id, status, typed_character_count, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, status, typed, reason, now),
        )

    def _set_status(self, task_id: str, status: str, typed: int, reason, now: str) -> None:
        self._write(
            "UPDATE tasks SET status=?, typed_character_count=?, failure_reason=?, updated_at=?"
            " WHERE task_id=?",
            (status, typed, reason, now, task_id),
        )

    def _notify(self) -> None:
        """唤醒所有长轮询等待者（调用方必须已持有 self._condition 的锁）。"""
        self._condition.notify_all()

    # ---- 创建（含幂等重放与设备可用性检查） ----
    async def create(self, request: dict, *, direct: bool = False) -> tuple[dict, bool]:
        self._bind_loop()
        self._require_control_ready("create")
        if not isinstance(request, dict):
            raise ScreenImeApiError(400, "invalid_request", "Request body is required")
        task_id = _require_text(request.get("taskId"), "taskId")
        device_id = _require_text(request.get("deviceId"), "deviceId")
        target_user = _require_text(request.get("targetUser"), "targetUser")
        content = _require_text(request.get("content"), "content")
        if len(content) > MAX_CONTENT_LENGTH:
            raise ScreenImeApiError(400, "content_too_long",
                                    "content must not exceed 10000 characters")
        requested_speed, resolved_speed = _resolve_speed(request.get("typingSpeed"))
        source = _normalize_source(request.get("source"))

        def find_existing():
            row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if row is not None:
                task = self._task_view(row)
                identical = (
                    task["deviceId"] == device_id
                    and task["targetUser"] == target_user
                    and task["content"] == content
                    and task["typingSpeed"] == requested_speed
                )
                if not identical:
                    raise ScreenImeApiError(409, "task_conflict",
                                            "taskId already exists with different data")
                return task

            device = self._query_one("SELECT keyboard_active, last_seen_at FROM devices WHERE device_id=?",
                                     (device_id,))
            if (device is None or device["keyboard_active"] == 0
                    or parse_iso(device["last_seen_at"]) < datetime.now(timezone.utc) - ONLINE_WINDOW):
                raise ScreenImeApiError(409, "device_unavailable",
                                        "Device is offline or its keyboard is closed")
            return None

        @contextlib.contextmanager
        def repository_admission():
            # 独立/内部平台路径不取得设备锁；保持仓储自身的幂等串行。
            with self._thread_lock:
                yield find_existing()

        def create_sync():
            # 原任务重放只读，不重新接纳许可（关闭/换绑后仍返回原状态）。
            existing = find_existing()
            if existing is not None:
                return existing, False
            admit = None
            if direct and self._control_hooks:
                admit = self._control_hooks.get("direct_create")
                if admit is None:
                    raise ScreenImeApiError(503, "input_control_not_ready",
                                            "Fleet 直发输入许可接纳尚未就绪")
            now = now_iso()
            try:
                # 设备短锁内重读原任务，再接纳 owner/认领固定 slot/同步入库。
                # 此处不持 IME 条件锁、不 await；完成后才通知长轮询。
                admission = (admit(task_id, device_id, find_existing=find_existing, payload={
                    "deviceId": device_id, "targetUser": target_user,
                    "content": content, "typingSpeed": requested_speed})
                    if admit is not None else repository_admission())
                with admission as existing:
                    if existing is not None:
                        return existing, False
                    with self._transaction():
                        self._execute(
                            "INSERT INTO tasks(task_id, device_id, target_user, content, requested_speed,"
                            " resolved_speed, status, typed_character_count, failure_reason, source,"
                            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?, ?)",
                            (task_id, device_id, target_user, content, requested_speed, resolved_speed,
                             source, now, now),
                        )
                        self._execute(
                            "INSERT INTO task_history(task_id, status, typed_character_count, reason, created_at)"
                            " VALUES (?, 'pending', 0, NULL, ?)", (task_id, now))
            except InputControlError as exc:
                raise ScreenImeApiError(
                    getattr(exc, "hook_status", 409),
                    getattr(exc, "hook_code", None) or exc.reason_code, str(exc)) from exc
            task = {
                "taskId": task_id, "deviceId": device_id, "targetUser": target_user,
                "content": content, "typingSpeed": requested_speed,
                "resolvedTypingSpeed": resolved_speed, "status": "pending",
                "typedCharacterCount": 0, "failureReason": None, "source": source,
                "createdAt": now, "updatedAt": now,
            }
            return task, True

        # 仅新直发链把等待设备锁交给线程；内部平台调用可能已认领创建 effect，
        # 必须立即同步插入，不在其认领与插入之间另排异步任务。
        task, created = (await asyncio.to_thread(create_sync)
                         if direct and self._control_hooks else create_sync())
        if created:
            async with self._condition:
                self._notify()
        return task, created

    # ---- 手机端长轮询取任务 ----
    async def next_task(self, device_id: str, wait_seconds: int, *,
                        disconnect_check=None) -> dict | None:
        self._bind_loop()
        self._require_control_ready("next")
        _require_text(device_id, "deviceId")
        remaining = max(0.0, float(wait_seconds))
        deadline = asyncio.get_running_loop().time() + remaining
        async with self._condition:
            while True:
                # A phone may time out/close its input view during the long poll.
                # Do not claim its one-time delivery after ASGI saw disconnect.
                if disconnect_check is not None and await disconnect_check():
                    return None
                row = self._query_one(
                    "SELECT * FROM tasks WHERE device_id=? AND status IN ('pending','displayed')"
                    " AND task_id NOT IN (SELECT task_id FROM ime_deliveries)"
                    " ORDER BY CASE status WHEN 'displayed' THEN 0 ELSE 1 END, created_at ASC LIMIT 1",
                    (device_id,),
                )
                if row is not None:
                    # §4.8.5-3：同一 taskId 正文最多交付一次——ime_deliveries 先行
                    # 持久化（含响应丢失窗口，重启后不再交付）
                    self._write(
                        "INSERT OR IGNORE INTO ime_deliveries (task_id, delivered_at)"
                        " VALUES (?, ?)", (row["task_id"], now_iso()))
                    if "deliver" in self._control_hooks:
                        try:
                            self._control_hooks["deliver"](row["task_id"], device_id)
                        except InputControlError:
                            # §4.8.5-2：关闭/许可失效/绑定变化——不返回正文，
                            # 保持原长轮询等待到原截止时间（到点 204，不忙轮询）；
                            # 交付已持久认领，交由取消工作器对账为 cancel_requested。
                            self._wake_worker()
                            row = None
                    if row is not None:
                        return self._task_view(row)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                try:
                    interval = min(remaining, 0.25) if disconnect_check is not None else remaining
                    await asyncio.wait_for(self._condition.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    if disconnect_check is None:
                        return None

    # ---- Agent 侧等待状态 ----
    async def wait_for_status(self, task_id: str, expected: set[str], wait_seconds: int,
                              probe=None) -> dict:
        _require_text(task_id, "taskId")
        if not expected:
            raise ScreenImeApiError(400, "invalid_request", "expected status is required")
        remaining = max(0.0, float(wait_seconds))
        deadline = asyncio.get_running_loop().time() + remaining
        async with self._condition:
            while True:
                row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
                if row is None:
                    raise ScreenImeApiError(404, "task_not_found", "Task not found")
                task = self._task_view(row)
                if task["status"] in expected or task["status"] in TERMINAL_STATUSES:
                    return task
                if probe is not None:
                    try:
                        triggered = bool(probe())
                    except Exception:
                        triggered = False
                    if triggered:
                        # 关闭唤醒等待：输入被关闭时立即取消并返回真实状态，
                        # 不继续长等到原截止时间。（条件锁已持有：走同步取消核心）
                        with self._cancel_mutex, contextlib.suppress(Exception):
                            self._cancel_locked(task_id)
                        row = self._query_one(
                            "SELECT * FROM tasks WHERE task_id=?", (task_id,))
                        return self._task_view(row)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return task
                slice_seconds = remaining
                if probe is not None:
                    slice_seconds = min(remaining, 0.5)
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=slice_seconds)
                except asyncio.TimeoutError:
                    if probe is None:
                        return task

    # ---- 手机端回报状态 ----
    async def update_status(self, task_id: str, request: dict) -> dict:
        self._bind_loop()
        _require_text(task_id, "taskId")
        if not isinstance(request, dict):
            raise ScreenImeApiError(400, "invalid_request", "Request body is required")
        device_id = _require_text(request.get("deviceId"), "deviceId")
        status = _require_text(request.get("status"), "status")
        if status not in TASK_STATUSES:
            raise ScreenImeApiError(400, "invalid_status", "Unsupported task status")
        if status == "typing":
            # §4.8.5-10：挂载模式缺输入控制注入时开始许可不可用
            self._require_control_ready("typing")

        async with self._condition:
            row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if row is None:
                raise ScreenImeApiError(404, "task_not_found", "Task not found")
            current = self._task_view(row)
            if current["deviceId"] != device_id:
                raise ScreenImeApiError(409, "device_mismatch",
                                        "Task belongs to a different device")
            if (status not in ("cancel_requested", "cancelled")
                    and current["status"] in ("cancel_requested", "cancelled")
                    and "typing_claim" in self._control_hooks):
                if status == "typing":
                    # typing 是开始许可，Android 把任意 200 视作允许输入。
                    raise ScreenImeApiError(409, "input_authorization_revoked",
                                            "任务已取消，不再允许开始输入")
                # §4.8.5-5/7：取消后迟到的 displayed/终态/进度补报只补录
                # 观察事实，回 200+真实取消态；不改回可领取状态、不复活任务
                #（旧端 displayed 后才启动控制监听，需要原 cancel 命令可领取）。
                now = now_iso()
                self._insert_history(task_id, f"{status}_observed",
                                     current["typedCharacterCount"], None, now)
                row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
                return self._task_view(row)
            if status != current["status"] and status not in _TRANSITIONS[current["status"]]:
                raise ScreenImeApiError(409, "invalid_transition",
                                        f"Cannot transition from {current['status']} to {status}")
            typed = request.get("typedCharacterCount")
            typed = current["typedCharacterCount"] if typed is None else int(typed)
            if typed < 0 or typed > MAX_CONTENT_LENGTH:
                raise ScreenImeApiError(400, "invalid_typed_count",
                                        "typedCharacterCount is out of range")
            reason = _blank_to_none(request.get("failureReason"))
            if isinstance(reason, str) and len(reason) > 500:
                reason = reason[:500]
            if status == "typing" and "typing_claim" in self._control_hooks:
                # §4.8.5-6：重复 typing（含同值重放）必须 409 already_claimed，
                # 不得被"同值去重"吞成 200。
                try:
                    self._control_hooks["typing_claim"](task_id, device_id)
                except InputControlError as exc:
                    raise ScreenImeApiError(
                        409, getattr(exc, "hook_code", None) or "input_authorization_revoked",
                        str(exc)) from exc
            if (current["status"] == status
                    and typed == current["typedCharacterCount"]
                    and reason == current["failureReason"]):
                return current
            if status == "displayed":
                other = self._query_one(
                    "SELECT 1 FROM tasks WHERE device_id=? AND status='displayed' AND task_id<>? LIMIT 1",
                    (device_id, task_id),
                )
                if other is not None:
                    raise ScreenImeApiError(409, "device_has_displayed_task",
                                            "Device already has another displayed task")
            now = now_iso()
            self._set_status(task_id, status, typed, reason, now)
            self._insert_history(task_id, status, typed, reason, now)
            self._notify()
            return {**current, "status": status, "typedCharacterCount": typed,
                    "failureReason": reason, "updatedAt": now}

    # ---- 取消 ----
    async def cancel(self, task_id: str) -> dict:
        self._bind_loop()
        _require_text(task_id, "taskId")
        async with self._condition:
            # 同步短临界区（cancel_mutex 与工作器扫描互斥；内无 await，不跨网络）
            with self._cancel_mutex:
                return self._cancel_locked(task_id)

    def _cancel_locked(self, task_id: str) -> dict:
        """取消核心：条件锁与 _cancel_mutex 已持有（同步、单事务提交）。"""
        row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if row is None:
            raise ScreenImeApiError(404, "task_not_found", "Task not found")
        current = self._task_view(row)
        if current["status"] in ("cancelled", "cancel_requested"):
            return current
        if current["status"] in TERMINAL_STATUSES:
            raise ScreenImeApiError(409, "task_terminal",
                                    "Completed or failed task cannot be cancelled")
        now = now_iso()
        # §4.8.5-4：pending 不是"没发给手机"的证明——交付已认领的 pending
        # 与 displayed/typing 一样进入 cancel_requested 等原手机确认。
        delivered = (current["status"] != "pending"
                     or self._query_one(
                         "SELECT task_id FROM ime_deliveries WHERE task_id=?",
                         (task_id,)) is not None)
        next_status = "cancel_requested" if delivered else "cancelled"
        if "cancel" in self._control_hooks:
            # §4.8.5：身份核验（原任务/原手机）在 IME 变更前执行；
            # 拒绝则 409 且 IME 状态不变。
            try:
                self._control_hooks["cancel"](task_id, current["deviceId"],
                                              delivered=delivered)
            except InputControlError as exc:
                raise ScreenImeApiError(
                    409, getattr(exc, "hook_code", None) or "input_control_rejected",
                    str(exc)) from exc
        with self._transaction():
            if next_status == "cancel_requested":
                pending = self._execute(
                    "SELECT id FROM control_commands WHERE task_id=? AND state='pending'"
                    " ORDER BY id ASC LIMIT 1", (task_id,)).fetchone()
                if pending is None:
                    self._execute(
                        "INSERT INTO control_commands(task_id, command, state, created_at)"
                        " VALUES (?, 'cancel', 'pending', ?)", (task_id, now))
            self._execute(
                "UPDATE tasks SET status=?, typed_character_count=?, failure_reason=?,"
                " updated_at=? WHERE task_id=?",
                (next_status, current["typedCharacterCount"], None, now, task_id))
            self._execute(
                "INSERT INTO task_history(task_id, status, typed_character_count,"
                " reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, next_status, current["typedCharacterCount"],
                 "cancel requested", now))
        self._notify()
        self._wake_worker()
        row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return self._task_view(row)

    # ---- FIX-02：持久取消清扫（重启/丢通知后从 IME DB 恢复取消意图） ----
    def sweep_cancellations(self) -> int:
        """每设备取消工作器的持久扫描：不依赖内存通知，可重复触发、幂等。

        只处理有取消理由的任务，绝不动健康在途候选：
        - cancel_requested 任务缺 pending cancel 控制命令 → 补一条（幂等）；
        - displayed/typing 任务经取消检查（撤销边界/owner 撤销/无归属）确认
          须取消时 → 转 cancel_requested 并补控制命令（等待原手机确认）；
        - pending 任务经取消检查确认须取消时：已有交付认领（ime_deliveries
          有记录）→ 转 cancel_requested 并补控制命令（deliver 认领≠安全，
          pending 不是"没发给手机"的证明）；从未交付 → 本地直接取消
          （原语义，无需手机握手）。
        取消命令/任务状态/历史在同一次 IME DB 事务提交（§4.8.5-7）。
        返回本轮补发/核对的控制命令数。
        """
        transitions: list[tuple[str, str, int, bool]] = []
        commands: list[str] = []
        with self._cancel_mutex:
            with self._thread_lock:
                self._ensure_open()
                now = now_iso()
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status IN"
                    " ('pending','displayed','typing','cancel_requested')"
                ).fetchall()
                for row in rows:
                    task_id = row["task_id"]
                    has_pending_cancel = self._conn.execute(
                        "SELECT id FROM control_commands WHERE task_id=?"
                        " AND command='cancel' AND state='pending'",
                        (task_id,)).fetchone() is not None
                    typed = int(row["typed_character_count"] or 0)
                    if row["status"] == "cancel_requested":
                        # 取消意图已存在：只补缺失的待确认命令
                        if not has_pending_cancel:
                            commands.append(task_id)
                        continue
                    if row["status"] in ("displayed", "typing"):
                        # 已在手机侧：只能 cancel_requested 等原手机确认
                        if not self._pending_needs_cancel(task_id, row["device_id"]):
                            continue
                        transitions.append((task_id, "cancel_requested", typed, True))
                        if not has_pending_cancel:
                            commands.append(task_id)
                        continue
                    # pending：必须有取消理由（撤销边界/owner 撤销/无归属）才清扫
                    if not self._pending_needs_cancel(task_id, row["device_id"]):
                        continue
                    delivered = self._conn.execute(
                        "SELECT task_id FROM ime_deliveries WHERE task_id=?",
                        (task_id,)).fetchone() is not None
                    if delivered:
                        transitions.append((task_id, "cancel_requested", typed, True))
                        if not has_pending_cancel:
                            commands.append(task_id)
                    else:
                        transitions.append((task_id, "cancelled", typed, False))
                if transitions or commands:
                    with self._transaction():
                        for task_id, status, typed, _delivered in transitions:
                            self._execute(
                                "UPDATE tasks SET status=?, typed_character_count=?,"
                                " failure_reason=?, updated_at=? WHERE task_id=?",
                                (status, typed, "cancel sweep", now, task_id))
                            self._execute(
                                "INSERT INTO task_history(task_id, status,"
                                " typed_character_count, reason, created_at)"
                                " VALUES (?, ?, ?, ?, ?)",
                                (task_id, status, typed, "cancel sweep", now))
                        for task_id in commands:
                            self._execute(
                                "INSERT INTO control_commands(task_id, command,"
                                " state, created_at) VALUES (?, 'cancel', 'pending', ?)",
                                (task_id, now))
        if transitions:
            self.wake_waiters()
            self._wake_worker()
        # Fleet 侧 cancel_state 对账（幂等补建/核对；失败不阻断 IME 层取消）
        for task_id, status, _typed, delivered in transitions:
            if "cancel" in self._control_hooks:
                try:
                    row = self._query_one(
                        "SELECT device_id FROM tasks WHERE task_id=?", (task_id,))
                    if row is not None:
                        self._control_hooks["cancel"](task_id, row["device_id"],
                                                      delivered=delivered)
                except Exception:
                    pass  # 工作器对账尽力而为；Fleet 记录保持保守（阻断不放开）
        return len(commands)

    def _pending_needs_cancel(self, task_id: str, ime_device: str) -> bool:
        """pending 任务是否有取消理由（§4.8.6：从持久撤销边界/意图恢复）。

        经 Fleet 注入的 cancel_pending_check 判定：owner 缺失/已撤销、
        settings_revision 落在撤销边界及以下、或许可版本不可解释 → True。
        无该钩子（独立服务）时不动 pending 候选，保持原合同。
        """
        check = self._control_hooks.get("cancel_pending_check")
        if check is None:
            return False
        try:
            return bool(check(task_id, ime_device))
        except Exception:
            # 判定本身不可解释：按须取消处理（保守，阻断不放开）
            return True

    def pending_cancellation_count(self) -> int:
        """状态 API 投影：待取消数取自实际记录（cancel_requested + 待确认 cancel）。"""
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='cancel_requested'")
        n = int(row["n"]) if row else 0
        row2 = self._query_one(
            "SELECT COUNT(*) AS n FROM control_commands WHERE command='cancel'"
            " AND state='pending'")
        return max(n, int(row2["n"]) if row2 else 0)

    # ---- 取消控制长轮询 / 确认 ----
    async def next_control(self, task_id: str, device_id: str, wait_seconds: int) -> dict | None:
        self._bind_loop()
        _require_text(task_id, "taskId")
        _require_text(device_id, "deviceId")
        deadline = asyncio.get_running_loop().time() + max(0.0, float(wait_seconds))
        async with self._condition:
            while True:
                row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
                if row is None:
                    raise ScreenImeApiError(404, "task_not_found", "Task not found")
                if self._task_view(row)["deviceId"] != device_id:
                    raise ScreenImeApiError(409, "device_mismatch",
                                            "Task belongs to a different device")
                cmd = self._query_one(
                    "SELECT * FROM control_commands WHERE task_id=? AND state='pending'"
                    " ORDER BY id ASC LIMIT 1", (task_id,))
                if cmd is not None:
                    return {"commandId": cmd["id"], "taskId": cmd["task_id"],
                            "command": cmd["command"], "createdAt": cmd["created_at"]}
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None

    async def acknowledge_control(self, task_id: str, request: dict) -> dict:
        self._bind_loop()
        if (not isinstance(request, dict) or request.get("commandId") is None):
            raise ScreenImeApiError(400, "invalid_request", "deviceId and commandId are required")
        device_id = _require_text(request.get("deviceId"), "deviceId")
        command_id = request["commandId"]
        async with self._condition:
            # 同步短临界区（cancel_mutex 与工作器扫描互斥；内无 await，不跨网络）
            with self._cancel_mutex:
                return self._acknowledge_locked(task_id, device_id, command_id,
                                                request)

    def _acknowledge_locked(self, task_id: str, device_id: str,
                            command_id, request: dict) -> dict:
        """ack 核心：条件锁与 cancel_mutex 已持有（同步、单事务提交）。"""
        row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if row is None:
            raise ScreenImeApiError(404, "task_not_found", "Task not found")
        task = self._task_view(row)
        if task["deviceId"] != device_id:
            # 原任务/原手机身份核对：新手机冒认旧任务在此拒绝（变更前）
            raise ScreenImeApiError(409, "device_mismatch",
                                    "Task belongs to a different device")
        typed_reported = request.get("typedCharacterCount")
        typed = task["typedCharacterCount"] if typed_reported is None \
            else int(typed_reported)
        if typed < 0 or typed > MAX_CONTENT_LENGTH:
            raise ScreenImeApiError(400, "invalid_typed_count",
                                    "typedCharacterCount is out of range")
        cmd = self._query_one(
            "SELECT command, state FROM control_commands WHERE id=? AND task_id=?",
            (command_id, task_id))
        if cmd is None:
            raise ScreenImeApiError(404, "command_not_found", "Command not found")
        if cmd["command"] != "cancel":
            raise ScreenImeApiError(409, "unsupported_command", "Unsupported command")
        if cmd["state"] == "acknowledged":
            return task  # 幂等重放：返回原结果，不重复推进
        if "ack" in self._control_hooks:
            # §4.8.5：身份核验（原任务/原手机/commandId）在变更前执行；
            # 仅原手机 ack 产生 cancel_state=confirmed；拒绝则 409 且状态不变。
            try:
                self._control_hooks["ack"](
                    task_id, device_id,
                    typed_character_count=(None if typed_reported is None else typed))
            except InputControlError as exc:
                raise ScreenImeApiError(
                    409, getattr(exc, "hook_code", None) or "input_control_rejected",
                    str(exc)) from exc
        now = now_iso()
        with self._transaction():
            # ack 的命令确认、任务取消与历史一次 IME DB 事务提交（§4.8.5-7）
            self._execute(
                "UPDATE control_commands SET state='acknowledged', acknowledged_at=?"
                " WHERE id=? AND task_id=?", (now, command_id, task_id))
            if task["status"] == "cancel_requested":
                self._execute(
                    "UPDATE tasks SET status=?, typed_character_count=?,"
                    " failure_reason=?, updated_at=? WHERE task_id=?",
                    ("cancelled", typed, "cancel acknowledged", now, task_id))
                self._execute(
                    "INSERT INTO task_history(task_id, status, typed_character_count,"
                    " reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, "cancelled", typed, "cancel acknowledged", now))
        self._notify()
        self._wake_worker()
        row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return self._task_view(row)

    # ---- 心跳与设备列表 ----
    def touch(self, device_id: str) -> None:
        """设备级轮询视为隐式心跳：只刷新 last_seen_at（10 秒节流）。

        手机停在校准页时不再发心跳，但每 750ms 轮询
        GET /api/v1/devices/{id}/calibration——这本身就是在线证据
        （用户拍板 2026-09-03：调试台刷新页面应自动看到在线设备，
        不需要手机端点「测试连接」）。新设备首次出现时以 unknown 落库，
        不覆盖已有设备的 app_version/keyboard_active。
        """
        _require_text(device_id, "deviceId")
        row = self._query_one(
            "SELECT last_seen_at FROM devices WHERE device_id=?", (device_id,))
        now_dt = datetime.now(timezone.utc)
        if row is not None:
            seen = parse_iso(row["last_seen_at"])
            if seen is not None and now_dt - seen < timedelta(seconds=10):
                return
        self._write(
            "INSERT INTO devices(device_id, app_version, keyboard_active,"
            " current_task_id, last_seen_at) VALUES (?, 'unknown', 0, NULL, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (device_id, now_iso()))

    async def heartbeat(self, device_id: str, request: dict) -> dict:
        """真实心跳（§4.9.3）。

        - 完整 v1 心跳携带 inputTaskControlVersion（正整数）时，同事务保存
          实际版本、服务端接收时间与本次服务启动 UUID；
        - 合法心跳**缺字段**（键不存在）→ 三列全部置 NULL：旧能力立即失效，
          不伪造新报告时间；
        - 显式 null / 非法类型（字符串/浮点/布尔）→ 422 拒绝，本次不写任何能力；
        - touch/普通轮询/appVersion 不补值、不续期（见 touch）。
        """
        self._bind_loop()
        _require_text(device_id, "deviceId")
        if not isinstance(request, dict):
            raise ScreenImeApiError(400, "invalid_request", "Request body is required")
        app_version = _blank_to_none(request.get("appVersion")) or "unknown"
        keyboard_active = request.get("keyboardActive") is True
        now = now_iso()
        current_task = _blank_to_none(request.get("currentTaskId"))
        if "inputTaskControlVersion" in request:
            raw_version = request["inputTaskControlVersion"]
            if raw_version is None or isinstance(raw_version, bool) \
                    or not isinstance(raw_version, int) or raw_version < 1:
                raise ScreenImeApiError(
                    422, "invalid_input_task_control_version",
                    "inputTaskControlVersion must be a positive integer")
            control_version = raw_version
        else:
            # 合法旧版心跳：不带版本字段 → 三列全部 NULL（旧能力立即失效）
            control_version = None
        with self._transaction():
            self._execute(
                "INSERT INTO devices(device_id, app_version, keyboard_active, current_task_id,"
                " last_seen_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(device_id) DO UPDATE SET app_version=excluded.app_version,"
                " keyboard_active=excluded.keyboard_active,"
                " current_task_id=excluded.current_task_id, last_seen_at=excluded.last_seen_at",
                (device_id, app_version, 1 if keyboard_active else 0, current_task, now))
            # 同事务写三列：版本 1 才构成能力；非 1 正整数如实记录但不放行
            self._execute(
                "UPDATE devices SET input_task_control_version=?,"
                " input_control_reported_at=?, input_control_runtime_instance=?"
                " WHERE device_id=?",
                (control_version,
                 now if control_version is not None else None,
                 self.runtime_instance if control_version is not None else None,
                 device_id))
        return {"deviceId": device_id, "appVersion": app_version,
                "keyboardActive": keyboard_active, "currentTaskId": current_task,
                "lastSeenAt": now, "online": True,
                "inputTaskControlVersion": control_version}

    def input_control_capability(self, device_id: str) -> dict:
        """FIX-15 资格门（只读）：当前绑定手机输入控制能力是否可放行。

        放行条件=完整版本 1 心跳、三列非空、同本次服务启动 UUID、
        **能力报告年龄≤45 秒**（按 input_control_reported_at 计算——
        last_seen_at 会被 touch/普通轮询刷新，不能作为能力续期依据；
        touch 不续期）。重启后必须等新心跳，不从 appVersion/旧在线时间补值。
        """
        row = self._query_one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        if row is None:
            return {"compatible": False, "reason_code": "heartbeat_missing",
                    "control_version": None}
        version = row["input_task_control_version"]
        if version is None:
            return {"compatible": False, "reason_code": "control_version_missing",
                    "control_version": None}
        if version != 1:
            return {"compatible": False, "reason_code": "control_version_unsupported",
                    "control_version": version}
        reported_at = row["input_control_reported_at"]
        if not reported_at or not row["input_control_runtime_instance"]:
            return {"compatible": False, "reason_code": "control_report_incomplete",
                    "control_version": version}
        try:
            age = (datetime.now(timezone.utc) - parse_iso(reported_at)).total_seconds()
        except (ValueError, TypeError):
            return {"compatible": False, "reason_code": "control_report_incomplete",
                    "control_version": version}
        if age > ONLINE_WINDOW.total_seconds():
            return {"compatible": False, "reason_code": "control_report_expired",
                    "control_version": version}
        if row["input_control_runtime_instance"] != self.runtime_instance:
            return {"compatible": False, "reason_code": "runtime_instance_mismatch",
                    "control_version": version}
        return {"compatible": True, "reason_code": None,
                "control_version": version,
                "reported_at": reported_at,
                "age_seconds": max(0.0, age)}

    def list_devices(self) -> list[dict]:
        rows = self._query_all("SELECT * FROM devices ORDER BY last_seen_at DESC, device_id ASC")
        threshold = datetime.now(timezone.utc) - ONLINE_WINDOW
        devices = []
        for row in rows:
            capability = self.input_control_capability(row["device_id"])
            devices.append({
                "deviceId": row["device_id"], "appVersion": row["app_version"],
                "keyboardActive": row["keyboard_active"] != 0,
                "currentTaskId": row["current_task_id"], "lastSeenAt": row["last_seen_at"],
                "online": parse_iso(row["last_seen_at"]) >= threshold,
                # FIX-15：输入控制能力投影（§4.9.3）——开关可保存开启，
                # 不兼容时执行资格 false（blocking ime_control_incompatible）。
                "inputControlVersion": capability.get("control_version"),
                "inputControlCompatible": capability["compatible"],
                "inputControlIncompatibleReason": capability.get("reason_code"),
            })
        return devices

    # ---- 查询 ----
    def find(self, task_id: str) -> dict | None:
        row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return self._task_view(row) if row else None

    def detail(self, task_id: str) -> dict:
        row = self._query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if row is None:
            raise ScreenImeApiError(404, "task_not_found", "Task not found")
        history = []
        for h in self._query_all(
                "SELECT * FROM task_history WHERE task_id=? ORDER BY id ASC", (task_id,)):
            history.append({
                "id": h["id"], "taskId": h["task_id"], "status": h["status"],
                "typedCharacterCount": h["typed_character_count"], "reason": h["reason"],
                "createdAt": h["created_at"],
            })
        return {"task": self._task_view(row), "history": history}

    def list(self, *, device_id=None, status=None, target_user=None, text_query=None,
             limit=50, cursor=None, created_from=None, created_to=None) -> dict:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if device_id:
            sql += " AND device_id=?"; params.append(device_id)
        if status:
            if status not in TASK_STATUSES:
                raise ScreenImeApiError(400, "invalid_status", "Unsupported task status")
            sql += " AND status=?"; params.append(status)
        if target_user:
            sql += " AND target_user=?"; params.append(target_user)
        if created_from:
            sql += " AND created_at>=?"; params.append(created_from)
        if created_to:
            sql += " AND created_at<=?"; params.append(created_to)
        if text_query:
            escaped = text_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += (" AND (task_id LIKE ? ESCAPE '\\' OR target_user LIKE ? ESCAPE '\\'"
                    " OR content LIKE ? ESCAPE '\\')")
            params += [f"%{escaped}%"] * 3
        if cursor:
            sql += " AND (created_at < ? OR (created_at = ? AND task_id < ?))"
            params += [cursor[0], cursor[0], cursor[1]]
        sql += " ORDER BY created_at DESC, task_id DESC LIMIT ?"
        params.append(min(max(int(limit), 1), MAX_PAGE_SIZE) + 1)
        rows = self._query_all(sql, tuple(params))
        tasks = [self._task_view(row) for row in rows]
        next_cursor = None
        if len(tasks) > min(max(int(limit), 1), MAX_PAGE_SIZE):
            tasks = tasks[: min(max(int(limit), 1), MAX_PAGE_SIZE)]
            last = tasks[-1]
            raw = f"{last['createdAt']}\n{last['taskId']}".encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return {"tasks": tasks, "nextCursor": next_cursor}

    def decode_cursor(self, cursor: str | None) -> tuple[str, str] | None:
        if not cursor:
            return None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            created_at, task_id = decoded.split("\n", 1)
            parse_iso(created_at)
            return created_at, task_id
        except Exception:
            raise ScreenImeApiError(400, "invalid_cursor", "cursor is invalid")

    def database_healthy(self) -> bool:
        try:
            row = self._query_one("SELECT 1")
            return row is not None and row[0] == 1
        except sqlite3.Error:
            return False

    def clear_all(self) -> None:
        with self._thread_lock:
            self._conn.execute("DELETE FROM control_commands")
            self._conn.execute("DELETE FROM task_history")
            self._conn.execute("DELETE FROM devices")
            self._conn.execute("DELETE FROM tasks")
            self._conn.commit()


class ScreenImeService:
    """输入法服务门面：任务仓储 + 校准协调器 + 校准映射注册表。"""

    def set_control_hooks(self, hooks: dict) -> None:
        """Fleet 挂载注入；hooks 中的 `attach_runtime` 回调先接走 Runtime 引用
        （FIX-02 装配段：Runtime 持有服务，并接取消工作器通知）。"""
        hooks = dict(hooks or {})
        attach = hooks.pop("attach_runtime", None)
        if attach is not None:
            attach(self)
        self.tasks.set_control_hooks(hooks)

    def __init__(self, db_path: Path, mappings_path: Path | None = None,
                 control_hooks: dict | None = None):
        from adapters.screen_ime.calibration import CalibrationCoordinator
        from adapters.screen_ime.geometry import MappingRegistry

        self._closed = False
        self.tasks = ImeTaskRepository(Path(db_path))
        if control_hooks:
            # 构造期注入 Fleet 钩子：走同一挂载路径（attach + 标记 + 取消工作器）
            self.set_control_hooks(dict(control_hooks))
        self.calibrations = CalibrationCoordinator()
        db_dir = Path(db_path).parent
        self.mappings = MappingRegistry(Path(mappings_path) if mappings_path
                                        else db_dir / "screen_ime_mappings.json")
        # 摄像头定位存档（可选：numpy 缺失时置 None，相关接口返回 503）
        try:
            from adapters.screen_ime.camera_calibration import CameraCalibrationStore

            self.camera_store = CameraCalibrationStore(
                db_dir / "screen_ime_camera.json")
        except Exception:
            self.camera_store = None
        # 手动校准触点（机械臂逐次点击屏幕时校准页自动回报，按顺序积累；进程内存态）
        self.manual_touches: dict[str, list] = {}
        # 手势采样协调器（gesture-capture.v1；默认挂载，测试可整体替换）
        from adapters.screen_ime.gesture_capture import GestureCaptureCoordinator
        from pathlib import Path as _Path

        # P1-4（2026-09-30 审查）：证据根与 db 同根（data/）。旧启发式只在
        # 父目录名为 devices 时生效，生产 db=data/screen_ime_tasks.db 落空 →
        # 证据漂到 adapters/data/，与 run 状态（data/devices/）撕裂。
        self.gesture_capture = GestureCaptureCoordinator(
            data_root=_Path(db_path).parent,
            legacy_armed_provider=self.calibration_armed)

    def calibration_armed(self, device_id: str) -> bool:
        """旧校准是否存在活动布防（只读；供手势互斥检查）。"""
        session_id = self.calibrations._active_by_device.get(device_id)
        if not session_id:
            return False
        session = self.calibrations._sessions.get(session_id)
        return bool(session and session.get("status") == "active"
                    and session.get("armed"))

    def close(self) -> None:
        """关机序（§4.8.6-7）：先停取消工作器（停接纳/交付由 Runtime/HTTP 停止
        保证），再关仓储。可重入（Runtime 与 API 装配都可能先关）。"""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        gesture = getattr(self, "gesture_capture", None)
        if gesture is not None:
            try:
                gesture.close()
            except Exception:
                pass
        self.tasks.close()


# ---- 进程级默认实例（Agent 进程内嵌；插件与路由共用） ----
_default_service: ScreenImeService | None = None


def get_default_service() -> ScreenImeService:
    """返回进程内默认服务；首次调用时按平台配置惰性创建。"""
    global _default_service
    if _default_service is None:
        from runtime.config import load_platform_config

        config = load_platform_config()
        _default_service = ScreenImeService(config.screen_ime_db_path,
                                            config.screen_ime_mappings_path)
    return _default_service


def set_default_service(service: ScreenImeService | None) -> None:
    """测试/装配用：替换或清空默认服务（替换时关闭旧实例的数据库连接）。"""
    global _default_service
    if _default_service is not None and service is not None and _default_service is not service:
        try:
            _default_service.close()
        except Exception:
            pass
    _default_service = service
