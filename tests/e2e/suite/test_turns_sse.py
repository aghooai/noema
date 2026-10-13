"""Turns over SSE on the compose stack, part 1: the commit rule (spec-04 §4.2 G01-G06 through the real adapters, both
engines and the wire mock).

Each test loads a scenario ported from the golden fixture of the same id (tests/golden/sse) with real-time margins
(tests/e2e/suite/scenarios.py) and asserts the event order (P-SSE-1), the commit and cancel reasons, the text, the
turn as stored (read on the other replica) and what the mock received. Every frame and response is validated.
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import data, deltas_text, first, kinds, mock_calls, mock_seq, tag
from tests.e2e.suite.turns import opus_session, run_turn, started, stored_turn

pytestmark = [pytest.mark.e2e]


async def test_g01_agree(stack: Stack, mock: Mock) -> None:
    """Sol's first text at ~300 ms is held; S1 at ~500 ms agrees: commit spec_agrees at S1, no restart."""
    sol = sc.stream(sc.SOL, first_text_ms=300, step_ms=150)
    await mock.load(sc.document("e2e-G01", [sol], sc.router()))
    before = await mock_seq(mock)
    turn = await run_turn(stack.client(0), f"What is the capital of France? ({tag()})")
    events = turn.events
    att = started(events)[0]
    assert (att["route_id"], att["model"], att["speculative"]) == ("sol-fast", sc.SOL, True)
    assert first(events, "decision.completed")["arrived"] == "during_holdback"
    confirmed = first(events, "route.confirmed")["routing"]
    assert (confirmed["selection"], confirmed["matched_bindings"], confirmed["served_spec_differs"]) == (
        "rule",
        ["bindings[0]"],
        False,
    )
    assert first(events, "generation.committed")["reason"] == "spec_agrees"
    assert "generation.cancelled" not in kinds(events) and "decision.updated" in kinds(events)
    assert deltas_text(events) == sc.TEXT and first(events, "generation.completed")["finish_reason"] == "stop"
    stored = await stored_turn(stack, turn.turn_id)
    assert (
        stored["text"] == sc.TEXT
        and stored["routing"]["decision_id"] == first(events, "decision.completed")["decision"]["id"]
    )
    assert [a["outcome"] for a in stored["attempts"]] == ["committed"]
    ops = [c["op"] for c in await mock_calls(mock, before)]
    assert ops.count("converse-stream") == 1 and {"converse", "chat"} <= set(ops)  # Converse + Mantle members


async def test_g02_disagree_then_invisible_restart(stack: Stack, mock: Mock) -> None:
    """intent coding + hard decides opus-deep (high, 4000): A1 (sol-fast) is cancelled spec_changed before any of
    its text reached the client; A2 runs on Opus and commits. The session's sticky spec becomes opus-deep."""
    session_id, events = await opus_session(stack, mock)
    a1, a2 = started(events)
    assert (a1["route_id"], a1["speculative"]) == ("sol-fast", True)
    assert (a2["route_id"], a2["model"], a2["speculative"]) == ("opus-deep", sc.OPUS, False)
    cancelled = first(events, "generation.cancelled")
    assert (cancelled["attempt_id"], cancelled["reason"]) == (a1["attempt_id"], "spec_changed")
    committed = first(events, "generation.committed")
    assert (committed["attempt_id"], committed["reason"]) == (a2["attempt_id"], "spec_agrees")
    assert deltas_text(events, a1["attempt_id"]) == "" and deltas_text(events) == sc.TEXT  # invisible restart
    routing = first(events, "route.confirmed")["routing"]
    assert routing["params"] == {"effort": "high", "max_output_tokens": 4000}
    assert first(events, "turn.completed")["timings"]["spec_restarts"] == 1
    stored = await stored_turn(stack, data(events[0])["turn_id"])
    assert [(a["attempt_id"], a["outcome"]) for a in stored["attempts"]] == [
        (a1["attempt_id"], "cancelled"),
        (a2["attempt_id"], "committed"),
    ]
    session = (await stack.client(1).request("GET", f"/v1/sessions/{session_id}", expect=200)).json()
    assert session["last_seq"] == 1


@pytest.mark.parametrize("decided", ["agrees", "differs"])
async def test_g03_holdback_deadline_then_after_commit(stack: Stack, mock: Mock, decided: str) -> None:
    """Sol's first text at ~100 ms: the 400 ms hold cap commits spec0 (holdback_deadline) before S1 (~850 ms)
    arrives after_commit. G03b: a decided spec that differs restarts nothing (served_spec_differs: true)."""
    intent, level = (sc.FACTUAL, sc.MODERATE) if decided == "agrees" else (sc.CODING, sc.HARD)
    sol = sc.stream(sc.SOL, first_text_ms=100, step_ms=150)
    await mock.load(sc.document("e2e-G03", [sol], sc.router(intent=intent, level=level, ms=850)))
    turn = await run_turn(stack.client(0), f"What is the capital of France? ({tag()})")
    events = turn.events
    order = kinds(events)
    assert order.index("generation.committed") < order.index("decision.completed"), turn.timeline()
    assert first(events, "generation.committed")["reason"] == "holdback_deadline"
    held = first(events, "route.confirmed")["routing"]
    assert (held["selection"], held["decision_id"], held["reason"]) == ("default", None, "holdback_deadline")
    assert first(events, "decision.completed")["arrived"] == "after_commit"
    assert len(started(events)) == 1 and deltas_text(events) == sc.TEXT
    stored = await stored_turn(stack, turn.turn_id)
    assert stored["routing"]["served_spec_differs"] is (decided == "differs")
    assert stored["routing"]["decision_id"] == first(events, "decision.completed")["decision"]["id"]


