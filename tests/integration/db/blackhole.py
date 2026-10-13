"""A TCP proxy for the hung-dependency tests (Wave E): it forwards until `freeze()`, then the connections open at
that moment keep their sockets but pass no byte either way (a failover or partition without FIN/RST, a dropped
NAT or conntrack entry); connections opened later forward normally (the healed path a reconnect takes)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class BlackholeProxy:
    def __init__(self, upstream_port: int) -> None:
        self._upstream = upstream_port
        self._frozen: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.accepted = 0
        self.port = 0

    def freeze(self) -> None:
        """Blackhole every connection open now."""
        self._frozen.update(range(self.accepted))

    async def _pump(self, n: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while data := await reader.read(65536):
                if n in self._frozen:
                    continue  # swallowed: never forwarded, never closed
                writer.write(data)
                await writer.drain()

    async def _handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        n, self.accepted = self.accepted, self.accepted + 1
        up_r, up_w = await asyncio.open_connection("127.0.0.1", self._upstream)
        for task in (self._pump(n, client_r, up_w), self._pump(n, up_r, client_w)):
            t = asyncio.create_task(task)
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)


@asynccontextmanager
async def blackhole_proxy(upstream_port: int) -> AsyncIterator[BlackholeProxy]:
    proxy = BlackholeProxy(upstream_port)
    server = await asyncio.start_server(proxy._handle, "127.0.0.1", 0)
    proxy.port = int(server.sockets[0].getsockname()[1])
    try:
        yield proxy
    finally:
        server.close()
        for task in list(proxy._tasks):
            task.cancel()
