"""`LocalFileEvents` (also the local half of `RedisFileEvents`) keeps state only for files with a subscriber (C1
review: unbounded memory in long-running processes)."""

from __future__ import annotations

import anyio
import pytest

from noema.media.pipeline import LocalFileEvents

pytestmark = pytest.mark.unit


async def test_publishing_without_subscribers_keeps_nothing() -> None:
    events = LocalFileEvents()
    for n in range(1000):
        await events.publish("t_acme", f"file_{n}", "ready")
    assert events.tracked == 0


async def test_a_subscriber_is_woken_and_its_state_goes_with_it() -> None:
    events = LocalFileEvents()
    woken = anyio.Event()
    async with events.subscribe("t_acme", "file_1") as waiter:

        async def wait() -> None:
            await waiter.wait()
            woken.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(wait)
            await anyio.wait_all_tasks_blocked()
            await events.publish("t_acme", "file_1", "processing")
            with anyio.fail_after(1):
                await woken.wait()
        assert events.tracked == 1
    assert events.tracked == 0


async def test_cancelled_and_timed_out_waiters_leave_nothing_behind() -> None:
    events = LocalFileEvents()
    for n in range(100):
        async with events.subscribe("t_acme", f"file_{n}") as waiter:
            with anyio.move_on_after(0):
                await waiter.wait()
    async with events.subscribe("t_acme", "file_x") as first, events.subscribe("t_acme", "file_x") as second:
        with anyio.move_on_after(0):
            await first.wait()
        await events.publish("t_acme", "file_x", "ready")
        with anyio.fail_after(1):
            await second.wait()  # still served while another subscriber remains
    assert events.tracked == 0
