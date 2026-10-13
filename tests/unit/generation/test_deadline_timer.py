"""On `SystemClock` a run's ctx.deadline (T19) is the event loop's timer, not a watcher task (E3); every other
clock keeps the watcher task (the FakeClock suites)."""

from __future__ import annotations

import asyncio
import random
import time

import pytest
from tests.contract_internal.kits import principal
from tests.fakes.generation import StaticPromptBuilder
from tests.fakes.ops_admission import CountingReservation
from tests.fakes.world import build_world

from noema.core.clock import Deadline, SystemClock
from noema.core.protocols.generation import AttemptBudget, AttemptSpec, TurnContext
from noema.generation import StreamingGenerationEngine

from .scripted import Call, ScriptedAdapter, ScriptedRegistry, text_stream

pytestmark = pytest.mark.unit
SOL_A = "sol-6.1"


def _engine(calls: list[Call]) -> tuple[StreamingGenerationEngine, SystemClock, TurnContext, float]:
    clock = SystemClock()
    world = build_world(clock=clock)
    snap = world.config.current()
    adapter = ScriptedAdapter(clock, snap, {SOL_A: calls})
    engine = StreamingGenerationEngine(
        clock=clock,
        ids=world.ids,
        adapters=ScriptedRegistry(adapter),
        broker=world.broker,
        health=world.health,
        retry_budget=world.retry_budget,
        estimator=world.estimator,
        cost=world.cost,
        telemetry=world.telemetry,
        rng=random.Random(7),
    )
    deadline_s = 0.15
    ctx = TurnContext(
        principal(),
        snap.tenant_ref("t_acme"),
        snap,
        Deadline(clock.monotonic() + deadline_s, clock),
        CountingReservation(),
        "turn_1",
        "ses_1",
        "0" * 32,
    )
    return engine, clock, ctx, deadline_s


def _watchers() -> list[asyncio.Task[object]]:
    return [t for t in asyncio.all_tasks() if "_watch_deadline" in repr(t.get_coro())]


async def test_a_held_answer_ends_by_t19_at_the_deadline_without_a_watcher_task() -> None:
    engine, _, ctx, deadline_s = _engine([Call(text_stream("held", first_s=0.01, stop=None), hang=True)])
    t0 = time.monotonic()
    async with engine.run(
        ctx,
        AttemptSpec("sol-fast", None, None, "none"),
        StaticPromptBuilder(),
        hold=True,
        speculative=True,
        failure_budget=AttemptBudget(),
    ) as run:
        await asyncio.sleep(0.02)
        assert _watchers() == [] and run._deadline_timer is not None
        [rec] = await run.result()
        events = [ev async for ev in run.events()]
    assert deadline_s <= time.monotonic() - t0 < 2.0
    assert (rec.outcome, rec.cancel_reason) == ("cancelled", "deadline")
    assert [ev["type"] for ev in events] == ["generation.started"]  # T19: no frame


async def test_a_run_that_ends_before_its_deadline_cancels_the_timer() -> None:
    engine, _, ctx, _ = _engine([Call(text_stream("a", "b", first_s=0.01, every_s=0.005))])
    async with engine.run(
        ctx,
        AttemptSpec("sol-fast", None, None, "none"),
        StaticPromptBuilder(),
        hold=False,
        speculative=True,
        failure_budget=AttemptBudget(),
    ) as run:
        [rec] = await run.result()
    assert rec.outcome == "committed" and rec.finish_reason == "stop"
    timer = run._deadline_timer
    assert timer is not None and timer.cancelled()
