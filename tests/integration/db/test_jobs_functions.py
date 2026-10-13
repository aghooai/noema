"""jobs.* and ops.reap_stuck (spec-03 §4, spec-11 §4): claim order and SKIP LOCKED, the worker fence of extend and
finish, backoff and dead at max_attempts, periodic jobs, the reaper and the enqueue dedupe path."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from noema.db.engine import Engine
from noema.db.system import PgSystemCalls
from noema.db.uow import PgDatabase, Tx

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]


def _kind() -> str:
    return f"test.{uuid.uuid4().hex[:10]}"


async def _job(admin: Conn, kind: str, *, priority: int = 100, due_in_s: int = -1, **cols: object) -> str:
    """A queued job of `kind` (extra columns by keyword), due `due_in_s` from now."""
    job_id = f"job_{uuid.uuid4().hex[:20]}"
    names = ["id", "tenant_id", "kind", "priority", "payload", "state", *cols]
    values = [job_id, "t_jobs", kind, priority, {"n": 1}, "queued", *cols.values()]
    params = ", ".join(f"${i}" for i in range(1, len(names) + 1))
    await admin.execute(
        f"INSERT INTO jobs.job ({', '.join(names)}, run_after)"
        f" VALUES ({params}, now() + ${len(names) + 1}::int * interval '1 second')",
        *values,
        due_in_s,
    )
    return job_id


async def _set(admin: Conn, job_ids: list[str], assignments: str, *args: object) -> None:
    await admin.execute(f"UPDATE jobs.job SET {assignments} WHERE id = ANY($1)", job_ids, *args)


async def _state(admin: Conn, job_id: str) -> asyncpg.Record:
    row = await admin.fetchrow(
        "SELECT state, attempts, locked_by, last_error, run_after - now() AS delay FROM jobs.job WHERE id = $1", job_id
    )
    assert row is not None
    return row


async def test_claim_takes_due_jobs_of_the_kinds_by_priority(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    kind, other = _kind(), _kind()
    low = await _job(pg_admin, kind, priority=200)
    high = await _job(pg_admin, kind, priority=10)
    await _job(pg_admin, kind, due_in_s=3600)  # not due
    await _job(pg_admin, other)  # another kind
    async with pg_db.system_uow() as calls:
        claimed = await calls.claim_jobs([kind], 5, "pod:1", 60)
    assert [j.id for j in claimed] == [high, low]
    assert {(j.kind, j.attempt, j.tenant_id) for j in claimed} == {(kind, 1, "t_jobs")}
    assert claimed[0].payload == {"n": 1}
    row = await _state(pg_admin, high)
    assert (row["state"], row["locked_by"]) == ("running", "pod:1")


async def test_claim_skips_rows_locked_by_a_concurrent_claim(pg_admin: Conn, pg_app_engine: Engine) -> None:
    kind = _kind()
    jobs = {await _job(pg_admin, kind) for _ in range(4)}
    # the first claim's transaction holds its row locks until commit; the second claim must skip, not block
    async with pg_app_engine.acquire() as first, pg_app_engine.acquire() as second, first.transaction():
        mine = await PgSystemCalls(first).claim_jobs([kind], 2, "w1", 60)
        theirs = await PgSystemCalls(second).claim_jobs([kind], 10, "w2", 60)
    assert len(mine) == 2 and len(theirs) == 2
    assert {j.id for j in mine} | {j.id for j in theirs} == jobs


async def test_extend_and_finish_are_fenced_by_the_worker(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    kind = _kind()
    job_id = await _job(pg_admin, kind)
    async with pg_db.system_uow() as calls:
        (job,) = await calls.claim_jobs([kind], 1, "w-new", 60)
        assert await calls.extend_job(job.id, "w-old", 60) is False
        assert await calls.finish_job(job.id, "w-old", ok=True, error=None) is None  # not the holder
        assert (await _state(pg_admin, job_id))["state"] == "running"
        assert await calls.extend_job(job.id, "w-new", 60) is True
        assert await calls.finish_job(job.id, "w-new", ok=True, error=None) == "done"
        assert await calls.finish_job(job.id, "w-new", ok=True, error=None) is None  # no longer running
    row = await _state(pg_admin, job_id)
    assert (row["state"], row["locked_by"], row["last_error"]) == ("done", None, None)


async def test_finish_backs_off_then_dead_at_max_attempts(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    kind = _kind()
    job_id = await _job(pg_admin, kind, max_attempts=3)
    async with pg_db.system_uow() as calls:
        for attempt, expected_delay_s in ((1, 30), (2, 60)):
            (job,) = await calls.claim_jobs([kind], 1, "w", 60)
            assert job.attempt == attempt
            assert await calls.finish_job(job_id, "w", ok=False, error="upstream_error: boom") == "queued"
            row = await _state(pg_admin, job_id)
            assert row["state"] == "queued" and row["last_error"] == "upstream_error: boom"
            assert expected_delay_s - 2 <= row["delay"].total_seconds() <= expected_delay_s
            await pg_admin.execute("UPDATE jobs.job SET run_after = now() WHERE id = $1", job_id)
        (job,) = await calls.claim_jobs([kind], 1, "w", 60)
        assert await calls.finish_job(job_id, "w", ok=False, error="x" * 400) == "dead"
    row = await _state(pg_admin, job_id)
    assert (row["state"], row["attempts"], len(row["last_error"])) == ("dead", 3, 256)


async def test_permanent_failure_is_dead_at_once(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    kind = _kind()
    job_id = await _job(pg_admin, kind)
    async with pg_db.system_uow() as calls:
        await calls.claim_jobs([kind], 1, "w", 60)
        assert await calls.finish_job(job_id, "w", ok=False, error="bad_payload", permanent=True) == "dead"
    assert (await _state(pg_admin, job_id))["state"] == "dead"


async def test_ensure_periodic_keeps_one_live_job_per_kind(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    kind = _kind()
    async with pg_db.system_uow() as calls:
        await calls.ensure_periodic(kind, 3600)
        await calls.ensure_periodic(kind, 3600)
        rows = await pg_admin.fetch(
            "SELECT id, tenant_id, priority, dedupe_key, run_after,"
            " date_bin('1 hour', now(), timestamptz '2000-01-01 00:00:00+00') + interval '1 hour' AS boundary"
            " FROM jobs.job WHERE kind = $1",
            kind,
        )
        assert len(rows) == 1
        (row,) = rows
        assert (row["tenant_id"], row["priority"], row["dedupe_key"]) == ("__platform__", 300, kind)
        assert row["run_after"] == row["boundary"]
        await pg_admin.execute("UPDATE jobs.job SET state = 'done' WHERE id = $1", row["id"])
        await calls.ensure_periodic(kind, 3600)  # the finished one is replaced
    assert await pg_admin.fetchval("SELECT count(*) FROM jobs.job WHERE kind = $1", kind) == 2


async def test_reap_stuck_requeues_lapsed_locks_and_deletes_old_finished_jobs(
    pg_admin: Conn, pg_db: PgDatabase[Tx]
) -> None:
    kind = _kind()
    lapsed, exhausted, fresh = [await _job(pg_admin, kind) for _ in range(3)]
    running = (
        "state = 'running', locked_by = 'pod', attempts = $2::int, locked_until = now() + $3::int * interval '1 s'"
    )
    await _set(pg_admin, [lapsed], running, 1, -600)
    await _set(pg_admin, [exhausted], running, 5, -600)
    await _set(pg_admin, [fresh], running, 1, 60)
    old_done, recent_done, old_dead, recent_dead = [await _job(pg_admin, kind) for _ in range(4)]
    await _set(pg_admin, [old_done, recent_done], "state = 'done'")
    await _set(pg_admin, [old_dead, recent_dead], "state = 'dead'")
    for job_id, days in {old_done: 8, recent_done: 1, old_dead: 31, recent_dead: 10}.items():
        await _set(pg_admin, [job_id], "updated_at = now() - $2::int * interval '1 day'", days)
    async with pg_db.system_uow() as calls:
        assert await calls.reap_stuck(120) == 1
    states = {r["id"]: r["state"] for r in await pg_admin.fetch("SELECT id, state FROM jobs.job WHERE kind = $1", kind)}
    assert states == {lapsed: "queued", exhausted: "dead", fresh: "running", recent_done: "done", recent_dead: "dead"}


async def test_enqueue_dedupe_insert_under_tenant_scope(pg_db: PgDatabase[Tx]) -> None:
    kind, key = _kind(), uuid.uuid4().hex
    enqueue = (
        "INSERT INTO jobs.job (id, tenant_id, kind, payload, state, dedupe_key) VALUES ($1, $2, $3, '{}', 'queued', $4)"
        " ON CONFLICT (tenant_id, kind, dedupe_key) WHERE dedupe_key IS NOT NULL AND state IN ('queued', 'running')"
        " DO NOTHING RETURNING id"
    )
    async with pg_db.tenant_uow("t_jobs") as tx:
        first = await tx.conn.fetchval(enqueue, "job_a" + key[:10], "t_jobs", kind, key)
        again = await tx.conn.fetchval(enqueue, "job_b" + key[:10], "t_jobs", kind, key)
    assert first == "job_a" + key[:10]
    assert again is None
