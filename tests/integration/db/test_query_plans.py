"""Hot queries use their index on a seeded database (spec-03 §6): EXPLAIN with sequential scans disabled must name
the expected index (so an index that serves the query exists and the planner can use it under RLS)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
from tests.fixtures.db import PgTestDatabase, new_tenant_id
from tests.integration.db._rows import seed_parents

from noema.db.engine import PooledConnection, connect
from noema.db.uow import PgDatabase, Tx

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]
N = 2000

_SEED = [
    "INSERT INTO auth.api_key (key_id, tenant_id, project_id, principal_id, key_hash, pepper_version, env, scopes)"
    " SELECT 'K' || $1 || i, $1, 'prj_a', 'prn_prj_a', 'h', 1, 'live', '{turns}' FROM generate_series(1, $2) i",
    "INSERT INTO noema.session (tenant_id, project_id, id, owner_principal_id, data_class, expires_at)"
    " SELECT $1, 'prj_a', 'ses_bulk_' || i, 'prn_prj_a', 'internal', now() + i * interval '1 hour'"
    " FROM generate_series(1, $2) i",
    "INSERT INTO noema.turn (tenant_id, project_id, id, session_id, seq, principal_id, status, content_policy, routing,"
    " usage, timings, created_at, completed_at, expires_at)"
    " SELECT $1, 'prj_a', 'turn_bulk_' || i, 'ses_bulk_' || (i % 50 + 1), i, 'prn_prj_a', 'completed', 'full', '{}',"
    " '{}', '{}', now(), now(), now() + i * interval '1 hour' FROM generate_series(1, $2) i",
    "INSERT INTO noema.decision (tenant_id, project_id, id, created_at, principal_id, purpose, origin, output_format,"
    " profile_id, profile_version, question_signature, input_view_signature, input_digest, status, cache_hit,"
    " questions, result, expires_at) SELECT $1, 'prj_a', 'dec_bulk_' || i, now(), 'prn_prj_a', 'decision', 'native',"
    " 'native', 'p', '1', 'q', 'v', 'd', 'completed', false, '[]', '{}', now() + i * interval '1 hour'"
    " FROM generate_series(1, $2) i",
    "INSERT INTO noema.file (tenant_id, project_id, id, principal_id, origin, declared_type, bytes, sha256, status,"
    " quarantine_key, expires_at) SELECT $1, 'prj_a', 'file_bulk_' || i, 'prn_prj_a', 'upload', 'image/png', 1, 's',"
    " 'ready', 'q', now() + i * interval '1 hour' FROM generate_series(1, $2) i",
    "INSERT INTO noema.idempotency_record (tenant_id, project_id, id, scope_hash, key, fingerprint, state, owner,"
    " lease_expires_at, expires_at) SELECT $1, CASE WHEN i % 2 = 0 THEN 'prj_a' ELSE 'prj_b' END, 'idem_' || i,"
    " 'scope_' || (i % 7), 'key_' || i, 'f', 'completed', 'o', now(), now() + i * interval '1 hour'"
    " FROM generate_series(1, $2) i",
    "INSERT INTO noema.usage_line (tenant_id, project_id, id, line, model_id, region, input_tokens, output_tokens,"
    " cached_input_tokens, cache_write_input_tokens, reasoning_tokens, tokens_estimated, cost_usd, cost_estimated, at)"
    " SELECT $1, CASE WHEN i % 2 = 0 THEN 'prj_a' ELSE 'prj_b' END, 'use_' || i, 'generation', 'm', 'r', 1, 1, 0, 0,"
    " 0, false, 0.01, false, timestamptz '2026-01-01' + (i % 300) * interval '1 day' FROM generate_series(1, $2) i",
    "INSERT INTO jobs.job (id, tenant_id, kind, priority, payload, state, run_after, locked_by, locked_until,"
    " updated_at) SELECT 'job_bulk_' || $1 || i, $1, 'bulk.kind', i % 3 * 100,  '{}',"
    " (ARRAY['queued', 'running', 'done', 'dead'])[i % 4 + 1], now(),"
    " CASE WHEN i % 4 = 1 THEN 'w' END, CASE WHEN i % 4 = 1 THEN now() END, now() - i * interval '1 hour'"
    " FROM generate_series(1, $2) i",
]
_TABLES = ["auth.api_key", "noema.session", "noema.turn", "noema.decision", "noema.file", "noema.idempotency_record",
           "noema.usage_line", "jobs.job"]  # fmt: skip


@pytest.fixture(scope="module")
async def seeded(pg_database: PgTestDatabase) -> AsyncIterator[str]:
    """One tenant with N rows in every hot table, analyzed (once per module)."""
    tenant = new_tenant_id()
    admin = await connect(pg_database.admin_dsn, expected_role=None)
    try:
        await seed_parents(admin, tenant)
        for statement in _SEED:
            await admin.execute(statement, tenant, N)
        for table in _TABLES:
            await admin.execute(f"ANALYZE {table}")
        yield tenant
    finally:
        await admin.close()


async def _plan(conn: Conn | PooledConnection, sql: str) -> str:
    async with conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        return "\n".join(r[0] for r in await conn.fetch(f"EXPLAIN {sql}"))


_EXPIRY = (
    "SELECT tenant_id, id FROM noema.{table} WHERE expires_at < now() + interval '1 day' ORDER BY expires_at LIMIT 1000"
)
DEFINER_SIDE = {
    "api_key_pkey": "SELECT k.* FROM auth.api_key k JOIN noema.principal p ON p.tenant_id = k.tenant_id"
    " AND p.id = k.principal_id WHERE k.key_id = 'K{t}7'",
    "job_ready": "SELECT id FROM jobs.job WHERE state = 'queued' AND run_after <= now()"
    " AND kind = ANY('{{bulk.kind}}') ORDER BY priority, run_after LIMIT 10 FOR UPDATE SKIP LOCKED",
    "job_running": "SELECT id FROM jobs.job WHERE state = 'running' AND locked_until < now() - interval '2 minutes'",
    "job_finished": "SELECT id FROM jobs.job WHERE state IN ('done', 'dead')"
    " AND updated_at < now() - interval '7 days' LIMIT 1000",
    **{f"{t}_expiry": _EXPIRY.format(table=t) for t in ("session", "turn", "decision", "file")},
    "idempotency_expiry": _EXPIRY.format(table="idempotency_record"),
}
APP_SIDE = {
    "session_pkey": "UPDATE noema.session SET active_turn_id = 'turn_x', lease_expires_at = now()"
    " WHERE tenant_id = '{t}' AND id = 'ses_bulk_9' AND (active_turn_id IS NULL OR lease_expires_at < now())",
    "turn_tenant_id_session_id_seq_key": "SELECT id, seq FROM noema.turn WHERE tenant_id = '{t}'"
    " AND session_id = 'ses_bulk_3' AND seq <= 500 ORDER BY seq DESC LIMIT 20",
    "idempotency_record_tenant_id_project_id_scope_hash_key_key": "SELECT * FROM noema.idempotency_record"
    " WHERE tenant_id = '{t}' AND project_id = 'prj_a' AND scope_hash = 'scope_2' AND key = 'key_44'",
    "usage_report": "SELECT project_id, line, sum(cost_usd) FROM noema.usage_line WHERE tenant_id = '{t}'"
    " AND project_id = 'prj_a' AND day BETWEEN '2026-02-01' AND '2026-02-07' GROUP BY 1, 2",
    "usage_report_tenant": "SELECT day, sum(cost_usd) FROM noema.usage_line WHERE tenant_id = '{t}'"
    " AND day BETWEEN '2026-02-01' AND '2026-02-07' GROUP BY 1",
}


@pytest.mark.parametrize("index", DEFINER_SIDE)
async def test_definer_side_queries_use_their_index(seeded: str, pg_admin: Conn, index: str) -> None:
    plan = await _plan(pg_admin, DEFINER_SIDE[index].format(t=seeded))
    assert index in plan, plan


@pytest.mark.parametrize("index", APP_SIDE)
async def test_app_queries_use_their_index_under_rls(seeded: str, pg_db: PgDatabase[Tx], index: str) -> None:
    async with pg_db.tenant_uow(seeded) as tx:
        plan = await _plan(tx.conn, APP_SIDE[index].format(t=seeded))
    assert index in plan, plan
    assert "Seq Scan" not in plan, plan


_KEYSET = (  # one branch of ops.retention_page after a cursor (a SECURITY DEFINER function is never inlined)
    "SELECT tenant_id, id FROM noema.{table} WHERE expires_at < now() + interval '1 day' AND (expires_at >= now()"
    " AND (expires_at, 'k' COLLATE \"C\", id COLLATE \"C\", tenant_id COLLATE \"C\") > (now(), 'k', 'x', 't'))"
    ' ORDER BY expires_at, id COLLATE "C", tenant_id COLLATE "C" LIMIT 1000'
)


@pytest.mark.parametrize("table", ["session", "turn", "decision", "file", "idempotency_record"])
async def test_retention_page_branches_range_scan_their_expiry_index(seeded: str, pg_admin: Conn, table: str) -> None:
    plan = await _plan(pg_admin, _KEYSET.format(table=table))
    index = "idempotency_expiry" if table == "idempotency_record" else f"{table}_expiry"
    assert index in plan and "expires_at >= now()" in plan, plan
