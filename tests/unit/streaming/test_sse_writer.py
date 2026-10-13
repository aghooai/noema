"""`BackpressureSseWriter` beyond the conformance suite: coalescing at the event and byte high-water marks (per
attempt, never across other frames), client_too_slow exactly `too_slow_s` after the queue filled or a send blocked,
with the id-less error frame, pings exactly `ping_s` after the last write, disconnects, and the source being closed
every time."""

from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
import orjson
import pytest
from tests.fakes.clock import FakeClock

from noema.contracts.validate import ContractValidator
from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue
from noema.streaming import BackpressureSseWriter, encode_frame


def ev(kind: str, **fields: JsonValue) -> dict[str, JsonValue]:
    return {"type": kind, **fields}


def stream(*events: dict[str, JsonValue]) -> list[SseFrame]:
    return [encode_frame(i, e) for i, e in enumerate(events, start=1)]


class Source:
    """A frame source that records whether it was closed, optionally hanging after its frames."""

    def __init__(self, frames: list[SseFrame], clock: FakeClock, *, hang_s: float = 0, fail: bool = False) -> None:
        self.frames, self.clock, self.hang_s, self.fail = frames, clock, hang_s, fail
        self.closed = False

    async def gen(self) -> AsyncIterator[SseFrame]:
        try:
            for f in self.frames:
                yield f
            if self.hang_s:
                await self.clock.sleep(self.hang_s)
            if self.fail:
                raise LookupError("source failed")
        finally:
            self.closed = True


def parse(body: bytes) -> list[tuple[str | None, str, dict[str, JsonValue]]]:
    return [(i, str(e), orjson.loads(d)) for i, e, d in ContractValidator.parse_sse(body)]


async def test_coalescing_merges_runs_per_attempt_only() -> None:
    clock = FakeClock()
    events = [ev("generation.committed", attempt_id="att_1", reason="spec_agrees")]
    events += [ev("generation.delta", attempt_id="att_1", seq=i, text=f"{i},") for i in range(40)]
    events += [ev("decision.updated", n=1)]
    events += [ev("generation.delta", attempt_id="att_1", seq=40 + i, text=f"{40 + i},") for i in range(40)]
    events += [ev("turn.completed", turn_id="turn_1")]
    sent: list[bytes] = []

    async def slow(b: bytes) -> None:
        await clock.sleep(0.05)
        sent.append(b)

    writer = BackpressureSseWriter(clock)
    async with clock.autojump():
        assert await writer.run(Source(stream(*events), clock).gen(), slow, queue_events=8) == "completed"
    got = parse(b"".join(sent))
    texts = "".join(str(p["text"]) for _, e, p in got if e == "generation.delta")
    assert texts == "".join(f"{i}," for i in range(80))
    assert writer.merged_frames > 0 and len(got) < len(events)
    ids = [int(i or 0) for i, _, _ in got]
    assert ids == sorted(ids) and ids[-1] == len(events)  # monotonic, gaps allowed, nothing after completed
    kinds = [e for _, e, _ in got]
    assert kinds.count("decision.updated") == 1  # never merged
    seqs = [int(p["seq"]) for _, e, p in got if e == "generation.delta"]  # type: ignore[arg-type]
    assert seqs == sorted(seqs) and seqs[-1] == 79  # a merged frame keeps the last seq


async def test_the_byte_bound_also_triggers_coalescing() -> None:
    clock = FakeClock()
    events = [ev("generation.delta", attempt_id="att_1", seq=i, text="x" * 100) for i in range(30)]
    sent: list[bytes] = []

    async def slow(b: bytes) -> None:
        await clock.sleep(0.05)
        sent.append(b)

    writer = BackpressureSseWriter(clock)
    async with clock.autojump():
        await writer.run(Source(stream(*events), clock).gen(), slow, queue_bytes=1000)
    got = parse(b"".join(sent))
    assert len(got) < 30 and "".join(str(p["text"]) for _, _, p in got) == "x" * 3000


async def test_client_too_slow_fires_exactly_after_too_slow_s_of_a_full_queue() -> None:
    clock = FakeClock()
    sent: list[tuple[float, bytes]] = []

    async def stuck(b: bytes) -> None:
        if sent:
            await clock.sleep(3600)
        sent.append((clock.monotonic(), b))

    source = Source(stream(*(ev("decision.updated", n=i) for i in range(10))), clock, hang_s=3600)
    writer = BackpressureSseWriter(clock)
    t0 = clock.monotonic()
    async with clock.autojump():
        result = await writer.run(source.gen(), stuck, queue_events=4, too_slow_s=30)
        done_at = clock.monotonic() - t0
    assert result == "client_too_slow" and source.closed and writer.too_slow == 1
    assert done_at == pytest.approx(31.0)  # 30 s full, then the 1 s best-effort error frame timeout


