"""spec-02 §4.3 error classification (frozen, IMPL-076), including the exact ValidationException strings the probes
recorded (`probes/results/*.json`), botocore SDK-side rejections, stream exception events and Mantle statuses."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import aiohttp
import httpx
import orjson
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    EventStreamError,
    ParamValidationError,
    ReadTimeoutError,
    SSLError,
)
from tests.unit.adapters.emulator_wire import PROBE_VALIDATION

from noema.adapters.classify import (
    ProviderHttpError,
    StreamFault,
    classify,
    is_connect_error,
    possibly_delivered,
    retry_after,
    stream_error,
)
from noema.core.errors import ClassifiedError, ErrorKind, TenantAccessMisconfigured
from noema.core.protocols.adapters import AdapterError


def client_error(
    code: str, message: str = "x", status: int = 400, headers: dict[str, str] | None = None
) -> ClientError:
    response: Any = {
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": headers or {}},
    }
    return ClientError(response, "Converse")


@pytest.mark.parametrize("key", sorted(PROBE_VALIDATION))
def test_probe_validation_strings_are_invalid_request_and_never_echoed(key: str) -> None:
    message = PROBE_VALIDATION[key]
    error = classify(client_error("ValidationException", message))
    assert error.kind is ErrorKind.INVALID_REQUEST and error.provider_code == "ValidationException"
    assert message not in error.message and "temperature" not in error.message


def test_sdk_side_rejection_is_invalid_request() -> None:
    # the probes' "where": "sdk" audio rejection is a botocore ParamValidationError
    exc = ParamValidationError(report='Unknown parameter in messages[0].content[0]: "audio", must be one of: text')
    assert classify(exc).kind is ErrorKind.INVALID_REQUEST


@pytest.mark.parametrize(
    ("code", "status", "kind", "cause"),
    [
        ("ThrottlingException", 429, ErrorKind.THROTTLED, None),
        ("ServiceQuotaExceededException", 400, ErrorKind.THROTTLED, None),
        ("ModelNotReadyException", 429, ErrorKind.RETRYABLE, None),
        ("ServiceUnavailableException", 503, ErrorKind.RETRYABLE, None),
        ("InternalServerException", 500, ErrorKind.RETRYABLE, None),
        ("ModelTimeoutException", 408, ErrorKind.RETRYABLE, None),
        ("AccessDeniedException", 403, ErrorKind.TENANT_ACCESS, "model_access_denied"),
        ("UnrecognizedClientException", 403, ErrorKind.TENANT_ACCESS, "access_denied"),
        ("ExpiredTokenException", 403, ErrorKind.TENANT_ACCESS, "access_denied"),
        ("ResourceNotFoundException", 404, ErrorKind.TENANT_ACCESS, "model_access_denied"),  # model not enabled
        ("ModelErrorException", 424, ErrorKind.FATAL, None),
        ("", 502, ErrorKind.RETRYABLE, None),
        ("", 429, ErrorKind.THROTTLED, None),
        ("", 409, ErrorKind.FATAL, None),
    ],
)
def test_bedrock_client_errors(code: str, status: int, kind: ErrorKind, cause: str | None) -> None:
    error = classify(client_error(code, status=status))
    assert error.kind is kind and error.cause == cause
    assert error.provider_code == (code or str(status))


def test_throttling_retry_after_header() -> None:
    error = classify(client_error("ThrottlingException", status=429, headers={"retry-after": "2.5"}))
    assert error.kind is ErrorKind.THROTTLED and error.retry_after_s == 2.5
    assert classify(client_error("ThrottlingException", status=429)).retry_after_s is None


def test_retry_after_forms() -> None:
    soon = datetime.now(UTC) + timedelta(seconds=30)
    assert retry_after({"retry-after": "3"}) == 3.0
    assert 25 <= (retry_after({"Retry-After": format_datetime(soon, usegmt=True)}) or 0) <= 30
    assert retry_after({"retry-after": "soon"}) is None
    assert retry_after({"retry-after": "-4"}) == 0.0
    assert retry_after(None) is None


@pytest.mark.parametrize("exception_type", ["modelStreamErrorException", "throttlingException", "validationException"])
def test_stream_exception_events_are_retryable(exception_type: str) -> None:
    exc = EventStreamError({"Error": {"Code": exception_type, "Message": "m"}}, "ConverseStream")
    error = classify(exc)
    assert error.kind is ErrorKind.RETRYABLE
    assert error.provider_code == exception_type[0].upper() + exception_type[1:]


@pytest.mark.parametrize(
    ("exc", "code", "connect"),
    [
        (EndpointConnectionError(endpoint_url="https://x"), "EndpointConnectionError", True),
        (ConnectTimeoutError(endpoint_url="https://x"), "timeout:connect", True),
        (SSLError(endpoint_url="https://x", error=OSError("tls")), "SSLError", True),
        (ConnectionClosedError(endpoint_url="https://x"), "ConnectionClosedError", True),
        (ReadTimeoutError(endpoint_url="https://x"), "timeout:read", False),
        (httpx.ConnectError("refused"), "ConnectError", True),
        (httpx.ConnectTimeout("slow"), "timeout:connect", True),
        (httpx.ReadTimeout("slow"), "timeout:read", False),
        (httpx.ReadError("reset"), "ReadError", True),
        (TimeoutError(), "timeout:read", False),
        (ConnectionResetError(), "ConnectionResetError", False),
    ],
)
def test_transport_errors_are_retryable(exc: BaseException, code: str, connect: bool) -> None:
    error = classify(exc)
    assert error.kind is ErrorKind.RETRYABLE and error.provider_code == code
    assert is_connect_error(exc) is connect


@pytest.mark.parametrize(
    ("status", "kind", "cause"),
    [
        (400, ErrorKind.INVALID_REQUEST, None),
        (401, ErrorKind.TENANT_ACCESS, "access_denied"),
        (403, ErrorKind.TENANT_ACCESS, "model_access_denied"),
        (404, ErrorKind.FATAL, None),
        (408, ErrorKind.RETRYABLE, None),
        (422, ErrorKind.FATAL, None),
        (429, ErrorKind.THROTTLED, None),
        (500, ErrorKind.RETRYABLE, None),
        (503, ErrorKind.RETRYABLE, None),
    ],
)
def test_mantle_http_statuses(status: int, kind: ErrorKind, cause: str | None) -> None:
    error = classify(ProviderHttpError(status, 1.0 if status == 429 else None))
    assert error.kind is kind and error.cause == cause and error.provider_code == str(status)
    assert error.retry_after_s == (1.0 if status == 429 else None)


def test_passthrough_and_fallbacks() -> None:
    known = ClassifiedError(ErrorKind.THROTTLED, "t", "x")
    assert classify(AdapterError(known)) is known
    assert classify(StreamFault(stream_error("malformed:x"))).provider_code == "malformed:x"
    broker = classify(TenantAccessMisconfigured("external_id_invalid"))
    assert broker.kind is ErrorKind.TENANT_ACCESS and broker.cause == "external_id_invalid"
    try:
        orjson.loads(b"{")
    except orjson.JSONDecodeError as exc:
        assert classify(exc).provider_code == "malformed:json"
    unknown = classify(KeyError("boom"))
    assert unknown.kind is ErrorKind.FATAL and unknown.provider_code == "KeyError" and "boom" not in unknown.message


def _aiohttp_os_error() -> aiohttp.ClientOSError:
    return aiohttp.ClientOSError(104, "Connection reset by peer")


@pytest.mark.parametrize(
    ("exc", "delivered"),
    [
        (httpx.ConnectError("refused"), False),
        (httpx.ConnectTimeout("slow"), False),
        (httpx.WriteError("broken pipe"), False),  # the request was never fully written
        (httpx.RemoteProtocolError("Server disconnected without sending a response"), True),
        (httpx.ReadError("reset"), True),
        (EndpointConnectionError(endpoint_url="https://x"), False),
        (EndpointConnectionError(endpoint_url="https://x", error=socket.gaierror("dns")), False),
        (EndpointConnectionError(endpoint_url="https://x", error=_aiohttp_os_error()), True),  # reset after the send
        (ConnectionClosedError(endpoint_url="https://x"), True),  # aiohttp ServerDisconnectedError
        (ConnectTimeoutError(endpoint_url="https://x"), False),
        (SSLError(endpoint_url="https://x", error=OSError("tls")), False),
    ],
)
def test_connect_errors_after_the_request_write_are_possibly_delivered(exc: BaseException, delivered: bool) -> None:
    # adapters review (IMPL-016): both halves are retried (spec-02 §4.1 "before any response byte"); only the
    # possibly delivered half may have been billed
    assert is_connect_error(exc) and possibly_delivered(exc) is delivered
