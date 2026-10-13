"""The spec-03 `noema.decision` row of a run, and its outbox payload (spec-06 §16; IMPL-022, IMPL-071). No SQL:
the INSERT is `DecisionRepo.insert` (db/repos)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Final

from noema.core.protocols.decision import DecisionContext, DecisionInput, DecisionRequest, Msg, VerificationInput
from noema.core.protocols.media import ContentPart
from noema.core.values import ContentPolicy, JsonValue

_STRICTNESS: Final[Mapping[ContentPolicy, int]] = {"full": 0, "redacted": 1, "metadata_only": 2}


def strictest(a: ContentPolicy, b: ContentPolicy) -> ContentPolicy:
    """The more restrictive of two content policies (a laxer request is clamped)."""
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


def _part(part: ContentPart, redact: Callable[[str], str] | None) -> dict[str, JsonValue]:
    if part.kind == "text":
        text = part.text or ""
        return {"type": "input_text", "text": redact(text) if redact else text}
    if part.kind == "json":
        return {"type": "input_json", "json": _json(part.json, redact)}
    kind = "input_image" if part.kind == "image" else "input_file"
    return {"type": kind, "file_id": part.file.file_id if part.file is not None else None}


def _json(value: JsonValue | object, redact: Callable[[str], str] | None) -> JsonValue:
    if isinstance(value, str):
        return redact(value) if redact else value
    if isinstance(value, Mapping):
        return {str(k): _json(v, redact) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json(v, redact) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)


def stored_input(value: DecisionInput, redact: Callable[[str], str] | None = None) -> JsonValue:
    """The `input` column: the input as JSON (media parts by file id, never bytes); `redact` rewrites every text."""
    if isinstance(value, str):
        return redact(value) if redact else value
    if isinstance(value, VerificationInput):
        return {
            "type": "verification",
            "question": redact(value.question) if redact else value.question,
            "answers": [{"id": i, "text": redact(t) if redact else t} for i, t in value.answers],
        }
    if isinstance(value, tuple):
        return [_message(m, redact) for m in value]
    return _json(value, redact)


def _message(msg: Msg, redact: Callable[[str], str] | None) -> JsonValue:
    return {"role": msg.role, "content": [_part(p, redact) for p in msg.parts]}


def decision_row(
    ctx: DecisionContext,
    req: DecisionRequest,
    *,
    decision_id: str,
    created_at: datetime,
    status: str,
    result: Mapping[str, JsonValue],
    cache_hit: bool,
    policy: ContentPolicy,
    redact: Callable[[str], str],
) -> dict[str, object]:
    """The row (spec-03 column names). `input` follows `policy` (clamped to `ctx.content_policy`): full, the
    redacted text, or NULL; `expires_at` is the data class's retention."""
    effective = strictest(ctx.content_policy, policy)
    stored: JsonValue = None
    if effective != "metadata_only":
        stored = stored_input(req.input, redact if effective == "redacted" else None)
    prov = result.get("provenance")
    view_sig = prov.get("input_view_signature") if isinstance(prov, Mapping) else None
    digest = prov.get("input_digest") if isinstance(prov, Mapping) else None
    retain = ctx.snapshot.platform.data_classes.for_class(ctx.data_class).retain_days
    return {
        "id": decision_id,
        "tenant_id": ctx.principal.tenant_id,
        "project_id": ctx.principal.project_id,
        "created_at": created_at,
        "principal_id": ctx.principal.principal_id,
        "turn_id": ctx.turn_id,
        "session_id": ctx.session_id,
        "purpose": ctx.purpose,
        "origin": req.origin,
        "output_format": req.output_format,
        "profile_id": req.profile.id,
        "profile_version": str(req.profile.version),
        "question_signature": req.questions.signature,
        "input_view_signature": view_sig,
        "input_digest": digest,
        "status": status,
        "cache_hit": cache_hit,
        "safety_identifier": pg_text(req.safety_identifier),
        "questions": pg_text([_json(q.wire, None) for q in req.questions.questions]),
        "input": pg_text(stored),
        "result": pg_text(result),
        "expires_at": created_at + timedelta(days=retain),
    }


def pg_text(value: object) -> object:
    """`value` with every U+0000 (in strings and keys) replaced by U+FFFD: Postgres text and jsonb cannot store it,
    so a stored NUL would fail the insert and then poison the outbox (Wave E)."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd") if "\x00" in value else value
    if isinstance(value, Mapping):
        return {str(k).replace("\x00", "\ufffd"): pg_text(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [pg_text(v) for v in value]
    return value


def outbox_payload(row: Mapping[str, object]) -> dict[str, JsonValue]:
    """The `decision_row` outbox payload: the row without its key columns, timestamps as RFC 3339 UTC."""
    out: dict[str, JsonValue] = {}
    for key, value in row.items():
        if key in ("tenant_id", "id"):
            continue
        if isinstance(value, datetime):
            out[key] = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        else:
            out[key] = _json(value, None)
    return out
