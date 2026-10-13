"""`GcraAdmission` on the compose Valkey (spec-11 §1.2): a cancellation that lands while a shielded attempt is in
flight (the caller's direct attempt, or a queued waiter's attempt run by the pump) rolls the Redis debit back; a
transient Redis failure does not leak the slots or calls of reservations debited on Redis. Asserted on the Redis
state. The reserve script is gated, so "in flight" is exact rather than timed."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anyio
import pytest
import redis.asyncio
from redis.exceptions import TimeoutError as RedisTimeout
from tests.contract_internal.kits import principal
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from

from noema.core.protocols.ops import WorkGraph
from noema.core.redis_keys import RedisKeys
from noema.ops.admission import GcraAdmission

from .redis_support import redis_namespace

pytestmark = pytest.mark.integration
TIMEOUT_S = 2.0


class Gate:
    """Wraps the registered reserve script: once armed, the next call signals `started`, then waits for `opened`
    (or raises `fail` instead)."""

    def __init__(self, script: Callable[..., Awaitable[Any]]) -> None:
        self.script = script
        self.armed = False
        self.fail: Exception | None = None
        self.started = anyio.Event()
        self.opened = anyio.Event()

    async def __call__(self, *, keys: list[str], args: list[str | int]) -> Any:
        if self.armed:
            self.armed = False
            if self.fail is not None:
                raise self.fail
            self.started.set()
            await self.opened.wait()
        return await self.script(keys=keys, args=args)


def gated(adm: GcraAdmission) -> Gate:
    store = adm._store._redis
    assert store is not None
    gate = Gate(store._reserve)
    store._reserve = gate  # type: ignore[assignment]
    return gate


class Depth:
    """Follows the admission's queue depth (its `on_depth` hook) so a test can wait for a waiter to come or go."""

    def __init__(self, adm: GcraAdmission) -> None:
        self.value = 0
        self.changed = anyio.Event()
        adm._queue._on_depth = self._on

    def _on(self, depth: int) -> None:
        self.value = depth
        self.changed.set()

    async def reach(self, depth: int) -> None:
        with anyio.fail_after(5):
            while self.value != depth:
                self.changed = anyio.Event()
                await self.changed.wait()


def graph(calls: int = 1, streams: int = 1) -> WorkGraph:
    return WorkGraph({("sol-6.1", "generation"): calls}, {"sol-6.1": 10}, calls, 5, 5, streams)


@pytest.fixture
async def pod(noema_services: Any, noema_run_id: str) -> AsyncIterator[tuple[GcraAdmission, redis.asyncio.Redis, Any]]:
    """(an admission on Valkey with rpm_per_key 100 and one stream per project, a direct client, the keys)."""
    limits = {"rpm_per_key": 100, "concurrent_streams_per_project": 1}
    config = FakeConfigRegistry(snapshot_from(patched_example({"tenants": {"t_acme": {"limits": limits}}})))
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (direct, keys):
        client = redis.asyncio.Redis.from_url(noema_services.redis_url, decode_responses=False)
        try:
            yield GcraAdmission(FakeClock(), config, keys, client, env="test", redis_timeout_s=TIMEOUT_S), direct, keys
        finally:
            await client.aclose()


async def busy_ms(direct: redis.asyncio.Redis, keys: RedisKeys, adm: GcraAdmission, name: str) -> float:
    raw = await direct.get(keys.gcra(name))
    now = adm._clock.now().timestamp() * 1000
    return 0.0 if raw is None else max(0.0, float(raw) - now)


async def test_a_cancel_during_the_direct_attempt_rolls_the_debit_back(pod: Any) -> None:
    adm, direct, keys = pod
    gate = gated(adm)
    gate.armed = True
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: adm.reserve(principal(), graph(calls=3), queue_timeout_s=0))
        await gate.started.wait()
        tg.cancel_scope.cancel()  # lands while the shielded script call is in flight
        gate.opened.set()
    assert await direct.zcard(keys.streams("t_acme", "p1")) == 0
    assert await busy_ms(direct, keys, adm, "key:key_1:rpm") == 0.0  # the 3 calls were refunded
    assert await busy_ms(direct, keys, adm, "key:key_1:tpm") == 0.0


async def test_a_waiter_cancelled_while_the_pump_runs_its_attempt_is_rolled_back(pod: Any) -> None:
    adm, direct, keys = pod
    p1, p2 = principal(), principal(project_id="p2")
    holder_1 = await adm.reserve(p1, graph(), queue_timeout_s=0)
    holder_2 = await adm.reserve(p2, graph(), queue_timeout_s=0)
    gate, depth = gated(adm), Depth(adm)
    waiter = anyio.CancelScope()

    async def queued_on_p2() -> None:
        with waiter:
            await adm.reserve(p2, graph(calls=2), queue_timeout_s=5)

    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: adm.reserve(p1, graph(), queue_timeout_s=5))  # the leader, blocked on p1
        await depth.reach(1)
        tg.start_soon(queued_on_p2)
        await depth.reach(2)
        gate.armed = True
        await holder_2.release()  # the leader's pump now attempts the p2 waiter
        await gate.started.wait()
        waiter.cancel()
        await depth.reach(1)  # the p2 waiter left while its attempt is in flight
        gate.opened.set()
        tg.cancel_scope.cancel()  # the leader finishes the shielded attempt and its rollback first
    assert await direct.zcard(keys.streams("t_acme", "p2")) == 0
    assert await direct.zcard(keys.streams("t_acme", "p1")) == 1  # holder_1 only
    assert await busy_ms(direct, keys, adm, "key:key_1:rpm") == pytest.approx(600.0)  # holder_1's one call
    await holder_1.release()


async def test_a_transient_failure_does_not_leak_redis_slots_or_calls(pod: Any) -> None:
    adm, direct, keys = pod
    held = [await adm.reserve(principal(project_id=p), graph(calls=5), queue_timeout_s=0) for p in ("p1", "p2")]
    gate = gated(adm)
    gate.armed, gate.fail = True, RedisTimeout("a stall over the bound")
    await adm.reserve(principal(project_id="p3"), graph(), queue_timeout_s=0)  # on the local share
    assert adm.degraded
    for res in held:
        await res.release()  # Redis is healthy: the releases go to Redis
    assert adm.degraded and adm._store.pending_releases == 0
    for p in ("p1", "p2"):
        assert await direct.zcard(keys.streams("t_acme", p)) == 0
    assert await busy_ms(direct, keys, adm, "key:key_1:rpm") == 0.0  # the 10 unused calls came back
