"""T-TIME-1…4 (spec-04 §3, §13; IMPL-023): the commit rule fires at the exact instants on the FakeClock, the
decision gets `escalate_until == W`, and spec0 carries the request pins (no restart when sticky != pin)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tests.golden.sse_scenarios import CODING, HARD, document, router_rules, stream  # noqa: F401

from noema.core.clock import Clock
from noema.core.values import JsonValue

from .harness import STICKY_SOL, USER_BODY, TurnHarness, first, make_harness, run_turn, running, timings, types_of

pytestmark = pytest.mark.unit

CHUNKS = ("a ", "b ", "c.")
OPUS = "us.anthropic.claude-opus-5-5"
SOL = "us.openai.gpt-6.1-sol"


def _h(*rules: list[dict[str, Any]], hold: bool = False, sticky: Mapping[str, JsonValue] | None = None) -> TurnHarness:
    h = make_harness(scenario=document("t", *rules), hold_decisions_until_cancel=hold)
    h.seed_session("ses_1", last_seq=1, sticky_spec=STICKY_SOL if sticky is None else sticky)
    return h


async def test_time_1_a_commit_at_s1_or_at_first_text() -> None:
    """(a): S1 at 930 with text since 720 commits at 930; S1 before the first text arms the run, which commits
    at its first text (1200), never earlier."""
    h = _h([stream(SOL, first_event_ms=100, first_text_ms=720, chunks=CHUNKS)], router_rules())
    async with running(h):
        runner = await run_turn(h)
    at, ev = first(h, runner, "generation.committed")
    assert (at, ev["reason"]) == (930, "spec_agrees")
    assert first(h, runner, "decision.completed")[0] == 930
    h2 = _h([stream(SOL, first_event_ms=100, first_text_ms=1200, chunks=CHUNKS)], router_rules())
    async with running(h2):
        runner2 = await run_turn(h2)
    assert first(h2, runner2, "generation.committed")[0] == 1200
    assert first(h2, runner2, "route.confirmed")[0] == 1200
    assert timings(runner2)["holdback_ms"] == 0


async def test_time_2_b_hold_cap_fires_at_first_text_plus_max_hold() -> None:
    """(b): first text at 300 -> commit with holdback_deadline at exactly 700 (max_hold_ms 400); S1 applied late."""
    h = _h([stream(SOL, first_event_ms=50, first_text_ms=300, chunks=CHUNKS, step_ms=500)], router_rules())
    async with running(h):
        runner = await run_turn(h)
    at, ev = first(h, runner, "generation.committed")
    assert (at, ev["reason"]) == (700, "holdback_deadline")
    _, confirmed = first(h, runner, "route.confirmed")
    assert confirmed["routing"]["served_spec_differs"] is None and confirmed["routing"]["decision_id"] is None
    _, completed = first(h, runner, "decision.completed")
    assert completed["arrived"] == "after_commit"


async def test_hold_cap_with_nothing_buffered_arms_the_fallback_ruling_r2() -> None:
    """R2 (spec-04 §3): Opus's text at 300 makes `first_text()`, then Opus fails and Sol starts. The hold cap
    fires at 700 with no attempt buffering, so the run is armed: Sol commits at its own first text (900) with
    `holdback_deadline`, and S1 (930) arrives `after_commit` without a restart."""
    opus_fault = {"kind": "exception_event", "type": "modelStreamErrorException", "after": 1}
    opus = stream(OPUS, first_event_ms=50, first_text_ms=300, chunks=CHUNKS, step_ms=200, fault=opus_fault)
    sol = stream(SOL, first_event_ms=100, first_text_ms=600, chunks=CHUNKS)
    sticky = {**STICKY_SOL, "generation_profile": "opus-deep", "effort": "high", "max_output_tokens": 4000}
    h = _h([opus, sol], router_rules(), sticky=sticky)
    async with running(h):
        runner = await run_turn(h)
    events = h.events(runner.turn_id)
    started = [e["attempt_id"] for e in events if e["type"] == "generation.started"]
    assert [e["reason"] for e in events if e["type"] == "generation.cancelled"] == ["failed"]
    at, committed = first(h, runner, "generation.committed")
    assert (at, committed["reason"], committed["attempt_id"]) == (900, "holdback_deadline", started[1])
    assert runner.hold_fired and first(h, runner, "route.confirmed")[0] == 900
    at, completed = first(h, runner, "decision.completed")
    assert (at, completed["arrived"]) == (930, "after_commit")
    assert runner.result is not None and runner.result.status == "completed"
    assert timings(runner)["spec_restarts"] == 0
    # spec-04 §10: from the committed attempt's own first text (Sol, 900) to its commit (900), not Opus's text
    assert timings(runner)["holdback_ms"] == 0


class LateCommitted:
    """A generation engine whose runs hand `generation.committed` to the runner `delay_s` after the engine committed
    (the event's queue hop to the runner's consumer task on a busy loop; C2b perf smoke R3)."""

    def __init__(self, inner: Any, clock: Clock, delay_s: float) -> None:
        self.inner, self.clock, self.delay_s = inner, clock, delay_s

    @asynccontextmanager
    async def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with self.inner.run(*args, **kwargs) as run:
            yield _LateRun(run, self.clock, self.delay_s)


class _LateRun:
    def __init__(self, inner: Any, clock: Clock, delay_s: float) -> None:
        self._inner, self._clock, self._delay_s = inner, clock, delay_s

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def events(self) -> AsyncIterator[Any]:
        async for ev in self._inner.events():
            if ev.get("type") == "generation.committed":
                await self._clock.sleep(self._delay_s)
            yield ev


async def test_holdback_ends_at_the_engine_commit_not_when_the_runner_sees_it() -> None:
    """spec-04 §10: an armed run commits at its first text (holdback 0) even when `generation.committed` reaches
    the runner 3 ms later; a buffered answer counts from its first text (720) to the commit request at S1 (930)."""
    for first_text_ms, holdback_ms in ((1200, 0), (720, 210)):
        h = _h([stream(SOL, first_event_ms=100, first_text_ms=first_text_ms, chunks=CHUNKS)], router_rules())
        h.override("generation", LateCommitted(h.world.generation, h.clock, 0.003))
        async with running(h):
            runner = await run_turn(h)
        assert first(h, runner, "generation.committed")[0] == max(first_text_ms, 930) + 3  # seen 3 ms late
        assert timings(runner)["holdback_ms"] == holdback_ms


async def test_time_3_c_wait_limit_cancels_the_decision_at_w() -> None:
    """(c): S1 never resolves by itself -> at W = t_d0 + deadline_ms + slack_ms the run is cancelled and the closed
    snapshot is S1; `escalate_until == W` (IMPL-023); `decisions.deadline_ms` moves W."""
    for deadline_ms, w_ms in ((None, 1050), (600, 650)):
        rules = router_rules(haiku_silent=True)
        h = _h([stream(SOL, first_event_ms=100, first_text_ms=2000, chunks=CHUNKS)], rules, hold=True)
        body = {**USER_BODY, "session_id": "ses_1"}
        if deadline_ms is not None:
            body["decisions"] = {"deadline_ms": deadline_ms}
        async with running(h):
            runner = await run_turn(h, body)
        assert first(h, runner, "decision.completed")[0] == w_ms
        (drun,) = h.world.decisions.runs
        assert runner.t_d0 is not None
        escalate_until = drun._ctx.escalate_until
        assert escalate_until is not None and round((escalate_until.at - runner.t_d0) * 1000) == w_ms
        assert first(h, runner, "generation.committed")[0] == 2000


async def test_time_4_spec0_carries_request_pins() -> None:
    """spec0 = sticky with the request pins: a pinned profile/effort that differs from the sticky value does not
    restart (the decided spec carries the same pins)."""
    rules = [stream(OPUS, first_event_ms=100, first_text_ms=1500, chunks=CHUNKS)]
    h = _h(rules, router_rules())  # factual/moderate: decided = (opus-deep pinned, medium pinned, null)
    body = {**USER_BODY, "session_id": "ses_1", "generation": {"profile": "opus-deep", "effort": "medium"}}
    async with running(h):
        runner = await run_turn(h, body)
    _, selected = first(h, runner, "route.selected")
    assert selected["routing"]["selection"] == "pinned" and selected["routing"]["route_id"] == "opus-deep"
    assert selected["routing"]["params"] == {"effort": "medium"}
    assert "generation.cancelled" not in types_of(h, runner)  # no spec_changed restart
    _, confirmed = first(h, runner, "route.confirmed")
    assert confirmed["routing"]["selection"] == "pinned"
    assert confirmed["routing"]["params"] == {"effort": "medium"}
    assert confirmed["routing"]["served_spec_differs"] is False
    assert timings(runner)["spec_restarts"] == 0


async def test_deadline_cancels_without_frames_t19() -> None:
    """T19: at ctx.deadline every attempt is cancelled without a generation.cancelled frame and the turn ends
    with error{deadline_exceeded, retryable}, at the deadline instant."""
    h = _h([stream(SOL, first_event_ms=100, first_text_ms=720, chunks=CHUNKS, step_ms=2000)], router_rules())
    async with running(h):
        runner = await run_turn(h, deadline_s=2.0)
    types = types_of(h, runner)
    assert "generation.cancelled" not in types and "generation.completed" not in types
    at, err = first(h, runner, "error")
    assert err["code"] == "deadline_exceeded" and err["retryable"] is True
    assert runner.t_d0 is not None and round(at + (runner.t_d0 - runner.p.t_in) * 1000) == 2000
    assert types[-1] == "turn.completed" and runner.result is not None and runner.result.status == "failed"
    att = h.world.db.store.rows("attempt")[0]
    assert att["outcome"] == "committed" and att["finish_reason"] == "cancelled"  # T19
    turn = h.world.db.store.rows("turn")[0]
    assert turn["status"] == "failed" and turn["output_text"] == "a "


async def test_deadline_t19_with_the_real_generation_engine_records_finish_reason_cancelled() -> None:
    """T19 on the real generation engine: the committed attempt's row says `finish_reason: cancelled`; no
    generation.cancelled frame."""
    import random

    engine_mod = pytest.importorskip("noema.generation.engine")
    h = _h([stream(SOL, first_event_ms=100, first_text_ms=720, chunks=CHUNKS, step_ms=2000)], router_rules())
    w = h.world
    w.generation = engine_mod.StreamingGenerationEngine(
        clock=w.clock,
        ids=w.ids,
        adapters=w.adapters,
        broker=w.broker,
        health=w.health,
        retry_budget=w.retry_budget,
        estimator=w.estimator,
        cost=w.cost,
        telemetry=w.telemetry,
        rng=random.Random(7),
    )
    async with running(h):
        runner = await run_turn(h, deadline_s=2.0)
    types = types_of(h, runner)
    assert "generation.cancelled" not in types and first(h, runner, "error")[1]["code"] == "deadline_exceeded"
    (att,) = h.world.db.store.rows("attempt")
    assert att["outcome"] == "committed" and att["finish_reason"] == "cancelled"
