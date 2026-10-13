"""IdempotencyRepo (spec-02 §7.1, spec-11 §5; IMPL-077, IMPL-085): begin's insert, lookups, the conditional
take-over (exactly one racing winner), owner-fenced renew/finish, the large-body object key, scoping."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import anyio
import pytest
from tests.fixtures.db import SeededTenant, seed_tenant
from tests.integration.db_repos._rows import Conn, new_id, principal

from noema.core.protocols.storage import StoredResponse
from noema.db.repos import IDEM_INLINE_MAX_BYTES, PgUnitOfWork, idempotency_body_key
from noema.db.uow import PgDatabase

pytestmark = pytest.mark.integration
DB = PgDatabase[PgUnitOfWork]


async def _record(db: DB, tenant: SeededTenant, *, owner: str = "pod1:req1", fingerprint: str = "fp1",
                  project: str = "prj_a") -> dict[str, Any]:  # fmt: skip
    """A fresh in_flight record (30 s lease, 24 h TTL) inserted through the repo; the stored row."""
    key = new_id("key")
    async with db.uow(principal(tenant.tenant_id, project)) as u:
        now = await u.tx.conn.fetchval("SELECT now()")
        row = {"id": new_id("idem"), "scope_hash": "sh1", "key": key, "fingerprint": fingerprint, "owner": owner,
               "lease_expires_at": now + timedelta(seconds=30), "expires_at": now + timedelta(days=1),
               "resource_id": "turn_orig"}  # fmt: skip
        assert await u.idempotency.insert_in_flight(row)
        stored = await u.idempotency.get_by_key("sh1", key)
    assert stored is not None
    return dict(stored)


async def test_insert_conflict_and_lookup(repo_db: DB, tenant: SeededTenant) -> None:
    rec = await _record(repo_db, tenant)
    assert (rec["state"], rec["project_id"], rec["resource_id"]) == ("in_flight", "prj_a", "turn_orig")
    dev = principal(tenant.tenant_id, "prj_a")
    async with repo_db.uow(dev) as u:
        clash = {**{k: rec[k] for k in ("scope_hash", "key", "fingerprint", "owner", "lease_expires_at",
                                        "expires_at")}, "id": new_id("idem")}  # fmt: skip
        assert not await u.idempotency.insert_in_flight(clash)
        assert await u.idempotency.get_by_key("sh1", "key_absent") is None
    async with repo_db.uow(principal(tenant.tenant_id, "prj_b", "tenant_admin")) as u:  # tenant-wide lookup
        assert (await u.idempotency.get_by_key("sh1", rec["key"]) or {}).get("id") == rec["id"]
    async with repo_db.uow(principal(tenant.tenant_id, "prj_b")) as u:  # another project: another record space
        assert await u.idempotency.get_by_key("sh1", rec["key"]) is None
        assert await u.idempotency.insert_in_flight({**clash, "id": new_id("idem")})


async def test_take_over_only_when_free(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    dev = principal(tenant.tenant_id, "prj_a")
    live = await _record(repo_db, tenant)
    async with repo_db.uow(dev) as u:
        assert not await u.idempotency.take_over(live["id"], owner="pod2:r", fingerprint="fp1", lease_s=30, ttl_s=60)
        await u.idempotency.finish(live["id"], owner="pod1:req1", response=StoredResponse(200, (), b"{}", None))
        assert not await u.idempotency.take_over(live["id"], owner="pod2:r", fingerprint="fp1", lease_s=30, ttl_s=60)
    cases = {
        "lapsed lease": "UPDATE noema.idempotency_record SET lease_expires_at = now() - interval '1 s' WHERE id = $1",
        "not_stored": "UPDATE noema.idempotency_record SET state = 'not_stored' WHERE id = $1",
        "expired": "UPDATE noema.idempotency_record SET state = 'completed', expires_at = now() - interval '1 s'"
        " WHERE id = $1",
    }
    for case, sql in cases.items():
        rec = await _record(repo_db, tenant)
        await pg_admin.execute(sql, rec["id"])
        async with repo_db.uow(dev) as u:
            assert await u.idempotency.take_over(rec["id"], owner="pod2:r2", fingerprint="fp2", lease_s=30,
                                                 ttl_s=3600, resource_id="turn_new"), case  # fmt: skip
            now = await u.tx.conn.fetchval("SELECT now()")
            taken = await u.idempotency.get_by_key("sh1", rec["key"])
        assert taken is not None
        assert (taken["state"], taken["owner"], taken["fingerprint"], taken["resource_id"]) == (
            "in_flight", "pod2:r2", "fp2", "turn_new")  # fmt: skip
        assert (
            taken["response_status"] is None and taken["response_body"] is None and taken["response_body_key"] is None
        )
        assert taken["lease_expires_at"] == now + timedelta(seconds=30)
        assert taken["expires_at"] == now + timedelta(seconds=3600) and taken["created_at"] == now


async def test_racing_take_overs_have_one_winner(
    repo_db: DB, other_db: DB, tenant: SeededTenant, pg_admin: Conn
) -> None:
    for _ in range(5):
        rec = await _record(repo_db, tenant)
        await pg_admin.execute("UPDATE noema.idempotency_record SET state = 'not_stored' WHERE id = $1", rec["id"])
        wins: list[str] = []

        async def take(db: DB, owner: str, rec: dict[str, Any] = rec, wins: list[str] = wins) -> None:
            async with db.uow(principal(tenant.tenant_id, "prj_a")) as u:
                if await u.idempotency.take_over(rec["id"], owner=owner, fingerprint="fp1", lease_s=30, ttl_s=60):
                    wins.append(owner)

        async with anyio.create_task_group() as tg:
            for i in range(4):
                tg.start_soon(take, repo_db if i % 2 else other_db, f"pod{i}:r")
        assert len(wins) == 1
        assert await pg_admin.fetchval("SELECT owner FROM noema.idempotency_record WHERE id = $1", rec["id"]) == wins[0]


async def test_renew_and_finish_are_owner_fenced(repo_db: DB, tenant: SeededTenant) -> None:
    rec = await _record(repo_db, tenant)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        assert await u.idempotency.renew(rec["id"], owner="pod1:req1", lease_s=30)
        assert not await u.idempotency.renew(rec["id"], owner="pod9:x", lease_s=30)
        await u.idempotency.finish(rec["id"], owner="pod9:x", response=None)  # not the owner: no-op
        assert (await u.idempotency.get_by_key("sh1", rec["key"]) or {})["state"] == "in_flight"
        response = StoredResponse(201, (("content-type", "application/json"), ("x-a", "1")), b'{"ok":true}', None)
        await u.idempotency.finish(rec["id"], owner="pod1:req1", response=response)
        await u.idempotency.finish(rec["id"], owner="pod1:req1", response=None)  # completed stays completed
        done = await u.idempotency.get_by_key("sh1", rec["key"])
        assert done is not None and done["state"] == "completed"
        assert (done["response_status"], done["response_body"], done["response_body_key"]) == (
            201,
            b'{"ok":true}',
            None,
        )
        assert done["response_headers"] == [["content-type", "application/json"], ["x-a", "1"]]
        assert not await u.idempotency.renew(rec["id"], owner="pod1:req1", lease_s=30)  # no lease once completed
    other = await _record(repo_db, tenant)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        await u.idempotency.finish(other["id"], owner="pod1:req1", response=None)
        assert (await u.idempotency.get_by_key("sh1", other["key"]) or {})["state"] == "not_stored"
        with pytest.raises(ValueError, match="lease_s"):
            await u.idempotency.renew(other["id"], owner="pod1:req1", lease_s=0)


@pytest.mark.parametrize(("size", "inline"), [(IDEM_INLINE_MAX_BYTES, True), (IDEM_INLINE_MAX_BYTES + 1, False)])
async def test_large_bodies_are_stored_by_object_key(
    repo_db: DB, tenant: SeededTenant, size: int, inline: bool
) -> None:
    rec = await _record(repo_db, tenant)
    body = b"x" * size
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        await u.idempotency.finish(rec["id"], owner="pod1:req1", response=StoredResponse(200, (), body, None))
        done = await u.idempotency.get_by_key("sh1", rec["key"])
    assert done is not None and done["state"] == "completed"
    if inline:
        assert (done["response_body"], done["response_body_key"]) == (body, None)
    else:
        key = idempotency_body_key(tenant.tenant_id, rec["id"])
        assert (done["response_body"], done["response_body_key"]) == (None, key)
        assert key == f"{tenant.tenant_id}/idempotency/{rec['id']}"


async def test_sse_responses_delete_and_scope(repo_db: DB, tenant: SeededTenant, pg_admin: Conn) -> None:
    rec = await _record(repo_db, tenant)
    other = await seed_tenant(pg_admin)
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        sse = StoredResponse(200, (("content-type", "text/event-stream"),), None, "turn_sse")
        await u.idempotency.finish(rec["id"], owner="pod1:req1", response=sse)
        done = await u.idempotency.get_by_key("sh1", rec["key"])
        assert done is not None and (done["sse_turn_id"], done["response_body"]) == ("turn_sse", None)
    async with repo_db.uow(principal(other.tenant_id, "prj_a")) as u:
        assert await u.idempotency.get_by_key("sh1", rec["key"]) is None
        assert not await u.idempotency.renew(rec["id"], owner="pod1:req1", lease_s=30)
        await u.idempotency.delete(rec["id"])  # invisible: nothing happens
    async with repo_db.uow(principal(tenant.tenant_id, "prj_a")) as u:
        await u.idempotency.delete(rec["id"])
        assert await u.idempotency.get_by_key("sh1", rec["key"]) is None
