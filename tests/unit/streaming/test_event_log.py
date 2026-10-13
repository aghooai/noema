"""`InProcessTurnEventLog` beyond the conformance suite: several subscribers, resume after an id, seal and expiry
ending live subscribers, the orphan sweep, idle accounting and the in-process-only attach."""

from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
import pytest
from tests.fakes.clock import FakeClock

from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue
from noema.streaming import InProcessTurnEventLog

T = "turn_1"


def delta(i: int) -> dict[str, JsonValue]:
    return {"type": "generation.delta", "attempt_id": "att_1", "seq": i, "text": f"<{i}>"}


async def collect(it: AsyncIterator[SseFrame]) -> list[int]:
    return [f.id async for f in it]


async def test_every_subscriber_sees_every_frame_once() -> None:
    clock = FakeClock()
    log = InProcessTurnEventLog(clock)
    log.append(T, {"type": "turn.created", "turn_id": T})
    got: dict[str, list[int]] = {}

    async def sub(name: str, after: int) -> None:
        got[name] = await collect(log.subscribe(T, after=after))

    async with anyio.create_task_group() as tg:
        tg.start_soon(sub, "a", 0)
        tg.start_soon(sub, "b", 1)
        await clock.settle()
        for i in range(3):
            log.append(T, delta(i))
            await clock.settle()
        log.append(T, {"type": "turn.completed", "turn_id": T})
    assert got == {"a": [1, 2, 3, 4, 5], "b": [2, 3, 4, 5]}


async def test_frames_are_serialized_once_and_shared() -> None:
    clock = FakeClock()
    log = InProcessTurnEventLog(clock)
    log.append(T, {"type": "turn.created", "turn_id": T})
    log.append(T, {"type": "turn.completed", "turn_id": T})
    first = [f async for f in log.subscribe(T)]
    second = [f async for f in log.subscribe(T)]
    # the serialized bytes are shared, never copied; the SseFrame around them is rebuilt per read (E3: a live log
    # keeps untracked tuples, not ~50 GC-tracked frame objects per turn)
    assert first == second
    assert all(a.wire is b.wire and a.payload is b.payload for a, b in zip(first, second, strict=True))


async def test_expiry_ends_a_waiting_subscriber_and_the_log() -> None:
    clock = FakeClock()
    log = InProcessTurnEventLog(clock)
    log.append(T, {"type": "turn.created", "turn_id": T})
    log.append(T, {"type": "turn.completed", "turn_id": T})
    log.seal(T, ttl_s=60)
    log.seal(T, ttl_s=999)  # a second seal changes nothing
    log.seal("turn_unknown", ttl_s=60)  # nothing to seal
    await clock.advance(59)
    assert log.is_local(T)
    await clock.advance(1)
    assert not log.is_local(T) and await collect(log.subscribe(T)) == []
    with pytest.raises(KeyError):
        log._logs[T]  # the expired log was dropped


async def test_append_after_seal_raises() -> None:
    log = InProcessTurnEventLog(FakeClock())
    log.append(T, {"type": "turn.completed", "turn_id": T})
    log.seal(T, ttl_s=60)
    with pytest.raises(RuntimeError, match="after seal"):
        log.append(T, delta(9))


async def test_sweep_drops_orphans_and_ends_their_subscribers() -> None:
    clock = FakeClock()
    log = InProcessTurnEventLog(clock, orphan_after_s=100)
    log.append(T, {"type": "turn.created", "turn_id": T})
    log.append("turn_2", {"type": "turn.created", "turn_id": "turn_2"})
    got: list[int] = []

    async with anyio.create_task_group() as tg:

        async def sub() -> None:
            got.extend(await collect(log.subscribe(T)))

        tg.start_soon(sub)
        await clock.advance(50)
        log.append("turn_2", delta(0))
        await clock.advance(60)
        assert log.sweep() == 1  # turn_1 had no append for 110 s; turn_2 is still alive
    assert got == [1] and not log.is_local(T) and log.is_local("turn_2")


async def test_run_sweeps_every_30_seconds() -> None:
    clock = FakeClock()
    log = InProcessTurnEventLog(clock)
    log.append(T, {"type": "turn.completed", "turn_id": T})
    log.seal(T, ttl_s=40)
    async with anyio.create_task_group() as tg:
        tg.start_soon(log.run)
        await clock.advance(61)
        assert T not in log._logs  # removed by the sweep at 60 s, not only lazily
        tg.cancel_scope.cancel()


async def test_idle_counts_from_the_last_delivery_and_without_mirror_attach_is_none() -> None:
    clock = FakeClock()
    log = InProcessTurnEventLog(clock)
    log.append(T, {"type": "turn.created", "turn_id": T})
    await clock.advance(7)
    assert await log.idle_seconds(T) == 7
    assert await log.idle_seconds("turn_unknown") is None
    assert await log.attach_remote(T) is None


async def test_a_subscriber_cancelled_while_waiting_leaves_the_others_waiting() -> None:
    # every subscriber of a log waits on the log's one asyncio.Event (E sprint B): a cancelled waiter (a client that
    # went away) must not take the wake-up of the others with it, and the log keeps waking new waiters
    clock = FakeClock()
    log = InProcessTurnEventLog(clock)
    log.append(T, {"type": "turn.created", "turn_id": T})
    got: list[int] = []

    async def survivor() -> None:
        async for frame in log.subscribe(T):
            got.append(frame.id)  # noqa: PERF401 (incremental: the test reads `got` mid-stream)

    async def gone(scope: anyio.CancelScope) -> None:
        with scope:
            await collect(log.subscribe(T))

    async with anyio.create_task_group() as tg:
        tg.start_soon(survivor)
        doomed = anyio.CancelScope()
        tg.start_soon(gone, doomed)
        await clock.settle()  # both wait for frame 2
        doomed.cancel()
        await clock.settle()
        log.append(T, delta(0))
        await clock.settle()
        assert got == [1, 2]
        log.append(T, {"type": "turn.completed", "turn_id": T})
    assert got == [1, 2, 3]
    late = await collect(log.subscribe(T))  # a later subscriber of the same log still reads everything
    assert late == [1, 2, 3]
