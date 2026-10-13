"""spec-09 §1: the `audit_event` outbox record of a security event is accepted by the real `ops.outbox_apply` and
lands as a content-free system row (a tenant row, or `__platform__` without a tenant)."""

from __future__ import annotations

import secrets
from typing import Any

import asyncpg
import pytest
from tests.fakes.clock import FakeClock, FakeIdGen
from tests.fakes.outbox import FakeOutbox

from noema.identity.security import ACTOR, PLATFORM_TENANT, SecurityEvents

pytestmark = pytest.mark.integration


async def test_security_audit_rows_apply_on_postgres(pg_db: Any, pg_admin: asyncpg.Connection[asyncpg.Record]) -> None:
    clock = FakeClock()
    outbox = FakeOutbox()
    security = SecurityEvents(outbox=outbox, ids=FakeIdGen(clock), clock=clock)
    tenant = f"t_sec_{secrets.token_hex(4)}"
    security.emit("account_mismatch", tenant_id=tenant, request_id="req_1", details={"source": "sts"})
    security.emit("pubsub_tenant_mismatch", tenant_id=None, details={"channel": "keys_revoked"})
    records = outbox.of_kind("audit_event")
    async with pg_db.system_uow() as calls:
        assert await calls.outbox_apply(records) == (None, None)
    rows = await pg_admin.fetch(
        "SELECT tenant_id, actor_type, actor_id, action, resource_kind, resource_id, details, request_id, at"
        " FROM noema.audit_event WHERE id = ANY($1::text[]) ORDER BY action",
        [r.id for r in records],
    )
    assert [(r["tenant_id"], r["action"], r["details"], r["request_id"]) for r in rows] == [
        (tenant, "security.account_mismatch", {"source": "sts"}, "req_1"),
        (PLATFORM_TENANT, "security.pubsub_tenant_mismatch", {"channel": "keys_revoked"}, None),
    ]
    assert {(r["actor_type"], r["actor_id"], r["resource_kind"], r["resource_id"]) for r in rows} == {
        ("system", ACTOR, "security", None)
    }
    assert all(r["at"] == clock.now() for r in rows)
