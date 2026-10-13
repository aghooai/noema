"""SE2 (06 §9 "RLS"; spec-03 §6, extended to EVERY table): on the security stack's database, after real traffic,
the catalog is enumerated (no hand-written table list) and every table that holds a tenant column is either under
ENABLE + FORCE row-level security or not readable by `noema_app` at all. As `noema_app` with a wrong, empty, unset
or unknown `app.tenant_id`, every RLS table returns 0 rows of t_acme, and t_beta sees none of t_acme's rows; with
the right tenant the same query returns the rows the superuser counts (so the zeros are not vacuous)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final

import asyncpg
import pytest
from tests.e2e.conftest import Stack
from tests.security.artifacts import write_jsonl
from tools.bootstrap_dev import PG_SUPERUSER, dsn

from noema.core.values import JsonValue

pytestmark = [pytest.mark.e2e]

type Conn = asyncpg.Connection[asyncpg.Record]
VICTIM: Final = "t_acme"
TENANT_COLUMNS: Final = ("tenant_id", "scope_key")  # noema.tenant: its `id`
PLATFORM_ROWS: Final = {"noema.calibrator": "scope = 'platform'", "noema.calibrator_epoch": "scope_key = 'platform'"}

CATALOG: Final = """
SELECT n.nspname || '.' || c.relname AS name, c.relrowsecurity AS rls, c.relforcerowsecurity AS force,
       has_table_privilege('noema_app', c.oid, 'SELECT') AS app_select,
       array_agg(a.attname::text ORDER BY a.attnum) AS columns
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
GROUP BY c.oid, n.nspname, c.relname ORDER BY 1
"""


def tenant_column(name: str, columns: list[str]) -> str | None:
    if name == "noema.tenant":
        return "id"
    if name == "noema.calibrator_epoch":
        return "scope_key"
    return next((c for c in TENANT_COLUMNS if c in columns), None)


@pytest.fixture
async def dbs(stack: Stack) -> AsyncIterator[tuple[Conn, Conn]]:
    host = str(stack.state["endpoints"]["postgres"])
    user, password = PG_SUPERUSER
    admin = await asyncpg.connect(f"postgresql://{user}:{password}@{host}/noema")
    app = await asyncpg.connect(dsn("noema_app", host))
    try:
        yield admin, app
    finally:
        await admin.close()
        await app.close()


async def count_as_app(app: Conn, table: str, column: str, setting: str | None, where: str) -> int:
    async with app.transaction():
        if setting is not None:
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", setting)
        await app.execute("SELECT set_config('app.project_id', '', true)")
        return int(await app.fetchval(f"SELECT count(*) FROM {table} WHERE {where}", VICTIM))


async def test_every_tenant_table_is_isolated(dbs: tuple[Conn, Conn]) -> None:
    admin, app = dbs
    catalog = await admin.fetch(CATALOG)
    rows: list[JsonValue] = []
    problems: list[str] = []
    rls_tables = 0
    for entry in catalog:
        name, columns = str(entry["name"]), list(entry["columns"])
        column = tenant_column(name, columns)
        if column is None:
            rows.append({"table": name, "tenant_column": None, "app_select": entry["app_select"]})
            continue
        if not entry["app_select"]:  # e.g. auth.api_key, ops.outbox_dead_letter: definer functions only
            rows.append({"table": name, "tenant_column": column, "app_select": False})
            continue
        if not (entry["rls"] and entry["force"]):
            problems.append(f"{name}: tenant column {column} readable by noema_app without ENABLE+FORCE RLS")
            continue
        rls_tables += 1
        where = f"{column} = $1" + (f" AND NOT ({PLATFORM_ROWS[name]})" if name in PLATFORM_ROWS else "")
        real = int(await admin.fetchval(f"SELECT count(*) FROM {name} WHERE {where}", VICTIM))
        seen = {
            setting or repr(setting): await count_as_app(app, name, column, setting, where)
            for setting in ("t_beta", "", None, "t_nobody", "t_acme' OR '1'='1")
        }
        right = await count_as_app(app, name, column, VICTIM, where)
        rows.append({"table": name, "tenant_column": column, "victim_rows": real, "right_tenant": right, **seen})
        if any(seen.values()):
            problems.append(f"{name}: {seen} rows of {VICTIM} visible under a wrong tenant")
        if right != real:
            problems.append(f"{name}: the right tenant sees {right} of {real} rows (positive control)")
    write_jsonl("rls-all-tables.jsonl", rows)
    assert problems == [], problems
    assert rls_tables >= 18  # spec-03 §3's 19 RLS tables minus auth.api_key (definer functions only)


async def test_no_cross_tenant_rows_through_the_app_role(dbs: tuple[Conn, Conn]) -> None:
    """With app.tenant_id = t_beta, every readable RLS table returns only t_beta (or platform) rows."""
    admin, app = dbs
    leaked: list[str] = []
    for entry in await admin.fetch(CATALOG):
        name, columns = str(entry["name"]), list(entry["columns"])
        column = tenant_column(name, columns)
        if column is None or not entry["app_select"]:
            continue
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', 't_beta', true)")
            values = {str(v) for v in await app.fetchval(f"SELECT array_agg(DISTINCT {column}) FROM {name}") or []}
        if values - {"t_beta", "platform", "None"}:
            leaked.append(f"{name}: {sorted(values)}")
    assert leaked == [], leaked
