"""Turns over SSE on the compose stack, part 2: cancel, verification, outages, errors after commit, store: false
(spec-04 §4.2 G07-G11). G12 (idempotent attach) is in test_turns_attach.py."""

from __future__ import annotations

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import (
    data,
    deltas_text,
    first,
    in_order,
    kinds,
    live_turn,
    mock_calls,
    mock_seq,
    tag,
    turn_body,
)
from tests.e2e.suite.turns import invariants, new_session, run_turn, started, stored_turn

pytestmark = [pytest.mark.e2e]


async def test_g07_explicit_cancel_from_the_other_replica(stack: Stack, mock: Mock) -> None:
    """The turn streams on api-1; cancelTurn on api-2 reaches its owner pod (turn_owner + pub/sub): the stream ends
    with generation.cancelled{client_abort} and turn.completed, never generation.completed."""
    await mock.load(sc.document("e2e-G07", [sc.stream(sc.SOL, first_text_ms=300, step_ms=400)], sc.router()))
    async with live_turn(stack.client(0), turn_body(f"Tell me about Paris. ({tag()})")) as turn:
        await turn.until(lambda e: e.event == "generation.delta")
        cancel = await stack.client(1).request("POST", f"/v1/turns/{turn.turn_id}/cancel", expect=202)
        assert cancel.json() == {"turn_id": turn.turn_id, "status": "cancelling"}
        events = await turn.rest()
    invariants(events)
    order = kinds(events)
    assert "generation.completed" not in order and order[-1] == "turn.completed", turn.timeline()
    cancelled = first(events, "generation.cancelled")
    assert (cancelled["attempt_id"], cancelled["reason"]) == (started(events)[0]["attempt_id"], "client_abort")
    partial = deltas_text(events)
    assert partial and sc.TEXT.startswith(partial) and partial != sc.TEXT
    again = await stack.client(0).request("POST", f"/v1/turns/{turn.turn_id}/cancel", expect=202)
    assert again.json() == {"turn_id": turn.turn_id, "status": "cancelled"}
    stored = await stored_turn(stack, turn.turn_id)
    assert stored["text"] == partial and [a["outcome"] for a in stored["attempts"]] == ["committed"]


async def test_g08_verification_after_an_injection_suspicion(stack: Stack, mock: Mock) -> None:
    """injection_suspected 0.35 matches bindings[4] (verify_answer): turn.verified (answer-verifier on Haiku and
    Luna) follows generation.completed; the stored turn carries the verification."""
    router = sc.router(injection={"true": 0.35, "false": 0.65})
    await mock.load(sc.document("e2e-G08", [sc.stream(sc.SOL, first_text_ms=300)], sc.verifier(), router))
    turn = await run_turn(stack.client(0), f"What is the capital of France? ({tag()})")
    events = turn.events
    assert in_order(events, "generation.completed", "turn.verified", "turn.completed"), turn.timeline()
    # injection_suspected is non-blocking: it is in S1 (route.confirmed) or only in the final snapshot, whose
    # tightening keys (verify_answer) still apply; the stored routing has S1 and the final bindings either way
    assert first(events, "route.confirmed")["routing"]["matched_bindings"][0] == "bindings[0]"
    verification = first(events, "turn.verified")["verification"]
    answers = {a["name"]: a for a in verification["answers"]}
    assert set(answers) == {"answers_question", "unsupported_claims"}
    assert answers["answers_question"]["candidates"][0]["value"] is True
    assert answers["unsupported_claims"]["candidates"][0]["value"] is False
    stored = await stored_turn(stack, turn.turn_id)
    assert stored["verification"] is not None and stored["verification"]["id"] == verification["id"]
    assert stored["routing"]["matched_bindings"] == ["bindings[0]", "bindings[4]"]


