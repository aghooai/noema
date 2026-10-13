"""`InProcessTurnEventLog` (spec-02 §7 `TurnEventLog`, IMPL-021): the authoritative per-turn log of this pod, with
an optional best-effort Redis Streams mirror (`RedisEventMirror`) for idempotent attach from another pod.

- `append` (sync, the per-event hot path) assigns the next id, serializes the event once into its `SseFrame`, wakes
  subscribers and hands the frame to the mirror's queue (never awaits Redis);
- `subscribe` yields the frames with id > after, then live frames, and ends after `turn.completed`;
- `seal` keeps the log `ttl_s` (= `platform.events.resume_window_s`) and gives the mirror the same EXPIRE;
- `idle_seconds` (the abandon watchdog) combines local subscribers with the mirror's `turn_attach` heartbeat;
- `run()` (a background task: `services.runtime.spawn`) runs the mirror writer and drops expired or orphaned logs.
"""

from __future__ import annotations

import asyncio
import logging
from array import array
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Final, overload

import anyio
import anyio.lowlevel

from noema.core.clock import Clock
from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue

from .frames import TERMINAL_EVENT, encode_frame
from .mirror import RedisEventMirror

log = logging.getLogger("noema.streaming")
SWEEP_EVERY_S: Final = 30.0
ORPHAN_AFTER_S: Final = 3600.0  # an unsealed log with no append for this long belongs to a turn that died


Row = tuple[int, str, bytes, bytes]  # (id, type, payload, wire)
Packed = tuple[bytes, bytes, bytes, tuple[str, ...]]  # (blob, end offsets, head lengths, types): see _SealedFrames


class _LiveFrames(Sequence[SseFrame]):
    """An unsealed turn's frames as plain `(id, type, payload, wire)` tuples, which the GC untracks after their first
    young collection, while an `SseFrame` instance stays tracked: ~50 per live turn that every full collection
    scanned (E3, QL-5). Indexing rebuilds an equal `SseFrame`; `append` is the only mutation."""

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: list[Row] = []

    def append(self, frame: SseFrame) -> None:
        self._rows.append((frame.id, frame.type, frame.payload, frame.wire))

    def rows(self) -> list[Row]:
        return self._rows

    def __len__(self) -> int:
        return len(self._rows)

    @overload
    def __getitem__(self, index: int) -> SseFrame: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[SseFrame]: ...
    def __getitem__(self, index: int | slice) -> SseFrame | Sequence[SseFrame]:
        if isinstance(index, slice):
            return [SseFrame(*row) for row in self._rows[index]]
        return SseFrame(*self._rows[index])


class _SealedFrames(Sequence[SseFrame]):
    """A sealed turn's frames packed into one bytes blob (the wires back to back) plus offset arrays: one tracked
    object per turn instead of one `SseFrame` per event. Sealed logs stay for the resume window (300 s), so a busy
    pod keeps hundreds of thousands of frames; as objects every full GC scanned them (C2b perf smoke: 58% of the
    tracked heap at 50 streams, 47 ms per full collection; reports/perf/c2b-perf-smoke.md). Indexing rebuilds an
    equal `SseFrame` (the wire and payload are copies, so reads after the seal are a little slower)."""

    __slots__ = ("_blob", "_ends", "_heads", "_types")

    def __init__(self, frames: Sequence[SseFrame]) -> None:
        self._blob, self._ends, self._heads, self._types = _pack(frames)

    @classmethod
    def of(cls, packed: Packed) -> _SealedFrames:
        """A view over packed frames (a sealed `_TurnLog` keeps only the untracked tuple, E3)."""
        view = cls.__new__(cls)
        view._blob, view._ends, view._heads, view._types = packed
        return view

    def __len__(self) -> int:
        return len(self._types)

    @overload
    def __getitem__(self, index: int) -> SseFrame: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[SseFrame]: ...
    def __getitem__(self, index: int | slice) -> SseFrame | Sequence[SseFrame]:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        kind = self._types[index]  # IndexError out of range
        ends, heads = memoryview(self._ends).cast("Q"), memoryview(self._heads).cast("I")
        wire = self._blob[ends[index - 1] if index > 0 else 0 : ends[index]]
        return SseFrame(id=index + 1, type=kind, payload=wire[heads[index] : -2], wire=wire)


