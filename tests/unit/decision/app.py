"""The API app with the decision routes over the fake world and the real decision engine (route-level tests).
The test-mode contract validator is on: every response is checked against the bundle and the vendored
invariants (`check_examples`)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from tests.contract_internal.kits import principal
from tests.fakes.auth import FakeAuthenticator
from tests.unit.decision.world import EngineWorld, engine_world

from noema.api.app import create_app
from noema.api.routes import decisions, openai_compat, profiles
from noema.core.principal import Principal

KEY = "ak_test_01J000000000000000000DEC00." + "d" * 43
OTHER_KEY = "ak_test_01J000000000000000000OTH00." + "o" * 43


class DecisionServices:
    """The fake world's fields, with the real decision engine and compiler."""

    def __init__(self, ew: EngineWorld) -> None:
        self._ew = ew
        self.decisions = ew.engine
        self.compiler = ew.compiler
        self.authn = FakeAuthenticator()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ew.world, name)


def make(
    ew: EngineWorld | None = None, *, who: Principal | None = None
) -> tuple[FastAPI, DecisionServices, EngineWorld]:
    ew = ew or engine_world()
    svc = DecisionServices(ew)
    svc.authn.register(KEY, who or principal())
    svc.authn.register(OTHER_KEY, replace(principal(), principal_id="prn_other", roles=frozenset({"end_user"})))
    routers = [decisions.router, openai_compat.router, profiles.router]
    app = create_app(services=svc, routers=routers, validate=True, trusted_proxies=())
    return app, svc, ew


@asynccontextmanager
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process client; at exit, asserts that the contract validator recorded no violation."""
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, client=("203.0.113.7", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
    assert app.state.contract_violations == []


def headers(key: str = KEY, **extra: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}", "content-type": "application/json", **extra}
