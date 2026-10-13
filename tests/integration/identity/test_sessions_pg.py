"""Sessions on Postgres (the real repositories under RLS) through the API middleware and contract validator: CRUD,
history, the `session.delete` job, and SEC-AZ-04 for the session operations (an allow-all authorizer still yields
404 or an empty page across tenants and, for project roles, across projects: RLS is the second line)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg
import pytest
from tests.fakes.auth import FakeAuthenticator
from tests.fakes.world import FakeWorld, build_world
from tests.fixtures.db import new_tenant_id
from tests.unit.identity.sessions_rig import Caller, client
from tests.unit.identity.stored_turns import REDACTED, text_turn

from noema.core.ids import IdGen
from noema.core.principal import ALL_SCOPES, Principal, Role
from noema.core.protocols.auth import Action, ListScope, ResourceRef
from noema.core.protocols.storage import Job
from noema.core.values import JsonValue
from noema.db.engine import Engine
from noema.db.repos import PgUnitOfWork, make_unit_of_work
from noema.db.uow import PgDatabase
from noema.identity.authz import MatrixAuthorizer
from noema.identity.jobs import make_handlers

pytestmark = pytest.mark.integration
JSON = {"content-type": "application/json"}


class PgJobs:
    """`JobQueue.enqueue` as the ops-ledger queue does it (INSERT under tenant_uow + notify), for these tests."""

    def __init__(self, db: PgDatabase[PgUnitOfWork], ids: IdGen) -> None:
        self.db = db
        self.ids = ids

    async def enqueue(
        self, kind: str, tenant_id: str, payload: Mapping[str, JsonValue], *, dedupe_key: str | None = None, **_: Any
    ) -> str:
        async with self.db.tenant_uow(tenant_id) as uow:
            job_id: str = await uow.jobs.insert(
                {"id": self.ids.new("job"), "kind": kind, "payload": dict(payload), "dedupe_key": dedupe_key}
            )
        return job_id


class AllowAll:
    """SEC-AZ-04: an authorizer that allows everything (RLS must still hide other tenants and projects)."""

    def check(self, principal: Principal, action: Action, resource: ResourceRef | None = None) -> None:
        return None

    def list_scope(self, principal: Principal, action: Action) -> ListScope:
        return "tenant"


async def seed(
    admin: asyncpg.Connection[asyncpg.Record], tenant: str, project: str, who: str, roles: tuple[str, ...]
) -> None:
    await admin.execute(
        "INSERT INTO noema.tenant (id, aws_account_id) VALUES ($1, '111122223333') ON CONFLICT DO NOTHING", tenant
    )
    await admin.execute(
        "INSERT INTO noema.project (tenant_id, id, name) VALUES ($1, $2, $2) ON CONFLICT DO NOTHING", tenant, project
    )
    await admin.execute(
        "INSERT INTO noema.principal (tenant_id, project_id, id, subject, roles) VALUES ($1, $2, $3, $3, $4)",
        tenant,
        project,
        who,
        list(roles),
    )


@dataclass
class PgWorld:
    world: FakeWorld
    conn: asyncpg.Connection[asyncpg.Record]
    t_a: str
    dev: Caller
    dev2: Caller
    admin: Caller
    other: Caller


@pytest.fixture
async def pg_world(pg_app_engine: Engine, pg_admin: asyncpg.Connection[asyncpg.Record]) -> PgWorld:
    t_a, t_b = new_tenant_id(), new_tenant_id()
    callers: dict[str, Caller] = {}
    for name, tenant, project, roles in (
        ("dev", t_a, "p1", ("developer",)),
        ("dev2", t_a, "p2", ("developer",)),
        ("admin", t_a, "p2", ("tenant_admin",)),
        ("other", t_b, "p1", ("developer", "tenant_admin")),
    ):
        pid = f"prn_{name}_{tenant[-6:]}"
        await seed(pg_admin, tenant, project, pid, roles)
        roleset: frozenset[Role] = frozenset(roles)  # type: ignore[arg-type]
        callers[name] = Caller(f"tok_{name}_{tenant}", Principal(tenant, project, pid, "k", roleset, ALL_SCOPES, ""))
    world: FakeWorld = build_world()
    world.db = PgDatabase(pg_app_engine, make_unit_of_work)  # type: ignore[assignment]
    world.jobs = PgJobs(world.db, world.ids)  # type: ignore[assignment,arg-type]
    authn = FakeAuthenticator()
    for c in callers.values():
        authn.register(c.token, c.principal)
    world.authn = authn
    world.authz = MatrixAuthorizer()  # type: ignore[assignment]
    return PgWorld(world, pg_admin, t_a, callers["dev"], callers["dev2"], callers["admin"], callers["other"])


async def seed_turn(
    admin: asyncpg.Connection[asyncpg.Record],
    tenant: str,
    session_id: str,
    seq: int,
    who: str,
    content: Mapping[str, Any] | None = None,
) -> None:
    """One completed turn; `content` (policy, input_parts, output_text, redactions) defaults to a full text turn
    in the orchestrator's stored format."""
    now = datetime.now().astimezone()
    stored = content or text_turn(f"q{seq}", f"a{seq}")
    await admin.execute(
        "INSERT INTO noema.turn (tenant_id, project_id, id, session_id, seq, principal_id, status, content_policy,"
        " input_parts, output_text, redactions, routing, usage, timings, created_at, completed_at)"
        " VALUES ($1, 'p1', $2, $3, $4, $5, 'completed', $6, $7, $8, $9, '{}', '{}', '{}', $10, $10)",
        tenant,
        f"turn_{session_id[4:]}_{seq}",
        session_id,
        seq,
        who,
        stored["content_policy"],
        stored["input_parts"],
        stored["output_text"],
        stored["redactions"],
        now,
    )
    await admin.execute(
        "UPDATE noema.session SET last_seq = $3 WHERE tenant_id = $1 AND id = $2", tenant, session_id, seq
    )


