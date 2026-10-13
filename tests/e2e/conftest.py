"""Fixtures of the e2e suites (tests/e2e) against the compose stack of `make e2e-up` (docs/modules/integration.md).

- `stack` (session): the bootstrap state file (`NOEMA_E2E_STATE`, default `~/.noema/stacks/<NOEMA_E2E_PROJECT or
  noema-e2e>.json`, written by `tools/bootstrap_dev.py`) with both api replicas ready (`/readyz` 200 within 60 s).
  Without a running stack the e2e tests skip; `NOEMA_E2E_REQUIRE=1` (the make targets) makes that a failure.
- `stack.client(replica, tenant=, project=, role=)`: a `CheckedClient` on api replica 0 (api-1) or 1 (api-2),
  authenticated with that principal's bootstrap key. Every response it returns has been validated with the
  `ContractValidator` (operation from the bundled OpenAPI by method and path; JSON bodies, error envelopes, SSE
  frames, decision invariants paired with the request); a violation raises `ContractViolation` at the call.
- `mock` (function): the wire mock's control API (`load`, `requests`); a test that loads its own scenario gets the
  stack's default scenario (deploy/compose/mockbedrock/default.yaml) back at teardown.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, NoReturn

import anyio
import httpx
import orjson
import pytest
import yaml

from noema.api.middleware.routing import OperationTable
from noema.contracts.bundle import default_bundle
from noema.contracts.validate import ContractValidator, SseEvent
from noema.core.values import JsonValue

REPO: Final = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO: Final = REPO / "deploy" / "compose" / "mockbedrock" / "default.yaml"
AGHOO_VERSION: Final = "2026-10-01"
READY_TIMEOUT_S: Final = 60.0


@dataclass
class CheckedClient:
    """httpx on one api replica; see the module docstring. `checked` lists (operation id, status) per response."""

    http: httpx.AsyncClient
    validator: ContractValidator
    table: OperationTable
    checked: list[tuple[str, int]] = field(default_factory=list)

    def operation_id(self, response: httpx.Response) -> str:
        match = self.table.match(response.request.method, response.request.url.path)
        assert match is not None, f"no contract operation for {response.request.method} {response.request.url.path}"
        return match.operation.operation_id

    def check(self, response: httpx.Response, request_json: JsonValue | None = None) -> str:
        """Validate a complete (read) response; returns its operation id."""
        op = self.operation_id(response)
        self.validator.check_response(op, response.status_code, dict(response.headers), response.content, request_json)
        self.checked.append((op, response.status_code))
        return op

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: JsonValue | None = None,
        expect: int | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """One request (JSON body as `json`, or httpx's `files`/`data`/`content`/`params`/`headers`); the response
        is read, contract-validated and, with `expect`, its status asserted."""
        if json is not None:
            kwargs["content"] = orjson.dumps(json)
            kwargs["headers"] = {"content-type": "application/json", **kwargs.get("headers", {})}
        response = await self.http.request(method, path, **kwargs)
        self.check(response, json)
        if expect is not None:
            assert response.status_code == expect, (response.status_code, response.text[:500])
        return response

    async def sse(self, path: str, *, json: JsonValue, headers: Mapping[str, str] | None = None) -> list[SseEvent]:
        """POST a streaming request and read the whole event stream; the stream is validated frame by frame (ids
        1, 2, ... and every event's schema) and must be a 200 text/event-stream."""
        request_headers = {"content-type": "application/json", "accept": "text/event-stream", **(headers or {})}
        async with self.http.stream("POST", path, content=orjson.dumps(json), headers=request_headers) as response:
            await response.aread()
        self.check(response)
        assert response.status_code == 200, response.text[:500]
        return self.validator.check_sse_stream(self.operation_id(response), response.content)


@dataclass
class Stack:
    """The bootstrap state of the running stack (see tools/bootstrap_dev.py for its layout)."""

    state: Mapping[str, Any]
    validator: ContractValidator
    table: OperationTable
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    @property
    def apis(self) -> list[str]:
        return list(self.state["endpoints"]["api"])

    @property
    def mock_url(self) -> str:
        return str(self.state["endpoints"]["mockbedrock"])

    def principal(self, tenant: str = "t_acme", project: str = "main", role: str = "developer") -> Mapping[str, Any]:
        entry: Mapping[str, Any] = self.state["tenants"][tenant]["projects"][project]["principals"][role]
        return entry

    def project_id(self, tenant: str = "t_acme", project: str = "main") -> str:
        return str(self.state["tenants"][tenant]["projects"][project]["project_id"])

    def client(
        self, replica: int = 0, *, tenant: str = "t_acme", project: str = "main", role: str | None = "developer"
    ) -> CheckedClient:
        """A validated client on replica 0 or 1; `role=None` sends no credentials."""
        headers = {"aghoo-version": AGHOO_VERSION}
        if role is not None:
            headers["authorization"] = f"Bearer {self.principal(tenant, project, role)['key']}"
        http = httpx.AsyncClient(base_url=self.apis[replica], headers=headers, timeout=60.0)
        self.clients.append(http)
        return CheckedClient(http, self.validator, self.table)


def _state_path() -> Path:
    explicit = os.environ.get("NOEMA_E2E_STATE")
    project = os.environ.get("NOEMA_E2E_PROJECT") or "noema-e2e"
    return Path(explicit) if explicit else Path.home() / ".noema" / "stacks" / f"{project}.json"


def _unavailable(reason: str) -> NoReturn:
    if os.environ.get("NOEMA_E2E_REQUIRE") == "1":
        pytest.fail(f"e2e stack unavailable: {reason}")
    pytest.skip(f"e2e stack unavailable ({reason}): make e2e-up")


async def _ready(client: CheckedClient) -> None:
    with anyio.move_on_after(READY_TIMEOUT_S):
        while True:
            try:
                response = await client.request("GET", "/readyz")
            except httpx.TransportError:
                response = None
            if response is not None and response.status_code == 200:
                return
            await anyio.sleep(0.5)
    _unavailable(f"{client.http.base_url} is not ready after {READY_TIMEOUT_S:.0f} s")


@pytest.fixture(scope="session")
async def stack() -> AsyncIterator[Stack]:
    path = _state_path()
    try:
        state = orjson.loads(path.read_bytes())
    except (FileNotFoundError, orjson.JSONDecodeError) as exc:
        _unavailable(f"no bootstrap state at {path} ({type(exc).__name__})")
    if len(state.get("endpoints", {}).get("api", [])) < 2:
        _unavailable(f"{path} lists fewer than 2 api replicas (re-run make e2e-bootstrap)")
    stack = Stack(state, ContractValidator(), OperationTable(default_bundle().operations()))
    try:
        for replica in range(2):
            await _ready(stack.client(replica, role=None))
        yield stack
    finally:
        for http in stack.clients:
            await http.aclose()


@dataclass
class Mock:
    """The mock's control API (`/_mock/*`, docs/modules/qa-tools.md)."""

    http: httpx.AsyncClient
    loaded: bool = False

    async def load(self, scenario: Mapping[str, Any] | str) -> None:
        body = scenario if isinstance(scenario, str) else yaml.safe_dump(dict(scenario), sort_keys=False)
        response = await self.http.post("/_mock/scenario", content=body, headers={"content-type": "application/yaml"})
        assert response.status_code == 200, response.text
        self.loaded = True

    async def requests(self, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        response = await self.http.get("/_mock/requests", params={"after": after, "limit": limit})
        assert response.status_code == 200, response.text
        page: dict[str, Any] = response.json()
        return page


@pytest.fixture
async def mock(stack: Stack) -> AsyncIterator[Mock]:
    async with httpx.AsyncClient(base_url=stack.mock_url, timeout=30.0) as http:
        handle = Mock(http)
        try:
            yield handle
        finally:
            if handle.loaded:
                await handle.load(DEFAULT_SCENARIO.read_text())
