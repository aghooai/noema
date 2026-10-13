"""Idempotency store faults on the compose Postgres, Valkey (direct and through toxiproxy) and a scripted S3
(spec-11 §5, spec-02 §7, IMPL-077/085), all on a virtual clock (no wall-clock assertions): the `idem_done` message
alone wakes a waiter (the poll is an hour); a blackholed Redis bounds `complete` (PUBLISH 0.5 s) and `await_result`
(SUBSCRIBE, then the poll decides the 504); a hung S3 bounds a replay's GetObject (2 s, then 500 retryable) and a
take-over's stale DeleteObject (1 s, then New); `aghoo_idempotency_attach_total{result}`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, cast

import anyio
import pytest
from redis.asyncio import Redis
from tests.fakes.clock import FakeClock, FakeIdGen
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.s3 import FakeS3Client
from tests.fakes.telemetry import FakeTelemetry
from tests.fixtures.db import PgTestDatabase, new_tenant_id

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import InFlight, New, StoredResponse
from noema.core.redis_keys import RedisKeys
from noema.db.engine import Engine, PoolSettings
from noema.db.repos import PgUnitOfWork, make_unit_of_work
from noema.db.uow import PgDatabase
from noema.ops.idempotency import BodyStore, PgIdempotencyStore
from noema.ops.idempotency.store import ATTACH, GET_TIMEOUT_S, PUBLISH_TIMEOUT_S, STALE_DELETE_TIMEOUT_S

from .conftest import ValkeyProxy
from .ledger_harness import DbTimeClock
from .test_idempotency import OK, ROUTE, who

pytestmark = pytest.mark.integration
BIG = StoredResponse(200, (("content-type", "application/json"),), b"x" * (2 << 20), None)


class HangingBodies(BodyStore):
    """A `BodyStore` whose GetObject / DeleteObject never answer while `hang_get` / `hang_delete` are set."""

    hang_get = False
    hang_delete = False

    async def get(self, key: str) -> bytes:
        if self.hang_get:
            await anyio.sleep_forever()
        return await super().get(key)

    async def delete(self, tenant_id: str, record_id: str) -> None:
        if self.hang_delete:
            await anyio.sleep_forever()
        await super().delete(tenant_id, record_id)


class PollCounter:
    """A `Database` that counts the finished tenant units of work (the waiter's polls)."""

    def __init__(self, db: PgDatabase[PgUnitOfWork]) -> None:
        self._db, self.done = db, 0

    def uow(self, principal: Any) -> Any:
        return self._db.uow(principal)

    @asynccontextmanager
    async def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> AsyncIterator[PgUnitOfWork]:
        async with self._db.tenant_uow(tenant_id, project_id=project_id) as uow:
            yield uow
        self.done += 1

    def system_uow(self) -> Any:
        return self._db.system_uow()


async def make_store(
    stack: AsyncExitStack, pg: PgTestDatabase, redis_url: str, keys: RedisKeys, virtual: FakeClock, **kw: Any
) -> tuple[PgIdempotencyStore, PollCounter, FakeTelemetry, HangingBodies]:
    """One "process" (its own pool, Redis client and pod id) on the shared virtual clock; closed by `stack`."""
    engine = await Engine.create(PoolSettings(dsn=pg.app_dsn, max_size=4))
    stack.push_async_callback(engine.close)
    redis = Redis.from_url(redis_url, decode_responses=False, socket_connect_timeout=0.5)
    stack.push_async_callback(redis.aclose)
    db, telemetry = PollCounter(PgDatabase(engine, make_unit_of_work)), FakeTelemetry()
    bodies = HangingBodies(cast(Any, kw.pop("s3")), bucket="noema-test", env="test")
    s = PgIdempotencyStore(
        cast(Any, db), redis, keys, bodies, DbTimeClock(virtual), FakeIdGen(virtual), FakeConfigRegistry(),
        telemetry.instruments, pod_id=kw.pop("pod"), **kw,
    )  # fmt: skip
    return s, db, telemetry, bodies


async def test_the_idem_done_message_alone_wakes_the_waiter(
    pg_database: PgTestDatabase, noema_services: Any, keys: RedisKeys
) -> None:
    virtual, s3, t = FakeClock(), FakeS3Client(buckets=("noema-test",)), new_tenant_id()
    url = noema_services.redis_url
    async with AsyncExitStack() as stack:
        a, _, _, _ = await make_store(stack, pg_database, url, keys, virtual, s3=s3, pod="pod0")
        b, polls, telemetry, _ = await make_store(  # an hour-long poll: only the message can wake it in time
            stack, pg_database, url, keys, virtual, s3=s3, pod="pod1", poll_s=3600.0
        )
        new = await a.begin(who(t, "req_a"), "POST", ROUTE, "k", "fp")
        dup = await b.begin(who(t, "req_b"), "POST", ROUTE, "k", "fp")
        assert isinstance(new, New) and isinstance(dup, InFlight)
        got: list[StoredResponse] = []
        with anyio.fail_after(10):  # a safety net only, never a timing assertion
            async with anyio.create_task_group() as tg:

                async def wait() -> None:
                    got.append(await b.await_result(dup.token, deadline=Deadline(virtual.monotonic() + 30, virtual)))

                base = polls.done
                tg.start_soon(wait)
                while polls.done < base + 1:  # noqa: ASYNC110 (subscribed, then the first poll found it in flight)
                    await anyio.sleep(0.01)
                await a.complete(new.token, OK)
        assert got == [OK] and polls.done == base + 2  # woken by the message: one more poll read the response
        assert virtual.monotonic() == FakeClock().monotonic()  # the hour-long poll wait never elapsed
        assert telemetry.counter(ATTACH, result="in_flight") == 1 and telemetry.counter(ATTACH, result="completed") == 1


async def test_a_blackholed_redis_bounds_complete_and_await_result(
    pg_database: PgTestDatabase, valkey_proxy: ValkeyProxy, keys: RedisKeys
) -> None:
    virtual, s3, t = FakeClock(), FakeS3Client(buckets=("noema-test",)), new_tenant_id()
    async with AsyncExitStack() as stack:
        a, _, _, _ = await make_store(stack, pg_database, valkey_proxy.url, keys, virtual, s3=s3, pod="pod0")
        b, polls, telemetry, _ = await make_store(
            stack, pg_database, valkey_proxy.url, keys, virtual, s3=s3, pod="pod1"
        )
        new = await a.begin(who(t, "req_a"), "POST", ROUTE, "k", "fp")
        dup = await b.begin(who(t, "req_b"), "POST", ROUTE, "k", "fp")
        assert isinstance(new, New) and isinstance(dup, InFlight)
        valkey_proxy.blackhole()  # replies are lost, nothing is refused
        stack.callback(valkey_proxy.heal)
        with anyio.fail_after(10):  # real time: a safety net only
            async with virtual.autojump():
                started, base = virtual.monotonic(), polls.done
                with pytest.raises(NoemaError) as err:  # SUBSCRIBE gives up, the poll decides at the deadline
                    await b.await_result(dup.token, deadline=Deadline(started + 1.0, virtual))
                assert err.value.code is ErrorCode.DEADLINE_EXCEEDED
                assert polls.done - base == 3  # SUBSCRIBE gave up at 0.5 s; polls at 0.5, 0.75 and 1.0 s only
                assert 1.0 <= virtual.monotonic() - started <= 1.25  # not the socket timeout
                started = virtual.monotonic()
                await a.complete(new.token, OK)  # the row is written; PUBLISH gives up after 0.5 s
                assert virtual.monotonic() - started == PUBLISH_TIMEOUT_S
        assert telemetry.counter(ATTACH, result="deadline") == 1
        replay = await b.begin(who(t, "req_c"), "POST", ROUTE, "k", "fp")
        assert replay.response == OK  # type: ignore[union-attr]


async def test_a_hung_s3_bounds_the_replay_read_and_the_stale_delete(
    pg_database: PgTestDatabase, noema_services: Any, keys: RedisKeys, pg_admin: Any
) -> None:
    virtual, s3, t = FakeClock(), FakeS3Client(buckets=("noema-test",)), new_tenant_id()
    async with AsyncExitStack() as stack:
        a, _, _, bodies = await make_store(stack, pg_database, noema_services.redis_url, keys, virtual, s3=s3, pod="p")
        new = await a.begin(who(t, "req_a"), "POST", ROUTE, "k", "fp")
        assert isinstance(new, New)
        await a.complete(new.token, BIG)  # > 1 MiB: stored in S3
        bodies.hang_get = bodies.hang_delete = True
        with anyio.fail_after(10):  # real time: a safety net only
            async with virtual.autojump():
                started = virtual.monotonic()
                with pytest.raises(NoemaError) as err:
                    await a.begin(who(t, "req_b"), "POST", ROUTE, "k", "fp")  # the replay's GetObject never answers
                assert err.value.code is ErrorCode.INTERNAL_ERROR and err.value.spec.retryable
                assert virtual.monotonic() - started == GET_TIMEOUT_S
                await pg_admin.execute(
                    "UPDATE noema.idempotency_record SET expires_at = now() - interval '1 second' WHERE tenant_id = $1",
                    t,
                )
                started = virtual.monotonic()
                taken = await a.begin(who(t, "req_c"), "POST", ROUTE, "k", "fp")  # expired: taken over
                assert isinstance(taken, New) and taken.token.record_id == new.token.record_id
                assert virtual.monotonic() - started == STALE_DELETE_TIMEOUT_S  # the stale delete gave up
        bodies.hang_get = bodies.hang_delete = False
        await a.complete(taken.token, BIG)  # the new owner overwrites the same key
        replay = await a.begin(who(t, "req_d"), "POST", ROUTE, "k", "fp")
        assert replay.response.body == BIG.body  # type: ignore[union-attr]


async def test_the_waiters_of_one_process_share_one_idem_done_connection(
    pg_database: PgTestDatabase, noema_services: Any, keys: RedisKeys
) -> None:
    """Wave E2 (edge defect 12 rest): every JSON duplicate waiting in one process reads its `idem_done` wake-up
    from ONE shared pub/sub connection (each opened its own before, so a burst of duplicates could drain the Redis
    pool); each waiter is still woken by its own original's message alone (an hour-long poll)."""
    virtual, s3, t = FakeClock(), FakeS3Client(buckets=("noema-test",)), new_tenant_id()
    url, n = noema_services.redis_url, 8
    async with AsyncExitStack() as stack:
        a, _, _, _ = await make_store(stack, pg_database, url, keys, virtual, s3=s3, pod="pod0")
        b, polls, _, _ = await make_store(stack, pg_database, url, keys, virtual, s3=s3, pod="pod1", poll_s=3600.0)
        redis, opened = b._redis, []
        pubsub = redis.pubsub

        def counting() -> Any:
            opened.append(1)
            return pubsub()

        redis.pubsub = counting  # type: ignore[method-assign,assignment]
        pairs = []
        for i in range(n):
            new = await a.begin(who(t, f"req_a{i}"), "POST", ROUTE, f"k{i}", "fp")
            dup = await b.begin(who(t, f"req_b{i}"), "POST", ROUTE, f"k{i}", "fp")
            assert isinstance(new, New) and isinstance(dup, InFlight)
            pairs.append((new, dup))
        got: list[StoredResponse] = []
        with anyio.fail_after(10):  # a safety net only, never a timing assertion
            async with anyio.create_task_group() as tg:

                async def wait(dup: InFlight) -> None:
                    got.append(await b.await_result(dup.token, deadline=Deadline(virtual.monotonic() + 30, virtual)))

                base = polls.done
                for _, dup in pairs:
                    tg.start_soon(wait, dup)
                while polls.done < base + n:  # noqa: ASYNC110 (each subscribed, then its first poll)
                    await anyio.sleep(0.01)
                assert len(opened) == 1
                for new, _ in pairs:
                    await a.complete(new.token, OK)
        assert got == [OK] * n
        assert virtual.monotonic() == FakeClock().monotonic()  # only the messages woke them
