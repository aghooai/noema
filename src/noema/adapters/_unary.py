"""The lean unary HTTP path of the non-streaming calls (decision, verification and labeling members; spec-02 §4.1
`complete`): one pooled aiohttp client per (tenant, account, region) that POSTs a pre-built, pre-signed request
and returns the whole response (C2b perf smoke §6: aiobotocore's per-call machinery and httpx cost 0.7-1.4 ms of
CPU per member call; this path about 0.25 ms).

`AiohttpUnary` maps aiohttp's exceptions exactly as aiobotocore's `AIOHTTPSession.send` does (botocore
`EndpointConnectionError`, `ConnectionClosedError`, `ConnectTimeoutError`, `ReadTimeoutError`, `SSLError`,
`ProxyConnectionError`, `HTTPClientError`), so `classify` and the in-adapter connect retry see the same exceptions
as before. The transport settings are aiobotocore's: connect timeout 3 s, read timeout 300 s, keep-alive 12 s, no
redirects followed (botocore never follows them), `Accept-Encoding: identity`, no ambient proxies or netrc. The
URL goes out exactly as signed (`yarl.URL(url, encoded=True)`, as aiobotocore sends it): aiohttp re-quotes a str
URL, which decodes the model id's `%3A` back to `:` and breaks SigV4 for every versioned id and ARN.
A failure while reading the body (after the response headers) is botocore's `ResponseStreamingError`
(retryable, never the in-adapter connect retry: IMPL-016 retries only before any response byte). `HttpxUnary`
runs the same requests over an injected httpx transport (the tests' wire emulator)."""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

import aiohttp
import httpx
from aiohttp import ClientResponse
from aiohttp.http_exceptions import BadStatusLine
from botocore.exceptions import (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    ProxyConnectionError,
    ReadTimeoutError,
    ResponseStreamingError,
    SSLError,
)
from botocore.httpsession import create_urllib3_context, get_cert_path
from yarl import URL

CONNECT_TIMEOUT_S: Final = 3.0
READ_TIMEOUT_S: Final = 300.0
KEEPALIVE_S: Final = 12.0  # aiobotocore DEFAULT_KEEPALIVE_TIMEOUT
MAX_CONNECTIONS: Final = 512


@dataclass(frozen=True, slots=True)
class RawResponse:
    """A whole HTTP response: status, headers (lower-cased names) and body."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class UnaryTransport(Protocol):
    """POST one request and read its whole response. Transport failures raise (classified by `classify`)."""

    async def post(self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float) -> RawResponse: ...

    async def aclose(self) -> None: ...


class StreamedResponse(Protocol):
    """A response whose body is read as it arrives (ConverseStream). Read failures are the transport's own."""

    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    async def chunk(self) -> bytes:
        """The next bytes of the body as they arrive; b"" at its end."""
        ...

    async def read(self) -> bytes:
        """The rest of the body (an error response)."""
        ...

    def close(self) -> None:
        """Close the response without reading further (its connection is not reused)."""
        ...


class StreamTransport(UnaryTransport, Protocol):
    """A unary transport that can also open a streamed response (the Converse transport)."""

    async def open(
        self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float
    ) -> StreamedResponse: ...


def tls_context() -> ssl.SSLContext:
    """aiobotocore's verifying TLS context (urllib3 defaults, botocore's CA bundle). Sync: run it in a worker
    thread (loading the CA bundle takes 5-20 ms)."""
    context: ssl.SSLContext = create_urllib3_context()
    context.check_hostname = True
    context.load_verify_locations(get_cert_path(True), None, None)
    return context


