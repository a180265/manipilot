"""全局急停控制器。

- 急停后平台立即停止领取新任务，运行中任务走停止/清理/取证路径。
- 机械臂驱动侧急停锁定，复位必须由人工确认（现场复位策略待项目方确认）。
- 急停触发时通知 WX 暂停 Outbox 和跟进调度。
- 物理暂停、恢复、隔离、归位和急停只有 Arm Fleet 后端可写。
"""
from __future__ import annotations

from runtime.util import iso, utc_now


class EstopController:
    def __init__(self, store, audit, events, devices, reporter=None):
        self._store = store
        self._audit = audit
        self._events = events
        self._devices = devices
        self._reporter = reporter

    @property
    def active(self) -> bool:
        return self._store.kv_get("estop_active") == "1"

    def trigger(self, operator_id: str, reason: str, correlation_id: str | None = None) -> dict:
        if self.active:
            return {"estop_active": True, "already_active": True}
        self._store.kv_set("estop_active", "1")
        self._store.kv_set("estop_triggered_at", iso(utc_now()))
        self._store.kv_set("estop_reason", reason)
        # 驱动侧急停 + 设备暂停
        for device in self._devices.all():
            if device["status"] in ("quarantined",):
                continue
            try:
                arm = self._devices.driver(device["device_id"])
                import asyncio

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._estop_device(device["device_id"]))
                else:
                    loop.run_until_complete(self._estop_device(device["device_id"]))
            except Exception:
                pass
            if device["status"] not in ("offline", "quarantined"):
                self._devices.pause(device["device_id"], f"全局急停：{reason}")
        self._devices._alert(None, "critical", f"全局急停已触发：{reason}", action="emergency_stop")
        self._events.publish("system.emergency_stopped", {"reason": reason, "operator_id": operator_id})
        if self._reporter is not None:
            import asyncio

            # §7.6（P3-6）：急停回报先落本地回调队列再投递（同一 payload、
            # 同一 callback_id=emergency-stop:{occurred_at}）；事件循环立即退出
            # 时由后台 flush 兜底重投。未配置回调队列（None）时保持
            # fire-and-forget 降级。投递成功由 send_emergency_stop 内的
            # mark_succeeded 标记，不阻塞急停本地生效。
            payload = self._reporter.persist_emergency_stop_callback(reason)
            coro = None
            try:
                coro = (self._reporter.send_emergency_stop(reason, payload=payload)
                        if payload is not None
                        else self._reporter.send_emergency_stop(reason))
                asyncio.create_task(coro)
            except Exception:
                # 事件循环不可用/立即退出：关闭未调度协程；已持久化的回调
                # 由后台 flush 兜底重投，不阻塞急停本地生效。
                if coro is not None:
                    coro.close()
        return {"estop_active": True, "already_active": False}

    async def _estop_device(self, device_id: str) -> None:
        try:
            await self._devices.driver(device_id).estop()
            device = self._devices.get(device_id)
            device["estop_latched"] = True
            self._devices._store.save_device(device)
        except Exception:
            pass

    async def reset(self, operator_id: str, reason: str) -> dict:
        if not self.active:
            return {"estop_active": False, "already_reset": True}
        self._store.kv_set("estop_active", "0")
        self._store.kv_set("estop_reset_at", iso(utc_now()))
        self._store.kv_set("estop_reset_by", operator_id)
        # 解除驱动侧急停锁定（人工确认后），并对因急停暂停的设备做归位检查后恢复
        for device in self._devices.all():
            device_id = device["device_id"]
            try:
                arm = self._devices.driver(device_id)
                if hasattr(arm, "reset_estop"):
                    arm.reset_estop(operator_id)
                if hasattr(arm, "connect") and not getattr(arm, "connected", True):
                    await arm.connect()
                d = self._devices.get(device_id)
                d["estop_latched"] = False
                self._devices._store.save_device(d)
            except Exception:
                pass
        for device in self._devices.all():
            device_id = device["device_id"]
            fresh = self._devices.get(device_id)
            if fresh["status"] == "paused" and str(fresh.get("pause_reason", "")).startswith("全局急停"):
                try:
                    home = await self._devices.home_check(device_id)
                except Exception:
                    home = {"home_ok": False}
                if home.get("home_ok"):
                    self._devices.resume(device_id, f"急停复位，归位检查通过：{reason}")
                else:
                    self._devices.pause(device_id, "急停复位但归位未确认，需人工处理")
        self._audit.record("emergency_stop_reset", operator_id=operator_id, reason=reason)
        return {"estop_active": False, "already_reset": False}
