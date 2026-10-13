"""Fake engine regressions (C2a, orchestrator.md items 6 and 12): behaviors the orchestrator relies on that the
generation and decision suites do not reach (an outer cancel, `hold_until_cancel`)."""

from __future__ import annotations

from typing import Any

import anyio
from tests.fakes.generation import FakeGenerationEngine, StaticPromptBuilder
from tests.fakes.world import build_world

from noema.core.protocols.generation import AttemptBudget, AttemptSpec

from .kits import DecisionKit, GenerationKit
from .test_decision_engine import context, d_std, turn_request
from .test_generation_engine import ctx, scenario

SOL = "us.openai.gpt-6.1-sol"


async def test_an_outer_cancel_does_not_hang_the_fake_generation_run() -> None:
    # item 12: the caller's scope is cancelled while the committed attempt streams; the run's exit must not wait
    # for its (already cancelled) drive task
    world = build_world()
    assert isinstance(world.generation, FakeGenerationEngine)
    stall = {"kind": "stall_after", "n": 1}
    world.use_scenario(scenario({"match": {"model": SOL}, "stream": {"chunks": ["a", "b"]}, "fault": stall}))
    kit = GenerationKit(world.generation, world.config.current(), world.fake_clock, lambda doc: None)
    scope = anyio.CancelScope()
    streaming = anyio.Event()

    async def turn() -> None:
        with scope:
            spec = AttemptSpec("sol-fast", None, None, "none")
            cm = kit.engine.run(
                ctx(kit), spec, StaticPromptBuilder(), hold=False, speculative=True, failure_budget=AttemptBudget()
            )
            async with cm as run:
                await run.first_text()
                streaming.set()
                await kit.clock.sleep(3600)

    async with kit.clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(turn)
        await streaming.wait()
        scope.cancel()
    [run] = world.generation.runs
    [rec] = await run.result()
    assert scope.cancelled_caught
    assert (rec.outcome, rec.finish_reason) == ("committed", "cancelled")


async def test_hold_until_cancel_still_closes_open_answers_by_the_turn_deadline() -> None:
    # item 6: S1 resolves by itself (no cancel); the stage deadline is ignored, but the open answer closes by
    # ctx.deadline, so updates() ends
    world = build_world(scenario=d_std(), hold_decisions_until_cancel=True)
    kit = DecisionKit(world.decisions, world.compiler, world.config.current(), world.fake_clock, lambda doc: None)
    req = turn_request(kit)  # Haiku never answers sensitive_data
    t0 = kit.clock.monotonic()
    updates: list[Any] = []
    with anyio.fail_after(10):  # real time: before the fix updates() never ended
        async with kit.clock.autojump(), kit.engine.run(context(kit, budget_s=60), req) as run:
            s1 = await run.commit_critical()
            s1_ms = round((kit.clock.monotonic() - t0) * 1000)
            updates = [u async for u in run.updates()]
            final_ms = round((kit.clock.monotonic() - t0) * 1000)
    assert (s1_ms, s1.final) == (930, False)
    assert len(updates) == 1 and updates[0].final and final_ms == 60_000
    assert "sensitive_data" in updates[0].final_names
