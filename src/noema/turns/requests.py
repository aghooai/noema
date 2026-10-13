"""The router and verification `DecisionRequest`s of a turn (spec-06 §2.4, §10; IMPL-023, IMPL-024, IMPL-045,
IMPL-069)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from noema.core.clock import Deadline
from noema.core.protocols.decision import DecisionContext, DecisionRequest, Msg, ProfileRef, VerificationInput
from noema.core.protocols.media import ContentPart
from noema.turns.context.history import decision_context
from noema.turns.preflight import Preflight
from noema.turns.prompt.builder import media_kind

if TYPE_CHECKING:
    from noema.turns.runner import TurnRunner

INCLUDE_MEMBERS: Final[frozenset[Literal["members", "member_distributions", "stages"]]] = frozenset({"members"})


def _context(
    runner: TurnRunner,
    purpose: str,
    deadline: Deadline,
    escalate_until: Deadline | None,
    escalate_slack_ms: int | None = None,
) -> DecisionContext:
    p = runner.p
    return DecisionContext(
        principal=p.principal,
        tenant=p.tenant,
        snapshot=p.snapshot,
        deadline=deadline,
        escalate_until=escalate_until,
        escalate_slack_ms=escalate_slack_ms,
        reservation=p.reservation,
        purpose="router" if purpose == "router" else "verification",
        store=p.request.store,
        data_class=p.data_class,
        content_policy=p.store_content,
        trace_id=p.trace_id,
        turn_id=p.turn_id,
        session_id=p.session_id,
    )


def router_request(
    runner: TurnRunner, pf: Preflight, *, escalate_until: float
) -> tuple[DecisionContext, DecisionRequest]:
    """The router decision: `decisions.profile ?? routing.router_profile` with the router profile's questions,
    the compact decision context (the last 2 committed turns, ≤ 2,000 tokens) and the current user message;
    `escalate_until` = W measured from the entry (monotonic, IMPL-023), and `escalate_slack_ms` = the speculation
    slack, so the engine counts W from its own t0 (the instant the orchestrator reads as t_d0)."""
    p = runner.p
    msgs: list[Msg] = []
    for user, assistant in decision_context(p.recent):
        msgs.append(Msg("user", (ContentPart(0, "text", None, text=user),)))
        msgs.append(Msg("assistant", (ContentPart(0, "text", None, text=assistant),)))
    msgs.append(Msg("user", pf.parts))
    slack_ms = p.snapshot.routing.speculation.slack_ms or 0
    ctx = _context(runner, "router", p.deadline, Deadline(escalate_until, runner.clock), slack_ms)
    req = DecisionRequest(
        profile=ProfileRef(p.profile.id, p.profile.version),
        questions=p.questions,
        input=tuple(msgs),
        deadline_ms=p.deadline_ms,
        min_quorum=p.min_quorum,
        escalation="profile_default",
        include=INCLUDE_MEMBERS,
        origin="turn",
        output_format="native",
    )
    return ctx, req


def verifier_question(parts: tuple[ContentPart, ...], last_item_indexes: frozenset[int]) -> str:
    """The text parts of the final user message, media as their labels (`[doc-1: document]`, IMPL-024)."""
    lines: list[str] = []
    for part in parts:
        if part.index not in last_item_indexes:
            continue
        if part.kind == "text" and part.text:
            lines.append(part.text)
        elif part.kind in ("image", "file"):
            lines.append(f"[{part.label or 'media'}: {media_kind(part)}]")
    return "\n".join(lines)


def verification_request(runner: TurnRunner) -> tuple[DecisionContext, DecisionRequest]:
    """`routing.verification`: the verifier profile, the selected questions, `VerificationInput(question,
    (("A", committed text),))`, deadline `min(now + verifier deadline_ms, ctx.deadline)`, no escalation."""
    p = runner.p
    verification = p.snapshot.routing.verification
    assert verification is not None
    assert verification.decision_profile is not None
    assert p.verify_questions is not None
    profile = p.snapshot.decision_profile(verification.decision_profile)
    last_item = max((part.item for part in p.request.parts), default=0)
    last = frozenset(part.index for part in p.request.parts if part.item == last_item)
    parts = runner.preflight.parts if runner.preflight is not None else ()
    question = verifier_question(parts, last)
    deadline = Deadline(min(runner.now() + profile.deadline_ms / 1000, p.deadline.at), runner.clock)
    ctx = _context(runner, "verification", deadline, None)
    req = DecisionRequest(
        profile=ProfileRef(profile.id, profile.version),
        questions=p.verify_questions,
        input=VerificationInput(question=question, answers=(("A", "".join(runner.text)),)),
        deadline_ms=profile.deadline_ms,
        min_quorum=profile.min_quorum,
        escalation="profile_default",
        include=INCLUDE_MEMBERS,
        origin="verification",
        output_format="native",
    )
    return ctx, req
