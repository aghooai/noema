"""Calibrator RLS (spec-03 §3, IMPL-009 §4, IMPL-072): a tenant sees its rows and the platform rows, never another
tenant's; noema_app writes only its own tenant rows, only as fitted/accepted/rejected; the two-person CHECK holds."""

from __future__ import annotations

import uuid

import asyncpg
import pytest
from tests.fixtures.db import new_tenant_id

from noema.db.engine import PooledConnection
from noema.db.uow import PgDatabase, Tx

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]

_INSERT = (
    "INSERT INTO noema.calibrator (id, scope, tenant_id, profile_id, profile_version, question_name,"
    " question_signature, member_subset, input_view_signature, method, member_t, pooled, holdout, acceptance,"
    " reliability, status, fitted_at, approved_by, activated_by)"
    " VALUES ($1, $2, $3, 'router-default', '1', 'q', $4, '{m1,m2}', 'vs', 'temperature', '{}', '{}', '{}', '{}',"
    " '{}', $5, now(), $6, $7)"
)


def _cal_id() -> str:
    return f"cal_{uuid.uuid4().hex[:20]}"


async def _insert(
    conn: Conn | PooledConnection,
    *,
    scope: str,
    tenant: str | None,
    status: str = "fitted",
    approved_by: str | None = None,
    activated_by: list[str] | None = None,
) -> str:
    cal = _cal_id()
    await conn.execute(_INSERT, cal, scope, tenant, f"qs_{cal}", status, approved_by, activated_by)
    return cal


async def test_tenant_sees_own_and_platform_rows_only(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    a, b = new_tenant_id(), new_tenant_id()
    own = await _insert(pg_admin, scope="tenant", tenant=a)
    other = await _insert(pg_admin, scope="tenant", tenant=b)
    platform = await _insert(pg_admin, scope="platform", tenant=None, status="accepted")
    async with pg_db.tenant_uow(a) as tx:
        visible = {r["id"] for r in await tx.conn.fetch("SELECT id FROM noema.calibrator")}
    assert own in visible and platform in visible and other not in visible


async def test_app_cannot_write_platform_or_foreign_rows(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    a, b = new_tenant_id(), new_tenant_id()
    platform = await _insert(pg_admin, scope="platform", tenant=None)
    foreign = await _insert(pg_admin, scope="tenant", tenant=b)
    for scope, tenant in (("platform", None), ("tenant", b)):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            async with pg_db.tenant_uow(a) as tx:
                await _insert(tx.conn, scope=scope, tenant=tenant)
    for target in (platform, foreign):
        async with pg_db.tenant_uow(a) as tx:
            assert await tx.conn.execute("UPDATE noema.calibrator SET status = 'accepted' WHERE id = $1", target) == (
                "UPDATE 0"
            )
    statuses = await pg_admin.fetch("SELECT status FROM noema.calibrator WHERE id = ANY($1)", [platform, foreign])
    assert {r["status"] for r in statuses} == {"fitted"}


async def test_app_writes_follow_the_lifecycle_guard(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    tenant = new_tenant_id()
    async with pg_db.tenant_uow(tenant) as tx:
        fitted = await _insert(tx.conn, scope="tenant", tenant=tenant)
    for status, approved, activated in (("active", None, ["a", "b"]), ("accepted", "op_a", None)):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            async with pg_db.tenant_uow(tenant) as tx:
                await _insert(
                    tx.conn, scope="tenant", tenant=tenant, status=status, approved_by=approved, activated_by=activated
                )
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with pg_db.tenant_uow(tenant) as tx:  # self-activation, bypassing the two-person rule
            await tx.conn.execute(
                "UPDATE noema.calibrator SET status = 'active', activated_by = '{a,b}', activated_at = now()"
                " WHERE id = $1",
                fitted,
            )
    async with pg_db.tenant_uow(tenant) as tx:  # the acceptance gate: fitted -> accepted
        assert await tx.conn.execute(
            "UPDATE noema.calibrator SET status = 'accepted', accepted_at = now() WHERE id = $1", fitted
        ) == ("UPDATE 1")
    async with pg_db.tenant_uow(tenant) as tx:  # an accepted row is no longer the app's to change
        assert await tx.conn.execute("UPDATE noema.calibrator SET status = 'rejected' WHERE id = $1", fitted) == (
            "UPDATE 0"
        )
    assert await pg_admin.fetchval("SELECT status FROM noema.calibrator WHERE id = $1", fitted) == "accepted"


@pytest.mark.parametrize("activated_by", [["op_a", "op_a"], ["op_a"], None])
async def test_active_needs_two_distinct_activators(pg_admin: Conn, activated_by: list[str] | None) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(pg_admin, scope="tenant", tenant=new_tenant_id(), status="active", activated_by=activated_by)


async def test_one_active_artifact_per_binding_and_scope(pg_admin: Conn) -> None:
    tenant = new_tenant_id()
    first = _cal_id()
    await pg_admin.execute(_INSERT, first, "tenant", tenant, "qs_shared", "active", "op_a", ["op_a", "op_b"])
    with pytest.raises(asyncpg.UniqueViolationError, match="calibrator_one_active"):
        await pg_admin.execute(_INSERT, _cal_id(), "tenant", tenant, "qs_shared", "active", "op_a", ["op_a", "op_b"])
    await pg_admin.execute(_INSERT, _cal_id(), "platform", None, "qs_shared", "active", "op_a", ["op_a", "op_b"])


async def test_epochs_and_tenant_rows_are_read_only_and_scoped(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    a, b = new_tenant_id(), new_tenant_id()
    for tenant in (a, b):
        await pg_admin.execute("INSERT INTO noema.tenant (id, aws_account_id) VALUES ($1, '1')", tenant)
        await pg_admin.execute("INSERT INTO noema.calibrator_epoch (scope_key, n) VALUES ($1, 3)", tenant)
    await pg_admin.execute(
        "INSERT INTO noema.calibrator_epoch (scope_key, n) VALUES ('platform', 0) ON CONFLICT DO NOTHING"
    )
    async with pg_db.tenant_uow(a) as tx:
        keys = {r["scope_key"] for r in await tx.conn.fetch("SELECT scope_key FROM noema.calibrator_epoch")}
        tenants = [r["id"] for r in await tx.conn.fetch("SELECT id FROM noema.tenant")]
    assert keys == {"platform", a}
    assert tenants == [a]
    for statement in ("UPDATE noema.calibrator_epoch SET n = n + 1", "UPDATE noema.tenant SET deleted_at = now()"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
            async with pg_db.tenant_uow(a) as tx:
                await tx.conn.execute(statement)
