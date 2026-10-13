"""The golden fixtures with the real generation engine (`noema.generation`, Wave C1) under the orchestrator, on the
scenario-driven fake adapters: everything matches (G02/G06 do not pin `timings.wasted_output_tokens`, whose real
value applies IMPL-020; generation.md request 2). Skipped while the generation package has no engine."""

from __future__ import annotations

import random

import pytest
from tests.fakes.telemetry import FakeTelemetry
from tests.golden.compare import compare, fixture_paths, load_fixture

from . import test_golden_sse as golden
from .harness import TurnHarness

pytestmark = pytest.mark.unit

engine_mod = pytest.importorskip("noema.generation.engine")


def _with_real_engine(h: TurnHarness) -> TurnHarness:
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
    return h


@pytest.mark.parametrize("fixture_id", sorted(f for f in fixture_paths() if not f.startswith("G12")))
async def test_golden_fixture_with_the_real_generation_engine(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    h, runner, _ = await golden.run_fixture(fx, _with_real_engine)
    assert compare(h.timed(runner), fx) == []
    assert golden.invariants_ok([e for _, e in h.timed(runner)])


@pytest.mark.parametrize("fixture_id", ["G02", "G06"])
async def test_waste_counter_is_counted_once_per_waste_attempt(fixture_id: str) -> None:
    """`aghoo_wasted_output_tokens_total` (08 §7) = the output tokens of the turn's non-committed attempts, once:
    the generation engine owns it, the orchestrator does not add it again (Wave E fix)."""
    fx = load_fixture(fixture_id)
    h, runner, _ = await golden.run_fixture(fx, _with_real_engine)
    wasted = sum(rec.usage.output_tokens for rec in runner.records if rec.outcome != "committed")
    assert wasted > 0
    assert runner.result.turn["timings"]["wasted_output_tokens"] == wasted
    telemetry = h.world.telemetry
    assert isinstance(telemetry, FakeTelemetry)
    assert telemetry.counter("aghoo_wasted_output_tokens_total") == wasted
