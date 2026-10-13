"""`RedisEventMirror` batching (C2b perf smoke): the writer waits `batch_s` after a wake, so the frames of several
turns appended meanwhile share one pipeline, and a running turn's key gets the orphan TTL once (its first batch),
not once per batch; the seal's EXPIRE ends that, and a broken turn forgets it."""

from __future__ import annotations

from typing import Any

import anyio
from tests.fakes.clock import FakeClock
from tests.unit.streaming.test_mirror_queue import KEYS, FakeRedis, delta

from noema.streaming import RedisEventMirror
from noema.streaming.mirror import SCRIPT_FRAMES


def _ops(redis: FakeRedis, kind: str) -> list[tuple[Any, ...]]:
    return [op for batch in redis.batches for op in batch if op[0] == kind]


async def test_frames_appended_within_batch_s_share_one_pipeline() -> None:
    clock, redis = FakeClock(), FakeRedis()
    mirror = RedisEventMirror(redis, KEYS, clock, batch_s=0.005)  # type: ignore[arg-type]
    async with anyio.create_task_group() as tg:
        tg.start_soon(mirror.run)
        await clock.settle()
        mirror.frame("turn_a", delta(1))  # wakes the writer, which now gathers for 5 ms
        await clock.advance(0.002)
        mirror.frame("turn_b", delta(1))
        mirror.frame("turn_a", delta(2))
        await clock.advance(0.002)
        assert redis.batches == []
        await clock.advance(0.002)
        assert len(redis.batches) == 1
        tg.cancel_scope.cancel()
    assert len(_ops(redis, "xadd")) == 3
    assert sorted(op[1] for op in _ops(redis, "expire")) == [KEYS.turn_events("turn_a"), KEYS.turn_events("turn_b")]


async def test_the_orphan_ttl_is_set_once_per_turn_and_the_seal_sets_the_window() -> None:
    clock, redis = FakeClock(), FakeRedis()
    mirror = RedisEventMirror(redis, KEYS, clock, orphan_ttl_s=900)  # type: ignore[arg-type]
    mirror.frame("turn_a", delta(1))
    assert await mirror.flush()
    mirror.frame("turn_a", delta(2))
    assert await mirror.flush()
    mirror.frame("turn_a", delta(3))
    mirror.seal("turn_a", 300)
    assert await mirror.flush()
    assert [op[2] for op in _ops(redis, "expire")] == [900, 300]
    assert len(_ops(redis, "xadd")) == 3
    mirror.frame("turn_a", delta(4))  # a turn id seen again after its seal gets the orphan TTL again
    assert await mirror.flush()
    assert [op[2] for op in _ops(redis, "expire")] == [900, 300, 900]


async def test_a_broken_turn_forgets_its_orphan_ttl() -> None:
    clock, redis = FakeClock(), FakeRedis()
    mirror = RedisEventMirror(redis, KEYS, clock, orphan_ttl_s=900)  # type: ignore[arg-type]
    mirror.frame("turn_a", delta(1))
    assert await mirror.flush()
    redis.down = True
    mirror.frame("turn_a", delta(2))
    assert await mirror.flush() is False  # the failed batch breaks the turn
    assert "turn_a" not in mirror._orphan_ttl_set


class _DownAfter(FakeRedis):
    """Redis that goes down after `calls` script calls (a failure in the middle of a split batch)."""

    def __init__(self, calls: int) -> None:
        super().__init__()
        self.calls = calls

    def register_script(self, script: str) -> Any:
        inner = super().register_script(script)

        async def call(*, keys: list[str], args: list[Any]) -> list[int]:
            self.down = self.calls <= len(self.batches)
            return await inner(keys=keys, args=args)

        return call


async def test_a_large_batch_is_split_into_whole_turns_per_script_call() -> None:
    redis = FakeRedis()
    mirror = RedisEventMirror(redis, KEYS, FakeClock(), max_pending=10_000)  # type: ignore[arg-type]
    for turn, n in (("t1", SCRIPT_FRAMES - 1), ("t2", 3), ("t3", 2)):
        for i in range(1, n + 1):
            mirror.frame(turn, delta(i))
    assert await mirror.flush()
    first, second = redis.batches  # t1 + t2 reach SCRIPT_FRAMES: t3 starts the second call
    assert {op[1] for op in first} == {KEYS.turn_events("t1"), KEYS.turn_events("t2")}
    assert [op[2] for op in second if op[0] == "xadd"] == ["1-0", "2-0"]
    assert len(_ops(redis, "xadd")) == SCRIPT_FRAMES + 4


async def test_a_failure_in_a_later_call_breaks_only_the_turns_not_written() -> None:
    redis = _DownAfter(calls=1)
    mirror = RedisEventMirror(redis, KEYS, FakeClock(), max_pending=10_000)  # type: ignore[arg-type]
    for i in range(1, SCRIPT_FRAMES + 1):
        mirror.frame("t1", delta(i))
    mirror.frame("t2", delta(1))
    assert await mirror.flush() is False
    assert mirror._broken == {"t2"}  # t1's frames were written by the first call


async def test_a_failure_in_a_later_call_forgets_the_orphan_ttl_of_the_turns_sealed_before_it() -> None:
    """A Redis failure in chunk i > 0 of a sealing batch: the turns of the chunks already written had their seal
    written, so they leave `_orphan_ttl_set` (no later batch would ever remove them: an unbounded leak per Redis
    blip during a backlog; Wave E fix)."""
    redis = _DownAfter(calls=2)
    mirror = RedisEventMirror(redis, KEYS, FakeClock(), max_pending=10_000)  # type: ignore[arg-type]
    turns = [f"t{i:04d}" for i in range(SCRIPT_FRAMES + 10)]
    for turn in turns:
        mirror.frame(turn, delta(1))
    assert await mirror.flush()  # two calls: every turn now has the orphan TTL
    assert mirror._orphan_ttl_set == set(turns)
    redis.calls = len(redis.batches) + 1  # the next batch's second call fails
    for turn in turns:
        mirror.frame(turn, delta(2))
        mirror.seal(turn, 300)
    assert await mirror.flush() is False
    assert mirror._orphan_ttl_set == set()
