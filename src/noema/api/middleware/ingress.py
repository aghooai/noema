"""The ingress pipeline: spec-04 §1 steps 0-5 and 7 for every HTTP request, in the frozen order.

0. `t_in = Clock.monotonic()`; pin `snapshot = ConfigRegistry.current()` (the body cap reads this snapshot) once
   the route is known: the probes pin nothing, so `/healthz` and `/readyz` answer before the first config load,
   and an authenticated operation then gets the typed, retryable 500 `internal_error` (Wave E).
1. Request id (echo a valid `x-request-id`, else `req_<ULID>`).
2. Trace context (join a valid `traceparent`, else a new trace).
   Routing: an unknown path or method is 404 `not_found` (it needs no credentials; the route table is public).
3. JSON body cap -> 413 `request_too_large`; Content-Type -> 415. Wave E: only what the headers declare is
   decided before step 4 (a `Content-Length` over the cap, the Content-Type of a declared or required body); the
   body itself is read after authentication, so a client without a valid key makes the process buffer nothing
   (an undeclared-length body over the cap is then 413 after authentication). After the gate the raw bytes are
   dropped (routes read `ctx.json`); bodies over `OFFLOAD_BYTES` are parsed, validated and fingerprinted in a
   worker thread, so a near-cap body does not stall the event loop for the other streams.
4. Authenticate -> `Principal`; `Aghoo-Version` -> 400; `x-aghoo-end-user` -> 422 `feature_not_available`.
5. Request gate: parameters (unknown query parameters 400; `Aghoo-Deadline-Ms` -> `ctx.deadline`), JSON Schema,
   semantic rules, inline-image size (413 `image_too_large`).
6. (route) Authorize via `Authorizer`.
7. (route, through `deps.begin_idempotency`) the idempotency hook; this middleware heartbeats a `New` record and
   completes it on every exit path.

Every failure is the typed error envelope; unexpected exceptions are logged and become 500 `internal_error`. An
error that carries x-ratelimit-* headers (admission's 429, `AdmissionRejected.headers`) sends them with its
envelope (spec-08 §4: every 429). The access log records method, route template, status, latency, request id,
tenant and key id, never header values. Unauthenticated operations (`/healthz`, `/readyz`) skip steps 3-7.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

import anyio
import anyio.to_thread
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from noema.api.middleware import auth, gate
from noema.api.middleware.body import ClientDisconnected, read_capped_body, replay_receive, too_large
from noema.api.middleware.client_ip import Network, client_ip
from noema.api.middleware.context import CTX_SCOPE_KEY, SERVICES_STATE_KEY, IngressServices, RequestContext
from noema.api.middleware.errors import internal_error, send_error
from noema.api.middleware.idempotency import (
    DEFAULT_AWAIT_MS,
    KEY_PARAM,
    CapturedResponse,
    IdempotencyHook,
    request_fingerprint,
)
from noema.api.middleware.request_id import REQUEST_ID_HEADER, resolve_request_id
from noema.api.middleware.routing import OperationTable
from noema.api.middleware.trace import attached, parse_trace
from noema.contracts.bundle import RawOperation
from noema.contracts.gate import RequestGate
from noema.core.clock import Deadline, SystemClock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import UlidIdGen
from noema.core.values import JsonValue

log = logging.getLogger("noema.api.errors")
access_log = logging.getLogger("noema.api.access")

_JOINED_HEADERS: Final = frozenset({"x-forwarded-for"})
OFFLOAD_BYTES: Final = 1 << 20  # JSON bodies above this are checked (and fingerprinted) off the event loop
_RATE_HEADER_PREFIX: Final = "x-ratelimit-"
_FALLBACK_IDS: Final = UlidIdGen(SystemClock())  # request ids when the services are missing (a wiring bug)


def request_headers(scope: Scope) -> dict[str, str]:
    """Lower-cased request headers (latin-1). The first occurrence of a name wins, except X-Forwarded-For, whose
    occurrences are joined with ","."""
    out: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name in out:
            if name in _JOINED_HEADERS:
                out[name] = f"{out[name]},{value}"
            continue
        out[name] = value
    return out


class _ResponseTracker:
    """Wraps `send`: adds `x-request-id` when the response has none, records the status, and captures the body
    while an idempotency record is owned (for `complete`)."""

    def __init__(self, send: Send, ctx: RequestContext) -> None:
        self._send = send
        self._ctx = ctx
        self.started = False
        self.finished = False
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.sse = False
        self._capture = False
        self._body = bytearray()

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", ()))
            if not any(k.lower() == REQUEST_ID_HEADER.encode() for k, _ in headers):
                headers.append((REQUEST_ID_HEADER.encode(), self._ctx.request_id.encode("latin-1")))
            message = {**message, "headers": headers}
            self.started, self.status, self.headers = True, int(message["status"]), headers
            ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
            self.sse = ctype.split(b";", 1)[0].strip().lower() == b"text/event-stream"
            hook = self._ctx.idempotency
            self._capture = hook is not None and hook.token is not None and not self.sse
        elif message["type"] == "http.response.body":
            if self._capture:
                self._body += message.get("body", b"")
            if not message.get("more_body", False):
                self.finished = True
        await self._send(message)

    async def fail(self, err: NoemaError) -> None:
        """Send the envelope for `err`, or close a response that already started (its status can't change)."""
        if not self.started:
            await send_error(self, err, self._ctx.request_id, self._ctx.error_headers)
        elif not self.finished:
            log.warning("error after the response started (%s); closing the stream", err.code.value)
            with contextlib.suppress(OSError):
                await self({"type": "http.response.body", "body": b"", "more_body": False})

    def captured(self) -> CapturedResponse | None:
        """The final response for the idempotency record (None if nothing was sent)."""
        if not self.started or self.status is None:
            return None
        headers = tuple(
            (k.decode("latin-1").lower(), v.decode("latin-1"))
            for k, v in self.headers
            if k.lower() != b"content-length"
        )
        return CapturedResponse(self.status, headers, bytes(self._body), self.sse)


class IngressMiddleware:
    """Pure ASGI middleware running the ingress steps (module docstring). `gate` and `table` come from the same
    bundle; `trusted_proxies` is `NOEMA_TRUSTED_PROXIES` (spec-09 §2). Services are read from the lifespan state
    (`scope["state"]["noema.services"]`), so the lifespan must have run."""

    def __init__(
        self, app: ASGIApp, *, gate: RequestGate, table: OperationTable, trusted_proxies: Sequence[Network] = ()
    ) -> None:
        self.app = app
        self.gate = gate
        self.table = table
        self.trusted_proxies = tuple(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        services = cast("IngressServices | None", scope.get("state", {}).get(SERVICES_STATE_KEY))
        if services is None:
            log.error("noema services are missing from the lifespan state (was the lifespan run?)")
            await send_error(send, internal_error(), _FALLBACK_IDS.new("req"))
            return
        t_in = services.clock.monotonic()  # step 0 (the snapshot is pinned once the route is known)
        headers = request_headers(scope)
        request_id = resolve_request_id(headers.get(REQUEST_ID_HEADER), services.ids)  # step 1
        headers[REQUEST_ID_HEADER] = request_id
        trace, otel_ctx = parse_trace(headers)  # step 2
        ctx = RequestContext(request_id, t_in, None, services, scope["method"], trace)
        scope[CTX_SCOPE_KEY] = ctx
        tracker = _ResponseTracker(send, ctx)
        with attached(otel_ctx):
            try:
                await self._handle(scope, receive, tracker, ctx, headers)
            except Exception as exc:
                await self._fail(ctx, tracker, exc)
            finally:
                if ctx.idempotency is not None:
                    await ctx.idempotency.finish(tracker.captured())
                self._log_access(ctx, tracker)

    @staticmethod
    async def _fail(ctx: RequestContext, tracker: _ResponseTracker, exc: Exception) -> None:
        """Render a failure: a `NoemaError` (also inside a single-exception group, e.g. from a route's task
        group) as its envelope, a client disconnect as nothing, anything else as a logged 500."""
        inner: BaseException = exc
        while isinstance(inner, BaseExceptionGroup) and len(inner.exceptions) == 1:
            inner = inner.exceptions[0]
        if isinstance(inner, ClientDisconnected):
            return
        if isinstance(inner, NoemaError):
            rate = getattr(inner, "headers", None)  # AdmissionRejected (spec-08 §4: x-ratelimit-* on every 429)
            if isinstance(rate, Mapping):
                ctx.error_headers.extend(
                    (k, v) for k, v in rate.items() if isinstance(k, str) and k.startswith(_RATE_HEADER_PREFIX)
                )
            await tracker.fail(inner)
            return
        log.error("unhandled error (request_id=%s, route=%s)", ctx.request_id, ctx.route, exc_info=exc)
        await tracker.fail(internal_error())

    async def _handle(
        self, scope: Scope, receive: Receive, send: Send, ctx: RequestContext, headers: dict[str, str]
    ) -> None:
        match = self.table.match(ctx.method, scope["path"])
        if match is None:
            raise NoemaError(ErrorCode.NOT_FOUND, "Unknown route.")
        op = ctx.operation = match.operation
        ctx.path_params = match.path_params
        if not op.authenticated:
            await self.app(scope, receive, send)
            return
        services = ctx.services
        ctx.pinned = services.config.current()  # step 0's pin; the retryable 500 until the first config load
        content_type = headers.get("content-type")
        limit = ctx.snapshot.platform.limits.max_json_body_bytes
        json_op = gate.is_json_operation(op)
        declared = headers.get("content-length")
        size = int(declared) if declared is not None and declared.isdigit() else None
        if json_op:  # step 3 as far as the headers declare it; the body is read after authentication
            if size is not None and size > limit:
                raise too_large(limit)
            if size or op.body_required:
                self.gate.check_content_type(op.operation_id, content_type)
        elif op.body_ptrs:  # multipart: streamed (and capped per type) by the route
            self.gate.check_content_type(op.operation_id, content_type)
        peer = scope.get("client")
        ctx.client_ip = client_ip(peer[0] if peer else None, headers.get("x-forwarded-for"), self.trusted_proxies)
        ctx.principal = await auth.authenticate(services.authn, headers, ctx.client_ip, ctx.request_id)  # step 4
        raw = b""
        if json_op:  # step 3: the body of an authenticated caller
            raw = await read_capped_body(receive, limit, declared)
            if raw or op.body_required:
                self.gate.check_content_type(op.operation_id, content_type)
            receive = replay_receive(b"", receive)  # routes read ctx.json: the raw bytes are not kept
        ctx.version = auth.check_version(op, headers)
        auth.check_end_user(headers)
        query = gate.parse_query(scope.get("query_string", b""))  # step 5
        ctx.params = gate.check_params(self.gate, op, path=ctx.path_params, query=query, headers=headers)
        key = ctx.params.get(KEY_PARAM)
        fingerprint: str | None = None
        if json_op:
            if len(raw) > OFFLOAD_BYTES:
                ctx.json, fingerprint = await anyio.to_thread.run_sync(
                    self._check_large, op, content_type, raw, limit, isinstance(key, str), ctx.version
                )
            else:
                ctx.json = gate.check_body(self.gate, op, content_type, raw, limit)
        del raw  # not kept for the rest of the request (an SSE request lives as long as its stream)
        deadline_ms = ctx.params.get(gate.DEADLINE_PARAM)
        if isinstance(deadline_ms, int):
            ctx.deadline = Deadline(ctx.t_in + deadline_ms / 1000, services.clock)
        hook = ctx.idempotency = IdempotencyHook(
            store=services.idempotency,
            events=services.events,
            sse=services.sse,
            clock=services.clock,
            principal=ctx.principal,
            operation=op,
            key=key if isinstance(key, str) else None,
            body=ctx.json,
            version=ctx.version,
            wait_deadline=ctx.deadline or Deadline(ctx.t_in + DEFAULT_AWAIT_MS / 1000, services.clock),
            fingerprint=fingerprint,
        )
        if hook.key is None:
            await self.app(scope, receive, send)
            return
        error: Exception | None = None
        async with anyio.create_task_group() as tg:  # hosts the lease heartbeat of a New record
            hook.start_soon = tg.start_soon
            try:
                await self.app(scope, receive, send)
            except Exception as exc:  # re-raised below, outside the task group (no ExceptionGroup wrapping)
                error = exc
            finally:
                tg.cancel_scope.cancel()
        if error is not None:
            raise error

    def _check_large(
        self, op: RawOperation, content_type: str | None, raw: bytes, limit: int, keyed: bool, version: str | None
    ) -> tuple[JsonValue, str | None]:
        """The gate's body check and, for a keyed request, its fingerprint (run in a worker thread)."""
        value = gate.check_body(self.gate, op, content_type, raw, limit)
        return value, request_fingerprint(value, version) if keyed else None

    def _log_access(self, ctx: RequestContext, tracker: _ResponseTracker) -> None:
        """One access line. The JSON formatter keeps only its 08 §7 keys, so the spec-09 §2 fields are in the
        message (and `tenant`) too: `<method> <route> <status> <ms>ms request_id=<id> key_id=<id>`."""
        principal = ctx.principal
        fields: dict[str, Any] = {
            "http_method": ctx.method,
            "http_route": ctx.route or "<unmatched>",
            "http_status": tracker.status,
            "latency_ms": round((ctx.services.clock.monotonic() - ctx.t_in) * 1000, 3),
            "request_id": ctx.request_id,
            "tenant_id": principal.tenant_id if principal else None,
            "key_id": principal.key_id if principal else None,
            "tenant": principal.tenant_id if principal else None,
            "event": "http.access",
        }
        access_log.info(
            "%s %s %s %sms request_id=%s key_id=%s",
            ctx.method,
            fields["http_route"],
            tracker.status,
            fields["latency_ms"],
            ctx.request_id,
            fields["key_id"] or "-",
            extra=fields,
        )
