"""ops.outbox_apply / ops.outbox_dead (spec-03 §4, spec-11 §3; IMPL-022): idempotent per-kind applies, per-record
subtransactions with value-free error codes, the guarded turn_finalize and the labeling_offer job."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import asyncpg
import pytest
from tests.fixtures.db import new_tenant_id
from tests.integration.db._rows import seed_parents

from noema.core.protocols.storage import OutboxKind, OutboxRecord
from noema.core.values import JsonValue
from noema.db.uow import PgDatabase, Tx

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]
AT = "2026-10-10T08:00:00Z"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _record(kind: OutboxKind, tenant: str, rid: str, payload: Mapping[str, JsonValue]) -> OutboxRecord:
    return OutboxRecord(kind=kind, id=rid, tenant_id=tenant, payload=payload)


def _usage(**over: JsonValue) -> dict[str, JsonValue]:
    row: dict[str, JsonValue] = {
        "project_id": "prj_a", "line": "generation", "model_id": "us.anthropic.claude-x", "region": "us-east-1",
        "input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
        "reasoning_tokens": 0, "tokens_estimated": False, "cost_usd": "0.001234", "cost_estimated": False, "at": AT,
    }  # fmt: skip
    return {**row, **over}


def _decision(rid: str, **over: JsonValue) -> dict[str, JsonValue]:
    row: dict[str, JsonValue] = {
        "id": rid, "project_id": "prj_a", "created_at": AT, "principal_id": "prn_prj_a", "purpose": "decision",
        "origin": "native", "output_format": "native", "profile_id": "lmd", "profile_version": "1",
        "question_signature": "qs", "input_view_signature": "vs", "input_digest": "dg", "status": "completed",
        "cache_hit": False, "questions": [{"name": "q"}], "input": None, "result": {"answers": []},
    }  # fmt: skip
    return {**row, **over}


def _turn(rid: str, *, session_id: str | None, seq: int | None) -> dict[str, JsonValue]:
    return {
        "id": rid, "project_id": "prj_a", "session_id": session_id, "seq": seq, "principal_id": "prn_prj_a",
        "status": "completed", "content_policy": "full", "input_parts": [{"type": "text", "text": "hi"}],
        "output_text": "hello", "routing": {}, "usage": {"input_tokens": 1}, "timings": {}, "created_at": AT,
        "completed_at": AT,
    }  # fmt: skip


def _finalize(tenant: str, turn_id: str, lease: Mapping[str, JsonValue] | None, seq: int | None) -> OutboxRecord:
    session = None if lease is None else lease["session_id"]
    attempt: dict[str, JsonValue] = {
        "id": _id("att"), "turn_id": turn_id, "route_id": "r1", "model": "m", "spec_signature": "sig",
        "outcome": "committed", "usage": {}, "cost_usd": "0.5", "cost_estimated": False, "started_at": AT,
        "ended_at": AT,
    }  # fmt: skip
    payload: dict[str, JsonValue] = {
        "lease": None if lease is None else dict(lease),
        "turn": _turn(turn_id, session_id=session if isinstance(session, str) else None, seq=seq),
        "attempts": [attempt],
        "decisions": [_decision(_id("dec"), turn_id=turn_id, purpose="router", origin="turn")],
        "turn_files": [{"turn_id": turn_id, "part_index": 0, "file_id": "file_prj_a"}],
        "sticky_spec": {"model_alias": "sonnet", "effort": "low"},
    }
    return OutboxRecord(kind="turn_finalize", id=turn_id, tenant_id=tenant, payload=payload,
                        session_id=session if isinstance(session, str) else None)  # fmt: skip


async def test_rows_apply_once_and_replays_are_no_ops(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant = new_tenant_id()
    usage_id, audit_id, decision_id = _id("use"), _id("aud"), _id("dec")
    records = [
        _record("usage_line", tenant, usage_id, _usage()),
        _record("audit_event", tenant, audit_id, {"actor_type": "principal", "actor_id": "prn_1",
                                                  "action": "turn.create", "resource_kind": "turn"}),
        _record("decision_row", tenant, decision_id, _decision(decision_id)),
    ]  # fmt: skip
    async with pg_db.system_uow() as calls:
        assert await calls.outbox_apply(records) == (None, None, None)
        assert await calls.outbox_apply(records) == (None, None, None)
        assert await calls.outbox_apply([]) == ()
    usage = await pg_admin.fetchrow("SELECT cost_usd::text AS cost, day::text AS day FROM noema.usage_line"
                                    " WHERE tenant_id = $1", tenant)  # fmt: skip
    assert usage is not None
    assert (usage["cost"], usage["day"]) == ("0.001234", "2026-10-10")
    audit = await pg_admin.fetchrow("SELECT details, at FROM noema.audit_event WHERE tenant_id = $1", tenant)
    assert audit is not None
    assert audit["details"] == {} and audit["at"] is not None
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.decision WHERE tenant_id = $1", tenant) == 1


async def test_records_fail_independently_with_codes_that_carry_no_values(
    pg_admin: Conn, pg_db: PgDatabase[Tx]
) -> None:
    tenant, other = new_tenant_id(), new_tenant_id()
    good = _id("use")
    secret = "s3cr3t-value-never-in-errors"
    records = [
        _record("usage_line", tenant, good, _usage()),
        _record("usage_line", tenant, _id("use"), _usage(tenant_id=other)),
        _record("usage_line", tenant, _id("use"), _usage(id="another-id")),
        OutboxRecord(kind="bogus", id=_id("x"), tenant_id=tenant, payload={}),  # type: ignore[arg-type]
        _record("usage_line", tenant, _id("use"), _usage(line=secret)),
        _record("usage_line", "", _id("use"), _usage()),
    ]
    async with pg_db.system_uow() as calls:
        result = await calls.outbox_apply(records)
    assert result == (None, "tenant_mismatch", "id_mismatch", "unknown_kind", "sqlstate_23514:usage_line_line_check",
                      "invalid_record")  # fmt: skip
    assert not any(secret in (code or "") for code in result)
    ids = [r["id"] for r in await pg_admin.fetch("SELECT id FROM noema.usage_line WHERE tenant_id = $1", tenant)]
    assert ids == [good]


async def _leased_session(admin: Conn) -> tuple[str, str]:
    tenant = new_tenant_id()
    await seed_parents(admin, tenant)
    await admin.execute(
        "UPDATE noema.session SET active_turn_id = 'turn_lease', lease_expires_at = now() + interval '30 s'"
        " WHERE tenant_id = $1 AND id = 'ses_prj_a'",
        tenant,
    )
    return tenant, "ses_prj_a"


async def test_turn_finalize_writes_the_turn_and_advances_the_session_once(
    pg_admin: Conn, pg_db: PgDatabase[Tx]
) -> None:
    tenant, session = await _leased_session(pg_admin)
    turn_id = _id("turn")
    record = _finalize(tenant, turn_id, {"session_id": session, "turn_id": "turn_lease", "last_seq": 0}, 1)
    async with pg_db.system_uow() as calls:
        assert await calls.outbox_apply([record]) == (None,)
        assert await calls.outbox_apply([record]) == (None,)  # a replay: applied, nothing changes
    row = await pg_admin.fetchrow(
        "SELECT last_seq, sticky_spec, active_turn_id, lease_expires_at FROM noema.session"
        " WHERE tenant_id = $1 AND id = $2",
        tenant,
        session,
    )
    assert row is not None
    assert (row["last_seq"], row["active_turn_id"], row["lease_expires_at"]) == (1, None, None)
    assert row["sticky_spec"] == {"model_alias": "sonnet", "effort": "low"}
    for table, column in (("turn", "id"), ("attempt", "turn_id"), ("decision", "turn_id"), ("turn_file", "turn_id")):
        assert (
            await pg_admin.fetchval(
                f"SELECT count(*) FROM noema.{table} WHERE tenant_id = $1 AND {column} = $2", tenant, turn_id
            )
            == 1
        ), table
    turn = await pg_admin.fetchrow("SELECT seq, redactions, degradations FROM noema.turn WHERE id = $1", turn_id)
    assert turn is not None
    assert (turn["seq"], turn["redactions"], turn["degradations"]) == (1, {"user": [], "assistant": []}, [])


async def test_turn_finalize_with_a_stale_lease_is_a_seq_conflict_and_writes_nothing(
    pg_admin: Conn, pg_db: PgDatabase[Tx]
) -> None:
    tenant, session = await _leased_session(pg_admin)
    await pg_admin.execute("UPDATE noema.session SET last_seq = 3 WHERE tenant_id = $1 AND id = $2", tenant, session)
    turn_id = _id("turn")
    stale = _finalize(tenant, turn_id, {"session_id": session, "turn_id": "turn_lease", "last_seq": 0}, 1)
    wrong_seq = _finalize(tenant, _id("turn"), {"session_id": session, "turn_id": "turn_lease", "last_seq": 3}, 7)
    async with pg_db.system_uow() as calls:
        assert await calls.outbox_apply([stale, wrong_seq]) == ("seq_conflict", "invalid_record")
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.turn WHERE tenant_id = $1 AND id = $2", tenant,
                                   turn_id) == 0  # fmt: skip
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.attempt WHERE tenant_id = $1 AND turn_id = $2",
                                   tenant, turn_id) == 0  # fmt: skip


async def test_turn_finalize_without_a_session(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant = new_tenant_id()
    await seed_parents(pg_admin, tenant)
    turn_id = _id("turn")
    async with pg_db.system_uow() as calls:
        assert await calls.outbox_apply([_finalize(tenant, turn_id, None, None)]) == (None,)
    row = await pg_admin.fetchrow("SELECT session_id, seq FROM noema.turn WHERE tenant_id = $1 AND id = $2", tenant,
                                  turn_id)  # fmt: skip
    assert row is not None
    assert (row["session_id"], row["seq"]) == (None, None)


async def test_labeling_offer_enqueues_one_job_even_when_replayed_later(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant, decision_id = new_tenant_id(), _id("dec")
    payload: dict[str, JsonValue] = {"decision_id": decision_id, "turn_id": None, "question_names": ["q"],
                                     "sample_reason": "uniform"}  # fmt: skip
    record = _record("labeling_offer", tenant, decision_id, payload)
    async with pg_db.system_uow() as calls:
        assert await calls.outbox_apply([record]) == (None,)
        job = await pg_admin.fetchrow("SELECT * FROM jobs.job WHERE tenant_id = $1", tenant)
        assert job is not None
        assert (job["kind"], job["priority"], job["dedupe_key"], job["state"]) == (
            "labeling.item", 200, decision_id, "queued"
        )  # fmt: skip
        assert job["payload"] == payload
        await pg_admin.execute("UPDATE jobs.job SET state = 'done' WHERE id = $1", job["id"])
        assert await calls.outbox_apply([record]) == (None,)
        mismatched = _record("labeling_offer", tenant, _id("dec"), payload)
        assert await calls.outbox_apply([mismatched]) == ("id_mismatch",)
    assert await pg_admin.fetchval("SELECT count(*) FROM jobs.job WHERE tenant_id = $1", tenant) == 1


async def test_outbox_dead_is_idempotent_per_kind_and_id(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant, rid = new_tenant_id(), _id("dec")
    record = _record("decision_row", tenant, rid, _decision(rid))
    async with pg_db.system_uow() as calls:
        await calls.outbox_dead(record, "sqlstate_23503", attempts=10)
        await calls.outbox_dead(record, "again", attempts=11)
        await calls.outbox_dead(_record("labeling_offer", tenant, rid, {"decision_id": rid}), "x")
    rows = await pg_admin.fetch(
        "SELECT kind, error, attempts FROM ops.outbox_dead_letter WHERE id = $1 ORDER BY 1", rid
    )
    assert [(r["kind"], r["error"], r["attempts"]) for r in rows] == [
        ("decision_row", "sqlstate_23503", 10), ("labeling_offer", "x", 0)
    ]  # fmt: skip
