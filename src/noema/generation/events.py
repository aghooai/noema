"""`EventQueue`: the run's unbounded, single-consumer event stream (sync `put`, async iteration)."""

from __future__ import annotations

import asyncio
from collections import deque

from noema.core.protocols.generation import GenEvent


class EventQueue:
    """Events in order; `put` never blocks (the producers are sync state transitions). Iterating ends once the
    queue is closed and drained.

    The consumer waits on one `asyncio.Event` for the queue's life (cleared before each wait), not on a new
    `anyio.Event` per wait: it wakes once per generation.delta, and anyio's event construction (backend lookup
    through sniffio, a wrapper around a fresh asyncio.Event) was ~1% of the api's CPU at 50 streams on this path
    alone (E sprint B). Every Noema process and test runs on asyncio; anyio cancel scopes cancel the waiting task
    as before."""

    __slots__ = ("_closed", "_event", "_items")

    def __init__(self) -> None:
        self._items: deque[GenEvent] = deque()
        self._closed = False
        self._event: asyncio.Event | None = None  # created by the consumer's first wait (inside the loop)

    def put(self, event: GenEvent) -> None:
        if self._closed:
            raise RuntimeError("event after the run ended")
        self._items.append(event)
        if self._event is not None:
            self._event.set()

    def close(self) -> None:
        self._closed = True
        if self._event is not None:
            self._event.set()

    def __aiter__(self) -> EventQueue:
        return self

    async def __anext__(self) -> GenEvent:
        while not self._items:
            if self._closed:
                raise StopAsyncIteration
            event = self._event
            if event is None:
                event = self._event = asyncio.Event()
            event.clear()  # no await between the check above and the wait: a put() after it sets the event
            await event.wait()
        return self._items.popleft()
