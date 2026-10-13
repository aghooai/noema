"""The decision and generation entries (spec-04 §3; IMPL-023, IMPL-074) and the gate strategy, deterministically:
- S1 may resolve before A1's `generation.run().__aenter__` returns (credentials refresh, an L2 hit): A1 is still
  committed (`spec_agrees`) or restarted (`spec_changed`) when it registers;
- t_d0 is the instant the decision entry returned (stage-0 views resolved), so W and `decision_ms` count from it;
- a failing decision entry never reaches t_d0: one spec0 `route.selected`, then `error`, no `decision.completed`;
- `strategy: gate`: S1 first, then `route.selected{speculative: false}`, one run committed with `gate`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tests.golden.compare import sse_invariants
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError

from .harness import STICKY_SOL, USER_BODY, TurnHarness, first, make_harness, run_turn, running, timings, types_of

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"
STICKY_OPUS = {**STICKY_SOL, "generation_profile": "opus-deep", "effort": "high", "max_output_tokens": 4000}


class SlowEntry:
    """Wraps an engine whose `run()` entry takes `delay_s` before the inner entry (broker.get refreshing STS for
    generation; view resolution, the pepper and broker.get for a decision). Records the contexts it was given."""

    def __init__(self, inner: Any, clock: Clock, delay_s: float) -> None:
        self.inner, self.clock, self.delay_s, self.runs = inner, clock, delay_s, inner.runs
        self.contexts: list[Any] = []

    def run(self, ctx: Any, *args: Any, **kwargs: Any) -> Any:
        self.contexts.append(ctx)
        return self._run(ctx, *args, **kwargs)

    @asynccontextmanager
    async def _run(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        await self.clock.sleep(self.delay_s)
        async with self.inner.run(*args, **kwargs) as run:
            yield run


class FailingEntry:
    """A decision engine whose entry raises (IMPL-057 dispatch check, or tenant access)."""

    def __init__(self, inner: Any, error: NoemaError) -> None:
        self.inner, self.runs, self.error = inner, inner.runs, error

    def run(self, ctx: Any, req: Any) -> FailingEntry:
        return self

    async def __aenter__(self) -> Any:
        raise self.error

    async def __aexit__(self, *exc: object) -> None:
        return None


def _h(*, sticky: Any = STICKY_SOL, decision_ms: int | None = None, text_ms: float = 300) -> TurnHarness:
    sol = stream(SOL, first_event_ms=100, first_text_ms=text_ms, chunks=["ok ", "done"], step_ms=50)
    rules = router_rules()
    if decision_ms is not None:  # every member answers at decision_ms (a fast quorum or an L2 hit)
        for rule in rules:
            if "latency_ms" in rule:
                rule["latency_ms"] = decision_ms
    h = make_harness(scenario=document("entry", [sol], rules))
    h.seed_session("ses_1", last_seq=3, sticky_spec=sticky)
    return h


async def test_s1_before_a1_entry_returns_still_commits_a1() -> None:
    h = _h(decision_ms=10)
    h.override("generation", SlowEntry(h.world.generation, h.clock, 0.05))
    async with running(h):
        runner = await run_turn(h, deadline_s=20.0)
    types = types_of(h, runner)
    assert runner.result is not None and runner.result.status == "completed" and "error" not in types
    assert first(h, runner, "decision.completed")[1]["arrived"] == "before_generation"
    assert first(h, runner, "generation.committed")[1]["reason"] == "spec_agrees"
    assert types.count("generation.started") == 1
    assert first(h, runner, "generation.committed")[0] < 500  # at A1's first text (~350 ms), not at ctx.deadline


async def test_s1_before_a1_entry_returns_still_restarts_on_a_different_spec() -> None:
    h = _h(sticky=STICKY_OPUS, decision_ms=10)
    h.override("generation", SlowEntry(h.world.generation, h.clock, 0.05))
    async with running(h):
        runner = await run_turn(h, deadline_s=20.0)
    events = h.events(runner.turn_id)
    cancelled = [e["reason"] for e in events if e["type"] == "generation.cancelled"]
    assert cancelled == ["spec_changed"] and types_of(h, runner).count("generation.started") == 2
    assert runner.result is not None and runner.result.status == "completed"
    assert timings(runner)["spec_restarts"] == 1


@pytest.mark.parametrize("entry_ms", [0, 200])
async def test_t_d0_is_after_the_decision_entry_so_w_counts_from_resolved_views(entry_ms: int) -> None:
    h = _h(text_ms=720)
    slow = SlowEntry(h.world.decisions, h.clock, entry_ms / 1000)
    h.override("decisions", slow)
    async with running(h):
        runner = await run_turn(h)
    at, completed = first(h, runner, "decision.completed")
    assert at == 930.0 and completed["decision"]["status"] == "quorum"  # ms from t_d0, as without the delay
    assert "decision.updated" in types_of(h, runner)
    assert timings(runner)["decision_ms"] == 930
    assert runner.t_d0 is not None
    (ctx,) = slow.contexts
    # escalate_until is fixed before the entry: W measured from there, never later than the W armed at t_d0; the
    # slack lets the engine count W from its own t0 instead (orchestrator request 10)
    assert ctx.escalate_until.at == pytest.approx(runner.wait_limit() - entry_ms / 1000)
    slack_ms = h.service.svc.config.current().routing.speculation.slack_ms
    assert slack_ms is not None and ctx.escalate_slack_ms == slack_ms
    assert runner.t_d0 + (runner.p.deadline_ms + slack_ms) / 1000 == pytest.approx(runner.wait_limit())


@pytest.mark.parametrize(
    "error",
    [
        NoemaError(ErrorCode.DEADLINE_EXCEEDED, "No time to dispatch the decision."),
        NoemaError(ErrorCode.TENANT_ACCESS_MISCONFIGURED, "AssumeRole failed."),
    ],
)
async def test_a_failing_decision_entry_owes_one_route_selected_and_no_decision(error: NoemaError) -> None:
    h = _h()
    h.override("decisions", FailingEntry(h.world.decisions, error))
    async with running(h):
        runner = await run_turn(h)
    events = h.events(runner.turn_id)
    assert types_of(h, runner) == ["turn.created", "preflight.completed", "route.selected", "error", "turn.completed"]
    assert runner.t_d0 is None  # t_d0 not reached: decision.completed is not owed (P-SSE-1)
    assert sse_invariants(events, reached_t_d0=False) == []
    selected = first(h, runner, "route.selected")[1]
    assert selected["speculative"] is True and selected["routing"]["route_id"] == "sol-fast"
    assert first(h, runner, "error")[1]["code"] == error.code.value
    assert runner.result is not None and runner.result.status == "failed"
    assert h.world.db.store.rows("turn")[0]["routing"]["route_id"] == "sol-fast"


async def test_gate_waits_for_s1_then_runs_the_decided_spec_once() -> None:
    h = _h(sticky=STICKY_OPUS, text_ms=720)
    async with running(h):
        runner = await run_turn(h, {**USER_BODY, "session_id": "ses_1", "strategy": "gate"})
    events = h.events(runner.turn_id)
    types = [e["type"] for e in events]
    assert types[:5] == [
        "turn.created",
        "preflight.completed",
        "decision.completed",
        "route.selected",
        "generation.started",
    ]
    assert first(h, runner, "decision.completed")[1]["arrived"] == "before_generation"
    at_s1, _ = first(h, runner, "decision.completed")
    at_sel, selected = first(h, runner, "route.selected")
    assert at_sel >= at_s1 and selected["speculative"] is False
    assert selected["routing"]["strategy"] == "gate" and selected["routing"]["decision_id"] is not None
    _, started = first(h, runner, "generation.started")
    assert started["speculative"] is False and started["attempt_id"] == selected["attempt_id"]
    _, committed = first(h, runner, "generation.committed")
    assert committed["reason"] == "gate" and types.count("generation.started") == 1
    assert "generation.cancelled" not in types and sse_invariants(events) == []
    assert runner.result is not None and runner.result.status == "completed"
    assert timings(runner)["spec_restarts"] == 0
