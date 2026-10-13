"""QL-3 (C3): the SSE writer's queue never holds more than `queue_bytes` wire bytes; the frame's bytes are checked
before the push (before E3 the queue ended one frame past the mark: 1,052,270 B at the 1 MiB bound). A frame larger
than the bound still passes alone, so the stream never deadlocks on it."""

from __future__ import annotations

import pytest
from tests.fakes.clock import FakeClock

from noema.core.protocols.events import SseFrame
from noema.streaming import BackpressureSseWriter
from noema.streaming import sse_writer as writer_mod

from .test_sse_writer import Source, ev, parse, stream

pytestmark = pytest.mark.unit


def _record_high_water(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    marks: list[int] = []
    push = writer_mod._Stream.push

    def recording(self: writer_mod._Stream, frame: SseFrame) -> None:
        push(self, frame)
        marks.append(self.nbytes)

    monkeypatch.setattr(writer_mod._Stream, "push", recording)
    return marks


@pytest.mark.parametrize("bound", [1000, 4096, 10_000])
async def test_the_queue_never_holds_more_than_its_byte_bound(monkeypatch: pytest.MonkeyPatch, bound: int) -> None:
    marks = _record_high_water(monkeypatch)
    clock = FakeClock()
    events = [ev("generation.delta", attempt_id="att_1", seq=i, text="x" * 300) for i in range(120)]
    events.insert(60, ev("decision.updated", n=1))  # an unmergeable frame in between
    sent: list[bytes] = []

    async def slow(b: bytes) -> None:
        await clock.sleep(0.05)
        sent.append(b)

    async with clock.autojump():
        outcome = await BackpressureSseWriter(clock).run(Source(stream(*events), clock).gen(), slow, queue_bytes=bound)
    assert outcome == "completed"
    assert max(marks) <= bound
    got = parse(b"".join(sent))
    assert "".join(str(p["text"]) for _, e, p in got if e == "generation.delta") == "x" * 36_000


async def test_a_frame_larger_than_the_bound_passes_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    marks = _record_high_water(monkeypatch)
    clock = FakeClock()
    frames = stream(ev("generation.delta", attempt_id="att_1", seq=0, text="y" * 5000), ev("turn.completed"))
    sent: list[bytes] = []

    async def send(b: bytes) -> None:
        await clock.sleep(0.01)
        sent.append(b)

    async with clock.autojump():
        assert (
            await BackpressureSseWriter(clock).run(Source(frames, clock).gen(), send, queue_bytes=1000) == "completed"
        )
    assert len(sent) == 2 and marks[0] == len(frames[0].wire) > 1000
