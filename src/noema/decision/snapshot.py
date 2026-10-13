"""The native `DecisionResult` of a run at one point in time, and the `include` view (spec-06 §12.1; IMPL-055).

The full result (members included) is what the row, `GET /v1/decisions/{id}` and L2 keep; the response view
omits `members` unless `include` asks for them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Literal

from noema.core.protocols.decision import DecisionRequest
from noema.core.values import JsonValue

from .state import Slot, StageInfo
from .usage import UsageSum

Status = Literal["quorum", "completed", "partial"]


def iso_ms(at: datetime) -> str:
    """ISO 8601 UTC with milliseconds and `Z`."""
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def usage_of(slots: Iterable[Slot]) -> UsageSum:
    total = UsageSum()
    for slot in slots:
        total.add_calls(slot.calls)
    return total


def status_of(answers: Sequence[Mapping[str, JsonValue]], final: bool) -> Status:
    """`completed` (no unavailable answer) or `partial` when final; `quorum` for a provisional S1."""
    if not final:
        return "quorum"
    return "partial" if any(a.get("type") == "unavailable" for a in answers) else "completed"


def provenance_quorum(answers: Sequence[Mapping[str, JsonValue]]) -> JsonValue:
    """The QuorumInfo of the answer with the smallest `responded` (ties: the first in answer order)."""
    best: Mapping[str, JsonValue] | None = None
    for a in answers:
        q = a.get("quorum")
        if isinstance(q, Mapping) and (best is None or _int(q.get("responded")) < _int(best.get("responded"))):
            best = q
    return dict(best) if best is not None else {"required": 1, "responded": 0, "refused": 0, "total": 1}


def _int(value: JsonValue) -> int:
    return value if isinstance(value, int) else 0


def stage_wire(stage: StageInfo, slots: Iterable[Slot]) -> dict[str, JsonValue]:
    """One `EscalationStage` (with CP-DEC-2's per-stage views and templates)."""
    return {
        "profile_id": stage.profile.id,
        "profile_version": str(stage.profile.version),
        "triggered_by": list(stage.triggered_by),
        "questions": list(stage.questions),
        "usage": usage_of(slots).wire(),
        "input_view_signature": stage.view_signature,
        "prompt_templates": {m.member_id: m.cfg.template for m in stage.members},
    }


def build_result(
    *,
    decision_id: str,
    req: DecisionRequest,
    trace_id: str,
    digest: str,
    seed: int,
    stages: Sequence[StageInfo],
    closed_stages: Iterable[int],
    all_slots: Sequence[tuple[int, Slot]],
    answers: list[dict[str, JsonValue]],
    final: bool,
    started_at: datetime,
    elapsed_s: float,
) -> dict[str, JsonValue]:
    """The full native result at this moment (usage: the calls completed or cancelled so far)."""
    stage0 = stages[0]
    closed = set(closed_stages)
    escalations: list[JsonValue] = [
        stage_wire(st, (s for i, s in all_slots if i == st.index)) for st in stages[1:] if st.index in closed
    ]
    return {
        "id": decision_id,
        "object": "decision",
        "created_at": int(started_at.timestamp()),
        "status": status_of(answers, final),
        "answers": list(answers),
        "provenance": {
            "trace_id": trace_id,
            "profile_id": stage0.profile.id,
            "profile_version": str(stage0.profile.version),
            "question_signature": req.questions.signature,
            "input_digest": digest,
            "input_view_signature": stage0.view_signature,
            "option_order_seed": seed,
            "prompt_templates": {m.member_id: m.cfg.template for m in stage0.members},
            "aggregation": {"method": "linear_pool"},
            "deadline_ms": req.deadline_ms,
            "quorum": provenance_quorum(answers),
            "cache": {"hit": False},
            "stages": escalations,
            "started_at": iso_ms(started_at),
            "completed_at": iso_ms(started_at + timedelta(seconds=max(0.0, elapsed_s))),
        },
        "usage": usage_of(s for _, s in all_slots).wire(),
    }


def response_view(result: Mapping[str, JsonValue], include: Iterable[str]) -> dict[str, JsonValue]:
    """The response form: `members` only with `include` `members` or `member_distributions` (IMPL-055)."""
    wanted = set(include)
    if "members" in wanted or "member_distributions" in wanted:
        return dict(result)
    answers = result.get("answers")
    out = dict(result)
    if isinstance(answers, list):
        out["answers"] = [
            {k: v for k, v in a.items() if k != "members"} if isinstance(a, Mapping) else a for a in answers
        ]
    return out
