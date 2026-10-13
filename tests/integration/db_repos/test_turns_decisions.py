"""TurnRepo, AttemptRepo and DecisionRepo (spec-02 §7.1, spec-03 §2, §5): inserts, history order and pages,
cascades, the label-excerpt rule, RLS scoping."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.fixtures.db import SeededTenant, seed_tenant
from tests.integration.db_repos._rows import (
    NOW,
    Conn,
    admin_insert,
    attempt_row,
    decision_row,
    item_row,
    principal,
    run_row,
    session_row,
    turn_row,
)

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import SessionLease
from noema.db.repos import PgUnitOfWork, encode_cursor
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration
DB = PgDatabase[PgUnitOfWork]


async def _session_with_turns(db: DB, tenant: SeededTenant, n: int) -> tuple[str, list[str]]:
    dev = principal(tenant.tenant_id, "prj_a")
    async with db.uow(dev) as u:
        sid = str((await u.sessions.create(session_row()))["id"])
    turns = []
    for seq in range(n):
        turn = turn_row()
        async with db.uow(dev) as u:
            lease = await u.sessions.acquire(sid, str(turn["id"]), expected_seq=seq, lease_s=30)
            assert isinstance(lease, SessionLease)
            await u.sessions.finalize_turn(lease, turn, [attempt_row()], [], [], None)
        turns.append(str(turn["id"]))
    return sid, turns


async def test_insert_without_a_session_and_get(repo_db: DB, tenant: SeededTenant) -> None:
    turn = turn_row(session_id="ses_ignored", seq=4)
    t0 = NOW
    attempts = [attempt_row(t0 + timedelta(seconds=2)), attempt_row(t0), attempt_row(t0 + timedelta(seconds=1))]
    verification = decision_row(purpose="verification", origin="verification", turn_id=turn["id"])
    dev = principal(tenant.tenant_id, "prj_a")
    async with repo_db.uow(dev) as u:
        await u.turns.insert(turn, attempts, [verification], [{"part_index": 0, "file_id": "file_x"}])
    async with repo_db.uow(dev) as u:
        stored = await u.turns.get(str(turn["id"]))
        assert stored is not None
        assert (stored["session_id"], stored["seq"], stored["project_id"]) == (None, None, "prj_a")
        assert stored["input_parts"] == turn["input_parts"] and stored["routing"] == turn["routing"]
        assert stored["redactions"] == {"user": [], "assistant": []} and stored["degradations"] == []
        listed = await u.attempts.list_for_turn(str(turn["id"]))
        assert [a["id"] for a in listed] == [attempts[1]["id"], attempts[2]["id"], attempts[0]["id"]]
        assert listed[0]["cost_usd"] == attempts[1]["cost_usd"] and listed[0]["turn_id"] == turn["id"]
        assert await u.decisions.get(str(verification["id"])) is not None
    async with repo_db.uow(principal(tenant.tenant_id, "prj_b")) as u:
        assert await u.turns.get(str(turn["id"])) is None
        assert await u.attempts.list_for_turn(str(turn["id"])) == ()


async def test_history_pages_seq_asc_and_recent_seq_desc(repo_db: DB, tenant: SeededTenant) -> None:
    sid, turns = await _session_with_turns(repo_db, tenant, 5)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        first = await u.turns.list_for_session(sid, after=None, limit=2)
        assert [r["seq"] for r in first.data] == [1, 2] and first.has_more
        second = await u.turns.list_for_session(sid, after=first.next_cursor, limit=2)
        third = await u.turns.list_for_session(sid, after=second.next_cursor, limit=2)
        assert [r["seq"] for r in second.data + third.data] == [3, 4, 5]
        assert not third.has_more and third.next_cursor is None
        assert [r["id"] for r in first.data + second.data + third.data] == turns
        assert [r["seq"] for r in await u.turns.recent(sid, max_seq=4, limit=2)] == [4, 3]
        assert [r["seq"] for r in await u.turns.recent(sid, max_seq=10, limit=10)] == [5, 4, 3, 2, 1]
        assert await u.turns.recent(sid, max_seq=4, limit=0) == ()
        with pytest.raises(NoemaError) as err:
            await u.turns.list_for_session(sid, after="WyJ4Il0", limit=2)  # ["x"]: not a seq cursor
        assert err.value.param == "after"
        with pytest.raises(NoemaError) as err:
            await u.turns.list_for_session(sid, after=encode_cursor([2**31]), limit=2)  # beyond int4
        assert err.value.param == "after"


async def test_delete_cascades_and_delete_for_session_counts(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    sid, turns = await _session_with_turns(repo_db, tenant, 3)
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        await u.turns.delete(turns[0])
        await u.turns.delete("turn_absent")
        assert await u.turns.get(turns[0]) is None
        assert await u.turns.delete_for_session(sid) == 2
        assert await u.turns.delete_for_session(sid) == 0
        await u.sessions.delete(sid)
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.attempt WHERE turn_id = ANY($1)", turns) == 0


async def test_decisions_insert_get_delete_and_label_excerpts(
    repo_db: DB, tenant: SeededTenant, pg_admin: Conn
) -> None:
    dev = principal(tenant.tenant_id, "prj_a")
    kept = decision_row(session_id="ses_1")
    other = decision_row(session_id="ses_1")
    async with repo_db.uow(dev) as u:
        await u.decisions.insert(kept)
        await u.decisions.insert(other)
        stored = await u.decisions.get(str(kept["id"]))
        assert stored is not None and stored["questions"] == kept["questions"] and stored["project_id"] == "prj_a"
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        run = await u.labels.create_run(run_row(status="completed"))
        await u.labels.insert_item(item_row(str(run["id"]), decision_id=kept["id"]))
        await u.decisions.delete(str(kept["id"]))
        assert await u.decisions.get(str(kept["id"])) is None
        (item,) = await u.labels.items(str(run["id"]))
        assert item["content_excerpt"] is None and item["kept"]  # the label stays, its content goes
        assert await u.decisions.delete_for_session("ses_1") == 1


async def test_decisions_are_scoped(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    other = await seed_tenant(pg_admin)
    row = decision_row()
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        await u.decisions.insert(row)
    for t, project in ((other, "prj_a"), (tenant, "prj_b")):
        async with repo_db.uow(principal(t.tenant_id, project)) as u:
            assert await u.decisions.get(str(row["id"])) is None
            await u.decisions.delete(str(row["id"]))  # invisible: affects nothing
    async with repo_db.uow(principal(tenant.tenant_id, "prj_b", "auditor")) as u:
        assert await u.decisions.get(str(row["id"])) is not None


async def test_unknown_columns_are_rejected(repo_db: DB, tenant: SeededTenant) -> None:
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        with pytest.raises(ValueError, match="bogus"):
            await u.decisions.insert({**decision_row(), "bogus": 1})
        with pytest.raises(ValueError, match="day"):
            await u.turns.insert({**turn_row(), "day": 1}, [], [], [])


async def test_admin_seeded_rows_are_visible_to_the_repos(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    row = {**decision_row(), "tenant_id": tenant.tenant_id, "project_id": "prj_b"}
    await admin_insert(pg_admin, "noema.decision", row)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_b")) as u:
        assert (await u.decisions.get(str(row["id"])) or {}).get("id") == row["id"]
    with pytest.raises(NoemaError) as err:
        async with repo_db.uow(principal(tenant.tenant_id, "prj_b")) as u:
            await u.sessions.update("ses_absent", metadata={})
    assert err.value.code is ErrorCode.SESSION_NOT_FOUND
