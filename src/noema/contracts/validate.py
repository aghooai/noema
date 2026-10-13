"""`ContractValidator`: the test-mode response and SSE validator (IMPL-041 point 4).

Enabled only under `NOEMA_CONTRACT_VALIDATE=1` and refused in prod (the composition root enforces that). It checks
each response against the bundled OpenAPI: the status is declared, the required headers are present, the body
matches the schema of its media type, error envelopes agree with `ERROR_TABLE` and the operation's
`x-noema-error-codes`, and decision bodies satisfy the vendored invariants (parameterized by the active config,
IMPL-039) and parse with the tolerant client models. Violations raise `ContractViolation`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import orjson

from noema.contracts import vendor
from noema.contracts.bundle import Bundle, RawOperation, default_bundle
from noema.contracts.gate import media_type, param_path
from noema.core.errors import ERROR_TABLE, ErrorCode, ForbiddenInEnvironment
from noema.core.values import JsonValue

_JSON: Final = "application/json"
_SSE: Final = "text/event-stream"
_MAX_REPORTED: Final = 5
_FRAME_ID: Final = re.compile(r"[1-9][0-9]*")


class ContractViolation(AssertionError):
    """A response or SSE frame that breaks the contract."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One parsed SSE frame (comments such as `: ping` are skipped)."""

    id: str | None
    event: str
    data: JsonValue


def _fail(operation_id: str, what: str) -> ContractViolation:
    return ContractViolation(f"{operation_id}: {what}")


