"""JobRepo (spec-02 §7.1, spec-11 §4: dedupe, pg_notify on commit, SKIP LOCKED claims by two workers), AuditRepo
(append-only) and ProjectRepo."""

from __future__ import annotations

from typing import Any

import anyio
import asyncpg
import pytest
from tests.fixtures.db import PgTestDatabase, SeededTenant, seed_tenant
from tests.integration.db_repos._rows import Conn, new_id

from noema.db.engine import ROLE_APP, connect
from noema.db.repos import JOBS_CHANNEL, PgUnitOfWork
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration
DB = PgDatabase[PgUnitOfWork]


def _job(kind: str, **extra: Any) -> dict[str, Any]:
    return {"id": new_id("job"), "kind": kind, "payload": {"file_id": "file_1"}, **extra}


async def test_insert_dedupes_live_jobs_only(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        first = await u.jobs.insert(_job("media.process", dedupe_key="file_1", priority=10))
        assert await u.jobs.insert(_job("media.process", dedupe_key="file_1")) == first  # live duplicate
        other_kind = await u.jobs.insert(_job("session.delete", dedupe_key="file_1"))
        no_key = [await u.jobs.insert(_job("media.process")) for _ in range(2)]
    assert len({first, other_kind, *no_key}) == 4
    row = await pg_admin.fetchrow("SELECT * FROM jobs.job WHERE id = $1", first)
    assert row is not None
    assert (row["tenant_id"], row["state"], row["priority"], row["attempts"], row["payload"]) == (
        tenant.tenant_id, "queued", 10, 0, {"file_id": "file_1"})  # fmt: skip
    await pg_admin.execute("UPDATE jobs.job SET state = 'done' WHERE id = $1", first)
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        again = await u.jobs.insert(_job("media.process", dedupe_key="file_1"))
    assert again != first  # a finished job no longer blocks its dedupe key
    other = await seed_tenant(pg_admin)
    async with repo_db.tenant_uow(other.tenant_id) as u:  # dedupe is per tenant
        assert await u.jobs.insert(_job("media.process", dedupe_key="file_1")) not in (first, again)


async def test_insert_notifies_the_jobs_channel_on_commit(
    repo_db: DB, tenant: SeededTenant, pg_database: PgTestDatabase
) -> None:
    listener = await connect(pg_database.app_dsn, expected_role=ROLE_APP)
    got: list[str] = []
    arrived = anyio.Event()

    def on_notify(con_ref: object, pid: int, channel: str, payload: object) -> None:
        got.append(f"{channel}:{payload}")
        arrived.set()

    await listener.add_listener(JOBS_CHANNEL, on_notify)
    try:
        async with repo_db.tenant_uow(tenant.tenant_id) as u:
            await u.jobs.insert(_job("labeling.run", dedupe_key="lrun_1"))
            await u.jobs.insert(_job("labeling.run", dedupe_key="lrun_1"))  # duplicate: no second notification
            await listener.execute("SELECT 1")  # nothing is delivered before the commit
            assert got == []
        with anyio.fail_after(5):
            await arrived.wait()
        await listener.execute("SELECT 1")
        assert got == ["jobs:labeling.run"]
    finally:
        await listener.close()


async def test_two_workers_claim_with_skip_locked_without_double_processing(
    repo_db: DB, other_db: DB, tenant: SeededTenant, pg_admin: Conn
) -> None:
    kind = f"test.claim.{new_id('k')}"
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        ids = {await u.jobs.insert(_job(kind, priority=p)) for p in [300, 100, 200] * 10}
    claimed: dict[str, list[str]] = {"w1": [], "w2": []}

    async def work(db: DB, worker: str) -> None:
        while True:
            async with db.system_uow() as calls:
                jobs = await calls.claim_jobs([kind], 3, worker, 60)
                if not jobs:
                    return
                for job in jobs:
                    assert (job.tenant_id, job.kind, job.attempt) == (tenant.tenant_id, kind, 1)
                    claimed[worker].append(job.id)
                    await calls.finish_job(job.id, worker, ok=True, error=None)

    async with anyio.create_task_group() as tg:
        tg.start_soon(work, repo_db, "w1")
        tg.start_soon(work, other_db, "w2")
    everything = claimed["w1"] + claimed["w2"]
    assert sorted(everything) == sorted(ids)  # every job exactly once
    states = await pg_admin.fetch("SELECT DISTINCT state FROM jobs.job WHERE kind = $1", kind)
    assert [r["state"] for r in states] == ["done"]


async def test_claim_order_is_priority_first(repo_db: DB, tenant: SeededTenant) -> None:
    kind = f"test.order.{new_id('k')}"
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        low = await u.jobs.insert(_job(kind, priority=200))
        high = await u.jobs.insert(_job(kind, priority=10))
    async with repo_db.system_uow() as calls:
        assert [j.id for j in await calls.claim_jobs([kind], 2, "w", 60)] == [high, low]


async def test_audit_is_append_only(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    event = {"id": new_id("aud"), "actor_type": "principal", "actor_id": "prn_1", "action": "session.delete",
             "resource_kind": "session", "resource_id": "ses_1", "request_id": "req_1"}  # fmt: skip
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        await u.audit.insert(event)
    row = await pg_admin.fetchrow("SELECT * FROM noema.audit_event WHERE id = $1", event["id"])
    assert row is not None
    assert (row["tenant_id"], row["details"], row["at"] is not None) == (tenant.tenant_id, {}, True)

    async def tamper() -> None:
        async with repo_db.tenant_uow(tenant.tenant_id) as u:
            await u.tx.conn.execute("UPDATE noema.audit_event SET action = 'x' WHERE id = $1", event["id"])

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await tamper()


async def test_project_get_hides_deleted_and_foreign_projects(
    repo_db: DB, tenant: SeededTenant, pg_admin: Conn
) -> None:
    other = await seed_tenant(pg_admin)
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        project = await u.projects.get("prj_a")
        assert project is not None and (project["tenant_id"], project["name"]) == (tenant.tenant_id, "prj_a")
        assert await u.projects.get("prj_absent") is None
    await pg_admin.execute("UPDATE noema.project SET deleted_at = now() WHERE tenant_id = $1 AND id = 'prj_b'",
                           tenant.tenant_id)  # fmt: skip
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        assert await u.projects.get("prj_b") is None
    async with repo_db.tenant_uow(other.tenant_id) as u:
        assert (await u.projects.get("prj_a") or {}).get("tenant_id") == other.tenant_id


async def test_a_claim_skips_jobs_locked_by_another_claimer(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    kind = f"test.skip.{new_id('k')}"
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        busy = await u.jobs.insert(_job(kind, priority=1))
        free = await u.jobs.insert(_job(kind, priority=2))
    async with pg_admin.transaction():  # another claimer's open transaction holds the first job's row lock
        await pg_admin.execute("SELECT id FROM jobs.job WHERE id = $1 FOR UPDATE", busy)
        with anyio.fail_after(5):  # SKIP LOCKED: no waiting on the locked row
            async with repo_db.system_uow() as calls:
                assert [j.id for j in await calls.claim_jobs([kind], 2, "w1", 60)] == [free]
    async with repo_db.system_uow() as calls:
        assert [j.id for j in await calls.claim_jobs([kind], 2, "w2", 60)] == [busy]
