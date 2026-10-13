"""T-JOB-1 / T-JOB-2 (spec-11 §8) and the reaper, on the compose Postgres: SKIP LOCKED across 4 workers (separate
pools) with no double processing; the lease is extended past the 60 s lock; a lost lease cancels the handler and
its result is never written; LISTEN wakes a worker of another process; ensure_periodic from 3 processes keeps one
instance per period; the reaper requeues a dead worker's job."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import anyio
import pytest
from tests.fakes.clock import FakeClock, FakeIdGen
from tests.fixtures.db import PgTestDatabase

from noema.core.clock import SystemClock
from noema.core.ids import UlidIdGen
from noema.core.protocols.storage import Job
from noema.db.engine import Engine, PoolSettings
from noema.db.repos import PgUnitOfWork, make_unit_of_work
from noema.db.uow import PgDatabase
from noema.ops.jobs import PeriodicScheduler, PgJobQueue
from noema.ops.notify_bridge.listener import PgListener
from noema.ops.reapers import JobReaper

from .ledger_harness import CountingDatabase, DbTimeClock, LedgerClock

pytestmark = pytest.mark.integration
CLOCK = SystemClock()


@asynccontextmanager
async def processes(pg: PgTestDatabase, n: int) -> AsyncIterator[list[PgDatabase[PgUnitOfWork]]]:
    """`n` independent noema_app pools: n 'processes'."""
    async with AsyncExitStack() as stack:
        dbs = []
        for _ in range(n):
            engine = await Engine.create(PoolSettings(dsn=pg.app_dsn, max_size=3))
            stack.push_async_callback(engine.close)
            dbs.append(PgDatabase(engine, make_unit_of_work))
        yield dbs


def queue(db: Any, pod: str, *, listener: PgListener | None = None) -> PgJobQueue:
    return PgJobQueue(db, CLOCK, UlidIdGen(CLOCK), pod_id=pod, listener=listener, redact=str)


async def states(admin: Any, kind: str) -> Counter[str]:
    rows = await admin.fetch("SELECT state FROM jobs.job WHERE kind = $1", kind)
    return Counter(r["state"] for r in rows)


async def test_four_workers_never_process_a_job_twice(pg_database: PgTestDatabase, pg_admin: Any) -> None:
    kind = f"test.skip_locked.{uuid.uuid4().hex[:8]}"
    seen: list[str] = []
    async with processes(pg_database, 4) as dbs:
        queues = [queue(db, f"pod{i}") for i, db in enumerate(dbs)]
        ids = {await queues[0].enqueue(kind, "t_jobs", {"i": i}) for i in range(40)}

        async def handler(job: Job) -> None:
            seen.append(job.id)
            await anyio.sleep(0.005)

        with anyio.fail_after(20):
            async with anyio.create_task_group() as tg:
                for q in queues:
                    tg.start_soon(q.worker([kind], handler, concurrency=2).run)
                while (await states(pg_admin, kind))["done"] < 40:  # noqa: ASYNC110 (polls Postgres)
                    await anyio.sleep(0.05)
                tg.cancel_scope.cancel()
    assert sorted(seen) == sorted(ids) and len(seen) == 40
    workers = await pg_admin.fetch("SELECT DISTINCT last_error FROM jobs.job WHERE kind = $1", kind)
    assert [w["last_error"] for w in workers] == [None]


async def test_listen_wakes_a_worker_of_another_process(pg_database: PgTestDatabase, pg_admin: Any) -> None:
    kind = f"test.listen.{uuid.uuid4().hex[:8]}"
    ran, claims_at_run = anyio.Event(), []

    async def handler(job: Job) -> None:
        claims_at_run.append(claims.done)
        ran.set()

    virtual = FakeClock()  # the worker's 5 s poll never fires: only the NOTIFY can wake it
    async with processes(pg_database, 2) as (a, b):
        listener = PgListener(pg_database.app_dsn, virtual)
        claims = ClaimCounter(a)
        q = PgJobQueue(claims, virtual, UlidIdGen(CLOCK), pod_id="listener-pod", listener=listener, redact=str)
        async with anyio.create_task_group() as tg:
            tg.start_soon(q.worker([kind], handler, concurrency=1).run)
            with anyio.fail_after(5):  # safety nets only, never a timing assertion
                await listener.connected.wait()
                while claims.done < 1:  # noqa: ASYNC110 (the first, empty claim has finished)
                    await anyio.sleep(0.01)
            await queue(b, "producer-pod").enqueue(kind, "t_jobs", {})
            with anyio.fail_after(5):
                await ran.wait()
            assert claims_at_run == [2] and virtual.monotonic() == FakeClock().monotonic()  # the NOTIFY woke claim 2
            tg.cancel_scope.cancel()


class ClaimCounter:
    """A `Database` that counts the finished `system_uow` blocks (the worker's claims)."""

    def __init__(self, db: PgDatabase[PgUnitOfWork]) -> None:
        self._db, self.done = db, 0

    def uow(self, principal: Any) -> Any:
        return self._db.uow(principal)

    def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> Any:
        return self._db.tenant_uow(tenant_id, project_id=project_id)

    @asynccontextmanager
    async def system_uow(self) -> AsyncIterator[Any]:
        async with self._db.system_uow() as calls:
            yield calls
        self.done += 1


async def test_the_lease_is_extended_past_the_lock(emptied: tuple[Any, CountingDatabase, LedgerClock]) -> None:
    admin, db, clock = emptied
    q = PgJobQueue(db, DbTimeClock(clock), FakeIdGen(clock), pod_id="w", listener=None, redact=str)
    job_id = await q.enqueue("labeling.run", "t_acme", {"run_id": "lrun_1"})
    alive: list[bool] = []

    async def handler(job: Job) -> None:
        await clock.sleep(65)  # past the 60 s lock: the 20/40/60 s extensions keep it
        with db.io():
            alive.append(await admin.fetchval("SELECT locked_until > now() FROM jobs.job WHERE id = $1", job.id))

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["labeling.run"], handler, concurrency=1).run)
        await clock.sleep(70)
        tg.cancel_scope.cancel()
    assert alive == [True]
    assert await admin.fetchval("SELECT state FROM jobs.job WHERE id = $1", job_id) == "done"


