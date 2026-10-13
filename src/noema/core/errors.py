"""Error codes, the frozen code table and the error types every module raises (spec-02 §1, spec-08 §2, IMPL-030,
IMPL-076).

Request-level failures raise `NoemaError(code, ...)`; the API maps it to the openapi `Error` envelope using
`ERROR_TABLE` (one code -> one HTTP status, `error.type` and `retryable`). Anything else that escapes a request is a
bug and becomes 500 `internal_error`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from noema.core.values import JsonValue


class ErrorType(StrEnum):
    """openapi `Error.error.type`."""

    INVALID_REQUEST = "invalid_request_error"
    AUTHENTICATION = "authentication_error"
    PERMISSION = "permission_error"
    NOT_FOUND = "not_found_error"
    CONFLICT = "conflict_error"
    CAPABILITY = "capability_error"
    RATE_LIMIT = "rate_limit_error"
    UPSTREAM = "upstream_error"
    SERVER = "server_error"


class ErrorCode(StrEnum):
    """openapi `components.schemas.ErrorCode` (36 values, in the contract's order; test-enforced)."""

    SCHEMA_VIOLATION = "schema_violation"
    UNSUPPORTED_QUESTION_TYPE = "unsupported_question_type"
    RESERVED_QUESTION_NAME = "reserved_question_name"
    UNSUPPORTED_PARAMETER = "unsupported_parameter"
    MODEL_NOT_AVAILABLE = "model_not_available"
    PROFILE_NOT_ALLOWED = "profile_not_allowed"
    INVALID_API_KEY = "invalid_api_key"
    SESSION_FORBIDDEN = "session_forbidden"
    SESSION_NOT_FOUND = "session_not_found"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    FILE_NOT_READY = "file_not_ready"
    FILE_REJECTED = "file_rejected"
    FILE_TOO_LARGE = "file_too_large"
    IMAGE_TOO_LARGE = "image_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    NO_ROUTE_SATISFIES_CONSTRAINTS = "no_route_satisfies_constraints"
    NO_CAPABLE_MEMBER = "no_capable_member"
    RPM_EXCEEDED = "rpm_exceeded"
    TPM_EXCEEDED = "tpm_exceeded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    QUEUE_TIMEOUT = "queue_timeout"
    ALL_ROUTES_UNAVAILABLE = "all_routes_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    TENANT_ACCESS_MISCONFIGURED = "tenant_access_misconfigured"
    UPSTREAM_STALLED = "upstream_stalled"
    CLIENT_TOO_SLOW = "client_too_slow"
    RESUME_WINDOW_EXPIRED = "resume_window_expired"
    SESSION_CONFLICT = "session_conflict"
    SESSION_BUSY = "session_busy"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    INVALID_END_USER_TOKEN = "invalid_end_user_token"  # noqa: S105 (an error code, not a secret)
    REQUEST_TOO_LARGE = "request_too_large"
    FEATURE_NOT_AVAILABLE = "feature_not_available"
    INTERNAL_ERROR = "internal_error"
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """The fixed wire behavior of one code. `status` is None for codes that occur only as SSE `error` events."""

    status: int | None
    type: ErrorType
    retryable: bool


def _spec(status: int | None, type_: ErrorType, retryable: bool) -> ErrorSpec:
    return ErrorSpec(status=status, type=type_, retryable=retryable)


_C, _T = ErrorCode, ErrorType
# Exactly spec-08 §2 (IMPL-030): one row per code.
ERROR_TABLE: Final[Mapping[ErrorCode, ErrorSpec]] = MappingProxyType(
    {
        _C.SCHEMA_VIOLATION: _spec(400, _T.INVALID_REQUEST, False),
        _C.UNSUPPORTED_QUESTION_TYPE: _spec(400, _T.INVALID_REQUEST, False),
        _C.RESERVED_QUESTION_NAME: _spec(400, _T.INVALID_REQUEST, False),
        _C.MODEL_NOT_AVAILABLE: _spec(400, _T.INVALID_REQUEST, False),
        _C.INVALID_API_KEY: _spec(401, _T.AUTHENTICATION, False),
        _C.INVALID_END_USER_TOKEN: _spec(401, _T.AUTHENTICATION, False),
        _C.PERMISSION_DENIED: _spec(403, _T.PERMISSION, False),
        _C.PROFILE_NOT_ALLOWED: _spec(403, _T.PERMISSION, False),
        _C.SESSION_FORBIDDEN: _spec(403, _T.PERMISSION, False),
        _C.NOT_FOUND: _spec(404, _T.NOT_FOUND, False),
        _C.SESSION_NOT_FOUND: _spec(404, _T.NOT_FOUND, False),
        _C.IDEMPOTENCY_KEY_REUSED: _spec(409, _T.CONFLICT, False),
        _C.SESSION_CONFLICT: _spec(409, _T.CONFLICT, False),
        _C.SESSION_BUSY: _spec(409, _T.CONFLICT, True),
        _C.FILE_NOT_READY: _spec(409, _T.CONFLICT, True),
        _C.FILE_REJECTED: _spec(409, _T.CONFLICT, False),
        _C.RESUME_WINDOW_EXPIRED: _spec(410, _T.INVALID_REQUEST, False),
        _C.REQUEST_TOO_LARGE: _spec(413, _T.INVALID_REQUEST, False),
        _C.FILE_TOO_LARGE: _spec(413, _T.INVALID_REQUEST, False),
        _C.IMAGE_TOO_LARGE: _spec(413, _T.INVALID_REQUEST, False),
        _C.UNSUPPORTED_MEDIA_TYPE: _spec(415, _T.INVALID_REQUEST, False),
        _C.NO_ROUTE_SATISFIES_CONSTRAINTS: _spec(422, _T.CAPABILITY, False),
        _C.NO_CAPABLE_MEMBER: _spec(422, _T.CAPABILITY, False),
        _C.UNSUPPORTED_PARAMETER: _spec(422, _T.CAPABILITY, False),
        _C.FEATURE_NOT_AVAILABLE: _spec(422, _T.CAPABILITY, False),
        _C.RPM_EXCEEDED: _spec(429, _T.RATE_LIMIT, True),
        _C.TPM_EXCEEDED: _spec(429, _T.RATE_LIMIT, True),
        _C.QUEUE_TIMEOUT: _spec(429, _T.RATE_LIMIT, True),
        _C.BUDGET_EXHAUSTED: _spec(429, _T.RATE_LIMIT, False),  # never raised in Phase 1 (IMPL-030)
        _C.INTERNAL_ERROR: _spec(500, _T.SERVER, True),
        _C.NOT_IMPLEMENTED: _spec(501, _T.INVALID_REQUEST, False),
        _C.ALL_ROUTES_UNAVAILABLE: _spec(503, _T.UPSTREAM, True),
        _C.TENANT_ACCESS_MISCONFIGURED: _spec(503, _T.UPSTREAM, False),
        _C.DEADLINE_EXCEEDED: _spec(504, _T.UPSTREAM, True),
        _C.UPSTREAM_STALLED: _spec(None, _T.UPSTREAM, True),  # SSE only
        _C.CLIENT_TOO_SLOW: _spec(None, _T.UPSTREAM, False),  # SSE only
    }
)
del _C, _T


class NoemaError(Exception):
    """A request-level failure with a contract error code.

    `message` is client-facing: short, never containing exception text, secrets or model output. `param` names the
    offending field path (e.g. `questions[1].choices`) or header. `retry_after_s` feeds the `retry-after` header
    (429/503). `details` is the optional `error.details` object.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        param: str | None = None,
        details: Mapping[str, JsonValue] | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = ErrorCode(code)
        self.message: str = message
        self.param: str | None = param
        self.details: Mapping[str, JsonValue] | None = details
        self.retry_after_s: int | None = retry_after_s

    @property
    def spec(self) -> ErrorSpec:
        """The `ERROR_TABLE` row of this code."""
        return ERROR_TABLE[self.code]

    def envelope(self, request_id: str) -> dict[str, JsonValue]:
        """The openapi `Error` body: `{"error": {type, code, message, param, request_id, retryable[, details]}}`."""
        error: dict[str, JsonValue] = {
            "type": self.spec.type.value,
            "code": self.code.value,
            "message": self.message,
            "param": self.param,
            "request_id": request_id,
            "retryable": self.spec.retryable,
        }
        if self.details is not None:
            error["details"] = dict(self.details)
        return {"error": error}

    def sse_event(self) -> dict[str, JsonValue]:
        """The openapi `ErrorEvent` payload: `{"type": "error", code, message, retryable}`."""
        return {"type": "error", "code": self.code.value, "message": self.message, "retryable": self.spec.retryable}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value!r}, param={self.param!r})"


class ErrorKind(StrEnum):
    """Provider-error classes (IMPL-076; the mapping table is spec-02 §4.3)."""

    RETRYABLE = "retryable"
    THROTTLED = "throttled"
    FATAL = "fatal"
    INVALID_REQUEST = "invalid_request"
    TENANT_ACCESS = "tenant_access"


TenantAccessCause = Literal[
    "role_not_allowlisted",
    "external_id_unavailable",
    "external_id_invalid",
    "access_denied",
    "sts_unavailable",
    "account_mismatch",
    "model_access_denied",
    "ambient_account_mismatch",
]


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    """A classified provider failure.

    `message` is short and redacted, never model output. `provider_code` is e.g. "ThrottlingException", "429" or
    "timeout:first_event". `cause` is set iff `kind` is TENANT_ACCESS (ValueError otherwise).
    """

    kind: ErrorKind
    message: str
    provider_code: str | None = None
    retry_after_s: float | None = None
    cause: TenantAccessCause | None = None

    def __post_init__(self) -> None:
        if (self.kind is ErrorKind.TENANT_ACCESS) != (self.cause is not None):
            raise ValueError("ClassifiedError.cause is set iff kind == tenant_access")


class TenantAccessMisconfigured(NoemaError):
    """503 `tenant_access_misconfigured` (not retryable) with `details = {"cause": cause}` (ADR-001 §2)."""

    def __init__(self, cause: TenantAccessCause, message: str = "Tenant access is misconfigured.") -> None:
        super().__init__(ErrorCode.TENANT_ACCESS_MISCONFIGURED, message, details={"cause": cause})
        self.cause: TenantAccessCause = cause


class SecretMissing(NoemaError):
    """500 `internal_error` from a `SecretsProvider` when the secret, or the requested version, does not exist. A
    backend that cannot be read (throttled, timed out, denied) raises a plain `NoemaError(internal_error)`, so a
    caller can tell "no such version" (e.g. no previous api-key pepper) from an outage. The envelope is the same."""

    def __init__(self, message: str = "A platform secret is missing.") -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, message)


class ForbiddenInEnvironment(Exception):
    """A component was constructed in a `NOEMA_ENV` that does not allow it (AmbientBroker outside dev/test,
    ADR-001 §7; a sandbox backend outside its environments, ADR-002 §7). The process must exit non-zero."""
