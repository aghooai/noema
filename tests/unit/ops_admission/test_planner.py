"""`DefaultWorkGraphPlanner` (spec-11 §1.1): the T-WG-1…5 vectors with exact counts per alias and kind, the token
formulas, and the generation worst case (IMPL-016, IMPL-084)."""

from __future__ import annotations

from typing import Any, Literal

import pytest
from tests.contract_internal.kits import principal
from tests.fakes.config import example_snapshot, patched_example, snapshot_from

from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.ops import DecisionWork, GenerationWork, QuestionWork, WorkGraph
from noema.ops.admission.planner import DefaultWorkGraphPlanner, decision_max_tokens, generation_out

pytestmark = pytest.mark.unit
PLANNER = DefaultWorkGraphPlanner()
ROUTER_QS = (
    QuestionWork("intent", 10, "platform"),
    QuestionWork("complexity", 4, "platform"),
    QuestionWork("needs_fresh_info", 2, "platform"),
    QuestionWork("sensitive_data", 2, "platform"),
    QuestionWork("injection_suspected", 2, "platform"),
)
VERIFY_QS = (QuestionWork("answers_question", 2, "platform"), QuestionWork("unsupported_claims", 2, "platform"))


def qs(*ks: int, source: Literal["platform", "request"] = "request") -> tuple[QuestionWork, ...]:
    return tuple(QuestionWork(f"q{i + 1}", k, source) for i, k in enumerate(ks))


def router(deadline_ms: int = 1000, budget: int | None = 1050, input_tokens: int = 200) -> DecisionWork:
    return DecisionWork(
        "router-default", ROUTER_QS, deadline_ms, "profile_default", budget, "decision_member", input_tokens
    )


def verifier(input_tokens: int = 300) -> DecisionWork:
    return DecisionWork(
        "answer-verifier", VERIFY_QS, 4000, "profile_default", None, "verification_member", input_tokens
    )


def plan(
    *decisions: DecisionWork, generation: GenerationWork | None = None, snap: ConfigSnapshot | None = None
) -> WorkGraph:
    return PLANNER.plan(snap or example_snapshot(), principal(), decisions=list(decisions), generation=generation)


def patched(patch: dict[str, Any]) -> ConfigSnapshot:
    return snapshot_from(patched_example(patch))


def test_twg1_router_only_exact_calls_and_tokens() -> None:
    graph = plan(router())
    assert dict(graph.calls) == {
        ("deepseek-3.2", "decision_member"): 10,  # 5 questions x (primary + backup: 400 + 590 <= 1000)
        ("luna-6", "decision_member"): 5,
        ("haiku-5.5", "decision_member"): 5,
    }
    assert (graph.total_calls, graph.streams) == (20, 0)
    verbalized_out = sum(64 + 12 * k for k in (10, 4, 2, 2, 2))  # luna effort none: R = 0
    haiku_out = verbalized_out + 5 * 512  # haiku reasons (Wave E2, CR #26): R = EFFORT_RESERVE["low"] per call
    assert dict(graph.tokens_by_model) == {
        "deepseek-3.2": 10 * (200 + 600 + 8),
        "luna-6": 5 * 800 + verbalized_out,
        "haiku-5.5": 5 * 800 + haiku_out,
    }
    assert graph.tokens_in == 20 * 800 and graph.tokens_out == 10 * 8 + verbalized_out + haiku_out


def test_twg1_tight_deadline_drops_the_backup() -> None:
    graph = plan(router(900, 950))
    assert graph.total_calls == 15 and graph.calls[("deepseek-3.2", "decision_member")] == 5


def test_twg2_full_default_turn() -> None:
    gen = GenerationWork(("sol-fast", "opus-deep", "glm-reason"), 200, None)
    graph = plan(router(), verifier(), generation=gen)
    assert dict(graph.calls) == {
        ("deepseek-3.2", "decision_member"): 10,
        ("luna-6", "decision_member"): 5,
        ("haiku-5.5", "decision_member"): 5,
        ("haiku-5.5", "verification_member"): 2,
        ("luna-6", "verification_member"): 2,
        ("sol-6.1", "generation"): 4,
        ("opus-5.5", "generation"): 4,
        ("glm-5.3", "generation"): 4,
    }
    assert (graph.total_calls, graph.streams) == (28, 1)
    # out(m) = max(4096 default, binding pins 4000 / 600) + reserve 0; GLM's cap is 4096 too
    assert graph.tokens_by_model["sol-6.1"] == 4 * (200 + 4096)
    assert graph.tokens_by_model["glm-5.3"] == 4 * (200 + 4096)
    decision_in = 20 * 800 + 4 * 900
    assert graph.tokens_in == decision_in + 4 * 200


def test_twg3_decisions_escalate_through_two_stages() -> None:
    work = DecisionWork(
        "lightweight-model-decide", qs(2, 3, 4), 1000, "profile_default", 120_000, "decision_member", 50
    )
    graph = plan(work)
    assert dict(graph.calls) == {
        ("luna-6", "decision_member"): 3 + 3 + 3,  # stage 0, router-default, router-plus
        ("deepseek-3.2", "decision_member"): 6 + 6,  # backups: 400 + 590 <= 1000, 900 + 590 <= 2500
        ("haiku-5.5", "decision_member"): 3 + 3,
    }
    assert graph.total_calls == 27
    off = DecisionWork("lightweight-model-decide", qs(2, 3, 4), 1000, "off", 120_000, "decision_member", 50)
    assert plan(off).total_calls == 3