async def test_a_send_blocked_for_too_slow_s_is_client_too_slow_even_with_room_in_the_queue() -> None:
    # a client that stays connected but stops reading: coalescing keeps the queue below its bound, so only the
    # blocked send shows it; without this the writer (and its queued frames) would be pinned forever
    clock = FakeClock()
    sent: list[bytes] = []

    async def stops_reading(b: bytes) -> None:
        if sent:
            await clock.sleep(3600)
        sent.append(b)

    events = [ev("generation.committed", attempt_id="att_1", reason="spec_agrees")]
    events += [ev("generation.delta", attempt_id="att_1", seq=i, text="x") for i in range(100)]
    events += [ev("turn.completed", turn_id="turn_1")]
    source = Source(stream(*events), clock)
    writer = BackpressureSseWriter(clock)
    t0 = clock.monotonic()
    async with clock.autojump():
        result = await writer.run(source.gen(), stops_reading, too_slow_s=30)
        done_at = clock.monotonic() - t0
    assert result == "client_too_slow" and writer.too_slow == 1 and source.closed
    assert done_at == pytest.approx(31.0)  # the send blocked 30 s, then the 1 s best-effort error frame timeout


async def test_a_slow_but_reading_client_is_not_too_slow() -> None:
    clock = FakeClock()
    sent: list[bytes] = []

    async def slow(b: bytes) -> None:
        await clock.sleep(20)  # every send completes within too_slow_s
        sent.append(b)

    source = Source(stream(*(ev("decision.updated", n=i) for i in range(4))), clock)
    async with clock.autojump():
        assert await BackpressureSseWriter(clock).run(source.gen(), slow, too_slow_s=30) == "completed"
    assert len(sent) == 4


async def test_the_too_slow_frame_is_sent_when_the_client_recovers() -> None:
    clock = FakeClock()
    sent: list[bytes] = []
    blocked = [True]

    async def send(b: bytes) -> None:
        if blocked[0] and sent:
            blocked[0] = False
            await clock.sleep(3600)  # stuck until cancelled at too_slow
        sent.append(b)

    source = Source(stream(*(ev("decision.updated", n=i) for i in range(10))), clock, hang_s=3600)
    async with clock.autojump():
        result = await BackpressureSseWriter(clock).run(source.gen(), send, queue_events=2, too_slow_s=5)
    assert result == "client_too_slow"
    assert sent[-1].startswith(b"event: error\n") and b"client_too_slow" in sent[-1] and b"id:" not in sent[-1]


async def test_pings_follow_the_last_write_exactly() -> None:
    clock = FakeClock()
    times: list[tuple[float, bytes]] = []
    t0 = clock.monotonic()

    async def send(b: bytes) -> None:
        times.append((clock.monotonic() - t0, b))

    frames = stream(ev("turn.created", turn_id="t"))

    async def source() -> AsyncIterator[SseFrame]:
        yield frames[0]
        await clock.sleep(10)
        yield encode_frame(2, ev("decision.updated", n=1))
        await clock.sleep(40)

    async with clock.autojump():
        assert await BackpressureSseWriter(clock).run(source(), send, ping_s=15) == "completed"
    pings = [round(t, 3) for t, b in times if b == b": ping\n\n"]
    assert pings == [25.0, 40.0]  # 15 s after the frame at 10 s, then every 15 s; none in the first 10 s


async def test_a_failing_send_is_disconnected_and_closes_the_source() -> None:
    clock = FakeClock()

    async def send(b: bytes) -> None:
        raise ConnectionResetError

    source = Source(stream(ev("turn.created", turn_id="t")), clock, hang_s=100)
    async with clock.autojump():
        assert await BackpressureSseWriter(clock).run(source.gen(), send) == "disconnected"
    assert source.closed


async def test_a_failing_source_raises_unwrapped() -> None:
    clock = FakeClock()

    async def send(b: bytes) -> None:
        await anyio.lowlevel.checkpoint()

    source = Source(stream(ev("turn.created", turn_id="t")), clock, fail=True)
    with pytest.raises(LookupError):
        await BackpressureSseWriter(clock).run(source.gen(), send)


async def test_invalid_bounds() -> None:
    clock = FakeClock()

    async def send(b: bytes) -> None:
        await anyio.lowlevel.checkpoint()

    with pytest.raises(ValueError, match="positive"):
        await BackpressureSseWriter(clock).run(Source([], clock).gen(), send, queue_events=0)
