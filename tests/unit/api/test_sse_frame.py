"""SSE framing (IMPL-034): pre-serialized frames, monotonic ids, `: ping` after 15 s, round trip through the
contract validator's parser."""

from __future__ import annotations

import tracemalloc
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import orjson
import pytest
from starlette.middleware import Middleware
from starlette.types import Message
from tests.unit.api.apifakes import ManualClock

from noema.api import sse_frame
from noema.api.app import NoemaApp
from noema.api.sse_frame import (
    PING,
    PING_INTERVAL_S,
    RAW_SEND_KEY,
    FrameSequence,
    SseResponse,
    WriteOutcome,
    encode_frame,
    encode_unnumbered,
    write_frames,
)
from noema.contracts.validate import ContractValidator
from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue

pytestmark = pytest.mark.unit
CREATED: dict[str, JsonValue] = {"type": "turn.created", "turn_id": "turn_1", "session_id": None, "created_at": 1}
ERROR: dict[str, JsonValue] = {
    "type": "error",
    "code": "all_routes_unavailable",
    "message": "line1\nline2\r\n",
    "retryable": True,
}
FILE: dict[str, JsonValue] = {
    "id": "file_1",
    "filename": "a.pdf",
    "media_type": "application/pdf",
    "bytes": 3,
    "sha256": "ab",
}
FILE_READY: dict[str, JsonValue] = {
    **FILE,
    "status": "ready",
    "derivatives": [{"kind": "text", "id": "der_1", "status": "ready"}],
}


def test_a_frame_is_serialized_once_on_one_data_line() -> None:
    frame = encode_frame(7, ERROR)
    assert frame.id == 7
    assert frame.type == "error"
    assert frame.payload == orjson.dumps(ERROR)
    assert frame.wire == b"id: 7\nevent: error\ndata: " + frame.payload + b"\n\n"
    assert frame.wire.count(b"\n") == 4  # control characters inside strings are escaped
    with pytest.raises(ValueError, match="start at 1"):
        encode_frame(0, ERROR)
    with pytest.raises(ValueError, match="event type"):
        encode_frame(1, {"type": "a\nb"})
    with pytest.raises(ValueError, match="event type"):
        encode_frame(1, {"no": "type"})


def test_a_retained_frame_holds_only_right_sized_bytes() -> None:
    # orjson.dumps returns bytes backed by a ~4 KiB allocation whatever the payload size; a frame must not keep it
    events: list[dict[str, JsonValue]] = [{**CREATED, "turn_id": f"turn_{i}"} for i in range(2000)]
    seq = FrameSequence()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        frames = [seq.next(e) for e in events]
        per_frame = (tracemalloc.get_traced_memory()[0] - base) / len(frames)
    finally:
        tracemalloc.stop()
    assert per_frame < 3 * len(frames[-1].wire) + 256, per_frame  # wire + payload + the SseFrame, not 4 KiB
    assert frames[-1].payload == orjson.dumps(events[-1])
    assert frames[-1].wire == b"id: 2000\nevent: turn.created\ndata: " + frames[-1].payload + b"\n\n"


def test_frame_ids_are_monotonic_per_stream() -> None:
    seq = FrameSequence()
    assert [seq.next(CREATED).id for _ in range(3)] == [1, 2, 3]
    assert seq.last_id == 3
    resumed = FrameSequence(last_id=41)
    assert resumed.next(ERROR).id == 42
    with pytest.raises(ValueError, match="last_id"):
        FrameSequence(last_id=-1)


def test_round_trip_through_the_contract_parser() -> None:
    seq = FrameSequence()
    turn_stream = seq.next(CREATED).wire + PING + seq.next(ERROR).wire
    events = ContractValidator().check_sse_stream("createTurn", turn_stream)
    assert [(e.id, e.event, e.data) for e in events] == [("1", "turn.created", CREATED), ("2", "error", ERROR)]
    files = FrameSequence()
    file_stream = files.next(FILE_READY, event_type="file.updated").wire
    (event,) = ContractValidator().check_sse_stream("watchFile", file_stream)
    assert (event.event, event.data) == ("file.updated", FILE_READY)


