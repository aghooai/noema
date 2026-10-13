"""Quorum, classification, adequacy and the parts every answer shares (spec-06 §4.4, §7.3; IMPL-001, IMPL-058).

`Qcount(X)` = distinct model aliases among non-optional members of X with weight > 0 (a repeated model counts
once). Classification is evaluated in the §7.3 order; for a provisional answer (an S1 snapshot) pending members
count as `late`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Final, Literal

from noema.core.protocols.decision import CompiledQuestion
from noema.core.values import JsonValue, Modality

from .state import MemberStatus, Slot
from .usage import UsageSum

Kind = Literal["scored", "refusal", "unavailable"]
Reason = Literal["deadline_exceeded", "no_quorum", "all_members_failed", "no_capable_member"]
ADEQUATE: Final[Mapping[Modality, frozenset[str]]] = {
    "image": frozenset({"native"}),
    "document": frozenset({"native", "derived_text"}),
    "text": frozenset({"native"}),
}


def status_of(slot: Slot) -> MemberStatus:
    """The slot's status; a pending member counts as `late` (provisional answers)."""
    return slot.status or "late"


def qcount(slots: Iterable[Slot]) -> int:
    """Distinct model aliases among the quorum-counting members of `slots`."""
    return len({s.member.cfg.model for s in slots if s.member.counts})


def adequate(ok: Sequence[Slot], modalities: frozenset[Modality]) -> bool:
    """For every media modality of the input, at least one ok member has an adequate view of it (spec-06 §4.4)."""
    return all(any(s.member.by_modality.get(m) in ADEQUATE[m] for s in ok) for m in modalities)


def classify(slots: Sequence[Slot], q_req: int, modalities: frozenset[Modality]) -> tuple[Kind, Reason | None]:
    """spec-06 §7.3, rules 1-7 in order."""
    ok = [s for s in slots if status_of(s) == "ok"]
    if qcount(ok) >= q_req:
        return ("scored", None) if adequate(ok, modalities) else ("unavailable", "no_capable_member")
    if qcount(s for s in slots if status_of(s) in ("ok", "refused")) >= q_req:
        return "refusal", None
    ran = [s for s in slots if status_of(s) != "skipped"]
    if qcount(ran) < q_req:
        return "unavailable", "no_capable_member"
    if all(status_of(s) == "error" for s in ran):
        return "unavailable", "all_members_failed"
    if any(status_of(s) == "timeout" for s in ran):
        return "unavailable", "deadline_exceeded"
    return "unavailable", "no_quorum"


def quorum_wire(slots: Sequence[Slot], q_req: int) -> dict[str, JsonValue]:
    """`QuorumInfo`: required, #ok, #refused, every member of the stage (skipped and optional included)."""
    statuses = [status_of(s) for s in slots]
    return {
        "required": q_req,
        "responded": statuses.count("ok"),
        "refused": statuses.count("refused"),
        "total": len(slots),
    }


def latency_ms(slot: Slot, now: float) -> int:
    """Winning call end (or close, or `now` while pending) minus the primary start; 0 when it never ran."""
    start = slot.started_at
    if start is None:
        return 0
    end = slot.ended_at if slot.ended_at is not None else now
    return max(0, math.floor((end - start) * 1000 + 1e-6))


def vote_base(slot: Slot, now: float, region: str, *, with_usage: bool) -> dict[str, JsonValue]:
    """The `MemberVote` fields of every status (weight 0; the scored builders add distribution and weight)."""
    m = slot.member
    status = status_of(slot)
    vote: dict[str, JsonValue] = {
        "member_id": m.member_id,
        "provider": m.model.adapter,
        "model": m.model.id,
        "region": slot.region or region,
        "elicitation": m.cfg.elicitation,
        "input_view": m.view_kind,
        "status": status,
        "weight": 0,
        "latency_ms": latency_ms(slot, now),
    }
    if slot.model_revision:
        vote["model_revision"] = slot.model_revision
    if status == "skipped":
        vote["skip_reason"] = slot.skip_reason or "input_view"
    if slot.error is not None and status == "error":
        vote["error"] = {"code": slot.error[0], "message": slot.error[1]}
    if with_usage and any(c.outcome in ("completed", "cancelled") for c in slot.calls):  # failed calls bill nothing
        vote["usage"] = UsageSum().add_calls(slot.calls).wire()
    return vote


def unscored_answer(
    q: CompiledQuestion,
    kind: Literal["refusal", "unavailable"],
    reason: Reason | None,
    quorum: dict[str, JsonValue],
    members: list[JsonValue] | None,
) -> dict[str, JsonValue]:
    """A `RefusalAnswer` or `UnavailableAnswer` (no candidates, no groups)."""
    answer: dict[str, JsonValue] = {"type": kind, "name": q.name}
    if kind == "unavailable":
        answer["reason"] = reason or "no_quorum"
    answer["quorum"] = quorum
    if members is not None:
        answer["members"] = members
    return answer
