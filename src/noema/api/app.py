"""The API application (spec-01 §4): `uvicorn noema.api.app:create_app --factory --loop uvloop`.

`create_app()` registers every Phase-1 route of the bundled OpenAPI behind the ingress middleware (`/metrics` is no
API route: `composition.start` serves the Prometheus exposition on `NOEMA_METRICS_PORT`, spec-11 §7), plus the
test-mode response validator when `NOEMA_CONTRACT_VALIDATE=1`. Its lifespan builds the process services with
`noema.composition.build(env)`, starts the background tasks with `start(services, "api")` and stops them with
`stop(services)` on shutdown. Tests and module owners may inject `services` instead (the caller then owns their
lifecycle).

FastAPI's own request validation, OpenAPI/docs routes, slash redirects and native OpenTelemetry instrumentation are
off: the `RequestGate` is the only accept/reject authority, and spans and metrics come from `noema.telemetry`.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final, Protocol, cast, get_args

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.telemetry import TelemetryConfig
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from noema.api.deps import ApiServices, RequestContextView
from noema.api.middleware.client_ip import parse_trusted_proxies, trusted_proxies_from_env
from noema.api.middleware.context import CTX_SCOPE_KEY, SERVICES_STATE_KEY, IngressServices, RequestContext
from noema.api.middleware.errors import error_response, internal_error
from noema.api.middleware.ingress import IngressMiddleware
from noema.api.middleware.routing import OperationTable
from noema.api.middleware.validation import ResponseValidationMiddleware
from noema.api.overload import TurnSlots, WatchSlots, export_held, max_turns_from_env
from noema.api.routes import (
    admin_readonly,
    decisions,
    files,
    health,
    openai_compat,
    profiles,
    sessions,
    turns,
    usage,
)
from noema.api.shed import shed
from noema.api.sse_frame import RAW_SEND_KEY
from noema.config.rules import NoemaEnv
from noema.contracts.bundle import default_bundle
from noema.contracts.gate import RequestGate
from noema.contracts.validate import ContractValidator
from noema.core.errors import ErrorCode, ForbiddenInEnvironment, NoemaError

API_ROUTERS: Final[tuple[APIRouter, ...]] = tuple(
    m.router for m in (turns, decisions, openai_compat, profiles, sessions, files, usage, admin_readonly, health)
)
VALIDATE_ENV: Final = "NOEMA_CONTRACT_VALIDATE"
_ENVS: Final[frozenset[str]] = frozenset(get_args(NoemaEnv))
_FASTAPI_TELEMETRY_OFF: Final[TelemetryConfig] = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "auto_configure": False,
}


if TYPE_CHECKING:  # mypy proves the structural contracts between the middleware and the route-facing views

    def _context_conforms(ctx: RequestContext) -> RequestContextView:
        return ctx

    def _services_conform(services: ApiServices) -> IngressServices:
        return services


class NoemaApp(FastAPI):
    """FastAPI plus the ASGI server's own `send` in every HTTP scope (`RAW_SEND_KEY`), so `SseResponse` writes its
    frames past the middleware wrappers (E sprint B), and the edge shed (`noema.api.shed`, Wave E3): a request the
    per-process cap refuses is answered before the middleware stack. Both are off with the test-mode response
    validator, which has to see every frame and every response."""

    raw_sse_send = True
    edge_shed = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if self.raw_sse_send:
                scope[RAW_SEND_KEY] = send
            slots = getattr(self.state, "turn_slots", None) if self.edge_shed else None
            if slots is not None and await shed(slots, scope, send):
                return
        await super().__call__(scope, receive, send)


class _Composition(Protocol):
    """The `noema.composition` module surface the lifespan uses (spec-01 §3)."""

    def build(self, env: NoemaEnv) -> Any: ...
    async def start(self, services: Any, process: str) -> None: ...
    async def stop(self, services: Any) -> None: ...


def resolve_env(env: str | None) -> NoemaEnv:
    """`env`, else `NOEMA_ENV`; ValueError unless it is test, dev, staging or prod."""
    value = env if env is not None else os.environ.get("NOEMA_ENV")
    if value not in _ENVS:
        raise ValueError(f"NOEMA_ENV must be one of {sorted(_ENVS)}, got {value!r}")
    return cast(NoemaEnv, value)


def _request_id(request: Request) -> str:
    ctx = request.scope.get(CTX_SCOPE_KEY)
    return ctx.request_id if isinstance(ctx, RequestContext) else "req_unknown"


async def _http_exception(request: Request, exc: Exception) -> Response:
    """Starlette's 404/405 (a route the bundle has but no router registered) -> 404 `not_found`; else 500."""
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    err = NoemaError(ErrorCode.NOT_FOUND, "Unknown route.") if status in (404, 405) else internal_error()
    return error_response(err, _request_id(request))