async def test_g04_reasoning_route_commits_at_its_first_text(stack: Stack, mock: Mock) -> None:
    """Sticky opus-deep; Haiku silent: DeepSeek + Luna decide by the deadline while Opus only reasons; the commit
    waits for Opus's first text (~1.8 s), so the decision leads it and nothing is held back."""
    session_id, _ = await opus_session(stack, mock)
    opus = sc.stream(sc.OPUS, first_text_ms=1800, reasoning=60)
    router = sc.router(intent=sc.CODING, level=sc.HARD, haiku_silent=True)
    await mock.load(sc.document("e2e-G04", [opus], router))
    turn = await run_turn(stack.client(1), f"Refactor this function. ({tag()})", session_id=session_id)
    events = turn.events
    (att,) = started(events)
    assert (att["route_id"], att["model"]) == ("opus-deep", sc.OPUS)
    assert first(events, "route.selected")["routing"]["selection"] == "sticky"
    order = kinds(events)
    assert order.index("decision.completed") < order.index("generation.committed"), turn.timeline()
    assert first(events, "generation.committed")["reason"] == "spec_agrees"
    timings = first(events, "turn.completed")["timings"]
    assert timings["decision_lead_ms"] > 0 and timings["holdback_ms"] == 0 and timings["spec_restarts"] == 0
    assert deltas_text(events) == sc.TEXT


async def test_g05_pre_commit_failure_falls_back_within_the_profile(stack: Stack, mock: Mock) -> None:
    """Sticky opus-deep; Opus answers 503 before any byte: A1 fails, opus-deep's fallback member Sol (A2) streams
    and commits spec_agrees. Nothing failed reached the client."""
    session_id, _ = await opus_session(stack, mock)
    opus_down = sc.fault({"model": sc.OPUS, "op": "converse-stream"}, "http_5xx", 150, status=503)
    sol = sc.stream(sc.SOL, first_text_ms=900)
    await mock.load(sc.document("e2e-G05", [opus_down, sol], sc.router(intent=sc.CODING, level=sc.HARD)))
    turn = await run_turn(stack.client(0), f"Write a parser. ({tag()})", session_id=session_id)
    events = turn.events
    a1, a2 = started(events)
    assert (a1["model"], a2["model"], a2["route_id"], a2["speculative"]) == (sc.OPUS, sc.SOL, "opus-deep", True)
    cancelled = first(events, "generation.cancelled")
    assert (cancelled["attempt_id"], cancelled["reason"]) == (a1["attempt_id"], "failed")
    committed = first(events, "generation.committed")
    assert (committed["attempt_id"], committed["reason"]) == (a2["attempt_id"], "spec_agrees")
    assert deltas_text(events) == deltas_text(events, a2["attempt_id"]) == sc.TEXT
    stored = await stored_turn(stack, turn.turn_id)
    assert [a["outcome"] for a in stored["attempts"]] == ["failed", "committed"]


async def test_g06_race_commits_the_first_text_and_cancels_the_loser(stack: Stack, mock: Mock) -> None:
    """Pinned race-sol-glm: both members start; Sol's text (~300 ms) wins, GLM is cancelled lost_race."""
    sol, glm = sc.stream(sc.SOL, first_text_ms=300, step_ms=150), sc.stream(sc.GLM, first_text_ms=900)
    await mock.load(sc.document("e2e-G06", [sol, glm], sc.router()))
    body_extra = {"generation": {"profile": "race-sol-glm"}}
    turn = await run_turn(stack.client(1), f"What is the capital of France? ({tag()})", **body_extra)
    events = turn.events
    a1, a2 = started(events)
    assert {(a1["model"], a1["route_id"]), (a2["model"], a2["route_id"])} == {
        (sc.SOL, "race-sol-glm"),
        (sc.GLM, "race-sol-glm"),
    }
    sol_att = a1 if a1["model"] == sc.SOL else a2
    glm_att = a2 if sol_att is a1 else a1
    assert first(events, "route.selected")["routing"]["selection"] == "pinned"
    committed = first(events, "generation.committed")
    assert (committed["attempt_id"], committed["reason"]) == (sol_att["attempt_id"], "spec_agrees")
    cancelled = first(events, "generation.cancelled")
    assert (cancelled["attempt_id"], cancelled["reason"]) == (glm_att["attempt_id"], "lost_race")
    assert deltas_text(events) == deltas_text(events, sol_att["attempt_id"]) == sc.TEXT
