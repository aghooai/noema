"""T-SESS-1…6 (spec-04 §2, §13; IMPL-022): session ordering across two API pods on the real stores: the real
Postgres `SessionRepo` (noema_app, RLS) and valkey. Each pod has its own Postgres pool and its own Redis
client and coordinator, so the only shared state is the databases (true OS processes run in C2's compose `app`
profile). FIFO under 5 contenders (they join in a fixed order: each starts once the previous one is queued, no
timing sleeps), `session_conflict` without waiting, `session_busy` at 2,000 ± 100 ms, lease takeover after a pod
kill (the lease is expired in Postgres, whose clock decides), `session_pending` blocking even after lease expiry,
and `store: false` never queueing."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from redis.asyncio import Redis
from tests.fixtures.db import PgTestDatabase, seed_tenant
from tests.golden.sse_scenarios import document, router_rules, stream
from tests.unit.turns.harness import STICKY_SOL, USER_BODY, make_harness, principal, run_turn

from noema.core.clock import SystemClock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import ALL_SCOPES, Principal
from noema.core.protocols.storage import SessionLease
from noema.core.redis_keys import RedisKeys
from noema.core.runtime import TaskGroupRuntime
from noema.db.engine import Engine, PoolSettings
from noema.db.repos import make_unit_of_work
from noema.db.uow import PgDatabase
from noema.turns import session_order
from noema.turns.coordination import RedisCoordinator

pytestmark = pytest.mark.integration

SESSION = "ses_1"


@dataclass
class Pod:
    """One API process's view of the stores: its own Postgres pool and Redis client."""

    db: PgDatabase[Any]
    coordinator: RedisCoordinator


@dataclass
class Cluster:
    pods: tuple[Pod, Pod]
    who: Principal
    admin: Any
    valkey: Redis
    keys: RedisKeys

    async def acquire(
        self, pod: Pod, turn_id: str, expected: int | None = None, timeout_ms: int = 2000
    ) -> SessionLease:
        return await session_order.acquire(
            pod.db,
            pod.coordinator,
            SystemClock(),
            self.who,
            session_id=SESSION,
            turn_id=turn_id,
            expected_seq=expected,
            queue_timeout_ms=timeout_ms,
        )

    async def queued(self, turn_id: str) -> bool:
        return await self.valkey.zscore(self.keys.session_queue(SESSION), turn_id) is not None

    @asynccontextmanager
    async def listening(self) -> AsyncIterator[None]:
        """Both pods subscribed to `session_released` (waiters wake on a release, not only by polling)."""
        async with anyio.create_task_group() as tg:
            for pod in self.pods:
                tg.start_soon(pod.coordinator.listen, _ignore)
            channel = self.keys.turn_cancel()

            async def subscribed() -> bool:
                counts = await self.valkey.pubsub_numsub(channel)
                return bool(counts) and int(counts[0][1]) == len(self.pods)

            await _until(subscribed)  # wake-ups only speed waiters up; the FIFO order is the queue's
            yield
            tg.cancel_scope.cancel()

    async def expire_lease(self) -> None:
        """A killed pod never renews: its lease runs out (Postgres' now() decides)."""
        await self.admin.execute(
            "UPDATE noema.session SET lease_expires_at = now() - interval '1 second' WHERE tenant_id = $1 AND id = $2",
            self.who.tenant_id,
            SESSION,
        )


@pytest.fixture
async def cluster(
    pg_database: PgTestDatabase, pg_admin: Any, noema_services: Any, keys: RedisKeys, valkey: Redis
) -> AsyncIterator[Cluster]:
    tenant = await seed_tenant(pg_admin, projects=("p1",))
    principal_id = tenant.principal_ids["p1"]
    await pg_admin.execute(
        "INSERT INTO noema.session (tenant_id, project_id, id, owner_principal_id, data_class, last_seq)"
        " VALUES ($1, 'p1', $2, $3, 'internal', 3)",
        tenant.tenant_id,
        SESSION,
        principal_id,
    )
    who = Principal(tenant.tenant_id, "p1", principal_id, "key_1", frozenset({"developer"}), ALL_SCOPES, "req_1")
    engines = [await Engine.create(PoolSettings(dsn=pg_database.app_dsn, max_size=4)) for _ in range(2)]
    clients = [Redis.from_url(noema_services.redis_url, decode_responses=False) for _ in range(2)]
    pods = tuple(
        Pod(PgDatabase(engine, make_unit_of_work), RedisCoordinator(client, keys, SystemClock()))
        for engine, client in zip(engines, clients, strict=True)
    )
    try:
        yield Cluster((pods[0], pods[1]), who, pg_admin, valkey, keys)
    finally:
        for client in clients:
            await client.aclose()
        for engine in engines:
            await engine.close()


async def _ignore(tenant_id: str, turn_id: str) -> None:
    return None


