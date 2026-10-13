"""Errors raised by the client. SSE `error` events are yielded as events, never raised."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final

_TYPE_BY_STATUS: Final[Mapping[int, str]] = {
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    422: "capability_error",
    429: "rate_limit_error",
    503: "upstream_error",
    504: "upstream_error",
}
_INT: Final = re.compile(r"^\s*\d+\s*$")


class AghooError(Exception):
    """Raised for every non-2xx HTTP response (after retries); fields mirror the `Error` envelope (03 §1)."""

    def __init__(
        self,
        *,
        status: int,
        type: str,
        code: str,
        message: str,
        param: str | None,
        request_id: str | None,
        retryable: bool,
        retry_after_s: int | None,
        details: dict[str, Any] | None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.type = type
        self.code = code
        self.message = message
        self.param = param
        self.request_id = request_id
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.details = details

    def __repr__(self) -> str:
        return f"AghooError(status={self.status}, code={self.code!r}, param={self.param!r}, message={self.message!r})"


class AghooConnectionError(Exception):
    """No usable HTTP response (connect, TLS or read failure), or a stream that ended before its final event or
    carried a frame that is not valid JSON. Retried before the first stream event; raised after that."""


class AghooTimeoutError(AghooConnectionError):
    """The timeout elapsed before the response (for streams: before the first event) arrived."""


def parse_retry_after(value: str | None) -> int | None:
    """`retry-after` as integer seconds (the contract never sends the HTTP-date form)."""
    if value is None or not _INT.match(value):
        return None
    return int(value)


def _retryable_status(status: int) -> bool:
    return status in (408, 429) or (status >= 500 and status != 501)


def error_from_response(status: int, headers: Mapping[str, str], body: bytes) -> AghooError:
    """The AghooError of a non-2xx response: the envelope when the body is one, else a status-only fallback."""
    retry_after = parse_retry_after(headers.get("retry-after"))
    header_request_id = headers.get("x-request-id")
    try:
        parsed: Any = json.loads(body) if body else None
    except ValueError:
        parsed = None
    env = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(env, dict) and isinstance(env.get("code"), str) and isinstance(env.get("type"), str):
        message = env.get("message")
        param = env.get("param")
        request_id = env.get("request_id")
        retryable = env.get("retryable")
        details = env.get("details")
        return AghooError(
            status=status,
            type=env["type"],
            code=env["code"],
            message=message if isinstance(message, str) else f"HTTP {status}",
            param=param if isinstance(param, str) else None,
            request_id=request_id if isinstance(request_id, str) else header_request_id,
            retryable=retryable if isinstance(retryable, bool) else _retryable_status(status),
            retry_after_s=retry_after,
            details=details if isinstance(details, dict) else None,
        )
    text = body[:200].decode("utf-8", "replace")
    return AghooError(
        status=status,
        type=_TYPE_BY_STATUS.get(status, "server_error" if status >= 500 else "invalid_request_error"),
        code="unknown_error",
        message=f"HTTP {status}: {text}" if text else f"HTTP {status}",
        param=None,
        request_id=header_request_id,
        retryable=_retryable_status(status),
        retry_after_s=retry_after,
        details=None,
    )
