"""A deliberately slow SSE client for the slow-client row (06 §5, spec-10 C3): one createTurn over a raw socket with a
small receive buffer, straight to an api container on the compose network (no docker-proxy buffering in between),
reading `rate_bps` bytes per second. The bytes are kept and parsed afterwards (h11 for the chunked body, then the
ContractValidator frame by frame; coalesced frames leave id gaps, so ids must only increase)."""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass, field
from typing import Final

import anyio
import h11
import orjson

from noema.contracts.validate import ContractValidator, SseEvent

TICK_S: Final = 0.25
CREATE_TURN: Final = "createTurn"


@dataclass
class SlowClient:
    host: str
    port: int
    key: str
    body: dict[str, object]
    rate_bps: float
    deadline_ms: int = 150_000
    rcvbuf: int = 4096
    raw: bytearray = field(default_factory=bytearray)
    eof_after_s: float | None = None
    tail_bytes: int = 0  # bytes of the frame still being received when reading stopped (parse())

    def request(self) -> tuple[h11.Request, bytes]:
        payload = orjson.dumps(self.body)
        headers = [
            ("host", f"{self.host}:{self.port}"),
            ("authorization", f"Bearer {self.key}"),
            ("aghoo-version", "2026-10-01"),
            ("aghoo-deadline-ms", str(self.deadline_ms)),
            ("content-type", "application/json"),
            ("accept", "text/event-stream"),
            ("content-length", str(len(payload))),
        ]
        return h11.Request(method="POST", target="/v1/turns", headers=headers), payload

    async def run(self, stop: anyio.Event, max_s: float) -> None:
        """Send the request, then read at `rate_bps` until EOF, `stop` or `max_s`."""
        loop = asyncio.get_running_loop()
        request, payload = self.request()
        wire = h11.Connection(h11.CLIENT)
        data = wire.send(request) or b""
        data += wire.send(h11.Data(data=payload)) or b""
        data += wire.send(h11.EndOfMessage()) or b""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
        sock.setblocking(False)
        t0 = time.monotonic()
        try:
            await loop.sock_connect(sock, (self.host, self.port))
            await loop.sock_sendall(sock, data)
            per_tick = max(1, int(self.rate_bps * TICK_S))
            while not stop.is_set() and time.monotonic() - t0 < max_s:
                with anyio.move_on_after(TICK_S * 4):
                    chunk = await loop.sock_recv(sock, per_tick)
                    if not chunk:
                        self.eof_after_s = time.monotonic() - t0
                        return
                    self.raw += chunk
                await anyio.sleep(TICK_S)
        finally:
            sock.close()

    def parse(self, validator: ContractValidator) -> tuple[int, list[SseEvent]]:
        """(HTTP status, every complete SSE frame received, each contract-validated; ids strictly increasing)."""
        request, payload = self.request()
        wire = h11.Connection(h11.CLIENT)
        wire.send(request)
        wire.send(h11.Data(data=payload))
        wire.send(h11.EndOfMessage())
        wire.receive_data(bytes(self.raw))
        status, body = 0, bytearray()
        while True:
            event = wire.next_event()
            if isinstance(event, h11.Response):
                status = event.status_code
            elif isinstance(event, h11.Data):
                body += event.data
            else:
                break
        complete = bytes(body[: body.rfind(b"\n\n") + 2]) if b"\n\n" in body else b""
        self.tail_bytes = len(body) - len(complete)
        events: list[SseEvent] = []
        last = 0
        for frame_id, name, data in validator.parse_sse(complete):
            assert name is not None
            value = validator.check_sse_frame(CREATE_TURN, frame_id, name, data)
            if frame_id is not None:
                assert int(frame_id) > last, (frame_id, last)
                last = int(frame_id)
            events.append(SseEvent(id=frame_id, event=name, data=value))
        return status, events