class AiohttpUnary:
    """One pooled aiohttp client (see the module docstring); `tls` None only for plain-http endpoints (dev/test)."""

    def __init__(self, tls: ssl.SSLContext | None, *, max_connections: int = MAX_CONNECTIONS) -> None:
        connector = aiohttp.TCPConnector(limit=max_connections, ssl=tls or True, keepalive_timeout=KEEPALIVE_S)
        self._default = aiohttp.ClientTimeout(sock_connect=CONNECT_TIMEOUT_S, sock_read=READ_TIMEOUT_S)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=self._default,
            skip_auto_headers=("Content-Type",),
            auto_decompress=False,
            trust_env=False,
        )
        self._timeouts: dict[float, aiohttp.ClientTimeout] = {}

    def _timeout(self, connect_s: float) -> aiohttp.ClientTimeout:
        if connect_s == CONNECT_TIMEOUT_S:
            return self._default
        found = self._timeouts.get(connect_s)
        if found is None:
            found = self._timeouts[connect_s] = aiohttp.ClientTimeout(sock_connect=connect_s, sock_read=READ_TIMEOUT_S)
        return found

    async def _send(self, url: str, headers: Mapping[str, str], body: bytes, connect_s: float) -> ClientResponse:
        """POST and wait for the response headers (failures mapped as aiobotocore's `send` maps them)."""
        try:
            context = self._session.post(
                URL(url, encoded=True),  # as signed: a str URL is re-quoted (%3A back to ':'; aiobotocore's form)
                data=body,
                headers={**headers, "Accept-Encoding": "identity"},
                allow_redirects=False,
                timeout=self._timeout(connect_s),
            )
            return await context.__aenter__()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _sent_error(exc, url) from exc

    async def open(self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float) -> StreamedResponse:
        return _Streamed(await self._send(url, headers, body, connect_s))

    async def post(self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float) -> RawResponse:
        response = await self._send(url, headers, body, connect_s)
        try:
            content = await response.read()
        except BaseException as exc:
            response.close()  # a partly read response never goes back to the pool
            if isinstance(exc, Exception):  # after the response headers: never the connect retry (IMPL-016)
                raise ResponseStreamingError(error=exc) from exc
            raise
        response.release()
        return RawResponse(response.status, _names(response), content)

    async def aclose(self) -> None:
        await self._session.close()


def _names(response: ClientResponse) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in response.raw_headers}


class _Streamed:
    """An aiohttp response read as it arrives (`StreamedResponse`); read failures raise aiohttp's own exceptions,
    as aiobotocore's event stream surfaced them."""

    def __init__(self, response: ClientResponse) -> None:
        self._response = response
        self.status = response.status
        self.headers = _names(response)

    async def chunk(self) -> bytes:
        return await self._response.content.readany()

    async def read(self) -> bytes:
        return await self._response.read()

    def close(self) -> None:
        self._response.close()


def _sent_error(exc: Exception, url: str) -> Exception:
    """aiobotocore's `AIOHTTPSession.send` mapping (in its order) of a failure before the response headers."""
    if isinstance(exc, aiohttp.ClientSSLError):
        return SSLError(endpoint_url=url, error=exc)
    if isinstance(exc, aiohttp.ClientProxyConnectionError | aiohttp.ClientHttpProxyError):
        return ProxyConnectionError(proxy_url="", error=exc)
    if isinstance(exc, aiohttp.ServerDisconnectedError | aiohttp.ClientPayloadError | BadStatusLine):
        return ConnectionClosedError(error=exc, request=None, endpoint_url=url)
    if isinstance(exc, aiohttp.ServerTimeoutError):
        if str(exc).lower().startswith("connect"):
            return ConnectTimeoutError(endpoint_url=url, error=exc)
        return ReadTimeoutError(endpoint_url=url, error=exc)
    if isinstance(exc, aiohttp.ClientConnectorError | aiohttp.ClientConnectionError | socket.gaierror):
        return EndpointConnectionError(endpoint_url=url, error=exc)
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError
        return ReadTimeoutError(endpoint_url=url, error=exc)
    return HTTPClientError(error=exc)


class HttpxUnary:
    """The unary path over an httpx transport (the tests' wire emulator, `MantleAdapter(transport=...)`)."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._client = httpx.AsyncClient(transport=transport, trust_env=False)

    async def post(self, url: str, headers: Mapping[str, str], body: bytes, *, connect_s: float) -> RawResponse:
        timeout = httpx.Timeout(READ_TIMEOUT_S, connect=connect_s)
        request = self._client.build_request("POST", url, content=body, headers=headers, timeout=timeout)
        response = await self._client.send(request, stream=True)
        try:
            content = await response.aread()
        except httpx.TransportError as exc:  # after the response headers: never the connect retry (IMPL-016)
            raise ResponseStreamingError(error=exc) from exc
        finally:
            await response.aclose()
        return RawResponse(response.status_code, {k.lower(): v for k, v in response.headers.items()}, content)

    async def aclose(self) -> None:
        await self._client.aclose()
