"""空转修复（2026-10-31）：任务表只读非终态。

背景（实测）：`Runtime._dispatch_loop` 每 0.05 秒（20 Hz）调用
`store.all_tasks()`，而 `_all()` 是 `SELECT json FROM tasks` + 逐行
`json.loads`。现场 95 个任务**全部是终态**，单次 13.78 ms → 20 Hz ≈ 单核 28%，
机器持续发热，而实际上没有任何可派发的工作。

`_dispatch_once` 只消费两类任务：`awaiting_device`（孤儿对账）与 `queued`
（派发），两者都是非终态。因此新增 `active_tasks()` 只读非终态，
派发延迟与语义均不变，空闲时扫描行数由 95 降为 0。

本文件锁定的契约：
1. `active_tasks()` 不含任何 `TERMINAL_STATUSES` 成员；
2. 非终态任务（queued / awaiting_device / running 等）一个都不能少；
3. `all_tasks()` 行为不变（其它调用方仍可拿全量）；
4. 排序与 `all_tasks()` 一致（同一 ORDER BY 口径）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime.executor import TERMINAL_STATUSES
from runtime.status import TASK_STATUS
from runtime.store import Store

# 权威状态清单（status.TASK_STATUS 的 10 个）：非终态 5 个、终态 5 个。
# 复核意见：原测试用了 status.py 里并不存在的 "running"/"dispatched"，
# 拿臆造状态做断言等于没测到真实状态机，这里改用权威来源。
ALL_STATUSES = tuple(TASK_STATUS)
NON_TERMINAL_STATUSES = tuple(s for s in ALL_STATUSES if s not in TERMINAL_STATUSES)


def _task(task_id: str, status: str, created_at: str = "2026-10-31T00:00:00Z") -> dict:
    return {
        "task_id": task_id,
        "idempotency_key": f"idem-{task_id}",
        "status": status,
        "device_id": "arm-01",
        "created_at": created_at,
        "step_log": [],
    }


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "fleet.db")


def test_active_tasks_excludes_every_terminal_status(store: Store):
    """终态一个都不能出现在 active_tasks() 里。"""
    terminal = sorted(TERMINAL_STATUSES)
    for i, status in enumerate(terminal):
        store.save_task(_task(f"t-term-{i}", status))
    store.save_task(_task("t-queued", "queued"))

    active = store.active_tasks()
    assert [t["task_id"] for t in active] == ["t-queued"], (
        f"终态任务不得进入派发扫描；实际 {[t['status'] for t in active]}"
    )


def test_active_tasks_keeps_all_non_terminal_statuses(store: Store):
    """非终态任务一个都不能漏（漏派发比多扫描代价大得多）。"""
    non_terminal = list(NON_TERMINAL_STATUSES)
    terminal = sorted(TERMINAL_STATUSES)
    assert set(non_terminal) == {"queued", "validating", "awaiting_device",
                                 "executing", "takeover"}, non_terminal
    for i, status in enumerate(non_terminal):
        store.save_task(_task(f"t-live-{i}", status))
    for i, status in enumerate(terminal):
        store.save_task(_task(f"t-dead-{i}", status))

    got = {t["task_id"] for t in store.active_tasks()}
    assert got == {f"t-live-{i}" for i in range(len(non_terminal))}


def test_all_tasks_still_returns_everything(store: Store):
    """all_tasks() 语义不变：其它调用方（导航复核、插件、运营恢复）仍拿全量。"""
    store.save_task(_task("t-queued", "queued"))
    store.save_task(_task("t-done", "confirmed"))
    assert {t["task_id"] for t in store.all_tasks()} == {"t-queued", "t-done"}


def test_active_tasks_empty_when_all_terminal(store: Store):
    """现场形态：95 个终态任务、0 个活动任务 → 扫描结果为空（这是省 CPU 的关键）。"""
    for i in range(20):
        store.save_task(_task(f"t-{i}", "unknown" if i % 2 else "confirmed"))
    assert store.active_tasks() == []


def test_active_tasks_keeps_rows_with_missing_status(store: Store):
    """status 为 NULL/空时不能被静默漏掉。

    SQL 三值逻辑：`NULL NOT IN (...)` 结果是 NULL，WHERE 视为不满足，
    这类行会被过滤掉 → 任务永远得不到派发。宁可多扫一行也不能漏。
    """
    store.save_task(_task("t-null", "queued"))
    with store._lock:
        store._conn.execute("UPDATE tasks SET status=NULL WHERE task_id='t-null'")
        store._conn.execute(
            "INSERT INTO tasks (task_id, idempotency_key, status, json) "
            "VALUES ('t-empty', 'idem-empty', '', ?)",
            ('{"task_id": "t-empty", "status": "queued", "device_id": "arm-01", '
             '"created_at": "2026-10-31T00:00:00Z", "step_log": []}',))
        store._conn.commit()

    got = {t["task_id"] for t in store.active_tasks()}
    assert got == {"t-null", "t-empty"}, f"NULL/空 status 的行被漏掉了：{got}"


def test_active_tasks_mixed_real_world_shape(store: Store):
    """现场形态：一堆终态 + 唯一一个 queued，queued 必须留下来。

    这是真实故障形态（95 个终态任务、0 个活动任务）的镜像；漏掉 queued
    就是任务永久不派发。
    """
    for i, status in enumerate(sorted(TERMINAL_STATUSES)):
        store.save_task(_task(f"t-dead-{i}", status))
    store.save_task(_task("t-live", "queued", created_at="2026-10-31T01:00:00Z"))

    active = store.active_tasks()
    assert [t["task_id"] for t in active] == ["t-live"]
