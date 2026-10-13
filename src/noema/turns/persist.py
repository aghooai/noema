"""Turn persistence (spec-04 §10; spec-03 §2; IMPL-018, IMPL-020, IMPL-022, IMPL-071).

Row mapping only (no SQL): the `turn`, `attempt` and `turn_file` rows, the stored content under the turn's
`content_policy`, the usage lines of the attempts, and the finalize transaction with its outbox fallback
(`session_pending` first, then a `turn_finalize` record; the turn still completes).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal, cast

from noema.core.clock import Clock, deadline_scope
from noema.core.principal import Principal
from noema.core.protocols.generation import AttemptRecord
from noema.core.protocols.media import ContentPart
from noema.core.protocols.ops import UsageLine
from noema.core.protocols.storage import Database, LeaseLost, Outbox, OutboxRecord, Row, SessionLease
from noema.core.values import ContentPolicy, Cost, JsonValue, UsageRecord
from noema.turns.context.history import part_text
from noema.turns.coordination import Coordinator

log = logging.getLogger("noema.turns.persist")

_MARKER: Final = re.compile(r"\[REDACTED:([a-z0-9_]+)\]")
_USAGE_KEYS: Final = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
)
FinalizeOutcome = Literal["stored", "lease_lost", "outboxed", "skipped"]
# Bound on the finalize transaction: a hung (not refused) Postgres, e.g. a black-holed socket, a failover or a lock
# wait, is a DB failure once it passes (outbox; its apply is idempotent), so the turn still completes (Wave E)
FINALIZE_BUDGET_S: Final = 5.0


def line_id(*parts: str) -> str:
    """spec-11 §2 deterministic id: `sha256(":".join(parts)).hexdigest()[:26]`."""
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:26]


def usage_wire(usage: UsageRecord, cost: Cost) -> dict[str, JsonValue]:
    """The openapi `Usage` object of one usage record and its cost."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cost_usd": round(float(cost.usd), 6),
        "cost_estimated": cost.estimated,
        "tokens_estimated": usage.tokens_estimated,
    }


def usage_sum(parts: Sequence[Mapping[str, JsonValue]]) -> dict[str, JsonValue]:
    """Σ of `Usage` objects (cost rounded per part, then summed; estimated if any part is)."""
    totals: dict[str, int] = dict.fromkeys(_USAGE_KEYS, 0)
    cost = 0.0
    cost_estimated = tokens_estimated = False
    for p in parts:
        for k in _USAGE_KEYS:
            v = p.get(k, 0)
            totals[k] += v if isinstance(v, int) and not isinstance(v, bool) else 0
        raw = p.get("cost_usd", 0.0)
        cost += float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0.0
        cost_estimated = cost_estimated or p.get("cost_estimated") is True
        tokens_estimated = tokens_estimated or p.get("tokens_estimated") is True
    out: dict[str, JsonValue] = {}
    out.update(totals)
    out.update(cost_usd=round(cost, 6), cost_estimated=cost_estimated, tokens_estimated=tokens_estimated)
    return out


def attempt_line(record: AttemptRecord, *, committed: bool) -> Literal["generation", "waste"]:
    """IMPL-020: the committed attempt is `generation`, every other attempt `waste`."""
    return "generation" if committed and record.outcome == "committed" else "waste"


def usage_lines(
    records: Sequence[AttemptRecord],
    *,
    principal: Principal,
    region: str,
    turn_id: str,
    session_id: str | None,
) -> list[UsageLine]:
    """One `UsageLine` per attempt (`h(f"{attempt_id}:{line}")`)."""
    out = []
    for r in records:
        line = attempt_line(r, committed=True)
        out.append(
            UsageLine(
                id=line_id(r.attempt_id, line),
                tenant_id=principal.tenant_id,
                project_id=principal.project_id,
                line=line,
                model_id=r.model,
                region=region,
                usage=r.usage,
                cost=r.cost,
                turn_id=turn_id,
                attempt_id=r.attempt_id,
                session_id=session_id,
            )
        )
    return out


def attempt_rows(records: Sequence[AttemptRecord]) -> list[dict[str, object]]:
    """The `attempt` rows (spec-03 §2); `turn_id` is filled by the repository."""
    return [
        {
            "id": r.attempt_id,
            "route_id": r.route_id,
            "model": r.model,
            "spec_signature": r.spec_signature,
            "outcome": r.outcome,
            "cancel_reason": r.cancel_reason,
            "finish_reason": r.finish_reason,
            "error_code": r.error_code,
            "usage": usage_wire(r.usage, r.cost),
            "cost_usd": r.cost.usd,
            "cost_estimated": r.cost.estimated,
            "started_at": r.started_at,
            "ended_at": r.ended_at,
        }
        for r in records
    ]


def stored_part(part: ContentPart) -> dict[str, JsonValue]:
    """One part in the stored `input_parts` format (see `turns.context.history`)."""
    if part.kind == "text":
        return {"type": "text", "text": part.text or ""}
    if part.kind == "json":
        return {"type": "json", "json": part.json}
    file_id = part.file.file_id if part.file is not None else ""
    return {"type": "image" if part.kind == "image" else "file", "file_id": file_id}


def _redact_json(value: JsonValue, redact: Callable[[str], str]) -> JsonValue:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_json(v, redact) for v in value]
    if isinstance(value, dict):
        return {k: _redact_json(v, redact) for k, v in value.items()}
    return value


