"""Wave E2, CR #26 (live L7: verification `no_quorum`): live Haiku 5.5 reasons at effort low and Bedrock hides it;
on the verify-dist@1 budget of 88 tokens it spent all 88 on reasoning and returned no text (`reasoning_only`), so
answer-verifier (2 of 2) never reached quorum. With `models.haiku-5.5.controls.reasoning: true` its decision calls
carry the effort reserve (spec-06 §5.5: 64 + 12K + 512 at low), and the same hidden reasoning leaves room for the
answer. Real engine on the fake world; the scenario plans Haiku's hidden reasoning (`reasoning.hidden_tokens`)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.decision.world import HAIKU, LUNA, EngineWorld, answer, engine_world, res

from noema.core.protocols.decision import DecisionRequest, ProfileRef, VerificationInput

pytestmark = [pytest.mark.unit]

TRUE = {"true": 0.9, "false": 0.1}
HIDDEN = {"reasoning": {"hidden_tokens": 200}}  # live: >= 88 (the whole budget), prompt-dependent
QUESTIONS = ["answers_question", "unsupported_claims"]


async def verify(ew: EngineWorld) -> Any:
    ew.use([answer(HAIKU, None, TRUE, 300, stream=HIDDEN), answer(LUNA, None, TRUE, 300)])
    verifier = ew.snapshot.decision_profile("answer-verifier")
    qs = ew.compiler.compile(verifier, [], origin="verification", output_format="native", select=QUESTIONS)
    req = DecisionRequest(
        ProfileRef("answer-verifier", verifier.version),
        qs,
        VerificationInput("Where is my parcel?", (("A", "It ships tomorrow."),)),
        verifier.deadline_ms,
        verifier.min_quorum,
        "off",
        frozenset({"members"}),
        "verification",
        "native",
    )
    async with ew.clock.autojump():
        return await ew.engine.decide(ew.context(purpose="verification", escalate=False, turn_id="turn_1"), req)


async def test_the_verifier_reaches_quorum_when_haiku_hides_its_reasoning() -> None:
    ew = engine_world()
    snap = await verify(ew)
    answers = res(snap)["answers"]
    assert [(a["name"], a["type"]) for a in answers] == [(q, "predicate") for q in QUESTIONS], answers
    haiku = [c.request for c in ew.world.adapters.calls() if c.model_id == HAIKU]
    assert len(haiku) == 2 and {r.max_tokens for r in haiku} == {64 + 12 * 2 + 512}


async def test_without_the_reserve_the_hidden_reasoning_takes_the_whole_budget() -> None:
    """The pre-fix config (Haiku `reasoning: false`, 88 tokens) reproduces live L7: no quorum on either question."""
    ew = engine_world(patch={"models": {"haiku-5.5": {"controls": {"reasoning": False}}}})
    snap = await verify(ew)
    answers = res(snap)["answers"]
    assert {(a["type"], a["reason"]) for a in answers} == {("unavailable", "no_quorum")}, answers
