"""Route-facing dependencies: services, the request context and the ingress hooks (spec-01 §2 rule 5).

Routes get everything from here, never from `noema.composition` or `noema.api.middleware` (the import layers
forbid both). The ingress middleware has already run spec-04 §1 steps 0-5 when a route body starts: the context
holds the pinned config snapshot, the authenticated `Principal`, the coerced parameters and the schema-valid JSON
body. The route then authorizes (step 6, `services.authz`), calls `begin_idempotency` (step 7) and does its work.

Typical use (FastAPI dependencies):

    @router.post("/v1/decisions", name="createDecision")
    async def create_decision(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
        svc.authz.check(who, "decision.create")                       # step 6 (resource checks after lookups)
        decision_id = svc.ids.new("dec")
        replay = await begin_idempotency(request, resource_id=decision_id)  # step 7
        if replay is not None:
            return replay
        ...                                                           # ctx.json, ctx.params, ctx.deadline
        mark_model_dispatched(request)  # once a model call was dispatched: the response is stored for replay
        return json_response(result)

Raise `NoemaError` for every request-level failure: the middleware renders the typed envelope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Annotated, Final, NoReturn, Protocol, cast

import orjson
from fastapi import Depends, Request
from starlette.responses import Response

from noema.contracts.bundle import RawOperation
from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.services import ServiceSet
from noema.core.values import DataClass, JsonValue

CTX_SCOPE_KEY: Final = "noema.ctx"  # == noema.api.middleware.context.CTX_SCOPE_KEY (test-enforced)
SERVICES_STATE_KEY: Final = "noema.services"  # == noema.api.middleware.context.SERVICES_STATE_KEY


# The process services as routes see them: the core `ServiceSet` (every `composition.Services` field, read-only;
# the frozen dataclass satisfies it structurally, checked statically in `noema.composition`).
ApiServices = ServiceSet


class TraceView(Protocol):
    """The request's W3C trace context (spec-04 §1 step 2)."""

    @property
    def traceparent(self) -> str | None: ...
    @property
    def tracestate(self) -> str | None: ...
    @property
    def trace_id(self) -> str | None: ...
    @property
    def parent_span_id(self) -> str | None: ...
    @property
    def sampled(self) -> bool | None: ...
    @property
    def joined(self) -> bool: ...


class IdempotencyView(Protocol):
    """The step-7 hook of one authenticated request (see `begin_idempotency`)."""

    @property
    def key(self) -> str | None: ...

    async def begin(
        self, *, resource_id: str | None = None, stream: bool = False, fingerprint: str | None = None
    ) -> Response | None: ...

    def mark_dispatched(self) -> None: ...

    def withhold(self) -> None: ...


class RequestContextView(Protocol):
    """What the ingress middleware established for this request (read-only except `stub` and `error_headers`)."""

    @property
    def request_id(self) -> str: ...
    @property
    def t_in(self) -> float:
        """`Clock.monotonic()` at ingress (step 0)."""
        ...

    @property
    def snapshot(self) -> ConfigSnapshot:
        """The config snapshot pinned at ingress; use it for the whole request."""
        ...

    @property
    def method(self) -> str: ...
    @property
    def trace(self) -> TraceView: ...
    @property
    def operation(self) -> RawOperation | None: ...
    @property
    def operation_id(self) -> str | None: ...
    @property
    def route(self) -> str | None:
        """The openapi path template, e.g. `/v1/turns/{turn_id}`."""
        ...

    @property
    def path_params(self) -> Mapping[str, str]: ...
    @property
    def client_ip(self) -> str | None: ...
    @property
    def principal(self) -> Principal | None:
        """None only on unauthenticated operations (`/healthz`, `/readyz`)."""
        ...

    @property
    def version(self) -> str | None:
        """`Aghoo-Version` (explicit or the default)."""
        ...

    @property
    def params(self) -> Mapping[str, JsonValue]:
        """Declared parameters (path, query, headers in their declared spelling), coerced, with schema defaults."""
        ...

    @property
    def json(self) -> JsonValue:
        """The decoded JSON body; schema, semantic rules and inline-image sizes already checked."""
        ...

    @property
    def deadline(self) -> Deadline | None:
        """`t_in + Aghoo-Deadline-Ms` on createTurn, createDecision and the facade; None elsewhere."""
        ...

    @property
    def error_headers(self) -> list[tuple[str, str]]:
        """Headers added to an error envelope of this request (e.g. `x-ratelimit-*` on a 429)."""
        ...

    @property
    def idempotency(self) -> IdempotencyView | None: ...

    stub: bool


def get_context(request: Request) -> RequestContextView:
    """The request context. RuntimeError (500) if the ingress middleware didn't run (a wiring bug)."""
    ctx = request.scope.get(CTX_SCOPE_KEY)
    if ctx is None:
        raise RuntimeError("the ingress middleware did not run for this request")
    return cast(RequestContextView, ctx)


