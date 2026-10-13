"""The repositories' own list/report statements use their indexes under RLS (spec-03 §6 "hot queries use their
index"): each statement is captured from the repo method, then EXPLAINed with sequential scans disabled."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from tests.fixtures.db import SeededTenant
from tests.integration.db_repos._rows import Conn

from noema.db.repos import PgLabelRepo, PgSessionRepo, PgUnitOfWork, PgUsageRepo
from noema.db.uow import PgDatabase, Scope, Tx

pytestmark = pytest.mark.integration
N = 2000


class _Capture:
    """Stands in for the connection: records the statement and returns no rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append((sql, args))
        return []


async def _statement(scope: Scope, call: Callable[[Tx], Awaitable[object]]) -> tuple[str, tuple[Any, ...]]:
    capture = _Capture()
    await call(Tx(capture, scope))  # type: ignore[arg-type]
    (statement,) = capture.calls
    return statement


@pytest.fixture
async def seeded(pg_admin: Conn, tenant: SeededTenant) -> str:
    t = tenant.tenant_id
    await pg_admin.execute(
        "INSERT INTO noema.usage_line (tenant_id, project_id, id, line, model_id, region, input_tokens, output_tokens,"
        " cached_input_tokens, cache_write_input_tokens, reasoning_tokens, tokens_estimated, cost_usd, cost_estimated,"
        " at) SELECT $1, CASE WHEN i % 2 = 0 THEN 'prj_a' ELSE 'prj_b' END, 'use_' || i, 'generation', 'm', 'r', 1, 1,"
        " 0, 0, 0, false, 0.01, false, timestamptz '2026-01-01' + (i % 300) * interval '1 day'"
        " FROM generate_series(1, $2) i",
        t,
        N,
    )
    await pg_admin.execute(
        "INSERT INTO noema.session (tenant_id, project_id, id, owner_principal_id, data_class)"
        " SELECT $1, 'prj_a', 'ses_p' || lpad(i::text, 6, '0'), 'prn_' || (i % 40), 'internal'"
        " FROM generate_series(1, $2) i",
        t,
        N,
    )
    await pg_admin.execute(  # 100 runs: 20 definitions (signatures) x 5 runs
        "INSERT INTO noema.labeling_run (tenant_id, id, profile_id, profile_version, question_name, question_signature,"
        " status, labelers, rule, counts, usage, started_at) SELECT $1, 'lrun_' || i, 'lmd', '1', 'q0',"
        " 'sha256:' || repeat(to_hex(i % 20), 64 / length(to_hex(i % 20))), 'completed', '{a,b}', 'unanimous', '{}',"
        " '{}', now() FROM generate_series(1, 100) i",
        t,
    )
    await pg_admin.execute(  # N items over the runs: one decision each, 3 of 4 kept, over 300 days
        "INSERT INTO noema.label_item (tenant_id, id, run_id, decision_id, question_name, sample_reason,"
        " labeler_answers, kept, value, drop_reason, ensemble, redacted, input_view_signature, member_subset,"
        " created_at) SELECT $1, 'lab_' || i, 'lrun_' || (i % 100 + 1), 'dec_' || i, 'q0', 'uniform', '[]', i % 4 > 0,"
        " CASE WHEN i % 4 > 0 THEN '\"yes\"'::jsonb END, CASE WHEN i % 4 = 0 THEN 'disagreed' END, '{}', false, 'v',"
        " '{m1}', timestamptz '2026-01-01' + (i % 300) * interval '1 day' FROM generate_series(1, $2) i",
        t,
        N,
    )
    for table in ("noema.usage_line", "noema.session", "noema.labeling_run", "noema.label_item"):
        await pg_admin.execute(f"ANALYZE {table}")
    return t


CASES: dict[str, tuple[str, Callable[[Tx], Awaitable[object]], str]] = {
    "usage_report": (
        "prj_a",
        lambda tx: PgUsageRepo(tx).report(
            group_by="model", day_from=date(2026, 2, 1), day_to=date(2026, 2, 7), after=None, limit=50
        ),
        "usage_report",
    ),
    "usage_report_tenant": (
        "",
        lambda tx: PgUsageRepo(tx).report(
            group_by="day",
            day_from=date(2026, 2, 1),
            day_to=date(2026, 2, 7),
            after="WyIyMDI2LTAyLTAyIiwiZyJd",
            limit=50,
        ),
        "usage_report_tenant",
    ),
    "session_by_owner": (
        "prj_a",
        lambda tx: PgSessionRepo(tx).list(owner_principal_id="prn_7", after=None, limit=20),
        "session_by_owner",
    ),
    "label_item_dropped": (
        "",
        lambda tx: PgLabelRepo(tx).list_dropped(run_id=None, question_name=None, reason=None, after=None, limit=20),
        "label_item_dropped",
    ),
    "label_item_by_decision": ("", lambda tx: PgLabelRepo(tx).items_for_decision("dec_7"), "label_item_by_decision"),
    "label_item_kept": (
        "",
        lambda tx: PgLabelRepo(tx).kept_items("lmd", "1", "sha256:" + "a" * 64, datetime(2026, 1, 1, tzinfo=UTC)),
        "label_item_kept",
    ),
    "labeling_run_by_definition": (
        "",
        lambda tx: PgLabelRepo(tx).kept_items("lmd", "1", "sha256:" + "a" * 64, datetime(2026, 1, 1, tzinfo=UTC)),
        "labeling_run_by_definition",
    ),
}


@pytest.mark.parametrize("case", CASES)
async def test_repo_statements_use_their_index(seeded: str, repo_db: PgDatabase[PgUnitOfWork], case: str) -> None:
    project, call, index = CASES[case]
    sql, args = await _statement(Scope(seeded, project), call)
    async with repo_db.tenant_uow(seeded, project_id=project or None) as u:
        await u.tx.conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in await u.tx.conn.fetch(f"EXPLAIN {sql}", *args))
    assert index in plan, plan
    assert "Seq Scan" not in plan, plan