def test_the_unnumbered_client_too_slow_frame() -> None:
    too_slow: dict[str, JsonValue] = {
        "type": "error",
        "code": "client_too_slow",
        "message": "Too slow.",
        "retryable": False,
    }
    wire = encode_unnumbered(too_slow)
    assert wire == b"event: error\ndata: " + orjson.dumps(too_slow) + b"\n\n"
    ((frame_id, event, data),) = ContractValidator.parse_sse(wire)
    assert frame_id is None
    assert ContractValidator().check_sse_frame("createTurn", frame_id, event or "", data) == too_slow


async def _feed(receive: anyio.abc.ObjectReceiveStream[SseFrame]) -> AsyncIterator[SseFrame]:
    async with receive:
        async for frame in receive:
            yield frame


async def test_write_frames_pings_after_15_s_without_a_write() -> None:
    clock = ManualClock()
    sent: list[bytes] = []
    out: dict[str, Any] = {}
    tx, rx = anyio.create_memory_object_stream[SseFrame](10)
    seq = FrameSequence()

    async def send(chunk: bytes) -> None:
        sent.append(chunk)

    async def run() -> None:
        out["outcome"] = await write_frames(_feed(rx), send, clock=clock)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await anyio.wait_all_tasks_blocked()
        clock.advance(PING_INTERVAL_S - 0.001)
        await anyio.wait_all_tasks_blocked()
        assert sent == []
        clock.advance(0.001)
        await anyio.wait_all_tasks_blocked()
        assert sent == [PING]
        clock.advance(10)
        await tx.send(seq.next(CREATED))  # a frame restarts the 15 s window
        await anyio.wait_all_tasks_blocked()
        clock.advance(10)
        await anyio.wait_all_tasks_blocked()
        assert sent == [PING, encode_frame(1, CREATED).wire]  # 10 s after the frame: no ping yet
        clock.advance(5)
        await anyio.wait_all_tasks_blocked()
        assert sent[-1] == PING
        await tx.aclose()
    assert out["outcome"] == "completed"
    assert clock.sleepers == 0


async def test_write_frames_reports_a_disconnect() -> None:
    async def frames() -> AsyncIterator[SseFrame]:
        yield encode_frame(1, CREATED)

    async def send(chunk: bytes) -> None:
        raise OSError("gone")

    assert await write_frames(frames(), send, clock=ManualClock()) == "disconnected"


class _Asgi:
    """Captures the messages an ASGI response sends; `disconnect` releases the receive channel."""

    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.disconnect = anyio.Event()

    async def receive(self) -> Message:
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        self.sent.append(message)


async def test_sse_response_sends_headers_frames_and_the_end_of_body() -> None:
    asgi, closed = _Asgi(), []

    async def write(send: Any) -> WriteOutcome:
        await send(encode_frame(1, CREATED).wire)
        return "completed"

    async def on_close(outcome: WriteOutcome) -> None:
        closed.append(outcome)

    response = SseResponse(write, headers={"x-extra": "1"}, on_close=on_close)
    await response({"type": "http"}, asgi.receive, asgi.send)
    start, frame, end = asgi.sent
    headers = dict(start["headers"])
    assert start["status"] == 200
    assert headers[b"content-type"] == b"text/event-stream; charset=utf-8"
    assert (headers[b"cache-control"], headers[b"x-accel-buffering"], headers[b"x-extra"]) == (b"no-cache", b"no", b"1")
    assert b"content-length" not in headers
    assert frame == {"type": "http.response.body", "body": encode_frame(1, CREATED).wire, "more_body": True}
    assert end == {"type": "http.response.body", "body": b"", "more_body": False}
    assert closed == ["completed"]


async def test_sse_response_stops_the_writer_when_the_client_disconnects() -> None:
    asgi, closed, cancelled = _Asgi(), [], anyio.Event()

    async def write(send: Any) -> WriteOutcome:
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()
        return "completed"

    async def on_close(outcome: WriteOutcome) -> None:
        closed.append(outcome)

    async with anyio.create_task_group() as tg:
        tg.start_soon(SseResponse(write, on_close=on_close), {"type": "http"}, asgi.receive, asgi.send)
        await anyio.wait_all_tasks_blocked()
        asgi.disconnect.set()
    assert cancelled.is_set()
    assert closed == ["disconnected"]
    assert len(asgi.sent) == 1  # only the response start: no end-of-body after a disconnect


