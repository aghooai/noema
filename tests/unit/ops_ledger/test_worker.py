"""PgWorker / PgJobQueue / scheduler / reaper over the fake database (spec-11 §4, §6): concurrency bound, local and
notify wake-ups, the stored error (redacted, <= 200 chars), the dead-job ERROR log (at the job's own max_attempts,
from the state finish_job returns), an ignored finish, drain() and the shutdown grace, claim and finish failures,
and argument checks."""

from __future__ import annotations

import logging
from typing import Any

import anyio
import anyio.lowlevel
import pytest
from tests.fakes.clock import FakeClock, FakeIdGen
from tests.fakes.db import FakeDatabase

from noema.core.protocols.storage import Job, PermanentJobError
from noema.ops.jobs import PeriodicScheduler, PgJobQueue
from noema.ops.reapers import JobReaper

pytestmark = pytest.mark.unit


def make(clock: FakeClock | None = None) -> tuple[PgJobQueue, FakeDatabase, FakeClock]:
    clock = clock or FakeClock()
    db = FakeDatabase(clock)
    q = PgJobQueue(db, clock, FakeIdGen(clock), pod_id="pod", listener=None, redact=lambda s: s.replace("sk-1", "***"))
    return q, db, clock


def job(db: FakeDatabase, job_id: str) -> dict[str, Any]:
    return dict(db.store.table("job")[(job_id,)])


async def test_concurrency_bounds_the_running_handlers() -> None:
    q, _, clock = make()
    for i in range(5):
        await q.enqueue("media.process", "t_acme", {"i": i})
    peak, running = 0, 0

    async def handler(j: Job) -> None:
        nonlocal peak, running
        running += 1
        peak = max(peak, running)
        await clock.sleep(1)
        running -= 1

    worker = q.worker(["media.process"], handler, concurrency=2)
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(worker.run)
        await clock.sleep(4)
        tg.cancel_scope.cancel()
    assert peak == 2 and worker.running == 0 and worker.worker_id.startswith("pod:")


async def test_a_local_enqueue_wakes_the_worker_before_the_poll() -> None:
    q, _, clock = make()
    ran: list[float] = []

    async def handler(j: Job) -> None:
        ran.append(clock.monotonic())

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["session.delete"], handler, concurrency=1).run)
        await clock.sleep(1)
        t = clock.monotonic()
        await q.enqueue("session.delete", "t_acme", {})
        await clock.sleep(1)
        tg.cancel_scope.cancel()
    assert ran == [t]


async def test_stored_errors_are_redacted_and_short(caplog: pytest.LogCaptureFixture) -> None:
    q, db, clock = make()
    long_id = await q.enqueue("labeling.item", "t_acme", {"k": "long"})
    fatal_id = await q.enqueue("labeling.item", "t_acme", {"k": "fatal"})

    async def handler(j: Job) -> None:
        if j.payload["k"] == "fatal":
            raise PermanentJobError("payload refused")
        raise RuntimeError("token sk-1 " + "x" * 500)

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["labeling.item"], handler, concurrency=2).run)
        await clock.sleep(1)
        tg.cancel_scope.cancel()
    error = job(db, long_id)["last_error"]
    assert error.startswith("RuntimeError: token *** xxx") and len(error) == 200
    assert (
        job(db, fatal_id)["state"] == "dead" and job(db, fatal_id)["last_error"] == "PermanentJobError: payload refused"
    )
    dead = [r for r in caplog.records if r.levelno == logging.ERROR and "job dead" in r.getMessage()]
    assert len(dead) == 1 and fatal_id in dead[0].getMessage()


async def test_attempts_exhausted_logs_dead(caplog: pytest.LogCaptureFixture) -> None:
    q, db, clock = make()
    job_id = await q.enqueue("labeling.item", "t_acme", {})

    async def handler(j: Job) -> None:
        raise ValueError

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["labeling.item"], handler, concurrency=1).run)
        await clock.sleep(30 + 60 + 120 + 240 + 5 * 5 + 10)  # the backoffs plus one poll interval each
        tg.cancel_scope.cancel()
    row = job(db, job_id)
    assert (row["state"], row["attempts"], row["last_error"]) == ("dead", 5, "ValueError")
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "job dead" in errors[0] and "attempt=5" in errors[0]


