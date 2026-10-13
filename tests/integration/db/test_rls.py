"""RLS matrix (spec-03 §3, §6; IMPL-009): as noema_app a wrong, empty or missing app.tenant_id sees no row of another
tenant and cannot write one; FORCE binds the table owner too; the restrictive project policy scopes six tables."""

from __future__ import annotations

import uuid

import asyncpg
import pytest
from tests.fixtures.db import new_tenant_id
from tests.integration.db._rows import (
    APP_CANNOT_SELECT,
    PROJECTS,
    RLS_TABLES,
    WRITE_PRIVILEGES,
    row_sql,
    seed_parents,
    tenant_column,
)

from noema.core.principal import ALL_SCOPES, Principal, Role
from noema.db.engine import Engine, PooledConnection
from noema.db.uow import PgDatabase, Tx

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]
PROJECT_TABLES = ("noema.session", "noema.turn", "noema.decision", "noema.file", "noema.usage_line",
                  "noema.idempotency_record")  # fmt: skip


# Platform rows are readable by every tenant by design (cal_read, epoch_read); "sees nothing" means no tenant row.
_TENANT_ROWS = {"noema.calibrator": "scope = 'tenant'", "noema.calibrator_epoch": "scope_key <> 'platform'"}


def _suffix(table: str) -> str:
    return f"{table.split('.')[1][:4]}_{uuid.uuid4().hex[:10]}"


async def _two_tenants(admin: Conn, table: str) -> tuple[str, str]:
    """Tenant A owns one row of `table`; tenant B owns the same parents but no such row."""
    a, b = new_tenant_id(), new_tenant_id()
    await seed_parents(admin, a)
    await seed_parents(admin, b)
    if table != "noema.tenant":
        sql, args = row_sql(table, a, PROJECTS[0], _suffix(table))
        await admin.execute(sql, *args)
    return a, b


async def _count(conn: Conn | PooledConnection, table: str, tenant: str) -> int:
    return int(await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {tenant_column(table)} = $1", tenant))


@pytest.mark.parametrize("table", RLS_TABLES)
async def test_other_tenant_rows_are_invisible_and_unwritable(
    table: str, pg_admin: Conn, pg_db: PgDatabase[Tx]
) -> None:
    a, b = await _two_tenants(pg_admin, table)
    before = await _count(pg_admin, table, a)
    col = tenant_column(table)
    allowed = WRITE_PRIVILEGES.get(table, set())
    async with pg_db.tenant_uow(b) as tx:
        if table in APP_CANNOT_SELECT:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                await _count(tx.conn, table, a)
            return
        assert await _count(tx.conn, table, a) == 0
    sql, args = row_sql(table, a, PROJECTS[0], _suffix(table))
    with pytest.raises(asyncpg.InsufficientPrivilegeError) as insert_error:
        async with pg_db.tenant_uow(b) as tx:
            await tx.conn.execute(sql, *args)
    expected = "row-level security" if "INSERT" in allowed else "permission denied"
    assert expected in str(insert_error.value)
    for verb, statement in (
        ("UPDATE", f"UPDATE {table} SET {col} = {col} WHERE {col} = $1"),
        ("DELETE", f"DELETE FROM {table} WHERE {col} = $1"),
    ):
        if verb in allowed:
            async with pg_db.tenant_uow(b) as tx:
                assert await tx.conn.execute(statement, a) == f"{verb} 0"
        else:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                async with pg_db.tenant_uow(b) as tx:
                    await tx.conn.execute(statement, a)
    if "UPDATE" in allowed:  # a visible row can't be moved to another tenant (WITH CHECK)
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            async with pg_db.tenant_uow(a) as tx:
                await tx.conn.execute(f"UPDATE {table} SET {col} = $2 WHERE {col} = $1", a, b)
    assert await _count(pg_admin, table, a) == before  # nothing written, moved or deleted


@pytest.mark.parametrize("table", [t for t in RLS_TABLES if t not in APP_CANNOT_SELECT])
async def test_missing_or_empty_tenant_setting_sees_nothing(table: str, pg_admin: Conn, pg_app_engine: Engine) -> None:
    a, _ = await _two_tenants(pg_admin, table)
    assert await _count(pg_admin, table, a) >= 1
    async with pg_app_engine.acquire() as conn:  # a pooled connection outside any unit of work: setting unset
        assert await conn.fetchval("SELECT current_setting('app.tenant_id', true)") in (None, "")
        assert await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {_TENANT_ROWS.get(table, 'true')}") == 0
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', '', true), set_config('app.project_id', '', true)")
            assert await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {_TENANT_ROWS.get(table, 'true')}") == 0


async def _execute_scoped(conn: Conn, tenant: str, sql: str, *args: object) -> None:
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant)
        await conn.execute(sql, *args)


