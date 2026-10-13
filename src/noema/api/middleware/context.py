"""Per-request ingress state (spec-04 §1 steps 0-7) and the services the middleware needs.

The ingress middleware creates one `RequestContext` per HTTP request and stores it in the ASGI scope under
`CTX_SCOPE_KEY`; routes read it through `noema.api.deps` (which declares a structural view of it, because the
import layers of spec-01 §2 rule 5 keep routes and deps from importing `noema.api.middleware`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol

from noema.contracts.bundle import RawOperation
from noema.core.clock import Clock, Deadline
from noema.core.ids import IdGen
from noema.core.principal import Principal
from noema.core.protocols.auth import Authenticator
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.protocols.events import SseWriter, TurnEventLog
from noema.core.protocols.storage import IdempotencyStore
from noema.core.values import JsonValue

if TYPE_CHECKING:
    from noema.api.middleware.idempotency import IdempotencyHook

CTX_SCOPE_KEY: Final = "noema.ctx"  # ASGI scope key of the RequestContext (api.deps reads the same literal)
SERVICES_STATE_KEY: Final = "noema.services"  # lifespan-state key of the process Services (api.deps: same literal)


class IngressServices(Protocol):
    """The part of `composition.Services` (spec-01 §3) the middleware uses; the full Services satisfies it."""

    @property
    def clock(self) -> Clock: ...

    @property
    def ids(self) -> IdGen: ...

    @property
    def config(self) -> ConfigRegistry: ...

    @property
    def authn(self) -> Authenticator: ...

    @property
    def idempotency(self) -> IdempotencyStore: ...

    @property
    def events(self) -> TurnEventLog: ...

    @property
    def sse(self) -> SseWriter: ...


@dataclass(frozen=True, slots=True)
class TraceContext:
    """The W3C trace context of a request (spec-04 §1 step 2). `traceparent` is the client's header when it was
    valid and joined; None means the request starts a new trace (the first span becomes the root)."""

    traceparent: str | None = None
    tracestate: str | None = None
    trace_id: str | None = None  # 32 lower-case hex digits of the joined trace
    parent_span_id: str | None = None  # 16 hex digits of the client's span
    sampled: bool | None = None

    @property
    def joined(self) -> bool:
        """True when the request continues the client's trace."""
        return self.traceparent is not None


@dataclass(slots=True, eq=False)
class RequestContext:
    """Everything the ingress steps established for one request. Fields after `trace` are filled as the steps pass:
    `operation`/`path_params` after routing, `principal`/`version` after step 4, `params`/`json`/`deadline` after
    the request gate (step 5), `idempotency` for authenticated operations just before dispatch."""

    request_id: str
    t_in: float  # Clock.monotonic() at ingress (step 0)
    pinned: ConfigSnapshot | None  # pinned for authenticated operations (`snapshot`); None for the probes
    services: IngressServices
    method: str
    trace: TraceContext
    operation: RawOperation | None = None
    path_params: dict[str, str] = field(default_factory=dict)
    client_ip: str | None = None
    principal: Principal | None = None
    version: str | None = None  # Aghoo-Version (explicit or the schema default)
    params: dict[str, JsonValue] = field(default_factory=dict)  # RequestGate.check_params output
    json: JsonValue = None  # the decoded, schema-valid JSON body (the raw bytes are not kept, Wave E)
    deadline: Deadline | None = None  # t_in + Aghoo-Deadline-Ms, on operations that declare the header
    error_headers: list[tuple[str, str]] = field(default_factory=list)  # added to an error envelope of this request
    idempotency: IdempotencyHook | None = None
    stub: bool = False  # set by a 501 route stub (the test-mode validator allows its undeclared 501)

    @property
    def snapshot(self) -> ConfigSnapshot:
        """The config snapshot pinned at ingress; every step after routing reads this one snapshot."""
        if self.pinned is None:
            raise RuntimeError("no config snapshot was pinned for this request")
        return self.pinned

    @property
    def operation_id(self) -> str | None:
        """The matched openapi operationId, None before routing or for an unknown route."""
        return None if self.operation is None else self.operation.operation_id

    @property
    def route(self) -> str | None:
        """The matched openapi path template (e.g. `/v1/turns/{turn_id}`)."""
        return None if self.operation is None else self.operation.path
