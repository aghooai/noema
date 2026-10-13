"""The orchestrator on the real decision engine (`noema.decision`, Wave C1) and the real question compiler, on the
scenario-driven fake adapters and the FakeClock:
- G01-G11 match with the real decision engine alone and with both real engines (generation too). Allowed
  differences: G04b's `hold_until_cancel` is a fake-engine knob (the real engine closes S1 at its own stage-0
  deadline, t_d0 + 1000 ms, before W = t_d0 + 1050 ms), so its `decision_ms` / `decision_lead_ms` differ;
- T-HIER-T1 with the engine's two rounds (spec-04 §3, spec-06 §19): non-blocking provisional at S1 and final in
  the update; blocking with `deadline_ms: 3000` moves S1 and W; blocking with the default deadline is unavailable.
Skipped while the decision package has no engine."""

from __future__ import annotations

import random
from typing import Any

import pytest
from tests.golden.compare import compare, fixture_paths, load_fixture
from tests.golden.sse_scenarios import document, router_rules, stream

from . import test_golden_sse as golden
from .harness import STICKY_SOL, TurnHarness, first, make_harness, run_turn, running
from .test_hierarchical_app_question import APP_Q, _body

pytestmark = pytest.mark.unit

engine_mod = pytest.importorskip("noema.decision.engine")
compiler_mod = pytest.importorskip("noema.decision.compiler")
deps_mod = pytest.importorskip("noema.decision.deps")
generation_mod = pytest.importorskip("noema.generation.engine")

SOL = "us.openai.gpt-6.1-sol"
FIXTURES = sorted(f for f in fixture_paths() if not f.startswith("G12"))
HOLD_ONLY = {"G04b": ("decision_ms", "decision_lead_ms")}  # fake-only hold_until_cancel


def real_decisions(h: TurnHarness) -> TurnHarness:
    w = h.world
    deps = deps_mod.EngineDeps(
        clock=w.clock,
        ids=w.ids,
        adapters=w.adapters,
        broker=w.broker,
        secrets=w.secrets,
        views=w.views,
        health=w.health,
        estimator=w.estimator,
        cost=w.cost,
        calibrators=w.calibrators,
        sampler=w.sampler,
        cache=w.decision_cache,
        keys=w.keys,
        db=w.db,
        outbox=w.outbox,
        telemetry=w.telemetry,
    )
    w.decisions = engine_mod.DecisionEngineImpl(deps)
    w.compiler = compiler_mod.QuestionCompilerImpl(w.config)
    return h


