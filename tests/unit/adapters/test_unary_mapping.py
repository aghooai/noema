"""E sprint A: `AiohttpUnary` maps every aiohttp failure before the response headers to the botocore exception
aiobotocore's `AIOHTTPSession.send` raises for it, so the classification (kind, provider code), the connect-retry
eligibility and `possibly_delivered` of a Converse member call are unchanged; a failure while reading the body is
`ResponseStreamingError` (retryable, never retried in the adapter: IMPL-016). No network: aiohttp's request
coroutine is replaced."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import aiohttp
import pytest
from aiobotocore.httpsession import AIOHTTPSession
from aiohttp.client_reqrep import ConnectionKey
from aiohttp.http_exceptions import BadStatusLine
from botocore.awsrequest import AWSRequest

from noema.adapters._unary import AiohttpUnary
from noema.adapters.classify import classify, is_connect_error, possibly_delivered

URL = "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse"
KEY = ConnectionKey("bedrock-runtime.us-east-1.amazonaws.com", 443, True, True, None, None, None)
FAILURES: dict[str, BaseException] = {
    "connector": aiohttp.ClientConnectorError(KEY, OSError(111, "Connection refused")),
    "connector_dns": aiohttp.ClientConnectorDNSError(KEY, OSError(-2, "Name or service not known")),
    "ssl": aiohttp.ClientConnectorCertificateError(KEY, OSError("bad cert")),
    "disconnected": aiohttp.ServerDisconnectedError(),
    "payload": aiohttp.ClientPayloadError("Response payload is not completed"),
    "bad_status_line": BadStatusLine("HTTP/9"),
    "connect_timeout": aiohttp.ConnectionTimeoutError("Connection timeout to host https://x"),
    "socket_timeout": aiohttp.SocketTimeoutError("Timeout on reading data from socket"),
    "server_timeout_connect": aiohttp.ServerTimeoutError("Connect timeout"),
    "os_error": aiohttp.ClientOSError(104, "Connection reset by peer"),
    "connection": aiohttp.ClientConnectionError("closed"),
    "gaierror": socket.gaierror(-2, "Name or service not known"),
    "timeout": TimeoutError(),
    "proxy": aiohttp.ClientProxyConnectionError(KEY, OSError("proxy")),
    "other": ValueError("unexpected"),
}


def _raising(exc: BaseException) -> Any:
    async def request(*_: Any, **__: Any) -> Any:
        raise exc

    return request


async def raised(call: Any) -> Exception:
    """The exception `await call` raises (it must raise)."""
    try:
        await call
    except Exception as exc:
        return exc
    raise AssertionError("did not raise")


def facts(exc: BaseException) -> tuple[Any, ...]:
    error = classify(exc)
    return type(exc).__name__, error.kind, error.provider_code, is_connect_error(exc), possibly_delivered(exc)


@pytest.mark.parametrize("name", list(FAILURES))
async def test_failures_map_as_aiobotocore_maps_them(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aiohttp.ClientSession, "_request", _raising(FAILURES[name]))
    timeouts: Any = (3.0, 300.0)  # aiobotocore takes (connect, read)
    async with AIOHTTPSession(timeout=timeouts) as session:
        request = AWSRequest("POST", URL, data=b"{}", headers={"Content-Type": "application/json"}).prepare()
        botocore_side = await raised(session.send(request))  # type: ignore[arg-type]
    unary = AiohttpUnary(None)
    try:
        lean_side = await raised(unary.post(URL, {"Content-Type": "application/json"}, b"{}", connect_s=3.0))
    finally:
        await unary.aclose()
    assert facts(lean_side) == facts(botocore_side)


async def test_cancellation_is_not_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aiohttp.ClientSession, "_request", _raising(asyncio.CancelledError()))
    unary = AiohttpUnary(None)
    try:
        with pytest.raises(asyncio.CancelledError):
            await unary.post(URL, {}, b"{}", connect_s=3.0)
    finally:
        await unary.aclose()


class _Failing:
    status, raw_headers, closed, released = 200, ((b"Content-Type", b"application/json"),), False, False

    async def read(self) -> bytes:
        raise aiohttp.ClientPayloadError("Response payload is not completed")

    def close(self) -> None:
        self.closed = True

    def release(self) -> None:
        self.released = True

    async def __aenter__(self) -> _Failing:
        return self


async def test_a_body_read_failure_is_a_streaming_error_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _Failing()

    async def request(*_: Any, **__: Any) -> Any:
        return response

    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)
    unary = AiohttpUnary(None)
    try:
        error = await raised(unary.post(URL, {}, b"{}", connect_s=3.0))
    finally:
        await unary.aclose()
    name, kind, code, connect_error, _ = facts(error)
    assert (name, kind.value, code, connect_error) == (
        "ResponseStreamingError",
        "retryable",
        "ResponseStreamingError",
        False,
    )
    assert response.closed and not response.released  # a partly read response never goes back to the pool