class ContractValidator:
    """Validates responses of one bundle. `bands`, `accept` and `template_max` are the active config's
    `routing.confidence_bands`, acceptance thresholds (`check_examples.ACCEPT` keys) and template limits; None uses
    the vendored defaults (the design example config). Raises ForbiddenInEnvironment under NOEMA_ENV=prod."""

    def __init__(
        self,
        bundle: Bundle | None = None,
        *,
        bands: Mapping[str, float] | None = None,
        accept: Mapping[str, float] | None = None,
        template_max: Mapping[str, int] | None = None,
    ) -> None:
        if os.environ.get("NOEMA_ENV") == "prod":
            raise ForbiddenInEnvironment("the ContractValidator is test-mode only (IMPL-041)")
        self.bundle: Final = bundle or default_bundle()
        self._ops = {op.operation_id: op for op in self.bundle.operations()}
        self._bands = dict(bands) if bands is not None else None
        self._accept = dict(accept) if accept is not None else None
        self._template_max = dict(template_max) if template_max is not None else None

    # --- schema helpers ------------------------------------------------------------------------------------------
    def _op(self, operation_id: str) -> RawOperation:
        try:
            return self._ops[operation_id]
        except KeyError:
            raise ContractViolation(f"unknown operation {operation_id!r}") from None

    @staticmethod
    def _status_key(op: RawOperation, status: int) -> str:
        for key in (str(status), f"{status // 100}XX", "default"):
            if key in op.responses:
                return key
        raise _fail(op.operation_id, f"status {status} is not declared")

    def _schema_errors(self, ptr: str, value: object) -> list[str]:
        errors = []
        for err in self.bundle.validator(ptr).iter_errors(value):
            errors.append(f"{param_path(err.instance_path) or '<root>'}: {err.message[:160]}")
            if len(errors) >= _MAX_REPORTED:
                break
        return errors

    def validate_schema(self, operation_id: str, status: int, media: str, value: object) -> None:
        """Validate a decoded body against the declared schema of (operation, status, media type)."""
        op = self._op(operation_id)
        declared = op.responses[self._status_key(op, status)]
        if media not in declared:
            raise _fail(operation_id, f"{status} does not declare {media}")
        errors = self._schema_errors(declared[media], value)
        if errors:
            raise _fail(operation_id, f"{status} {media} body violates the schema: " + "; ".join(errors))

    # --- responses -----------------------------------------------------------------------------------------------
    def check_response(
        self,
        operation_id: str,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        request_json: JsonValue | None = None,
    ) -> None:
        """Validate one complete response. `request_json` (the decoded request body) enables the paired decision
        invariants of JSON decision bodies. SSE bodies are split into frames and checked with `check_sse_frame`,
        without `request_json` (a createTurn body is not a decision request)."""
        op = self._op(operation_id)
        key = self._status_key(op, status)
        lowered = {k.lower(): v for k, v in headers.items()}
        missing = [h for h in op.response_headers.get(key, ()) if h not in lowered]
        if missing:
            raise _fail(operation_id, f"{status} is missing required headers {missing}")
        declared = op.responses[key]
        if not declared:
            if body:
                raise _fail(operation_id, f"{status} declares no content but has a body")
            return
        mt = media_type(lowered.get("content-type"))
        if mt not in declared:
            raise _fail(operation_id, f"{status} content type {mt!r} is not one of {sorted(declared)}")
        if mt == _SSE:
            # No SSE operation takes a decision request body: createTurn's body is TurnCreateParams, and a turn's
            # decision pairs with the config's router/verifier questions, which only the caller knows (pass them to
            # `check_sse_stream(decision_request=)`). Frames are therefore checked without request pairing.
            self.check_sse_stream(operation_id, body)
            return
        if mt != _JSON:
            return  # binary or text derivative: nothing to validate beyond the content type
        try:
            value: JsonValue = orjson.loads(body)
        except orjson.JSONDecodeError:
            raise _fail(operation_id, f"{status} body is not valid JSON") from None
        self.validate_schema(operation_id, status, _JSON, value)
        if status >= 400:
            self._check_error(op, status, lowered, value)
        elif isinstance(value, Mapping):
            self._check_decision_body(op, value, request_json)

    def _check_error(self, op: RawOperation, status: int, headers: Mapping[str, str], value: JsonValue) -> None:
        assert isinstance(value, Mapping)  # the Error schema passed
        error = value["error"]
        assert isinstance(error, Mapping)
        code = ErrorCode(str(error["code"]))
        spec = ERROR_TABLE[code]
        if spec.status != status:
            raise _fail(op.operation_id, f"{code} must be HTTP {spec.status}, got {status}")
        if error["type"] != spec.type.value or error["retryable"] is not spec.retryable:
            raise _fail(op.operation_id, f"{code} must have type {spec.type} and retryable {spec.retryable}")
        if op.error_codes and code.value not in op.error_codes:
            raise _fail(op.operation_id, f"{code} is not in x-noema-error-codes")
        if error["request_id"] != headers.get("x-request-id"):
            raise _fail(op.operation_id, "error.request_id differs from the x-request-id header")

    def _check_decision_body(
        self, op: RawOperation, value: Mapping[str, JsonValue], request_json: JsonValue | None
    ) -> None:
        request = request_json if isinstance(request_json, Mapping) else None
        if op.operation_id == "createDecisionOpenAICompat":
            if request is not None:
                self._invariants("oa_invariants", op.operation_id, value, request)
            return
        if op.operation_id not in ("createDecision", "getDecision"):
            return
        sdk = vendor.load("python_sdk_types")
        if value.get("object") == "decision":
            self._parse_tolerant(op.operation_id, sdk.DecisionResult, value)
            if request is not None:
                self.check_decision_result(value, request, where=op.operation_id)
        else:
            self._parse_tolerant(op.operation_id, sdk.JevDecisionResponse, value)
            if request is not None:
                self._invariants("jev_invariants", op.operation_id, value, request)

    @staticmethod
    def _parse_tolerant(where: str, model: Any, value: Mapping[str, JsonValue]) -> None:
        try:
            model.model_validate(value)
        except Exception as exc:  # pydantic.ValidationError; the vendored models are client-side
            raise _fail(where, f"the tolerant client models reject the body: {str(exc)[:300]}") from exc

    def _invariants(self, fn: str, where: str, value: Mapping[str, JsonValue], request: Mapping[str, Any]) -> None:
        errs = list(vendor.function("check_examples", fn)(value, request))
        if errs:
            raise _fail(where, f"{fn}: " + "; ".join(str(e) for e in errs[:_MAX_REPORTED]))

    def check_decision_result(
        self, result: Mapping[str, JsonValue], request: Mapping[str, Any], *, where: str = "decision"
    ) -> None:
        """`check_examples.result_invariants` with this validator's bands, acceptance and template limits."""
        errs = list(
            vendor.function("check_examples", "result_invariants")(
                result, request, bands=self._bands, accept=self._accept, template_max=self._template_max
            )
        )
        if errs:
            raise _fail(where, "result_invariants: " + "; ".join(str(e) for e in errs[:_MAX_REPORTED]))

    # --- SSE -------------------------------------------------------------------------------------------------------
    def check_sse_frame(
        self,
        operation_id: str,
        frame_id: str | None,
        event: str,
        data: bytes,
        *,
        decision_request: Mapping[str, Any] | None = None,
    ) -> JsonValue:
        """Validate one SSE frame; returns the decoded `data`.

        Turn streams: `data` is a `TurnEvent` whose `type` equals `event`; `frame_id` is a positive integer, except
        the best-effort `client_too_slow` error frame, which has no id. With `decision_request` (the question wire
        dicts and input), `decision.completed`/`decision.updated`/`turn.verified` results are also checked with
        `result_invariants`. File streams: `event` is `file.updated` and `data` a `FileObject`.
        """
        op = self._op(operation_id)
        try:
            value: JsonValue = orjson.loads(data)
        except orjson.JSONDecodeError:
            raise _fail(operation_id, f"SSE frame {frame_id}: data is not valid JSON") from None
        self.validate_schema(operation_id, 200, _SSE, value)
        assert isinstance(value, Mapping)
        if operation_id == "watchFile":
            if event != "file.updated":
                raise _fail(operation_id, f"file stream event {event!r} must be 'file.updated'")
        else:
            if value.get("type") != event:
                raise _fail(operation_id, f"SSE event {event!r} differs from data.type {value.get('type')!r}")
            if decision_request is not None:
                nested = {
                    "decision.completed": "decision",
                    "decision.updated": "decision",
                    "turn.verified": "verification",
                }
                field = nested.get(event)
                result = value.get(field) if field else None
                if isinstance(result, Mapping):
                    self.check_decision_result(result, decision_request, where=f"{operation_id} {event}")
        if frame_id is None:
            too_slow = event == "error" and value.get("code") == ErrorCode.CLIENT_TOO_SLOW.value
            if op.operation_id == "watchFile" or not too_slow:
                raise _fail(operation_id, f"SSE frame {event!r} has no id")
        elif not _FRAME_ID.fullmatch(frame_id):
            raise _fail(operation_id, f"SSE frame id {frame_id!r} is not a positive integer")
        return value

    @staticmethod
    def parse_sse(stream: bytes) -> list[tuple[str | None, str | None, bytes]]:
        """Split an SSE body into (id, event, data) frames; comment-only blocks (`: ping`) are skipped. Raises
        ContractViolation on a malformed frame (unknown field, several data lines, missing event or data)."""
        frames: list[tuple[str | None, str | None, bytes]] = []
        text = stream.replace(b"\r\n", b"\n")
        for block in text.split(b"\n\n"):
            lines = [ln for ln in block.split(b"\n") if ln and not ln.startswith(b":")]
            if not lines:
                continue
            fields: dict[str, list[bytes]] = {}
            for ln in lines:
                name, sep, val = ln.partition(b":")
                if not sep or name.decode() not in ("id", "event", "data"):
                    raise ContractViolation(f"malformed SSE line {ln[:80]!r}")
                fields.setdefault(name.decode(), []).append(val[1:] if val.startswith(b" ") else val)
            if (
                len(fields.get("data", ())) != 1
                or len(fields.get("event", ())) != 1
                or len(fields.get("id", [b""])) != 1
            ):
                raise ContractViolation(f"SSE frame needs exactly one event and one data line: {block[:120]!r}")
            frame_id = fields["id"][0].decode() if "id" in fields else None
            frames.append((frame_id, fields["event"][0].decode(), fields["data"][0]))
        return frames

    def check_sse_stream(
        self, operation_id: str, stream: bytes, *, decision_request: Mapping[str, Any] | None = None
    ) -> list[SseEvent]:
        """Validate a whole SSE body: every frame, plus ids strictly increasing from 1 by 1."""
        out: list[SseEvent] = []
        expected = 1
        for frame_id, event, data in self.parse_sse(stream):
            assert event is not None
            value = self.check_sse_frame(operation_id, frame_id, event, data, decision_request=decision_request)
            if frame_id is not None:
                if int(frame_id) != expected:
                    raise _fail(operation_id, f"SSE frame id {frame_id} out of order (expected {expected})")
                expected += 1
            out.append(SseEvent(id=frame_id, event=event, data=value))
        return out
