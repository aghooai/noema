"""T-IDEM-1 (spec-11 §8, IMPL-077): two "processes" (separate pools, Redis clients and pod ids) share one key:
attach (InFlight + resource_id for SSE, await_result for JSON), replay, conflict, owner-death take-over, not_stored
re-execution, racing take-overs with exactly one winner; Postgres down -> 500. Pub/sub wake-up, slow Redis and hung
S3 cases (virtual clock): test_idempotency_faults.py."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, cast

import anyio
import pytest
from redis.asyncio import Redis
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.s3 import FakeS3Client
from tests.fakes.telemetry import FakeTelemetry
from tests.fixtures.db import PgTestDatabase, new_tenant_id

from noema.core.clock import Deadline, SystemClock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import UlidIdGen
from noema.core.principal import ALL_SCOPES, Principal
from noema.core.protocols.storage import Conflict, InFlight, New, Replay, StoredResponse
from noema.core.redis_keys import RedisKeys
from noema.db.engine import Engine, PoolSettings
from noema.db.repos import make_unit_of_work
from noema.db.uow import PgDatabase
from noema.ops.idempotency import BodyStore, PgIdempotencyStore

from .conftest import UNREACHABLE_PG

pytestmark = pytest.mark.integration
CLOCK = SystemClock()
ROUTE = "/v1/decisions"
OK = StoredResponse(200, (("content-type", "application/json"),), b'{"id": "dec_1"}', None)


def who(tenant: str, request_id: str, project: str = "prj_a") -> Principal:
    return Principal(tenant, project, "prn_1", "key_1", frozenset({"developer"}), ALL_SCOPES, request_id)


@asynccontextmanager
async def stores(pg: PgTestDatabase, redis_url: str, keys: RedisKeys, n: int = 2) -> AsyncIterator[list[Any]]:
    """`n` stores, each with its own pool, Redis client and pod id."""
    async with AsyncExitStack() as stack:
        out = []
        s3 = FakeS3Client(buckets=("noema-test",))
        for i in range(n):
            engine = await Engine.create(PoolSettings(dsn=pg.app_dsn, max_size=4))
            stack.push_async_callback(engine.close)
            redis = Redis.from_url(redis_url, decode_responses=False)
            stack.push_async_callback(redis.aclose)
            out.append(
                PgIdempotencyStore(
                    PgDatabase(engine, make_unit_of_work),
                    redis,
                    keys,
                    BodyStore(s3, bucket="noema-test", env="test"),  # type: ignore[arg-type]
                    CLOCK,
                    UlidIdGen(CLOCK),
                    FakeConfigRegistry(),
                    FakeTelemetry().instruments,
                    pod_id=f"pod{i}",
                )
            )
        yield out


@pytest.fixture
async def pair(pg_database: PgTestDatabase, noema_services: Any, keys: RedisKeys) -> AsyncIterator[list[Any]]:
    async with stores(pg_database, noema_services.redis_url, keys) as both:
        yield both


async def lapse_lease(pg_admin: Any, tenant: str) -> None:
    await pg_admin.execute(
        "UPDATE noema.idempotency_record SET lease_expires_at = now() - interval '1 second' WHERE tenant_id = $1",
        tenant,
    )


async def test_attach_replay_and_conflict(pair: list[Any]) -> None:
    a, b = pair
    t = new_tenant_id()
    new = await a.begin(who(t, "req_a"), "POST", ROUTE, "k", "fp", resource_id="dec_A")
    assert isinstance(new, New)
    dup = await b.begin(who(t, "req_b"), "POST", ROUTE, "k", "fp", resource_id="dec_B")
    assert isinstance(dup, InFlight) and dup.resource_id == "dec_A"  # an SSE duplicate attaches to dec_A's log
    assert isinstance(await b.begin(who(t, "req_c"), "POST", ROUTE, "k", "other"), Conflict)
    got: list[StoredResponse] = []
    async with anyio.create_task_group() as tg:

        async def wait() -> None:
            got.append(await b.await_result(dup.token, deadline=Deadline(CLOCK.monotonic() + 10, CLOCK)))

        tg.start_soon(wait)
        assert await a.heartbeat(new.token)
        await a.complete(new.token, OK)  # before or after the waiter's first poll: it returns OK either way
    assert got == [OK]  # that the idem_done message alone wakes it: test_idempotency_faults.py
    replay = await b.begin(who(t, "req_d"), "POST", ROUTE, "k", "fp")
    assert isinstance(replay, Replay) and replay.response == OK
    assert isinstance(await b.begin(who(t, "req_e"), "POST", ROUTE, "k", "other"), Conflict)


async def test_owner_death_is_taken_over_and_the_old_owner_is_fenced(pair: list[Any], pg_admin: Any) -> None:
    a, b = pair
    t = new_tenant_id()
    old = await a.begin(who(t, "req_a"), "POST", ROUTE, "k", "fp", resource_id="dec_A")
    dup = await b.begin(who(t, "req_w"), "POST", ROUTE, "k", "fp")
    await lapse_lease(pg_admin, t)  # pod0 died: its lease lapses
    with pytest.raises(NoemaError) as err:  # the waiting duplicate learns that the original died
        await b.await_result(dup.token, deadline=Deadline(CLOCK.monotonic() + 5, CLOCK))
    assert err.value.code is ErrorCode.INTERNAL_ERROR
    new = await b.begin(who(t, "req_b"), "POST", ROUTE, "k", "fp", resource_id="dec_B")
    assert isinstance(new, New) and new.token.record_id == old.token.record_id and new.token != old.token
    assert not await a.heartbeat(old.token)
    await a.complete(old.token, StoredResponse(200, (), b"stale", None))  # fenced: no effect
    await b.complete(new.token, OK)
    replay = await a.begin(who(t, "req_c"), "POST", ROUTE, "k", "fp")
    assert isinstance(replay, Replay) and replay.response.body == OK.body
    row = await pg_admin.fetchrow("SELECT owner, resource_id FROM noema.idempotency_record WHERE tenant_id = $1", t)
    assert (row["owner"], row["resource_id"]) == ("pod1:req_b", "dec_B")


async def test_not_stored_re_executes(pair: list[Any]) -> None:
    a, b = pair
    t = new_tenant_id()
    first = await a.begin(who(t, "req_a"), "POST", ROUTE, "k", "fp")
    await a.complete(first.token, None)  # failed before any model call
    assert isinstance(await b.begin(who(t, "req_b"), "POST", ROUTE, "k", "fp"), New)


async def test_racing_take_overs_have_one_winner(
    pg_database: PgTestDatabase, noema_services: Any, keys: RedisKeys, pg_admin: Any
) -> None:
    t = new_tenant_id()
    async with stores(pg_database, noema_services.redis_url, keys, n=4) as racers:
        await racers[0].begin(who(t, "req_0"), "POST", ROUTE, "k", "fp")
        await lapse_lease(pg_admin, t)
        outcomes: list[object] = []
        async with anyio.create_task_group() as tg:
            for i in range(12):

                async def race(i: int = i) -> None:
                    outcomes.append(await racers[i % 4].begin(who(t, f"req_r{i}"), "POST", ROUTE, "k", "fp"))

                tg.start_soon(race)
    assert sum(isinstance(o, New) for o in outcomes) == 1
    assert all(isinstance(o, (New, InFlight)) for o in outcomes)
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.idempotency_record WHERE tenant_id = $1", t) == 1


async def test_scopes_are_separate_per_tenant_project_and_route(pair: list[Any]) -> None:
    a, _ = pair
    t, other = new_tenant_id(), new_tenant_id()
    assert isinstance(await a.begin(who(t, "r1"), "POST", ROUTE, "k", "fp"), New)
    assert isinstance(await a.begin(who(other, "r2"), "POST", ROUTE, "k", "fp"), New)
    assert isinstance(await a.begin(who(t, "r3", project="prj_b"), "POST", ROUTE, "k", "fp"), New)
    assert isinstance(await a.begin(who(t, "r4"), "POST", "/v1/turns", "k", "fp"), New)


async def test_postgres_down_is_a_retryable_500(keys: RedisKeys) -> None:
    engine = await Engine.create(PoolSettings(dsn=UNREACHABLE_PG, max_size=1, acquire_timeout_s=0.5))
    store = PgIdempotencyStore(
        PgDatabase(engine, make_unit_of_work),
        Redis.from_url("redis://127.0.0.1:1/0"),
        keys,
        BodyStore(cast(Any, FakeS3Client()), bucket="b", env="test"),
        CLOCK,
        UlidIdGen(CLOCK),
        FakeConfigRegistry(),
        FakeTelemetry().instruments,
        pod_id="p",
    )
    try:
        with pytest.raises(NoemaError) as err:
            await store.begin(who("t_x", "r"), "POST", ROUTE, "k", "fp")
        assert err.value.code is ErrorCode.INTERNAL_ERROR and err.value.spec.retryable
    finally:
        await engine.close()