def _pack(frames: Sequence[SseFrame]) -> Packed:
    rows = frames.rows() if isinstance(frames, _LiveFrames) else [(f.id, f.type, f.payload, f.wire) for f in frames]
    ends = array("Q")  # end offset of frame i in the blob
    heads = array("I")  # len(wire) - len(payload) - 2: the `id: ..\nevent: ..\ndata: ` prefix
    end = 0
    for _, _, payload, wire in rows:
        end += len(wire)
        ends.append(end)
        heads.append(len(wire) - len(payload) - 2)
    # bytes and tuples of str are not GC-tracked (an array is), so neither is the tuple of them
    return b"".join(row[3] for row in rows), ends.tobytes(), heads.tobytes(), tuple(row[1] for row in rows)


class _TurnLog:
    """One turn's frames (frames[i].id == i + 1) and its subscriber bookkeeping. Sealed, it keeps its frames as one
    untracked tuple (`frames` is then a view over it): one tracked object per turn of the resume window (E3)."""

    __slots__ = (
        "_event",
        "_live",
        "_packed",
        "closed",
        "expires_at",
        "last_append",
        "last_delivery",
        "sealed",
        "subscribers",
    )

    def __init__(self, now: float) -> None:
        self._live: _LiveFrames | None = _LiveFrames()  # until the seal (append raises after it)
        self._packed: Packed | None = None
        self.sealed = False
        self.closed = False  # dropped from the log (expired or orphaned): subscribers end
        self.expires_at: float | None = None
        self.subscribers = 0
        self.last_delivery = now  # the abandon clock starts at the turn's first append
        self.last_append = now
        # One asyncio.Event for the log's life, cleared by each waiter before it waits (not an anyio.Event per wait:
        # every subscriber wakes once per frame; E sprint B). Every subscriber that waits before a signal() gets it
        # (the event resolves each waiter's own future), and a subscriber re-checks the frames before it waits.
        self._event: asyncio.Event | None = None

    @property
    def frames(self) -> _LiveFrames | _SealedFrames:
        if self._live is not None:
            return self._live
        assert self._packed is not None
        return _SealedFrames.of(self._packed)

    def pack(self) -> None:
        """At the seal: the frames packed (a subscriber mid-iteration keeps the live frames it holds)."""
        assert self._live is not None
        self._packed, self._live = _pack(self._live), None

    def close(self) -> None:
        self.closed = True
        self.signal()

    def signal(self) -> None:
        if self._event is not None:
            self._event.set()

    def drop_wake(self) -> None:
        """After the seal's `signal()`: nothing waits on a sealed log again (a subscriber re-checks `sealed` before it
        waits), so the resume window keeps no Event and waiter deque per turn (E3: 2 of a sealed turn's 5 tracked
        objects)."""
        self._event = None

    async def wait(self) -> None:
        event = self._event
        if event is None:
            event = self._event = asyncio.Event()
        event.clear()
        await event.wait()


