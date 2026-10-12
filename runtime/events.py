"""进程内事件总线：支撑 SSE 状态推送（device.updated / task.updated /
supervision.updated / alert.created / system.emergency_stopped）。"""
from __future__ import annotations

import asyncio
from collections import deque

from runtime.util import iso, new_id, utc_now


class EventBus:
    def __init__(self, history_size: int = 500):
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=history_size)

    def publish(self, event_type: str, data: dict) -> dict:
        event = {
            "event_id": new_id("evt"),
            "event_type": event_type,
            "occurred_at": iso(utc_now()),
            "data": data,
        }
        self._history.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def recent_events(self, limit: int = 50) -> list[dict]:
        events = list(self._history)
        return events[-limit:]
