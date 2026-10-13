"""Time sources and deadlines (spec-02 §1).

Every component reads time through a `Clock`, so tests drive time with a fake clock instead of sleeping. Timeouts
use `deadline_scope(clock, at)` (or `Deadline.scope()`), which is driven by the injected clock, so the same code
times out under a virtual clock in tests. On `SystemClock` (whose time is the event loop's) the scope is a native
anyio deadline: no task and no task group per scope (E3; sprint B F1).
"""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol

import anyio


class Clock(Protocol):
    """Wall and monotonic time plus sleeping."""

    def now(self) -> datetime:
        """Timezone-aware UTC wall time; sync."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds (arbitrary origin); sync."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend for `seconds` of this clock's time. Cancellation-safe."""
        ...


class SystemClock:
    """The real clock: `datetime.now(UTC)`, `time.monotonic()` and `anyio.sleep`."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class Deadline:
    """An absolute point on `clock`'s monotonic timeline (`at`, seconds)."""

    at: float
    clock: Clock

    def remaining_ms(self) -> int:
        """Whole milliseconds left: `max(0, floor((at - now) * 1000))`."""
        return max(0, math.floor((self.at - self.clock.monotonic()) * 1000))

    def expired(self) -> bool:
        """True once the clock has reached `at`."""
        return self.clock.monotonic() >= self.at

    def child(self, cap_ms: int) -> Deadline:
        """A deadline at `min(self.at, now + cap_ms / 1000)` on the same clock."""
        return Deadline(min(self.at, self.clock.monotonic() + cap_ms / 1000), self.clock)

    def earliest(self, other: Deadline | None) -> Deadline:
        """The earlier of `self` and `other` (`self` when `other` is None or on a tie)."""
        if other is None or self.at <= other.at:
            return self
        return other

    def scope(self) -> contextlib.AbstractAsyncContextManager[anyio.CancelScope]:
        """`deadline_scope(self.clock, self.at)`: `async with deadline.scope() as scope:` cancels the body when
        this deadline's clock reaches `at` (see `deadline_scope` for the expiry semantics)."""
        return deadline_scope(self.clock, self.at)


def deadline_scope(clock: Clock, at: float) -> contextlib.AbstractAsyncContextManager[anyio.CancelScope]:
    """A cancel scope around the body that is cancelled when `clock.monotonic()` reaches `at`.

    It fires on any `Clock`, including a virtual one: on `SystemClock` it is an anyio cancel scope with the deadline
    `at` mapped onto the event loop's clock (the time remaining at entry, as a sleep on that clock would count it;
    no task), on every other clock a watcher task awaits `clock.sleep`. An `at` already reached cancels the body at
    its first checkpoint. Expiry semantics are those of `anyio.move_on_after`: the block exits normally and
    `scope.cancelled_caught` is True; the caller raises its own error (fail_after style, e.g. `deadline_exceeded`).
    Exceptions raised by the body propagate unchanged (not wrapped in an exception group). Must be entered inside a
    running event loop.
    """
    if type(clock) is SystemClock:
        return _LoopDeadline(clock, at)
    return _watched_scope(clock, at)


class _LoopDeadline:
    """`deadline_scope` on `SystemClock`: one `anyio.CancelScope` whose deadline the event loop's timer enforces (a
    watcher task per scope was ~26 tasks per turn and 4% of the api's CPU, E sprint B F1)."""

    __slots__ = ("_at", "_clock", "_scope")

    def __init__(self, clock: Clock, at: float) -> None:
        self._clock = clock
        self._at = at
        self._scope: anyio.CancelScope | None = None

    async def __aenter__(self) -> anyio.CancelScope:
        remaining = self._at - self._clock.monotonic()
        scope = self._scope = anyio.CancelScope(deadline=anyio.current_time() + remaining)
        return scope.__enter__()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool:
        assert self._scope is not None
        return self._scope.__exit__(exc_type, exc, tb)


@contextlib.asynccontextmanager
async def _watched_scope(clock: Clock, at: float) -> AsyncIterator[anyio.CancelScope]:
    """`deadline_scope` on any other clock: a watcher task sleeps on `clock` and then cancels the scope."""
    scope = anyio.CancelScope()
    remaining = at - clock.monotonic()
    if remaining <= 0:
        scope.cancel()
        with scope:
            yield scope
        return
    try:
        async with anyio.create_task_group() as tg:

            async def watch() -> None:
                await clock.sleep(remaining)
                scope.cancel()

            tg.start_soon(watch)
            try:
                with scope:
                    yield scope
            finally:
                tg.cancel_scope.cancel()
    except BaseExceptionGroup as group:  # anyio groups the body's exception with the (cancelled) watcher
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise
