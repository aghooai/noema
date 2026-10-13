"""WorkGraphPlanner conformance: the spec-11 §1.1 vectors T-WG-1…5 (exact call counts per alias and kind)."""

from __future__ import annotations

from typing import Literal

from noema.core.protocols.ops import DecisionWork, GenerationWork, QuestionWork, WorkGraph

from .kits import PlannerKit, principal
from .registry import kit_fixture

kit = kit_fixture("work_graph")
ROUTER = (
    QuestionWork("intent", 10, "platform"),
    QuestionWork("complexity", 4, "platform"),
    QuestionWork("needs_fresh_info", 2, "platform"),
    QuestionWork("sensitive_data", 2, "platform"),
    QuestionWork("injection_suspected", 2, "platform"),
)


def qs(*ks: int, source: Literal["platform", "request"] = "request") -> tuple[QuestionWork, ...]:
    return tuple(QuestionWork(f"q{i + 1}", k, source) for i, k in enumerate(ks))


def router(deadline_ms: int = 1000, budget: int | None = 1050) -> DecisionWork:
    return DecisionWork("router-default", ROUTER, deadline_ms, "profile_default", budget, "decision_member", 200)


def by_alias(graph: WorkGraph) -> dict[str, int]:
    out: dict[str, int] = {}
    for (alias, _kind), n in graph.calls.items():
        out[alias] = out.get(alias, 0) + n
    return out


async def test_twg1_router_only(kit: PlannerKit) -> None:
    graph = kit.planner.plan(kit.snapshot, principal(), decisions=[router()], generation=None)
    assert graph.total_calls == 20 and graph.streams == 0
    assert by_alias(graph) == {"deepseek-3.2": 10, "luna-6": 5, "haiku-5.5": 5}
    tight = kit.planner.plan(kit.snapshot, principal(), decisions=[router(900, 950)], generation=None)
    assert tight.total_calls == 15  # 400 + 590 > 900: no DeepSeek backup


async def test_twg2_full_default_turn(kit: PlannerKit) -> None:
    verify_qs = (QuestionWork("answers_question", 2, "platform"), QuestionWork("unsupported_claims", 2, "platform"))
    verify = DecisionWork("answer-verifier", verify_qs, 4000, "profile_default", None, "verification_member", 300)
    gen = GenerationWork(("sol-fast", "opus-deep", "glm-reason"), 200, None)
    graph = kit.planner.plan(kit.snapshot, principal(), decisions=[router(), verify], generation=gen)
    assert graph.total_calls == 28 and graph.streams == 1
    assert by_alias(graph) == {
        "deepseek-3.2": 10,
        "luna-6": 7,
        "haiku-5.5": 7,
        "sol-6.1": 4,
        "opus-5.5": 4,
        "glm-5.3": 4,
    }
    assert graph.calls[("haiku-5.5", "verification_member")] == 2
    assert graph.calls[("sol-6.1", "generation")] == 4


async def test_twg3_decisions_with_escalation(kit: PlannerKit) -> None:
    work = DecisionWork(
        "lightweight-model-decide", qs(2, 3, 4), 1000, "profile_default", 120_000, "decision_member", 50
    )
    graph = kit.planner.plan(kit.snapshot, principal(), decisions=[work], generation=None)
    assert graph.total_calls == 27
    off = DecisionWork("lightweight-model-decide", qs(2, 3, 4), 1000, "off", 120_000, "decision_member", 50)
    assert kit.planner.plan(kit.snapshot, principal(), decisions=[off], generation=None).total_calls == 3


async def test_twg4_turn_budget_excludes_router_plus(kit: PlannerKit) -> None:
    graph = kit.planner.plan(kit.snapshot, principal(), decisions=[router(1000, 1050)], generation=None)
    assert graph.total_calls == 20


async def test_escalation_filters_platform_questions_by_name(kit: PlannerKit) -> None:
    """router-default escalates only `intent` (escalation.questions, IMPL-068): router-plus reserves 1 question ×
    (DeepSeek 2 with its backup, 900 + 590 <= 2,500, + Luna 1 + Haiku 1) on top of stage 0's 20 calls."""
    graph = kit.planner.plan(kit.snapshot, principal(), decisions=[router(1000, 3000)], generation=None)
    assert graph.total_calls == 24
    request = DecisionWork("router-default", qs(2, 2), 1000, "profile_default", 3000, "decision_member", 200)
    both = kit.planner.plan(kit.snapshot, principal(), decisions=[request], generation=None)
    assert both.total_calls == 2 * 4 + 2 * 4  # request questions always pass the filter


async def test_twg5_hierarchical(kit: PlannerKit) -> None:
    work = DecisionWork("lightweight-model-decide", qs(32), 3000, "profile_default", 120_000, "decision_member", 50)
    assert kit.planner.plan(kit.snapshot, principal(), decisions=[work], generation=None).total_calls == 4
    wide = DecisionWork("router-default", qs(40), 3000, "profile_default", 120_000, "decision_member", 50)
    graph = kit.planner.plan(kit.snapshot, principal(), decisions=[wide], generation=None)
    assert graph.total_calls == 16
    assert by_alias(graph) == {"deepseek-3.2": 8, "luna-6": 4, "haiku-5.5": 4}


async def test_totals_are_consistent(kit: PlannerKit) -> None:
    gen = GenerationWork(("race-sol-glm",), 100, 600)
    graph = kit.planner.plan(kit.snapshot, principal(), decisions=[router()], generation=gen)
    assert graph.total_calls == 20 + 8  # race width 2 -> 4·2 generation calls
    assert 0 < graph.tokens_in + graph.tokens_out <= sum(graph.tokens_by_model.values())  # key/project <= models
    assert all(n > 0 for n in graph.calls.values())
    assert kit.planner.plan(kit.snapshot, principal(), decisions=[router()], generation=gen) == graph  # pure
