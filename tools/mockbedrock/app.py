"""`MockBedrock`: one listener serving Converse, ConverseStream, Mantle and the control API (spec-10 §2.1).

Model endpoints: `POST /model/{modelId}/converse`, `POST /model/{modelId}/converse-stream`,
`POST [/openai]/v1/chat/completions`, `POST [/openai]/v1/responses`, `POST [/openai]/v1/decisions`.
Control endpoints: `GET /_mock/requests[?after=<seq>&limit=<n>]` (`limit=0` is the O(1) health probe: only
`last_seq` and `dropped`), `GET|POST /_mock/scenario`,
`POST /_mock/barrier/{name}/release`, `POST /_mock/reset`, `POST /_mock/quotas`.
In-process tests call the same controls as methods (`load_scenario`, `release`, `requests`, `reset`, ...).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Final
from urllib.parse import parse_qs, unquote

import orjson
from tests.fakes.scenario import Scenario, ScenarioError, load_scenario

from .clock import MockClock, SystemClock
from .converse import ConverseWire
from .httpserver import MAX_BODY_BYTES, HttpServer, Request, Responder
from .mantle import MantleWire, Route
from .state import DEFAULT_RECORD_BYTES, DEFAULT_RECORD_LIMIT, MockState, Quota

LOG: Final = logging.getLogger("mockbedrock")
_JSON: Final = (("content-type", "application/json"),)
DEFAULT_REGION: Final = "us-east-1"
_MANTLE_ROUTES: Final[dict[str, Route]] = {
    "/v1/chat/completions": "chat",
    "/openai/v1/chat/completions": "chat",
    "/v1/responses": "responses",
    "/openai/v1/responses": "openai_responses",
    "/v1/decisions": "decisions",
    "/openai/v1/decisions": "decisions",
}
ScenarioSource = Scenario | str | bytes | Path | Mapping[str, Any]


class MockBedrock:
    """The wire mock. `start(host, port)` binds (port 0 = ephemeral); `endpoint_url` is the bedrock-runtime
    override and `mantle_url` the Mantle base (`.../v1`) for `NOEMA_AWS_ENDPOINT_OVERRIDES` (ADR-001 §8).
    The request log keeps at most `record_limit` records and about `record_bytes` bytes; Converse accepts SigV4
    credentials scoped to `<region>/bedrock` only."""

    def __init__(
        self,
        *,
        clock: MockClock | None = None,
        scenario: ScenarioSource | None = None,
        record_limit: int = DEFAULT_RECORD_LIMIT,
        record_bytes: int = DEFAULT_RECORD_BYTES,
        max_body: int = MAX_BODY_BYTES,
        region: str = DEFAULT_REGION,
    ) -> None:
        self.clock: MockClock = clock or SystemClock()
        self.state = MockState(self.clock, record_limit=record_limit, record_bytes=record_bytes)
        self._converse = ConverseWire(self.state, region=region)
        self._mantle = {route: MantleWire(self.state, route) for route in set(_MANTLE_ROUTES.values())}
        self._http = HttpServer(self.handle, clock_ns=self.clock.now_ns, max_body=max_body)
        self._host = "127.0.0.1"
        if scenario is not None:
            self.load_scenario(scenario)

    # --- lifecycle ---------------------------------------------------------------------------------------------------
    async def start(self, host: str = "127.0.0.1", port: int = 0, *, reuse_port: bool = False) -> MockBedrock:
        await self._http.start(host, port, reuse_port=reuse_port)
        self._host = host
        LOG.info("mockbedrock listening on %s", self.endpoint_url)
        return self

    async def stop(self) -> None:
        await self._http.stop()

    async def __aenter__(self) -> MockBedrock:
        if not self._http.started:
            await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        return self._http.port

    @property
    def endpoint_url(self) -> str:
        host = "127.0.0.1" if self._host in ("0.0.0.0", "") else self._host  # noqa: S104 (a comparison)
        return f"http://{host}:{self.port}"

    @property
    def mantle_url(self) -> str:
        return self.endpoint_url + "/v1"

    # --- controls (also served under /_mock) --------------------------------------------------------------------------
    def load_scenario(self, source: ScenarioSource) -> Scenario:
        """Validate and install a scenario (raises `ScenarioError`)."""
        scenario = source if isinstance(source, Scenario) else load_scenario(source)
        self.state.load(scenario)
        return scenario

    def reset(self) -> None:
        """Forget the scenario, the recorded requests and the quotas."""
        self.state.reset()

    def release(self, barrier: str) -> None:
        self.state.release(barrier)

    def requests(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """Copies of at most `limit` request records with seq > after, oldest first (see `tools.mockbedrock.record`);
        O(page). Raises ValueError for a negative `after` or `limit`."""
        return self.state.log.since(after, limit)

    def unmatched(self) -> list[dict[str, Any]]:
        """Requests no scenario rule answered (each one is a test failure)."""
        return [r for r in self.requests() if r["outcome"] == "unmatched"]

    def set_quotas(self, quotas: Iterable[Quota | Mapping[str, Any]]) -> None:
        """Replace the per-(AKID, model) quotas (raises ValueError for a malformed entry)."""
        self.state.quotas.set(q if isinstance(q, Quota) else Quota.parse(q) for q in quotas)

    # --- routing -----------------------------------------------------------------------------------------------------
    async def handle(self, req: Request, resp: Responder) -> None:
        """Dispatch one request (the HttpServer handler)."""
        path = req.path
        if path.startswith("/_mock/"):
            await self._control(req, resp)
            return
        if req.method == "POST" and path.startswith("/model/"):
            parts = path.split("/")
            if len(parts) == 4 and parts[3] in ("converse", "converse-stream") and parts[2]:
                await self._converse.handle(req, resp, unquote(parts[2]), stream=parts[3] == "converse-stream")
                return
        route = _MANTLE_ROUTES.get(path) if req.method == "POST" else None
        if route is not None:
            await self._mantle[route].handle(req, resp)
            return
        body = orjson.dumps({"message": f"mockbedrock: no operation at {req.method} {path}"})
        await resp.send(404, (*_JSON, ("x-amzn-errortype", "UnknownOperationException")), body)

    async def _control(self, req: Request, resp: Responder) -> None:
        status, payload = self._control_result(req)
        await resp.send(status, _JSON, orjson.dumps(payload))

    def _control_result(self, req: Request) -> tuple[int, Any]:
        method, path = req.method, req.path
        if path == "/_mock/requests" and method == "GET":
            query = parse_qs(req.query)
            try:
                after = int(query.get("after", ["0"])[0])
                limit = int(query["limit"][0]) if "limit" in query else None
                records = self.requests(after, limit)
            except ValueError:
                return 400, {"errors": ["after and limit must be integers >= 0"]}
            log = self.state.log
            return 200, {"requests": records, "dropped": log.dropped, "last_seq": log.last_seq}
        if path == "/_mock/scenario" and method == "GET":
            scenario = self.state.scenario
            return 200, {"scenario": None if scenario is None else dict(scenario.document)}
        if path == "/_mock/scenario" and method == "POST":
            try:
                scenario = self.load_scenario(req.body.decode("utf-8"))
            except (ScenarioError, UnicodeDecodeError, ValueError) as exc:
                errors = list(exc.errors) if isinstance(exc, ScenarioError) else [str(exc)]
                return 400, {"errors": errors}
            return 200, {"scenario": scenario.name, "rules": len(scenario.rules)}
        if path == "/_mock/reset" and method == "POST":
            self.reset()
            return 200, {"reset": True}
        if path == "/_mock/quotas" and method == "POST":
            try:
                raw = orjson.loads(req.body or b"{}")
                quotas = raw.get("quotas") if isinstance(raw, dict) else None
                if not isinstance(quotas, list):
                    raise ValueError('expected {"quotas": [...]}')
                self.set_quotas(quotas)
            except (orjson.JSONDecodeError, ValueError, AttributeError) as exc:
                return 400, {"errors": [str(exc)]}
            return 200, {"quotas": len(quotas)}
        parts = path.split("/")
        if method == "POST" and len(parts) == 5 and parts[2] == "barrier" and parts[4] == "release" and parts[3]:
            name = unquote(parts[3])
            self.release(name)
            return 200, {"released": name}
        return 404, {"errors": [f"unknown control endpoint {method} {path}"]}
