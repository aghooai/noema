"""ops-ledger registrations of the storage conformance suites (spec-02 §14) on the compose Postgres (the session
clone, noema_app, RLS) and Valkey: `outbox` (WriteBehindOutbox), `job_queue` (PgJobQueue) and `idempotency`
(PgIdempotencyStore). The suites use fixed ids (t_acme, k1, ...), so the job and idempotency kits run on a
dedicated per-process database emptied before every kit; their Postgres clock is aged with the suite's virtual
clock (see ledger_harness.py)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

import pytest
from redis.asyncio import Redis
from tests.contract_internal.kits import IdempotencyKit, JobQueueKit, OutboxKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.clock import FakeIdGen
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.s3 import FakeS3Client
from tests.fakes.telemetry import FakeTelemetry

from noema.core.principal import Principal
from noema.core.protocols.storage import OutboxRecord
from noema.core.redis_keys import RedisKeys
from noema.db.engine import Engine, PoolSettings
from noema.db.repos import PgUnitOfWork, make_unit_of_work
from noema.db.system import PgSystemCalls
from noema.db.uow import PgDatabase
from noema.ops.idempotency import BodyStore, PgIdempotencyStore
from noema.ops.jobs import PgJobQueue
from noema.ops.outbox import WriteBehindOutbox

from .ledger_harness import DbTimeClock, dedicated_database, emptied_database


class _RecordingCalls:
    def __init__(self, calls: PgSystemCalls, applied: list[OutboxRecord]) -> None:
        self._calls, self._applied = calls, applied

    async def outbox_apply(self, records: Sequence[OutboxRecord]) -> tuple[str | None, ...]:
        results = await self._calls.outbox_apply(records)
        self._applied += [r for r, error in zip(records, results, strict=True) if error is None]
        return results

    def __getattr__(self, name: str) -> Any:
        return getattr(self._calls, name)


class RecordingDatabase:
    """The real `Database`, logging every record `outbox_apply` accepted (the kit's `applied` hook)."""

    def __init__(self, db: PgDatabase[PgUnitOfWork]) -> None:
        self._db = db
        self.applied: list[OutboxRecord] = []

    def uow(self, principal: Principal) -> AbstractAsyncContextManager[PgUnitOfWork]:
        return self._db.uow(principal)

    def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> AbstractAsyncContextManager[PgUnitOfWork]:
        return self._db.tenant_uow(tenant_id, project_id=project_id)

    @asynccontextmanager
    async def system_uow(self) -> AsyncIterator[_RecordingCalls]:
        async with self._db.system_uow() as calls:
            yield _RecordingCalls(calls, self.applied)


@asynccontextmanager
async def valkey(h: Harness) -> AsyncIterator[tuple[Redis, RedisKeys]]:
    """A Valkey client and a fresh key namespace, unlinked at exit."""
    client = Redis.from_url(h.fixture("noema_services").redis_url, decode_responses=False)
    keys = RedisKeys(f"ns:{h.run_id}:{uuid.uuid4().hex[:8]}:")
    try:
        yield client, keys
    finally:
        async for key in client.scan_iter(match=f"{keys.ns}*"):
            await client.unlink(key)
        await client.aclose()


@register("outbox", "write_behind", marks=(pytest.mark.integration,))
@asynccontextmanager
async def write_behind_outbox(h: Harness) -> AsyncIterator[OutboxKit]:
    _, app_dsn = await dedicated_database(h)
    engine = await Engine.create(PoolSettings(dsn=app_dsn, max_size=4))
    try:
        async with valkey(h) as (redis, keys):
            db = RecordingDatabase(PgDatabase(engine, make_unit_of_work))
            outbox = WriteBehindOutbox(db, redis, keys, h.clock, FakeTelemetry().instruments)

            async def applied() -> list[OutboxRecord]:
                return list(db.applied)

            yield OutboxKit(outbox, applied)
    finally:
        await engine.close()


@register("job_queue", "postgres", marks=(pytest.mark.integration,))
@asynccontextmanager
async def pg_job_queue(h: Harness) -> AsyncIterator[JobQueueKit]:
    async with emptied_database(h) as (admin, db, clock):
        queue = PgJobQueue(db, DbTimeClock(clock), FakeIdGen(clock), pod_id="kit", listener=None, redact=str)

        async def state(job_id: str) -> str:
            return str(await admin.fetchval("SELECT state FROM jobs.job WHERE id = $1", job_id))

        yield JobQueueKit(queue, clock, state)


@register("idempotency", "postgres", marks=(pytest.mark.integration,))
@asynccontextmanager
async def pg_idempotency(h: Harness) -> AsyncIterator[IdempotencyKit]:
    async with emptied_database(h) as (_, db, clock), valkey(h) as (redis, keys):
        s3 = FakeS3Client(buckets=("noema-test",))
        bodies = BodyStore(cast(Any, s3), bucket="noema-test", env="test")
        telemetry = FakeTelemetry()
        store = PgIdempotencyStore(
            db, redis, keys, bodies, DbTimeClock(clock), FakeIdGen(clock), FakeConfigRegistry(), telemetry.instruments,
            pod_id="kit",
        )  # fmt: skip
        yield IdempotencyKit(store, clock)
