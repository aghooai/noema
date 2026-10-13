"""F12 (spec-10 §4, 06 §7): a retry storm: `http_5xx{503}` at `rate: 0.5` on every generation model (Sol, Opus,
GLM; the draw is seeded per request body and repeat), under a mix of pinned sol-fast, opus-deep and glm-reason
turns at RATE turns/s for 60 s (spec-10 asks 5x the C1 load = 200 streams per pod; this stack's two api processes
reach ~19 turns/s each (reports/perf/c2b-perf-smoke.md), so the row runs at what they sustain, labeled).

Pass (rulings R6 and R8): the amplification against each failing upstream X (Sol, Opus, GLM), every request to X at
the mock / X's first sends, is <= 1.1 in both rows; every turn ends typed (completed or `all_routes_unavailable`), S1
on every stream. X's first sends are the turns with an attempt started on X plus X's non-generation requests (the
labelers' unary calls to Sol: the retry budget of (account, X) counts them too, and nothing re-sends them here). The
generation-only ratio (converse-stream requests to X / those turns) is recorded too. A fallback to a different model
is a first send of that model, not a retry (R6), so requests per turn count every fallback (~1.2 by construction at
0.25): recorded, not gated. R8 drops IMPL-016's floor of 10 re-sends per account-minute, which allowed 1 + 10 / N
below N = 100 first sends (Sol 1.149 per upstream at `rate: 0.5` in Wave E2). The `rate: 0.5` row also opens the
service breakers (> 50% errors); the `rate: 0.25` row keeps them closed, so the retry budget alone bounds the storm
there. Every measure is recorded for both rows.
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import mock_calls, mock_seq
from tests.fault import kit

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
WINDOW_S, RATE = 60.0, 12.5
PROFILES = ("sol-fast", "opus-deep", "glm-reason")
FAILING = (kit.SOL, kit.OPUS, kit.GLM)


@pytest.mark.parametrize("rate", [0.5, 0.25])
async def test_f12_retry_storm_amplification_stays_within_the_budget(stack: Stack, mock: Mock, rate: float) -> None:
    await kit.fresh_retry_budget()
    storm = {"kind": "http_5xx", "status": 503, "rate": rate}
    gens = [kit.gen(m, fault=storm) for m in FAILING]
    await mock.load(kit.scenario("fault-F12", gens, kit.members()))
    before = await mock_seq(mock)
    records = await kit.run_turns(
        stack,
        int(WINDOW_S * RATE),
        lambda i, t: kit.body(f"f12-{t}", PROFILES[i % 3]),
        pace_s=1 / RATE,
        concurrency=100,
    )
    every = await mock_calls(mock, before)
    calls = [c for c in every if c["op"] == "converse-stream"]
    kit.mark_budget_used()
    per_profile = {p: [r for r in records if r.index % 3 == n] for n, p in enumerate(PROFILES)}
    amplification = len(calls) / len(records)
    requests_by_model = kit.counts(c["model"] for c in calls)
    first_sends = kit.counts(m for r in records for m in {str(a["model"]) for a in r.started})
    generation_per_upstream = {m: round(n / max(1, first_sends.get(m, 0)), 3) for m, n in requests_by_model.items()}
    other = kit.counts(c["model"] for c in every if c["op"] != "converse-stream")
    per_upstream = {
        m: round((requests_by_model.get(m, 0) + other.get(m, 0)) / max(1, first_sends.get(m, 0) + other.get(m, 0)), 3)
        for m in FAILING
    }
    attempted = [r for r in records if r.started]
    kit.record_result(
        f"F12-rate{rate}",
        turns=len(records),
        amplification_per_attempted_turn=round(len(calls) / max(1, len(attempted)), 3),
        rate_turns_per_s=RATE,
        generation_requests=len(calls),
        faulted_requests=sum(1 for c in calls if c.get("fault")),
        amplification=round(amplification, 3),
        requests_by_model=requests_by_model,
        first_sends_by_model=first_sends,
        other_requests_by_model=other,
        generation_amplification_per_upstream=generation_per_upstream,
        amplification_per_upstream=per_upstream,
        attempts_per_turn=kit.counts(len(r.started) for r in records),
        outcomes={p: kit.counts(kit.outcome(r) for r in rs) for p, rs in per_profile.items()},
        fallback=kit.fallback_numbers(records),
    )
    kit.assert_s1(records)
    assert all(kit.outcome(r) in ("completed", "error:all_routes_unavailable") for r in records)
    assert all(a <= 1.1 for a in per_upstream.values()), per_upstream
