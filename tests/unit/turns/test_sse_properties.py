"""P-SSE-1 (spec-04 §4, §13): over random scenario timings and faults (hypothesis), every turn's event log keeps
the §4 invariants, every frame is contract-valid, the failure budget holds (at most 2 failure-driven attempts) and
there is at most one restart; the turn always ends with `turn.completed` and its stored status matches."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.golden.compare import sse_invariants
from tests.golden.sse_scenarios import CODING, FACTUAL, HARD, MODERATE, document, router_rules, stream

from .harness import STICKY_SOL, USER_BODY, make_harness, principal, run_turn, running
from .test_decision_entry import SlowEntry
from .test_golden_sse import assert_frames_valid

pytestmark = pytest.mark.unit

SOL, OPUS, GLM = "us.openai.gpt-6.1-sol", "us.anthropic.claude-opus-5-5", "us.zai.glm-5.3"
STICKY_OPUS = {**STICKY_SOL, "generation_profile": "opus-deep", "effort": "high", "max_output_tokens": 4000}
GEN_FAULTS = (
    None,
    {"kind": "http_5xx", "status": 503},
    {"kind": "hang_before_first_byte"},
    {"kind": "stall_after", "n": 1},
    {"kind": "exception_event", "type": "modelStreamErrorException", "after": 1},
    {"kind": "reasoning_only"},
    {"kind": "empty_text"},
    {"kind": "throttling"},
)


@st.composite
def turns(draw: st.DrawFn) -> dict[str, Any]:
    gen_rules: list[dict[str, Any]] = []
    for model in (SOL, OPUS, GLM):
        first_event = draw(st.integers(0, 600))
        first_text = first_event + draw(st.integers(0, 2500))
        n = draw(st.integers(1, 4))
        fault = draw(st.sampled_from(GEN_FAULTS))
        rule = stream(
            model,
            first_event_ms=first_event,
            first_text_ms=first_text,
            chunks=[f"t{i} " for i in range(n)],
            step_ms=draw(st.sampled_from((50, 100, 300))),
            fault=fault,
        )
        if fault is not None and draw(st.booleans()):
            rule["match"]["nth"] = 1  # the fault hits the first request only
        gen_rules.append(rule)
    decision = router_rules(
        intent=draw(st.sampled_from((FACTUAL, CODING))),
        level=draw(st.sampled_from((MODERATE, HARD))),
        haiku_silent=draw(st.booleans()),
        all_down=draw(st.integers(0, 5)) == 0,
    )
    body: dict[str, Any] = {**USER_BODY, "session_id": "ses_1"}
    if draw(st.integers(0, 4)) == 0:
        body["strategy"] = "gate"
    if draw(st.integers(0, 4)) == 0:
        body["generation"] = {"profile": draw(st.sampled_from(("race-sol-glm", "glm-reason", "opus-deep")))}
    if draw(st.integers(0, 3)) == 0:
        body["store"] = False
    return {
        "scenario": document("p-sse-1", gen_rules, decision),
        "sticky": draw(st.sampled_from((STICKY_SOL, STICKY_OPUS, None))),
        "body": body,
        "cancel_ms": draw(st.one_of(st.none(), st.integers(0, 4000))),
        "deadline_s": draw(st.sampled_from((120.0, 120.0, 3.0, 1.2))),
        "hold": False,
        # entry latencies: generation (credentials refresh: S1 may come first) and decision (view resolution)
        "gen_entry_ms": draw(st.sampled_from((0, 0, 30, 400))),
        "decision_entry_ms": draw(st.sampled_from((0, 0, 200))),
    }


async def _run(case: dict[str, Any]) -> tuple[list[dict[str, Any]], Any, Any]:
    h = make_harness(scenario=case["scenario"])
    h.seed_session("ses_1", last_seq=2, sticky_spec=case["sticky"])
    if case["gen_entry_ms"]:
        h.override("generation", SlowEntry(h.world.generation, h.clock, case["gen_entry_ms"] / 1000))
    if case["decision_entry_ms"]:
        h.override("decisions", SlowEntry(h.world.decisions, h.clock, case["decision_entry_ms"] / 1000))
    frames: list[bytes] = []

    async def during(runner: Any) -> None:
        async def consume() -> None:
            frames.extend([f.wire async for f in h.world.events.subscribe(runner.turn_id)])

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            if case["cancel_ms"] is not None:
                await h.clock.sleep(case["cancel_ms"] / 1000)
                await h.service.cancel(principal(), runner.turn_id)

    async with running(h):
        runner = await run_turn(h, case["body"], deadline_s=case["deadline_s"], during=during)
    assert_frames_valid(b"".join(frames), [e["type"] for e in h.events(runner.turn_id)])
    return h.events(runner.turn_id), runner, h


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(turns())
def test_p_sse_1_invariants_hold_under_random_timings_and_faults(case: dict[str, Any]) -> None:
    events, runner, h = anyio.run(_run, case)
    types = [e["type"] for e in events]
    # a turn that never reached t_d0 (preflight or the decision entry ended it) owes no decision.completed
    problems = sse_invariants(events, reached_t_d0=runner.t_d0 is not None)
    assert problems == [], problems
    spec_changed = min(
        1, sum(1 for e in events if e["type"] == "generation.cancelled" and e["reason"] == "spec_changed")
    )
    failed = sum(1 for e in events if e["type"] == "generation.cancelled" and e["reason"] == "failed")
    race_width = 2 if (case["body"].get("generation") or {}).get("profile") == "race-sol-glm" else 1
    assert types.count("generation.started") <= 1 + spec_changed + min(failed, 2) + (race_width - 1) * (
        1 + spec_changed
    )
    assert types[-1] == "turn.completed" and types.count("turn.completed") == 1
    status = runner.result.status
    if "generation.completed" in types and runner.cancel_status is None and "error" not in types:
        assert status == "completed"
    if runner.cancel_status is not None:
        assert status == "cancelled" and "turn.verified" not in types
    if "error" in types:
        assert "turn.verified" not in types
    stored = [r for r in h.world.db.store.rows("turn") if r["id"] == runner.turn_id]
    if case["body"].get("store") is False:
        assert stored == []
    else:
        assert len(stored) == 1 and stored[0]["status"] == status
    assert h.world.admission.live_streams("t_acme", "p1") == 0  # the reservation was released
