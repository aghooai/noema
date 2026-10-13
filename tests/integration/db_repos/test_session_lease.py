"""The session ordering lease (spec-02 §7.1 SessionRepo, spec-04 §2, §10; IMPL-022): acquire/renew/finalize/release,
Busy, session_conflict, session_not_found, LeaseLost, lease expiry and the two-acquirer races (two pools)."""

from __future__ import annotations

from datetime import timedelta

import anyio
import pytest
from tests.fakes.clock import FakeClock
from tests.fixtures.db import SeededTenant, seed_tenant
from tests.integration.db_repos._rows import (
    Conn,
    attempt_row,
    decision_row,
    principal,
    session_row,
    turn_row,
)

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Busy, LeaseLost, SessionLease
from noema.db.repos import PgUnitOfWork
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration
DB = PgDatabase[PgUnitOfWork]


async def _session(db: DB, tenant: SeededTenant) -> str:
    async with db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        return str((await u.sessions.create(session_row()))["id"])


async def _acquire(
    db: DB, tenant: SeededTenant, sid: str, turn: str, expected: int | None = None
) -> SessionLease | Busy:
    async with db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        return await u.sessions.acquire(sid, turn, expected_seq=expected, lease_s=30)


async def _expire(admin: Conn, sid: str) -> None:
    await admin.execute(
        "UPDATE noema.session SET lease_expires_at = now() - interval '1 second' WHERE id = $1 AND active_turn_id"
        " IS NOT NULL",
        sid,
    )