async def test_a_lost_lease_cancels_the_handler(emptied: tuple[Any, CountingDatabase, LedgerClock]) -> None:
    admin, db, clock = emptied
    q = PgJobQueue(db, DbTimeClock(clock), FakeIdGen(clock), pod_id="w", listener=None, redact=str)
    job_id = await q.enqueue("session.delete", "t_acme", {"session_id": "ses_1"})
    started, cancelled = anyio.Event(), anyio.Event()

    async def handler(job: Job) -> None:
        started.set()
        try:
            await clock.sleep(3600)
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["session.delete"], handler, concurrency=1).run)
        await started.wait()
        with db.io():  # another worker took the job over (reaped and re-claimed)
            await admin.execute("UPDATE jobs.job SET locked_by = 'other:1' WHERE id = $1", job_id)
        await clock.sleep(25)  # the 20 s extension is refused
        assert cancelled.is_set()
        tg.cancel_scope.cancel()
    row = await admin.fetchrow("SELECT state, locked_by, last_error FROM jobs.job WHERE id = $1", job_id)
    assert (row["state"], row["locked_by"], row["last_error"]) == ("running", "other:1", None)


async def test_ensure_periodic_from_three_processes(pg_database: PgTestDatabase, pg_admin: Any) -> None:
    admin, kind = pg_admin, f"test.periodic.{uuid.uuid4().hex[:8]}"
    async with processes(pg_database, 3) as dbs:  # three pools: three jobs processes
        schedulers = [PeriodicScheduler(db, CLOCK, periodic=[(kind, 3600)]) for db in dbs]
        async with anyio.create_task_group() as tg:
            for s in schedulers:
                for _ in range(3):
                    tg.start_soon(s.ensure_once)
        await finish_and_replace(admin, kind, schedulers[0])


async def finish_and_replace(admin: Any, kind: str, scheduler: PeriodicScheduler) -> None:
    rows = await admin.fetch("SELECT * FROM jobs.job WHERE kind = $1", kind)
    assert len(rows) == 1 and rows[0]["tenant_id"] == "__platform__" and rows[0]["state"] == "queued"
    due: datetime = rows[0]["run_after"]
    assert due > datetime.now(UTC) and due.timestamp() % 3600 == 0  # the next hour boundary
    await admin.execute("UPDATE jobs.job SET state = 'done' WHERE kind = $1", kind)
    await scheduler.ensure_once()  # the finished instance is replaced by the next one
    assert await states(admin, kind) == Counter({"done": 1, "queued": 1})


