"""租约管理：同一设备同一时刻最多一个任务租约。

- 过期任务不得取得租约。
- Mac mini / Agent 重启后所有租约失效（clear_all）。
- 实时画面查看会话不得获取租约。
- S1（第六轮，执行单§3）：检查与写入经线程锁原子化（L49 归属条件）；
  release 返回是否实际释放（L47）；过期回收按持久任务/动作事实恢复设备
  状态，不以超时直接放行；acquire 部分写失败回滚时同时恢复设备镜像。
"""
from __future__ import annotations

import threading

from runtime.errors import ApiError
from runtime.util import iso, new_id, parse_iso, utc_now

TERMINAL_TASK_STATUSES = {"confirmed", "failed", "unknown", "cancelled", "expired"}


class LeaseManager:
    def __init__(self, store, devices):
        self._store = store
        self._devices = devices
        # L49：检查→写入原子化（内存屏障反例下仍单赢家）
        self._lock = threading.RLock()

    def acquire(self, device_id: str, task_id: str, ttl_seconds: int = 300) -> dict:
        with self._lock:
            existing = self._store.get_lease(device_id)
            if existing is not None:
                expires = parse_iso(existing["expires_at"])
                if expires and expires > utc_now():
                    raise ApiError("lease_conflict", f"设备 {device_id} 已被任务 {existing['task_id']} 租用")
                # 过期租约回收：按持久任务/动作事实恢复，不因超时直接放行
                self._reclaim_expired(device_id, existing)
            lease = {
                "lease_id": new_id("lease"),
                "device_id": device_id,
                "task_id": task_id,
                "acquired_at": iso(utc_now()),
                "expires_at": iso(_later(ttl_seconds)),
            }
            self._store.save_lease(lease)
            old_device_lease = None
            try:
                dev = self._devices._devices.get(device_id) if hasattr(
                    self._devices, "_devices") else None
                old_device_lease = (dev or {}).get("lease_id")
                self._devices.set_lease(device_id, lease["lease_id"])
            except Exception:
                # P1（第五轮§6.2）：租约存储与设备登记要么都成、要么都不成——
                # 部分写成功会让设备带着幽灵租约被调度器永久跳过；
                # 回滚同时恢复设备镜像上的旧 lease_id（L47 幽灵设备 lease_id）。
                try:
                    self._store._delete("leases", "device_id", device_id)
                except Exception:
                    pass
                try:
                    self._devices.set_lease(device_id, old_device_lease)
                except Exception:
                    pass
                raise
            return lease

    def _reclaim_expired(self, device_id: str, expired: dict) -> None:
        """回收过期租约时按持久任务事实恢复设备，而不是无条件 ready。"""
        old_task_id = expired.get("task_id")
        try:
            old_task = self._store.get_task(old_task_id) if old_task_id else None
        except Exception:
            old_task = None
        try:
            device = self._devices.get(device_id)
        except Exception:
            device = None
        if not device or device.get("status") != "busy":
            return
        acted = False
        if old_task:
            acted = any(entry.get("is_action")
                        for entry in old_task.get("step_log") or [])
        if old_task is None or old_task.get("status") in TERMINAL_TASK_STATUSES or not acted:
            # 旧任务已终局或从未发生动作：设备可恢复 ready（持有/隔离由
            # _set_status 自身的守卫保留，不强行覆盖）
            try:
                self._devices._set_status(device_id, "ready",
                                           "过期租约回收：旧任务已终局或零动作")
            except Exception:
                pass
        else:
            # 旧任务非终局且已有动作：结果未知，不因超时放行——设备保持 busy，
            # 交由人工对账（audit 留痕）
            try:
                self._devices._alert(
                    device_id, "warning",
                    f"任务 {old_task_id} 租约过期但已有动作且未终局，"
                    "设备保持阻断待人工对账，不自动恢复",
                    task_id=old_task_id, action="task_lease_expired_actions_pending")
            except Exception:
                pass

    def renew(self, device_id: str, lease_id: str, ttl_seconds: int = 300) -> dict:
        with self._lock:
            lease = self._store.get_lease(device_id)
            if not lease or lease["lease_id"] != lease_id:
                raise ApiError("lease_conflict", "租约不存在或不匹配")
            lease["expires_at"] = iso(_later(ttl_seconds))
            self._store.save_lease(lease)
            return lease

    def release(self, device_id: str, lease_id: str) -> bool:
        """释放自己的租约；返回是否实际删除（L47：迟到收尾不冒充成功）。"""
        with self._lock:
            lease = self._store.get_lease(device_id)
            if lease and lease["lease_id"] == lease_id:
                self._store._delete("leases", "device_id", device_id)
                try:
                    self._devices.set_lease(device_id, None)
                except Exception:
                    pass
                return True
            return False

    def active(self, device_id: str) -> dict | None:
        """未过期租约；过期租约返回 None（调度器可经 acquire 走过期回收）。"""
        lease = self._store.get_lease(device_id)
        if lease is None:
            return None
        expires = parse_iso(lease.get("expires_at"))
        if expires and expires > utc_now():
            return lease
        return None

    def get(self, device_id: str) -> dict | None:
        return self._store.get_lease(device_id)

    def clear_all(self) -> int:
        """重启后所有租约失效。"""
        with self._lock:
            return self._store.clear_leases()


def _later(seconds: int):
    from datetime import timedelta

    return utc_now() + timedelta(seconds=seconds)
