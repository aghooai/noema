"""`create_app()` without injected services boots the REAL composition (wave-B api handoff risk; c2b brief E): the
lifespan runs `composition.build/start/stop` against a seeded stack (tests/integration/composition/conftest.py), and
a bootstrap key, a decision through the real engine, adapters and STS broker (moto) to the wire mock, the usage
report and the config-backed readiness all work. The jobs entrypoint's worker runs a `session.delete` job that the
api enqueued. Every response is contract-validated (client side, and the server's NOEMA_CONTRACT_VALIDATE=1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Final

import anyio
import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager
from tests.integration.composition.conftest import Seeded

from noema import composition
from noema.api.app import create_app
from noema.api.middleware.routing import OperationTable
from noema.contracts.bundle import default_bundle
from noema.contracts.validate import ContractValidator
from noema.core.secret import SecretStr
from noema.db.engine import connect
from noema.entrypoints import jobs as jobs_entrypoint
from noema.entrypoints._worker import serve

pytestmark = [pytest.mark.integration]
VERSION: Final = {"aghoo-version": "2026-10-01"}
QUESTION: Final = {"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}


class Api:
    """An ASGI client whose every response is contract-validated."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.validator = ContractValidator()
        self.table = OperationTable(default_bundle().operations())

    async def call(self, method: str, path: str, key: str | None = None, json: Any = None) -> httpx.Response:
        headers = dict(VERSION)
        if key is not None:
            headers["authorization"] = f"Bearer {key}"
        content = None
        if json is not None:
            headers["content-type"] = "application/json"
            content = orjson.dumps(json)
        response = await self.http.request(method, path, headers=headers, content=content)
        match = self.table.match(method, path.split("?")[0])
        assert match is not None
        self.validator.check_response(
            match.operation.operation_id, response.status_code, dict(response.headers), response.content, json
        )
        return response


@pytest.fixture
async def api(seeded: Seeded) -> AsyncIterator[Api]:
    app = create_app()  # no services: the lifespan builds, starts and stops the real composition
    async with LifespanManager(app, startup_timeout=30, shutdown_timeout=60) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://noema.test") as http:
            yield Api(http)
        assert not app.state.contract_violations


async def _until_ready(api: Api) -> None:
    with anyio.fail_after(20):
        while True:  # the config loads in start(); the watcher retries every 5 s
            if (await api.call("GET", "/readyz")).status_code == 200:
                return
            await anyio.sleep(0.1)


async def test_create_app_boots_the_real_composition(api: Api, seeded: Seeded) -> None:
    await _until_ready(api)
    assert (await api.call("GET", "/healthz")).status_code == 200
    assert (await api.call("GET", "/v1/usage")).status_code == 401  # no key
    key = seeded.key()
    body = {"profile": "router-default", "escalation": "off", "input": "Call me back please.", "questions": [QUESTION]}
    before = max((int(r["seq"]) for r in seeded.mock.requests()), default=0)
    decision = await api.call("POST", "/v1/decisions", key, body)
    assert decision.status_code == 200, decision.text
    assert decision.json()["answers"][0]["name"] == "callback"
    calls = seeded.mock.requests(after=before)
    assert {r["op"] for r in calls} == {"converse", "chat"}  # haiku + luna (Converse), deepseek (Mantle)
    assert all(r["outcome"] == "ok" for r in calls)
    assert all(r["auth"]["session_token"] for r in calls if r["op"] == "converse")  # tenant STS credentials (moto)
    with anyio.fail_after(20):
        while True:  # usage lines land through the write-behind outbox
            usage = await api.call("GET", "/v1/usage?group_by=line", key)
            assert usage.status_code == 200
            if any(row["line"] == "decision" for row in usage.json()["data"]):
                break
            await anyio.sleep(0.2)
    # priced with the bootstrap's dev catalog (prices import, prices_ref db:<n>, loaded by ActiveConfigRegistry)
    line = next(row for row in usage.json()["data"] if row["line"] == "decision")
    assert line["usage"]["cost_usd"] > 0 and line["usage"]["cost_estimated"] is False, line
    other = await api.call("GET", f"/v1/decisions/{decision.json()['id']}", seeded.key(tenant="t_beta"))
    assert other.status_code == 404  # tenant isolation through the real authenticator and RLS


async def test_the_jobs_entrypoint_runs_the_jobs_the_api_enqueued(api: Api, seeded: Seeded) -> None:
    await _until_ready(api)
    key = seeded.key()
    created = await api.call("POST", "/v1/sessions", key, {})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert (await api.call("DELETE", f"/v1/sessions/{session_id}", key)).status_code == 202  # enqueues session.delete
    admin = await connect(SecretStr(_admin_dsn(seeded)), expected_role=None)

    async def gone() -> None:
        with anyio.fail_after(30):
            while True:
                if not await admin.fetchval("SELECT count(*) FROM noema.session WHERE id = $1", session_id):
                    return
                await anyio.sleep(0.2)

    try:
        services = composition.build("test")
        assert set(services.wiring.values()) == {"real"}
        factories = jobs_entrypoint.factories()
        await serve("jobs", factories, ["session.delete"], concurrency=1, services=services, stopped=gone)
    finally:
        await admin.close()


def _admin_dsn(seeded: Seeded) -> str:
    user, password = seeded.target.pg_superuser
    return f"postgresql://{user}:{password}@{seeded.target.pg_host}/{seeded.target.db_name}"
