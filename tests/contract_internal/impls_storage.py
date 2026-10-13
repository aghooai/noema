"""Registrations: the fakes of the storage suites (database, jobs, idempotency, outbox)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from tests.fakes.clock import FakeIdGen
from tests.fakes.db import FakeDatabase
from tests.fakes.db_system import FakeSystemCalls
from tests.fakes.idempotency import FakeIdempotencyStore
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.outbox import FakeOutbox

from noema.core.protocols.storage import OutboxRecord

from .kits import DatabaseKit, IdempotencyKit, JobQueueKit, OutboxKit
from .registry import Harness, register


@register("database", "fake")
@asynccontextmanager
async def fake_database(h: Harness) -> AsyncIterator[DatabaseKit]:
    db = FakeDatabase(h.clock)

    async def seed(table: str, row: Mapping[str, Any]) -> None:
        db.store.seed(table, row)

    def available(up: bool) -> None:
        db.store.available = up

    yield DatabaseKit(db, h.clock, seed, available)


@register("job_queue", "fake")
@asynccontextmanager
async def fake_jobs(h: Harness) -> AsyncIterator[JobQueueKit]:
    db = FakeDatabase(h.clock)
    queue = FakeJobQueue(h.clock, db, FakeIdGen(h.clock))

    async def state(job_id: str) -> str:
        return str(db.store.table("job")[(job_id,)]["state"])

    yield JobQueueKit(queue, h.clock, state)


@register("idempotency", "fake")
@asynccontextmanager
async def fake_idempotency(h: Harness) -> AsyncIterator[IdempotencyKit]:
    yield IdempotencyKit(FakeIdempotencyStore(h.clock, FakeIdGen(h.clock)), h.clock)


@register("outbox", "fake")
@asynccontextmanager
async def fake_outbox(h: Harness) -> AsyncIterator[OutboxKit]:
    db = FakeDatabase(h.clock)
    outbox = FakeOutbox(sink=FakeSystemCalls(db.store).outbox_apply)

    async def applied() -> list[OutboxRecord]:
        return list(outbox.applied)

    yield OutboxKit(outbox, applied)
