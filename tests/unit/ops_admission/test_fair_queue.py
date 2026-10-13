"""`FairQueue` (spec-11 §1.2): round robin over tenants, FIFO within one, the 100 ms poll, queue timeouts,
cancellation and rollback. T-ADM-1: a flooding tenant does not delay another tenant's admission by > 10%."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio
import anyio.lowlevel
import pytest
from tests.fakes.clock import FakeClock

from noema.ops.admission.fair_queue import Attempt, FairQueue, Full, QueueTimeout

pytestmark = pytest.mark.unit
ATTEMPT_COST_S = 0.001  # one simulated Redis round trip of virtual time


@dataclass
class Slots:
    """A toy slot store: key -> free places; every attempt costs ATTEMPT_COST_S of virtual time."""

    clock: FakeClock
    free: dict[str, int] = field(default_factory=dict)
    attempts: list[str] = field(default_factory=list)
    rolled_back: list[object] = field(default_factory=list)

    def attempt(self, key: str, value: object) -> Attempt:
        async def run() -> object:
            await self.clock.sleep(ATTEMPT_COST_S)
            self.attempts.append(key)
            if self.free.get(key, 0) <= 0:
                return Full(key)
            self.free[key] -= 1
            return value

        return run

    async def rollback(self, value: object) -> None:
        self.rolled_back.append(value)


def waiter(
    queue: FairQueue, slots: Slots, tenant: str, key: str, value: object, timeout_s: float = 30.0
) -> Callable[[], Awaitable[object]]:
    async def run() -> object:
        return await queue.wait(tenant, frozenset({key}), timeout_s, slots.attempt(key, value), slots.rollback)

    return run


async def victim_wait(flood: int) -> float:
    """Tenant B waits for slot "b" (freed at t = 1 s) while tenant A floods `flood` waiters on slots that never
    free (distinct keys, so each costs an attempt per poll tick). Returns B's queue wait (virtual seconds)."""
    clock = FakeClock()
    slots = Slots(clock)
    queue = FairQueue(clock)
    admitted: dict[str, float] = {}
    timeouts: list[int] = []
    start = clock.monotonic()

    async def run_a(i: int) -> None:
        try:
            await waiter(queue, slots, "A", f"a{i}", i, timeout_s=3.0)()
        except QueueTimeout:
            timeouts.append(i)

    async def run_b() -> None:
        await waiter(queue, slots, "B", "b", "B")()
        admitted["B"] = clock.monotonic() - start

    async def free_b() -> None:
        await clock.sleep(1.0)
        slots.free["b"] = 1
        queue.released(["b"])

    async with clock.autojump(), anyio.create_task_group() as tg:
        for i in range(flood):
            tg.start_soon(run_a, i)
        tg.start_soon(run_b)
        tg.start_soon(free_b)
    assert len(timeouts) == flood and len(queue) == 0
    return admitted["B"]


async def test_tadm1_a_flooding_tenant_does_not_starve_another() -> None:
    solo = await victim_wait(0)
    flooded = await victim_wait(200)
    assert solo == pytest.approx(1.0, abs=0.01)
    assert flooded <= 1.10 * solo  # spec-11 T-ADM-1: at most 10% worse


async def test_fifo_within_a_tenant_and_release_wakes_the_head() -> None:
    clock = FakeClock()
    slots = Slots(clock)
    queue = FairQueue(clock)
    order: list[object] = []

    async def run(value: str) -> None:
        order.append(await waiter(queue, slots, "A", "k", value)())

    async with clock.autojump(), anyio.create_task_group() as tg:
        for value in ("first", "second", "third"):
            tg.start_soon(run, value)
            await clock.sleep(0.01)
        for _ in range(3):
            await clock.sleep(0.05)
            slots.free["k"] = 1
            queue.released(["k"])
    assert order == ["first", "second", "third"]


async def test_a_release_elsewhere_is_seen_at_the_next_poll_tick() -> None:
    clock = FakeClock()
    slots = Slots(clock)
    queue = FairQueue(clock, poll_s=0.1)
    got: list[float] = []

    async def run() -> None:
        await waiter(queue, slots, "A", "k", "v")()
        got.append(clock.monotonic())

    start = clock.monotonic()
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(run)
        await clock.sleep(0.25)
        slots.free["k"] = 1  # freed by another pod: no released() here
    assert 0.25 < got[0] - start <= 0.25 + 0.1 + 2 * ATTEMPT_COST_S


async def test_queue_timeout_and_depth_gauge() -> None:
    clock = FakeClock()
    slots = Slots(clock)
    depths: list[int] = []
    queue = FairQueue(clock, on_depth=depths.append)
    start = clock.monotonic()
    async with clock.autojump():
        with pytest.raises(QueueTimeout):
            await waiter(queue, slots, "A", "k", "v", timeout_s=0.5)()
    assert clock.monotonic() - start == pytest.approx(0.5, abs=2 * ATTEMPT_COST_S)
    assert depths == [1, 0] and len(queue) == 0 and not queue.blocks(["k"])


async def test_an_attempt_error_ends_only_that_waiter() -> None:
    clock = FakeClock()
    slots = Slots(clock)
    queue = FairQueue(clock)

    async def failing() -> object:
        raise LookupError("rate exceeded while queued")

    results: list[object] = []

    async def bad() -> None:
        with pytest.raises(LookupError):
            await queue.wait("A", frozenset({"x"}), 5.0, failing, slots.rollback)

    async def good() -> None:
        results.append(await waiter(queue, slots, "B", "y", "ok")())

    slots.free["y"] = 1
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(bad)
        tg.start_soon(good)
    assert results == ["ok"]


async def test_a_cancelled_waiter_leaves_the_queue_and_the_leader_hands_over() -> None:
    clock = FakeClock()
    slots = Slots(clock)
    queue = FairQueue(clock)
    got: list[object] = []
    leader_scope = anyio.CancelScope()
    leader_done = anyio.Event()

    async def leader() -> None:
        try:
            with leader_scope:
                await waiter(queue, slots, "A", "k1", "one")()
        finally:
            leader_done.set()

    async def second() -> None:
        got.append(await waiter(queue, slots, "B", "k2", "two")())

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(leader)
        await clock.sleep(0.01)
        tg.start_soon(second)
        await clock.sleep(0.01)
        leader_scope.cancel()  # the pump moves to the other waiter
        await leader_done.wait()
        assert len(queue) == 1 and not queue.blocks(["k1"])
        await clock.sleep(0.2)
        slots.free["k2"] = 1
        queue.released(["k2"])
    assert got == ["two"] and slots.rolled_back == []


async def test_a_success_for_a_cancelled_waiter_is_rolled_back() -> None:
    clock = FakeClock()
    slots = Slots(clock)
    queue = FairQueue(clock)
    started = anyio.Event()
    finish = anyio.Event()

    async def slow_attempt() -> object:
        started.set()
        await finish.wait()  # shielded by the pump: the waiter's cancellation cannot interrupt it
        return "granted"

    scope = anyio.CancelScope()
    async with anyio.create_task_group() as tg:

        async def run() -> None:
            with scope:
                await queue.wait("A", frozenset({"k"}), 5.0, slow_attempt, slots.rollback)

        tg.start_soon(run)
        await started.wait()
        scope.cancel()
        await anyio.lowlevel.checkpoint()
        finish.set()
    assert slots.rolled_back == ["granted"] and len(queue) == 0