class InProcessTurnEventLog:
    """Implements `TurnEventLog`. `mirror` None = in-process only (attach_remote always None)."""

    def __init__(
        self, clock: Clock, *, mirror: RedisEventMirror | None = None, orphan_after_s: float = ORPHAN_AFTER_S
    ) -> None:
        self._clock = clock
        self._mirror = mirror
        self._orphan_after_s = orphan_after_s
        self._logs: dict[str, _TurnLog] = {}

    def _local(self, turn_id: str) -> _TurnLog | None:
        tlog = self._logs.get(turn_id)
        if tlog is not None and tlog.expires_at is not None and self._clock.monotonic() >= tlog.expires_at:
            del self._logs[turn_id]
            tlog.close()
            return None
        return tlog

    def append(self, turn_id: str, event: Mapping[str, JsonValue]) -> int:
        tlog = self._local(turn_id)
        now = self._clock.monotonic()
        if tlog is None:
            tlog = self._logs[turn_id] = _TurnLog(now)
        elif tlog.sealed:
            raise RuntimeError(f"append after seal on turn {turn_id}")
        frames = tlog.frames
        assert isinstance(frames, _LiveFrames)  # unsealed
        frame = encode_frame(len(frames) + 1, event)
        frames.append(frame)
        tlog.last_append = now
        tlog.signal()
        if self._mirror is not None:
            self._mirror.frame(turn_id, frame)
        return frame.id

    def subscribe(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame]:
        tlog = self._local(turn_id)
        if tlog is None:
            return _empty()
        return self._follow(tlog, max(0, after))

    async def _follow(self, tlog: _TurnLog, after: int) -> AsyncIterator[SseFrame]:
        tlog.subscribers += 1
        clock = self._clock
        try:
            nxt = after
            while True:
                frames = tlog.frames
                while nxt < len(frames):
                    frame = frames[nxt]
                    nxt += 1
                    yield frame
                    tlog.last_delivery = clock.monotonic()
                    if frame.type == TERMINAL_EVENT:
                        return
                if (tlog.sealed or tlog.closed) and nxt >= len(tlog.frames):
                    return
                await tlog.wait()
        finally:
            tlog.subscribers -= 1
            tlog.last_delivery = clock.monotonic()

    def is_local(self, turn_id: str) -> bool:
        return self._local(turn_id) is not None

    def seal(self, turn_id: str, ttl_s: int) -> None:
        tlog = self._local(turn_id)
        if tlog is None or tlog.sealed:
            return
        tlog.sealed = True
        tlog.pack()
        tlog.expires_at = self._clock.monotonic() + ttl_s
        tlog.signal()
        tlog.drop_wake()
        if self._mirror is not None:
            self._mirror.seal(turn_id, ttl_s)

    async def attach_remote(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame] | None:
        if self._mirror is None:
            await anyio.lowlevel.checkpoint()
            return None
        return await self._mirror.follow(turn_id, after=max(0, after))

    async def idle_seconds(self, turn_id: str) -> float | None:
        tlog = self._local(turn_id)
        if tlog is None or tlog.subscribers:
            await anyio.lowlevel.checkpoint()
            return None
        idle = self._clock.monotonic() - tlog.last_delivery
        if self._mirror is not None:
            attached, remote_idle = await self._mirror.remote_delivery(turn_id)
            if attached or tlog.subscribers:
                return None
            if remote_idle is not None:
                idle = min(idle, remote_idle)
        return max(0.0, idle)

    def sweep(self) -> int:
        """Drop expired sealed logs and orphaned unsealed ones; returns how many were dropped."""
        now = self._clock.monotonic()
        dropped = 0
        for turn_id, tlog in list(self._logs.items()):
            expired = tlog.expires_at is not None and now >= tlog.expires_at
            orphan = not tlog.sealed and now - tlog.last_append >= self._orphan_after_s
            if expired or orphan:
                if orphan:
                    log.warning("dropping an unsealed turn event log", extra={"turn_id": turn_id})
                del self._logs[turn_id]
                tlog.close()
                dropped += 1
        return dropped

    async def run(self) -> None:
        """Background task: the mirror writer plus the local sweep every 30 s; runs until cancelled."""
        async with anyio.create_task_group() as tg:
            if self._mirror is not None:
                tg.start_soon(self._mirror.run)
            while True:
                await self._clock.sleep(SWEEP_EVERY_S)
                self.sweep()


async def _empty() -> AsyncIterator[SseFrame]:
    return
    yield  # pragma: no cover
