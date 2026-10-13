"""The error envelope (IMPL-030, spec-08 §2-§3): every HTTP error is the openapi `Error` body, never a stack trace.

`NoemaError` -> HTTP status, `error.type` and `retryable` from `ERROR_TABLE`; `x-request-id` (= `error.request_id`)
on every error; `retry-after` when the error carries `retry_after_s` (429/503); `www-authenticate: Bearer` on 401.
Anything that is not a `NoemaError` is a bug and becomes 500 `internal_error` with a fixed message (the exception
is logged server-side only).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Final

import orjson
from starlette.responses import Response
from starlette.types import Send

from noema.core.errors import ErrorCode, NoemaError

log = logging.getLogger("noema.api.errors")

JSON_CONTENT_TYPE: Final = b"application/json"
INTERNAL_MESSAGE: Final = "Internal server error."
WWW_AUTHENTICATE: Final = b"Bearer"


def internal_error() -> NoemaError:
    """500 `internal_error` (retryable) with the fixed client message."""
    return NoemaError(ErrorCode.INTERNAL_ERROR, INTERNAL_MESSAGE)


def http_error(err: NoemaError) -> NoemaError:
    """The error to send over HTTP. The SSE-only codes have no status: `upstream_stalled` maps to 503
    `all_routes_unavailable` (spec-04 §1, `stream: false`); `client_too_slow` can't happen outside SSE (500)."""
    if err.spec.status is not None:
        return err
    if err.code is ErrorCode.UPSTREAM_STALLED:
        return NoemaError(ErrorCode.ALL_ROUTES_UNAVAILABLE, "All routes are unavailable.", details=err.details)
    log.error("SSE-only error code %s raised outside an SSE stream", err.code.value)
    return internal_error()


def error_parts(
    err: NoemaError, request_id: str, extra_headers: Iterable[tuple[str, str]] = ()
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """(status, raw ASGI headers, body) of the envelope for `err` (after `http_error`)."""
    err = http_error(err)
    status = err.spec.status
    assert status is not None
    body = orjson.dumps(err.envelope(request_id))
    headers = [
        (b"content-type", JSON_CONTENT_TYPE),
        (b"content-length", str(len(body)).encode()),
        (b"x-request-id", request_id.encode("latin-1")),
    ]
    if err.retry_after_s is not None:
        headers.append((b"retry-after", str(max(1, int(err.retry_after_s))).encode()))
    if status == 401:
        headers.append((b"www-authenticate", WWW_AUTHENTICATE))
    reserved = {b"content-type", b"content-length", b"x-request-id"}
    for name, value in extra_headers:
        key = name.lower().encode("latin-1")
        if key not in reserved:
            headers.append((key, value.encode("latin-1")))
    return status, headers, body


async def send_error(
    send: Send, err: NoemaError, request_id: str, extra_headers: Iterable[tuple[str, str]] = ()
) -> int:
    """Send the envelope for `err` as a complete response; returns the HTTP status sent."""
    status, headers, body = error_parts(err, request_id, extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})
    return status


def error_response(err: NoemaError, request_id: str, extra_headers: Iterable[tuple[str, str]] = ()) -> Response:
    """The envelope for `err` as a Starlette `Response` (for exception handlers)."""
    status, headers, body = error_parts(err, request_id, extra_headers)
    response = Response(body, status_code=status)
    response.raw_headers = headers
    return response