async def test_sessions_on_postgres(pg_world: PgWorld) -> None:
    pw = pg_world
    world, admin, dev, dev2, adm, t_a = pw.world, pw.conn, pw.dev, pw.dev2, pw.admin, pw.t_a
    async with client(world) as http:
        created = await http.post("/v1/sessions", content='{"metadata": {"k": "v"}}', headers=dev.headers(**JSON))
        assert created.status_code == 201
        s = created.json()
        url = f"/v1/sessions/{s['id']}"
        assert (await http.get(url, headers=dev.headers())).json() == s
        assert (await http.get(url, headers=dev2.headers())).status_code == 404
        assert (await http.get(url, headers=adm.headers())).status_code == 200  # tenant-wide
        patched = await http.patch(url, content='{"data_class": "confidential"}', headers=adm.headers(**JSON))
        assert patched.status_code == 200 and patched.json()["data_class"] == "confidential"
        listed = (await http.get("/v1/sessions?limit=1", headers=dev.headers())).json()
        assert [x["id"] for x in listed["data"]] == [s["id"]] and listed["has_more"] is False
        await seed_turn(admin, t_a, s["id"], 1, dev.principal.principal_id)
        await seed_turn(admin, t_a, s["id"], 2, dev.principal.principal_id)
        history = (await http.get(f"{url}/turns?limit=3", headers=dev.headers())).json()  # limit counts messages
        assert [(m["seq"], m["role"], m["text"]) for m in history["data"]] == [
            (1, "user", "q1"),
            (1, "assistant", "a1"),
            (2, "user", "q2"),
        ]
        rest = (await http.get(f"{url}/turns?after={history['next_cursor']}", headers=dev.headers())).json()
        assert [m["text"] for m in rest["data"]] == ["a2"] and rest["has_more"] is False
        await seed_turn(admin, t_a, s["id"], 3, dev.principal.principal_id, REDACTED)  # through jsonb (key order)
        redacted = (await http.get(f"{url}/turns", headers=dev.headers())).json()
        user, assistant = redacted["data"][-2:]
        assert user["text"] == 'Hello, mail me at [redacted]\n{"mail":"[redacted]","order":42}'
        assert [(r["start"], r["end"], r["reason"]) for r in user["redactions"]] == [
            (18, 28, "pii_email"),
            (38, 48, "pii_email"),
        ]
        assert assistant["text"] == "Sure, [redacted]" and assistant["redactions"][0]["start"] == 6
        assert (await http.get(url, headers=dev.headers())).json()["last_seq"] == 3
        deleted = await http.delete(url, headers=dev.headers())
        assert deleted.status_code == 202
        assert (await http.get(url, headers=dev.headers())).status_code == 404
    audit = await admin.fetch("SELECT actor_id, details FROM noema.audit_event WHERE tenant_id = $1", t_a)
    assert [(r["actor_id"], r["details"]) for r in audit] == [
        (adm.principal.principal_id, {"from": "internal", "to": "confidential"})
    ]
    job = await admin.fetchrow("SELECT id, kind, payload, dedupe_key FROM jobs.job WHERE tenant_id = $1", t_a)
    assert job is not None and job["id"] == deleted.json()["deletion_job_id"] and job["dedupe_key"] == s["id"]
    handler = make_handlers(world)["session.delete"]  # type: ignore[arg-type]
    await handler(Job(id=job["id"], tenant_id=t_a, kind=job["kind"], payload=job["payload"], attempt=1))
    assert await admin.fetchval("SELECT count(*) FROM noema.turn WHERE tenant_id = $1", t_a) == 0
    assert await admin.fetchval("SELECT count(*) FROM noema.session WHERE tenant_id = $1", t_a) == 0
    await handler(Job(id=job["id"], tenant_id=t_a, kind=job["kind"], payload=job["payload"], attempt=2))  # idempotent


