"""SseWriter conformance (spec-02 §7, 04:L281): in-order writes, "disconnected" when send raises, pings after
`ping_s`, delta merging at the high-water mark, and `client_too_slow` with the id-less error frame."""

from __future__ import annotations

from collections.abc import AsyncIterator

import orjson
from tests.fakes.events import make_frame

from noema.contracts.validate import ContractValidator
from noema.core.protocols.events import SseFrame

from .kits import SseWriterKit
from .registry import kit_fixture

kit = kit_fixture("sse_writer")


def deltas(n: int, *, done: bool = True) -> list[SseFrame]:
    frames = [make_frame(1, {"type": "generation.committed", "attempt_id": "att_1", "reason": "spec_agrees"})]
    frames += [
        make_frame(i + 2, {"type": "generation.delta", "attempt_id": "att_1", "seq": i, "text": f"<{i}>"})
        for i in range(n)
    ]
    if done:
        frames.append(make_frame(n + 2, {"type": "turn.completed", "turn_id": "turn_1"}))
    return frames


async def source(
    frames: list[SseFrame], *, hang_s: float | None = None, clock: object = None
) -> AsyncIterator[SseFrame]:
    for f in frames:
        yield f
    if hang_s is not None:
        await clock.sleep(hang_s)  # type: ignore[attr-defined]


async def test_writes_every_frame_in_order(kit: SseWriterKit) -> None:
    sent: list[bytes] = []

    async def send(b: bytes) -> None:
        sent.append(b)

    frames = deltas(5)
    async with kit.clock.autojump():
        assert await kit.writer.run(source(frames), send) == "completed"
    assert sent == [f.wire for f in frames]


async def test_send_failure_is_disconnected(kit: SseWriterKit) -> None:
    async def send(b: bytes) -> None:
        raise ConnectionResetError

    async with kit.clock.autojump():
        assert await kit.writer.run(source(deltas(3)), send) == "disconnected"


async def test_ping_after_quiet_period(kit: SseWriterKit) -> None:
    sent: list[bytes] = []

    async def send(b: bytes) -> None:
        sent.append(b)

    async with kit.clock.autojump():
        result = await kit.writer.run(source(deltas(1, done=False), hang_s=40, clock=kit.clock), send, ping_s=15)
    assert result == "completed"
    assert sent.count(b": ping\n\n") == 2


async def test_slow_client_gets_merged_deltas(kit: SseWriterKit) -> None:
    sent: list[bytes] = []

    async def slow_send(b: bytes) -> None:
        await kit.clock.sleep(0.05)
        sent.append(b)

    frames = deltas(60)
    async with kit.clock.autojump():
        assert await kit.writer.run(source(frames), slow_send, queue_events=8) == "completed"
    body = b"".join(sent)
    events = ContractValidator.parse_sse(body)
    texts = "".join(orjson.loads(d)["text"] for _, e, d in events if e == "generation.delta")
    assert texts == "".join(f"<{i}>" for i in range(60))  # nothing lost, nothing duplicated
    assert len(events) < len(frames)  # merged
    assert events[-1][1] == "turn.completed"


async def test_client_too_slow(kit: SseWriterKit) -> None:
    sent: list[bytes] = []

    async def stuck_send(b: bytes) -> None:
        if sent:
            await kit.clock.sleep(3600)
        sent.append(b)

    frames = [make_frame(i + 1, {"type": "decision.updated", "n": i}) for i in range(20)]
    async with kit.clock.autojump():
        result = await kit.writer.run(source(frames), stuck_send, queue_events=4, too_slow_s=30)
    assert result == "client_too_slow"
