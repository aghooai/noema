"""Time for the wire mock. Production runs on `SystemClock` (CLOCK_MONOTONIC, shared by every process on the host,
spec-10 §5); unit tests use `VirtualClock`, whose sleeps jump time forward instead of waiting, so a stream's
schedule can be asserted exactly and nothing sleeps for logic."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class MockClock(Protocol):
    """A monotonic nanosecond clock with an absolute-deadline sleep."""

    def now_ns(self) -> int: ...

    async def sleep_until_ns(self, t_ns: int) -> None: ...


class SystemClock:
    """`time.monotonic_ns` and `asyncio.sleep`."""

    def now_ns(self) -> int:
        return time.monotonic_ns()

    async def sleep_until_ns(self, t_ns: int) -> None:
        delay = (t_ns - time.monotonic_ns()) / 1e9
        if delay > 0:
            await asyncio.sleep(delay)


class VirtualClock:
    """A test clock: `sleep_until_ns` moves time to the deadline (never backwards) and yields once."""

    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self.t_ns = start_ns

    def now_ns(self) -> int:
        return self.t_ns

    async def sleep_until_ns(self, t_ns: int) -> None:
        self.t_ns = max(self.t_ns, t_ns)
        await asyncio.sleep(0)

    def advance_ms(self, ms: float) -> None:
        """Move time forward (e.g. into a fault's `window_ms`)."""
        self.t_ns += round(ms * 1e6)
