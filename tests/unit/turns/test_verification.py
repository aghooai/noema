"""Verification (spec-04 §8; IMPL-024; R5 machinery): when it runs, its input, its deadline and its persistence."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from tests.golden.sse_scenarios import document, router_rules, stream, verifier_rules

from noema.core.protocols.decision import VerificationInput

from .harness import STICKY_SOL, USER_BODY, first, make_harness, run_turn, running, types_of

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"
HIGH_INJECTION = {"true": 0.6, "false": 0.4}


def _h(*, fault: dict[str, Any] | None = None, injection: dict[str, float] | None = None):  # type: ignore[no-untyped-def]
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=["The answer. "], fault=fault)
    decision = router_rules(injection=injection) if injection is not None else router_rules()
    h = make_harness(scenario=document("v", [sol], verifier_rules(), decision))
    h.seed_session("ses_1", last_seq=1, sticky_spec=STICKY_SOL)
    return h


async def test_verify_true_forces_verification_with_the_final_message_and_the_answer() -> None:
    h = _h()
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    body = {
        **USER_BODY,
        "session_id": "ses_1",
        "verify": True,
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "earlier item"}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Is it right?"},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{png}"},
                ],
            },
        ],
    }
    async with running(h):
        runner = await run_turn(h, body)
    types = types_of(h, runner)
    assert types[-2:] == ["turn.verified", "turn.completed"]
    assert types.index("generation.completed") < types.index("decision.updated") < types.index("turn.verified")
    _, verified = first(h, runner, "turn.verified")
    assert [a["name"] for a in verified["verification"]["answers"]] == ["answers_question", "unsupported_claims"]
    _router, verifier = h.world.decisions.runs
    vinput = verifier._req.input
    assert isinstance(vinput, VerificationInput)
    assert vinput.question == "Is it right?\n[image-1: image]" and vinput.answers == (("A", "The answer. "),)
    assert verifier._ctx.purpose == "verification" and verifier._ctx.escalate_until is None
    assert verifier._ctx.deadline.at <= runner.deadline.at
    turn = h.world.db.store.rows("turn")[0]
    assert turn["verification_decision_id"] == verified["verification"]["id"]
    assert {r["purpose"] for r in h.world.db.store.rows("decision")} == {"router", "verification"}


async def test_binding_verify_answer_holds_against_verify_false() -> None:
    h = _h(injection=HIGH_INJECTION)
    async with running(h):
        runner = await run_turn(h, {**USER_BODY, "session_id": "ses_1", "verify": False})
    assert "turn.verified" in types_of(h, runner)
    assert "bindings[4]" in h.world.db.store.rows("turn")[0]["routing"]["matched_bindings"]


async def test_no_verification_after_an_error_or_without_a_trigger() -> None:
    h = _h(fault={"kind": "stall_after", "n": 1}, injection=HIGH_INJECTION)
    async with running(h):
        runner = await run_turn(h)
    assert "turn.verified" not in types_of(h, runner) and len(h.world.decisions.runs) == 1
    h2 = _h()
    async with running(h2):
        runner2 = await run_turn(h2)
    assert "turn.verified" not in types_of(h2, runner2)


async def test_store_false_verifies_and_persists_nothing() -> None:
    h = _h()
    async with running(h):
        runner = await run_turn(h, {**USER_BODY, "session_id": "ses_1", "verify": True, "store": False})
    assert "turn.verified" in types_of(h, runner)
    assert h.world.db.store.rows("decision") == [] and h.world.db.store.rows("turn") == []
