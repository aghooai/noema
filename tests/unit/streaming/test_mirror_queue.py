"""`RedisEventMirror` write queue without Redis (a recording write script): a full queue breaks the appending turn in
O(1) and the next batch drops its queued ops and deletes its key; a broken turn is forgotten once nothing can follow
(its seal), whether it broke before its seal or in the failed batch that carried the seal."""

from __future__ import annotations

from typing import Any

import anyio
from redis.exceptions import ConnectionError as RedisConnectionError
from tests.fakes.clock import FakeClock

from noema.core.redis_keys import RedisKeys
from noema.streaming import RedisEventMirror, encode_frame

KEYS = RedisKeys("ns:unit-mirror:")


class _Script:
    """The mirror's write script (`WRITE_LUA`) as the ops it runs: ("delete", key), ("xadd", key, id) and
    ("expire", key, ttl), one batch per call."""

    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis

    async def __call__(self, *, keys: list[str], args: list[Any]) -> list[int]:
        await anyio.lowlevel.checkpoint()
        if self.redis.down:
            raise RedisConnectionError("down")
        ndel, a = int(args[1]), 2
        ops: list[tuple[Any, ...]] = [("delete", key) for key in keys[:ndel]]
        for key in keys[ndel:]:
            n, ttl = int(args[a]), int(args[a + 1])
            ops += [("xadd", key, f"{args[a + 2 + 3 * j]}-0") for j in range(n)]
            ops += [("expire", key, ttl)] if ttl else []
            a += 2 + 3 * n
        self.redis.batches.append(ops)
        return []


class FakeRedis:
    """Only `register_script()`: what the mirror's writer uses."""

    def __init__(self) -> None:
        self.batches: list[list[tuple[Any, ...]]] = []
        self.down = False

    def register_script(self, script: str) -> _Script:
        return _Script(self)


def mirror_on(redis: FakeRedis, *, max_pending: int = 100) -> RedisEventMirror:
    return RedisEventMirror(redis, KEYS, FakeClock(), max_pending=max_pending)  # type: ignore[arg-type]


def delta(i: int) -> Any:
    return encode_frame(i, {"type": "generation.delta", "attempt_id": "att_1", "seq": i - 1, "text": "x"})


async def test_a_full_queue_breaks_the_appending_turn_lazily() -> None:
    redis = FakeRedis()
    mirror = mirror_on(redis, max_pending=4)
    for i in range(1, 4):
        mirror.frame("t1", delta(i))
    mirror.frame("t2", delta(1))
    mirror.frame("t3", delta(1))  # full: t3 is broken, the queue is left as it is
    mirror.frame("t1", delta(4))  # full: t1 is broken too (its queued frames are dropped by the next batch)
    mirror.frame("t3", delta(2))  # broken: skipped
    assert mirror.pending == 4
    assert await mirror.flush()
    [ops] = redis.batches
    t1, t2, t3 = (KEYS.turn_events(t) for t in ("t1", "t2", "t3"))
    assert ops == [("delete", t1), ("delete", t3), ("xadd", t2, "1-0"), ("expire", t2, 3600)]
    assert mirror.pending == 0


async def test_a_turn_broken_before_its_seal_is_forgotten_by_the_next_batch() -> None:
    redis = FakeRedis()
    mirror = mirror_on(redis, max_pending=2)
    mirror.frame("t1", delta(1))
    mirror.frame("t1", delta(2))
    mirror.frame("t1", delta(3))  # full: broken while its frames are still queued
    mirror.seal("t1", 300)  # queues nothing for a broken turn
    await mirror.drain()
    assert redis.batches == [[("delete", KEYS.turn_events("t1"))]]
    assert not mirror._broken and not mirror._to_delete


async def test_a_turn_broken_by_the_failed_batch_that_carried_its_seal_is_forgotten() -> None:
    redis = FakeRedis()
    mirror = mirror_on(redis)
    redis.down = True
    mirror.frame("t1", delta(1))
    mirror.seal("t1", 300)
    mirror.frame("t2", delta(1))
    assert await mirror.flush() is False
    assert mirror._broken == {"t2"}  # t2 may still append; t1 is sealed, nothing can follow
    assert mirror._to_delete == {"t1", "t2"}
    redis.down = False
    mirror.frame("t2", delta(2))  # skipped: broken
    mirror.seal("t2", 300)
    await mirror.drain()
    assert redis.batches == [[("delete", KEYS.turn_events("t1")), ("delete", KEYS.turn_events("t2"))]]
    assert not mirror._broken and not mirror._to_delete


class SilentRedis:
    """A Redis that keeps the connection open and never answers (a black-holed socket, the F10 'hung' case)."""

    def register_script(self, script: str) -> Any:
        async def call(*, keys: list[str], args: list[Any]) -> list[int]:
            await anyio.sleep_forever()
            raise AssertionError

        return call

    async def _never(self, *args: Any, **kwargs: Any) -> Any:
        await anyio.sleep_forever()

    exists = get = set = xread = _never


async def test_a_silent_redis_bounds_every_mirror_call() -> None:
    """Every mirror Redis call has a time limit (Wave E fix): the writer's script call fails the batch (its turns
    broken, retried after `retry_s`), the watchdog's `remote_delivery` and `follow` answer "no remote" / None, so
    neither the writer nor the abandon watchdog wedges on a Redis that stopped answering."""
    from noema.streaming import InProcessTurnEventLog

    mirror = RedisEventMirror(
        SilentRedis(),  # type: ignore[arg-type]
        KEYS,
        FakeClock(),
        call_timeout_s=0.05,
        write_timeout_s=0.05,
    )
    log = InProcessTurnEventLog(FakeClock(), mirror=mirror)
    log.append("t1", {"type": "turn.created", "turn_id": "t1"})
    with anyio.fail_after(2):
        assert await mirror.flush() is False
        assert mirror._broken == {"t1"}
        assert await mirror.remote_delivery("t1") == (False, None)
        assert await log.idle_seconds("t1") is not None
        assert await mirror.follow("t1") is None


class _OneStreamRedis(SilentRedis):
    """The mirror key exists; XREAD blocks (no frame yet); SET answers."""

    async def exists(self, *keys: str) -> int:
        return 1

    async def set(self, *args: Any, **kwargs: Any) -> bool:
        return True


async def test_remote_readers_are_capped_per_process() -> None:
    """Each remote reader holds one Redis connection in a blocking XREAD for its whole stream: at most
    `max_followers` run per process; above it `follow` answers None (Wave E fix: repeated attaches could open
    unbounded connections and exhaust Valkey's maxclients for every tenant)."""
    mirror = RedisEventMirror(_OneStreamRedis(), KEYS, FakeClock(), max_followers=1)  # type: ignore[arg-type]
    async with anyio.create_task_group() as tg:
        first = await mirror.follow("t1")
        assert first is not None

        async def read() -> None:
            async for _ in first:
                pass

        tg.start_soon(read)
        await anyio.wait_all_tasks_blocked()
        assert await mirror.follow("t2") is None  # the second reader is refused
        tg.cancel_scope.cancel()
    assert await mirror.follow("t2") is not None  # the first one ended: room again
