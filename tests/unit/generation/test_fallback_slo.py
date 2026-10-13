"""R6 properties of the pre-commit walk with the real `SlidingRetryBudget` and `BreakerRegistry` (adapters) under the
real engine, many turns on a FakeClock, one tenant account (the retry budget's worst case: one key per model):

- S1 / F1, F4-F8: >= 99 % of the turns whose first attempt failed pre-commit are served by the next member, whatever
  the failure kind (a fallback to another model is not a retry, so the budget's floor never refuses it);
- F12: in a retry storm (5xx or throttles at `rate` on every generation model; the breakers stay closed at 0.25)
  same-model re-sends (the default profile's Sol after a route's Sol failed, the throttle re-send) are the only
  retries: per failing upstream at most max(10, 10 % of its first sends) per 60 s window, i.e. amplification
  <= 1.1x against it once it carries >= 100 first sends a minute; at 0.5 (breakers open) upstream requests per turn
  <= 1.1 as well. Cross-model fallbacks are first sends of their model, not amplification (R6)."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import anyio
import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.generation import StaticPromptBuilder
from tests.fakes.world import build_world

from noema.adapters.resilience import BreakerRegistry, SlidingRetryBudget
from noema.core.clock import Deadline
from noema.core.errors import ErrorKind
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import MessageStart, ModelRequest, Stop, StreamEvent, StreamTimeouts
from noema.core.protocols.generation import AttemptBudget, AttemptRecord, AttemptSpec
from noema.generation import StreamingGenerationEngine

from .rig import GLM, OPUS, SOL, Rig
from .scripted import Call, ScriptedAdapter, ScriptedRegistry, error_at, failure, reasoning, text_stream

SOL_A, OPUS_A, GLM_A = "sol-6.1", "opus-5.5", "glm-5.3"
OK = Call(text_stream("The answer."))
START = (0.05, MessageStart(at=0.0))
FAULTS = {  # the fault rows' pre-commit failures of the first member
    "F1-throttled": Call(open_error=failure(ErrorKind.THROTTLED, "ThrottlingException"), open_s=0.06),
    "F4-hang": Call(open_error=failure(ErrorKind.RETRYABLE, "timeout:first_event"), open_s=10.0),
    "F5-stall": Call([START, reasoning(0.2, 300), error_at(30.0, ErrorKind.RETRYABLE, "timeout:first_answer")]),
    "F6-stream-exception": Call([START, error_at(0.2, ErrorKind.RETRYABLE, "modelStreamErrorException")]),
    "F7-reasoning-only": Call([START, reasoning(0.2, 900), (0.4, Stop("max_tokens"))]),
    "F7-empty-text": Call([START, (0.3, Stop("end_turn"))]),
    "F8-malformed": Call([START, error_at(0.2, ErrorKind.RETRYABLE, "malformed:chunk")]),
}


@dataclass
class NotingAdapter(ScriptedAdapter):
    """The scripted adapter, noting every request to the retry budget as the real adapters do (its ratio base)."""

    budget: SlidingRetryBudget | None = None

    def stream(
        self, req: ModelRequest, *, tenant: TenantRef, timeouts: StreamTimeouts, deadline: Deadline
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        assert self.budget is not None
        self.budget.note_request(tenant.aws_account_id, self.snapshot.model(req.model_alias).id)
        return super().stream(req, tenant=tenant, timeouts=timeouts, deadline=deadline)


def real_rig(scripts: dict[str, list[Call]]) -> Rig:
    clock = FakeClock()
    world = build_world(clock=clock)
    budget = SlidingRetryBudget(clock)
    adapter = NotingAdapter(clock, world.config.current(), scripts, budget=budget)
    engine = StreamingGenerationEngine(
        clock=clock,
        ids=world.ids,
        adapters=ScriptedRegistry(adapter),
        broker=world.broker,
        health=BreakerRegistry(clock),
        retry_budget=budget,
        estimator=world.estimator,
        cost=world.cost,
        telemetry=world.telemetry,
        rng=random.Random(7),
    )
    return Rig(clock, world, adapter, engine)


async def run_turns(rig: Rig, profiles: Sequence[str], pace_s: float) -> list[tuple[AttemptRecord, ...]]:
    """Start one turn every `pace_s` (concurrently, as the pods serve them); the attempt records of each turn."""
    out: list[tuple[AttemptRecord, ...]] = [() for _ in profiles]

    async def turn(i: int, profile: str) -> None:
        cm = rig.engine.run(
            rig.ctx(),
            AttemptSpec(profile, None, None, "none"),
            StaticPromptBuilder(),
            hold=False,
            speculative=False,
            auto_commit="spec_agrees",
            failure_budget=AttemptBudget(),
        )
        async with cm as run, anyio.create_task_group() as tg:
            tg.start_soon(lambda: _drain(run.events()))
            out[i] = await run.result()

    async with rig.clock.autojump(), anyio.create_task_group() as tg:
        for i, profile in enumerate(profiles):
            tg.start_soon(turn, i, profile)
            await rig.clock.sleep(pace_s)
    return out


async def _drain(events: AsyncIterator[object]) -> None:
    async for _ in events:
        pass


@pytest.mark.parametrize("fault", sorted(FAULTS))
async def test_pre_commit_fallback_success_is_at_least_99_percent(fault: str) -> None:
    first, profile = (GLM_A, "glm-reason") if fault.startswith("F7") else (OPUS_A, "opus-deep")
    rig = real_rig({first: [FAULTS[fault]] * 400, SOL_A: [OK] * 400})
    turns = await run_turns(rig, [profile] * 200, pace_s=0.25)
    failed_first = [t for t in turns if t and t[0].outcome == "failed"]
    served = [t for t in failed_first if t[-1].outcome == "committed" and t[-1].model == SOL]
    assert len(failed_first) >= 20, fault  # the row exercised the walk (the breakers skip the member afterwards)
    assert len(served) >= 0.99 * len(failed_first), (fault, len(served), len(failed_first))
    assert all(t and t[-1].outcome == "committed" for t in turns)  # every turn served (skips go to Sol directly)


@pytest.mark.parametrize(("kind", "rate"), [("5xx", 0.25), ("5xx", 0.5), ("throttled", 0.25)])
async def test_a_retry_storm_amplifies_no_failing_upstream_by_more_than_1_1(kind: str, rate: float) -> None:
    rng = random.Random(f"{kind}-{rate}")
    bad = (
        Call(open_error=failure(ErrorKind.RETRYABLE, "503"), open_s=0.05)
        if kind == "5xx"
        else Call(open_error=failure(ErrorKind.THROTTLED, "ThrottlingException"), open_s=0.05)
    )
    scripts = {alias: [bad if rng.random() < rate else OK for _ in range(3000)] for alias in (SOL_A, OPUS_A, GLM_A)}
    rig = real_rig(scripts)
    profiles = [("sol-fast", "opus-deep", "glm-reason")[i % 3] for i in range(750)]  # 12.5 turns/s for 60 s
    turns = await run_turns(rig, profiles, pace_s=0.08)
    requests = Counter(r.model for t in turns for r in t)
    first_sends = Counter(model for t in turns for model in {r.model for r in t})  # a turn's first request to it
    assert set(requests) == {SOL, OPUS, GLM}
    for model in (SOL, OPUS, GLM):
        resends = requests[model] - first_sends[model]  # the only retries (R6); one 60 s budget window
        assert resends <= max(10, 0.1 * first_sends[model]), (model, resends, first_sends[model])
        if first_sends[model] >= 100:  # the budget's ratio regime: amplification <= 1.1 against this upstream
            assert requests[model] <= 1.1 * first_sends[model], (model, requests[model], first_sends[model])
    if rate == 0.5:  # the gated F12 row (breakers open): upstream requests per turn sent
        assert sum(requests.values()) <= 1.1 * len(turns)
    assert max(len(t) for t in turns) <= 3  # the profile attempt cap: A1 + 2 failure-driven attempts