async def test_rls_backs_the_authorizer(pg_world: PgWorld) -> None:
    """SEC-AZ-04 (session operations): with an allow-all authorizer, another tenant's session and, for a developer,
    another project's session are still 404 and never listed."""
    world, dev, dev2, other = pg_world.world, pg_world.dev, pg_world.dev2, pg_world.other
    async with client(world) as http:
        s = (await http.post("/v1/sessions", content="{}", headers=dev.headers(**JSON))).json()
        world.authz = AllowAll()  # type: ignore[assignment]
        url = f"/v1/sessions/{s['id']}"
        for who in (other, dev2):
            assert (await http.get(url, headers=who.headers())).status_code == 404
            assert (await http.get(f"{url}/turns", headers=who.headers())).status_code == 404
            patched = await http.patch(url, content='{"metadata": {}}', headers=who.headers(**JSON))
            assert patched.status_code == 404
            assert (await http.delete(url, headers=who.headers())).status_code == 404
            assert (await http.get("/v1/sessions", headers=who.headers())).json()["data"] == []
        assert (await http.get(url, headers=dev.headers())).status_code == 200


async def seed_record(admin: asyncpg.Connection[asyncpg.Record], tenant: str, record_id: str, resource: str) -> None:
    """A completed, unexpired keyed-request record whose replay body holds content."""
    await admin.execute(
        "INSERT INTO noema.idempotency_record (tenant_id, project_id, id, scope_hash, key, fingerprint, state, owner,"
        " lease_expires_at, resource_id, response_status, response_headers, response_body, expires_at)"
        " VALUES ($1, 'p1', $2, 'scope', $2, 'fp', 'completed', 'pod:req', now(), $3, 200, '[]', $4,"
        " now() + interval '1 day')",
        tenant,
        record_id,
        resource,
        b'{"text": "SECRET"}',
    )


async def test_session_delete_expires_the_idempotency_records_of_its_turns(pg_world: PgWorld) -> None:
    """Wave E2 (edge defect 10 rest): the `session.delete` job also ends the keyed-request records of the session's
    turns and decisions: expired now (never replayed again; the retention sweeper deletes the row and any large
    body) with the inline body cleared. A record of another session keeps its replay."""
    pw = pg_world
    world, admin, dev, t_a = pw.world, pw.conn, pw.dev, pw.t_a
    async with client(world) as http:
        mine = (await http.post("/v1/sessions", content="{}", headers=dev.headers(**JSON))).json()["id"]
        other = (await http.post("/v1/sessions", content="{}", headers=dev.headers(**JSON))).json()["id"]
        await seed_turn(admin, t_a, mine, 1, dev.principal.principal_id)
        await seed_turn(admin, t_a, other, 1, dev.principal.principal_id)
        await seed_record(admin, t_a, "idem_mine", f"turn_{mine[4:]}_1")
        await seed_record(admin, t_a, "idem_other", f"turn_{other[4:]}_1")
        assert (await http.delete(f"/v1/sessions/{mine}", headers=dev.headers())).status_code == 202
    handler = make_handlers(world)["session.delete"]  # type: ignore[arg-type]
    await handler(Job(id="job_1", tenant_id=t_a, kind="session.delete", payload={"session_id": mine}, attempt=1))
    rows = await admin.fetch(
        "SELECT id, expires_at <= now() AS expired, response_body FROM noema.idempotency_record"
        " WHERE tenant_id = $1 ORDER BY id",
        t_a,
    )
    assert [(r["id"], r["expired"], r["response_body"]) for r in rows] == [
        ("idem_mine", True, None),
        ("idem_other", False, b'{"text": "SECRET"}'),
    ]