async def test_the_dead_error_follows_the_jobs_own_max_attempts(caplog: pytest.LogCaptureFixture) -> None:
    q, db, clock = make()  # finish_job returns the state the database chose (ops-ledger request 1)
    job_id = await q.enqueue("media.process", "t_acme", {}, max_attempts=2)

    async def handler(j: Job) -> None:
        raise ValueError

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["media.process"], handler, concurrency=1).run)
        await clock.sleep(30 + 2 * 5 + 10)
        tg.cancel_scope.cancel()
    assert (job(db, job_id)["state"], job(db, job_id)["attempts"]) == ("dead", 2)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "job dead" in errors[0] and "attempt=2" in errors[0]
    retried = [r.getMessage() for r in caplog.records if "retried with backoff" in r.getMessage()]
    assert len(retried) == 1 and "attempt=1" in retried[0]


async def test_a_finish_after_the_lease_moved_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    q, db, clock = make()
    job_id = await q.enqueue("labeling.item", "t_acme", {})

    async def handler(j: Job) -> None:
        db.store.table("job")[(j.id,)]["locked_by"] = "other:1"  # reaped and claimed elsewhere meanwhile
        raise PermanentJobError("too late")

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["labeling.item"], handler, concurrency=1).run)
        await clock.sleep(1)
        tg.cancel_scope.cancel()
    row = job(db, job_id)
    assert (row["state"], row["locked_by"], row["last_error"]) == ("running", "other:1", None)
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("finish ignored, the lease was lost" in r.getMessage() for r in caplog.records)


async def test_drain_stops_claiming_and_waits_for_running_handlers() -> None:
    q, db, clock = make()
    first = await q.enqueue("retention.sweep", "__platform__", {})
    done: list[str] = []

    async def handler(j: Job) -> None:
        await clock.sleep(3)
        done.append(j.id)

    worker = q.worker(["retention.sweep"], handler, concurrency=1)
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(worker.run)
        await clock.sleep(1)
        second = await q.enqueue("retention.sweep", "t_acme", {})
        await worker.drain(30)
        assert done == [first]
        await clock.sleep(10)  # draining: never claimed
        tg.cancel_scope.cancel()
    assert job(db, second)["state"] == "queued"


async def test_shutdown_grace_then_left_for_the_reaper() -> None:
    q, db, clock = make()
    job_id = await q.enqueue("labeling.run", "t_acme", {})

    async def handler(j: Job) -> None:
        await clock.sleep(60)

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["labeling.run"], handler, concurrency=1).run)
        await clock.sleep(1)
        tg.cancel_scope.cancel()
    row = job(db, job_id)
    assert row["state"] == "running" and row["last_error"] is None  # never finished: the reaper requeues it


async def test_claim_failures_are_retried_on_the_poll() -> None:
    q, db, clock = make()
    await q.enqueue("media.process", "t_acme", {})
    ran: list[str] = []

    async def handler(j: Job) -> None:
        ran.append(j.id)

    db.store.available = False
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(q.worker(["media.process"], handler, concurrency=1).run)
        await clock.sleep(2)
        assert ran == []
        db.store.available = True
        await clock.sleep(6)
        tg.cancel_scope.cancel()
    assert len(ran) == 1


async def test_arguments_are_checked() -> None:
    q, _, _ = make()
    with pytest.raises(ValueError, match="max_attempts"):
        await q.enqueue("x", "t_acme", {}, max_attempts=0)
    with pytest.raises(ValueError, match="kind and a tenant_id"):
        await q.enqueue("", "t_acme", {})
    with pytest.raises(ValueError, match="at least one job kind"):
        q.worker([], lambda j: anyio.lowlevel.checkpoint(), concurrency=1)
    with pytest.raises(ValueError, match="concurrency"):
        q.worker(["x"], lambda j: anyio.lowlevel.checkpoint(), concurrency=0)


async def test_dedupe_and_options_reach_the_row() -> None:
    q, db, _ = make()
    a = await q.enqueue("media.process", "t_acme", {"file_id": "f1"}, priority=10, dedupe_key="f1", max_attempts=2)
    assert await q.enqueue("media.process", "t_acme", {"file_id": "f1"}, dedupe_key="f1") == a
    row = job(db, a)
    assert (row["priority"], row["max_attempts"], row["dedupe_key"]) == (10, 2, "f1") and a.startswith("job_")


async def test_scheduler_and_reaper_survive_a_database_outage() -> None:
    clock = FakeClock()
    db = FakeDatabase(clock)
    db.store.available = False
    assert await PeriodicScheduler(db, clock).ensure_once() == 0
    assert await JobReaper(db, clock).reap_once() == 0
    db.store.available = True
    assert await PeriodicScheduler(db, clock).ensure_once() == 2
    kinds = sorted(r["kind"] for r in db.store.rows("job"))
    assert kinds == ["labeling.drift", "retention.sweep"]
