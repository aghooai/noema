"""`TurnObject` payloads (openapi `TurnObject`): from a finished turn in memory (`stream: false`) and from the
stored rows (`GET /v1/turns/{id}`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from noema.core.protocols.decision import DecisionSnapshot
from noema.core.protocols.generation import AttemptRecord
from noema.core.protocols.storage import Row
from noema.core.values import JsonValue
from noema.turns.persist import usage_wire


def _json_list(value: object) -> list[JsonValue]:
    return list(value) if isinstance(value, list) else []


def _json_dict(value: object) -> dict[str, JsonValue] | None:
    return dict(value) if isinstance(value, Mapping) else None


def turn_object(
    row: Mapping[str, object],
    routing: Mapping[str, JsonValue],
    records: Sequence[AttemptRecord],
    decision: DecisionSnapshot | None,
    verification: DecisionSnapshot | None,
    *,
    text: str,
    session_id: str | None,
    seq: int | None,
) -> dict[str, JsonValue]:
    """The TurnObject of a turn that just finished (the full committed text, whatever the stored policy)."""
    return {
        "turn_id": str(row["id"]),
        "session_id": session_id,
        "seq": seq,
        "text": text,
        "decision": dict(decision.result) if decision is not None and decision.final else None,
        "routing": dict(routing),
        "attempts": [
            {
                "attempt_id": r.attempt_id,
                "route_id": r.route_id,
                "model": r.model,
                "outcome": r.outcome,
                "usage": usage_wire(r.usage, r.cost),
            }
            for r in records
        ],
        "verification": dict(verification.result) if verification is not None else None,
        "degradations": _json_list(row.get("degradations")),
        "usage": _json_dict(row.get("usage")) or {},
        "timings": _json_dict(row.get("timings")) or {},
    }


def stored_turn_object(
    turn: Row, attempts: Sequence[Row], decision: Row | None, verification: Row | None
) -> dict[str, JsonValue]:
    """The TurnObject of a stored turn (`text` is "" when the turn stored metadata only)."""
    output = turn.get("output_text")
    seq = turn.get("seq")
    session = turn.get("session_id")
    ordered = sorted(attempts, key=lambda a: str(a.get("started_at", "")))
    return {
        "turn_id": str(turn["id"]),
        "session_id": session if isinstance(session, str) else None,
        "seq": seq if isinstance(seq, int) else None,
        "text": output if isinstance(output, str) else "",
        "decision": _json_dict(decision.get("result")) if decision is not None else None,
        "routing": _json_dict(turn.get("routing")) or {},
        "attempts": [
            {
                "attempt_id": str(a["id"]),
                "route_id": str(a.get("route_id", "")),
                "model": str(a.get("model", "")),
                "outcome": str(a.get("outcome", "")),
                "usage": _json_dict(a.get("usage")) or {},
            }
            for a in ordered
        ],
        "verification": _json_dict(verification.get("result")) if verification is not None else None,
        "degradations": _json_list(turn.get("degradations")),
        "usage": _json_dict(turn.get("usage")) or {},
        "timings": _json_dict(turn.get("timings")) or {},
    }
