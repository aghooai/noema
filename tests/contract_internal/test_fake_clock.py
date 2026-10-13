"""`FakeClock` settling regressions (C2a): virtual time moves only when every task is blocked on the clock.

The first cases are minimal repros of reports against the C1 clock, which jumped while work was still queued (each
fails on it): decision.md 3 / orchestrator.md 11 (a task group collecting its finished child), generation.md 5 (a
cancellation being delivered), orchestrator.md 3 (a thread call, e.g. FastAPI's threadpool), and `advance()` with
the same race. The others pin the rule: a due real-time timer is work, a far one is not, and chains of clock sleeps
still jump in zero wall time.
"""

from __future__ import annotations

import asyncio
import time

import anyio
import anyio.lowlevel
import anyio.to_thread
import pytest
from tests.fakes.clock import FakeClock

from noema.core.clock import deadline_scope


async def test_a_task_group_exit_inside_a_deadline_scope_takes_no_time() -> None:
    clock = FakeClock()
    async with clock.autojump():
        t0 = clock.monotonic()
        async with deadline_scope(clock, t0 + 120) as scope, anyio.create_task_group() as tg:
            tg.start_soon(anyio.lowlevel.checkpoint)
        assert clock.monotonic() - t0 == 0
        assert not scope.cancelled_caught


async def test_a_fan_out_beside_an_unrelated_sleeper_makes_no_phantom_time() -> None:
    clock = FakeClock()
    async with clock.autojump(), anyio.create_task_group() as outer:
        outer.start_soon(clock.sleep, 0.9)
        t0 = clock.monotonic()
        for _ in range(20):
            async with anyio.create_task_group() as tg:
                for _ in range(3):
                    tg.start_soon(anyio.lowlevel.checkpoint)
        assert clock.monotonic() - t0 == 0
        await clock.sleep(0.5)
        assert clock.monotonic() - t0 == 0.5
        outer.cancel_scope.cancel()


async def test_a_cancellation_lands_before_an_unrelated_sleeper_wakes() -> None:
    # generation.md 5: a writer's timer cancels its task group at 30 s, then a best-effort 1 s step runs
    clock = FakeClock()
    async with clock.autojump(), anyio.create_task_group() as outer:
        outer.start_soon(clock.sleep, 7200)
        t0 = clock.monotonic()
        async with anyio.create_task_group() as tg:

            async def timer() -> None:
                await clock.sleep(30)
                tg.cancel_scope.cancel()

            tg.start_soon(timer)
            tg.start_soon(clock.sleep, 3600)  # a send that never completes
        cut_at = clock.monotonic() - t0
        async with deadline_scope(clock, clock.monotonic() + 1) as best_effort:
            await clock.sleep(3600)
        done_at = clock.monotonic() - t0
        outer.cancel_scope.cancel()
    assert (cut_at, done_at, best_effort.cancelled_caught) == (30, 31, True)


async def test_a_thread_call_holds_virtual_time() -> None:
    clock = FakeClock()
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(clock.sleep, 10)
        t0 = clock.monotonic()
        await anyio.to_thread.run_sync(time.sleep, 0.02)
        assert clock.monotonic() - t0 == 0
        tg.cancel_scope.cancel()


async def test_a_due_real_timer_runs_before_time_moves() -> None:
    clock = FakeClock()
    loop = asyncio.get_running_loop()
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(clock.sleep, 10)
        t0 = clock.monotonic()
        fut: asyncio.Future[None] = loop.create_future()
        loop.call_later(0, fut.set_result, None)
        await fut
        assert clock.monotonic() - t0 == 0
        tg.cancel_scope.cancel()


async def test_advance_also_waits_for_queued_work() -> None:
    clock = FakeClock()
    seen: list[float] = []

    async def fan_out_then_sleep() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(anyio.lowlevel.checkpoint)
        seen.append(clock.monotonic())
        await clock.sleep(1)
        seen.append(clock.monotonic())

    async with anyio.create_task_group() as tg:
        t0 = clock.monotonic()
        tg.start_soon(fan_out_then_sleep)
        await clock.advance(5)
    assert [s - t0 for s in seen] == [0, 1]


async def test_chains_of_clock_sleeps_still_jump_at_once() -> None:
    clock = FakeClock()
    loop = asyncio.get_running_loop()
    far = loop.call_later(3600, lambda: None)  # a real timer that is not due does not hold virtual time
    started = time.monotonic()
    try:
        async with clock.autojump():
            t0 = clock.monotonic()
            async with anyio.create_task_group() as tg:
                for i in range(1, 6):
                    tg.start_soon(clock.sleep, 60.0 * i)
            assert clock.monotonic() - t0 == 300
            async with deadline_scope(clock, clock.monotonic() + 2) as scope:
                await clock.sleep(10)
            assert scope.cancelled_caught and clock.monotonic() - t0 == 302
    finally:
        far.cancel()
    assert time.monotonic() - started < 5


async def test_a_busy_loop_still_fails_settle() -> None:
    clock = FakeClock()
    stop = False

    async def spin() -> None:
        while not stop:
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(spin)
        with pytest.raises(RuntimeError, match="busy loop"):
            await clock.settle()
        stop = True