@pytest.mark.parametrize("table", RLS_TABLES)
async def test_force_rls_binds_the_table_owner(table: str, pg_admin: Conn, pg_owner: Conn) -> None:
    a, b = await _two_tenants(pg_admin, table)
    assert (
        await pg_owner.fetchval(f"SELECT count(*) FROM {table} WHERE {_TENANT_ROWS.get(table, 'true')}") == 0
    )  # owner, no scope: nothing
    async with pg_owner.transaction():
        await pg_owner.execute("SELECT set_config('app.tenant_id', $1, true)", b)
        assert await _count(pg_owner, table, a) == 0
    sql, args = row_sql(table, a, PROJECTS[0], _suffix(table))
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        await _execute_scoped(pg_owner, b, sql, *args)


def _principal(tenant: str, project: str, *roles: Role) -> Principal:
    return Principal(tenant_id=tenant, project_id=project, principal_id="prn_x", key_id="K", roles=frozenset(roles),
                     scopes=ALL_SCOPES, request_id="req_test")  # fmt: skip


@pytest.mark.parametrize("table", PROJECT_TABLES)
async def test_project_policy_scopes_project_principals_only(table: str, pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant = new_tenant_id()
    await seed_parents(pg_admin, tenant)
    for project in PROJECTS:
        sql, args = row_sql(table, tenant, project, _suffix(table))
        await pg_admin.execute(sql, *args)
    count_sql = f"SELECT project_id, count(*) AS n FROM {table} WHERE tenant_id = $1 GROUP BY 1 ORDER BY 1"
    async with pg_db.uow(_principal(tenant, "prj_a", "developer")) as tx:
        assert [r["project_id"] for r in await tx.conn.fetch(count_sql, tenant)] == ["prj_a"]
    for role in ("tenant_admin", "auditor"):
        async with pg_db.uow(_principal(tenant, "prj_a", role)) as tx:
            assert [r["project_id"] for r in await tx.conn.fetch(count_sql, tenant)] == ["prj_a", "prj_b"]
    async with pg_db.tenant_uow(tenant) as tx:
        assert len(await tx.conn.fetch(count_sql, tenant)) == 2
    async with pg_db.tenant_uow(tenant, project_id="prj_b") as tx:
        assert [r["project_id"] for r in await tx.conn.fetch(count_sql, tenant)] == ["prj_b"]
    if "INSERT" in WRITE_PRIVILEGES.get(table, set()):
        sql, args = row_sql(table, tenant, "prj_b", _suffix(table))
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            async with pg_db.uow(_principal(tenant, "prj_a", "developer")) as tx:
                await tx.conn.execute(sql, *args)


async def test_scope_values_are_bound_never_spliced(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant = new_tenant_id()
    await seed_parents(pg_admin, tenant)
    hostile = f"{tenant}' OR '1'='1"
    async with pg_db.tenant_uow(hostile) as tx:
        assert await tx.conn.fetchval("SELECT current_setting('app.tenant_id')") == hostile
        assert await tx.conn.fetchval("SELECT count(*) FROM noema.session") == 0


async def test_audit_events_are_append_only_for_the_app(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant = new_tenant_id()
    sql, args = row_sql("noema.audit_event", tenant, PROJECTS[0], "aud_x")
    async with pg_db.tenant_uow(tenant) as tx:
        await tx.conn.execute(sql, *args)
    for statement in ("UPDATE noema.audit_event SET action = 'x'", "DELETE FROM noema.audit_event"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
            async with pg_db.tenant_uow(tenant) as tx:
                await tx.conn.execute(statement)
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.audit_event WHERE tenant_id = $1", tenant) == 1


@pytest.mark.parametrize(
    ("statement", "error"),
    [
        ("SET ROLE noema_definer", asyncpg.InsufficientPrivilegeError),
        ("SET ROLE noema_owner", asyncpg.InsufficientPrivilegeError),
        ("SET SESSION AUTHORIZATION noema_owner", asyncpg.InsufficientPrivilegeError),
        ("ALTER TABLE noema.session DISABLE ROW LEVEL SECURITY", asyncpg.InsufficientPrivilegeError),
        ("ALTER TABLE noema.session NO FORCE ROW LEVEL SECURITY", asyncpg.InsufficientPrivilegeError),
        ("CREATE POLICY open ON noema.session USING (true)", asyncpg.InsufficientPrivilegeError),
        ("DROP POLICY tenant_isolation ON noema.session", asyncpg.InsufficientPrivilegeError),
        ("CREATE TABLE noema.x (a int)", asyncpg.InsufficientPrivilegeError),
        ("CREATE TABLE public.x (a int)", asyncpg.InsufficientPrivilegeError),
    ],
)
async def test_noema_app_cannot_switch_rls_off(pg_app_engine: Engine, statement: str, error: type[Exception]) -> None:
    async with pg_app_engine.acquire() as conn:
        with pytest.raises(error):
            await conn.execute(statement)


async def test_row_security_off_errors_instead_of_bypassing(pg_admin: Conn, pg_app_engine: Engine) -> None:
    a, _ = await _two_tenants(pg_admin, "noema.session")
    async with pg_app_engine.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL row_security = off")
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            await conn.fetchval("SELECT count(*) FROM noema.session WHERE tenant_id = $1", a)