def get_services(request: Request) -> ApiServices:
    """The process services (from the lifespan state)."""
    services = request.scope.get("state", {}).get(SERVICES_STATE_KEY)
    if services is None:
        raise RuntimeError("noema services are missing from the lifespan state")
    return cast(ApiServices, services)


def get_principal(request: Request) -> Principal:
    """The authenticated caller; RuntimeError (500) on an unauthenticated operation (a route bug)."""
    principal = get_context(request).principal
    if principal is None:
        raise RuntimeError("no principal: the operation is not authenticated")
    return principal


def get_snapshot(request: Request) -> ConfigSnapshot:
    """The config snapshot pinned at ingress."""
    return get_context(request).snapshot


# The FastAPI dependencies are `async def`: FastAPI runs a sync dependency in the threadpool (one thread hop per
# request, and a virtual clock sees the waiting request as blocked). The sync getters above stay for direct calls.
async def context_dep(request: Request) -> RequestContextView:
    """`get_context` as an async dependency (runs on the event loop)."""
    return get_context(request)


async def services_dep(request: Request) -> ApiServices:
    """`get_services` as an async dependency."""
    return get_services(request)


async def principal_dep(request: Request) -> Principal:
    """`get_principal` as an async dependency."""
    return get_principal(request)


async def snapshot_dep(request: Request) -> ConfigSnapshot:
    """`get_snapshot` as an async dependency."""
    return get_snapshot(request)


ContextDep = Annotated[RequestContextView, Depends(context_dep)]
ServicesDep = Annotated[ApiServices, Depends(services_dep)]
PrincipalDep = Annotated[Principal, Depends(principal_dep)]
SnapshotDep = Annotated[ConfigSnapshot, Depends(snapshot_dep)]


def _hook(request: Request) -> IdempotencyView:
    hook = get_context(request).idempotency
    if hook is None:
        raise RuntimeError("no idempotency hook: the operation is not authenticated")
    return hook


async def begin_idempotency(
    request: Request, *, resource_id: str | None = None, stream: bool = False, fingerprint: str | None = None
) -> Response | None:
    """spec-04 §1 step 7, after authorization. None: run the request (no `Idempotency-Key`, or this request owns
    the key). A Response: return it as is (the stored response of an earlier request, or an attach to one still
    running, with `x-aghoo-idempotent-replay: true`). Raises 409 `idempotency_key_reused`, 410
    `resume_window_expired`, and the store's 500/504. `resource_id`: the pre-generated `turn_`/`dec_` id;
    `stream`: this request answers with SSE; `fingerprint`: only for non-JSON bodies (uploads)."""
    return await _hook(request).begin(resource_id=resource_id, stream=stream, fingerprint=fingerprint)


def trace_id(ctx: RequestContextView) -> str:
    """The request's W3C trace id, else one derived from its request id (32 lower-case hex digits; the formula of
    `noema.decision.service.trace_id_of`, so every operation derives it alike). Never the invalid all-zero id."""
    joined = ctx.trace.trace_id
    if joined and len(joined) == 32 and all(c in "0123456789abcdef" for c in joined) and joined != "0" * 32:
        return joined
    return hashlib.sha256(ctx.request_id.encode()).hexdigest()[:32]


def mark_model_dispatched(request: Request) -> None:
    """Call once a model call was dispatched: the final response is then stored for replay (IMPL-077)."""
    _hook(request).mark_dispatched()


def withhold_response(request: Request) -> None:
    """The response's content may not be kept (`store: false`, or a data class whose `store_content` is not
    `full`): the idempotency record is then completed as not_stored, so a retry runs again (Wave E)."""
    _hook(request).withhold()


def content_may_be_kept(snapshot: ConfigSnapshot, data_class: DataClass, *, store: bool = True) -> bool:
    """Whether a response carrying model output may be kept (in an idempotency record): `store` and the data
    class's `store_content` is `full`."""
    return store and snapshot.platform.data_classes.for_class(data_class).store_content == "full"


def add_error_headers(request: Request, headers: Mapping[str, str]) -> None:
    """Headers for an error envelope raised later in this request (e.g. `Reservation.headers()` on a 429)."""
    get_context(request).error_headers.extend(headers.items())


def not_implemented(request: Request) -> NoReturn:
    """The body of a route stub: 501 `not_implemented` (the test-mode validator allows it until implemented)."""
    get_context(request).stub = True
    raise NoemaError(ErrorCode.NOT_IMPLEMENTED, "This operation is not implemented yet.")


def json_response(content: JsonValue, *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> Response:
    """An `application/json` response serialized with orjson (`x-request-id` is added by the middleware)."""
    return Response(orjson.dumps(content), status_code=status_code, headers=headers, media_type="application/json")