def real_engines(h: TurnHarness) -> TurnHarness:
    real_decisions(h)
    w = h.world
    w.generation = generation_mod.StreamingGenerationEngine(
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
    return h


def _allowed(problems: list[str], fixture_id: str) -> list[str]:
    return [p for p in problems if not any(f".{k}:" in p for k in HOLD_ONLY.get(fixture_id, ()))]


@pytest.mark.parametrize("fixture_id", FIXTURES)
async def test_golden_fixture_with_the_real_decision_engine(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    h, runner, _ = await golden.run_fixture(fx, real_decisions)
    assert _allowed(compare(h.timed(runner), fx), fixture_id) == []
    assert golden.invariants_ok([e for _, e in h.timed(runner)])


@pytest.mark.parametrize("fixture_id", FIXTURES)
async def test_golden_fixture_with_both_real_engines(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    h, runner, _ = await golden.run_fixture(fx, real_engines)
    assert _allowed(compare(h.timed(runner), fx), fixture_id) == []
    assert golden.invariants_ok([e for _, e in h.timed(runner)])


def _hier(round_ms: float) -> TurnHarness:
    """Round 1 picks the group "options 1-10", round 2 `line_07` inside it, each after `round_ms`."""
    item = {f"line_{i:02d}": (0.8 if i == 7 else 0.2 / 9) for i in range(10)}
    rules: list[dict[str, Any]] = [
        {
            "match": {"prompt_contains": '"value": "options 1-10"'},
            "answer": {"dist": {"options 1-10": 0.95, "options 11-20": 0.05}},
            "latency_ms": round_ms,
        },
        {"match": {"prompt_contains": "Which product line"}, "answer": {"dist": item}, "latency_ms": round_ms},
    ]
    sol = stream(SOL, first_event_ms=100, first_text_ms=600, chunks=["ok"])
    h = real_decisions(make_harness(scenario=document("h", [sol], rules, router_rules())))
    h.seed_session("ses_1", last_seq=1, sticky_spec=STICKY_SOL)
    return h


def _answer(event: dict[str, Any], name: str = "product_line") -> dict[str, Any]:
    return next(a for a in event["decision"]["answers"] if a["name"] == name)


async def test_t_hier_t1_non_blocking_is_provisional_at_s1_and_final_in_the_update() -> None:
    h = _hier(480)  # round 1 at 480 ms (its budget is 500), round 2 at 960: after S1 (930), before 1000
    async with running(h):
        runner = await run_turn(h, _body())
    q = next(q for q in runner.p.questions.questions if q.name == APP_Q["name"])
    assert q.method == "hierarchical" and not q.commit_critical
    at, completed = first(h, runner, "decision.completed")
    assert at == pytest.approx(930, abs=1) and completed["decision"]["status"] == "quorum"
    assert _answer(completed)["type"] == "unavailable"  # provisional: still open at S1
    _, updated = first(h, runner, "decision.updated")
    final = _answer(updated)
    assert "product_line" in updated["changed"] and final["type"] == "choice" and final["choice"] == "line_07"
    assert final["choice_method"] == "hierarchical"
    assert runner.result is not None and runner.result.status == "completed"


async def test_t_hier_t1_blocking_with_deadline_3000_moves_s1_and_w() -> None:
    h = _hier(900)
    async with running(h):
        runner = await run_turn(h, _body(blocking=["product_line"], deadline_ms=3000))
    at, completed = first(h, runner, "decision.completed")
    assert 1800 <= at < 2000  # both rounds (900 ms each) before S1
    assert _answer(completed)["type"] == "choice" and _answer(completed)["choice"] == "line_07"
    assert runner.t_d0 is not None and round((runner.wait_limit() - runner.t_d0) * 1000) == 3050
    assert runner.result is not None and runner.result.status == "completed"


async def test_t_hier_t1_blocking_past_the_default_deadline_is_unavailable() -> None:
    h = _hier(900)  # round 1 has 500 ms of the default 1,000 ms
    async with running(h):
        runner = await run_turn(h, _body(blocking=["product_line"]))
    at, completed = first(h, runner, "decision.completed")
    assert at <= 1000 and _answer(completed)["type"] == "unavailable"
    assert runner.decided is not None and runner.decided.spec.generation_profile == "sol-fast"
    assert runner.decided.selection == "rule"


class _SlowSecrets:
    """The decision entry's secrets read, slowed (a slow entry step: stage-0 media views, STS, the L2 GET)."""

    def __init__(self, inner: Any, clock: Any, delay: float) -> None:
        self.inner, self.clock, self.delay = inner, clock, delay

    async def get(self, ref: str) -> Any:
        await self.clock.sleep(self.delay)
        return await self.inner.get(ref)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_a_cancel_during_the_decision_entry_sends_no_member_call() -> None:
    """A cancel that lands while `decisions.run(...)` is still entering: the run is cancelled as soon as the entry
    returns, so no member call goes out after the cancel (Wave E fix; the turn is `cancelled`)."""
    import dataclasses

    import anyio

    from .harness import principal

    fx = load_fixture("G01")
    h = golden._setup(fx, real_engines)
    w = h.world
    engine = w.decisions
    assert isinstance(engine, engine_mod.DecisionEngineImpl)
    engine.deps = dataclasses.replace(engine.deps, secrets=_SlowSecrets(w.secrets, w.clock, 0.5))
    async with running(h):
        runner = await h.start(fx["request"]["body"])

        async def consume() -> None:
            async for _ in w.events.subscribe(runner.turn_id):
                pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await h.clock.sleep(0.2)
            t_cancel = h.clock.monotonic()
            await h.service.cancel(principal(), runner.turn_id)
            await runner.done.wait()
    assert runner.result is not None and runner.result.status == "cancelled"
    members = [c for c in w.adapters.calls() if c.request.call_kind == "decision_member"]
    assert [c for c in members if c.started_at >= t_cancel] == []