def _spans(text: str) -> list[JsonValue]:
    return [{"start": m.start(), "end": m.end(), "reason": m.group(1)} for m in _MARKER.finditer(text)]


def pg_text(value: JsonValue) -> JsonValue:
    """`value` with every U+0000 (in strings and keys) replaced by U+FFFD: Postgres text and jsonb cannot store it,
    so a stored NUL would fail the finalize and then poison the outbox (Wave E). One code point for one, so the
    redaction spans stay valid."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd") if "\x00" in value else value
    if isinstance(value, Mapping):
        return {str(k).replace("\x00", "\ufffd"): pg_text(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [pg_text(v) for v in value]
    return value


def stored_content(
    parts: Sequence[ContentPart], output_text: str, policy: ContentPolicy, redact: Callable[[str], str]
) -> tuple[list[JsonValue] | None, str | None, dict[str, JsonValue]]:
    """`(input_parts, output_text, redactions)` under the content policy (IMPL-018): full as is; redacted with the
    PII recognizers (spans in code points over the user text = the text and JSON parts joined by newlines, and over
    the output); metadata_only stores neither."""
    empty: dict[str, JsonValue] = {"user": [], "assistant": []}
    if policy == "metadata_only":
        return None, None, empty
    stored = [cast("dict[str, JsonValue]", pg_text(stored_part(p))) for p in parts]
    output_text = cast("str", pg_text(output_text))
    if policy == "full":
        return list(stored), output_text, empty
    redacted: list[JsonValue] = []
    for p in stored:
        if p["type"] == "text":
            redacted.append({"type": "text", "text": redact(str(p["text"]))})
        elif p["type"] == "json":
            redacted.append({"type": "json", "json": _redact_json(p["json"], redact)})
        else:
            redacted.append(p)
    user = "\n".join(part_text(p) for p in redacted if isinstance(p, dict) and p.get("type") in ("text", "json"))
    out = redact(output_text)
    return redacted, out, {"user": _spans(user), "assistant": _spans(out)}


def _json(value: object) -> JsonValue:
    """A row value as an outbox payload value (RFC 3339 UTC timestamps, Decimals as strings)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json(v) for v in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def finalize_payload(
    lease: SessionLease | None,
    turn: Row,
    attempts: Sequence[Row],
    decisions: Sequence[Row],
    turn_files: Sequence[Row],
    sticky_spec: Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """The `turn_finalize` outbox payload (spec-11 §3); the rows carry their keys (session_id/seq, turn_id)."""
    turn_id = str(turn["id"])
    full_turn = dict(turn)
    if lease is not None:
        full_turn.update(session_id=lease.session_id, seq=lease.last_seq + 1)
    return {
        "lease": None
        if lease is None
        else {"session_id": lease.session_id, "turn_id": lease.turn_id, "last_seq": lease.last_seq},
        "turn": _json(full_turn),
        "attempts": [_json({**a, "turn_id": turn_id}) for a in attempts],
        "decisions": [_json(d) for d in decisions],
        "turn_files": [_json({**f, "turn_id": turn_id}) for f in turn_files],
        "sticky_spec": _json(sticky_spec),
    }


async def finalize(
    db: Database,
    outbox: Outbox,
    coordinator: Coordinator,
    principal: Principal,
    *,
    clock: Clock,
    lease: SessionLease | None,
    turn: Row,
    attempts: Sequence[Row],
    decisions: Sequence[Row],
    turn_files: Sequence[Row],
    sticky_spec: Mapping[str, JsonValue] | None,
) -> FinalizeOutcome:
    """The finalize transaction (spec-04 §10 step 2). `LeaseLost` rolls back and stores nothing; a DB failure
    sets `session_pending`, then puts a `turn_finalize` record (the turn still completes); so does a transaction that
    passes `FINALIZE_BUDGET_S`. Publishes `session_released` after a commit with a session."""
    try:
        async with deadline_scope(clock, clock.monotonic() + FINALIZE_BUDGET_S) as scope, db.uow(principal) as uow:
            if lease is not None:
                await uow.sessions.finalize_turn(lease, turn, attempts, decisions, turn_files, sticky_spec)
            else:
                await uow.turns.insert(turn, attempts, decisions, turn_files)
        if scope.cancelled_caught:
            raise TimeoutError(f"the finalize transaction passed its {FINALIZE_BUDGET_S:.0f} s budget")
    except LeaseLost:
        log.warning("session_lease_lost: turn %s not finalized into its session", turn.get("id"))
        return "lease_lost"
    except Exception:
        log.error("turn finalize failed; queued in the outbox (turn %s)", turn.get("id"), exc_info=True)
        if lease is not None:
            await coordinator.mark_session_pending(lease.session_id, lease.turn_id)
        payload = finalize_payload(lease, turn, attempts, decisions, turn_files, sticky_spec)
        outbox.put(
            OutboxRecord(
                kind="turn_finalize",
                id=str(turn["id"]),
                tenant_id=principal.tenant_id,
                payload=payload,
                session_id=lease.session_id if lease is not None else None,
            )
        )
        return "outboxed"
    if lease is not None:
        await coordinator.publish_released(principal.tenant_id, lease.session_id, lease.last_seq + 1)
    return "stored"
