"""FileRepo and DerivativeRepo (spec-02 §7.1, spec-03 §2): create/get/update/list/delete, derivative upserts and
the cascade, RLS scoping."""

from __future__ import annotations

import asyncpg
import pytest
from tests.fixtures.db import SeededTenant, seed_tenant
from tests.integration.db_repos._rows import Conn, file_row, new_id, principal

from noema.core.errors import ErrorCode, NoemaError
from noema.db.repos import PgUnitOfWork
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration
DB = PgDatabase[PgUnitOfWork]


def _derivative(file_id: str, kind: str, **extra: object) -> dict[str, object]:
    return {"id": new_id("der"), "file_id": file_id, "kind": kind, "status": "pending", "extractor_version": "x1",
            **extra}  # fmt: skip


async def test_file_lifecycle(repo_db: DB, tenant: SeededTenant) -> None:
    dev = principal(tenant.tenant_id, "prj_a")
    async with repo_db.uow(dev) as u:
        created = await u.files.create(file_row(session_id="ses_f"))
        fid = str(created["id"])
        assert (created["project_id"], created["media_type"], created["deleted_at"]) == ("prj_a", None, None)
        updated = await u.files.update(fid, status="ready", media_type="image/png", pages=1)
        assert (updated["status"], updated["media_type"], updated["pages"]) == ("ready", "image/png", 1)
        assert await u.files.update(fid) == updated
        rejected = await u.files.update(fid, rejection={"code": "file_rejected"})
        assert rejected["rejection"] == {"code": "file_rejected"}
        with pytest.raises(ValueError, match="sha256"):
            await u.files.update(fid, sha256="ff")
        with pytest.raises(NoemaError) as err:
            await u.files.update("file_absent", status="ready")
        assert err.value.code is ErrorCode.NOT_FOUND
        second = await u.files.create(file_row(session_id="ses_f"))
        assert [f["id"] for f in await u.files.list_for_session("ses_f")] == [fid, second["id"]]
        await u.files.update(str(second["id"]), status="deleted", deleted_at=created["created_at"])
        assert await u.files.get(str(second["id"])) is None  # deleted: hidden from reads
        assert [f["id"] for f in await u.files.list_for_session("ses_f")] == [fid]


async def test_files_are_scoped(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    other = await seed_tenant(pg_admin)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        fid = str((await u.files.create(file_row()))["id"])
        await u.derivatives.upsert(_derivative(fid, "thumbnail"))
        assert len(await u.derivatives.list_for_file(fid)) == 1
    for t, project in ((other, "prj_a"), (tenant, "prj_b")):
        async with repo_db.uow(principal(t.tenant_id, project)) as u:
            assert await u.files.get(fid) is None
            assert await u.derivatives.list_for_file(fid) == ()  # derivatives follow their file's scope
            with pytest.raises(NoemaError):
                await u.files.update(fid, status="ready")


async def test_derivative_upsert_and_cascade(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        fid = str((await u.files.create(file_row(project_id="prj_a")))["id"])
        await u.derivatives.upsert(_derivative(fid, "thumbnail"))
        await u.derivatives.upsert(_derivative(fid, "raster", width=10, height=20))
        (raster_before,) = [d for d in await u.derivatives.list_for_file(fid) if d["kind"] == "raster"]
        await u.derivatives.upsert({"id": raster_before["id"], "file_id": fid, "kind": "raster", "status": "ready",
                                    "storage_key": "k", "bytes": 5, "extractor_version": "x1"})  # fmt: skip
        listed = await u.derivatives.list_for_file(fid)
        assert [d["kind"] for d in listed] == ["raster", "thumbnail"]
        raster = listed[0]
        assert (raster["status"], raster["storage_key"], raster["bytes"]) == ("ready", "k", 5)
        assert (raster["width"], raster["height"], raster["created_at"]) == (10, 20, raster_before["created_at"])
        await u.derivatives.upsert(
            {"file_id": fid, "kind": "raster", "tenant_id": tenant.tenant_id, "status": "failed"}
        )
        assert [d["status"] for d in await u.derivatives.list_for_file(fid)] == ["failed", "pending"]
        await u.files.delete(fid)
        assert await u.derivatives.list_for_file(fid) == ()
    assert await pg_admin.fetchval("SELECT count(*) FROM noema.derivative WHERE file_id = $1", fid) == 0

    async def partial_insert() -> None:
        async with repo_db.tenant_uow(tenant.tenant_id) as u:
            await u.derivatives.upsert({"file_id": "file_any", "kind": "markdown", "status": "ready"})

    with pytest.raises(asyncpg.NotNullViolationError):  # a partial row cannot create a derivative
        await partial_insert()
    async with repo_db.tenant_uow(tenant.tenant_id) as u:
        with pytest.raises(ValueError, match="file_id and kind"):
            await u.derivatives.upsert({"id": "der_x", "status": "ready"})
