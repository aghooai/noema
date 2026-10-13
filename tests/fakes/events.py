"""`FakeTurnEventLog` (+ `FakeEventMirror`) and `FakeSseWriter` (spec-02 §7, IMPL-021, 04:L281), on a `Clock`.

Event log: per-turn frames `id: n` / `event: type` / `data: orjson(event)`, serialized once at `append`; `seal`
keeps the local log `ttl_s` (then it is gone: `is_local` False, `subscribe` empty) and expires the mirror after
`ttl_s`. Two logs sharing one `FakeEventMirror` behave like two pods sharing the Redis mirror (`attach_remote`).
`idle_seconds` is None while any subscriber (local or remote) is attached, else the time since the last frame was
delivered (or since the turn's first append).
"""

from __future__ import annotations

import contextlib
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

import anyio
import anyio.lowlevel
import orjson

from noema.core.clock import Clock, deadline_scope
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue

TERMINAL_EVENT: Final = "turn.completed"


def make_frame(frame_id: int, event: Mapping[str, JsonValue]) -> SseFrame:
    """The pre-serialized frame of one event (spec-02 §7 `SseFrame.wire`)."""
    kind = str(event["type"])
    payload = orjson.dumps(event)
    wire = b"id: %d\nevent: %s\ndata: %s\n\n" % (frame_id, kind.encode(), payload)
    return SseFrame(id=frame_id, type=kind, payload=payload, wire=wire)


@dataclass
class _Log:
    frames: list[SseFrame] = field(default_factory=list)
    sealed: bool = False
    expires_at: float | None = None
    changed: anyio.Event = field(default_factory=anyio.Event)
    subscribers: int = 0
    last_delivery: float = 0.0

    def signal(self) -> None:
        self.changed.set()
        self.changed = anyio.Event()

    @property
    def complete(self) -> bool:
        return bool(self.frames) and self.frames[-1].type == TERMINAL_EVENT


class FakeEventMirror:
    """The shared Redis-mirror stand-in; `down = True` simulates Redis unavailable (no mirror writes or reads)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.logs: dict[str, _Log] = {}
        self.down = False

    def get(self, turn_id: str) -> _Log | None:
        log = self.logs.get(turn_id)
        if log is not None and log.expires_at is not None and self.clock.monotonic() >= log.expires_at:
            del self.logs[turn_id]
            return None
        return log


async def _follow(log: _Log, after: int, clock: Clock) -> AsyncIterator[SseFrame]:
    log.subscribers += 1
    try:
        nxt = after
        while True:
            pending = [f for f in log.frames if f.id > nxt]
            for frame in pending:
                yield frame
                nxt = frame.id
                log.last_delivery = clock.monotonic()
                if frame.type == TERMINAL_EVENT:
                    return
            if log.complete and not pending:
                return
            await log.changed.wait()
    finally:
        log.subscribers -= 1
        log.last_delivery = clock.monotonic()


class FakeTurnEventLog:
    """Implements `TurnEventLog` (see the module docstring)."""

    def __init__(self, clock: Clock, mirror: FakeEventMirror | None = None) -> None:
        self._clock = clock
        self.mirror = mirror if mirror is not None else FakeEventMirror(clock)
        self._logs: dict[str, _Log] = {}

    def _local(self, turn_id: str) -> _Log | None:
        log = self._logs.get(turn_id)
        if log is not None and log.expires_at is not None and self._clock.monotonic() >= log.expires_at:
            del self._logs[turn_id]
            return None
        return log

    def append(self, turn_id: str, event: Mapping[str, JsonValue]) -> int:
        log = self._local(turn_id)
        if log is None:
            log = self._logs[turn_id] = _Log(last_delivery=self._clock.monotonic())
        if log.sealed:
            raise RuntimeError(f"append after seal on turn {turn_id}")
        frame = make_frame(len(log.frames) + 1, event)
        log.frames.append(frame)
        log.signal()
        if not self.mirror.down:
            remote = self.mirror.logs.setdefault(turn_id, _Log(last_delivery=self._clock.monotonic()))
            remote.frames.append(frame)
            remote.signal()
        return frame.id

    def frames(self, turn_id: str) -> list[SseFrame]:
        """Every local frame of a turn (test inspection)."""
        log = self._local(turn_id)
        return [] if log is None else list(log.frames)

    def subscribe(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame]:
        log = self._local(turn_id)
        if log is None:
            return _empty()
        return _follow(log, after, self._clock)

    def is_local(self, turn_id: str) -> bool:
        return self._local(turn_id) is not None

    def seal(self, turn_id: str, ttl_s: int) -> None:
        log = self._local(turn_id)
        if log is None:
            return
        log.sealed = True
        log.expires_at = self._clock.monotonic() + ttl_s
        remote = None if self.mirror.down else self.mirror.get(turn_id)
        if remote is not None:
            remote.expires_at = log.expires_at
        log.signal()

    async def attach_remote(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame] | None:
        await anyio.lowlevel.checkpoint()
        if self.mirror.down:
            return None
        remote = self.mirror.get(turn_id)
        return None if remote is None else _follow(remote, after, self._clock)

    async def idle_seconds(self, turn_id: str) -> float | None:
        await anyio.lowlevel.checkpoint()
        log = self._local(turn_id)
        if log is None:
            return None
        remote = None if self.mirror.down else self.mirror.get(turn_id)
        if log.subscribers or (remote is not None and remote.subscribers):
            return None
        last = max(log.last_delivery, remote.last_delivery if remote is not None else 0.0)
        return self._clock.monotonic() - last


async def _empty() -> AsyncIterator[SseFrame]:
    return
    yield  # pragma: no cover


TOO_SLOW_FRAME_TIMEOUT_S: Final = 1.0
PING: Final = b": ping\n\n"


def merge_deltas(a: SseFrame, b: SseFrame) -> SseFrame:
    """Two consecutive generation.delta frames of one attempt as one: text concatenated, b's id and seq."""
    pa, pb = orjson.loads(a.payload), orjson.loads(b.payload)
    return make_frame(b.id, {**pb, "text": pa["text"] + pb["text"]})


