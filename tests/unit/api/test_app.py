"""create_app: the composition lifespan, environment gates and the deps/middleware contract."""

from __future__ import annotations

import sys
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from tests.fakes.telemetry import FakeTelemetry
from tests.unit.api.apifakes import FakeServices, asgi_client, auth, stub_routers

from noema.api import deps
from noema.api.app import create_app, resolve_env
from noema.api.middleware import context
from noema.core.errors import ForbiddenInEnvironment

pytestmark = pytest.mark.unit


@dataclass
class _Spawns:
    """The process runtime's `spawn` (names only; nothing runs)."""

    names: list[str] = field(default_factory=list)

    def spawn(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        self.names.append(name)


@dataclass
class ComposedServices(FakeServices):
    """FakeServices plus what the lifespan itself uses (the telemetry and the runtime's spawn)."""

    telemetry: FakeTelemetry = field(default_factory=FakeTelemetry)
    runtime: _Spawns = field(default_factory=_Spawns)


def _fake_composition(calls: list[Any], services: FakeServices) -> types.ModuleType:
    module = types.ModuleType("noema.composition")

    def build(env: str) -> FakeServices:
        calls.append(("build", env))
        return services

    async def start(svc: FakeServices, process: str) -> None:
        calls.append(("start", process, svc is services))

    async def stop(svc: FakeServices) -> None:
        calls.append(("stop", svc is services))

    module.build, module.start, module.stop = build, start, stop  # type: ignore[attr-defined]
    return module


async def test_the_lifespan_builds_starts_and_stops_the_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    services = ComposedServices()
    monkeypatch.setitem(sys.modules, "noema.composition", _fake_composition(calls, services))
    app = create_app(env="test", validate=True, routers=stub_routers())
    async with asgi_client(app) as client:
        assert calls == [("build", "test"), ("start", "api", True)]
        assert services.runtime.names == ["api.turn_slots_held"]  # aghoo_turn_slots_held (noema.api.overload)
        resp = await client.get("/v1/turns/turn_1", headers=auth())
        assert resp.status_code == 501
    assert calls[-1] == ("stop", True)


async def test_the_lifespan_reads_noema_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setitem(sys.modules, "noema.composition", _fake_composition(calls, ComposedServices()))
    monkeypatch.setenv("NOEMA_ENV", "dev")
    async with asgi_client(create_app(validate=False)):
        pass
    assert calls[0] == ("build", "dev")


async def test_injected_services_bypass_the_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "noema.composition", None)  # importing it would fail
    app = create_app(services=FakeServices(), validate=True, routers=stub_routers())  # type: ignore[arg-type]
    async with asgi_client(app) as client:
        assert (await client.get("/healthz")).status_code == 200


def test_resolve_env() -> None:
    assert resolve_env("staging") == "staging"
    with pytest.raises(ValueError, match="NOEMA_ENV"):
        resolve_env("production")


def test_the_response_validator_is_refused_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ForbiddenInEnvironment):
        create_app(env="prod", validate=True)
    monkeypatch.setenv("NOEMA_CONTRACT_VALIDATE", "1")
    with pytest.raises(ForbiddenInEnvironment):
        create_app(env="prod")
    monkeypatch.setenv("NOEMA_CONTRACT_VALIDATE", "0")
    create_app(env="prod")  # validation off: allowed


def test_trusted_proxies_are_validated_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="does not appear to be an IPv4 or IPv6 network"):
        create_app(trusted_proxies=["10.0.0.0/8", "not-a-cidr"], validate=False)
    monkeypatch.setenv("NOEMA_TRUSTED_PROXIES", "999.0.0.0/8")
    with pytest.raises(ValueError, match="does not appear"):
        create_app(validate=False)


def test_deps_and_middleware_share_the_scope_keys() -> None:
    """The structural views themselves are proven by mypy (noema.api.app `_context_conforms`)."""
    assert deps.CTX_SCOPE_KEY == context.CTX_SCOPE_KEY
    assert deps.SERVICES_STATE_KEY == context.SERVICES_STATE_KEY
