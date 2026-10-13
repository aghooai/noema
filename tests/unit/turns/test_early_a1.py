"""A1 starts before the decision run's entry (E3, sprint A follow-up): under holdback the spec0 stream request leaves
while the entry resolves its views and the L2 lookup, with the spec-04 §4 event order unchanged; gate still waits for
S1. A decision entry that fails after A1 started stops A1 before the turn's error (P-SSE-1 holds)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from tests.golden.compare import sse_invariants
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError

from .harness import STICKY_SOL, USER_BODY, TurnHarness, first, make_harness, run_turn, running, types_of

pytestmark = pytest.mark.unit
SOL = "us.openai.gpt-6.1-sol"
ENTRY_S = 0.2


class SlowEntry:
    """A decision engine whose `run()` entry takes ENTRY_S first, then enters the real one (or raises `error`)."""

    def __init__(self, inner: Any, clock: Clock, error: NoemaError | None = None) -> None:
        self.inner, self.clock, self.error, self.runs = inner, clock, error, inner.runs

    def run(self, ctx: Any, req: Any) -> Any:
        return self._run(ctx, req)

    @asynccontextmanager
    async def _run(self, ctx: Any, req: Any) -> AsyncIterator[Any]:
        await self.clock.sleep(ENTRY_S)
        if self.error is not None:
            raise self.error
        async with self.inner.run(ctx, req) as run:
            yield run


def _h(error: NoemaError | None = None) -> TurnHarness:
    sol = stream(SOL, first_event_ms=100, first_text_ms=1100, chunks=["ok ", "done"], step_ms=50)
    h = make_harness(scenario=document("early-a1", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    h.override("decisions", SlowEntry(h.world.decisions, h.clock, error))
    return h


def _stream_started(h: TurnHarness) -> float:
    return next(c.started_at for c in h.world.adapters.calls() if c.op.endswith("-stream"))


async def test_under_holdback_the_a1_request_leaves_before_the_decision_entry_returns() -> None:
    h = _h()
    async with running(h):
        runner = await run_turn(h)
    assert runner.t_d0 is not None
    assert runner.t_d0 - _stream_started(h) == pytest.approx(ENTRY_S)  # A1 opened at the entry's start
    types = types_of(h, runner)
    assert types[:5] == [
        "turn.created",
        "preflight.completed",
        "route.selected",
        "generation.started",
        "decision.completed",
    ]
    assert types.index("generation.committed") > types.index("decision.completed")
    assert sse_invariants(h.events(runner.turn_id)) == []
    assert runner.result is not None and runner.result.status == "completed"
    assert first(h, runner, "decision.completed")[1]["arrived"] == "during_holdback"


async def test_gate_still_starts_its_run_after_s1() -> None:
    h = _h()
    async with running(h):
        runner = await run_turn(h, {**USER_BODY, "strategy": "gate"})
    assert runner.t_d0 is not None and _stream_started(h) > runner.t_d0
    types = types_of(h, runner)
    assert types.index("decision.completed") < types.index("route.selected") < types.index("generation.started")
    assert sse_invariants(h.events(runner.turn_id)) == []


@pytest.mark.parametrize(
    ("error", "frames"),
    [
        (NoemaError(ErrorCode.DEADLINE_EXCEEDED, "No time to dispatch the decision."), []),  # T19: no frame
        (NoemaError(ErrorCode.TENANT_ACCESS_MISCONFIGURED, "AssumeRole failed."), ["generation.cancelled"]),
    ],
)
async def test_an_entry_that_fails_after_a1_started_stops_a1_before_the_error(
    error: NoemaError, frames: list[str]
) -> None:
    h = _h(error)
    async with running(h):
        runner = await run_turn(h)
    types = types_of(h, runner)
    head = ["turn.created", "preflight.completed", "route.selected", "generation.started"]
    assert types == [*head, *frames, "error", "turn.completed"]
    assert runner.t_d0 is None and sse_invariants(h.events(runner.turn_id), reached_t_d0=False) == []
    assert first(h, runner, "error")[1]["code"] == error.code.value
    [record] = runner.records
    reason = "deadline" if error.code is ErrorCode.DEADLINE_EXCEEDED else "client_abort"
    assert (record.outcome, record.cancel_reason) == ("cancelled", reason)
    assert runner.result is not None and runner.result.status == "failed"


async def _run_recording_dispatch(h: TurnHarness) -> tuple[Any, list[float]]:
    """run_turn, recording when the turn reports a dispatched model call (IMPL-077: a JSON response is then kept)."""
    runner = await h.start({**USER_BODY, "session_id": "ses_1"})
    calls: list[float] = []
    runner.on_dispatch = lambda: calls.append(h.clock.monotonic())

    async def consume() -> None:
        async for _ in h.world.events.subscribe(runner.turn_id):
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await runner.done.wait()
    return runner, calls


async def test_a1_reports_its_dispatch_and_a_failing_entry_before_a1_started_reports_none() -> None:
    h = _h()
    async with running(h):
        runner, calls = await _run_recording_dispatch(h)
    assert calls and calls[0] == pytest.approx(_stream_started(h))  # at A1's start, before the entry returned
    h = _h()
    h.override("decisions", FailingAtOnce(NoemaError(ErrorCode.DEADLINE_EXCEEDED, "No time to dispatch.")))
    async with running(h):
        runner, calls = await _run_recording_dispatch(h)
    assert calls == [] and types_of(h, runner)[2:] == ["route.selected", "error", "turn.completed"]


class FailingAtOnce:
    """A decision engine whose entry raises before A1's first step (no model call went out)."""

    def __init__(self, error: NoemaError) -> None:
        self.error = error
        self.runs: list[Any] = []

    def run(self, ctx: Any, req: Any) -> FailingAtOnce:
        return self

    async def __aenter__(self) -> Any:
        raise self.error

    async def __aexit__(self, *exc: object) -> None:
        return None
