"""WriteBehindOutbox (spec-11 §3, IMPL-022) against a scripted Postgres sink and an in-memory Redis: batches,
rejects and Postgres-down spill to the Redis stream, both down -> memory, the 100k bound (spill, then drop the
oldest), poison isolation, the session_pending release (background, compare-and-delete, backoff, never slowing the
writer while Redis is blackholed), flush timeout, cancellation safety."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

import anyio
import orjson
import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.telemetry import FakeTelemetry

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Database, OutboxRecord
from noema.core.redis_keys import RedisKeys
from noema.ops.outbox import WriteBehindOutbox, decode

from .redis_stub import MemoryRedis

pytestmark = pytest.mark.unit

KEYS = RedisKeys("ns:test:")


class ScriptedCalls:
    def __init__(self, sink: ScriptedSink) -> None:
        self._sink = sink

    async def outbox_apply(self, records: Sequence[OutboxRecord]) -> tuple[str | None, ...]:
        self._sink.calls.append([r.id for r in records])
        if self._sink.hang is not None:
            await self._sink.hang.wait()
        if any(r.id in self._sink.poison for r in records):
            raise TypeError("cannot encode")
        out = tuple(self._sink.reject.get(r.id) for r in records)
        self._sink.applied += [r for r, e in zip(records, out, strict=True) if e is None]
        return out


class ScriptedSink:
    """`Database.system_uow()` only; `down` raises like an unreachable Postgres."""

    def __init__(self) -> None:
        self.down = False
        self.reject: dict[str, str] = {}
        self.poison: set[str] = set()
        self.applied: list[OutboxRecord] = []
        self.calls: list[list[str]] = []
        self.hang: anyio.Event | None = None

    @asynccontextmanager
    async def system_uow(self) -> AsyncIterator[ScriptedCalls]:
        await anyio.lowlevel.checkpoint()
        if self.down:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "The database is temporarily unavailable.")
        yield ScriptedCalls(self)


def rec(i: int, *, kind: str = "usage_line", tenant: str = "t_acme", session: str | None = None) -> OutboxRecord:
    return OutboxRecord(kind, f"r{i}", tenant, {"n": i}, session)  # type: ignore[arg-type]


def make(**kw: Any) -> tuple[WriteBehindOutbox, ScriptedSink, MemoryRedis, FakeTelemetry, FakeClock]:
    sink, redis, telemetry, clock = ScriptedSink(), MemoryRedis(), FakeTelemetry(), FakeClock()
    outbox = WriteBehindOutbox(cast(Database, sink), cast(Any, redis), KEYS, clock, telemetry.instruments, **kw)
    return outbox, sink, redis, telemetry, clock


def ids(records: Sequence[OutboxRecord]) -> list[str]:
    return [r.id for r in records]


def redis_ids(redis: MemoryRedis) -> list[str]:
    return sorted(decode(raw).id for raw in redis.entries())


async def test_batches_of_at_most_batch_size() -> None:
    outbox, sink, _, _, _ = make(batch_size=2)
    for i in range(5):
        outbox.put(rec(i))
    await outbox.flush(10)
    assert sink.calls == [["r0", "r1"], ["r2", "r3"], ["r4"]] and ids(sink.applied) == ["r0", "r1", "r2", "r3", "r4"]


async def test_rejected_records_go_to_their_shard() -> None:
    outbox, sink, redis, _, _ = make()
    sink.reject["r1"] = "seq_conflict"
    for i, tenant in ((0, "t_acme"), (1, "t_other")):
        outbox.put(rec(i, tenant=tenant))
    assert await outbox.write_once() == 2
    assert ids(sink.applied) == ["r0"]
    assert list(redis.streams) == [KEYS.outbox(RedisKeys.outbox_shard("t_other"))]
    entry = orjson.loads(redis.entries()[0])
    assert entry == {"kind": "usage_line", "id": "r1", "tenant_id": "t_other", "payload": {"n": 1}, "session_id": None}


async def test_postgres_down_spills_the_batch_and_backs_off() -> None:
    outbox, sink, redis, _, clock = make()
    sink.down = True
    outbox.put(rec(0))
    await outbox.write_once()
    sink.down = False
    outbox.put(rec(1))
    await outbox.write_once()  # within PG_RETRY_S: straight to Redis, no apply attempt
    assert redis_ids(redis) == ["r0", "r1"] and sink.calls == []
    await clock.advance(1.0)
    outbox.put(rec(2))
    await outbox.write_once()
    assert ids(sink.applied) == ["r2"]


async def test_both_down_keeps_records_in_memory_in_order() -> None:
    outbox, sink, redis, telemetry, clock = make()
    sink.down, redis.down = True, True
    for i in range(3):
        outbox.put(rec(i))
    assert await outbox.write_once() == 0 and outbox.backlog == 3
    assert telemetry.metrics[-1].name == "aghoo_outbox_backlog" and telemetry.metrics[-1].value == 3
    sink.down, redis.down = False, False
    await clock.advance(1.0)  # past the Postgres retry backoff
    await outbox.flush(10)
    assert ids(sink.applied) == ["r0", "r1", "r2"] and outbox.backlog == 0


async def test_bound_spills_to_redis_then_drops_the_oldest() -> None:
    outbox, sink, redis, telemetry, _ = make(max_memory=2)
    for i in range(3):
        outbox.put(rec(i))  # r2 is over the bound: spilled straight to Redis
    await outbox.write_once()
    assert redis_ids(redis) == ["r2"] and ids(sink.applied) == ["r0", "r1"]
    sink.down, redis.down = True, True
    for i in range(3, 6):
        outbox.put(rec(i))
    await outbox.write_once()  # Redis now known down
    outbox.put(rec(6))
    outbox.put(rec(7))  # memory full and both sinks down: r3, r4 dropped (oldest first)
    assert telemetry.counter("aghoo_outbox_dropped_total") == 2.0
    sink.down, redis.down = False, False
    await outbox.flush(10)
    assert "r3" not in ids(sink.applied) and "r4" not in ids(sink.applied)
    assert {"r5", "r6", "r7"} <= set(ids(sink.applied)) | set(redis_ids(redis))


async def test_a_poison_record_does_not_hold_back_its_batch() -> None:
    outbox, sink, redis, _, _ = make()
    sink.poison.add("r1")
    for i in range(3):
        outbox.put(rec(i))
    await outbox.write_once()
    assert ids(sink.applied) == ["r0", "r2"] and redis_ids(redis) == ["r1"]


async def test_turn_finalize_releases_session_pending_in_the_background() -> None:
    outbox, _, redis, _, clock = make()
    await redis.set(KEYS.session_pending("ses_1"), b"r1")  # SETEX ... <turn_id> by the orchestrator
    outbox.put(rec(1, kind="turn_finalize", session="ses_1"))
    outbox.put(rec(2, kind="usage_line", session=None))
    assert await outbox.write_once() == 2  # the write itself never waits for Redis
    assert redis.evals == 0 and outbox.pending_releases.outstanding == {("ses_1", "r1")}
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(outbox.run)
        await clock.sleep(0.1)
        tg.cancel_scope.cancel()
    assert KEYS.session_pending("ses_1") not in redis.values and not outbox.pending_releases.outstanding


async def test_a_late_release_never_deletes_a_later_turns_key() -> None:
    outbox, _, redis, _, _ = make()
    await redis.set(KEYS.session_pending("ses_1"), b"turn_T2")  # T1 was released already; T2 is now pending
    outbox.put(rec(1, kind="turn_finalize", session="ses_1"))  # a replay of T1's finalize (id r1)
    await outbox.flush(5)
    assert redis.values[KEYS.session_pending("ses_1")] == b"turn_T2" and redis.evals == 1
    assert not outbox.pending_releases.outstanding  # done: the key no longer belongs to T1


async def test_a_failed_release_is_retried_with_backoff() -> None:
    outbox, _, redis, _, clock = make()
    releases = outbox.pending_releases
    await redis.set(KEYS.session_pending("ses_9"), b"r9")
    outbox.put(rec(9, kind="turn_finalize", session="ses_9"))
    await outbox.write_once()
    redis.down = True
    t0 = clock.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(releases.run)
        for at, evals in ((0.5, 1), (1.5, 2), (2.5, 2), (3.5, 3), (6.5, 3)):  # tries at 0, 1, 3, then 7 s
            await clock.advance_to(t0 + at)
            assert (redis.evals, bool(releases.outstanding)) == (evals, True), at
        redis.down = False
        await clock.advance_to(t0 + 7.5)
        tg.cancel_scope.cancel()
    assert redis.evals == 4 and not releases.outstanding and KEYS.session_pending("ses_9") not in redis.values


async def test_a_blackholed_redis_never_slows_the_writer() -> None:
    outbox, sink, redis, _, clock = make()
    releases = outbox.pending_releases
    await redis.set(KEYS.session_pending("ses_1"), b"r0")
    redis.hang = anyio.Event()  # every Redis reply is lost
    outbox.put(rec(0, kind="turn_finalize", session="ses_1"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(releases.run)
        assert await outbox.write_once() == 1
        await clock.settle()
        assert redis.evals == 1  # the release is stuck on Redis
        started = clock.monotonic()
        for cycle in range(3):
            for i in range(500):
                outbox.put(rec(1000 * (cycle + 1) + i))
            with anyio.fail_after(5):  # safety net only: the write completes at once
                assert await outbox.write_once() == 500
        assert clock.monotonic() == started and len(sink.applied) == 1501  # no time passed for the writes
        hung, redis.hang = redis.hang, None  # Redis answers again, but the first attempt's reply was lost
        await clock.advance(2.0)  # the 2 s bound cancels the stuck attempt
        assert releases.outstanding == {("ses_1", "r0")} and redis.evals == 1
        await clock.advance(1.0)  # the 1 s backoff, then the retry succeeds
        hung.set()
        assert not releases.outstanding and KEYS.session_pending("ses_1") not in redis.values
        tg.cancel_scope.cancel()


async def test_flush_logs_what_is_left_after_the_timeout(caplog: pytest.LogCaptureFixture) -> None:
    outbox, sink, redis, telemetry, clock = make()
    sink.down, redis.down = True, True
    outbox.put(rec(0))
    async with clock.autojump():
        await outbox.flush(2)
    assert outbox.backlog == 0 and telemetry.counter("aghoo_outbox_dropped_total") == 1.0
    assert any("lost at shutdown" in r.message for r in caplog.records)


async def test_a_cancelled_write_keeps_its_batch() -> None:
    outbox, sink, _, _, clock = make()
    sink.hang = anyio.Event()
    outbox.put(rec(0))
    async with anyio.create_task_group() as tg:
        tg.start_soon(outbox.write_once)
        await clock.settle()
        assert sink.calls == [["r0"]] and outbox.backlog == 0  # mid-apply
        tg.cancel_scope.cancel()  # e.g. the flush deadline or a shutdown
    assert outbox.backlog == 1  # back in memory, not lost
    sink.hang = None
    assert await outbox.write_once() == 1 and ids(sink.applied) == ["r0"]


async def test_put_after_write_failure_still_never_raises() -> None:
    outbox, sink, redis, _, _ = make(max_memory=1)
    sink.down, redis.down = True, True
    for i in range(50):
        outbox.put(rec(i))
    assert outbox.backlog <= 2


async def test_run_writes_on_the_interval() -> None:
    outbox, sink, _, _, clock = make(interval_s=0.2)
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(outbox.run)
        outbox.put(rec(0))
        await clock.sleep(0.5)
        tg.cancel_scope.cancel()
    assert ids(sink.applied) == ["r0"]
