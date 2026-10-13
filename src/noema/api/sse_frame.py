"""SSE framing (IMPL-034, spec-02 §7 `SseFrame`, spec-08 §5).

Every frame is pre-serialized once: `id: <n>\\nevent: <type>\\ndata: <one line of JSON>\\n\\n`, ids 1, 2, ... per
stream (monotonic, never reused), no `retry:` field. A `: ping` comment is written whenever no frame was written for
15 s. orjson escapes every control character inside strings, so the JSON payload is always exactly one `data:` line.

`SseResponse` is the ASGI response for any SSE route: it sends the headers, runs a writer coroutine (the turn
stream's `services.sse.run(...)`, or `write_frames` below for simple streams such as file events) and stops it
when the client disconnects. The response start and the final empty body pass through the middleware stack (the
ingress tracker records the status, error envelopes still apply before the stream starts); the frames themselves
go to the server's own `send` when the app put it in the scope (`RAW_SEND_KEY`, `create_app` without the
test-mode response validator), skipping the four pass-through wrappers (starlette's error and exception
middlewares, the route wrapper, the ingress tracker) on every frame.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Final, Literal

import anyio
import orjson
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from noema.core.clock import Clock
from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue

SSE_MEDIA_TYPE: Final = "text/event-stream"
RAW_SEND_KEY: Final = "noema.raw_send"  # the ASGI server's `send`, set by the app outside every middleware
PING: Final = b": ping\n\n"
PING_INTERVAL_S: Final = 15.0
FINAL_SEND_TIMEOUT_S: Final = 1.0  # the closing send after client_too_slow (QL-1)

SendBytes = Callable[[bytes], Awaitable[None]]
WriteOutcome = Literal["completed", "disconnected", "client_too_slow"]


def _event_type(event: Mapping[str, JsonValue], event_type: str | None) -> str:
    etype = event_type if event_type is not None else event.get("type")
    if not isinstance(etype, str) or not etype or "\r" in etype or "\n" in etype:
        raise ValueError("an SSE event type must be a non-empty single-line string")
    return etype


def encode_frame(frame_id: int, event: Mapping[str, JsonValue], *, event_type: str | None = None) -> SseFrame:
    """Serialize one frame. `event_type` defaults to `event["type"]` (turn events); file streams pass
    `event_type="file.updated"`. Raises ValueError for an id < 1 or an invalid event type."""
    if frame_id < 1:
        raise ValueError("SSE frame ids start at 1")
    etype = _event_type(event, event_type)
    head = b"id: %d\nevent: %s\ndata: " % (frame_id, etype.encode())
    wire = b"".join((head, orjson.dumps(event), b"\n\n"))
    # orjson.dumps returns bytes backed by a ~4 KiB allocation whatever their length: keep a right-sized slice
    return SseFrame(id=frame_id, type=etype, payload=wire[len(head) : -2], wire=wire)


def encode_unnumbered(event: Mapping[str, JsonValue]) -> bytes:
    """A frame without an `id:` line: only the best-effort `error{client_too_slow}` frame (spec-02 §7)."""
    etype = _event_type(event, None)
    return b"event: %s\ndata: %s\n\n" % (etype.encode(), orjson.dumps(event))


class FrameSequence:
    """Assigns the ids of one stream: 1, 2, ... (or continuing after `last_id`, e.g. a resumed stream)."""

    def __init__(self, last_id: int = 0) -> None:
        if last_id < 0:
            raise ValueError("last_id must be >= 0")
        self._last = last_id

    @property
    def last_id(self) -> int:
        """The id of the most recent frame (0 before the first)."""
        return self._last

    def next(self, event: Mapping[str, JsonValue], *, event_type: str | None = None) -> SseFrame:
        """Encode `event` as the next frame."""
        frame = encode_frame(self._last + 1, event, event_type=event_type)
        self._last = frame.id
        return frame


async def write_frames(
    frames: AsyncIterator[SseFrame], send: SendBytes, *, clock: Clock, ping_s: float = PING_INTERVAL_S
) -> Literal["completed", "disconnected"]:
    """Write `frames` in order, plus `PING` whenever `ping_s` seconds (on `clock`) pass without a write.

    No backpressure handling: turn streams use `services.sse` (spec-02 §7 `SseWriter`); this writer is for
    low-volume streams (file events). Returns "completed" when `frames` is exhausted and "disconnected" when
    `send` raised OSError. Exceptions from `frames` propagate.
    """
    if ping_s <= 0:
        raise ValueError("ping_s must be positive")
    lock = anyio.Lock()
    last_write = clock.monotonic()
    gone = False
    error: Exception | None = None

    async with anyio.create_task_group() as tg:

        async def write(chunk: bytes) -> None:
            nonlocal last_write, gone
            async with lock:
                try:
                    await send(chunk)
                except OSError:
                    gone = True
                    tg.cancel_scope.cancel()
                    return
                last_write = clock.monotonic()

        async def pinger() -> None:
            while True:
                wait = last_write + ping_s - clock.monotonic()
                if wait > 0:
                    await clock.sleep(wait)
                else:
                    await write(PING)

        tg.start_soon(pinger)
        try:
            async for frame in frames:
                await write(frame.wire)
        except Exception as exc:  # re-raised below, outside the task group (no ExceptionGroup wrapping)
            error = exc
        finally:
            tg.cancel_scope.cancel()
    if error is not None:
        raise error
    return "disconnected" if gone else "completed"


class SseResponse(Response):
    """A `text/event-stream` response driven by `write(send_bytes) -> outcome`.

    The response start (status, `content-type: text/event-stream`, `cache-control: no-cache`,
    `x-accel-buffering: no`, plus `headers`) is sent first; the writer then runs while a watcher waits for
    `http.disconnect`, which cancels it (outcome "disconnected"). `on_close(outcome)` runs afterwards on every
    path ("disconnected" when the writer raised), before the closing empty body, e.g. so the route can cancel a
    turn whose client went away or was too slow. Wave E2 (QL-1): uvicorn's `send` first waits for the transport to
    drain, so after "client_too_slow" the closing send is bounded by `FINAL_SEND_TIMEOUT_S`; past it the response
    returns unfinished and uvicorn closes the connection (it logs "ASGI callable returned without completing
    response" once).
    """

    media_type = SSE_MEDIA_TYPE

    def __init__(
        self,
        write: Callable[[SendBytes], Awaitable[WriteOutcome]],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        on_close: Callable[[WriteOutcome], Awaitable[None]] | None = None,
    ) -> None:
        self._write = write
        self._on_close = on_close
        self.status_code = status_code
        self.background = None
        merged = {"cache-control": "no-cache", "x-accel-buffering": "no", **(headers or {})}
        self.init_headers(merged)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        outcome: WriteOutcome = "disconnected"
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            outcome = await self._stream(scope, receive, send)
        finally:  # before any further send: a too-slow client's turn is cancelled now, not once it drained (QL-1)
            if self._on_close is not None:
                await self._on_close(outcome)
        if outcome == "client_too_slow":  # uvicorn holds this send until the client read its backlog: bounded
            with anyio.move_on_after(FINAL_SEND_TIMEOUT_S):
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        elif outcome == "completed":
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _stream(self, scope: Scope, receive: Receive, send: Send) -> WriteOutcome:
        """Run the writer until it returns or the client disconnects (then "disconnected")."""
        outcome: WriteOutcome = "disconnected"
        disconnected = False
        error: Exception | None = None

        body_send: Send = scope.get(RAW_SEND_KEY) or send

        async def send_bytes(chunk: bytes) -> None:
            await body_send({"type": "http.response.body", "body": chunk, "more_body": True})

        async with anyio.create_task_group() as tg:

            async def watch() -> None:
                nonlocal disconnected
                while (await receive())["type"] != "http.disconnect":
                    pass
                disconnected = True
                tg.cancel_scope.cancel()

            tg.start_soon(watch)
            try:
                outcome = await self._write(send_bytes)
            except OSError:
                disconnected = True
            except Exception as exc:  # re-raised below, outside the task group
                error = exc
            finally:
                tg.cancel_scope.cancel()
        if error is not None:
            raise error
        return "disconnected" if disconnected else outcome
