"""The admission input of a decision request: one `DecisionWork` (spec-11 §1.1; IMPL-027 (j))."""

from __future__ import annotations

from noema.core.protocols.decision import DecisionRequest
from noema.core.protocols.ops import DecisionWork, QuestionWork


def decision_work(req: DecisionRequest, *, escalate_budget_ms: int | None, input_tokens: int) -> DecisionWork:
    """The worst-case work of one decision: every question with its K and source (the planner derives the
    hierarchical 1 + 3 calls per member), the stage-0 deadline and the escalation budget."""
    return DecisionWork(
        profile_id=req.profile.id,
        questions=tuple(QuestionWork(q.name, len(q.options), q.source) for q in req.questions.questions),
        stage0_deadline_ms=req.deadline_ms,
        escalation=req.escalation,
        escalate_budget_ms=escalate_budget_ms,
        kind="verification_member" if req.origin == "verification" else "decision_member",
        input_tokens=input_tokens,
    )
