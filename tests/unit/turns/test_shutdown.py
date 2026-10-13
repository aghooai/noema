"""Graceful shutdown (spec-11 §7 steps 3-4): running turns continue for the drain grace, then stop with
`error{all_routes_unavailable, retryable: true}` (status `failed`), and every turn finalizes before the drain hook
returns (turn row, usage lines, lease and reservation released, `turn_owner` deleted, log sealed). A forced close
of the process runtime (no drain) still finalizes the turn (shielded, bounded)."""

from __future__ import annotations

import random
from typing import Any

import pytest
from tests.golden.sse_scenarios import document, router_rules, stream

from .harness import STICKY_SOL, USER_BODY, TurnHarness, first, make_harness, run_turn, running, types_of

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"


def _h(*, chunks: int = 60, step_ms: float = 1000) -> TurnHarness:
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=[f"c{i} " for i in range(chunks)], step_ms=step_ms)
    h = make_harness(scenario=document("shutdown", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    return h


def _assert_finalized(h: TurnHarness, runner: Any) -> None:
    types = types_of(h, runner)
    assert types[-1] == "turn.completed" and types.count("turn.completed") == 1
    _, err = first(h, runner, "error")
    assert err["code"] == "all_routes_unavailable" and err["retryable"] is True
    assert runner.result is not None and runner.result.status == "failed"
    (row,) = [r for r in h.world.db.store.rows("turn") if r["id"] == runner.turn_id]
    assert row["status"] == "failed" and row["output_text"]  # the committed text is kept
    assert [line for line in h.world.cost.lines if line.attempt_id]  # usage lines recorded
    session = h.world.db.store.rows("session")[0]
    assert session["active_turn_id"] is None and session["last_seq"] == 4  # the lease ended with the finalize
    assert h.world.admission.live_streams("t_acme", "p1") == 0  # the reservation was released
    assert runner.turn_id not in h.coordinator.state.owners
    assert h.log._local(runner.turn_id).sealed  # type: ignore[union-attr]


async def test_drain_lets_turns_run_for_the_grace_then_stops_them_with_a_retryable_error() -> None:
    h = _h()
    drained: list[bool] = []

    async def shutdown(runner: Any) -> None:
        await h.clock.sleep(2.0)
        drained.append(await h.runtime.drain(timeout_s=60))
        drained.append(runner.done.is_set())

    async with running(h):
        runner = await run_turn(h, deadline_s=600, during=shutdown)
    assert drained == [True, True]  # the drain hook returned only after the turn finalized
    _assert_finalized(h, runner)
    at, _ = first(h, runner, "error")
    assert 31_500 <= at <= 32_500  # t_d0 ≈ 0; drain at 2 s + the 30 s grace
    cancelled = [e for e in h.events(runner.turn_id) if e["type"] == "generation.cancelled"]
    assert [e["reason"] for e in cancelled] == ["client_abort"]
    deltas = [t for t, e in h.timed(runner) if e["type"] == "generation.delta"]
    assert max(deltas) > 30_000  # the turn kept streaming during the grace


async def test_drain_leaves_short_turns_alone() -> None:
    h = _h(chunks=3, step_ms=100)

    async def shutdown(runner: Any) -> None:
        await h.clock.sleep(0.5)
        assert await h.runtime.drain(timeout_s=60)

    async with running(h):
        runner = await run_turn(h, deadline_s=600, during=shutdown)
    assert runner.result is not None and runner.result.status == "completed"
    assert "error" not in types_of(h, runner)


async def test_drain_after_generation_completed_keeps_the_completed_status() -> None:
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=["a ", "b "], step_ms=100)
    # the final snapshot never comes by itself: the turn waits for it after generation.completed
    h = make_harness(scenario=document("shutdown", [sol], router_rules()), hold_decisions_until_cancel=True)
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)

    async def shutdown(runner: Any) -> None:
        await h.clock.sleep(1.5)
        assert runner.completed and not runner.done.is_set()
        await h.service._drain(0.0)

    async with running(h):
        runner = await run_turn(h, deadline_s=600, during=shutdown)
    types = types_of(h, runner)
    assert "generation.completed" in types and "error" not in types
    assert runner.result is not None and runner.result.status == "completed"
    assert h.world.db.store.rows("turn")[0]["status"] == "completed"


@pytest.mark.parametrize("engine", ["fake", "real"])
async def test_forced_runtime_close_still_finalizes_the_turn(engine: str) -> None:
    """Both engines end their attempts on an outer cancel (the fake's exit no longer hangs, request 12)."""
    h = _h()
    if engine == "real":
        engine_mod = pytest.importorskip("noema.generation.engine")
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
    await h.runtime.open()
    try:
        async with h.clock.autojump():
            runner = await h.start({**USER_BODY, "session_id": "ses_1"}, deadline_s=600)
            await h.clock.sleep(3.0)
        await h.runtime.close()  # no drain: the runtime cancels the turn task (in the task that opened it)
    finally:
        await h.runtime.close()
    assert runner.done.is_set()
    _assert_finalized(h, runner)


async def test_a_failing_finalize_still_ends_the_stream_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    from noema.turns import runner as runner_mod

    async def broken(r: Any) -> Any:
        raise RuntimeError("bug in finalize")

    monkeypatch.setattr(runner_mod, "finish", broken)
    h = _h(chunks=2, step_ms=100)
    async with running(h):
        runner = await run_turn(h, deadline_s=600)
    types = types_of(h, runner)
    assert types[-1] == "turn.completed" and types.count("turn.completed") == 1
    assert runner.done.is_set() and runner.result is not None
    assert h.world.admission.live_streams("t_acme", "p1") == 0
    assert runner.turn_id not in h.coordinator.state.owners
    assert h.log._local(runner.turn_id).sealed  # type: ignore[union-attr]


@pytest.mark.parametrize("persisted", [False, True])
async def test_a_failing_finalize_releases_the_session_lease_unless_outboxed(
    monkeypatch: pytest.MonkeyPatch, persisted: bool
) -> None:
    """`finish()` fails (a bug): nothing persisted -> the minimal close also releases the session lease (the next
    same-session turn need not wait out the 30 s expiry); an already outboxed finalize keeps it (Wave E fix)."""
    from noema.turns import finish as finish_mod
    from noema.turns import persist

    h = _h(chunks=2, step_ms=100)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bug in finalize")

    async def db_down(runner: Any) -> None:
        if persisted:  # the DB goes down after the lock (finalize outboxed), then a later step fails
            await h.clock.sleep(0.5)
            h.world.db.store.available = False

    if persisted:
        monkeypatch.setattr(persist, "usage_lines", broken)
    else:
        monkeypatch.setattr(finish_mod, "stored_routing", broken)
    async with running(h):
        runner = await run_turn(h, deadline_s=600, during=db_down)
    assert types_of(h, runner)[-1] == "turn.completed" and runner.result is not None
    h.world.db.store.available = True
    session = h.world.db.store.rows("session")[0]
    assert session["last_seq"] == 3
    assert session["active_turn_id"] == (runner.turn_id if persisted else None)
    assert [r.id for r in h.world.outbox.of_kind("turn_finalize")] == ([runner.turn_id] if persisted else [])
