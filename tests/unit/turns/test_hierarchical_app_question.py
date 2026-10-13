"""T-HIER-T1, the orchestrator's side (spec-04 §3 "Hierarchical app questions", §13; IMPL-027, IMPL-069): a
20-option app question on `router-default` (L = 16) compiles hierarchical; non-blocking it never delays S1 and
becomes final in the single `decision.updated`; listed in `decisions.blocking` with `deadline_ms: 3000` S1 waits
for it and W = t_d0 + 3,000 + slack; with the default 1,000 ms it ends unavailable and the decided spec comes from
the other answers; the work graph reserves 1 + 3 calls per member for it.

The two-round answer itself is the decision engine's (spec-06 §19, T-HIER-1…10); here the fake engine answers it
from the scenario. Running this test on the real engine and the wire mock is the integrator's (C2)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.golden.sse_scenarios import document, router_rules, stream

from .harness import STICKY_SOL, USER_BODY, first, make_harness, run_turn, running

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"
APP_Q: dict[str, Any] = {
    "type": "choice",
    "name": "product_line",
    "instructions": "Which product line is the message about?",
    "choices": [{"value": f"line_{i:02d}"} for i in range(20)],
}


def _h(app_latency_ms: int):  # type: ignore[no-untyped-def]
    app_rule = {
        "match": {"prompt_contains": "Which product line"},
        "answer": {"dist": {"line_07": 0.8, "line_03": 0.2}},
        "latency_ms": app_latency_ms,
    }
    sol = stream(SOL, first_event_ms=100, first_text_ms=600, chunks=["ok"])
    h = make_harness(scenario=document("h", [sol], [app_rule], router_rules()))
    h.seed_session("ses_1", last_seq=1, sticky_spec=STICKY_SOL)
    return h


def _body(**decisions: Any) -> dict[str, Any]:
    return {**USER_BODY, "session_id": "ses_1", "decisions": {"questions": [APP_Q], **decisions}}


async def test_non_blocking_app_question_is_provisional_at_s1_and_final_in_the_update() -> None:
    h = _h(990)
    async with running(h):
        runner = await run_turn(h, _body())
    q = next(q for q in runner.p.questions.questions if q.name == "product_line")
    assert (q.method, q.source, q.commit_critical) == ("hierarchical", "request", False)
    at, completed = first(h, runner, "decision.completed")
    assert at == 930 and completed["decision"]["status"] == "quorum"
    _, updated = first(h, runner, "decision.updated")
    assert "product_line" in updated["changed"]
    # the reservation counted 1 + 3 calls per member for the hierarchical question
    calls = runner.p.reservation.graph.calls  # type: ignore[attr-defined]
    assert calls[("luna-6", "decision_member")] == 5 + 4
    assert calls[("haiku-5.5", "decision_member")] == 5 + 4
    assert calls[("deepseek-3.2", "decision_member")] == (5 + 4) * 2  # with its backup (400 + 590 <= 1000)


async def test_blocking_app_question_with_deadline_3000_moves_s1_and_w() -> None:
    h = _h(1500)
    async with running(h):
        runner = await run_turn(h, _body(blocking=["product_line"], deadline_ms=3000))
    assert first(h, runner, "decision.completed")[0] == 1500
    (drun,) = h.world.decisions.runs
    assert runner.t_d0 is not None
    escalate_until = drun._ctx.escalate_until
    assert escalate_until is not None and round((escalate_until.at - runner.t_d0) * 1000) == 3050
    assert "product_line" in runner.p.questions.commit_critical


async def test_blocking_app_question_past_the_default_deadline_is_unavailable() -> None:
    h = _h(1500)
    async with running(h):
        runner = await run_turn(h, _body(blocking=["product_line"]))
    at, completed = first(h, runner, "decision.completed")
    assert at == 1000
    answer = next(a for a in completed["decision"]["answers"] if a["name"] == "product_line")
    assert answer["type"] == "unavailable"
    assert runner.decided is not None and runner.decided.spec.generation_profile == "sol-fast"
    assert runner.decided.selection == "rule"  # decided from intent/complexity, not "decision unavailable"
