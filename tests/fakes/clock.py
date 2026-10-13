"""`FakeClock` (virtual time) and `FakeIdGen` (spec-02 §1, spec-10 §1).

`FakeClock` implements `noema.core.clock.Clock`. Time moves only when a test moves it:
- `await clock.advance(s)` / `advance_to(t)` wake every sleeper due by then, in deadline order, letting the event
  loop settle (every other task blocked) after each wake-up, so chains of sleeps run exactly as in real time;
- `async with clock.autojump():` jumps to the next sleeper whenever every task is blocked (trio's autojump clock),
  so a scenario runs to completion in zero wall time.

"Blocked" (`_runnable` is False and no worker thread is busy): every other task waits on a pending future, the
loop's ready queue holds nothing but the settling tasks' own steps (no task wakeup, no future done-callback such as
an anyio task group's completion, no anyio cancel-delivery retry), no real-time timer is due, and no
`anyio.to_thread` call is in flight on the default limiter (FastAPI's threadpool included). So time never moves
while a task group is collecting a finished child, while a cancellation is being delivered, or while a thread
works. Not visible: real socket I/O still on the wire and asyncio's own executor; suites with real I/O mark it
themselves (e.g. the ops-ledger harness's busy counter).

Settling uses asyncio internals (`Task._fut_waiter`, `loop._ready`, `loop._scheduled`; the test backend is asyncio,
root conftest). Timeouts go through `noema.core.clock.deadline_scope(clock, at)` / `Deadline.scope()`, which are
driven by the injected `Clock` and so fire when this clock is moved past `at`.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import anyio
import anyio.lowlevel
import anyio.to_thread

from noema.core.clock import Clock
from noema.core.ids import UlidIdGen

DEFAULT_WALL: Final = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
DEFAULT_MONOTONIC: Final = 1000.0
_SETTLE_LIMIT: Final = 100_000
_THREAD_POLL_S: Final = 0.001


@contextmanager
def single_errors() -> Iterator[None]:
    """Re-raise a lone exception out of an anyio exception group, so a task-group-based context manager is
    transparent to the exceptions its body raises (several exceptions stay grouped)."""
    try:
        yield
    except BaseExceptionGroup as group:
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise


@dataclass(order=True)
class _Sleeper:
    at: float
    seq: int
    event: anyio.Event = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


def _runnable(settling: set[asyncio.Task[object]]) -> bool:
    """True while something can run without the clock moving (see the module docstring). Tasks waiting in
    `settle()` count as blocked, and their own queued steps are ignored."""
    loop = asyncio.get_running_loop()
    ignore = {id(t) for t in settling} | {id(asyncio.current_task())}
    for task in asyncio.all_tasks(loop):
        if id(task) in ignore:
            continue
        waiter = task._fut_waiter  # type: ignore[attr-defined]
        if waiter is None or waiter.done():
            return True
    ready: Iterable[asyncio.Handle] = loop._ready  # type: ignore[attr-defined]
    for handle in ready:  # a wakeup, a done callback or a retry queued for the next loop iteration
        if not handle.cancelled() and id(getattr(handle._callback, "__self__", None)) not in ignore:  # type: ignore[attr-defined]
            return True
    now = loop.time()
    scheduled: Iterable[asyncio.TimerHandle] = loop._scheduled  # type: ignore[attr-defined]
    return any(not t.cancelled() and t.when() <= now for t in scheduled)


def _threads_busy() -> bool:
    """An `anyio.to_thread` call (default limiter) is in flight: its result arrives without the clock moving."""
    return anyio.to_thread.current_default_thread_limiter().borrowed_tokens > 0


class FakeClock:
    """A virtual `Clock`. `now()` = `wall` + (monotonic - start); `sleep(s)` waits until the clock is moved past
    `monotonic() + s`. Moving time backwards is impossible."""

    def __init__(self, *, wall: datetime = DEFAULT_WALL, monotonic: float = DEFAULT_MONOTONIC) -> None:
        if wall.tzinfo is None:
            raise ValueError("wall must be timezone-aware")
        self._wall0 = wall.astimezone(UTC)
        self._mono0 = monotonic
        self._now = monotonic
        self._sleepers: list[_Sleeper] = []
        self._seq = itertools.count()
        self._driver_wake: anyio.Event | None = None
        self._autojump = False
        self._settling: set[asyncio.Task[object]] = set()

    # --- Clock -------------------------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return self._wall0 + timedelta(seconds=self._now - self._mono0)

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await anyio.lowlevel.checkpoint()
            return
        sleeper = _Sleeper(self._now + seconds, next(self._seq), anyio.Event())
        heapq.heappush(self._sleepers, sleeper)
        if self._driver_wake is not None:
            self._driver_wake.set()
        try:
            await sleeper.event.wait()
        finally:
            if not sleeper.event.is_set():
                sleeper.cancelled = True  # removed lazily

    # --- test control ----------------------------------------------------------------------------------------------
    def elapsed_ms(self) -> float:
        """Milliseconds since construction (virtual)."""
        return (self._now - self._mono0) * 1000

    def next_deadline(self) -> float | None:
        """The monotonic time of the earliest pending sleeper, if any."""
        self._drop_cancelled()
        return self._sleepers[0].at if self._sleepers else None

    def pending(self) -> int:
        """Number of tasks sleeping on this clock."""
        return sum(1 for s in self._sleepers if not s.cancelled)

    async def settle(self) -> None:
        """Yield until every other task is blocked (RuntimeError if a task never blocks, e.g. a busy loop). Tasks
        that are themselves waiting in `settle()` count as blocked."""
        await self._settle(defer=False)

    async def _settle(self, *, defer: bool) -> None:
        """`defer=True` (the time movers) also waits until no other task is settling, so work that settles before
        freezing a result (e.g. a fake engine's snapshot) always runs before time moves on."""
        me = asyncio.current_task()
        assert me is not None
        self._settling.add(me)
        try:
            spins = 0
            while spins < _SETTLE_LIMIT:
                await asyncio.sleep(0)
                if _runnable(self._settling):
                    spins += 1
                elif _threads_busy():
                    await asyncio.sleep(_THREAD_POLL_S)  # real time: the worker reports through the loop
                elif not (defer and len(self._settling) > 1):
                    return
                else:
                    spins += 1
        finally:
            self._settling.discard(me)
        raise RuntimeError("FakeClock.settle: tasks never blocked (busy loop?)")

    async def advance(self, seconds: float) -> None:
        """Move time forward by `seconds`, waking sleepers in order (see the module docstring)."""
        if seconds < 0:
            raise ValueError("time only moves forward")
        await self.advance_to(self._now + seconds)

    async def advance_to(self, target: float) -> None:
        """Move time to the monotonic `target` (no-op if it is in the past)."""
        if self._autojump:
            raise RuntimeError("advance() while autojump() is active")
        await self._settle(defer=True)
        while (due := self.next_deadline()) is not None and due <= target:
            self._wake_due(due)
            await self._settle(defer=True)
        self._now = max(self._now, target)
        await self._settle(defer=True)

    async def run_until_idle(self, *, limit_s: float = 86_400.0) -> None:
        """Advance through every pending sleeper until none is left (bounded by `limit_s` of virtual time)."""
        end = self._now + limit_s
        await self._settle(defer=True)
        while (due := self.next_deadline()) is not None and due <= end:
            self._wake_due(due)
            await self._settle(defer=True)

    @asynccontextmanager
    async def autojump(self) -> AsyncIterator[FakeClock]:
        """Jump to the next sleeper whenever every task is blocked, for the duration of the block."""
        if self._autojump:
            raise RuntimeError("autojump() is already active")
        self._autojump = True
        try:
            with single_errors():
                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._drive)
                    try:
                        yield self
                    finally:
                        tg.cancel_scope.cancel()
        finally:
            self._autojump = False
            self._driver_wake = None

    async def _drive(self) -> None:
        while True:
            await self._settle(defer=True)
            due = self.next_deadline()
            if due is None:
                self._driver_wake = anyio.Event()
                await self._driver_wake.wait()
                self._driver_wake = None
                continue
            self._wake_due(due)

    def _drop_cancelled(self) -> None:
        while self._sleepers and self._sleepers[0].cancelled:
            heapq.heappop(self._sleepers)

    def _wake_due(self, due: float) -> None:
        self._now = max(self._now, due)
        while self._sleepers and self._sleepers[0].at <= self._now:
            sleeper = heapq.heappop(self._sleepers)
            if not sleeper.cancelled:
                sleeper.event.set()


class FakeIdGen(UlidIdGen):
    """A deterministic `IdGen`: ULID time from `clock.now()`, the random part from a counter (so ids are
    reproducible across runs and still unique and monotonic)."""

    def __init__(self, clock: Clock, *, start: int = 1) -> None:
        counter = itertools.count(start)
        super().__init__(clock, randbytes=lambda n: next(counter).to_bytes(n, "big"))