async def test_g09_all_decision_members_down(stack: Stack, mock: Mock) -> None:
    """Every decision call answers 503: the decision is unavailable before generation starts; spec0 (sol-fast) is
    still the decided spec, so the turn commits at its first text and completes."""
    sol = sc.stream(sc.SOL, first_event_ms=500, first_text_ms=800)
    await mock.load(sc.document("e2e-G09", [sol, sc.fault({}, "http_5xx", 50, status=503)]))
    before = await mock_seq(mock)
    turn = await run_turn(stack.client(1), f"What is the capital of France? ({tag()})")
    events = turn.events
    decision = first(events, "decision.completed")
    assert decision["arrived"] == "before_generation", turn.timeline()
    updates = [data(e)["decision"] for e in events if e.event == "decision.updated"]  # none when S1 was final
    for snapshot in [decision["decision"], *updates]:
        assert {a["type"] for a in snapshot["answers"]} == {"unavailable"}
    assert first(events, "generation.committed")["reason"] == "spec_agrees"
    assert deltas_text(events) == sc.TEXT
    failed = [c for c in await mock_calls(mock, before) if c["op"] in ("converse", "chat")]
    assert any(c["status"] == 503 for c in failed)  # a call the engine gave up on first is `disconnected`
    assert {c["outcome"] for c in failed} <= {"fault", "disconnected"}


async def test_g10_error_after_commit_stream_exception(stack: Stack, mock: Mock) -> None:
    """A modelStreamErrorException after 4 committed chunks: the turn ends with error{all_routes_unavailable,
    retryable}; the 4 deltas stay the only text, no generation.completed."""
    broken = {"kind": "exception_event", "type": "modelStreamErrorException", "after": 4}
    await mock.load(sc.document("e2e-G10b", [sc.stream(sc.SOL, first_text_ms=300, fault=broken)], sc.router()))
    turn = await run_turn(stack.client(0), f"What is the capital of France? ({tag()})")
    events = turn.events
    assert in_order(events, "generation.committed", "error", "turn.completed"), turn.timeline()
    assert "generation.completed" not in kinds(events)
    error = first(events, "error")
    assert (error["code"], error["retryable"]) == ("all_routes_unavailable", True)
    assert deltas_text(events) == "".join(sc.CHUNKS[:4])


@pytest.mark.slow
async def test_g10_error_after_commit_stall(stack: Stack, mock: Mock) -> None:
    """The stream stalls after 4 chunks: the generation idle timer (30 s) ends the turn with upstream_stalled."""
    stalled = {"kind": "stall_after", "n": 4}
    await mock.load(sc.document("e2e-G10", [sc.stream(sc.SOL, first_text_ms=300, fault=stalled)], sc.router()))
    with anyio.fail_after(55):
        turn = await run_turn(stack.client(0), f"What is the capital of France? ({tag()})")
    error = first(turn.events, "error")
    assert error["code"] == "upstream_stalled", turn.timeline()
    assert deltas_text(turn.events) == "".join(sc.CHUNKS[:4])


async def test_g11_store_false(stack: Stack, mock: Mock) -> None:
    """store: false in a session: the same stream, but nothing is stored (getTurn 404 on both replicas) and the
    session's last_seq does not move."""
    session_id = await new_session(stack.client(0))
    await mock.load(sc.document("e2e-G11", [sc.stream(sc.SOL, first_text_ms=300)], sc.router()))
    turn = await run_turn(
        stack.client(0), f"What is the capital of France? ({tag()})", session_id=session_id, store=False
    )
    created = data(turn.events[0])
    assert created["session_id"] == session_id
    assert deltas_text(turn.events) == sc.TEXT and kinds(turn.events)[-1] == "turn.completed"
    await anyio.sleep(1.0)  # past a write-behind finalize, were there one
    for replica in (0, 1):
        await stack.client(replica).request("GET", f"/v1/turns/{turn.turn_id}", expect=404)
    session = (await stack.client(1).request("GET", f"/v1/sessions/{session_id}", expect=200)).json()
    assert session["last_seq"] == 0