def _mergeable(a: SseFrame, b: SseFrame) -> bool:
    if a.type != "generation.delta" or b.type != "generation.delta":
        return False
    return bool(orjson.loads(a.payload)["attempt_id"] == orjson.loads(b.payload)["attempt_id"])


def too_slow_frame() -> bytes:
    """The best-effort error{client_too_slow} frame (no id line; not in the event log)."""
    event = NoemaError(ErrorCode.CLIENT_TOO_SLOW, "The client is reading the stream too slowly.").sse_event()
    return b"event: error\ndata: %s\n\n" % orjson.dumps(event)


class FakeSseWriter:
    """Implements `SseWriter` (backpressure of spec-02 §7): a bounded queue; at the high-water mark consecutive
    deltas of one attempt are merged; still full for `too_slow_s` -> the client_too_slow frame (1 s, best effort)
    and "client_too_slow"; `: ping` after `ping_s` without a write; `send` raising -> "disconnected"."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.merges = 0
        self.pings = 0

    async def run(
        self,
        frames: AsyncIterator[SseFrame],
        send: Callable[[bytes], Awaitable[None]],
        *,
        queue_events: int = 256,
        queue_bytes: int = 1 << 20,
        too_slow_s: float = 30,
        ping_s: float = 15,
    ) -> Literal["completed", "disconnected", "client_too_slow"]:
        state = _WriterState(self._clock)
        result: list[Literal["completed", "disconnected", "client_too_slow"]] = []

        def full() -> bool:
            return len(state.queue) >= queue_events or state.nbytes >= queue_bytes

        async def reader() -> None:
            async for frame in frames:
                while full():
                    if self._merge(state):
                        continue
                    started = state.full_since = state.full_since or self._clock.monotonic()
                    if self._clock.monotonic() - started >= too_slow_s:
                        result.append("client_too_slow")
                        tg.cancel_scope.cancel()
                        return
                    await state.wait(started + too_slow_s - self._clock.monotonic())
                state.full_since = None
                state.push(frame)
            state.eof = True
            state.signal()

        async def writer() -> None:
            last_write = self._clock.monotonic()
            while True:
                if state.queue:
                    frame = state.pop()
                    try:
                        await send(frame.wire)
                    except Exception:
                        result.append("disconnected")
                        tg.cancel_scope.cancel()
                        return
                    last_write = self._clock.monotonic()
                    continue
                if state.eof:
                    result.append("completed")
                    tg.cancel_scope.cancel()
                    return
                if not await state.wait(last_write + ping_s - self._clock.monotonic()) and not state.queue:
                    try:
                        await send(PING)
                    except Exception:
                        result.append("disconnected")
                        tg.cancel_scope.cancel()
                        return
                    self.pings += 1
                    last_write = self._clock.monotonic()

        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(writer)
        outcome = result[0]
        if outcome == "client_too_slow":
            async with deadline_scope(self._clock, self._clock.monotonic() + TOO_SLOW_FRAME_TIMEOUT_S):
                with contextlib.suppress(Exception):  # best effort
                    await send(too_slow_frame())
        return outcome

    def _merge(self, state: _WriterState) -> bool:
        q = state.queue
        for i in range(len(q) - 1, 0, -1):
            if _mergeable(q[i - 1], q[i]):
                merged = merge_deltas(q[i - 1], q[i])
                state.nbytes += len(merged.wire) - len(q[i - 1].wire) - len(q[i].wire)
                q[i - 1] = merged
                del q[i]
                self.merges += 1
                return True
        return False


class _WriterState:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.queue: deque[SseFrame] = deque()
        self.nbytes = 0
        self.eof = False
        self.full_since: float | None = None
        self._changed = anyio.Event()

    def signal(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()

    def push(self, frame: SseFrame) -> None:
        self.queue.append(frame)
        self.nbytes += len(frame.wire)
        self.signal()

    def pop(self) -> SseFrame:
        frame = self.queue.popleft()
        self.nbytes -= len(frame.wire)
        self.signal()
        return frame

    async def wait(self, timeout_s: float) -> bool:
        """True if signalled, False when `timeout_s` passed on the clock first."""
        changed = self._changed
        async with deadline_scope(self.clock, self.clock.monotonic() + max(0.0, timeout_s)) as scope:
            await changed.wait()
        return not scope.cancelled_caught
