"""Provider error classification (spec-02 §4.3, frozen; IMPL-076).

| Provider signal | `ErrorKind` |
|---|---|
| `ThrottlingException`, HTTP 429, `ServiceQuotaExceededException` | throttled (`retry_after_s` from `retry-after`) |
| connect / DNS / TLS errors, HTTP 5xx, `ServiceUnavailableException`, `InternalServerException`,
  `ModelTimeoutException`, `ModelNotReadyException`, read timeouts, a stream exception event | retryable |
| `AccessDeniedException` and `ResourceNotFoundException` (cause `model_access_denied`: Bedrock answers 404 for a
  model or inference profile the account cannot use, spec-02 §4.3 "model not enabled"), `UnrecognizedClientException` /
  `ExpiredTokenException` / Mantle 401 (cause `access_denied`; the adapter invalidates the broker),
  `TenantAccessMisconfigured` | tenant_access |
| `ValidationException`, botocore `ParamValidationError`, HTTP 400 from Mantle | invalid_request |
| anything else | fatal |

Every exception event inside a stream (ConverseStream `:message-type: exception`, a Mantle `{"error": ...}` chunk)
is `retryable`, whatever its type. `ClassifiedError.message` is a fixed phrase, never provider or model text.

The in-adapter connect retry (`is_connect_error`) covers every connection failure before any response byte
(spec-02 §4.1, spec-10 `connect_reset`). `possibly_delivered` tells the two halves apart: a failed connect, DNS
lookup, TLS handshake or request write never reached the model (IMPL-016 "no model call happened"); a reset or a
disconnect after the request was written may have, so the provider may have billed that first dispatch.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

import aiohttp
import httpx
import orjson
from botocore.eventstream import ParserError as EventStreamParserError
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    EventStreamError,
    ParamValidationError,
    ProxyConnectionError,
    ReadTimeoutError,
    ResponseStreamingError,
    SSLError,
)

from noema.core.errors import ClassifiedError, ErrorKind, TenantAccessCause, TenantAccessMisconfigured
from noema.core.protocols.adapters import AdapterError

THROTTLE_CODES: Final = frozenset({"ThrottlingException", "ServiceQuotaExceededException"})
RETRYABLE_CODES: Final = frozenset(
    {
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
        "ModelStreamErrorException",
    }
)
ACCESS_CODES: Final[Mapping[str, TenantAccessCause]] = {
    "AccessDeniedException": "model_access_denied",
    "ResourceNotFoundException": "model_access_denied",
    "UnrecognizedClientException": "access_denied",
    "ExpiredTokenException": "access_denied",
}
# Provider codes after which the tenant's cached credentials are dropped (ADR-001 §2): the next call re-assumes.
INVALIDATING_CODES: Final = frozenset({"UnrecognizedClientException", "ExpiredTokenException", "401"})
VALIDATION_CODES: Final = frozenset({"ValidationException"})

MSG_THROTTLED: Final = "provider throttled the request"
MSG_RETRYABLE: Final = "provider unavailable"
MSG_CONNECT: Final = "connection to the provider failed"
MSG_TIMEOUT: Final = "provider timed out"
MSG_STREAM: Final = "provider stream failed"
MSG_MALFORMED: Final = "malformed provider response"
MSG_ACCESS: Final = "provider denied access"
MSG_INVALID: Final = "provider rejected the request"
MSG_FATAL: Final = "unexpected provider error"

_CONNECT_BOTO: Final = (
    EndpointConnectionError,
    ConnectTimeoutError,
    ConnectionClosedError,
    SSLError,
    ProxyConnectionError,
)
# httpx read/write/protocol errors count only while the adapter awaits the response headers (`_send` wraps nothing
# else), i.e. before any response byte, like aiohttp's ServerDisconnectedError -> ConnectionClosedError.
# Of these, a reset or disconnect after the request was written may have reached the model (`possibly_delivered`).
_MAYBE_DELIVERED: Final = (ConnectionClosedError, httpx.RemoteProtocolError, httpx.ReadError)
# aiobotocore wraps every aiohttp connection error in EndpointConnectionError; only these were raised before sending.
_PRE_SEND_AIOHTTP: Final = (aiohttp.ClientConnectorError, socket.gaierror)
_CONNECT_ERRORS: Final = (
    *_CONNECT_BOTO,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


class ProviderHttpError(Exception):
    """A non-200 Mantle response (raised by the Mantle adapter; classified by HTTP status)."""

    def __init__(self, status: int, retry_after_s: float | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after_s = retry_after_s


class StreamFault(Exception):
    """A failure the adapter detected while reading a stream (malformed frame, error chunk, truncation)."""

    def __init__(self, error: ClassifiedError) -> None:
        super().__init__(error.message)
        self.error = error


def pascal(code: str) -> str:
    """ConverseStream exception types are camelCase (`throttlingException`); service codes are PascalCase."""
    return code[:1].upper() + code[1:]


def retry_after(headers: Mapping[str, str] | None) -> float | None:
    """`retry-after` as seconds (delta-seconds or an HTTP date), None when absent or invalid."""
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        value = (when - datetime.now(UTC)).total_seconds()
    return max(0.0, value) if value == value else None  # NaN guard


def is_connect_error(exc: BaseException) -> bool:
    """A connect, DNS or TLS failure, or a reset before any response byte (eligible for the one in-adapter retry;
    the adapters call it only for failures while awaiting the response headers)."""
    return isinstance(exc, _CONNECT_ERRORS)


def possibly_delivered(exc: BaseException) -> bool:
    """A connect error (`is_connect_error`) raised after the request may have reached the provider: a reset or a
    disconnect while awaiting the response headers. False for connect, DNS, TLS, proxy and write failures."""
    if isinstance(exc, _MAYBE_DELIVERED):
        return True
    if isinstance(exc, EndpointConnectionError):
        cause: object = exc.kwargs.get("error")  # absent when botocore raised it without an underlying error
        return isinstance(cause, BaseException) and not isinstance(cause, _PRE_SEND_AIOHTTP)
    return False


def stream_error(provider_code: str, message: str = MSG_STREAM) -> ClassifiedError:
    """A failure after the stream opened: always retryable (spec-02 §4.3, "a stream exception event")."""
    return ClassifiedError(ErrorKind.RETRYABLE, message, provider_code)


def timeout_error(name: str) -> ClassifiedError:
    """`timeout:<name>` (connect, first_event, first_answer, idle, deadline)."""
    return ClassifiedError(ErrorKind.RETRYABLE, MSG_TIMEOUT, f"timeout:{name}")


def http_status_error(status: int, retry_after_s: float | None = None) -> ClassifiedError:
    """Mantle HTTP status classification."""
    code = str(status)
    if status == 429:
        return ClassifiedError(ErrorKind.THROTTLED, MSG_THROTTLED, code, retry_after_s)
    if status == 401:
        return ClassifiedError(ErrorKind.TENANT_ACCESS, MSG_ACCESS, code, cause="access_denied")
    if status == 403:
        return ClassifiedError(ErrorKind.TENANT_ACCESS, MSG_ACCESS, code, cause="model_access_denied")
    if status == 400:
        return ClassifiedError(ErrorKind.INVALID_REQUEST, MSG_INVALID, code)
    if status == 408 or status >= 500:
        return ClassifiedError(ErrorKind.RETRYABLE, MSG_RETRYABLE, code)
    return ClassifiedError(ErrorKind.FATAL, MSG_FATAL, code)


def client_error(exc: ClientError) -> ClassifiedError:
    """A botocore `ClientError` (an HTTP error response before the body)."""
    if isinstance(exc, EventStreamError):
        return stream_error(pascal(str(exc.response.get("Error", {}).get("Code") or "StreamException")))
    error = exc.response.get("Error", {})
    code = pascal(str(error.get("Code") or ""))
    meta = exc.response.get("ResponseMetadata", {})
    status = int(meta.get("HTTPStatusCode") or 0)
    headers = meta.get("HTTPHeaders") or {}
    if code in THROTTLE_CODES:
        return ClassifiedError(ErrorKind.THROTTLED, MSG_THROTTLED, code, retry_after(headers))
    if code in RETRYABLE_CODES:
        return ClassifiedError(ErrorKind.RETRYABLE, MSG_RETRYABLE, code)
    if code in ACCESS_CODES:
        return ClassifiedError(ErrorKind.TENANT_ACCESS, MSG_ACCESS, code, cause=ACCESS_CODES[code])
    if code in VALIDATION_CODES:
        return ClassifiedError(ErrorKind.INVALID_REQUEST, MSG_INVALID, code)
    if status == 429:
        return ClassifiedError(ErrorKind.THROTTLED, MSG_THROTTLED, code or "429", retry_after(headers))
    if status >= 500:
        return ClassifiedError(ErrorKind.RETRYABLE, MSG_RETRYABLE, code or str(status))
    return ClassifiedError(ErrorKind.FATAL, MSG_FATAL, code or (str(status) if status else None))


def classify(exc: BaseException) -> ClassifiedError:
    """spec-02 §4.3 over every exception an adapter call can raise (`Adapter.classify_error`)."""
    if isinstance(exc, AdapterError):
        return exc.error
    if isinstance(exc, StreamFault):
        return exc.error
    if isinstance(exc, TenantAccessMisconfigured):
        return ClassifiedError(ErrorKind.TENANT_ACCESS, "tenant access misconfigured", cause=exc.cause)
    if isinstance(exc, ProviderHttpError):
        return http_status_error(exc.status, exc.retry_after_s)
    if isinstance(exc, ClientError):
        return client_error(exc)
    if isinstance(exc, ParamValidationError):
        return ClassifiedError(ErrorKind.INVALID_REQUEST, MSG_INVALID, "ParamValidationError")
    if isinstance(exc, ConnectTimeoutError | httpx.ConnectTimeout):
        return ClassifiedError(ErrorKind.RETRYABLE, MSG_CONNECT, "timeout:connect")
    if is_connect_error(exc):
        return ClassifiedError(ErrorKind.RETRYABLE, MSG_CONNECT, type(exc).__name__)
    if isinstance(exc, ReadTimeoutError | httpx.ReadTimeout | asyncio.TimeoutError | TimeoutError):
        return ClassifiedError(ErrorKind.RETRYABLE, MSG_TIMEOUT, "timeout:read")
    if isinstance(exc, httpx.TimeoutException):
        return ClassifiedError(ErrorKind.RETRYABLE, MSG_TIMEOUT, f"timeout:{type(exc).__name__}")
    if isinstance(exc, EventStreamParserError):
        return stream_error("malformed:eventstream", MSG_MALFORMED)
    if isinstance(exc, orjson.JSONDecodeError):
        return stream_error("malformed:json", MSG_MALFORMED)
    if isinstance(exc, ResponseStreamingError | httpx.TransportError | aiohttp.ClientError | OSError):
        return ClassifiedError(ErrorKind.RETRYABLE, MSG_CONNECT, type(exc).__name__)
    return ClassifiedError(ErrorKind.FATAL, MSG_FATAL, type(exc).__name__)