async def test_sse_response_propagates_writer_bugs() -> None:
    asgi = _Asgi()

    async def write(send: Any) -> WriteOutcome:
        raise LookupError("bug")

    with pytest.raises(LookupError):
        await SseResponse(write)({"type": "http"}, asgi.receive, asgi.send)


async def test_sse_response_writes_frames_to_the_raw_send_and_the_rest_through_the_stack() -> None:
    wrapped, raw = _Asgi(), _Asgi()

    async def write(send: Any) -> WriteOutcome:
        await send(encode_frame(1, CREATED).wire)
        await send(encode_frame(2, CREATED).wire)
        return "completed"

    await SseResponse(write)({"type": "http", RAW_SEND_KEY: raw.send}, wrapped.receive, wrapped.send)
    assert [m["type"] for m in wrapped.sent] == ["http.response.start", "http.response.body"]
    assert wrapped.sent[1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert [m["body"] for m in raw.sent] == [encode_frame(1, CREATED).wire, encode_frame(2, CREATED).wire]


def _sse_app(*, raw: bool) -> tuple[NoemaApp, list[Message]]:
    """A NoemaApp with one SSE route behind a pure-ASGI middleware that records the messages it passes on."""
    seen: list[Message] = []

    class Recorder:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            async def record(message: Message) -> None:
                seen.append(message)
                await send(message)

            await self.app(scope, receive, record)

    async def stream() -> SseResponse:
        async def write(send: Any) -> WriteOutcome:
            for i in (1, 2, 3):
                await send(encode_frame(i, CREATED).wire)
            return "completed"

        return SseResponse(write)

    app = NoemaApp(middleware=[Middleware(Recorder)])
    app.add_api_route("/s", stream)
    app.raw_sse_send = raw
    return app, seen


@pytest.mark.parametrize("raw", [True, False])
async def test_noema_app_sends_sse_frames_past_the_middleware_only_when_raw(raw: bool) -> None:
    app, seen = _sse_app(raw=raw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        body = (await http.get("/s")).content
    assert body == b"".join(encode_frame(i, CREATED).wire for i in (1, 2, 3))
    frames_seen = [m for m in seen if m["type"] == "http.response.body" and m.get("more_body")]
    assert len(frames_seen) == (0 if raw else 3)  # the start and the end of body always pass the middleware
    assert seen[0]["type"] == "http.response.start" and seen[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }


class _Draining(_Asgi):
    """uvicorn's `send` for a client that stopped reading: the closing body waits for the backlog to drain."""

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order

    async def send(self, message: Message) -> None:
        self.sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            self.order.append("final send")
            await anyio.sleep_forever()


async def test_a_too_slow_client_is_closed_before_the_final_send_and_that_send_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QL-1 / fault B2 (C3): `on_close` (the turn's cancel) runs before the closing send, which uvicorn holds until
    the slow client drained its backlog; that send is bounded, so the response ends while the client stays
    connected and reads nothing."""
    monkeypatch.setattr(sse_frame, "FINAL_SEND_TIMEOUT_S", 0.05, raising=False)
    order: list[str] = []
    asgi = _Draining(order)

    async def write(send: Any) -> WriteOutcome:
        return "client_too_slow"

    async def on_close(outcome: WriteOutcome) -> None:
        order.append(f"on_close {outcome}")

    with anyio.fail_after(5):  # a safety net: before the fix the closing send never returned
        await SseResponse(write, on_close=on_close)({"type": "http"}, asgi.receive, asgi.send)
    assert order == ["on_close client_too_slow", "final send"]


async def test_on_close_runs_when_the_writer_fails() -> None:
    """A writer bug still releases what `on_close` holds (e.g. a watchFile slot); the error propagates."""
    asgi, closed = _Asgi(), []

    async def write(send: Any) -> WriteOutcome:
        raise LookupError("bug")

    async def on_close(outcome: WriteOutcome) -> None:
        closed.append(outcome)

    with pytest.raises(LookupError):
        await SseResponse(write, on_close=on_close)({"type": "http"}, asgi.receive, asgi.send)
    assert closed == ["disconnected"]
