"""Event-loop lag sampler shared by the load harness processes (the api launcher, the mock launcher, the drivers).

A task sleeps `interval_s` in a loop and records how late it woke up: `lag = woke - (slept_at + interval)`, in ns,
with the CLOCK_MONOTONIC timestamp of the sleep (spec-10 §5: every process on the host shares this clock, so the
analysis cuts the samples by phase window). Samples are appended to a binary file of int64 pairs (t_ns, lag_ns)
once a second, so a killed process loses at most one second. uvloop timers have 1 ms granularity, so a sample
carries up to ~1 ms of timer rounding on top of the real lag; an idle loop therefore reads 0-1 ms.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from array import array
from pathlib import Path
from typing import Final

DEFAULT_INTERVAL_S: Final = 0.01
FLUSH_EVERY: Final = 100  # samples (= 1 s at the default interval)


class LagSampler:
    """`start()` spawns the sampling task on the running loop; `stop()` cancels it and flushes."""

    def __init__(self, path: Path, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        self.path = path
        self.interval_s = interval_s
        self._pending = array("q")
        self._task: asyncio.Task[None] | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="load.lag_sampler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.flush()

    def flush(self) -> None:
        if self._pending:
            with self.path.open("ab") as out:
                self._pending.tofile(out)
            self._pending = array("q")

    async def _run(self) -> None:
        interval_ns = int(self.interval_s * 1e9)
        clock = time.monotonic_ns
        while True:
            slept_at = clock()
            await asyncio.sleep(self.interval_s)
            self._pending.append(slept_at)
            self._pending.append(clock() - slept_at - interval_ns)
            if len(self._pending) >= 2 * FLUSH_EVERY:
                self.flush()


def read_lag(path: Path) -> list[tuple[int, int]]:
    """The (t_ns, lag_ns) samples of a lag file (a truncated trailing pair is ignored)."""
    data = array("q")
    raw = path.read_bytes()
    data.frombytes(raw[: len(raw) // 16 * 16])
    return [(data[i], data[i + 1]) for i in range(0, len(data), 2)]
