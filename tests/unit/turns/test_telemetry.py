"""The orchestrator's spans and metrics (spec-02 §12, IMPL-006): only declared names (the fake telemetry is strict
in env test), and the values of a restarted turn."""

from __future__ import annotations

import pytest
from tests.fakes.telemetry import FakeTelemetry
from tests.golden.compare import load_fixture

from .harness import make_harness, run_turn, running

pytestmark = pytest.mark.unit


async def test_spans_and_metrics_of_a_restarted_turn() -> None:
    fx = load_fixture("G02")
    h = make_harness(scenario=fx["scenario"])
    session = fx["setup"]["session"]
    h.seed_session(session["id"], last_seq=session["last_seq"], sticky_spec=session["sticky_spec"])
    async with running(h):
        await run_turn(h, fx["request"]["body"])
    telemetry = h.world.telemetry
    assert isinstance(telemetry, FakeTelemetry)
    spans = {s.name for s in telemetry.spans}
    assert {"turn", "preflight", "history.fetch", "bindings.evaluate"} <= spans
    assert all(s.ended for s in telemetry.spans if s.name in ("turn", "preflight", "bindings.evaluate"))
    names = {m.name for m in telemetry.metrics}
    assert {"aghoo_ttft_seconds", "aghoo_e2e_seconds", "aghoo_spec_restarts_total"} <= names
    assert telemetry.counter("aghoo_spec_restarts_total", profile="sol-fast") == 1
    # the waste counter is the generation engine's (the fake engine emits none; the real one is checked in
    # test_golden_real_generation.py): the orchestrator does not add it a second time
    assert telemetry.counter("aghoo_wasted_output_tokens_total") == 0
    (agreement,) = [m for m in telemetry.metrics if m.name == "aghoo_spec_agreement_ratio"]
    assert agreement.value == 0.0 and agreement.labels == {"profile": "sol-fast"}
    assert telemetry.dropped == []
