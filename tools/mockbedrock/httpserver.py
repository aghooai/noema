"""A minimal asyncio HTTP/1.1 server on `httptools` for the wire mock.

An ASGI server cannot reset a TCP connection, hold a request without sending a byte, or emit a deliberately broken
chunked stream, so the mock owns its transport. Requests on one connection are handled in order (keep-alive; no
pipelining needed by botocore/aiohttp/httpx). A handler gets a `Request` and a `Responder`; when the peer closes
the connection the handler task is cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from http import HTTPStatus
from typing import Final, cast

import httptools

LOG: Final = logging.getLogger("mockbedrock.http")
MAX_BODY_BYTES: Final = 64 * 1024 * 1024
Headers = Sequence[tuple[str, str]]


class ClientGone(ConnectionError):
    """The peer closed the connection; nothing more can be written."""


class Request:
    """One parsed request. `headers` are lower-cased (repeated headers joined by ", "); `received_ns` is the
    clock reading when the last body byte arrived."""

    __slots__ = ("body", "headers", "keep_alive", "method", "path", "query", "received_ns")

    def __init__(
        self, method: str, path: str, query: str, headers: dict[str, str], body: bytes, keep_alive: bool, at: int
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.keep_alive = keep_alive
        self.received_ns = at


Handler = Callable[[Request, "Responder"], Awaitable[None]]


def _head(status: int, headers: Headers, extra: str) -> bytes:
    lines = [f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"]
    lines.extend(f"{k}: {v}\r\n" for k, v in headers)
    lines.append(extra)
    lines.append("\r\n")
    return "".join(lines).encode("latin-1")


class Responder:
    """Writes the response of one request: `send` (complete, Content-Length) or `start` + `write`* + `end`
    (chunked). `reset` aborts with a TCP RST; `wait_closed` holds the request until the peer goes away."""

    def __init__(self, conn: _Connection, keep_alive: bool) -> None:
        self._conn = conn
        self._keep_alive = keep_alive
        self.started = False
        self.finished = False

    @property
    def closed(self) -> bool:
        return self._conn.closed

    def _connection_header(self) -> str:
        return "connection: keep-alive\r\n" if self._keep_alive else "connection: close\r\n"

    async def send(self, status: int, headers: Headers, body: bytes) -> None:
        """A complete response with a Content-Length."""
        self.started = True
        extra = f"content-length: {len(body)}\r\n" + self._connection_header()
        await self._conn.write(_head(status, headers, extra) + body)
        self.finished = True

    async def start(self, status: int, headers: Headers) -> None:
        """Send the head of a chunked response."""
        self.started = True
        await self._conn.write(_head(status, headers, "transfer-encoding: chunked\r\n" + self._connection_header()))

    async def write(self, data: bytes) -> None:
        """One chunk of a started chunked response (empty data is skipped)."""
        if data:
            await self._conn.write(b"%x\r\n%b\r\n" % (len(data), data))

    async def end(self) -> None:
        """The terminating chunk."""
        await self._conn.write(b"0\r\n\r\n")
        self.finished = True

    def reset(self) -> None:
        """Abort the connection with a TCP RST (SO_LINGER 0), before or after any byte."""
        self._conn.reset()

    def close(self) -> None:
        """Close the connection (FIN) after what was written."""
        self._conn.close()

    async def wait_closed(self) -> None:
        """Wait until the peer closes the connection (a hang)."""
        await self._conn.closed_event.wait()


class _Connection(asyncio.Protocol):
    """One client connection: httptools callbacks, a request queue and one handler task at a time."""

    def __init__(self, server: HttpServer) -> None:
        self._server = server
        self._parser = httptools.HttpRequestParser(self)
        self._transport: asyncio.Transport | None = None
        self._queue: deque[Request] = deque()
        self._task: asyncio.Task[None] | None = None
        self._writable = asyncio.Event()
        self._writable.set()
        self.closed_event = asyncio.Event()
        self._url = b""
        self._headers: dict[str, str] = {}
        self._body = bytearray()
        self._too_large = False

    @property
    def closed(self) -> bool:
        return self.closed_event.is_set()

    # --- asyncio.Protocol --------------------------------------------------------------------------------------------
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast("asyncio.Transport", transport)  # uvloop's TCPTransport is not an asyncio subclass
        self._server.connections.add(self)

    def data_received(self, data: bytes) -> None:
        try:
            self._parser.feed_data(data)
        except (httptools.HttpParserError, httptools.HttpParserUpgrade):  # no protocol upgrades (h2c, websocket)
            self._reject(400)

    def connection_lost(self, exc: Exception | None) -> None:
        self.closed_event.set()
        self._writable.set()
        self._server.connections.discard(self)
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def pause_writing(self) -> None:
        self._writable.clear()

    def resume_writing(self) -> None:
        self._writable.set()

    # --- httptools callbacks -----------------------------------------------------------------------------------------
    def on_message_begin(self) -> None:
        self._url, self._headers, self._body, self._too_large = b"", {}, bytearray(), False

    def on_url(self, url: bytes) -> None:
        self._url += url

    def on_header(self, name: bytes, value: bytes) -> None:
        key = name.decode("latin-1").lower()
        text = value.decode("latin-1")
        self._headers[key] = f"{self._headers[key]}, {text}" if key in self._headers else text

    def on_headers_complete(self) -> None:
        if self._headers.get("expect", "").lower() == "100-continue" and self._transport is not None:
            self._transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")

    def on_body(self, body: bytes) -> None:
        if len(self._body) + len(body) > self._server.max_body:
            self._too_large = True
            return
        self._body += body

    def on_message_complete(self) -> None:
        if self._too_large:
            self._reject(413)
            return
        url = httptools.parse_url(self._url)
        request = Request(
            method=self._parser.get_method().decode("latin-1"),
            path=url.path.decode("latin-1"),
            query=(url.query or b"").decode("latin-1"),
            headers=self._headers,
            body=bytes(self._body),
            keep_alive=self._parser.should_keep_alive(),
            at=self._server.clock_ns(),
        )
        self._body = bytearray()  # an idle keep-alive connection must not pin its last body
        self._queue.append(request)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    # --- writing ------------------------------------------------------------------------------------------------------
    async def write(self, data: bytes) -> None:
        if self.closed or self._transport is None or self._transport.is_closing():
            raise ClientGone("the client closed the connection")
        self._transport.write(data)
        if not self._writable.is_set():
            await self._writable.wait()

    def reset(self) -> None:
        if self._transport is None or self._transport.is_closing():
            return
        sock = self._transport.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self._transport.abort()

    def close(self) -> None:
        if self._transport is not None and not self._transport.is_closing():
            self._transport.close()

    def _reject(self, status: int) -> None:
        if self._transport is not None and not self._transport.is_closing():
            self._transport.write(_head(status, (), "content-length: 0\r\nconnection: close\r\n"))
            self._transport.close()

    async def _run(self) -> None:
        while self._queue and not self.closed:
            request = self._queue.popleft()
            responder = Responder(self, request.keep_alive)
            try:
                await self._server.handler(request, responder)
            except ClientGone:
                return
            except Exception:
                LOG.exception("mockbedrock handler failed for %s %s", request.method, request.path)
                if not responder.started and not self.closed:
                    with contextlib.suppress(ClientGone):
                        await responder.send(500, (("content-type", "text/plain"),), b"mockbedrock internal error")
                self.close()
                return
            if not responder.finished or not request.keep_alive:
                self.close()
                return


class HttpServer:
    """Listens on (host, port); port 0 binds an ephemeral port (`port` after `start`)."""

    def __init__(self, handler: Handler, *, clock_ns: Callable[[], int], max_body: int = MAX_BODY_BYTES) -> None:
        self.handler = handler
        self.clock_ns = clock_ns
        self.max_body = max_body
        self.connections: set[_Connection] = set()
        self._server: asyncio.Server | None = None

    @property
    def started(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the server is not started")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self, host: str, port: int, *, reuse_port: bool = False) -> None:
        """Listen on (host, port); `reuse_port` (SO_REUSEPORT) lets several mock processes share one port, the
        kernel spreading the connections over them (load runs: spec-10 §5 runs the mock as 4 processes)."""
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(
            lambda: _Connection(self), host, port, backlog=4096, reuse_port=reuse_port
        )

    async def stop(self) -> None:
        """Stop listening, then abort every open connection (held requests are cancelled)."""
        if self._server is not None:
            self._server.close()
        for conn in list(self.connections):
            conn.reset()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
