from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from traceforge.models import EventType, RunEvent
from traceforge.storage import Storage


class EventBroker:
    """Persists every event before exposing it to connected clients."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)

    async def emit(
        self, run_id: str, event_type: EventType, payload: dict[str, Any] | None = None
    ) -> RunEvent:
        event = self.storage.append_event(run_id, event_type, payload)
        for queue in self._subscribers.get(run_id, set()).copy():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Persisted events are authoritative. Never let a background tab or slow
                # client backpressure the agent itself; evict one queued item and let the
                # WebSocket sequence-gap recovery replay it from SQLite.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(event)
        return event

    def subscribe(self, run_id: str) -> asyncio.Queue[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=500)
        self._subscribers[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[RunEvent]) -> None:
        subscribers = self._subscribers.get(run_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(run_id, None)