async def _until(check: Any, *, limit_s: float = 5.0) -> None:
    """Wait for a condition (polling the stores; no timing logic depends on the poll)."""
    with anyio.fail_after(limit_s):
        while not await check():  # noqa: ASYNC110 - the condition lives in Redis: no local event to wait on
            await anyio.sleep(0.005)


async def test_sess_1_fifo_under_five_contenders_on_two_pods(cluster: Cluster) -> None:
    a, b = cluster.pods
    order: list[str] = []
    async with cluster.listening():
        holder = await cluster.acquire(a, "turn_0")
        async with anyio.create_task_group() as tg:

            async def contender(i: int) -> None:
                pod = a if i % 2 else b
                lease = await cluster.acquire(pod, f"turn_{i}", timeout_ms=10_000)
                order.append(lease.turn_id)
                await session_order.release(pod.db, pod.coordinator, cluster.who, lease)

            for i in range(1, 6):  # each joins the queue only after the previous one is queued
                tg.start_soon(contender, i)
                await _until(lambda i=i: cluster.queued(f"turn_{i}"))
            await session_order.release(a.db, a.coordinator, cluster.who, holder)
    assert order == [f"turn_{i}" for i in range(1, 6)]
    assert not await cluster.valkey.exists(cluster.keys.session_queue(SESSION))


async def test_sess_2_conflict_without_waiting(cluster: Cluster) -> None:
    a, b = cluster.pods
    await cluster.acquire(a, "turn_holder")
    t0 = time.monotonic()
    with pytest.raises(NoemaError) as err:
        await cluster.acquire(b, "turn_x", expected=1)
    assert err.value.code is ErrorCode.SESSION_CONFLICT and err.value.details == {"last_seq": 3}
    assert time.monotonic() - t0 < 1.0 and not await cluster.queued("turn_x")  # never queued


async def test_sess_3_busy_at_2000_ms(cluster: Cluster) -> None:
    a, b = cluster.pods
    await cluster.acquire(a, "turn_holder")
    t0 = time.monotonic()
    with pytest.raises(NoemaError) as err:
        await cluster.acquire(b, "turn_waiter")
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert err.value.code is ErrorCode.SESSION_BUSY and err.value.retry_after_s == 1
    assert 2000 <= elapsed_ms <= 2100
    assert not await cluster.queued("turn_waiter")  # left the queue


async def test_sess_4_lease_takeover_after_a_pod_kill(cluster: Cluster) -> None:
    a, b = cluster.pods
    await cluster.acquire(a, "turn_on_killed_pod")  # never renewed nor released
    with pytest.raises(NoemaError) as busy:
        await cluster.acquire(b, "turn_early", timeout_ms=200)
    assert busy.value.code is ErrorCode.SESSION_BUSY
    await cluster.expire_lease()
    lease = await cluster.acquire(b, "turn_next")
    assert lease.turn_id == "turn_next" and lease.last_seq == 3
    assert not await session_order.renew(a.db, cluster.who, SessionLease(SESSION, "turn_on_killed_pod", 3, None))


async def test_sess_5_session_pending_blocks_after_lease_expiry(cluster: Cluster) -> None:
    a, b = cluster.pods
    await cluster.acquire(a, "turn_outboxed")
    await a.coordinator.mark_session_pending(SESSION, "turn_outboxed")  # its finalize went to the outbox
    await cluster.expire_lease()
    with pytest.raises(NoemaError) as err:
        await cluster.acquire(b, "turn_next", timeout_ms=300)
    assert err.value.code is ErrorCode.SESSION_BUSY
    await cluster.valkey.delete(cluster.keys.session_pending(SESSION))  # the drainer applied the finalize
    assert (await cluster.acquire(b, "turn_next")).turn_id == "turn_next"


async def test_sess_6_store_false_never_queues(valkey: Redis, keys: RedisKeys) -> None:
    from tests.fakes.world import build_world

    sol = stream("us.openai.gpt-6.1-sol", first_event_ms=5, first_text_ms=10, chunks=["ok"])
    world = build_world(clock=SystemClock(), scenario=document("s6", [sol], router_rules()))
    world.runtime = TaskGroupRuntime(world.clock)
    h = make_harness(world=world)
    h.coordinator = RedisCoordinator(valkey, keys, world.clock)  # type: ignore[assignment]
    h.service.coordinator = h.coordinator
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    held = await session_order.acquire(
        world.db,
        h.coordinator,
        SystemClock(),
        principal(),
        session_id="ses_1",
        turn_id="turn_holder",
        expected_seq=None,
        queue_timeout_ms=2000,
    )
    assert held.turn_id == "turn_holder"
    await world.runtime.open()
    try:
        with anyio.fail_after(10):
            t0 = time.monotonic()
            runner = await run_turn(h, {**USER_BODY, "session_id": "ses_1", "store": False})
            assert time.monotonic() - t0 < 2.0  # not delayed by the held session
    finally:
        await world.runtime.close()
    assert runner.result is not None and runner.result.status == "completed"
    assert not await valkey.exists(keys.session_queue("ses_1"))
