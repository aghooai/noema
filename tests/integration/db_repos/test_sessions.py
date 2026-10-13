"""SessionRepo rows (spec-02 §7.1): create/get/update/list/mark_deleted/delete, RLS scoping and keyset pages."""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest
from tests.fixtures.db import SeededTenant, seed_tenant
from tests.integration.db_repos._rows import Conn, principal, session_row

from noema.core.errors import ErrorCode, NoemaError
from noema.db.repos import PgUnitOfWork
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration


async def test_create_fills_defaults_and_scope(repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant) -> None:
    dev = principal(tenant.tenant_id, "prj_a")
    async with repo_db.uow(dev) as u:
        row = await u.sessions.create(session_row(metadata={"k": "v"}))
    assert row["tenant_id"] == tenant.tenant_id
    assert row["project_id"] == "prj_a"
    assert (row["retention"], row["last_seq"], row["metadata"]) == ("default", 0, {"k": "v"})
    assert row["active_turn_id"] is None and row["lease_expires_at"] is None and row["deleted_at"] is None
    assert isinstance(row["created_at"], datetime) and row["created_at"].tzinfo is not None
    async with repo_db.uow(dev) as u:
        assert await u.sessions.get(str(row["id"])) == row


async def test_get_hides_other_tenants_projects_and_deleted(
    repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant, pg_admin: Conn
) -> None:
    other = await seed_tenant(pg_admin)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        sid = str((await u.sessions.create(session_row()))["id"])
    async with repo_db.uow(principal(other.tenant_id, "prj_a")) as u:
        assert await u.sessions.get(sid) is None  # another tenant
        with pytest.raises(NoemaError) as err:
            await u.sessions.update(sid, metadata={})
        assert err.value.code is ErrorCode.SESSION_NOT_FOUND
    async with repo_db.uow(principal(tenant.tenant_id, "prj_b")) as u:
        assert await u.sessions.get(sid) is None  # another project
    async with repo_db.uow(principal(tenant.tenant_id, "prj_b", "tenant_admin")) as u:
        assert await u.sessions.get(sid) is not None  # tenant-wide
        await u.sessions.mark_deleted(sid)
        assert await u.sessions.get(sid) is None
        with pytest.raises(NoemaError) as err:
            await u.sessions.update(sid, metadata={})
        assert err.value.code is ErrorCode.SESSION_NOT_FOUND


async def test_create_in_another_project_is_an_rls_violation(
    repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
            await u.sessions.create(session_row(project_id="prj_b"))


async def test_update_sets_only_the_updatable_columns(repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant) -> None:
    expires = datetime(2027, 1, 1, tzinfo=UTC)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        sid = str((await u.sessions.create(session_row()))["id"])
        row = await u.sessions.update(sid, metadata={"a": 1}, data_class="confidential", expires_at=expires)
        assert (row["metadata"], row["data_class"], row["expires_at"]) == ({"a": 1}, "confidential", expires)
        assert await u.sessions.update(sid) == row
        with pytest.raises(ValueError, match="last_seq"):
            await u.sessions.update(sid, last_seq=5)


async def test_list_is_id_desc_with_owner_filter_and_keyset_pages(
    repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant
) -> None:
    dev = principal(tenant.tenant_id, "prj_a")
    async with repo_db.uow(dev) as u:
        ids = [str((await u.sessions.create(session_row(owner=f"prn_{i % 2}")))["id"]) for i in range(7)]
        await u.sessions.mark_deleted(ids[3])
    live = [i for i in reversed(ids) if i != ids[3]]
    seen: list[str] = []
    after: str | None = None
    async with repo_db.uow(dev) as u:
        while True:
            page = await u.sessions.list(owner_principal_id=None, after=after, limit=2)
            seen += [str(r["id"]) for r in page.data]
            assert page.has_more == (page.next_cursor is not None)
            if not page.has_more:
                break
            after = page.next_cursor
        assert seen == live
        owned = await u.sessions.list(owner_principal_id="prn_0", after=None, limit=200)
        assert [r["id"] for r in owned.data] == [i for i in live if ids.index(i) % 2 == 0]
        assert not owned.has_more and owned.next_cursor is None


async def test_list_rejects_bad_limits_and_cursors(repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant) -> None:
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        for limit in (0, 201):
            with pytest.raises(NoemaError) as err:
                await u.sessions.list(owner_principal_id=None, after=None, limit=limit)
            assert (err.value.code, err.value.param) == (ErrorCode.SCHEMA_VIOLATION, "limit")
        for cursor in ("!!!", "bm90IGpzb24", "WzFd", "WyJhIiwiYiJd"):  # junk, not JSON, [1], ["a","b"]
            with pytest.raises(NoemaError) as err:
                await u.sessions.list(owner_principal_id=None, after=cursor, limit=10)
            assert (err.value.code, err.value.param) == (ErrorCode.SCHEMA_VIOLATION, "after")


async def test_delete_removes_the_row_after_its_turns(
    repo_db: PgDatabase[PgUnitOfWork], tenant: SeededTenant, pg_admin: Conn
) -> None:
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        sid = str((await u.sessions.create(session_row(project_id="prj_a")))["id"])
        await u.sessions.mark_deleted(sid)
        await u.sessions.delete(sid)
        await u.sessions.delete(sid)  # absent: no-op
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.session WHERE id = $1", sid) == 0
