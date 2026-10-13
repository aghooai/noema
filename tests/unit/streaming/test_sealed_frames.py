"""Sealed turn logs are packed (`_SealedFrames`, C2b perf smoke): the frames read back after the seal equal the
frames appended, a subscriber that was reading at the seal still gets every frame, and the packed form holds no
per-frame object the GC would scan."""

from __future__ import annotations

import gc

import anyio
import pytest
from tests.fakes.clock import FakeClock

from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue
from noema.streaming import InProcessTurnEventLog
from noema.streaming.event_log import _SealedFrames
from noema.streaming.frames import encode_frame

pytestmark = [pytest.mark.unit]
T = "turn_1"
DELTAS: list[dict[str, JsonValue]] = [
    {"type": "generation.delta", "attempt_id": "att_1", "seq": i, "text": f"«ts:{i}» é\n{i}"} for i in range(5)
]
EVENTS: list[dict[str, JsonValue]] = [
    {"type": "turn.created", "turn_id": T, "session_id": None, "created_at": 1},
    *DELTAS,
    {"type": "turn.completed", "turn_id": T},
]


def _frames() -> list[SseFrame]:
    return [encode_frame(i + 1, e) for i, e in enumerate(EVENTS)]


def test_packed_frames_equal_the_originals_by_index_slice_and_negative_index() -> None:
    frames = _frames()
    packed = _SealedFrames(frames)
    assert len(packed) == len(frames)
    assert [packed[i] for i in range(len(frames))] == frames
    assert list(packed) == frames
    assert packed[-1] == frames[-1] and packed[1:3] == frames[1:3]
    with pytest.raises(IndexError):
        packed[len(frames)]
    assert len(_SealedFrames([])) == 0


def test_the_packed_form_has_no_per_frame_gc_objects() -> None:
    packed = _SealedFrames(_frames())
    gc.collect()
    assert not any(isinstance(o, SseFrame) for o in gc.get_referents(packed))
    fields = [o for o in gc.get_referents(packed) if not isinstance(o, type)]  # a heap type visits its class
    assert len(fields) == 4 and not any(gc.is_tracked(o) for o in fields)  # only the packed object is tracked


async def test_a_sealed_log_replays_the_same_frames_and_still_refuses_appends() -> None:
    log = InProcessTurnEventLog(FakeClock())
    for e in EVENTS:
        log.append(T, e)
    before = [f async for f in log.subscribe(T)]
    log.seal(T, 300)
    assert [f async for f in log.subscribe(T)] == before == _frames()
    assert [f.id async for f in log.subscribe(T, after=3)] == [4, 5, 6, 7]
    with pytest.raises(RuntimeError):
        log.append(T, EVENTS[1])


async def test_a_subscriber_reading_across_the_seal_gets_every_frame() -> None:
    clock = FakeClock()
    log = InProcessTurnEventLog(clock)
    log.append(T, EVENTS[0])
    got: list[SseFrame] = []

    async def read() -> None:
        got.extend([f async for f in log.subscribe(T)])

    async with anyio.create_task_group() as tg:
        tg.start_soon(read)
        await clock.settle()
        for e in EVENTS[1:]:
            log.append(T, e)
        log.seal(T, 300)
    assert got == _frames()