async def _validation_exception(request: Request, exc: Exception) -> Response:
    """FastAPI request validation never runs on purpose (routes declare no typed parameters): a route bug."""
    return error_response(internal_error(), _request_id(request))


def create_app(
    *,
    services: ApiServices | None = None,
    env: NoemaEnv | None = None,
    routers: Sequence[APIRouter] | None = None,
    validate: bool | None = None,
    validator: ContractValidator | None = None,
    trusted_proxies: Sequence[str] | None = None,
    max_turns: int | None = None,
) -> FastAPI:
    """Build the API app.

    - `services`: inject the process services (tests); None = `composition.build/start/stop` in the lifespan.
    - `env`: the environment for `composition.build` (default `NOEMA_ENV`).
    - `routers`: the routers to mount (default: every Phase-1 route module; tests may pass their own handlers on
      bundle paths).
    - `validate`: enable the test-mode response validator (default `NOEMA_CONTRACT_VALIDATE=1`); raises
      `ForbiddenInEnvironment` in prod. Violations are collected in `app.state.contract_violations`.
    - `validator`: the `ContractValidator` to use (default: built from the first request's snapshot).
    - `trusted_proxies`: CIDRs for the client IP (default `NOEMA_TRUSTED_PROXIES`); ValueError if invalid.
    - `max_turns`: the per-process cap on concurrent turns (`noema.api.overload`; default
      `NOEMA_MAX_TURNS_PER_PROCESS`, else 120; 0 = no cap); ValueError if the variable is invalid.
    """
    env_name = env if env is not None else os.environ.get("NOEMA_ENV")
    if validate is None:
        validate = os.environ.get(VALIDATE_ENV) == "1"
    if validate and env_name == "prod":
        raise ForbiddenInEnvironment("the test-mode response validator is refused in prod (IMPL-041)")
    proxies = trusted_proxies_from_env() if trusted_proxies is None else parse_trusted_proxies(trusted_proxies)
    turn_cap = max_turns_from_env() if max_turns is None else (max_turns or None)
    bundle = default_bundle()
    violations: list[str] = []
    middleware = [
        Middleware(
            IngressMiddleware,
            gate=RequestGate(bundle),
            table=OperationTable(bundle.operations()),
            trusted_proxies=proxies,
        )
    ]
    if validate:
        middleware.insert(0, Middleware(ResponseValidationMiddleware, validator=validator, violations=violations))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, object]]:
        if services is not None:
            yield {SERVICES_STATE_KEY: services}
            return
        composition = cast(_Composition, importlib.import_module("noema.composition"))
        built = composition.build(resolve_env(env_name))
        await composition.start(built, "api")
        try:
            slots, instruments = app.state.turn_slots, built.telemetry.instruments
            built.runtime.spawn("api.turn_slots_held", lambda: export_held(slots, instruments, built.clock))
            yield {SERVICES_STATE_KEY: built}
        finally:
            await composition.stop(built)

    app = NoemaApp(
        title="Noema",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
        middleware=middleware,
        exception_handlers={HTTPException: _http_exception, RequestValidationError: _validation_exception},
        telemetry=_FASTAPI_TELEMETRY_OFF,
    )
    for router in API_ROUTERS if routers is None else routers:
        app.include_router(router)
    app.state.contract_violations = violations
    app.raw_sse_send = not validate
    app.state.turn_slots = TurnSlots(turn_cap)
    app.state.watch_slots = WatchSlots()
    app.edge_shed = not validate
    return app
