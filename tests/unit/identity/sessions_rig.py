"""Session routes over the real API middleware and validator, on the tests/fakes world with the real authorizer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from tests.fakes.auth import FakeAuthenticator
from tests.fakes.db_system import seed_identity
from tests.fakes.world import FakeWorld, build_world

from noema.api.app import create_app
from noema.api.routes.sessions import router
from noema.core.principal import ALL_SCOPES, Principal, Role
from noema.identity.authz import MatrixAuthorizer

T_A: Final = "t_a"
T_B: Final = "t_b"


@dataclass(frozen=True)
class Caller:
    token: str
    principal: Principal

    def headers(self, **extra: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}", **extra}


def caller(name: str, roles: tuple[Role, ...], *, project: str = "p1", tenant: str = T_A) -> Caller:
    token = f"tok_{name}"
    return Caller(token, Principal(tenant, project, f"prn_{name}", f"key_{name}", frozenset(roles), ALL_SCOPES, ""))


DEV = caller("dev", ("developer",))
PEER = caller("peer", ("developer",))
EU = caller("eu", ("end_user",))
REV = caller("rev", ("reviewer",))
ADMIN = caller("admin", ("tenant_admin",), project="p2")
AUD = caller("aud", ("auditor",), project="p2")
DEV_P2 = caller("dev2", ("developer",), project="p2")
OTHER = caller("other", ("tenant_admin",), tenant=T_B)
NO_SESSIONS_SCOPE = Caller(
    "tok_noscope", Principal(T_A, "p1", "prn_noscope", "k", frozenset({"developer"}), frozenset({"turns"}), "")
)
CALLERS: Final = (DEV, PEER, EU, REV, ADMIN, AUD, DEV_P2, OTHER, NO_SESSIONS_SCOPE)


def sessions_world() -> FakeWorld:
    world = build_world()
    authn = FakeAuthenticator()
    for c in CALLERS:
        authn.register(c.token, c.principal)
        seed_identity(
            world.db.store,
            tenant_id=c.principal.tenant_id,
            project_id=c.principal.project_id,
            principal_id=c.principal.principal_id,
            roles=sorted(c.principal.roles),
        )
    world.authn = authn
    world.authz = MatrixAuthorizer()  # type: ignore[assignment]
    return world


@asynccontextmanager
async def client(world: FakeWorld, *, check_contract: bool = True) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client over create_app(services=world) with the response validator on."""
    app: FastAPI = create_app(services=world, routers=[router], validate=True, trusted_proxies=())  # type: ignore[arg-type]
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, client=("203.0.113.7", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
    if check_contract:
        assert app.state.contract_violations == []


def seed_turn(world: FakeWorld, session_id: str, seq: int, **row: Any) -> str:
    turn_id = f"turn_{session_id[4:]}{seq:02d}"
    now = world.clock.now()
    world.db.store.seed(
        "turn",
        {
            "tenant_id": T_A,
            "project_id": "p1",
            "id": turn_id,
            "session_id": session_id,
            "seq": seq,
            "principal_id": DEV.principal.principal_id,
            "status": "completed",
            "content_policy": "full",
            "input_parts": [{"type": "text", "text": f"question {seq}"}],  # the orchestrator's format
            "output_text": f"answer {seq}",
            "redactions": {"user": [], "assistant": []},
            "routing": {},
            "usage": {},
            "timings": {},
            "degradations": [],
            "created_at": now + timedelta(seconds=seq),
            "completed_at": now + timedelta(seconds=seq, milliseconds=500),
            **row,
        },
    )
    return turn_id
