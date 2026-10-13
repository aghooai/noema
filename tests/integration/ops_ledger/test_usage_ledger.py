"""T-USAGE-1 and the persistence half of T-COST-2 (spec-11 §2, §8) on the compose Postgres: the report for every
group_by against a Python reference (keys in "C" order, null keys last, then line), keyset pages without gaps or
repeats, developer (project) vs tenant_admin/auditor (tenant) scope; usage lines recorded through the real
CostMeter -> outbox -> ops.outbox_apply land exactly once (replays are no-ops) and the report sums equal Σ lines."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example
from tests.fakes.telemetry import FakeTelemetry
from tests.fixtures.db import SeededTenant
from tests.integration.db_repos._rows import admin_insert, new_id, principal, usage_row

from noema.config.loader import load_config
from noema.core.principal import Role
from noema.core.protocols.ops import UsageLine
from noema.core.values import Cost, UsageRecord
from noema.ops.cost import LedgerCostMeter, decision_line_id
from noema.ops.outbox import WriteBehindOutbox
from noema.ops.usage import UsageQuery, UsageReporter

from .conftest import SwitchDb, SwitchRedis

pytestmark = pytest.mark.integration
COLUMNS = {"project": "project_id", "session": "session_id", "model": "model_id", "line": "line"}


async def seed_lines(admin: Any, tenant: SeededTenant) -> list[dict[str, Any]]:
    t, rows = tenant.tenant_id, []
    plan = [
        ("prj_a", "decision", "ses_b", "deepseek.v3.2", datetime(2026, 10, 1, 8, tzinfo=UTC), "0.000100"),
        ("prj_a", "decision", "ses_a", "us.openai.gpt-6-luna", datetime(2026, 10, 1, 23, 59, tzinfo=UTC), "0.25"),
        ("prj_a", "generation", "ses_a", "us.openai.gpt-6.1-sol", datetime(2026, 10, 2, tzinfo=UTC), "1.000001"),
        ("prj_a", "waste", None, "deepseek.v3.2", datetime(2026, 10, 2, tzinfo=UTC), "0.000003"),
        ("prj_a", "cache_savings", None, None, datetime(2026, 10, 3, tzinfo=UTC), "0.5"),
        ("prj_b", "verification", "Ses_Z", "us.anthropic.claude-haiku-5-5", datetime(2026, 10, 3, tzinfo=UTC), "2"),
        ("prj_b", "labeling", None, "us.anthropic.claude-haiku-5-5", datetime(2026, 10, 4, tzinfo=UTC), "0.1"),
        ("prj_a", "decision", "ses_b", "deepseek.v3.2", datetime(2026, 9, 1, tzinfo=UTC), "7"),  # out of range
    ]
    for project, line, session, model, at, cost in plan:
        estimated = line == "waste"
        row = usage_row(t, project, line, at, session_id=session, cost_usd=Decimal(cost), cost_estimated=estimated)
        if model is not None:
            row["model_id"] = model
        await admin_insert(admin, "noema.usage_line", row)
        rows.append(row)
    return rows


def reference(rows: list[dict[str, Any]], group_by: str, project: str | None) -> list[tuple[str | None, str, Decimal]]:
    groups: dict[tuple[str | None, str], Decimal] = defaultdict(Decimal)
    for r in rows:
        if not date(2026, 9, 11) <= r["at"].date() <= date(2026, 10, 10) or (project and r["project_id"] != project):
            continue
        key = r["at"].date().isoformat() if group_by == "day" else r[COLUMNS[group_by]]
        groups[(key, r["line"])] += r["cost_usd"]
    order = sorted(groups, key=lambda k: (k[0] is None, (k[0] or "").encode(), k[1].encode()))
    return [(k[0], k[1], groups[k]) for k in order]


async def pages(reporter: UsageReporter, who: Any, group_by: Any, limit: int) -> list[tuple[str | None, str, Decimal]]:
    out: list[tuple[str | None, str, Decimal]] = []
    after = None
    while True:
        query = UsageQuery(group_by, date(2026, 9, 11), date(2026, 10, 10), after, limit)
        page = await reporter.page(who, query)
        assert len(page.data) <= limit
        out += [(r["key"], r["line"], r["cost_usd"]) for r in page.data]  # type: ignore[misc]
        if not page.has_more:
            assert page.next_cursor is None
            return out
        after = page.next_cursor


@pytest.mark.parametrize("group_by", ["project", "session", "model", "day", "line"])
@pytest.mark.parametrize("limit", [1, 2, 50])
async def test_report_pages_match_the_reference(
    ledger_db: SwitchDb, tenant: SeededTenant, pg_admin: Any, group_by: str, limit: int
) -> None:
    rows = await seed_lines(pg_admin, tenant)
    reporter = UsageReporter(ledger_db)
    developer = principal(tenant.tenant_id, "prj_a")
    assert await pages(reporter, developer, group_by, limit) == reference(rows, group_by, "prj_a")
    roles: tuple[Role, ...] = ("tenant_admin", "auditor")
    for role in roles:
        wide = principal(tenant.tenant_id, "prj_a", role)
        assert await pages(reporter, wide, group_by, limit) == reference(rows, group_by, None)


async def test_recorded_lines_land_once_and_sum_exactly(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: Any, tenant: SeededTenant, pg_admin: Any
) -> None:
    catalog = {
        "version": 1,
        "entries": [
            {"model": "deepseek.v3.2", "region": "us-east-1", "effective_from": "2026-01-01",
             "usd_per_mtok": {"input": "0.27", "output": "1.1"}},
        ],
    }  # fmt: skip
    config = FakeConfigRegistry(
        load_config(patched_example({"platform": {"cost": {"prices_ref": "db:2"}}}), env="test",
                    prices=lambda v: catalog if v == 2 else None)
    )  # fmt: skip
    clock = FakeClock(wall=datetime(2026, 10, 5, 9, tzinfo=UTC))
    outbox = WriteBehindOutbox(ledger_db, valkey.real, keys, clock, FakeTelemetry().instruments)
    meter = LedgerCostMeter(clock, config, outbox, FakeTelemetry().instruments)
    decision_id = new_id("dec")
    calls = {
        "decision": [UsageRecord(1200, 8), UsageRecord(1150, 8)],
        "waste": [UsageRecord(1200, 8, tokens_estimated=True)],
    }
    lines = []
    for name, usages in calls.items():
        costs = [meter.price("deepseek-3.2", "us-east-1", u) for u in usages]  # per-call rounding
        usage = sum(usages[1:], usages[0])
        cost = Cost(sum((c.usd for c in costs), Decimal(0)), any(c.estimated for c in costs))
        lines.append(
            UsageLine(decision_line_id(decision_id, name, "deepseek.v3.2"), tenant.tenant_id, "prj_a", name,  # type: ignore[arg-type]
                      "deepseek.v3.2", "us-east-1", usage, cost, decision_id=decision_id)
        )  # fmt: skip
    for _ in range(2):  # the second round is an outbox replay of the same line ids
        for line in lines:
            meter.record(line)
        await outbox.flush(5)
    stored = await pg_admin.fetch("SELECT id FROM noema.usage_line WHERE tenant_id = $1", tenant.tenant_id)
    assert sorted(r["id"] for r in stored) == sorted(line.id for line in lines)
    report = await UsageReporter(ledger_db).page(
        principal(tenant.tenant_id, "prj_a"), UsageQuery("line", date(2026, 10, 5), date(2026, 10, 5), None, 50)
    )
    by_line = {r["line"]: r for r in report.data}
    for line in lines:
        got = by_line[line.line]
        assert got["cost_usd"] == line.cost.usd and got["input_tokens"] == line.usage.input_tokens
        assert got["cost_estimated"] is line.cost.estimated and got["tokens_estimated"] is line.usage.tokens_estimated
