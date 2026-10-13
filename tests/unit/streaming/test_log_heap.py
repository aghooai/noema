"""The event log's share of the GC-tracked heap (E3, QL-5): a live log keeps its frames as untracked tuples (an
`SseFrame` instance is tracked), and a sealed log is one tracked object for its resume window (its frames one
untracked tuple, no wake-up Event and waiter deque). The frames read back are equal, and a subscriber waiting at the
seal still gets every frame."""

from __future__ import annotations

import asyncio
import gc

import anyio
import pytest
from tests.fakes.clock import FakeClock

from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue
from noema.streaming import InProcessTurnEventLog, encode_frame

pytestmark = pytest.mark.unit
T = "turn_1"


def _tlog(log: InProcessTurnEventLog) -> object:
    return log._logs[T]


async def test_a_live_log_keeps_no_tracked_frame_objects() -> None:
    log = InProcessTurnEventLog(FakeClock())
    events: list[dict[str, JsonValue]] = [
        {"type": "generation.delta", "attempt_id": "att_1", "seq": i, "text": f"t{i}"} for i in range(50)
    ]
    for e in events:
        log.append(T, e)
    gc.collect()
    frames = _tlog(log).frames  # type: ignore[attr-defined]
    rows = frames.rows()
    assert len(rows) == 50 and not any(gc.is_tracked(row) for row in rows)
    assert not [o for o in gc.get_referents(*rows) if isinstance(o, SseFrame)]
    assert [frames[i] for i in range(50)] == [encode_frame(i + 1, e) for i, e in enumerate(events)]
    assert frames[-1] == encode_frame(50, events[-1]) and frames[1:3] == [
        encode_frame(i, events[i - 1]) for i in (2, 3)
    ]


async def test_a_sealed_log_drops_its_wake_up_and_a_waiting_subscriber_still_ends() -> None:
    clock = FakeClock()
    log = InProcessTurnEventLog(clock)
    log.append(T, {"type": "turn.created", "turn_id": T})
    got: list[SseFrame] = []

    async def follow() -> None:
        got.extend([f async for f in log.subscribe(T)])

    async with anyio.create_task_group() as tg:
        tg.start_soon(follow)
        await clock.advance(0.01)  # settles: the subscriber has read turn.created and waits
        log.append(T, {"type": "generation.delta", "attempt_id": "att_1", "seq": 0, "text": "x"})
        log.seal(T, ttl_s=60)  # sealed before turn.completed: the waiting subscriber ends after what is there
    assert [f.type for f in got] == ["turn.created", "generation.delta"]
    tlog = _tlog(log)
    assert not [o for o in gc.get_referents(tlog) if isinstance(o, asyncio.Event)]
    gc.collect()
    gc.collect()  # the packed tuple is untracked once its inner tuple of types is
    held = [o for o in gc.get_referents(tlog) if gc.is_tracked(o) and not isinstance(o, type)]
    assert held == []  # one tracked object per sealed turn: the log itself
    assert [f.type async for f in log.subscribe(T)] == ["turn.created", "generation.delta"]  # a resume reads it all
