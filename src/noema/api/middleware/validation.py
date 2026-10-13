"""Test-mode response validation (IMPL-041 point 4): `NOEMA_CONTRACT_VALIDATE=1`, refused in prod.

Every response is checked against the bundled OpenAPI with `ContractValidator.check_response` (status declared,
required headers, body schema, `ERROR_TABLE` and `x-noema-error-codes` agreement, SSE framing, decision
invariants with the request body). A violation is recorded in `violations` and raised as `ContractViolation`
after the response was forwarded, so in-process test clients fail loudly.

A 2xx answer to a request with `Idempotency-Key` whose route never called `deps.begin_idempotency` is also a
violation (the key would be silently ignored).

Known contract gaps, validated against the shared `Error` schema and `ERROR_TABLE` instead of the operation:
- an unmatched route (404 `not_found`): no operation exists;
- a 501 `not_implemented` from a route stub on an operation that doesn't declare 501 (until the module owner
  implements it).
Every operation declares its 400 `schema_violation` since the contract rev 6 + 400 patch, so a 400 is checked
against the operation like any other status.
"""

from __future__ import annotations

import logging
from typing import Final

import orjson
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from noema.api.middleware.context import CTX_SCOPE_KEY, RequestContext
from noema.contracts.gate import media_type
from noema.contracts.validate import ContractValidator, ContractViolation
from noema.core.errors import ERROR_TABLE, ErrorCode
from noema.core.protocols.config import ConfigSnapshot
from noema.core.values import JsonValue

log = logging.getLogger("noema.api.validation")
ERROR_SCHEMA_PTR: Final = "/components/schemas/Error"


def validator_for(snapshot: ConfigSnapshot) -> ContractValidator:
    """A validator parameterized by a config snapshot (IMPL-039): `routing.confidence_bands` and the
    `labeling.acceptance` thresholds; template limits keep the vendored defaults (the snapshot has no listing)."""
    bands = snapshot.routing.confidence_bands
    acc = snapshot.labeling.acceptance
    return ContractValidator(
        bands={"high_gte": bands.high_gte, "low_lt": bands.low_lt},
        accept={
            "ece_max": acc.ece_max,
            "n_min": acc.n_min,
            "class_n_min": acc.class_n_min,
            "consensus_min": acc.consensus_min,
            "family_lift_max": snapshot.labeling.family_overlap_lift_max,
        },
    )


class ResponseValidationMiddleware:
    """Outermost test-mode middleware; `violations` collects every failure message (tests assert it is empty).
    Without an explicit `validator`, one is built from the config snapshot of the first request (`validator_for`)."""

    def __init__(self, app: ASGIApp, *, validator: ContractValidator | None, violations: list[str]) -> None:
        self.app = app
        self._validator = validator
        self.violations = violations

    def validator(self, ctx: RequestContext | None) -> ContractValidator:
        """The validator in use (built lazily from `ctx.snapshot`, else with the vendored defaults)."""
        if self._validator is None:
            if ctx is None or ctx.pinned is None:  # nothing pinned (a probe, or no config loaded yet)
                return ContractValidator()
            self._validator = validator_for(ctx.pinned)
        return self._validator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status: int | None = None
        headers: dict[str, str] = {}
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                for k, v in message.get("headers", ()):
                    headers.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
            await send(message)

        await self.app(scope, receive, capture)
        if status is None:
            return  # the client went away before a response started
        ctx = scope.get(CTX_SCOPE_KEY)
        try:
            self.check(ctx if isinstance(ctx, RequestContext) else None, status, headers, b"".join(chunks))
        except ContractViolation as exc:
            self.violations.append(str(exc))
            log.error("contract violation: %s", exc)
            raise

    def check(self, ctx: RequestContext | None, status: int, headers: dict[str, str], body: bytes) -> None:
        """Validate one complete response; raises ContractViolation."""
        if "x-request-id" not in headers:
            raise ContractViolation(f"{status} response without x-request-id")
        op = ctx.operation if ctx is not None else None
        if op is None:
            self.check_envelope("<unmatched route>", status, headers, body, ctx=ctx)
            return
        hook = ctx.idempotency if ctx is not None else None
        if hook is not None and hook.key is not None and not hook.begun and 200 <= status < 300:
            raise ContractViolation(f"{op.operation_id}: {status} with an Idempotency-Key but no begin_idempotency()")
        declared = str(status) in op.responses
        if status == 501 and ctx is not None and ctx.stub and not declared:
            self.check_envelope(op.operation_id, status, headers, body, ctx=ctx, code=ErrorCode.NOT_IMPLEMENTED)
            return
        self.validator(ctx).check_response(op.operation_id, status, headers, body, ctx.json if ctx else None)

    def check_envelope(
        self,
        where: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
        *,
        ctx: RequestContext | None,
        code: ErrorCode | None = None,
    ) -> None:
        """An error response checked against the shared `Error` schema and `ERROR_TABLE` only."""
        if media_type(headers.get("content-type")) != "application/json":
            raise ContractViolation(f"{where}: {status} error is not application/json")
        try:
            value: JsonValue = orjson.loads(body)
        except orjson.JSONDecodeError:
            raise ContractViolation(f"{where}: {status} error body is not JSON") from None
        errors = [e.message for e in self.validator(ctx).bundle.validator(ERROR_SCHEMA_PTR).iter_errors(value)]
        if errors:
            raise ContractViolation(f"{where}: {status} body is not an Error envelope: {errors[:3]}")
        assert isinstance(value, dict)
        error = value["error"]
        assert isinstance(error, dict)
        actual = ErrorCode(str(error["code"]))
        spec = ERROR_TABLE[actual]
        if code is not None and actual is not code:
            raise ContractViolation(f"{where}: expected {code.value}, got {actual.value}")
        if spec.status != status or error["type"] != spec.type.value or error["retryable"] is not spec.retryable:
            raise ContractViolation(f"{where}: {actual.value} disagrees with ERROR_TABLE (HTTP {status})")
        if error["request_id"] != headers.get("x-request-id"):
            raise ContractViolation(f"{where}: error.request_id differs from the x-request-id header")
