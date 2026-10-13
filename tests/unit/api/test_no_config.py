"""Wave E edge fix 15: before the first config load (`ActiveConfigRegistry` empty, e.g. a pod booting before
`config approve` or during a database failover) the probes answer as spec-11 §7 says (`/healthz` 200, `/readyz`
503) and every authenticated operation gets the typed, contract-valid 500 `internal_error` envelope with an
`x-request-id`, never Starlette's plain-text 500."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from tests.unit.api.apifakes import FakeServices, asgi_client, auth, make_app, stub_routers

from noema.api.routes import health
from noema.composition import ActiveConfigRegistry

pytestmark = pytest.mark.unit


@dataclass
class _Runtime:
    draining: bool = False


@dataclass
class _Booting(FakeServices):
    config: Any = field(default_factory=lambda: ActiveConfigRegistry("test"))
    runtime: _Runtime = field(default_factory=_Runtime)


async def test_probes_and_typed_errors_before_the_first_config_load() -> None:
    app, _ = make_app(_Booting(), routers=[health.router, *stub_routers()])
    async with asgi_client(app) as client:
        live = await client.get("/healthz")
        ready = await client.get("/readyz")
        turn = await client.get("/v1/turns/turn_1", headers=auth(**{"x-request-id": "req-boot-1"}))
    assert live.status_code == 200
    assert ready.status_code == 503
    assert turn.status_code == 500 and turn.headers["x-request-id"] == "req-boot-1"
    error = turn.json()["error"]
    assert error["code"] == "internal_error" and error["retryable"] is True
