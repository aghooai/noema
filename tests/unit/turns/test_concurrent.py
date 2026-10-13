"""`noema.turns.concurrent.gather`: a plain task-group fan-out (orchestrator request 11: the FakeClock no longer
jumps virtual time while a task waits for its task group's completion callback)."""

from __future__ import annotations

import anyio
import pytest
from tests.fakes.clock import FakeClock

from noema.turns.concurrent import gather

pytestmark = pytest.mark.unit


async def test_a_fan_out_under_autojump_makes_no_phantom_time() -> None:
    clock = FakeClock()
    seen: list[float] = []

    async def checkpoint() -> None:
        await anyio.lowlevel.checkpoint()

    async def fan_out() -> None:
        start = clock.monotonic()
        await gather([checkpoint, checkpoint])
        seen.append(clock.monotonic() - start)

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(clock.sleep, 0.9)  # an unrelated sleeper the driver could jump to
        tg.start_soon(fan_out)
    assert seen == [0.0]


async def test_the_first_failure_cancels_the_rest_and_propagates() -> None:
    finished: list[str] = []

    async def fails() -> None:
        raise ValueError("boom")

    async def slow() -> None:
        await anyio.sleep(10)
        finished.append("slow")

    with pytest.raises(ExceptionGroup) as info:
        await gather([fails, slow])
    assert info.group_contains(ValueError) and finished == []


async def test_a_fan_out_runs_at_most_gather_limit_calls_at_once() -> None:
    """A turn's fan-out (derivative reads, file lookups, negotiation) is bounded: one session owner cannot open
    hundreds of concurrent DB units or S3 reads per turn (Wave E fix)."""
    from noema.turns import concurrent

    live, peak, done = 0, 0, 0

    async def call() -> None:
        nonlocal live, peak, done
        live += 1
        peak = max(peak, live)
        await anyio.lowlevel.checkpoint()
        await anyio.lowlevel.checkpoint()
        live -= 1
        done += 1

    await gather([call] * 50)
    assert done == 50 and 1 < peak <= concurrent.GATHER_LIMIT
