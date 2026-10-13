"""Retry policy (IMPL-035): full jitter, base 0.5 s, cap 8 s, `max_retries` 2 by default, honoring `retry-after`."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Final

import anyio

DEFAULT_MAX_RETRIES: Final = 2
BACKOFF_BASE_S: Final = 0.5
BACKOFF_CAP_S: Final = 8.0
MAX_RETRY_AFTER_S: Final = 60
"""A `retry-after` above this is not waited for inside one call: the error is raised instead."""


def _sleep(delay_s: float, stop: threading.Event | None) -> None:
    """Blocks for `delay_s`, or until `stop` is set (a stream aborted during its backoff)."""
    if stop is None:
        time.sleep(delay_s)
    else:
        stop.wait(delay_s)


async def _async_sleep(delay_s: float, stop: anyio.Event | None) -> None:
    """Waits `delay_s`, or until `stop` is set."""
    if stop is None:
        await anyio.sleep(delay_s)
    else:
        with anyio.move_on_after(delay_s):
            await stop.wait()


# Indirections that tests replace for deterministic runs.
_random: Callable[[], float] = random.random
sleep: Callable[[float, threading.Event | None], None] = _sleep
async_sleep: Callable[[float, anyio.Event | None], Awaitable[None]] = _async_sleep


def delay_s(attempt: int, retry_after_s: int | None) -> float | None:
    """Delay before retry `attempt + 1` (attempt 0 = after the first failure): `retry-after` when the server sent it,
    else uniform in [0, min(cap, base * 2**attempt)). None means do not retry."""
    if retry_after_s is not None:
        return None if retry_after_s > MAX_RETRY_AFTER_S else float(retry_after_s)
    return _random() * min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0**attempt)
