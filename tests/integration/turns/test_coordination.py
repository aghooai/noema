"""`RedisCoordinator` against valkey (spec-04 §2, §11; spec-02 §13): the FIFO script, `session_pending`, the owner
registry, the `turn_cancel` / `session_released` channels, and the fail-soft behavior with Redis down."""

from __future__ import annotations

import time

import anyio
import pytest
from redis.asyncio import Redis

from noema.core.clock import SystemClock
from noema.core.redis_keys import RedisKeys
from noema.turns.coordination import CoordinationDown, RedisCoordinator

pytestmark = pytest.mark.integration


async def test_fifo_queue_scores_are_redis_time_and_head_is_the_oldest(valkey: Redis, keys: RedisKeys) -> None:
    c = RedisCoordinator(valkey, keys, SystemClock())
    for t in ("turn_a", "turn_b", "turn_c"):
        assert await c.queue_join("ses_1", t, 2000)
        await anyio.sleep(0.002)
    assert await c.queue_head("ses_1", "turn_c", 2000) == "turn_a"
    assert await c.queue_join("ses_1", "turn_a", 2000)  # NX: re-joining keeps the original ticket
    await c.queue_leave("ses_1", "turn_a")
    assert await c.queue_head("ses_1", "turn_c", 2000) == "turn_b"
    ttl = await valkey.pttl(keys.session_queue("ses_1"))
    assert 0 < ttl <= 3000
    await c.queue_leave("ses_1", "turn_b")
    await c.queue_leave("ses_1", "turn_c")
    assert await c.queue_head("ses_1", "turn_x", 2000) is None


async def test_stale_tickets_are_pruned(valkey: Redis, keys: RedisKeys) -> None:
    c = RedisCoordinator(valkey, keys, SystemClock())
    now_ms = int(time.time() * 1000)
    await valkey.zadd(keys.session_queue("ses_2"), {"turn_dead": now_ms - 10_000})
    assert await c.queue_join("ses_2", "turn_live", 2000)
    assert await c.queue_head("ses_2", "turn_live", 2000) == "turn_live"


async def test_pending_flag_and_owner_registry(valkey: Redis, keys: RedisKeys) -> None:
    c = RedisCoordinator(valkey, keys, SystemClock())
    assert not await c.session_pending("ses_1")
    await c.mark_session_pending("ses_1", "turn_1")
    assert await c.session_pending("ses_1")
    assert 86_000 < await valkey.ttl(keys.session_pending("ses_1")) <= 86_400
    payload = {
        "pod": "p",
        "tenant_id": "t",
        "project_id": "p1",
        "principal_id": "prn",
        "session_owner_principal_id": None,
    }
    await c.set_owner("turn_1", payload)
    assert await c.get_owner("turn_1") == payload
    assert 0 < await valkey.ttl(keys.turn_owner("turn_1")) <= 60
    await c.refresh_owners({"turn_1": payload, "turn_2": payload})
    assert await c.get_owner("turn_2") == payload
    await c.delete_owner("turn_1")
    assert await c.get_owner("turn_1") is None


async def test_cancel_and_released_channels(valkey: Redis, keys: RedisKeys) -> None:
    clock = SystemClock()
    a, b = RedisCoordinator(valkey, keys, clock), RedisCoordinator(valkey, keys, clock)
    got: list[tuple[str, str]] = []
    received = anyio.Event()

    async def on_cancel(tenant_id: str, turn_id: str) -> None:
        got.append((tenant_id, turn_id))
        received.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(a.listen, on_cancel)
        await anyio.sleep(0.3)  # subscribed
        await b.publish_cancel("t_acme", "turn_9")
        with anyio.fail_after(3):
            await received.wait()
        t0 = time.monotonic()
        async with anyio.create_task_group() as waiters:
            waiters.start_soon(a.wait_released, "ses_1", 5.0)
            await anyio.sleep(0.05)
            await b.publish_released("t_acme", "ses_1", 4)  # another pod releases
        assert time.monotonic() - t0 < 1.0  # woken by the channel, not the 5 s timeout
        tg.cancel_scope.cancel()
    assert got == [("t_acme", "turn_9")]


async def test_redis_down_is_fail_soft(dead_redis: Redis, keys: RedisKeys) -> None:
    c = RedisCoordinator(dead_redis, keys, SystemClock())
    assert await c.session_pending("ses_1") is False
    assert await c.queue_join("ses_1", "turn_1", 2000) is False
    assert await c.queue_head("ses_1", "turn_1", 2000) is None
    await c.mark_session_pending("ses_1", "turn_1")
    await c.set_owner("turn_1", {"tenant_id": "t"})
    await c.publish_released("t", "ses_1", 1)
    with pytest.raises(CoordinationDown):
        await c.get_owner("turn_1")
    with pytest.raises(CoordinationDown):
        await c.publish_cancel("t", "turn_1")
