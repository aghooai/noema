"""Clock and Deadline conformance (spec-02 §1, §14 `test_clock_deadline.py`): SystemClock and FakeClock."""

from __future__ import annotations

from datetime import UTC

import anyio
import pytest

from noema.core.clock import Deadline

from .kits import ClockKit
from .registry import kit_fixture

kit = kit_fixture("clock")


async def test_now_is_utc_aware(kit: ClockKit) -> None:
    now = kit.clock.now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(now)


async def test_monotonic_never_decreases(kit: ClockKit) -> None:
    a = kit.clock.monotonic()
    b = kit.clock.monotonic()
    assert b >= a


async def test_sleep_advances_monotonic_and_wall(kit: ClockKit) -> None:
    m0, w0 = kit.clock.monotonic(), kit.clock.now()
    if kit.advance is None:
        await kit.clock.sleep(0.002)
    else:
        async with anyio.create_task_group() as tg:
            tg.start_soon(kit.clock.sleep, 0.5)
            await kit.advance(0.5)
    assert kit.clock.monotonic() - m0 >= (0.002 if kit.advance is None else 0.5)
    assert kit.clock.now() >= w0


async def test_sleep_zero_or_negative_returns(kit: ClockKit) -> None:
    await kit.clock.sleep(0)
    await kit.clock.sleep(-1)


async def test_sleep_is_cancellation_safe(kit: ClockKit) -> None:
    with anyio.CancelScope() as scope:
        scope.cancel()
        await kit.clock.sleep(10)
    assert scope.cancelled_caught


async def test_deadline_arithmetic(kit: ClockKit) -> None:
    now = kit.clock.monotonic()
    d = Deadline(now + 2.0, kit.clock)
    assert 1990 <= d.remaining_ms() <= 2000
    assert not d.expired()
    child = d.child(500)
    assert child.at <= now + 0.5 + 0.01 and child.clock is kit.clock
    assert d.child(10_000).at == d.at
    assert d.earliest(None) is d and d.earliest(child) is child and child.earliest(d) is child
    past = Deadline(now - 1.0, kit.clock)
    assert past.expired() and past.remaining_ms() == 0


async def test_deadline_expires_on_this_clock(kit: ClockKit) -> None:
    if kit.advance is None:
        pytest.skip("real clock: covered by the virtual-time implementations")
    d = Deadline(kit.clock.monotonic() + 1.0, kit.clock)
    await kit.advance(0.999)
    assert not d.expired()
    await kit.advance(0.001)
    assert d.expired() and d.remaining_ms() == 0


async def test_deadline_scope_is_a_cancel_scope(kit: ClockKit) -> None:
    expired = Deadline(kit.clock.monotonic() - 1, kit.clock)
    async with expired.scope() as scope:
        await anyio.sleep(1)
    assert scope.cancelled_caught


async def test_deadline_scope_fires_when_this_clock_passes_at(kit: ClockKit) -> None:
    """A scope entered before `at` cancels its body once the clock (virtual or real) reaches `at`, and not before."""
    start = kit.clock.monotonic()
    span = 1.0 if kit.advance is not None else 0.05
    deadline = Deadline(start + span, kit.clock)
    reached: list[str] = []

    async def body() -> anyio.CancelScope:
        async with deadline.scope() as scope:
            await kit.clock.sleep(span * 20)
            reached.append("slept through")
        return scope

    if kit.advance is None:
        scope = await body()
    else:
        result: list[anyio.CancelScope] = []

        async def run() -> None:
            result.append(await body())

        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await kit.advance(span * 0.5)
            assert not result  # not yet expired
            await kit.advance(span)
        scope = result[0]
    assert scope.cancelled_caught and reached == []
    assert deadline.expired() and kit.clock.monotonic() - start < span * 20
