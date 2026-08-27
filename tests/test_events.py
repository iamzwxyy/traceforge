from __future__ import annotations

import pytest

from traceforge.events import EventBroker
from traceforge.models import EventType, RunRecord
from traceforge.storage import Storage


@pytest.mark.asyncio
async def test_slow_subscriber_cannot_backpressure_persisted_execution(
    settings, storage: Storage
) -> None:
    run = RunRecord(id="slow-client", task="Keep working", workspace=str(settings.workspace))
    storage.create_run(run)
    broker = EventBroker(storage)
    queue = broker.subscribe(run.id)

    for index in range(501):
        await broker.emit(run.id, EventType.MESSAGE, {"index": index})

    assert queue.qsize() == 500
    queued = []
    while not queue.empty():
        queued.append(queue.get_nowait())
    assert queued[0].seq == 2
    assert queued[-1].seq == 501
    assert [event.seq for event in storage.get_events(run.id)] == list(range(1, 502))