async def test_acquire_busy_reacquire_and_release(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    sid = await _session(repo_db, tenant)
    lease = await _acquire(repo_db, tenant, sid, "turn_a")
    assert lease == SessionLease(sid, "turn_a", 0, None)
    row = await pg_admin.fetchrow(
        "SELECT active_turn_id, lease_expires_at - now() AS left FROM noema.session WHERE id = $1", sid
    )
    assert row is not None
    assert row["active_turn_id"] == "turn_a" and timedelta(seconds=29) < row["left"] <= timedelta(seconds=30)
    assert isinstance(await _acquire(repo_db, tenant, sid, "turn_b"), Busy)
    assert await _acquire(repo_db, tenant, sid, "turn_a") == lease  # a retried acquire of the holder is idempotent
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        await u.sessions.release(SessionLease(sid, "turn_b", 0, None))  # not the holder: no-op
        assert (await u.sessions.get(sid) or {})["active_turn_id"] == "turn_a"
        await u.sessions.release(lease)
        assert (await u.sessions.get(sid) or {})["active_turn_id"] is None
    assert isinstance(await _acquire(repo_db, tenant, sid, "turn_b"), SessionLease)


async def test_missing_deleted_and_foreign_sessions_are_not_found(
    repo_db: DB, tenant: SeededTenant, pg_admin: Conn
) -> None:
    sid = await _session(repo_db, tenant)
    other = await seed_tenant(pg_admin)
    for t, project, session in ((tenant, "prj_a", "ses_missing"), (tenant, "prj_b", sid), (other, "prj_a", sid)):
        async with repo_db.uow(principal(t.tenant_id, project)) as u:
            with pytest.raises(NoemaError) as err:
                await u.sessions.acquire(session, "turn_x", expected_seq=None, lease_s=30)
            assert err.value.code is ErrorCode.SESSION_NOT_FOUND
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        await u.sessions.mark_deleted(sid)
        with pytest.raises(NoemaError) as err:
            await u.sessions.acquire(sid, "turn_x", expected_seq=0, lease_s=30)
        assert err.value.code is ErrorCode.SESSION_NOT_FOUND


async def test_expected_seq_mismatch_is_a_conflict_even_while_busy(repo_db: DB, tenant: SeededTenant) -> None:
    sid = await _session(repo_db, tenant)
    with pytest.raises(NoemaError) as err:
        await _acquire(repo_db, tenant, sid, "turn_a", expected=3)
    assert (err.value.code, err.value.details) == (ErrorCode.SESSION_CONFLICT, {"last_seq": 0})
    assert isinstance(await _acquire(repo_db, tenant, sid, "turn_a", expected=0), SessionLease)
    with pytest.raises(NoemaError) as err:
        await _acquire(repo_db, tenant, sid, "turn_b", expected=1)
    assert err.value.code is ErrorCode.SESSION_CONFLICT
    assert isinstance(await _acquire(repo_db, tenant, sid, "turn_b", expected=0), Busy)


async def test_finalize_consumes_the_next_seq_and_stores_everything(
    repo_db: DB, tenant: SeededTenant, pg_admin: Conn
) -> None:
    sid = await _session(repo_db, tenant)
    lease = await _acquire(repo_db, tenant, sid, "turn_1")
    assert isinstance(lease, SessionLease)
    turn = turn_row()
    attempts = [attempt_row(), attempt_row(outcome="cancelled", cancel_reason="lost_race")]
    router = decision_row(purpose="router", origin="turn", turn_id=turn["id"], session_id=sid)
    sticky = {"model_alias": "fast", "effort": "low"}
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a", "tenant_admin")) as u:  # tenant-wide uow
        seq = await u.sessions.finalize_turn(
            lease, {**turn, "session_id": "ignored", "seq": 99}, attempts, [router],
            [{"part_index": 0, "file_id": "file_1", "turn_id": "ignored"}], sticky,
        )  # fmt: skip
    assert seq == 1
    stored = await pg_admin.fetchrow("SELECT * FROM noema.turn WHERE id = $1", turn["id"])
    assert stored is not None
    assert (stored["session_id"], stored["seq"], stored["project_id"]) == (sid, 1, "prj_a")  # project of the session
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.attempt WHERE turn_id = $1", turn["id"]) == 2
    assert await pg_admin.fetchval("SELECT project_id FROM noema.decision WHERE id = $1", router["id"]) == "prj_a"
    assert await pg_admin.fetchval("SELECT turn_id FROM noema.turn_file WHERE file_id = 'file_1'") == turn["id"]
    session = await pg_admin.fetchrow("SELECT * FROM noema.session WHERE id = $1", sid)
    assert session is not None
    assert (session["last_seq"], session["sticky_spec"]) == (1, sticky)
    assert session["active_turn_id"] is None and session["lease_expires_at"] is None
    nxt = await _acquire(repo_db, tenant, sid, "turn_next", expected=1)
    assert nxt == SessionLease(sid, "turn_next", 1, sticky)


async def test_renew_extends_only_the_holders_lease(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    sid = await _session(repo_db, tenant)
    lease = await _acquire(repo_db, tenant, sid, "turn_a")
    assert isinstance(lease, SessionLease)
    await _expire(pg_admin, sid)  # renewal still works while nobody took the lease over
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        assert await u.sessions.renew(lease, 30)
        assert not await u.sessions.renew(SessionLease(sid, "turn_other", 0, None), 30)
        with pytest.raises(ValueError, match="lease_s"):
            await u.sessions.renew(lease, 0)
    left = await pg_admin.fetchval("SELECT lease_expires_at - now() FROM noema.session WHERE id = $1", sid)
    assert left > timedelta(seconds=29)


async def test_expired_lease_is_taken_over_and_the_old_holder_loses_it(
    repo_db: DB, tenant: SeededTenant, pg_admin: Conn
) -> None:
    sid = await _session(repo_db, tenant)
    old = await _acquire(repo_db, tenant, sid, "turn_old")
    assert isinstance(old, SessionLease)
    await _expire(pg_admin, sid)
    new = await _acquire(repo_db, tenant, sid, "turn_new", expected=0)
    assert new == SessionLease(sid, "turn_new", 0, None)
    dev = principal(tenant.tenant_id, "prj_a")
    async with repo_db.uow(dev) as u:
        assert not await u.sessions.renew(old, 30)
        await u.sessions.release(old)  # no-op: the new holder keeps the lease
    async with repo_db.uow(dev) as u:
        assert await u.sessions.finalize_turn(new, turn_row(), [], [], [], None) == 1
    orphan = turn_row()
    with pytest.raises(LeaseLost):  # the old holder's seq 1 is taken: LeaseLost, never a unique violation
        async with repo_db.uow(dev) as u:
            await u.sessions.finalize_turn(old, orphan, [attempt_row()], [], [], {"x": 1})
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.turn WHERE id = $1", orphan["id"]) == 0
    assert await pg_admin.fetchval("SELECT last_seq FROM noema.session WHERE id = $1", sid) == 1


async def test_lease_lost_rolls_back_the_whole_unit_of_work(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    sid = await _session(repo_db, tenant)
    lease = await _acquire(repo_db, tenant, sid, "turn_a")
    assert isinstance(lease, SessionLease)
    dev = principal(tenant.tenant_id, "prj_a")
    decision = decision_row()

    async def finalize_with_a_stale_lease() -> None:
        async with repo_db.uow(dev) as u:
            await u.decisions.insert(decision)  # an earlier write of the same uow
            await u.sessions.finalize_turn(SessionLease(sid, "turn_a", 7, None), turn_row(), [], [], [], None)

    with pytest.raises(LeaseLost):
        await finalize_with_a_stale_lease()
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.decision WHERE id = $1", decision["id"]) == 0
    async with repo_db.uow(dev) as u:  # the real lease is untouched and still finalizes
        assert await u.sessions.finalize_turn(lease, turn_row(), [], [], [], None) == 1


async def test_an_expired_lease_nobody_took_still_finalizes(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    sid = await _session(repo_db, tenant)
    lease = await _acquire(repo_db, tenant, sid, "turn_a")
    assert isinstance(lease, SessionLease)
    await _expire(pg_admin, sid)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        assert await u.sessions.finalize_turn(lease, turn_row(), [], [], [], None) == 1


async def test_a_session_deleted_mid_turn_loses_the_lease(repo_db: DB, tenant: SeededTenant) -> None:
    sid = await _session(repo_db, tenant)
    lease = await _acquire(repo_db, tenant, sid, "turn_a")
    assert isinstance(lease, SessionLease)
    dev = principal(tenant.tenant_id, "prj_a")
    async with repo_db.uow(dev) as u:
        await u.sessions.mark_deleted(sid)
        assert not await u.sessions.renew(lease, 30)
    with pytest.raises(LeaseLost):
        async with repo_db.uow(dev) as u:
            await u.sessions.finalize_turn(lease, turn_row(), [], [], [], None)


async def test_lease_seconds_must_be_positive(repo_db: DB, tenant: SeededTenant) -> None:
    sid = await _session(repo_db, tenant)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        with pytest.raises(ValueError, match="lease_s"):
            await u.sessions.acquire(sid, "turn_a", expected_seq=None, lease_s=0)


async def test_two_acquirers_race_exactly_one_wins(repo_db: DB, other_db: DB, tenant: SeededTenant) -> None:
    for _ in range(5):
        sid = await _session(repo_db, tenant)
        results: dict[str, SessionLease | Busy] = {}

        async def take(db: DB, turn: str, sid: str = sid, results: dict[str, SessionLease | Busy] = results) -> None:
            results[turn] = await _acquire(db, tenant, sid, turn)

        async with anyio.create_task_group() as tg:
            for i in range(4):
                tg.start_soon(take, repo_db if i % 2 else other_db, f"turn_{i}")
        leases = [r for r in results.values() if isinstance(r, SessionLease)]
        assert len(leases) == 1 and sum(isinstance(r, Busy) for r in results.values()) == 3


_LOCK_WAITERS = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"


async def _wait_for_lock_wait(admin: Conn) -> None:
    """Until a backend of this database waits on a lock (polls pg_stat_activity; bounded by the timeout)."""
    with anyio.fail_after(10):
        while not await admin.fetchval(_LOCK_WAITERS):  # noqa: ASYNC110 (the condition lives in Postgres)
            await anyio.sleep(0.01)


async def test_an_acquirer_blocked_behind_an_open_acquire_sees_busy_after_commit(
    repo_db: DB, other_db: DB, tenant: SeededTenant, pg_admin: Conn
) -> None:
    sid = await _session(repo_db, tenant)
    dev = principal(tenant.tenant_id, "prj_a")
    second: list[SessionLease | Busy] = []

    async def blocked() -> None:
        second.append(await _acquire(other_db, tenant, sid, "turn_b"))

    async with anyio.create_task_group() as tg, repo_db.uow(dev) as u:
        assert isinstance(await u.sessions.acquire(sid, "turn_a", expected_seq=None, lease_s=30), SessionLease)
        tg.start_soon(blocked)
        await _wait_for_lock_wait(pg_admin)
        assert second == []  # still waiting for our row lock
        # the uow committed turn_a's lease; the blocked UPDATE re-evaluates and finds the session held
    assert len(second) == 1 and isinstance(second[0], Busy)


async def test_a_busy_session_stays_busy_for_the_whole_wait(repo_db: DB, tenant: SeededTenant) -> None:
    """The caller's FIFO wait (turns/session_order.py) polls `acquire` until its queue timeout and then raises
    409 session_busy; every poll inside the window must see Busy, and the first poll after a release wins."""
    sid = await _session(repo_db, tenant)
    holder = await _acquire(repo_db, tenant, sid, "turn_holder")
    assert isinstance(holder, SessionLease)
    clock = FakeClock()
    deadline = clock.monotonic() + 2.0  # platform.sessions.queue_timeout_ms

    async def wait_for_lease(turn: str) -> SessionLease:
        while clock.monotonic() < deadline:
            result = await _acquire(repo_db, tenant, sid, turn)
            if isinstance(result, SessionLease):
                return result
            await clock.advance(0.05)  # the 50 ms poll, in virtual time
        raise NoemaError(ErrorCode.SESSION_BUSY, "The session is busy.")

    with pytest.raises(NoemaError) as err:
        await wait_for_lease("turn_waiter")
    assert err.value.code is ErrorCode.SESSION_BUSY
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        await u.sessions.release(holder)
    deadline = clock.monotonic() + 2.0
    assert (await wait_for_lease("turn_waiter")).turn_id == "turn_waiter"
