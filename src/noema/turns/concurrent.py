"""`gather`: run a few independent awaitables of one turn concurrently (derivative reads, per-part negotiation)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Final

import anyio

# At most this many calls of one fan-out at once: the number of a turn's media parts is bounded only by its
# context window, so an unbounded fan-out would open that many DB units of work or S3 reads per turn (Wave E)
GATHER_LIMIT: Final = 8


async def gather(calls: Sequence[Callable[[], Awaitable[None]]]) -> None:
    """Run `calls` concurrently, at most `GATHER_LIMIT` at once; return when all are done (the first failure
    cancels the rest and propagates, as from a task group). One call runs inline, without a task."""
    if not calls:
        return
    if len(calls) == 1:
        await calls[0]()
        return
    limiter = anyio.CapacityLimiter(GATHER_LIMIT)

    async def one(call: Callable[[], Awaitable[None]]) -> None:
        async with limiter:
            await call()

    async with anyio.create_task_group() as tg:
        for call in calls:
            tg.start_soon(one, call)
