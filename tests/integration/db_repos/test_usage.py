"""UsageRepo.report (spec-11 §2, T-USAGE-1): every group_by, sums per (key, line), NULL keys last, keyset pages
without gaps or repeats, the day range, developer (project) vs tenant_admin (tenant-wide) scope."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.fixtures.db import SeededTenant
from tests.integration.db_repos._rows import Conn, admin_insert, principal, usage_row

from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.db.repos import GROUP_KEYS, PgUnitOfWork
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration
DB = PgDatabase[PgUnitOfWork]
D1, D2, D3 = (datetime(2026, 3, d, 23, 30, tzinfo=UTC) for d in (1, 2, 3))


@pytest.fixture
async def lines(pg_admin: Conn, tenant: SeededTenant) -> list[dict[str, Any]]:
    """Usage lines over two projects, three days, several sessions, models and lines (some NULL keys)."""
    rows = [
        usage_row(tenant.tenant_id, "prj_a", "generation", D1, session_id="ses_1", turn_id="turn_1"),
        usage_row(tenant.tenant_id, "prj_a", "waste", D1, session_id="ses_1", output_tokens=7),
        usage_row(tenant.tenant_id, "prj_a", "decision", D2, session_id="ses_2", tokens_estimated=True),
        usage_row(tenant.tenant_id, "prj_a", "cache_savings", D2, cost_usd=Decimal("0.004500")),
        usage_row(tenant.tenant_id, "prj_a", "generation", D2, model_id="anthropic.claude-x", cost_estimated=True),
        usage_row(tenant.tenant_id, "prj_b", "generation", D2, session_id="ses_3"),
        usage_row(tenant.tenant_id, "prj_b", "labeling", D3, labeling_run_id="lrun_1"),
        usage_row(tenant.tenant_id, "prj_b", "verification", D3, session_id="ses_3", reasoning_tokens=4),
    ]
    for row in rows:
        await admin_insert(pg_admin, "noema.usage_line", row)
    return rows


def _expected(rows: list[dict[str, Any]], group_by: str, project: str | None) -> list[dict[str, Any]]:
    column = {"project": "project_id", "session": "session_id", "model": "model_id", "line": "line"}.get(group_by)
    groups: dict[tuple[str | None, str], dict[str, Any]] = defaultdict(dict)
    for r in rows:
        if project is not None and r["project_id"] != project:
            continue
        key = r["at"].date().isoformat() if group_by == "day" else r.get(column)  # type: ignore[arg-type]
        g = groups[(key, r["line"])]
        for c in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
                  "reasoning_tokens", "cost_usd"):  # fmt: skip
            g[c] = g.get(c, 0) + r[c]
        g["cost_estimated"] = g.get("cost_estimated", False) or r["cost_estimated"]
        g["tokens_estimated"] = g.get("tokens_estimated", False) or r["tokens_estimated"]
    ordered = sorted(groups, key=lambda k: (k[0] is None, k[0] or "", k[1]))
    return [{"key": k, "line": line, **groups[(k, line)]} for k, line in ordered]


async def _all_pages(db: DB, who: Principal, group_by: str, limit: int, **days: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    after: str | None = None
    day_from, day_to = days.get("day_from", date(2026, 3, 1)), days.get("day_to", date(2026, 3, 3))
    while True:
        async with db.uow(who) as u:
            page = await u.usage.report(group_by=group_by, day_from=day_from, day_to=day_to, after=after, limit=limit)  # type: ignore[arg-type]
        out += [dict(r) for r in page.data]
        assert len(page.data) <= limit and page.has_more == (page.next_cursor is not None)
        if not page.has_more:
            return out
        after = page.next_cursor


@pytest.mark.parametrize("group_by", sorted(GROUP_KEYS))
@pytest.mark.parametrize("limit", [1, 2, 200])
async def test_report_groups_orders_and_pages(
    repo_db: DB, tenant: SeededTenant, lines: list[dict[str, Any]], group_by: str, limit: int
) -> None:
    admin = principal(tenant.tenant_id, "prj_a", "tenant_admin")
    assert await _all_pages(repo_db, admin, group_by, limit) == _expected(lines, group_by, None)
    dev = principal(tenant.tenant_id, "prj_a")
    assert await _all_pages(repo_db, dev, group_by, limit) == _expected(lines, group_by, "prj_a")


async def test_null_keys_come_last(repo_db: DB, tenant: SeededTenant, lines: list[dict[str, Any]]) -> None:
    rows = await _all_pages(repo_db, principal(tenant.tenant_id, "prj_a", "auditor"), "session", 1)
    keys = [r["key"] for r in rows]
    assert keys == ["ses_1", "ses_1", "ses_2", "ses_3", "ses_3", None, None, None]
    assert [r["line"] for r in rows] == ["generation", "waste", "decision", "generation", "verification",
                                         "cache_savings", "generation", "labeling"]  # fmt: skip


async def test_day_range_and_day_keys(repo_db: DB, tenant: SeededTenant, lines: list[dict[str, Any]]) -> None:
    admin = principal(tenant.tenant_id, "prj_a", "tenant_admin")
    rows = await _all_pages(repo_db, admin, "day", 200, day_from=date(2026, 3, 2), day_to=date(2026, 3, 2))
    assert {r["key"] for r in rows} == {"2026-03-02"}  # 23:30 UTC stays on its UTC day
    assert sum(r["cost_usd"] for r in rows) == Decimal("0.007500")
    assert await _all_pages(repo_db, admin, "day", 200, day_from=date(2026, 3, 4), day_to=date(2026, 3, 9)) == []


async def test_report_validates_limit_and_cursor(repo_db: DB, tenant: SeededTenant) -> None:
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        for after, limit, param in ((None, 0, "limit"), ("WzEsMl0", 5, "after")):  # "WzEsMl0" = [1, 2]
            with pytest.raises(NoemaError) as err:
                await u.usage.report(
                    group_by="line", day_from=date(2026, 3, 1), day_to=date(2026, 3, 1), after=after, limit=limit
                )
            assert (err.value.code, err.value.param) == (ErrorCode.SCHEMA_VIOLATION, param)
