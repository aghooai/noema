"""E sprint A: `AiohttpUnary` over real localhost sockets: classification of connection failures (refused,
disconnect before the response, a truncated body), keep-alive reuse, cancellation closes the connection (never
reused), redirects are not followed, `Accept-Encoding: identity`, and no ambient proxy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

import anyio
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials

from noema.adapters._converse_wire import converse_url, signed_headers
from noema.adapters._unary import AiohttpUnary, RawResponse
from noema.adapters.classify import classify, is_connect_error, possibly_delivered
from noema.core.errors import ErrorKind
from noema.core.protocols.credentials import AwsCredentials, MantleAuth, TenantCredentials
from noema.core.secret import SecretStr

pytestmark = pytest.mark.integration
OK = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
Action = bytes | Literal["close", "hang", "truncate", "stall_body"]


class Server:
    """A raw HTTP/1.1 server; `act(n)` answers the n-th request (1-based): bytes, close the connection, hang until
    the client closes it, send a truncated body and close, or send the headers and part of the body and then hang
    until the client closes it."""

    def __init__(self, act: Callable[[int], Action]) -> None:
        self.act, self.connections, self.client_closed, self.heads = act, 0, 0, list[bytes]()
        self.closed_event = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.lower().split(b"\r\n")
                length = next((int(h.split(b":")[1]) for h in lines if h.startswith(b"content-length:")), 0)
                await reader.readexactly(length)
                self.heads.append(head)
                action = self.act(len(self.heads))
                if action == "close":
                    return
                if action == "stall_body":
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"a": 1')
                    await writer.drain()
                if action in ("hang", "stall_body"):
                    if await reader.read(1) == b"":
                        self.client_closed += 1
                        self.closed_event.set()
                    return
                if action == "truncate":
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"a": 1')
                    await writer.drain()
                    return
                assert isinstance(action, bytes)
                writer.write(action)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            self.client_closed += 1
        finally:
            writer.close()


@asynccontextmanager
async def serve(act: Callable[[int], Action]) -> AsyncIterator[tuple[Server, str]]:
    server = Server(act)
    srv = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        yield server, f"http://127.0.0.1:{port}/model/m/converse"
    finally:
        srv.close()


async def post(unary: AiohttpUnary, url: str) -> RawResponse | Exception:
    try:
        return await unary.post(url, {"Content-Type": "application/json"}, b"{}", connect_s=3.0)
    except Exception as exc:
        return exc


@asynccontextmanager
async def client() -> AsyncIterator[AiohttpUnary]:
    unary = AiohttpUnary(None)
    try:
        yield unary
    finally:
        await unary.aclose()


async def test_refused_is_a_pre_send_connect_error() -> None:
    async with serve(lambda _: OK) as (_, url):
        pass  # closed: nothing listens on the port any more
    async with client() as unary:
        exc = await post(unary, url)
    assert isinstance(exc, Exception) and type(exc).__name__ == "EndpointConnectionError"
    assert classify(exc).kind is ErrorKind.RETRYABLE and is_connect_error(exc) and not possibly_delivered(exc)


async def test_a_disconnect_before_the_response_may_have_been_delivered() -> None:
    async with serve(lambda _: "close") as (_, url), client() as unary:
        exc = await post(unary, url)
    assert isinstance(exc, Exception) and type(exc).__name__ == "ConnectionClosedError"
    assert classify(exc).kind is ErrorKind.RETRYABLE and is_connect_error(exc) and possibly_delivered(exc)


async def test_a_truncated_body_is_a_streaming_error_never_a_connect_error() -> None:
    async with serve(lambda _: "truncate") as (_, url), client() as unary:
        exc = await post(unary, url)
    assert isinstance(exc, Exception) and type(exc).__name__ == "ResponseStreamingError"
    assert classify(exc).kind is ErrorKind.RETRYABLE and not is_connect_error(exc)


async def test_keep_alive_reuse_no_redirects_identity_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # an ambient proxy is never used
    redirect = b"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://169.254.169.254/\r\nContent-Length: 0\r\n\r\n"
    async with serve(lambda n: redirect if n == 3 else OK) as (server, url), client() as unary:
        answers = [await post(unary, url) for _ in range(3)]
    assert [getattr(a, "status", a) for a in answers] == [200, 200, 307]
    assert server.connections == 1 and len(server.heads) == 3
    assert all(b"\r\naccept-encoding: identity\r\n" in h.lower() for h in server.heads)


async def test_cancellation_closes_the_connection_and_it_is_not_reused() -> None:
    async with serve(lambda n: "hang" if n == 2 else OK) as (server, url), client() as unary:
        assert getattr(await post(unary, url), "status", None) == 200
        with anyio.move_on_after(0.3) as scope:
            await post(unary, url)  # the second request hangs: the caller gives up
        assert scope.cancelled_caught
        with anyio.fail_after(5):
            await server.closed_event.wait()
        assert getattr(await post(unary, url), "status", None) == 200
    assert server.connections == 2  # the cancelled connection was closed, the next request opened a new one


async def test_cancellation_during_the_body_read_closes_the_connection() -> None:
    """Wave E (a test gap): a call cancelled after the response headers, while reading the body, closes its
    connection (a partly read response never goes back to the pool); the next request opens a new one."""
    async with serve(lambda n: "stall_body" if n == 2 else OK) as (server, url), client() as unary:
        assert getattr(await post(unary, url), "status", None) == 200
        with anyio.move_on_after(0.3) as scope:
            await post(unary, url)  # the headers arrive, the body stalls: the caller gives up
        assert scope.cancelled_caught
        with anyio.fail_after(5):
            await server.closed_event.wait()
        assert getattr(await post(unary, url), "status", None) == 200
    assert server.connections == 2 and len(server.heads) == 3


FROZEN = datetime(2026, 10, 11, 7, 30, 15)  # noqa: DTZ001 (botocore's SigV4 clock is naive UTC)
COLON_IDS = (
    "anthropic.claude-3-haiku-20240307-v1:0",
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc123",
)


def _creds() -> TenantCredentials:
    aws = AwsCredentials("ASIAEXAMPLEEXAMPLE01", SecretStr("c2VjcmV0LXNlY3JldA=="), SecretStr("FwoGZXIvYXdzE"), None)
    return TenantCredentials(
        "t_acme", "111122223333", "us-east-1", aws, MantleAuth("sigv4", None, None), None, "ambient"
    )


def _server_signature(origin: str, path: str, body: bytes) -> str:
    """The Authorization header AWS derives: SigV4 over the request path as received (frozen clock)."""
    request = AWSRequest(method="POST", url=origin + path, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(
        ReadOnlyCredentials("ASIAEXAMPLEEXAMPLE01", "c2VjcmV0LXNlY3JldA==", "FwoGZXIvYXdzE"), "bedrock", "us-east-1"
    ).add_auth(request)
    return str(request.headers["Authorization"])


@pytest.mark.parametrize("model_id", COLON_IDS)
@pytest.mark.parametrize("stream", [False, True])
async def test_the_wire_path_is_the_signed_path_for_a_colon_model_id(
    model_id: str, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect E-io-1: aiohttp re-quoted a str URL (%3A back to ':'), so the path on the wire was not the one SigV4
    signed and Bedrock would answer 403 InvalidSignatureException for every versioned id and ARN."""
    monkeypatch.setattr("botocore.auth.get_current_datetime", lambda *a, **k: FROZEN)
    monkeypatch.setattr("noema.adapters._sigv4.utcnow", lambda: FROZEN)  # the lean signer's clock (Wave E3)
    async with serve(lambda _: OK) as (server, base), client() as unary:
        origin = base.removesuffix("/model/m/converse")
        url = converse_url(origin, model_id, stream=stream)
        body = b'{"messages":[]}'
        headers = signed_headers(_creds(), url, body)
        if stream:
            (await unary.open(url, headers, body, connect_s=3.0)).close()
        else:
            assert getattr(await unary.post(url, headers, body, connect_s=3.0), "status", None) == 200
    request_line, *fields = server.heads[0].decode("latin-1").split("\r\n")
    wire_path = request_line.split(" ")[1]
    assert wire_path == urlsplit(url).path and "%3A" in wire_path
    received = {k.lower(): v for k, _, v in (f.partition(": ") for f in fields if f)}
    assert received["authorization"] == headers["Authorization"] == _server_signature(origin, wire_path, body)
