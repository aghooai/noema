"""`BackpressureSseWriter` (spec-02 §7 `SseWriter`, 04:L281): one client's SSE stream with a bounded queue.

Three tasks share one queue: the reader pulls frames from the event log; the writer sends them (`send()` raising
-> "disconnected"); the timer sends `: ping` after `ping_s` without a write and ends the stream with
"client_too_slow" once the queue has stayed full, or one `send()` has stayed blocked, for `too_slow_s` (a client
that stops reading but stays connected rarely fills the queue, because coalescing collapses the deltas). At the
high-water mark (`queue_events` frames or `queue_bytes` wire bytes) the reader first coalesces every run of
consecutive generation.delta frames of one attempt into one frame (texts concatenated; the last frame's id and seq),
so a slow client sees fewer, larger frames (and id/seq gaps); only if the queue is still full does it wait. After
"client_too_slow" one best-effort `error{client_too_slow}` frame (no id line, 1 s timeout) is sent; the caller then
cancels the turn (client_abort).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final, Literal

import anyio
import anyio.abc

from noema.core.clock import Clock, deadline_scope
from noema.core.protocols.events import SseFrame

from .frames import DELTA_EVENT, PING, delta_attempt, merge_deltas, too_slow_frame

log = logging.getLogger("noema.streaming")
TOO_SLOW_FRAME_TIMEOUT_S: Final = 1.0
CLOSE_TIMEOUT_S: Final = 1.0
Outcome = Literal["completed", "disconnected", "client_too_slow"]


class _Signal:
    """A resettable wake-up of one waiter: one `asyncio.Event` per stream side, cleared before each wait (the
    writer wakes once per frame; a new `anyio.Event` per wait cost sniffio's backend lookup and two objects each
    time, ~1.3% of the api's CPU at 50 streams, E sprint B). Every Noema process and test runs on asyncio."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event: asyncio.Event | None = None  # created by the first wait (inside the loop)

    def set(self) -> None:
        if self._event is not None:
            self._event.set()

    async def wait(self) -> None:
        event = self._event
        if event is None:
            event = self._event = asyncio.Event()
        event.clear()  # wait for the next set(): the callers re-check their condition before waiting
        await event.wait()


class _Stream:
    """The state of one `run()`."""

    __slots__ = (
        "data",
        "eof",
        "full_since",
        "last_write",
        "max_bytes",
        "max_events",
        "nbytes",
        "outcome",
        "ping_due",
        "queue",
        "send_since",
        "space",
    )

    def __init__(self, now: float, max_events: int, max_bytes: int) -> None:
        self.queue: deque[SseFrame] = deque()
        self.nbytes = 0
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.eof = False
        self.send_since: float | None = None  # when the send in progress started (None: no send in progress)
        self.ping_due = False
        self.last_write = now
        self.full_since: float | None = None
        self.outcome: Outcome | None = None
        self.data = _Signal()  # the writer waits for frames (or a ping request)
        self.space = _Signal()  # the reader waits for room

    def full(self) -> bool:
        return len(self.queue) >= self.max_events or self.nbytes >= self.max_bytes

    def fits(self, frame: SseFrame) -> bool:
        """`frame` can be queued without passing either bound (QL-3: the bytes are checked with the frame, so the
        queue never holds more than `max_bytes`); a frame alone in the queue always fits (no deadlock on a frame
        larger than the byte bound)."""
        return not self.queue or (len(self.queue) < self.max_events and self.nbytes + len(frame.wire) <= self.max_bytes)

    def push(self, frame: SseFrame) -> None:
        self.queue.append(frame)
        self.nbytes += len(frame.wire)
        self.data.set()

    def pop(self) -> SseFrame:
        frame = self.queue.popleft()
        self.nbytes -= len(frame.wire)
        if self.full_since is not None and not self.full():
            self.full_since = None
        self.space.set()
        return frame

    def coalesce(self) -> int:
        """Merge every run of consecutive generation.delta frames of one attempt; returns the frames saved."""
        before = len(self.queue)
        out: deque[SseFrame] = deque()
        run: list[SseFrame] = []
        run_attempt: str | None = None
        for frame in self.queue:
            attempt = delta_attempt(frame) if frame.type == DELTA_EVENT else None
            if attempt is not None and attempt == run_attempt:
                run.append(frame)
                continue
            if run:
                out.append(merge_deltas(run))
            run, run_attempt = ([frame], attempt) if attempt is not None else ([], None)
            if attempt is None:
                out.append(frame)
        if run:
            out.append(merge_deltas(run))
        if len(out) < before:
            self.queue = out
            self.nbytes = sum(len(f.wire) for f in out)
        return before - len(out)


class BackpressureSseWriter:
    """Implements `SseWriter` (see the module docstring). One instance serves every stream of the process;
    `merged_frames`, `pings` and `too_slow` are process-wide diagnostic counters."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.merged_frames = 0
        self.pings = 0
        self.too_slow = 0

    async def run(
        self,
        frames: AsyncIterator[SseFrame],
        send: Callable[[bytes], Awaitable[None]],
        *,
        queue_events: int = 256,
        queue_bytes: int = 1 << 20,
        too_slow_s: float = 30,
        ping_s: float = 15,
    ) -> Outcome:
        if queue_events < 1 or queue_bytes < 1 or too_slow_s <= 0 or ping_s <= 0:
            raise ValueError("queue bounds and timeouts must be positive")
        st = _Stream(self._clock.monotonic(), queue_events, queue_bytes)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._reader, frames, st, tg)
                tg.start_soon(self._writer, send, st, tg)
                tg.start_soon(self._timer, st, tg, too_slow_s, ping_s)
        except BaseExceptionGroup as group:  # a failing frame source: re-raise its exception unwrapped
            if len(group.exceptions) == 1:
                raise group.exceptions[0] from None
            raise
        finally:
            await _close(frames)
        outcome = st.outcome or "completed"
        if outcome == "client_too_slow":
            self.too_slow += 1
            log.info("sse client too slow; stream closed")
            async with deadline_scope(self._clock, self._clock.monotonic() + TOO_SLOW_FRAME_TIMEOUT_S):
                with contextlib.suppress(Exception):  # best effort
                    await send(too_slow_frame())
        return outcome

    async def _reader(self, frames: AsyncIterator[SseFrame], st: _Stream, tg: anyio.abc.TaskGroup) -> None:
        async for frame in frames:
            if not st.fits(frame):
                self.merged_frames += st.coalesce()
            while not st.fits(frame):
                if st.full_since is None:
                    st.full_since = self._clock.monotonic()
                await st.space.wait()
            st.push(frame)
        st.eof = True
        st.data.set()

    async def _writer(self, send: Callable[[bytes], Awaitable[None]], st: _Stream, tg: anyio.abc.TaskGroup) -> None:
        while True:
            if st.queue:
                chunk = st.pop().wire
            elif st.ping_due:
                chunk = PING
                self.pings += 1
            elif st.eof:
                _finish(st, tg, "completed")
                return
            else:
                await st.data.wait()
                continue
            st.ping_due = False
            st.send_since = self._clock.monotonic()
            try:
                await send(chunk)
            except Exception:  # any transport failure: the client is gone
                _finish(st, tg, "disconnected")
                return
            st.send_since = None
            st.last_write = self._clock.monotonic()

    async def _timer(self, st: _Stream, tg: anyio.abc.TaskGroup, too_slow_s: float, ping_s: float) -> None:
        due_ping = st.last_write + ping_s
        while True:
            now = self._clock.monotonic()
            full = math.inf if st.full_since is None else st.full_since
            blocked = math.inf if st.send_since is None else st.send_since
            due_slow = min(full, blocked) + too_slow_s
            if now >= due_slow:
                _finish(st, tg, "client_too_slow")
                return
            due_ping = max(due_ping, st.last_write + ping_s)
            if now >= due_ping:
                if not st.queue and st.send_since is None:
                    st.ping_due = True
                    st.data.set()
                due_ping = now + ping_s
            await self._clock.sleep(min(due_ping, due_slow) - now)


def _finish(st: _Stream, tg: anyio.abc.TaskGroup, outcome: Outcome) -> None:
    if st.outcome is None:
        st.outcome = outcome
    tg.cancel_scope.cancel()


async def _close(frames: AsyncIterator[SseFrame]) -> None:
    """Close an async-generator source so its cleanup (subscriber counts, attach heartbeats) runs now."""
    aclose = getattr(frames, "aclose", None)
    if aclose is None:
        return
    with anyio.CancelScope(shield=True), anyio.move_on_after(CLOSE_TIMEOUT_S):
        await aclose()