async def test_default_periodic_jobs(emptied: tuple[Any, CountingDatabase, LedgerClock]) -> None:
    admin, db, clock = emptied
    assert await PeriodicScheduler(db, clock).ensure_once() == 2
    rows = await admin.fetch("SELECT kind, priority, dedupe_key FROM jobs.job WHERE tenant_id = '__platform__'")
    assert sorted((r["kind"], r["priority"], r["dedupe_key"]) for r in rows) == [
        ("labeling.drift", 300, "labeling.drift"),
        ("retention.sweep", 300, "retention.sweep"),
    ]


async def test_the_reaper_requeues_a_dead_workers_job(emptied: tuple[Any, CountingDatabase, LedgerClock]) -> None:
    admin, db, clock = emptied
    q = PgJobQueue(db, DbTimeClock(clock), FakeIdGen(clock), pod_id="w", listener=None, redact=str)
    job_id = await q.enqueue("retention.sweep", "__platform__", {})
    async with db.system_uow() as calls:
        [job] = await calls.claim_jobs(["retention.sweep"], 1, "dead-pod:1", 60)
    reaper = JobReaper(db, clock)
    assert await reaper.reap_once() == 0  # the lock is still live
    await clock.advance(60 + 121)  # the lock lapsed more than 2 minutes ago
    assert await reaper.reap_once() == 1
    row = await admin.fetchrow("SELECT state, locked_by, attempts FROM jobs.job WHERE id = $1", job_id)
    assert (row["state"], row["locked_by"], row["attempts"]) == ("queued", None, 1) and job.attempt == 1


async def run_failing(q: PgJobQueue, clock: LedgerClock, kind: str, seconds: float) -> None:
    async def handler(job: Job) -> None:
        raise RuntimeError("media tool crashed")

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker([kind], handler, concurrency=1).run)
        await clock.sleep(seconds)
        tg.cancel_scope.cancel()


def logged(caplog: pytest.LogCaptureFixture, level: str, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == level and text in r.getMessage()]


async def test_a_job_dead_at_its_own_max_attempts_logs_the_error(
    emptied: tuple[Any, CountingDatabase, LedgerClock], caplog: pytest.LogCaptureFixture
) -> None:
    admin, db, clock = emptied
    q = PgJobQueue(db, DbTimeClock(clock), FakeIdGen(clock), pod_id="w", listener=None, redact=str)
    job_id = await q.enqueue("media.process", "t_acme", {"file_id": "file_1"}, max_attempts=2)
    await run_failing(q, clock, "media.process", 45)  # attempt 1, 30 s backoff, attempt 2: dead
    assert await admin.fetchval("SELECT state FROM jobs.job WHERE id = $1", job_id) == "dead"
    [error] = logged(caplog, "ERROR", "job dead")  # the alert counts it, although 2 < the default 5
    assert f"id={job_id}" in error and "attempt=2" in error


async def test_a_retried_job_past_five_attempts_is_not_reported_dead(
    emptied: tuple[Any, CountingDatabase, LedgerClock], caplog: pytest.LogCaptureFixture
) -> None:
    admin, db, clock = emptied
    q = PgJobQueue(db, DbTimeClock(clock), FakeIdGen(clock), pod_id="w", listener=None, redact=str)
    job_id = await q.enqueue("media.process", "t_acme", {"file_id": "file_2"}, max_attempts=7)
    with db.io():
        await admin.execute("UPDATE jobs.job SET attempts = 4 WHERE id = $1", job_id)  # the next run is attempt 5
    await run_failing(q, clock, "media.process", 3)
    row = await admin.fetchrow("SELECT state, attempts FROM jobs.job WHERE id = $1", job_id)
    assert (row["state"], row["attempts"]) == ("queued", 5)
    assert logged(caplog, "ERROR", "job dead") == [] and len(logged(caplog, "WARNING", "retried with backoff")) == 1