def test_twg4_turn_budget_excludes_router_plus() -> None:
    assert plan(router(1000, 1050)).total_calls == 20
    assert plan(router(1000, 2500)).total_calls == 24  # the budget covers router-plus: intent only (IMPL-068)
    assert plan(router(1000, None)).total_calls == 20  # no budget: no escalation


def test_twg5_hierarchical_reserves_one_plus_three_calls_at_the_template_k() -> None:
    work = DecisionWork("lightweight-model-decide", qs(32), 3000, "profile_default", 120_000, "decision_member", 50)
    graph = plan(work)  # K 32 > L 26: hierarchical; router-default (L 16) cannot carry it flat
    assert dict(graph.calls) == {("luna-6", "decision_member"): 4}
    assert graph.tokens_by_model["luna-6"] == 4 * (50 + 600 + 64 + 12 * 26)  # costed at K = max_choices 26
    wide = DecisionWork("router-default", qs(40), 3000, "profile_default", 120_000, "decision_member", 50)
    graph = plan(wide)
    assert dict(graph.calls) == {
        ("deepseek-3.2", "decision_member"): 8,  # 4 x 2 (backup: 400 + 590 <= 3000)
        ("luna-6", "decision_member"): 4,
        ("haiku-5.5", "decision_member"): 4,
    }
    assert graph.tokens_by_model["deepseek-3.2"] == 8 * (50 + 600 + 8)


def test_escalation_carries_only_listed_platform_questions_and_every_request_question() -> None:
    mixed = (QuestionWork("intent", 10, "platform"), QuestionWork("complexity", 4, "platform"), *qs(3))
    graph = plan(DecisionWork("router-default", mixed, 1000, "profile_default", 3000, "decision_member", 0))
    # stage 0: 3 questions x 4 calls; router-plus: intent + q1 x 4 calls
    assert graph.total_calls == 12 + 8


def test_a_stage_with_no_eligible_question_ends_the_chain() -> None:
    snap = patched({"decision_profiles": {"router-default": {"escalation": {"questions": ["complexity"]}}}})
    assert plan(router(1000, 3000), snap=snap).total_calls == 20 + 4  # complexity escalates instead of intent
    no_intent = DecisionWork("router-default", ROUTER_QS[1:], 1000, "profile_default", 3000, "decision_member", 0)
    assert plan(no_intent).total_calls == 16  # router-plus would carry intent only: no stage 1


def test_race_width_and_default_profile_always_included() -> None:
    graph = plan(generation=GenerationWork(("race-sol-glm",), 100, 600))
    assert dict(graph.calls) == {("sol-6.1", "generation"): 8, ("glm-5.3", "generation"): 8}
    assert graph.total_calls == 8 and graph.streams == 1  # 4 waves x width 2
    pinned = plan(generation=GenerationWork(("opus-deep",), 100, None))  # default sol-fast joins the candidates
    assert dict(pinned.calls) == {("opus-5.5", "generation"): 4, ("sol-6.1", "generation"): 4}
    assert pinned.total_calls == 4


def test_generation_out_formula_impl084() -> None:
    snap = patched({"models": {"sol-6.1": {"reasoning_reserve_tokens": 512}}})
    assert generation_out(snap, "sol-6.1", None, []) == 4096 + 512  # no cap: GEN_DEFAULT_MAX_TOKENS + reserve
    assert generation_out(snap, "sol-6.1", 1000, [8000, 600]) == 8000 + 512  # the largest pin wins
    assert generation_out(snap, "sol-6.1", 1000, []) == 1000 + 512  # the member's own max
    assert generation_out(snap, "glm-5.3", None, [8000]) == 4096  # min(cap, 8000)
    assert generation_out(snap, "glm-5.3", None, []) == 4096  # no member max: the cap
    graph = plan(generation=GenerationWork(("glm-reason",), 10, 9000), snap=snap)
    assert graph.tokens_by_model["glm-5.3"] == 4 * (10 + 4096)
    assert graph.tokens_by_model["sol-6.1"] == 4 * (10 + 9000 + 512)
    assert graph.tokens_out == 4 * (9000 + 512)  # 4 waves x the worst out(m)


def test_decision_max_tokens_reasoning_reserve_and_cap() -> None:
    snap = example_snapshot()
    deepseek, luna, haiku = snap.decision_profile("router-default").members
    assert decision_max_tokens(snap, deepseek, 16) == 8
    assert decision_max_tokens(snap, luna, 4) == 64 + 48
    plus_luna = snap.decision_profile("router-plus").members[1]  # luna-6 at effort medium: R = 1024
    assert decision_max_tokens(snap, plus_luna, 4) == 64 + 48 + 1024
    capped = patched({"models": {"haiku-5.5": {"max_output_tokens_cap": 100}}})
    assert decision_max_tokens(capped, haiku, 10) == 100


def test_plan_is_pure_and_frozen() -> None:
    gen = GenerationWork(("sol-fast",), 10, None)
    first = plan(router(), generation=gen)
    assert plan(router(), generation=gen) == first
    with pytest.raises(TypeError):
        first.calls[("sol-6.1", "generation")] = 0  # type: ignore[index]
    assert plan().total_calls == 0 and plan().streams == 0
